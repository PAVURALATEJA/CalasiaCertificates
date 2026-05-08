import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
load_dotenv()

conn = psycopg2.connect(
    host='localhost', database='calasia_certs',
    user='postgres', password='Calasia@2025', port='5432'
)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute('SELECT id, certificate_number, dropbox_file_path, dropbox_shared_link FROM certificates ORDER BY id')
rows = cur.fetchall()
print(f"Total certificates: {len(rows)}")
for r in rows:
    link = r['dropbox_shared_link']
    path = r['dropbox_file_path']
    print(f"  ID={r['id']} | Cert={r['certificate_number']} | Path={path} | Link={'SET' if link else 'NULL/MISSING'}")
conn.close()
