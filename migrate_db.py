"""Run DB migrations: add asset_number to instruments, plain_password to users"""
import os
from dotenv import load_dotenv
load_dotenv()
import psycopg2

conn = psycopg2.connect(
    host=os.environ.get('DB_HOST', 'localhost'),
    database=os.environ.get('DB_NAME', 'calasia_certs'),
    user=os.environ.get('DB_USER', 'postgres'),
    password=os.environ.get('DB_PASSWORD', 'Calasia@2025'),
    port=os.environ.get('DB_PORT', '5432'),
)
cur = conn.cursor()

migrations = [
    ("Add asset_number to instruments",
     "ALTER TABLE instruments ADD COLUMN IF NOT EXISTS asset_number VARCHAR(100)"),
    ("Add plain_password to users",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS plain_password VARCHAR(200)"),
]

for desc, sql in migrations:
    try:
        cur.execute(sql)
        print(f"[OK] {desc}")
    except Exception as e:
        print(f"[SKIP] {desc}: {e}")

conn.commit()
conn.close()
print("Done.")
