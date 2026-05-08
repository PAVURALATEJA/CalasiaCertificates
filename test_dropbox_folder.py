"""
Test Dropbox connection and list what folders actually exist at root.
"""
import os, sys
sys.stdout.reconfigure(encoding='utf-8')
from dotenv import load_dotenv
load_dotenv()

token  = os.environ.get('DROPBOX_ACCESS_TOKEN', '')
folder = os.environ.get('DROPBOX_FOLDER', '/Calibration_Certificates')

print(f"Token length : {len(token)}")
print(f"Configured folder: {repr(folder)}")
print()

try:
    import dropbox as dbx_lib
    dbx = dbx_lib.Dropbox(token)

    # Verify token by getting account info
    try:
        acc = dbx.users_get_current_account()
        print(f"[OK] Token VALID — Account: {acc.name.display_name} ({acc.email})")
    except dbx_lib.exceptions.AuthError as e:
        print(f"[ERROR] Token INVALID / EXPIRED: {e}")
        sys.exit(1)

    print()
    # List ROOT of Dropbox to see what folders exist
    print("=== Root folders in Dropbox ===")
    result = dbx.files_list_folder('', recursive=False)
    for entry in result.entries:
        print(f"  {type(entry).__name__:<15} | {entry.path_lower}")

    print()
    # Now try the configured folder
    print(f"=== Trying to list: {folder} ===")
    try:
        result2 = dbx.files_list_folder(folder, recursive=False)
        print(f"[OK] Folder EXISTS. Entries found: {len(result2.entries)}")
        for entry in result2.entries[:10]:
            print(f"  {type(entry).__name__:<15} | {entry.path_lower}")
    except dbx_lib.exceptions.ApiError as e:
        print(f"[ERROR] Folder NOT FOUND: {e}")

        # Try common variations
        variations = [
            '/calibration_certificates',
            '/Calibration certificates',
            '/certificates',
            '/Certificates',
            '/CalibrationCertificates',
        ]
        print()
        print("Trying alternative folder names...")
        for v in variations:
            try:
                r = dbx.files_list_folder(v, recursive=False)
                print(f"  [FOUND] {v}  ({len(r.entries)} entries)")
            except Exception:
                print(f"  [not found] {v}")

except ImportError:
    print("[ERROR] dropbox package not installed. Run: pip install dropbox")
except Exception as e:
    print(f"[ERROR] {e}")
