"""
Full diagnostic: Why are Qualcomm Noida instruments not syncing?
"""
import os, sys
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

import psycopg2
import psycopg2.extras

conn = psycopg2.connect(
    host=os.environ.get('DB_HOST', 'localhost'),
    database=os.environ.get('DB_NAME', 'calasia_certs'),
    user=os.environ.get('DB_USER', 'postgres'),
    password=os.environ.get('DB_PASSWORD', 'Calasia@2025'),
    port=os.environ.get('DB_PORT', '5432'),
)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

SEP = "=" * 70

# ── 1. Find all Qualcomm / Noida customers ────────────────────────────────
cur.execute("""
    SELECT id, company_name, contact_person, email
    FROM customers
    WHERE LOWER(company_name) LIKE '%qualcomm%'
       OR LOWER(company_name) LIKE '%noida%'
    ORDER BY company_name
""")
customers = cur.fetchall()

if not customers:
    print("[ERROR] No customer matching 'Qualcomm' or 'Noida' found!")
    cur.execute("SELECT id, company_name FROM customers ORDER BY company_name")
    print("All customers:", [dict(r) for r in cur.fetchall()])
    sys.exit(1)

print(SEP)
print("QUALCOMM / NOIDA CUSTOMER(S) FOUND:")
print(SEP)
for c in customers:
    print(f"  ID={c['id']} | {c['company_name']} | {c['contact_person']} | {c['email']}")

print()
all_no_cert_serials = []

for customer in customers:
    cid   = customer['id']
    cname = customer['company_name']

    # ── 2. Instruments for this customer ────────────────────────────────────
    cur.execute("""
        SELECT i.id, i.instrument_name, i.serial_number,
               COUNT(cert.id) AS cert_count
        FROM instruments i
        LEFT JOIN certificates cert ON cert.instrument_id = i.id
        WHERE i.customer_id = %s
        GROUP BY i.id, i.instrument_name, i.serial_number
        ORDER BY i.serial_number
    """, (cid,))
    instruments = cur.fetchall()

    print(SEP)
    print(f"INSTRUMENTS FOR: {cname}  (Total: {len(instruments)})")
    print(SEP)

    no_cert_serials = []
    has_cert_count  = 0

    for inst in instruments:
        if inst['cert_count'] > 0:
            has_cert_count += 1
            mark = "[OK]     "
        else:
            no_cert_serials.append(inst['serial_number'])
            all_no_cert_serials.append(inst['serial_number'])
            mark = "[NO CERT]"
        print(f"  {mark} SN={str(inst['serial_number']):<22} | {inst['instrument_name']}")

    print()
    print(f"  [OK]      WITH certificate   : {has_cert_count}")
    print(f"  [NO CERT] WITHOUT certificate: {len(no_cert_serials)}")

    # ── 3. Check unmatched_files for these serials ───────────────────────────
    if no_cert_serials:
        cur.execute("""
            SELECT file_name, serial_number, reason, detected_at
            FROM unmatched_files
            WHERE serial_number = ANY(%s)
              AND resolved = FALSE
            ORDER BY detected_at DESC
        """, (no_cert_serials,))
        unmatched = cur.fetchall()
        print()
        print(f"  -- UNMATCHED FILES (sync tried but failed to match) --")
        if unmatched:
            for u in unmatched:
                print(f"    FILE   : {u['file_name']}")
                print(f"    SERIAL : {u['serial_number']}")
                print(f"    REASON : {u['reason']}")
                print(f"    WHEN   : {u['detected_at']}")
                print()
        else:
            print("    (NONE) -- Sync has never even SEEN a file for these serials.")
            print("             Either the PDFs are not in Dropbox with matching filenames,")
            print("             OR the sync has not run recently.")

    print()

# ── 4. What Dropbox files DID sync successfully for qualcomm noida? ──────────
print(SEP)
print("CERTIFICATES ALREADY SYNCED (qualcomm noida, ID=3):")
print(SEP)
cur.execute("""
    SELECT c.certificate_number, c.calibration_date, c.due_date,
           c.dropbox_file_path, c.dropbox_shared_link, c.source,
           i.serial_number, i.instrument_name
    FROM certificates c
    JOIN instruments i ON i.id = c.instrument_id
    WHERE i.customer_id = 3
    ORDER BY c.calibration_date DESC
""")
certs = cur.fetchall()
if certs:
    for c in certs:
        print(f"  CERT# {c['certificate_number']} | SN={c['serial_number']} | {c['instrument_name']}")
        print(f"    Path : {c['dropbox_file_path']}")
        print(f"    Link : {'OK' if c['dropbox_shared_link'] else 'MISSING'}")
else:
    print("  (No certificates at all for qualcomm noida)")

print()

# ── 5. All unmatched files (latest 20) ──────────────────────────────────────
print(SEP)
print("ALL RECENT UNMATCHED FILES (up to 20, unresolved):")
print(SEP)
cur.execute("""
    SELECT file_name, serial_number, reason, detected_at
    FROM unmatched_files
    WHERE resolved = FALSE
    ORDER BY detected_at DESC
    LIMIT 20
""")
all_unmatched = cur.fetchall()
if all_unmatched:
    for u in all_unmatched:
        print(f"  {u['file_name']:<40} | SN={u['serial_number']}")
        print(f"    Reason: {u['reason']}")
        print(f"    When  : {u['detected_at']}")
        print()
else:
    print("  (No unmatched files recorded)")

# ── 6. Last 5 sync logs ──────────────────────────────────────────────────────
print(SEP)
print("LAST 5 SYNC LOG ENTRIES:")
print(SEP)
cur.execute("""
    SELECT synced_at, total_files, success_count, duplicate_count,
           unmatched_count, error_count, triggered_by, message
    FROM sync_logs
    ORDER BY synced_at DESC LIMIT 5
""")
logs = cur.fetchall()
if logs:
    for lg in logs:
        print(f"  {lg['synced_at']} | by={lg['triggered_by']}")
        print(f"    total={lg['total_files']}  success={lg['success_count']}  "
              f"dup={lg['duplicate_count']}  unmatched={lg['unmatched_count']}  err={lg['error_count']}")
        if lg['message']:
            print(f"    MSG: {lg['message']}")
        print()
else:
    print("  (No sync logs found -- sync may never have run!)")

conn.close()
print("Done.")
