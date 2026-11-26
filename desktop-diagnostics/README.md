# Daved Desktop Diagnostics (Windows/macOS/Linux)

A small Tkinter client for **user-selected application log files**. It does not read operating-system event logs, browser data, Discord data, keystrokes, or other apps' private data. It does not run in the background.

## Run
- Install Python 3.10+.
- From this folder run: `python diagnostics_client.py`
- Enter your HTTPS Render URL and the same secret configured as `ANDROID_LOGCAT_API_KEY`.
- Select a log file belonging to an application you own/administer.
- Confirm consent and click Upload.

The client sends at most 100 lines (up to 512 KiB read) to the existing authenticated `/api/android-logcat` endpoint. Common token/password/API-key patterns are redacted, but review the file before uploading: redaction cannot catch every secret.

## Build a Windows executable
```powershell
py -m pip install pyinstaller
py -m PyInstaller --onefile --windowed diagnostics_client.py
```
Output: `dist/diagnostics_client.exe`.

This is a source client, not a signed installer. Test with a non-sensitive sample log first. The server dashboard currently labels these entries in the shared Android Logcat table; a future UI pass can rename that page to “Device Diagnostics”.
