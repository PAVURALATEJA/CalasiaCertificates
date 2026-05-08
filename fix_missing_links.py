"""
Fix missing Dropbox shared links for all certificates in the database.
Run this script once to backfill all NULL dropbox_shared_link values.
"""
import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    'host':     os.environ.get('DB_HOST', 'localhost'),
    'database': os.environ.get('DB_NAME', 'calasia_certs'),
    'user':     os.environ.get('DB_USER', 'postgres'),
    'password': os.environ.get('DB_PASSWORD', 'Calasia@2025'),
    'port':     os.environ.get('DB_PORT', '5432'),
}

DROPBOX_TOKEN = os.environ.get('DROPBOX_ACCESS_TOKEN', '')

def fix_links():
    if not DROPBOX_TOKEN or DROPBOX_TOKEN.strip() == 'your_dropbox_token_here':
        print("ERROR: DROPBOX_ACCESS_TOKEN not configured in .env")
        return

    try:
        import dropbox
    except ImportError:
        print("ERROR: dropbox package not installed. Run: pip install dropbox")
        return

    print("Connecting to Dropbox...")
    dbx = dropbox.Dropbox(DROPBOX_TOKEN)

    # Test the token first
    try:
        account = dbx.users_get_current_account()
        print(f"Dropbox authenticated as: {account.name.display_name}")
    except Exception as e:
        print(f"ERROR: Dropbox authentication failed: {e}")
        print()
        print("Your Dropbox access token has EXPIRED.")
        print("To fix:")
        print("  1. Go to: https://www.dropbox.com/developers/apps")
        print("  2. Open your app > 'Generated access token' > click 'Generate'")
        print("  3. Copy the new token")
        print("  4. Open .env file and update: DROPBOX_ACCESS_TOKEN=<new token>")
        print("  5. Run this script again.")
        return

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Get all certs missing the shared link
    cur.execute("""
        SELECT id, certificate_number, dropbox_file_path
        FROM certificates
        WHERE dropbox_shared_link IS NULL AND dropbox_file_path IS NOT NULL
    """)
    rows = cur.fetchall()
    print(f"Found {len(rows)} certificates missing shared links.")

    fixed = 0
    failed = 0

    for row in rows:
        cert_id = row['id']
        cert_num = row['certificate_number']
        path = row['dropbox_file_path']
        print(f"\n  Cert #{cert_id} ({cert_num}) | Path: {path}")

        shared_link = None

        # Try to create a new shared link
        try:
            result = dbx.sharing_create_shared_link_with_settings(path)
            shared_link = result.url.replace('?dl=0', '?raw=1')
            print(f"    [OK] Created new shared link")
        except Exception as e:
            err_msg = str(e)
            if 'shared_link_already_exists' in err_msg.lower():
                # Link already exists - just fetch it
                print(f"    [INFO] Link already exists, fetching it...")
                try:
                    links = dbx.sharing_list_shared_links(path=path)
                    if links.links:
                        shared_link = links.links[0].url.replace('?dl=0', '?raw=1')
                        print(f"    [OK] Fetched existing link")
                    else:
                        print(f"    [FAIL] No links found for path.")
                except Exception as e2:
                    print(f"    [FAIL] Failed to fetch existing link: {e2}")
            else:
                print(f"    [FAIL] Error creating link: {err_msg}")

        if shared_link:
            # Update the database
            update_cur = conn.cursor()
            update_cur.execute(
                "UPDATE certificates SET dropbox_shared_link=%s WHERE id=%s",
                (shared_link, cert_id)
            )
            conn.commit()
            fixed += 1
            print(f"    [OK] Database updated for cert #{cert_id}")
        else:
            failed += 1

    print(f"\n{'='*50}")
    print(f"Done! Fixed: {fixed} | Failed: {failed}")
    if fixed > 0:
        print("Customers can now download their certificates.")
    conn.close()

if __name__ == '__main__':
    fix_links()
