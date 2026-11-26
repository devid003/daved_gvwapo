"""Opt-in desktop diagnostics uploader for the owner's own application logs.
Only uploads a log file explicitly selected by the user after consent.
"""
import json
import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

MAX_BYTES = 512 * 1024
MAX_LINES = 100
REDACT = re.compile(r"(?i)(token|password|authorization|api[_-]?key)\s*[:=]\s*[^\s,;]+")

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Daved Desktop Diagnostics")
        self.geometry("620x430")
        self.resizable(False, False)
        self.log_path = tk.StringVar()
        self.server = tk.StringVar(value="https://onlinemoney-sauce.onrender.com")
        self.api_key = tk.StringVar()
        self.consent = tk.BooleanVar(value=False)
        ttk.Label(self, text="Daved Desktop Diagnostics", font=("Segoe UI", 17, "bold")).pack(pady=12)
        ttk.Label(self, text="Uploads only the log file you choose. No background collection.").pack()
        self._field("Server URL", self.server)
        self._field("API key", self.api_key, show="•")
        row=ttk.Frame(self); row.pack(fill="x", padx=20, pady=8)
        ttk.Entry(row, textvariable=self.log_path).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Choose log file…", command=self.choose).pack(side="left", padx=6)
        ttk.Checkbutton(self, text="I own/administer this device and consent to upload this selected log.", variable=self.consent).pack(pady=12)
        ttk.Button(self, text="Upload selected log", command=self.upload).pack(pady=8)
        self.status=ttk.Label(self, text="Ready — nothing is collected until you select a file and upload.")
        self.status.pack(pady=12)

    def _field(self, label, var, show=None):
        frame=ttk.Frame(self); frame.pack(fill="x", padx=20, pady=5)
        ttk.Label(frame, text=label, width=13).pack(side="left")
        ttk.Entry(frame, textvariable=var, show=show).pack(side="left", fill="x", expand=True)

    def choose(self):
        path=filedialog.askopenfilename(title="Select your application log", filetypes=[("Log files","*.log *.txt"),("All files","*.*")])
        if path: self.log_path.set(path)

    def upload(self):
        if not self.consent.get():
            messagebox.showwarning("Consent required", "Please confirm ownership and consent first."); return
        path=self.log_path.get().strip()
        key=self.api_key.get().strip()
        server=self.server.get().strip().rstrip("/")
        if not path or not os.path.isfile(path) or not key or not server.startswith("https://"):
            messagebox.showerror("Missing details", "Choose a log file, enter the API key, and use an HTTPS server URL."); return
        try:
            with open(path, "rb") as f: raw=f.read(MAX_BYTES+1)
            if len(raw)>MAX_BYTES: raw=raw[:MAX_BYTES]
            lines=raw.decode("utf-8", errors="replace").splitlines()[-MAX_LINES:]
            entries=[{"device_id":"desktop-user-selected","device_name":os.environ.get("COMPUTERNAME","Desktop"),"app_version":"desktop-client-1.0","priority":"I","tag":"USER_SELECTED_LOG","message":REDACT.sub(r"\1=[redacted]", line)[:4000]} for line in lines if line.strip()]
            if not entries: raise ValueError("The selected file has no readable log lines.")
            body=json.dumps({"consent":True,"entries":entries}).encode()
            req=Request(server+"/api/android-logcat", data=body, headers={"Content-Type":"application/json","X-Logcat-Key":key}, method="POST")
            with urlopen(req, timeout=20) as response: result=json.loads(response.read().decode())
            self.status.config(text=f"Uploaded {result.get('accepted', len(entries))} log entries.")
            messagebox.showinfo("Upload complete", "Selected log entries were sent.")
        except (OSError, ValueError, HTTPError, URLError, TimeoutError) as e:
            self.status.config(text="Upload failed.")
            messagebox.showerror("Upload failed", str(e))

if __name__ == "__main__":
    App().mainloop()
