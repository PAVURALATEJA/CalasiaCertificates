import psycopg2
import psycopg2.extras
import re
import os
from dotenv import load_dotenv

load_dotenv()

conn = psycopg2.connect(
    host=os.environ.get('DB_HOST', 'localhost'),
    database=os.environ.get('DB_NAME', 'calasia_certs'),
    user=os.environ.get('DB_USER', 'postgres'),
    password=os.environ.get('DB_PASSWORD', 'Calasia@2025'),
    port=os.environ.get('DB_PORT', '5432'),
)

cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

print("=" * 55)
print("  DB INSTRUMENTS + SERIAL NUMBERS")
print("=" * 55)
cur.execute("SELECT id, serial_number, instrument_name FROM instruments ORDER BY id")
instruments = cur.fetchall()
for r in instruments:
    serial = r['serial_number']
    print(f"  id={r['id']} | name={r['instrument_name']}")
    print(f"         serial=[{serial}]  len={len(serial)}")
    print(f"         repr={repr(serial)}")

print()
print("=" * 55)
print("  MATCHING TESTS (using exact sync logic)")
print("=" * 55)

test_filenames = [
    "B012036_CAL2026-001.pdf",
    "165067_CAL2026-001.pdf",
    "b012036_CAL2026-001.pdf",
]

pattern = r'^([^\s_]+)_([^\s_]+)\.pdf$'

for fname in test_filenames:
    m = re.match(pattern, fname, re.IGNORECASE)
    if m:
        serial = m.group(1).strip()
        cert = m.group(2).strip()
        print(f"\n  File: {fname}")
        print(f"  Extracted serial: [{serial}]")
        cur.execute(
            "SELECT id, instrument_name FROM instruments WHERE LOWER(TRIM(serial_number))=LOWER(TRIM(%s))",
            (serial,)
        )
        match = cur.fetchone()
        if match:
            print(f"  >>> MATCHED: instrument_id={match['id']} name={match['instrument_name']}")
        else:
            print(f"  >>> NO MATCH in DB")
            # Try partial match to see what's close
            cur.execute("SELECT id, serial_number FROM instruments")
            all_instr = cur.fetchall()
            print(f"  >>> DB serials for comparison:")
            for i in all_instr:
                print(f"       [{i['serial_number']}] repr={repr(i['serial_number'])}")
    else:
        print(f"\n  File: {fname}  >>> REGEX FAILED")

print()
print("=" * 55)
print("  UNMATCHED FILE COUNT AND RECENT ENTRIES")
print("=" * 55)
cur.execute("SELECT COUNT(*) AS c FROM unmatched_files WHERE resolved=FALSE")
cnt = cur.fetchone()
print(f"  Total unresolved unmatched: {cnt['c']}")
cur.execute("SELECT file_name, serial_number, reason, detected_at FROM unmatched_files WHERE resolved=FALSE ORDER BY detected_at DESC LIMIT 5")
rows = cur.fetchall()
for r in rows:
    print(f"  [{r['detected_at']}] {r['file_name']} | serial={r['serial_number']}")
    print(f"    reason: {r['reason']}")

conn.close()
print()
print("Done.")
