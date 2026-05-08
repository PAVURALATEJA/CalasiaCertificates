"""
ONE-TIME Dropbox OAuth2 Setup Script
=====================================
Run this ONCE to get a permanent refresh token.
After running, DROPBOX_REFRESH_TOKEN will be saved to your .env file automatically.

Steps:
1. Go to https://www.dropbox.com/developers/apps
2. Click "Create app" → "Scoped access" → "Full Dropbox" → name it "CalAsiaSync"
3. Under "Permissions" tab, enable: files.content.read, files.metadata.read, sharing.read
4. Under "Settings" tab, copy your App key and App secret
5. Set them in .env as DROPBOX_APP_KEY and DROPBOX_APP_SECRET
6. Run this script once: python dropbox_auth_setup.py
"""

import os
import sys
import webbrowser
import urllib.parse
import pathlib
from dotenv import dotenv_values

ENV_PATH = pathlib.Path(__file__).parent / '.env'
env = dotenv_values(ENV_PATH)

APP_KEY    = env.get('DROPBOX_APP_KEY', '').strip()
APP_SECRET = env.get('DROPBOX_APP_SECRET', '').strip()

if not APP_KEY or not APP_SECRET:
    print("=" * 60)
    print("ERROR: DROPBOX_APP_KEY and DROPBOX_APP_SECRET not found in .env")
    print()
    print("Steps to fix:")
    print("  1. Go to https://www.dropbox.com/developers/apps")
    print("  2. Create a new app (Scoped access, Full Dropbox)")
    print("     Permissions needed: files.content.read, files.metadata.read, sharing.read")
    print("  3. Copy the App key and App secret from the Settings tab")
    print("  4. Add these lines to your .env file:")
    print("       DROPBOX_APP_KEY=your_app_key_here")
    print("       DROPBOX_APP_SECRET=your_app_secret_here")
    print("  5. Re-run this script")
    print("=" * 60)
    sys.exit(1)

try:
    import dropbox
    from dropbox import DropboxOAuth2FlowNoRedirect
except ImportError:
    print("ERROR: dropbox package not installed. Run: pip install dropbox")
    sys.exit(1)

print("=" * 60)
print("  Cal-Asia Dropbox OAuth2 Setup")
print("=" * 60)
print()

auth_flow = DropboxOAuth2FlowNoRedirect(
    APP_KEY,
    consumer_secret=APP_SECRET,
    token_access_type='offline'   # <-- this gives a permanent refresh token
)

authorize_url = auth_flow.start()

print("Step 1: Open the following URL in your browser (opening automatically)...")
print()
print(f"  {authorize_url}")
print()
webbrowser.open(authorize_url)

print("Step 2: Click 'Allow' on the Dropbox page.")
print("Step 3: Copy the authorization code shown and paste it below.")
print()

auth_code = input("Enter the authorization code: ").strip()

try:
    oauth_result = auth_flow.finish(auth_code)
except Exception as e:
    print(f"\nERROR: Could not complete authorization: {e}")
    sys.exit(1)

refresh_token = oauth_result.refresh_token
account_id    = oauth_result.account_id

print()
print(f"[OK] Authorization successful!")
print(f"     Account ID    : {account_id}")
print(f"     Refresh Token : {refresh_token[:20]}...  (saved to .env)")
print()

# --- Save refresh token to .env ---
env_text = ENV_PATH.read_text(encoding='utf-8') if ENV_PATH.exists() else ''

def _set_env_var(text, key, value):
    """Set or add a key=value line in .env text."""
    lines = text.splitlines()
    found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f'{key}=') or stripped.startswith(f'{key} ='):
            new_lines.append(f'{key}={value}')
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f'{key}={value}')
    return '\n'.join(new_lines) + '\n'

# Remove old access token, add refresh token
env_text = _set_env_var(env_text, 'DROPBOX_REFRESH_TOKEN', refresh_token)
# Comment out old access token if present
env_text_lines = env_text.splitlines()
new_env_lines = []
for line in env_text_lines:
    if line.strip().startswith('DROPBOX_ACCESS_TOKEN='):
        new_env_lines.append('# ' + line + '  # replaced by DROPBOX_REFRESH_TOKEN')
    else:
        new_env_lines.append(line)
env_text = '\n'.join(new_env_lines) + '\n'

ENV_PATH.write_text(env_text, encoding='utf-8')

print("[OK] DROPBOX_REFRESH_TOKEN has been saved to .env")
print("[OK] The old DROPBOX_ACCESS_TOKEN line has been commented out.")
print()
print("=" * 60)
print("  Setup complete! The refresh token NEVER expires.")
print("  Your sync will now work permanently without manual token updates.")
print("=" * 60)
print()
print("Verify Dropbox connection now? (y/n): ", end='')
if input().strip().lower() == 'y':
    dbx = dropbox.Dropbox(
        oauth2_refresh_token=refresh_token,
        app_key=APP_KEY,
        app_secret=APP_SECRET
    )
    acc = dbx.users_get_current_account()
    print(f"[OK] Connected as: {acc.name.display_name} ({acc.email})")
    folder = env.get('DROPBOX_FOLDER', '/Calibration_Certificates')
    try:
        result = dbx.files_list_folder(folder, recursive=False)
        print(f"[OK] Folder '{folder}' found — {len(result.entries)} items")
    except Exception as fe:
        print(f"[WARN] Folder '{folder}' not found: {fe}")
        print("       Create this folder in Dropbox, then upload PDFs to it.")
