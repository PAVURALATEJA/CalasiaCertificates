"""
Fix the wrong Dropbox path stored for cert ID=1 (had hyphen, needs underscore).
Run once: python fix_paths.py
"""
import psycopg2

conn = psycopg2.connect(
    host='localhost', database='calasia_certs',
    user='postgres', password='Calasia@2025', port='5432'
)
cur = conn.cursor()

# Cert ID=1 was manually entered with wrong path (hyphen in filename doesn't exist in Dropbox)
# The real Dropbox file is 165067_cal2026_001.pdf (underscore)
cur.execute(
    "UPDATE certificates SET dropbox_file_path=%s WHERE id=1",
    ('/calibration_certificates/165067_cal2026_001.pdf',)
)
print(f'Fixed cert ID=1 path, rows updated: {cur.rowcount}')

conn.commit()
conn.close()
print('Done.')
