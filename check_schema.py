import psycopg2, psycopg2.extras, os
from dotenv import load_dotenv
load_dotenv()
conn = psycopg2.connect(host='localhost', database='calasia_certs', user='postgres', password='Calasia@2025', port='5432')
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='unmatched_files' ORDER BY ordinal_position")
print('unmatched_files columns:', [r['column_name'] for r in cur.fetchall()])

cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='sync_logs' ORDER BY ordinal_position")
print('sync_logs columns:', [r['column_name'] for r in cur.fetchall()])

cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='duplicate_files' ORDER BY ordinal_position")
print('duplicate_files columns:', [r['column_name'] for r in cur.fetchall()])

# Also show all unmatched records for qualcomm noida serials
cur.execute("SELECT * FROM unmatched_files WHERE resolved=FALSE ORDER BY id DESC LIMIT 30")
rows = cur.fetchall()
print(f"\nAll unresolved unmatched_files ({len(rows)} records):")
for r in rows:
    print(dict(r))

conn.close()
