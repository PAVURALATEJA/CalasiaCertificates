"""
Diagnostic: Check what paths are in the DB and try to create/fetch shared links.
Run: python diagnose_links.py
"""
import psycopg2
import psycopg2.extras
import os
from dotenv import dotenv_values, load_dotenv
import pathlib

load_dotenv(override=True)

# Always read fresh from .env
env_path = pathlib.Path(__file__).parent / '.env'
fresh = dotenv_values(env_path)
token = fresh.get('DROPBOX_ACCESS_TOKEN') or os.environ.get('DROPBOX_ACCESS_TOKEN', '')

conn = psycopg2.connect(
    host='localhost', database='calasia_certs',
    user='postgres', password='Calasia@2025', port='5432'
)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""
    SELECT id, certificate_number, dropbox_file_path, dropbox_shared_link
    FROM certificates ORDER BY id
""")
rows = cur.fetchall()

print("=== CERTIFICATES IN DB ===")
for r in rows:
    cert = r['certificate_number']
    path = r['dropbox_file_path']
    link = r['dropbox_shared_link']
    print(f"  ID={r['id']}  cert={cert}")
    print(f"    path  = {path}")
    print(f"    link  = {link}")

missing = [r for r in rows if not r['dropbox_shared_link'] and r['dropbox_file_path']]
print(f"\n=== {len(missing)} cert(s) missing shared links ===")

if not token:
    print("ERROR: No DROPBOX_ACCESS_TOKEN in .env!")
    conn.close()
    exit(1)

import dropbox as dbx_lib

dbx = dbx_lib.Dropbox(token)

# Verify token
try:
    acc = dbx.users_get_current_account()
    print(f"Token valid. Account: {acc.name.display_name}")
except Exception as e:
    print(f"Token ERROR: {e}")
    conn.close()
    exit(1)

print("\n=== Attempting to get/create shared links ===")
for r in missing:
    path = r['dropbox_file_path']
    cert = r['certificate_number']
    print(f"\n  Cert: {cert}  Path: {path}")
    
    # 1) Try create
    shared_link = None
    try:
        result = dbx.sharing_create_shared_link_with_settings(path)
        shared_link = result.url.replace('?dl=0', '?raw=1')
        print(f"    -> Created new link: {shared_link[:60]}...")
    except Exception as e:
        err = str(e)
        print(f"    -> Create failed: {err[:120]}")
        
        # 2) If already exists, fetch it
        if 'shared_link_already_exists' in err.lower():
            try:
                links = dbx.sharing_list_shared_links(path=path)
                if links.links:
                    shared_link = links.links[0].url.replace('?dl=0', '?raw=1')
                    print(f"    -> Fetched existing: {shared_link[:60]}...")
                else:
                    print("    -> No existing links found")
            except Exception as e2:
                print(f"    -> List failed: {e2}")
        else:
            # 3) Try listing anyway (path might be wrong case, etc.)
            try:
                links = dbx.sharing_list_shared_links(path=path)
                if links.links:
                    shared_link = links.links[0].url.replace('?dl=0', '?raw=1')
                    print(f"    -> Found via list: {shared_link[:60]}...")
                else:
                    print(f"    -> No links via list either")
            except Exception as e3:
                print(f"    -> List also failed: {e3}")
    
    if shared_link:
        cur2 = conn.cursor()
        cur2.execute(
            "UPDATE certificates SET dropbox_shared_link=%s WHERE id=%s",
            (shared_link, r['id'])
        )
        conn.commit()
        print(f"    -> SAVED to DB!")
    else:
        print(f"    -> COULD NOT fix. Trying path lookup in Dropbox...")
        # 4) Last resort: list folder and find by filename
        try:
            folder = fresh.get('DROPBOX_FOLDER') or '/Calibration_Certificates'
            fname_expected = path.split('/')[-1] if path else ''
            print(f"    -> Looking for filename '{fname_expected}' in {folder}")
            result = dbx.files_list_folder(folder, recursive=True)
            found_path = None
            while True:
                for entry in result.entries:
                    en = getattr(entry, 'name', '')
                    ep = getattr(entry, 'path_lower', '')
                    if en.lower() == fname_expected.lower():
                        found_path = ep
                        print(f"    -> Found at actual path: {ep}")
                        break
                if found_path or not result.has_more:
                    break
                result = dbx.files_list_folder_continue(result.cursor)
            
            if found_path and found_path != path:
                print(f"    -> Path mismatch! DB has '{path}', Dropbox has '{found_path}'")
                # Update path and try link again
                try:
                    lnk = dbx.sharing_create_shared_link_with_settings(found_path)
                    shared_link = lnk.url.replace('?dl=0', '?raw=1')
                except Exception:
                    try:
                        lnks = dbx.sharing_list_shared_links(path=found_path)
                        if lnks.links:
                            shared_link = lnks.links[0].url.replace('?dl=0', '?raw=1')
                    except Exception:
                        pass
                if shared_link:
                    cur2 = conn.cursor()
                    cur2.execute(
                        "UPDATE certificates SET dropbox_shared_link=%s, dropbox_file_path=%s WHERE id=%s",
                        (shared_link, found_path, r['id'])
                    )
                    conn.commit()
                    print(f"    -> Fixed with corrected path! Link saved.")
        except Exception as ef:
            print(f"    -> Folder scan failed: {ef}")

print("\n=== Done ===")
conn.close()
