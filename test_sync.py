"""
Quick test: clears stale unmatched records and runs a direct sync
to verify the first-underscore parsing works correctly.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

# Patch the environment so app can find settings
os.environ.setdefault('DB_HOST', 'localhost')
os.environ.setdefault('DB_NAME', 'calasia_certs')
os.environ.setdefault('DB_USER', 'postgres')
os.environ.setdefault('DB_PASSWORD', 'Calasia@2025')

import psycopg2

conn = psycopg2.connect(
    host=os.environ['DB_HOST'],
    database=os.environ['DB_NAME'],
    user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'],
    port=os.environ.get('DB_PORT', '5432'),
)
cur = conn.cursor()

# Clear all unresolved unmatched / duplicate records
cur.execute("UPDATE unmatched_files SET resolved=TRUE WHERE resolved=FALSE")
print(f"Cleared {cur.rowcount} stale unmatched records")
cur.execute("UPDATE duplicate_files SET resolved=TRUE WHERE resolved=FALSE")
print(f"Cleared {cur.rowcount} stale duplicate records")
conn.commit()
conn.close()

print("\nNow importing and running sync...\n")

# Import app context and run the sync function directly
from calasiacertificates import app, run_dropbox_sync

with app.app_context():
    stats, err = run_dropbox_sync('test_script')
    print(f"Error: {err}")
    print(f"Stats: {stats}")
    print()
    if err:
        print(f"SYNC ERROR: {err}")
    else:
        print(f"SUCCESS! Added: {stats['success']} | Unmatched: {stats['unmatched']} | Duplicates: {stats['duplicates']} | Errors: {stats['errors']}")
