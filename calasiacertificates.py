# =============================================================================
# CAL-ASIA CALIBRATION CERTIFICATE PORTAL
# Python Flask + PostgreSQL + Dropbox Auto-Sync
# =============================================================================
# ROLES:
#   admin    -> Full control of everything
#   manager  -> Uploads PDFs to Dropbox, triggers sync, views reports
#   customer -> Views their own instruments and certificates only
# =============================================================================

import os
import re
import json
from datetime import datetime, timedelta, date
from functools import wraps
from typing import Any

from flask import (Flask, render_template, request, redirect,
                   url_for, flash, jsonify, abort, session)
from flask_login import (LoginManager, UserMixin, login_user,
                         logout_user, login_required, current_user)
import psycopg2
import psycopg2.extras
import bcrypt
from dotenv import load_dotenv

load_dotenv(override=True)


def _get_env(key: str, default: str = '') -> str:
    """Always re-read .env so token/config changes take effect without restart."""
    from dotenv import dotenv_values
    import pathlib
    env_path = pathlib.Path(__file__).parent / '.env'
    fresh = dotenv_values(env_path)
    # fresh values take priority; fall back to os.environ (e.g. system env vars)
    return fresh.get(key) or os.environ.get(key, default)

# =============================================================================
# APP SETUP
# =============================================================================
app = Flask(__name__)

@app.route("/")
def home():
    return render_template("login.html")

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'calasia-default-secret-2025')

# =============================================================================
# DATABASE HELPERS
# =============================================================================
DATABASE_URL = os.getenv("DATABASE_URL")

print("DATABASE_URL =", DATABASE_URL)


def get_db():
    return psycopg2.connect(DATABASE_URL)


def db_query(sql: str, params: Any = None, fetch: str = 'all') -> Any:
    """Run a SELECT and return results.
    fetch='one' → RealDictRow | None
    fetch='all' → list[RealDictRow]
    Return type is Any so type-checkers don't flag dict-key access.
    """
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        if fetch == 'one':
            return cur.fetchone()
        return cur.fetchall() or []
    finally:
        conn.close()


def db_execute(sql, params=None):
    """Run INSERT/UPDATE/DELETE."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def db_execute_returning(sql, params=None):
    """Run INSERT RETURNING and return the row id."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        result = cur.fetchone()
        conn.commit()
        return result[0] if result else None
    finally:
        conn.close()

# =============================================================================
# AUTH
# =============================================================================
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'warning'


class User(UserMixin):
    def __init__(self, data):
        self.id = data['id']
        self.username = data['username']
        self.full_name = data['full_name']
        self.role = data['role']
        self.customer_id = data.get('customer_id')

    def is_admin(self):
        return self.role == 'admin'

    def is_manager(self):
        return self.role == 'manager'

    def is_customer(self):
        return self.role == 'customer'


@login_manager.user_loader
def load_user(user_id):
    row = db_query("SELECT * FROM users WHERE id = %s AND is_active = TRUE",
                   (user_id,), fetch='one')
    return User(row) if row else None


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not current_user.is_authenticated or current_user.role not in roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator


def cert_status(due_date):
    """Calculate certificate status from due date."""
    today = date.today()
    if due_date < today:
        return 'overdue'
    elif due_date <= today + timedelta(days=30):
        return 'due_soon'
    return 'calibrated'

# =============================================================================
# DROPBOX SYNC ENGINE
# =============================================================================
_last_sync_stats = {}
_last_sync_time = None
_sync_in_progress = False   # True while a manual/background sync is running
_sync_last_result = None    # dict with 'stats', 'error', 'finished_at' from most recent run


def _get_dropbox_client():
    """Return an authenticated Dropbox client.
    Prefers OAuth2 refresh token (permanent, never expires).
    Falls back to legacy DROPBOX_ACCESS_TOKEN if refresh token not set.
    Returns None if no credentials are configured.
    """
    try:
        import dropbox as _dbx
        refresh_token = _get_env('DROPBOX_REFRESH_TOKEN')
        app_key       = _get_env('DROPBOX_APP_KEY')
        app_secret    = _get_env('DROPBOX_APP_SECRET')
        if refresh_token and app_key and app_secret:
            return _dbx.Dropbox(
                oauth2_refresh_token=refresh_token,
                app_key=app_key,
                app_secret=app_secret
            )
        # Legacy fallback
        token = _get_env('DROPBOX_ACCESS_TOKEN')
        if token and token.strip() and token.strip() != 'your_dropbox_token_here':
            return _dbx.Dropbox(token.strip())
        return None
    except ImportError:
        return None


def run_dropbox_sync(triggered_by='scheduler'):
    """Wrapper used by both the scheduler and the async manual trigger."""
    global _sync_in_progress, _sync_last_result
    """
    Core sync function.
    Pulls all PDFs from Dropbox /Calibration_Certificates/ folder.
    Validates filenames, matches to instruments, inserts certificates.
    Returns (stats_dict, error_message_or_None)
    """
    global _last_sync_stats, _last_sync_time  # noqa: F811 (redeclared above for clarity)

    token = _get_env('DROPBOX_ACCESS_TOKEN')
    folder = _get_env('DROPBOX_FOLDER') or '/Calibration_Certificates'
    stats = {'total': 0, 'success': 0, 'duplicates': 0, 'unmatched': 0, 'errors': 0}

    if not token or token.strip() == 'your_dropbox_token_here':
        msg = 'Dropbox token not configured. Add DROPBOX_ACCESS_TOKEN to .env file.'
        db_execute("""INSERT INTO sync_logs
                      (total_files, success_count, duplicate_count, unmatched_count,
                       error_count, triggered_by, message)
                      VALUES (0,0,0,0,0,%s,%s)""", (triggered_by, msg))
        return stats, msg

    try:
        import dropbox as dbx_lib
        import dropbox.exceptions  # noqa: F401 — imported for explicit availability
        dbx = dbx_lib.Dropbox(token)

        # --- List all PDF files recursively ---
        files = []
        try:
            result = dbx.files_list_folder(folder, recursive=True)  # type: ignore[union-attr]
            while True:
                for entry in result.entries:  # type: ignore[union-attr]
                    if (hasattr(entry, 'name') and
                            entry.name.lower().endswith('.pdf')):
                        files.append({
                            'name': entry.name,
                            'path': entry.path_lower,
                            'id': entry.id
                        })
                if result.has_more:  # type: ignore[union-attr]
                    result = dbx.files_list_folder_continue(result.cursor)  # type: ignore[union-attr]
                else:
                    break
        except dbx_lib.exceptions.ApiError as e:
            err = f'Dropbox folder error: {str(e)}'
            db_execute("""INSERT INTO sync_logs
                          (total_files,success_count,duplicate_count,unmatched_count,
                           error_count,triggered_by,message)
                          VALUES (0,0,0,0,0,%s,%s)""", (triggered_by, err))
            return stats, err

        stats['total'] = len(files)

        # --- Process each file ---
        for file in files:
            fname = file['name']
            fpath = file['path']
            try:
                # STEP A: Validate filename format → SERIAL_CERTNUM.pdf
                # Split on FIRST underscore only — cert number may contain underscores
                # e.g.  B012036_CAL2026_001.pdf  →  serial=B012036, cert=CAL2026_001
                # e.g.  B012036_CAL2026-001.pdf  →  serial=B012036, cert=CAL2026-001
                if '_' not in fname or not fname.lower().endswith('.pdf'):
                    # Only insert if not already recorded as unmatched
                    already_unmatched = db_query(
                        "SELECT id FROM unmatched_files WHERE file_name=%s AND resolved=FALSE",
                        (fname,), fetch='one')
                    if not already_unmatched:
                        db_execute("""INSERT INTO unmatched_files
                                      (file_name, dropbox_path, reason)
                                      VALUES (%s, %s, %s)""",
                                   (fname, fpath, 'Invalid filename. Use: SERIAL_CERTNUM.pdf'))
                    stats['errors'] += 1
                    continue

                # Split on FIRST underscore only
                base_name = fname[:-4]  # strip .pdf
                first_underscore = base_name.index('_')
                serial_number = base_name[:first_underscore].strip()
                cert_number   = base_name[first_underscore + 1:].strip()

                if not serial_number or not cert_number:
                    already_unmatched = db_query(
                        "SELECT id FROM unmatched_files WHERE file_name=%s AND resolved=FALSE",
                        (fname,), fetch='one')
                    if not already_unmatched:
                        db_execute("""INSERT INTO unmatched_files
                                      (file_name, dropbox_path, reason)
                                      VALUES (%s, %s, %s)""",
                                   (fname, fpath, 'Invalid filename. Use: SERIAL_CERTNUM.pdf'))
                    stats['errors'] += 1
                    continue

                print(f"[SYNC] Processing: {fname} | Serial={serial_number} | Cert={cert_number}")

                # STEP C: Find instrument by serial number (trim whitespace from DB too)
                instrument = db_query(
                    "SELECT id FROM instruments WHERE LOWER(TRIM(serial_number))=LOWER(TRIM(%s))",
                    (serial_number,), fetch='one')

                if not instrument:
                    print(f"[SYNC] No match for serial: '{serial_number}'")
                    # Only insert unmatched if not already recorded
                    already_unmatched = db_query(
                        "SELECT id FROM unmatched_files WHERE file_name=%s AND resolved=FALSE",
                        (fname,), fetch='one')
                    if not already_unmatched:
                        db_execute("""INSERT INTO unmatched_files
                                      (file_name, dropbox_path, serial_number, reason)
                                      VALUES (%s, %s, %s, %s)""",
                                   (fname, fpath, serial_number,
                                    f'No instrument with serial number "{serial_number}" found'))
                    stats['unmatched'] += 1
                    continue

                print(f"[SYNC] Matched instrument id={instrument['id']} for serial={serial_number}")

                # STEP D: Check duplicate in certificates
                existing = db_query(
                    """SELECT id FROM certificates
                       WHERE LOWER(certificate_number)=LOWER(%s)
                          OR dropbox_file_path=%s""",
                    (cert_number, fpath), fetch='one')

                if existing:
                    # Try to get a shared link to backfill missing links on existing certs
                    try:
                        import dropbox as _dbx_lib
                        _existing_link = None
                        try:
                            _lnk = dbx.sharing_create_shared_link_with_settings(fpath)
                            _existing_link = _lnk.url.replace('?dl=0', '?raw=1')  # type: ignore[union-attr]
                        except Exception:
                            try:
                                _lnks = dbx.sharing_list_shared_links(path=fpath)
                                if _lnks.links:  # type: ignore[union-attr]
                                    _existing_link = _lnks.links[0].url.replace('?dl=0', '?raw=1')  # type: ignore[union-attr,index]
                            except Exception:
                                pass
                        if _existing_link:
                            cert_row = db_query(
                                "SELECT id, dropbox_shared_link FROM certificates WHERE id=%s",
                                (existing['id'],), fetch='one')
                            if cert_row and not cert_row['dropbox_shared_link']:
                                db_execute(
                                    "UPDATE certificates SET dropbox_shared_link=%s, dropbox_file_path=%s WHERE id=%s",
                                    (_existing_link, fpath, cert_row['id'])
                                )
                    except Exception:
                        pass
                    # Only insert duplicate record if not already recorded
                    already_dup = db_query(
                        "SELECT id FROM duplicate_files WHERE file_name=%s AND resolved=FALSE",
                        (fname,), fetch='one')
                    if not already_dup:
                        db_execute("""INSERT INTO duplicate_files
                                      (file_name, dropbox_path, certificate_number, reason)
                                      VALUES (%s, %s, %s, %s)""",
                                   (fname, fpath, cert_number,
                                    'Certificate already exists in database'))
                    stats['duplicates'] += 1
                    continue

                # STEP E: Get Dropbox shared link for PDF viewing
                shared_link = None
                try:
                    link = dbx.sharing_create_shared_link_with_settings(fpath)
                    shared_link = link.url.replace('?dl=0', '?raw=1')  # type: ignore[union-attr]
                except Exception as link_err:
                    link_err_str = str(link_err).lower()
                    if 'expired_access_token' in link_err_str or 'authError' in str(link_err):
                        print(f'[SYNC] WARNING: Dropbox token expired — shared link NOT created for {fname}. Update DROPBOX_ACCESS_TOKEN in .env')
                    elif 'shared_link_already_exists' in link_err_str:
                        # Link exists already — fetch it
                        try:
                            links = dbx.sharing_list_shared_links(path=fpath)
                            if links.links:  # type: ignore[union-attr]
                                shared_link = links.links[0].url.replace('?dl=0', '?raw=1')  # type: ignore[union-attr,index]
                        except Exception:
                            pass
                    else:
                        print(f'[SYNC] WARNING: Could not create shared link for {fname}: {link_err}')
                        try:
                            links = dbx.sharing_list_shared_links(path=fpath)
                            if links.links:  # type: ignore[union-attr]
                                shared_link = links.links[0].url.replace('?dl=0', '?raw=1')  # type: ignore[union-attr,index]
                        except Exception:
                            pass

                if not shared_link:
                    print(f'[SYNC] WARNING: Certificate {cert_number} inserted WITHOUT shared link — customer will see Pending until link is fixed.')

                # STEP F: Insert certificate
                cal_date = date.today()
                due_date = cal_date + timedelta(days=365)
                db_execute("""INSERT INTO certificates
                              (instrument_id, certificate_number, calibration_date,
                               due_date, dropbox_file_path, dropbox_shared_link, source)
                              VALUES (%s, %s, %s, %s, %s, %s, 'dropbox')""",
                           (instrument['id'], cert_number, cal_date, due_date,
                            fpath, shared_link))
                print(f"[SYNC] SUCCESS: Inserted cert {cert_number} for instrument {instrument['id']}")
                stats['success'] += 1

            except Exception as e:
                stats['errors'] += 1
                print(f"[SYNC] Exception on {fname}: {e}")
                try:
                    already_unmatched = db_query(
                        "SELECT id FROM unmatched_files WHERE file_name=%s AND resolved=FALSE",
                        (fname,), fetch='one')
                    if not already_unmatched:
                        db_execute("""INSERT INTO unmatched_files
                                      (file_name, dropbox_path, reason)
                                      VALUES (%s, %s, %s)""",
                                   (fname, fpath, f'Error: {str(e)[:300]}'))
                except Exception:
                    pass

        # Log this sync
        db_execute("""INSERT INTO sync_logs
                      (total_files, success_count, duplicate_count,
                       unmatched_count, error_count, triggered_by)
                      VALUES (%s, %s, %s, %s, %s, %s)""",
                   (stats['total'], stats['success'], stats['duplicates'],
                    stats['unmatched'], stats['errors'], triggered_by))

        _last_sync_stats = stats
        _last_sync_time = datetime.now()
        _sync_last_result = {'stats': stats, 'error': None, 'finished_at': _last_sync_time}
        _sync_in_progress = False
        return stats, None

    except ImportError:
        msg = 'Dropbox package not installed. Run: pip install dropbox'
        _sync_last_result = {'stats': stats, 'error': msg, 'finished_at': datetime.now()}
        _sync_in_progress = False
        return stats, msg
    except Exception as e:
        _sync_last_result = {'stats': stats, 'error': str(e), 'finished_at': datetime.now()}
        _sync_in_progress = False
        return stats, str(e)

# =============================================================================
# BACKGROUND SCHEDULER (auto-sync every 10 min)
# =============================================================================
_scheduler = None


def start_scheduler():
    """Start the APScheduler background job. Safe to call multiple times."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        print("[SCHEDULER] Already running — skipping duplicate start.")
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        import atexit
        interval = int(os.environ.get('SYNC_INTERVAL_MINUTES', 10))
        _scheduler = BackgroundScheduler(daemon=True)
        _scheduler.add_job(
            func=lambda: run_dropbox_sync('scheduler'),
            trigger='interval',
            minutes=interval,
            id='dropbox_auto_sync',
            replace_existing=True,
            max_instances=1          # prevent overlapping runs
        )
        _scheduler.start()
        atexit.register(lambda: _scheduler.shutdown(wait=False))
        print(f"[SCHEDULER] Auto-sync started: every {interval} minutes")
        print(f"[SCHEDULER] Next run in {interval} min. Current time: {datetime.now().strftime('%H:%M:%S')}")
    except ImportError:
        print("[SCHEDULER] APScheduler not installed. Run: pip install apscheduler")
    except Exception as e:
        print(f"[SCHEDULER] Could not start: {e}")


def get_scheduler_status():
    """Return a dict with scheduler health info for the status endpoint."""
    if _scheduler is None:
        return {'running': False, 'reason': 'Scheduler never started'}
    if not _scheduler.running:
        return {'running': False, 'reason': 'Scheduler stopped'}
    jobs = _scheduler.get_jobs()
    job_info = []
    for j in jobs:
        job_info.append({
            'id': j.id,
            'next_run': j.next_run_time.strftime('%Y-%m-%d %H:%M:%S') if j.next_run_time else None,
        })
    return {'running': True, 'jobs': job_info}

# =============================================================================
# ROUTES — AUTH
# =============================================================================

@app.route('/')
def index():
    if current_user.is_authenticated:
        if current_user.role == 'admin':
            return redirect(url_for('admin_dashboard'))
        elif current_user.role == 'manager':
            return redirect(url_for('manager_dashboard'))
        else:
            return redirect(url_for('customer_dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        user_row = db_query(
            "SELECT * FROM users WHERE username=%s AND is_active=TRUE",
            (username,), fetch='one')

        if user_row:
            stored_hash = user_row['password_hash'].encode('utf-8')
            try:
                if bcrypt.checkpw(password.encode('utf-8'), stored_hash):
                    user = User(user_row)
                    login_user(user, remember=True)
                    flash(f'Welcome back, {user.full_name}!', 'success')
                    return redirect(url_for('index'))
            except Exception:
                pass

        flash('Invalid username or password.', 'danger')

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))


# One-time admin password setup route
@app.route('/setup-admin', methods=['GET', 'POST'])
def setup_admin():
    # Only works if password is the default bcrypt hash or admin doesn't exist
    admin = db_query("SELECT id FROM users WHERE username='admin'", fetch='one')
    if request.method == 'POST':
        pwd = request.form.get('password', '')
        if len(pwd) < 6:
            flash('Password must be at least 6 characters.', 'danger')
        else:
            hashed = bcrypt.hashpw(pwd.encode(), bcrypt.gensalt()).decode()
            if admin:
                db_execute("UPDATE users SET password_hash=%s WHERE username='admin'", (hashed,))
            else:
                db_execute("""INSERT INTO users (username,password_hash,full_name,role)
                              VALUES ('admin',%s,'System Administrator','admin')""", (hashed,))
            flash('Admin password set! Please log in.', 'success')
            return redirect(url_for('login'))
    return render_template('setup_admin.html')

# =============================================================================
# ROUTES — ADMIN
# =============================================================================

@app.route('/admin/dashboard')
@login_required
@role_required('admin')
def admin_dashboard():
    today_d = date.today()
    soon_threshold = today_d + timedelta(days=30)

    overdue_count = db_query("""
        SELECT COUNT(DISTINCT i.id) AS c FROM instruments i
        JOIN certificates c ON c.instrument_id = i.id
        WHERE c.due_date < %s
          AND c.id = (SELECT id FROM certificates WHERE instrument_id = i.id
                      ORDER BY calibration_date DESC LIMIT 1)
    """, (today_d,), fetch='one')['c']

    pending_pdfs = db_query("""
        SELECT COUNT(*) AS c FROM certificates
        WHERE (dropbox_file_path IS NULL OR dropbox_file_path = '')
          AND (dropbox_shared_link IS NULL OR dropbox_shared_link = '')
    """, fetch='one')['c']

    stats = {
        'customers':    db_query("SELECT COUNT(*) AS c FROM customers WHERE is_active=TRUE", fetch='one')['c'],
        'instruments':  db_query("SELECT COUNT(*) AS c FROM instruments", fetch='one')['c'],
        'certificates': db_query("SELECT COUNT(*) AS c FROM certificates", fetch='one')['c'],
        'managers':     db_query("SELECT COUNT(*) AS c FROM users WHERE role='manager' AND is_active=TRUE", fetch='one')['c'],
        'overdue':      overdue_count,
        'pending_pdfs': pending_pdfs,
    }

    # Instruments expiring in next 30 days OR already overdue
    expiring_soon = db_query("""
        SELECT i.id, i.asset_number, i.instrument_name, i.serial_number,
               cu.company_name,
               (SELECT due_date FROM certificates WHERE instrument_id = i.id
                ORDER BY calibration_date DESC LIMIT 1) AS latest_due
        FROM instruments i
        JOIN customers cu ON cu.id = i.customer_id
        WHERE (
          SELECT due_date FROM certificates WHERE instrument_id = i.id
          ORDER BY calibration_date DESC LIMIT 1
        ) <= %s
        ORDER BY (
          SELECT due_date FROM certificates WHERE instrument_id = i.id
          ORDER BY calibration_date DESC LIMIT 1
        ) ASC
        LIMIT 20
    """, (soon_threshold,))

    # Add days_left attribute for template
    expiring_with_days = []
    for e in expiring_soon:
        row = dict(e)
        row['days_left'] = (e['latest_due'] - today_d).days if e['latest_due'] else -999
        expiring_with_days.append(row)

    # Recent certificates
    recent_certs = db_query("""
        SELECT c.certificate_number, c.calibration_date, c.due_date, c.source,
               i.instrument_name, i.serial_number,
               cu.company_name
        FROM certificates c
        JOIN instruments i ON i.id = c.instrument_id
        JOIN customers cu ON cu.id = i.customer_id
        ORDER BY c.created_at DESC LIMIT 8
    """)
    # Last sync
    last_sync = db_query("SELECT * FROM sync_logs ORDER BY synced_at DESC LIMIT 1", fetch='one')
    return render_template('admin/dashboard.html',
                           stats=stats, recent_certs=recent_certs,
                           last_sync=last_sync, cert_status=cert_status,
                           expiring_soon=expiring_with_days, now=datetime.now())


# --- Customers ---
@app.route('/admin/customers')
@login_required
@role_required('admin')
def admin_customers():
    today_d = date.today()
    soon_d  = today_d + timedelta(days=30)
    customers = db_query("""
        SELECT
          cu.*,
          COUNT(DISTINCT i.id) AS instrument_count,
          COUNT(DISTINCT cert.id) AS cert_count,
          MAX(cert.calibration_date) AS last_cal_date,
          -- overdue: latest cert per instrument has due_date < today
          COUNT(DISTINCT CASE
            WHEN lc.due_date < %(today)s THEN i.id
          END) AS overdue_count,
          -- due soon: latest cert per instrument due within 30 days
          COUNT(DISTINCT CASE
            WHEN lc.due_date >= %(today)s AND lc.due_date <= %(soon)s THEN i.id
          END) AS due_soon_count
        FROM customers cu
        LEFT JOIN instruments i ON i.customer_id = cu.id
        LEFT JOIN certificates cert ON cert.instrument_id = i.id
        LEFT JOIN LATERAL (
          SELECT due_date FROM certificates
          WHERE instrument_id = i.id
          ORDER BY calibration_date DESC LIMIT 1
        ) lc ON true
        WHERE cu.is_active = TRUE
        GROUP BY cu.id
        ORDER BY cu.company_name
    """, {'today': today_d, 'soon': soon_d})
    return render_template('admin/customers.html', customers=customers)


@app.route('/admin/customers/add', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def admin_add_customer():
    if request.method == 'POST':
        name = request.form.get('company_name', '').strip()
        contact = request.form.get('contact_person', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        address = request.form.get('address', '').strip()

        if not name:
            flash('Company name is required.', 'danger')
        else:
            cid = db_execute_returning("""
                INSERT INTO customers (company_name, contact_person, email, phone, address)
                VALUES (%s,%s,%s,%s,%s) RETURNING id
            """, (name, contact, email, phone, address))

            # If creating a customer user too
            u_name = request.form.get('username', '').strip()
            u_pass = request.form.get('password', '').strip()
            u_full = request.form.get('full_name', '').strip()
            if u_name and u_pass and u_full:
                h = bcrypt.hashpw(u_pass.encode(), bcrypt.gensalt()).decode()
                try:
                    db_execute("""INSERT INTO users
                                  (username,password_hash,full_name,role,customer_id,email)
                                  VALUES (%s,%s,%s,'customer',%s,%s)""",
                               (u_name, h, u_full, cid, email))
                    flash('Customer and login account created!', 'success')
                except Exception:
                    flash('Customer created. Username already exists—create user separately.', 'warning')
            else:
                flash(f'Customer "{name}" added.', 'success')
            return redirect(url_for('admin_customers'))

    return render_template('admin/add_customer.html')


@app.route('/admin/customers/<int:cid>/delete', methods=['POST'])
@login_required
@role_required('admin')
def admin_delete_customer(cid):
    db_execute("UPDATE customers SET is_active=FALSE WHERE id=%s", (cid,))
    flash('Customer removed.', 'info')
    return redirect(url_for('admin_customers'))


# --- Instruments ---
@app.route('/admin/instruments')
@login_required
@role_required('admin')
def admin_instruments():
    instruments = db_query("""
        SELECT i.*, cu.company_name,
               COUNT(c.id) AS cert_count,
               (SELECT due_date FROM certificates
                WHERE instrument_id = i.id
                ORDER BY calibration_date DESC LIMIT 1) AS latest_due_date
        FROM instruments i
        JOIN customers cu ON cu.id = i.customer_id
        LEFT JOIN certificates c ON c.instrument_id = i.id
        GROUP BY i.id, cu.company_name
        ORDER BY cu.company_name, i.instrument_name
    """)
    return render_template('admin/instruments.html', instruments=instruments,
                           cert_status=cert_status, today=date.today())


@app.route('/admin/instruments/add', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def admin_add_instrument():
    customers = db_query("SELECT id, company_name FROM customers WHERE is_active=TRUE ORDER BY company_name")
    if request.method == 'POST':
        cid    = request.form.get('customer_id')
        asset  = request.form.get('asset_number', '').strip()
        name   = request.form.get('instrument_name', '').strip()  # kept as internal label
        serial = request.form.get('serial_number', '').strip()
        model  = request.form.get('model', '').strip()
        mfr    = request.form.get('manufacturer', '').strip()
        loc    = request.form.get('location', '').strip()

        if not cid or not serial:
            flash('Customer and serial number are required.', 'danger')
        else:
            try:
                db_execute("""INSERT INTO instruments
                              (customer_id, instrument_name, serial_number, model,
                               manufacturer, location, asset_number)
                              VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                           (cid, name or asset or serial, serial, model, mfr, loc, asset))
                label = asset or name or serial
                flash(f'Instrument "{label}" (SN: {serial}) added.', 'success')
                return redirect(url_for('admin_instruments'))
            except psycopg2.errors.UniqueViolation:
                flash(f'Serial number "{serial}" already exists.', 'danger')

    return render_template('admin/add_instrument.html', customers=customers)


@app.route('/admin/instruments/<int:iid>/delete', methods=['POST'])
@login_required
@role_required('admin')
def admin_delete_instrument(iid):
    db_execute("DELETE FROM instruments WHERE id=%s", (iid,))
    flash('Instrument deleted.', 'info')
    return redirect(url_for('admin_instruments'))


# --- Excel Bulk Import ---
@app.route('/admin/instruments/import-excel', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def admin_import_excel():
    """
    Upload an Excel (.xlsx) file with columns:
      company_name | instrument_name | serial_number | model | manufacturer | location
    Customers are created automatically if they don't exist.
    """
    results = None
    if request.method == 'POST':
        f = request.files.get('excel_file')
        if not f or not f.filename or not f.filename.lower().endswith(('.xlsx', '.xls')):
            flash('Please upload a valid Excel file (.xlsx or .xls).', 'danger')
            return redirect(url_for('admin_import_excel'))
        try:
            import openpyxl
            wb = openpyxl.load_workbook(f.stream, read_only=True, data_only=True)  # type: ignore[arg-type]
            ws = wb.active
            if ws is None:
                flash('Excel file has no active sheet.', 'danger')
                return redirect(url_for('admin_import_excel'))
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                flash('Excel file is empty.', 'danger')
                return redirect(url_for('admin_import_excel'))

            # Detect header row
            header = [str(h).strip().lower() if h else '' for h in rows[0]]
            required = ['instrument_name', 'serial_number', 'company_name']
            missing = [r for r in required if r not in header]
            if missing:
                flash(f'Missing required columns: {", ".join(missing)}. First row must be header.', 'danger')
                return redirect(url_for('admin_import_excel'))

            def col(row, name):
                idx = header.index(name) if name in header else -1
                if idx < 0 or idx >= len(row):
                    return ''
                return str(row[idx]).strip() if row[idx] is not None else ''

            added = skipped = errors = 0
            error_details = []
            for i, row in enumerate(rows[1:], start=2):
                company  = col(row, 'company_name')
                name     = col(row, 'instrument_name')
                serial   = col(row, 'serial_number')
                model    = col(row, 'model')
                mfr      = col(row, 'manufacturer')
                location = col(row, 'location')

                if not company or not name or not serial:
                    skipped += 1
                    continue

                # Get or create customer
                customer = db_query(
                    "SELECT id FROM customers WHERE LOWER(TRIM(company_name))=LOWER(TRIM(%s))",
                    (company,), fetch='one')
                if not customer:
                    cid = db_execute_returning(
                        "INSERT INTO customers (company_name) VALUES (%s) RETURNING id",
                        (company,))
                else:
                    cid = customer['id']

                # Insert instrument (skip if serial already exists)
                existing = db_query(
                    "SELECT id FROM instruments WHERE LOWER(TRIM(serial_number))=LOWER(TRIM(%s))",
                    (serial,), fetch='one')
                if existing:
                    skipped += 1
                    continue
                try:
                    db_execute("""
                        INSERT INTO instruments
                          (customer_id, instrument_name, serial_number, model, manufacturer, location)
                        VALUES (%s,%s,%s,%s,%s,%s)
                    """, (cid, name, serial, model, mfr, location))
                    added += 1
                except Exception as e:
                    errors += 1
                    error_details.append(f'Row {i}: {str(e)[:120]}')

            results = {'added': added, 'skipped': skipped, 'errors': errors,
                       'error_details': error_details}
            flash(f'Import complete! Added: {added} | Skipped/Duplicate: {skipped} | Errors: {errors}',
                  'success' if errors == 0 else 'warning')
        except Exception as e:
            flash(f'Failed to read Excel file: {e}', 'danger')

    return render_template('admin/import_excel.html', results=results)


# --- Users (Managers + Customers) ---
@app.route('/admin/users')
@login_required
@role_required('admin')
def admin_users():
    users = db_query("""
        SELECT u.id, u.username, u.full_name, u.role, u.email,
               u.is_active, u.created_at, u.plain_password, cu.company_name
        FROM users u
        LEFT JOIN customers cu ON cu.id = u.customer_id
        WHERE u.is_active = TRUE AND u.role != 'admin'
        ORDER BY u.role, u.full_name
    """)
    return render_template('admin/users.html', users=users)


@app.route('/admin/users/add', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def admin_add_user():
    customers = db_query("SELECT id, company_name FROM customers WHERE is_active=TRUE ORDER BY company_name")
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        full_name = request.form.get('full_name', '').strip()
        role = request.form.get('role', '').strip()
        cid = request.form.get('customer_id') or None
        email = request.form.get('email', '').strip()

        if not username or not password or not full_name or not role:
            flash('All required fields must be filled.', 'danger')
        else:
            h = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            try:
                db_execute("""INSERT INTO users
                              (username, password_hash, full_name, role, customer_id, email, plain_password)
                              VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                           (username, h, full_name, role, cid, email, password))
                flash(f'User "{full_name}" ({role}) created.', 'success')
                return redirect(url_for('admin_users'))
            except psycopg2.errors.UniqueViolation:
                flash(f'Username "{username}" already exists.', 'danger')

    return render_template('admin/add_user.html', customers=customers)


@app.route('/admin/users/<int:uid>/delete', methods=['POST'])
@login_required
@role_required('admin')
def admin_delete_user(uid):
    db_execute("UPDATE users SET is_active=FALSE WHERE id=%s", (uid,))
    flash('User deactivated.', 'info')
    return redirect(url_for('admin_users'))


# --- All Certificates View with download count ---
@app.route('/admin/certificates')
@login_required
@role_required('admin')
def admin_certificates():
    search = request.args.get('search', '').strip()
    base_q = """
        SELECT c.*, i.instrument_name, i.asset_number, i.serial_number, i.model,
               cu.company_name,
               (SELECT COUNT(*) FROM certificate_downloads cd WHERE cd.certificate_id = c.id) AS download_count,
               (SELECT MAX(cd.downloaded_at) FROM certificate_downloads cd WHERE cd.certificate_id = c.id) AS last_downloaded
        FROM certificates c
        JOIN instruments i ON i.id = c.instrument_id
        JOIN customers cu ON cu.id = i.customer_id
    """
    if search:
        certs = db_query(base_q + """
            WHERE LOWER(c.certificate_number) LIKE %s
               OR LOWER(i.serial_number) LIKE %s
               OR LOWER(i.asset_number) LIKE %s
            ORDER BY c.calibration_date DESC
        """, (f'%{search.lower()}%', f'%{search.lower()}%', f'%{search.lower()}%'))
    else:
        certs = db_query(base_q + ' ORDER BY c.calibration_date DESC')
    missing_links_count = sum(1 for c in certs if not c.get('dropbox_shared_link') and not c.get('dropbox_file_path'))
    all_instruments = db_query("""
        SELECT i.id, i.asset_number, i.instrument_name, i.serial_number, cu.company_name
        FROM instruments i
        JOIN customers cu ON cu.id = i.customer_id
        ORDER BY cu.company_name, i.asset_number
    """)
    return render_template('admin/certificates.html', certs=certs, cert_status=cert_status,
                           missing_links_count=missing_links_count, today=date.today(),
                           all_instruments=all_instruments)


# --- Admin: Verify & repair Dropbox paths using temporary links ---
@app.route('/admin/certificates/fix-links', methods=['POST'])
@login_required
@role_required('admin')
def admin_fix_missing_links():
    """
    Verify every certificate path is reachable on Dropbox using
    files_get_temporary_link (needs only files.content.read scope — no
    sharing scopes required).  Corrects any path-case mismatches found
    by scanning the Dropbox folder.
    """
    try:
        import dropbox as dbx_lib
        dbx = _get_dropbox_client()
        if dbx is None:
            flash('Dropbox not configured. Run dropbox_auth_setup.py to set up permanent credentials.', 'danger')
            return redirect(url_for('admin_certificates'))

        # Test connection
        try:
            dbx.users_get_current_account()
        except Exception as auth_err:
            flash(f'Dropbox connection failed: {auth_err}', 'danger')
            return redirect(url_for('admin_certificates'))

        # Build a map of filename → actual_path from Dropbox
        folder = _get_env('DROPBOX_FOLDER') or '/Calibration_Certificates'
        dropbox_file_map = {}  # filename.lower() -> path_lower
        try:
            result = dbx.files_list_folder(folder, recursive=True)  # type: ignore[union-attr]
            while True:
                for entry in result.entries:  # type: ignore[union-attr]
                    name = getattr(entry, 'name', '')
                    path = getattr(entry, 'path_lower', '')
                    if name.lower().endswith('.pdf'):
                        dropbox_file_map[name.lower()] = path
                if result.has_more:  # type: ignore[union-attr]
                    result = dbx.files_list_folder_continue(result.cursor)  # type: ignore[union-attr]
                else:
                    break
        except Exception as scan_err:
            flash(f'Could not scan Dropbox folder: {scan_err}', 'danger')
            return redirect(url_for('admin_certificates'))

        # Get all certificates with a stored file path
        all_certs = db_query("""
            SELECT id, certificate_number, dropbox_file_path
            FROM certificates
            WHERE dropbox_file_path IS NOT NULL
        """)

        verified = 0
        path_fixed = 0
        failed = 0

        for cert in all_certs:
            cert_id   = cert['id']
            stored    = cert['dropbox_file_path']
            filename  = stored.split('/')[-1].lower() if stored else ''

            # Find the real path on Dropbox
            real_path = dropbox_file_map.get(filename)
            if not real_path:
                failed += 1
                print(f'[FIX] cert {cert_id}: file "{filename}" not found in Dropbox')
                continue

            # Verify temp link works
            try:
                dbx.files_get_temporary_link(real_path)
            except Exception as e:
                failed += 1
                print(f'[FIX] cert {cert_id}: temp link failed for {real_path}: {e}')
                continue

            # Correct stored path if it differs
            if stored != real_path:
                db_execute(
                    'UPDATE certificates SET dropbox_file_path=%s WHERE id=%s',
                    (real_path, cert_id)
                )
                path_fixed += 1
                print(f'[FIX] cert {cert_id}: path corrected {stored} → {real_path}')
            else:
                verified += 1

        msg = f'Verified {verified} | Paths corrected {path_fixed} | Not found {failed}'
        if failed == 0:
            flash(f'All {verified + path_fixed} certificate(s) are now linked to Dropbox PDFs. {msg}', 'success')
        else:
            flash(f'{msg}. Certificates with "Not found" have no matching file in Dropbox.', 'warning')

    except ImportError:
        flash('Dropbox package not installed. Run: pip install dropbox', 'danger')
    except Exception as e:
        flash(f'Error: {e}', 'danger')

    return redirect(url_for('admin_certificates'))


# --- Admin: Manually Add a Certificate ---
@app.route('/admin/certificates/add-manual', methods=['POST'])
@login_required
@role_required('admin')
def admin_add_certificate_manual():
    """Allow admin to manually record a certificate for any instrument.
    Works even for instruments whose PDFs are already on Dropbox — the Dropbox
    path can be filled in now or fixed later with the 'Fix PDF Links' tool.
    """
    instrument_id    = request.form.get('instrument_id', '').strip()
    cert_number      = request.form.get('certificate_number', '').strip()
    calibration_date = request.form.get('calibration_date', '').strip()
    due_date_str     = request.form.get('due_date', '').strip()
    dropbox_path     = request.form.get('dropbox_file_path', '').strip() or None

    if not instrument_id or not cert_number or not calibration_date or not due_date_str:
        flash('All required fields must be filled.', 'danger')
        return redirect(url_for('admin_certificates'))

    try:
        cal_date = datetime.strptime(calibration_date, '%Y-%m-%d').date()
        due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
    except ValueError:
        flash('Invalid date format. Use YYYY-MM-DD.', 'danger')
        return redirect(url_for('admin_certificates'))

    # Check instrument exists
    instrument = db_query('SELECT id FROM instruments WHERE id=%s', (instrument_id,), fetch='one')
    if not instrument:
        flash('Instrument not found.', 'danger')
        return redirect(url_for('admin_certificates'))

    # Check certificate number unique
    existing = db_query(
        'SELECT id FROM certificates WHERE LOWER(certificate_number)=LOWER(%s)',
        (cert_number,), fetch='one')
    if existing:
        flash(f'Certificate number "{cert_number}" already exists.', 'danger')
        return redirect(url_for('admin_certificates'))

    try:
        db_execute("""
            INSERT INTO certificates
              (instrument_id, certificate_number, calibration_date, due_date,
               dropbox_file_path, source)
            VALUES (%s, %s, %s, %s, %s, 'manual')
        """, (instrument_id, cert_number, cal_date, due_date, dropbox_path))
        flash(f'Certificate "{cert_number}" added successfully.', 'success')
    except Exception as e:
        flash(f'Error adding certificate: {e}', 'danger')

    return redirect(url_for('admin_certificates'))


# --- Admin: Edit Certificate Dates / Dropbox Path ---
@app.route('/admin/certificates/<int:cert_id>/edit', methods=['POST'])
@login_required
@role_required('admin')
def admin_edit_certificate(cert_id):
    """Update calibration date, due date, and optionally the Dropbox path."""
    calibration_date = request.form.get('calibration_date', '').strip()
    due_date_str     = request.form.get('due_date', '').strip()
    dropbox_path     = request.form.get('dropbox_file_path', '').strip() or None

    if not calibration_date or not due_date_str:
        flash('Both calibration date and due date are required.', 'danger')
        return redirect(url_for('admin_certificates'))

    try:
        cal_date = datetime.strptime(calibration_date, '%Y-%m-%d').date()
        due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
    except ValueError:
        flash('Invalid date format.', 'danger')
        return redirect(url_for('admin_certificates'))

    cert = db_query('SELECT id FROM certificates WHERE id=%s', (cert_id,), fetch='one')
    if not cert:
        flash('Certificate not found.', 'danger')
        return redirect(url_for('admin_certificates'))

    if dropbox_path:
        db_execute(
            'UPDATE certificates SET calibration_date=%s, due_date=%s, dropbox_file_path=%s WHERE id=%s',
            (cal_date, due_date, dropbox_path, cert_id))
    else:
        db_execute(
            'UPDATE certificates SET calibration_date=%s, due_date=%s WHERE id=%s',
            (cal_date, due_date, cert_id))

    flash('Certificate updated successfully.', 'success')
    return redirect(url_for('admin_certificates'))


# --- Admin: Download Activity Log ---
@app.route('/admin/downloads')
@login_required
@role_required('admin')
def admin_downloads():
    downloads = db_query("""
        SELECT cd.id, cd.downloaded_at, cd.ip_address,
               c.certificate_number,
               i.instrument_name, i.serial_number,
               cu.company_name,
               u.full_name AS customer_name, u.username
        FROM certificate_downloads cd
        JOIN certificates c   ON c.id  = cd.certificate_id
        JOIN instruments i    ON i.id  = c.instrument_id
        JOIN customers cu     ON cu.id = i.customer_id
        LEFT JOIN users u     ON u.id  = cd.user_id
        ORDER BY cd.downloaded_at DESC
        LIMIT 200
    """)
    # per-certificate download summary
    summary = db_query("""
        SELECT c.certificate_number, i.instrument_name, cu.company_name,
               COUNT(cd.id) AS total_downloads,
               MAX(cd.downloaded_at) AS last_download
        FROM certificate_downloads cd
        JOIN certificates c ON c.id = cd.certificate_id
        JOIN instruments i  ON i.id = c.instrument_id
        JOIN customers cu   ON cu.id = i.customer_id
        GROUP BY c.certificate_number, i.instrument_name, cu.company_name
        ORDER BY total_downloads DESC
    """)
    return render_template('admin/downloads.html',
                           downloads=downloads, summary=summary)


# --- Sync Status API ---
@app.route('/sync/status')
@login_required
@role_required('admin', 'manager')
def sync_status_api():
    """JSON endpoint polled by the UI to check sync progress."""
    r = _sync_last_result
    return jsonify({
        'in_progress': _sync_in_progress,
        'finished_at': r['finished_at'].strftime('%d %b %Y %H:%M:%S') if r and r.get('finished_at') else None,
        'stats':       r['stats']  if r else None,
        'error':       r['error']  if r else None,
    })


# --- Sync Report (Admin + Manager) ---
@app.route('/sync', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'manager')
def sync_report():
    global _sync_in_progress

    if request.method == 'POST':
        if _sync_in_progress:
            return jsonify({'queued': False, 'message': 'Sync already running'}), 409

        triggered_by = f'{current_user.role}:{current_user.username}'
        _sync_in_progress = True

        import threading
        t = threading.Thread(
            target=run_dropbox_sync,
            args=(triggered_by,),
            daemon=True
        )
        t.start()

        # Return immediately — UI will poll /sync/status
        return jsonify({'queued': True, 'message': 'Sync started in background'}), 202

    sync_logs = db_query("SELECT * FROM sync_logs ORDER BY synced_at DESC LIMIT 20")
    unmatched = db_query("SELECT * FROM unmatched_files WHERE resolved=FALSE ORDER BY detected_at DESC")
    duplicates = db_query("SELECT * FROM duplicate_files WHERE resolved=FALSE ORDER BY detected_at DESC")

    return render_template('sync_report.html',
                           sync_logs=sync_logs,
                           unmatched=unmatched,
                           duplicates=duplicates,
                           last_sync_time=_last_sync_time)


@app.route('/sync/resolve-unmatched/<int:uid>', methods=['POST'])
@login_required
@role_required('admin', 'manager')
def resolve_unmatched(uid):
    iid = request.form.get('instrument_id')
    uf = db_query("SELECT * FROM unmatched_files WHERE id=%s", (uid,), fetch='one')
    if uf and iid:
        fname = uf['file_name']
        # Split on FIRST underscore only (cert number may contain underscores)
        if '_' in fname and fname.lower().endswith('.pdf'):
            base_name = fname[:-4]
            first_underscore = base_name.index('_')
            cert_number = base_name[first_underscore + 1:].strip()
        else:
            cert_number = fname.replace('.pdf', '')
        cal_date = date.today()
        due_date = cal_date + timedelta(days=365)
        try:
            db_execute("""INSERT INTO certificates
                          (instrument_id, certificate_number, calibration_date, due_date,
                           dropbox_file_path, source)
                          VALUES (%s,%s,%s,%s,%s,'dropbox')""",
                       (iid, cert_number, cal_date, due_date, uf.get('dropbox_path')))
            db_execute("UPDATE unmatched_files SET resolved=TRUE WHERE id=%s", (uid,))
            flash('Assigned successfully!', 'success')
        except Exception as e:
            flash(f'Error: {e}', 'danger')
    return redirect(url_for('sync_report'))


@app.route('/sync/debug')
@login_required
@role_required('admin', 'manager')
def sync_debug():
    """
    Diagnostic: shows DB serials vs Dropbox filenames and whether they match.
    Open /sync/debug in browser to diagnose ingestion problems.
    """
    # --- Fetch instruments safely ---
    instruments_raw = db_query(
        "SELECT id, instrument_name, serial_number FROM instruments ORDER BY serial_number")
    instruments = instruments_raw if instruments_raw is not None else []

    # Build a set of lowercase serials from DB for fast lookup
    db_serials_set = set()
    for inst in instruments:
        sn = inst.get('serial_number') if inst else None
        if sn is not None:
            db_serials_set.add(str(sn).strip().lower())

    # --- Connect to Dropbox ---
    dropbox_files = []
    dropbox_error = None
    folder = _get_env('DROPBOX_FOLDER') or '/Calibration_Certificates'

    try:
        import dropbox as dbx_lib
        dbx = _get_dropbox_client()
        if dbx is not None:
            result = dbx.files_list_folder(folder, recursive=True)
            while True:
                for entry in result.entries:  # type: ignore[union-attr]
                    # Only process file entries with a .pdf extension
                    entry_name = getattr(entry, 'name', None)
                    if not entry_name:
                        continue
                    if not entry_name.lower().endswith('.pdf'):
                        continue

                    # Use first-underscore split (same logic as main sync)
                    base = entry_name[:-4]  # strip .pdf
                    if '_' in base:
                        idx = base.index('_')
                        extracted_serial = base[:idx].strip()
                    else:
                        extracted_serial = 'NO_UNDERSCORE'

                    matched = extracted_serial.lower() in db_serials_set
                    dropbox_files.append({
                        'name': entry_name,
                        'path': getattr(entry, 'path_lower', ''),
                        'extracted_serial': extracted_serial,
                        'matched': matched,
                    })

                if result.has_more:  # type: ignore[union-attr]
                    result = dbx.files_list_folder_continue(result.cursor)  # type: ignore[union-attr]
                else:
                    break
        else:
            dropbox_error = 'Dropbox not configured. Run dropbox_auth_setup.py'
    except Exception as exc:
        dropbox_error = str(exc)

    return jsonify({
        'db_instruments': [dict(i) for i in instruments],
        'db_serials': sorted(db_serials_set),
        'dropbox_folder': folder,
        'dropbox_files': dropbox_files,
        'dropbox_error': dropbox_error,
        'total_db_instruments': len(instruments),
        'total_dropbox_pdfs': len(dropbox_files),
        'matched_count': sum(1 for f in dropbox_files if f.get('matched')),
    })


@app.route('/sync/clear-unmatched', methods=['POST'])
@login_required
@role_required('admin')
def sync_clear_unmatched():
    """Mark ALL unmatched_files as resolved so the list resets cleanly."""
    count = db_execute("UPDATE unmatched_files SET resolved=TRUE WHERE resolved=FALSE")
    flash(f'Cleared {count} unmatched file records. Next sync will re-evaluate them.', 'info')
    return redirect(url_for('sync_report'))


@app.route('/sync/clear-duplicates', methods=['POST'])
@login_required
@role_required('admin')
def sync_clear_duplicates():
    """Mark ALL duplicate_files as resolved so the list resets cleanly."""
    count = db_execute("UPDATE duplicate_files SET resolved=TRUE WHERE resolved=FALSE")
    flash(f'Cleared {count} duplicate file records.', 'info')
    return redirect(url_for('sync_report'))


@app.route('/sync/status')
@login_required
@role_required('admin', 'manager')
def sync_status():
    """JSON endpoint: returns scheduler health + last sync time.
       Polled every minute by the sync_report page countdown timer."""
    sched = get_scheduler_status()
    return jsonify({
        'scheduler': sched,
        'last_sync_time': _last_sync_time.strftime('%Y-%m-%d %H:%M:%S') if _last_sync_time else None,
        'last_sync_stats': _last_sync_stats,
        'server_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })


# =============================================================================
# ROUTES — CALIBRATION MANAGER
# =============================================================================

@app.route('/manager/dashboard')
@login_required
@role_required('manager')
def manager_dashboard():
    total_certs = db_query("SELECT COUNT(*) AS c FROM certificates", fetch='one')['c']
    last_sync = db_query("SELECT * FROM sync_logs ORDER BY synced_at DESC LIMIT 1", fetch='one')
    unmatched_count = db_query("SELECT COUNT(*) AS c FROM unmatched_files WHERE resolved=FALSE", fetch='one')['c']
    recent_certs = db_query("""
        SELECT c.certificate_number, c.calibration_date, c.due_date, c.source,
               i.instrument_name, i.serial_number, cu.company_name
        FROM certificates c
        JOIN instruments i ON i.id = c.instrument_id
        JOIN customers cu ON cu.id = i.customer_id
        ORDER BY c.created_at DESC LIMIT 10
    """)
    return render_template('manager/dashboard.html',
                           total_certs=total_certs,
                           last_sync=last_sync,
                           unmatched_count=unmatched_count,
                           recent_certs=recent_certs,
                           cert_status=cert_status)

# =============================================================================
# ROUTES — CUSTOMER
# =============================================================================

@app.route('/customer/dashboard')
@login_required
@role_required('customer')
def customer_dashboard():
    if not current_user.customer_id:
        flash('Your account is not linked to a customer. Please contact admin.', 'warning')
        return redirect(url_for('login'))


    search = request.args.get('search', '').strip()
    if search:
        instruments = db_query("""
            SELECT i.*,
                   (SELECT COUNT(*) FROM certificates WHERE instrument_id = i.id) AS cert_count,
                   (SELECT due_date FROM certificates WHERE instrument_id = i.id
                    ORDER BY calibration_date DESC LIMIT 1) AS latest_due_date
            FROM instruments i
            WHERE i.customer_id = %s
              AND (LOWER(i.serial_number) LIKE %s
               OR LOWER(COALESCE(i.asset_number,'')) LIKE %s
               OR LOWER(i.instrument_name) LIKE %s)
            ORDER BY i.serial_number
        """, (current_user.customer_id, f'%{search.lower()}%',
              f'%{search.lower()}%', f'%{search.lower()}%'))
        certs = db_query("""
            SELECT c.*, i.instrument_name, i.asset_number, i.serial_number, i.model
            FROM certificates c
            JOIN instruments i ON i.id = c.instrument_id
            WHERE i.customer_id = %s
              AND (LOWER(c.certificate_number) LIKE %s
               OR LOWER(i.serial_number) LIKE %s
               OR LOWER(COALESCE(i.asset_number,'')) LIKE %s)
            ORDER BY c.calibration_date DESC
        """, (current_user.customer_id, f'%{search.lower()}%',
              f'%{search.lower()}%', f'%{search.lower()}%'))
    else:
        instruments = db_query("""
            SELECT i.*,
                   (SELECT COUNT(*) FROM certificates WHERE instrument_id = i.id) AS cert_count,
                   (SELECT due_date FROM certificates WHERE instrument_id = i.id
                    ORDER BY calibration_date DESC LIMIT 1) AS latest_due_date
            FROM instruments i
            WHERE i.customer_id = %s
            ORDER BY i.serial_number
        """, (current_user.customer_id,))
        certs = db_query("""
            SELECT c.*, i.instrument_name, i.asset_number, i.serial_number, i.model
            FROM certificates c
            JOIN instruments i ON i.id = c.instrument_id
            WHERE i.customer_id = %s
            ORDER BY c.calibration_date DESC
        """, (current_user.customer_id,))

    customer = db_query("SELECT * FROM customers WHERE id=%s",
                        (current_user.customer_id,), fetch='one')

    return render_template('customer/dashboard.html',
                           instruments=instruments, certs=certs,
                           customer=customer, cert_status=cert_status,
                           today=date.today())


# =============================================================================
# ROUTES — CUSTOMER: CHANGE PASSWORD
# =============================================================================

@app.route('/customer/change-password', methods=['GET', 'POST'])
@login_required
@role_required('customer')
def customer_change_password():
    if request.method == 'POST':
        current_pw  = request.form.get('current_password', '').strip()
        new_pw      = request.form.get('new_password', '').strip()
        confirm_pw  = request.form.get('confirm_password', '').strip()

        user_row = db_query('SELECT password_hash FROM users WHERE id=%s',
                            (current_user.id,), fetch='one')
        if not user_row:
            flash('User not found.', 'danger')
            return redirect(url_for('customer_change_password'))

        if not bcrypt.checkpw(current_pw.encode(), user_row['password_hash'].encode()):
            flash('Current password is incorrect.', 'danger')
            return redirect(url_for('customer_change_password'))

        if len(new_pw) < 6:
            flash('New password must be at least 6 characters.', 'danger')
            return redirect(url_for('customer_change_password'))

        if new_pw != confirm_pw:
            flash('New passwords do not match.', 'danger')
            return redirect(url_for('customer_change_password'))

        new_hash = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
        db_execute(
            'UPDATE users SET password_hash=%s, plain_password=%s WHERE id=%s',
            (new_hash, new_pw, current_user.id)
        )
        flash('Password changed successfully!', 'success')
        return redirect(url_for('customer_dashboard'))

    return render_template('customer/change_password.html')


# --- Admin: reset any user password ---
@app.route('/admin/users/<int:uid>/reset-password', methods=['POST'])
@login_required
@role_required('admin')
def admin_reset_user_password(uid):
    new_pw = request.form.get('new_password', '').strip()
    if len(new_pw) < 6:
        flash('Password must be at least 6 characters.', 'danger')
        return redirect(url_for('admin_users'))
    new_hash = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
    db_execute(
        'UPDATE users SET password_hash=%s, plain_password=%s WHERE id=%s',
        (new_hash, new_pw, uid)
    )
    flash('Password reset successfully.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/customer/certificate/<int:cert_id>/open')
@login_required
@role_required('customer')
def customer_open_certificate(cert_id):
    """
    Gets a fresh Dropbox temporary link (4-hour expiry) and redirects the
    customer to the PDF. Logs the download for admin tracking.
    No shared-link / sharing scopes required — uses files.content.read only.
    """
    cert = db_query("""
        SELECT c.*, i.customer_id
        FROM certificates c
        JOIN instruments i ON i.id = c.instrument_id
        WHERE c.id = %s
    """, (cert_id,), fetch='one')

    if not cert or cert['customer_id'] != current_user.customer_id:
        abort(403)

    # Log the download
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    ua = request.headers.get('User-Agent', '')[:500]
    db_execute("""
        INSERT INTO certificate_downloads
          (certificate_id, user_id, customer_id, ip_address, user_agent)
        VALUES (%s, %s, %s, %s, %s)
    """, (cert_id, current_user.id, current_user.customer_id, ip, ua))

    # --- Try Dropbox temporary link (no sharing scopes needed) ---
    dropbox_path = cert.get('dropbox_file_path')
    if dropbox_path:
        try:
            import dropbox as dbx_lib
            dbx = _get_dropbox_client()
            if dbx is None:
                raise Exception('Dropbox not configured')
            tmp = dbx.files_get_temporary_link(dropbox_path)
            return redirect(tmp.link)
        except Exception as e:
            print(f'[PDF] Temp link failed for {dropbox_path}: {e}')

    # --- Fallback: stored shared link ---
    link = cert.get('dropbox_shared_link')
    if link:
        return redirect(link)

    flash('PDF is not available yet. Please contact admin.', 'warning')
    return redirect(url_for('customer_dashboard'))


@app.route('/admin/certificates/<int:cert_id>/preview')
@login_required
@role_required('admin', 'manager')
def admin_preview_pdf(cert_id):
    """Admin route to preview any certificate PDF via Dropbox temporary link."""
    cert = db_query(
        'SELECT dropbox_file_path, dropbox_shared_link FROM certificates WHERE id=%s',
        (cert_id,), fetch='one')
    if not cert:
        abort(404)
    dropbox_path = cert.get('dropbox_file_path')
    if dropbox_path:
        try:
            import dropbox as dbx_lib
            dbx = _get_dropbox_client()
            if dbx is None:
                raise Exception('Dropbox not configured')
            tmp = dbx.files_get_temporary_link(dropbox_path)
            return redirect(tmp.link)
        except Exception as e:
            flash(f'Could not open PDF: {e}', 'danger')
    elif cert.get('dropbox_shared_link'):
        return redirect(cert['dropbox_shared_link'])
    else:
        flash('No PDF path stored for this certificate.', 'warning')
    return redirect(url_for('admin_certificates'))

# =============================================================================
# ROUTES — ERROR HANDLERS
# =============================================================================

@app.errorhandler(403)
def forbidden(e):
    return render_template('error.html', code=403, msg="You don't have permission to access this page."), 403

@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', code=404, msg="Page not found."), 404

# =============================================================================
# TEMPLATE FILTERS
# =============================================================================

@app.template_filter('dateformat')
def dateformat(value, fmt='%d %b %Y'):
    if value is None:
        return '-'
    if isinstance(value, str):
        try:
            value = datetime.strptime(value, '%Y-%m-%d').date()
        except Exception:
            return value
    return value.strftime(fmt)


@app.route('/admin/instruments-json')
@login_required
@role_required('admin', 'manager')
def admin_instruments_json():
    rows = db_query("""
        SELECT i.id, i.instrument_name, i.serial_number, cu.company_name
        FROM instruments i
        JOIN customers cu ON cu.id = i.customer_id
        ORDER BY cu.company_name, i.instrument_name
    """) or []
    return jsonify([dict(r) for r in rows])


@app.context_processor
def inject_globals():
    return dict(cert_status=cert_status, today=date.today(), now=datetime.now())

# =============================================================================
# MAIN
# =============================================================================

if __name__ == '__main__':
    import sys
    print("=" * 60)
    print("  CAL-ASIA CALIBRATION CERTIFICATE PORTAL")
    print("  Starting Flask server...")
    print("=" * 60)
    print()
    print("  >> Open browser: http://localhost:5000")
    print("  >> Admin login:  http://localhost:5000/setup-admin  (first time)")
    print("  >> Username=admin  Password=Admin@2025")
    print()

    # --- Start background scheduler FIRST (before initial sync) ---
    print("[MAIN] About to call start_scheduler()...")
    start_scheduler()
    print("[MAIN] start_scheduler() returned.")

    # --- Run initial Dropbox sync on startup ---
    print("[STARTUP] Running initial Dropbox sync...")
    try:
        result, error = run_dropbox_sync('startup')
        if error:
            print(f"[STARTUP] Sync issue: {error}")
        else:
            print(f"[STARTUP] Sync complete! Added: {result['success']} | "
                  f"Unmatched: {result['unmatched']} | Duplicates: {result['duplicates']}")
    except Exception as _e:
        print(f"[STARTUP] Sync failed with exception: {_e}")

    # IMPORTANT: use_reloader=False is REQUIRED — reloader would start
    # a second Python process without the scheduler.
    app.run(debug=False, host='0.0.0.0', port=5000, use_reloader=False)

