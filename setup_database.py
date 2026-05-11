"""
Setup script: Creates the database and all tables, then creates the default admin user.
Run this once before starting the main app.
"""
import sys
import psycopg2
import psycopg2.extras
import bcrypt

# Force UTF-8 output on Windows
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

DB_PASSWORD = 'Calasia@2025'
DB_USER = 'postgres'
DB_HOST = 'localhost'
DB_PORT = 5432

print("=" * 55)
print("  CAL-ASIA CERTIFICATE PORTAL --- DATABASE SETUP")
print("=" * 55)

# Step 1: Create database if not exists
print("\n[1] Connecting to PostgreSQL...")
try:
    conn = psycopg2.connect(
        host=DB_HOST, database='postgres',
        user=DB_USER, password=DB_PASSWORD, port=DB_PORT
    )
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname='calasia_certs'")
    if cur.fetchone():
        print("    Database 'calasia_certs' already exists.")
    else:
        cur.execute("CREATE DATABASE calasia_certs")
        print("    [OK] Database 'calasia_certs' created!")
    conn.close()
except Exception as e:
    print(f"    [ERROR] {e}")
    print("    Make sure PostgreSQL is running and password is correct.")
    exit(1)

# Step 2: Create tables
print("\n[2] Creating tables...")
conn = psycopg2.connect(
    host=DB_HOST, database='calasia_certs',
    user=DB_USER, password=DB_PASSWORD, port=DB_PORT
)
conn.autocommit = True
cur = conn.cursor()

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id            SERIAL PRIMARY KEY,
    company_name  VARCHAR(200) NOT NULL,
    contact_person VARCHAR(100),
    email         VARCHAR(100),
    phone         VARCHAR(30),
    address       TEXT,
    is_active     BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name     VARCHAR(100) NOT NULL,
    role          VARCHAR(20) NOT NULL CHECK (role IN ('admin', 'manager', 'customer')),
    customer_id   INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    email         VARCHAR(100),
    is_active     BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS instruments (
    id              SERIAL PRIMARY KEY,
    customer_id     INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    instrument_name VARCHAR(200) NOT NULL,
    serial_number   VARCHAR(100) UNIQUE NOT NULL,
    model           VARCHAR(100),
    manufacturer    VARCHAR(100),
    location        VARCHAR(200),
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS certificates (
    id                   SERIAL PRIMARY KEY,
    instrument_id        INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    certificate_number   VARCHAR(100) UNIQUE NOT NULL,
    calibration_date     DATE NOT NULL,
    due_date             DATE NOT NULL,
    dropbox_file_path    VARCHAR(500),
    dropbox_shared_link  VARCHAR(1000),
    source               VARCHAR(20) DEFAULT 'manual',
    notes                TEXT,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sync_logs (
    id              SERIAL PRIMARY KEY,
    total_files     INTEGER DEFAULT 0,
    success_count   INTEGER DEFAULT 0,
    duplicate_count INTEGER DEFAULT 0,
    unmatched_count INTEGER DEFAULT 0,
    error_count     INTEGER DEFAULT 0,
    triggered_by    VARCHAR(50) DEFAULT 'scheduler',
    message         TEXT,
    synced_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS unmatched_files (
    id            SERIAL PRIMARY KEY,
    file_name     VARCHAR(500) NOT NULL,
    dropbox_path  VARCHAR(1000),
    serial_number VARCHAR(100),
    reason        TEXT,
    resolved      BOOLEAN DEFAULT FALSE,
    detected_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS duplicate_files (
    id                 SERIAL PRIMARY KEY,
    file_name          VARCHAR(500) NOT NULL,
    dropbox_path       VARCHAR(1000),
    certificate_number VARCHAR(100),
    reason             TEXT,
    resolved           BOOLEAN DEFAULT FALSE,
    detected_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS certificate_downloads (
    id SERIAL PRIMARY KEY,
    certificate_id INTEGER,
    downloaded_by VARCHAR(255),
    downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

try:
    cur.execute(SCHEMA)
    print("    [OK] All tables created successfully!")
except Exception as e:
    print(f"    [ERROR] creating tables: {e}")
    exit(1)

# Add missing columns to users table if they don't exist
try:
    cur.execute("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS plain_password VARCHAR(255)
    """)
    print("    [OK] Added missing 'plain_password' column to users table!")
except Exception as e:
    print(f"    [WARNING] Adding plain_password column: {e}")

# Step 3: Create default admin user
print("\n[3] Creating default admin user...")
admin_password = "Admin@2025"
hashed = bcrypt.hashpw(admin_password.encode(), bcrypt.gensalt()).decode()

try:
    cur.execute("""
        INSERT INTO users (username, password_hash, full_name, role)
        VALUES (%s, %s, %s, 'admin')
        ON CONFLICT (username) DO UPDATE SET password_hash = EXCLUDED.password_hash
    """, ('admin', hashed, 'System Administrator'))
    print("    [OK] Admin user created!")
    print(f"       Username : admin")
    print(f"       Password : {admin_password}")
except Exception as e:
    print(f"    [WARNING] Admin user: {e}")

conn.close()

print("\n" + "=" * 55)
print("  [DONE] DATABASE SETUP COMPLETE!")
print("=" * 55)
print("\nNow run the app with:")
print("    python calasiacertificates.py")
print("\nThen open: http://localhost:5000")
print("Login:  admin / Admin@2025")
print()
