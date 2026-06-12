"""
3SX Netplay Launcher.

Pre-game GUI for:
- Login / Register against Fistbump server
- Game settings (fullscreen, scale mode, server override)
- Launch 3sx.exe with proper args

No third-party deps. Pure Tkinter.
"""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import zipfile
from pathlib import Path
from tkinter import ttk, messagebox
from typing import Optional

APP_VERSION = "1.7.28"  # launcher version (this Python app + UI)
GAME_VERSION = "1.7.28"  # game build packaged in this launcher's zip (3sx.exe content)

# Verbose mode: print discovered paths, env vars, and the exact args used to
# launch the game. Toggle with `--verbose` / `-v` on the command line or via
# the FISTBUMP_VERBOSE=1 env var. Useful for diagnosing "the launcher started
# the wrong server" / "regions list is wrong" reports without attaching a
# debugger.
VERBOSE = "--verbose" in sys.argv or "-v" in sys.argv or os.environ.get("FISTBUMP_VERBOSE", "0") not in ("", "0", "false", "False")

def vlog(*args, **kwargs):
    if VERBOSE:
        print("[launcher]", *args, file=sys.stderr, **kwargs)

DEFAULT_SERVER = os.environ.get("FISTBUMP_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("FISTBUMP_PORT", "19000"))

# Region list rendered in the launcher dropdown. Override at build time by
# editing this constant, or at runtime by setting FISTBUMP_REGIONS to a
# semicolon-separated list of `code|label|host|port` entries.
_DEFAULT_REGIONS = [
    ("default", "Default", DEFAULT_SERVER, DEFAULT_PORT),
]


def _parse_regions_env(raw: str):
    out = []
    for entry in raw.split(";"):
        parts = entry.split("|")
        if len(parts) != 4:
            continue
        try:
            out.append((parts[0].strip(), parts[1].strip(), parts[2].strip(), int(parts[3].strip())))
        except ValueError:
            continue
    return out


REGIONS = _parse_regions_env(os.environ.get("FISTBUMP_REGIONS", "")) or _DEFAULT_REGIONS

vlog(f"APP_VERSION={APP_VERSION} GAME_VERSION={GAME_VERSION}")
vlog(f"DEFAULT_SERVER={DEFAULT_SERVER}:{DEFAULT_PORT}")
vlog(f"FISTBUMP_REGIONS env: {os.environ.get('FISTBUMP_REGIONS', '(unset)')}")
vlog(f"REGIONS effective:    {REGIONS}")
vlog(f"sys.executable={sys.executable!r}  frozen={getattr(sys, 'frozen', False)}")
vlog(f"argv={sys.argv}")


def _icd_friendly_name(path: str) -> str:
    lower = path.lower()
    if "nv" in lower and ("vk" in lower or "vulkan" in lower):
        return "NVIDIA"
    if "amd" in lower or "amdvlk" in lower:
        return "AMD"
    if "intel" in lower:
        return "Intel"
    return Path(path).stem


def _detect_vulkan_icds() -> list:
    """Returns [(friendly_name, json_path), ...] of Vulkan ICDs.
    Tries Khronos registry first, then scans known filesystem locations
    (some OEM drivers don't register ICDs but ship them in DriverStore).
    Forcing VK_ICD_FILENAMES to one ICD constrains the loader to a single
    vendor's GPUs — dual-GPU workaround."""
    results = []
    seen = set()

    # --- Registry: HKLM/HKCU Khronos\Vulkan\Drivers (value name = json path) ---
    try:
        import winreg
        candidates = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Khronos\Vulkan\Drivers"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Khronos\Vulkan\Drivers"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Khronos\Vulkan\Drivers"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\WOW6432Node\Khronos\Vulkan\Drivers"),
        ]
        for hive, subkey in candidates:
            try:
                key = winreg.OpenKey(hive, subkey)
            except OSError:
                continue
            try:
                i = 0
                while True:
                    try:
                        name, _, _ = winreg.EnumValue(key, i)
                        i += 1
                    except OSError:
                        break
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    results.append((_icd_friendly_name(name), name))
            finally:
                try:
                    winreg.CloseKey(key)
                except Exception:
                    pass
    except ImportError:
        pass

    # --- Filesystem fallback: known ICD JSON locations ---
    sys32 = Path(os.environ.get("WINDIR", "C:/Windows")) / "System32"
    for cand in [
        sys32 / "nv-vk64.json",
        sys32 / "nv-vk32.json",
        sys32 / "amd-vulkan-icd-x64.json",
        sys32 / "amd_icd64.json",
        sys32 / "amdvlk64.json",
        sys32 / "intel_icd.x64.json",
        sys32 / "Drivers" / "intel_icd.x64.json",
    ]:
        try:
            if cand.exists():
                p = str(cand)
                if p not in seen:
                    seen.add(p)
                    results.append((_icd_friendly_name(p), p))
        except Exception:
            pass

    # --- DriverStore scan (OEM/MS-distributed drivers) ---
    driver_store = sys32 / "DriverStore" / "FileRepository"
    if driver_store.exists():
        try:
            for sub in driver_store.iterdir():
                if not sub.is_dir():
                    continue
                lname = sub.name.lower()
                if not any(s in lname for s in ("nv", "amd", "intel", "ig")):
                    continue
                try:
                    for jp in sub.glob("*.json"):
                        fname = jp.name.lower()
                        if "vk" not in fname and "vulkan" not in fname and "icd" not in fname:
                            continue
                        p = str(jp)
                        if p not in seen:
                            seen.add(p)
                            results.append((_icd_friendly_name(p), p))
                except Exception:
                    continue
        except Exception:
            pass

    try:
        print(f"[Launcher] Vulkan ICDs detected: {len(results)}", flush=True)
        for fn, p in results:
            print(f"  - {fn}: {p}", flush=True)
    except Exception:
        pass

    return results

UPDATE_BASE_URL = os.environ.get(
    "FISTBUMP_UPDATE_BASE_URL",
    f"http://{DEFAULT_SERVER}:{DEFAULT_PORT + 1000}",
)
RELEASE_CHANNELS = ("stable", "beta")
DEFAULT_CHANNEL = "stable"

# Update source. Default: the GitHub Releases page of the public repo —
# players re-download from github.com (HTTPS, public checksums) instead of
# pulling zips off the matchmaking host. Forks that self-host a manifest
# channel can set FISTBUMP_UPDATE_BASE_URL to restore the legacy in-place
# updater (UPDATE_BASE_URL is still used for log telemetry either way).
UPDATE_USE_MANIFEST = "FISTBUMP_UPDATE_BASE_URL" in os.environ
UPDATE_REPO = os.environ.get("FISTBUMP_UPDATE_REPO", "SalieriMZ/3sx-online")


def update_manifest_url(channel: str) -> str:
    if channel not in RELEASE_CHANNELS:
        channel = DEFAULT_CHANNEL
    return f"{UPDATE_BASE_URL}/api/update/{channel}.json"


def _resolve_default_exe() -> str:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    return str(base / "3sx.exe")


DEFAULT_EXE = _resolve_default_exe()


def _resolve_exe(cfg: dict) -> str:
    """Prefer 3sx.exe sitting next to the launcher; fall back to cfg override
    only if the local copy is missing. Keeps installs portable across PCs even
    when launcher.json has a stale absolute path from an older install."""
    if Path(DEFAULT_EXE).exists():
        return DEFAULT_EXE
    return cfg.get("exe") or DEFAULT_EXE

CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "CrowdedStreet" / "3SX"
CONFIG_FILE = CONFIG_DIR / "launcher.json"
GAME_CONFIG_FILE = CONFIG_DIR / "config"
SECRET_FILE = CONFIG_DIR / ".cred"  # DPAPI-encrypted password (Windows)


def _dpapi_protect(data: bytes) -> bytes:
    """Encrypt with Windows DPAPI (machine + user scope). Returns ciphertext."""
    try:
        import ctypes
        from ctypes import wintypes
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        blob_in = DATA_BLOB(len(data), ctypes.cast(ctypes.c_char_p(data), ctypes.POINTER(ctypes.c_byte)))
        blob_out = DATA_BLOB()
        if not crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
            return b""
        out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        kernel32.LocalFree(blob_out.pbData)
        return out
    except Exception:
        return b""


def _dpapi_unprotect(data: bytes) -> bytes:
    try:
        import ctypes
        from ctypes import wintypes
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        blob_in = DATA_BLOB(len(data), ctypes.cast(ctypes.c_char_p(data), ctypes.POINTER(ctypes.c_byte)))
        blob_out = DATA_BLOB()
        if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
            return b""
        out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        kernel32.LocalFree(blob_out.pbData)
        return out
    except Exception:
        return b""


def save_password(pw: str) -> bool:
    """Persist password encrypted via Windows DPAPI. Returns True on success."""
    if not pw:
        return False
    ciphertext = _dpapi_protect(pw.encode("utf-8"))
    if not ciphertext:
        return False
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(SECRET_FILE, "wb") as f:
            f.write(ciphertext)
        os.chmod(SECRET_FILE, 0o600)
        return True
    except Exception:
        return False


def load_password() -> str:
    try:
        with open(SECRET_FILE, "rb") as f:
            data = f.read()
        pt = _dpapi_unprotect(data)
        return pt.decode("utf-8") if pt else ""
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def clear_password():
    try:
        SECRET_FILE.unlink()
    except FileNotFoundError:
        pass


def load_launcher_config() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_launcher_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def load_game_config() -> dict:
    """Parse the 3sx game config file (KEY=VALUE lines)."""
    cfg = {}
    try:
        with open(GAME_CONFIG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"game config read err: {e}", file=sys.stderr)
    return cfg


def save_game_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in cfg.items()]
    with open(GAME_CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def server_request(host: str, port: int, lines: list[str], timeout: float = 5.0) -> list[str]:
    """Send TCP lines, collect responses. Returns list of decoded lines."""
    responses = []
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        buf = b""
        deadline = time.time() + timeout
        for line in lines:
            payload = (line + "\n").encode("utf-8")
            s.send(payload)
        while time.time() < deadline:
            try:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    responses.append(line.decode("utf-8", errors="ignore").strip())
                # Heuristic stop: if any line starts with REJECT or PROFILE, we're done
                if any(r.startswith("REJECT") or r.startswith("PROFILE") for r in responses):
                    break
            except socket.timeout:
                break
    finally:
        s.close()
    return responses


class LauncherApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("3SX Netplay Launcher")
        self.root.geometry("520x540")
        self.root.configure(bg="#0d1117")

        self.cfg = load_launcher_config()
        self.game_cfg = load_game_config()

        # Style
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background="#0d1117", foreground="#c9d1d9", fieldbackground="#161b22")
        style.configure("TNotebook", background="#0d1117", borderwidth=0)
        style.configure("TNotebook.Tab", background="#161b22", foreground="#c9d1d9", padding=[16, 6])
        style.map("TNotebook.Tab", background=[("selected", "#1c2128")], foreground=[("selected", "#f0f6fc")])
        style.configure("TLabel", background="#0d1117", foreground="#c9d1d9")
        style.configure("TButton", background="#1f6feb", foreground="#ffffff", padding=[12, 6])
        style.map("TButton", background=[("active", "#2884ff")])
        style.configure("TEntry", fieldbackground="#161b22", foreground="#f0f6fc", insertcolor="#f0f6fc")
        style.configure("TCheckbutton", background="#0d1117", foreground="#c9d1d9")
        # Combobox: ttk's default Windows theme renders the dropdown as a
        # white listbox with white selection — invisible on our dark theme.
        # Force the entry field, button, and listbox to dark colors via
        # both style.configure (entry/button) and root.option_add (the
        # listbox is a plain Tk widget, not ttk-themed).
        style.configure("TCombobox",
                        fieldbackground="#161b22",
                        background="#161b22",
                        foreground="#f0f6fc",
                        selectbackground="#1f6feb",
                        selectforeground="#ffffff",
                        arrowcolor="#c9d1d9")
        style.map("TCombobox",
                  fieldbackground=[("readonly", "#161b22"), ("disabled", "#0d1117")],
                  background=[("readonly", "#161b22"), ("active", "#1f6feb")],
                  foreground=[("readonly", "#f0f6fc"), ("disabled", "#6e7681")])
        root.option_add("*TCombobox*Listbox.background", "#161b22")
        root.option_add("*TCombobox*Listbox.foreground", "#f0f6fc")
        root.option_add("*TCombobox*Listbox.selectBackground", "#1f6feb")
        root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

        # Header
        header = tk.Frame(root, bg="#0d1117")
        header.pack(fill="x", padx=20, pady=(20, 10))
        tk.Label(header, text="3SX", fg="#ff3030", bg="#0d1117",
                 font=("Segoe UI", 24, "bold")).pack(side="left")
        tk.Label(header, text="Netplay Launcher", fg="#c9d1d9", bg="#0d1117",
                 font=("Segoe UI", 14)).pack(side="left", padx=8, pady=(8, 0))
        tk.Label(header,
                 text=f"Launcher v{APP_VERSION}  ·  Game v{GAME_VERSION}",
                 fg="#8b949e", bg="#0d1117",
                 font=("Segoe UI", 9)).pack(side="right", padx=8, pady=(14, 0))

        # Tabs. Beta 1.7.2 slim: account/region/force-relay/chat/queue all
        # live in the in-game ImGui overlay now. Launcher keeps only the
        # update-channel + GPU picker + Play button.
        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=20, pady=10)
        # tab_account kept as a hidden frame so legacy _build_account_tab
        # code keeps compiling (auth functions still live in the file as
        # dead code for now; full cleanup is a follow-up).
        self.tab_account = ttk.Frame(nb)
        self.tab_settings = ttk.Frame(nb)
        self.tab_play = ttk.Frame(nb)
        nb.add(self.tab_settings, text="Settings")
        nb.add(self.tab_play, text="Play")
        self._build_account_tab()  # populates hidden frame, harmless
        self._build_settings_tab()
        self._build_play_tab()

        root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Status bar
        self.status_var = tk.StringVar(value="Ready.")
        status = tk.Label(root, textvariable=self.status_var, fg="#8b949e", bg="#161b22",
                          anchor="w", padx=12, font=("Consolas", 9))
        status.pack(side="bottom", fill="x")

        self.root.after(500, self._clear_boot_pending)
        threading.Thread(target=self._check_update, daemon=True).start()
        threading.Thread(target=self._log_upload_loop, daemon=True).start()

    # ---------- Account ----------

    def _build_account_tab(self):
        frame = self.tab_account
        for w in frame.winfo_children():
            w.destroy()
        pad = {"padx": 20, "pady": 8}

        ttk.Label(frame, text="Username").grid(row=0, column=0, sticky="w", **pad)
        self.username_var = tk.StringVar(value=self.cfg.get("username", ""))
        ttk.Entry(frame, textvariable=self.username_var, width=30).grid(row=0, column=1, **pad)

        ttk.Label(frame, text="Password").grid(row=1, column=0, sticky="w", **pad)
        saved_pw = load_password()
        self.password_var = tk.StringVar(value=saved_pw)
        ttk.Entry(frame, textvariable=self.password_var, show="•", width=30).grid(row=1, column=1, **pad)

        self.remember_pw_var = tk.BooleanVar(value=bool(saved_pw) or self.cfg.get("remember_password", False))
        ttk.Checkbutton(frame, text="Remember password (encrypted, this machine only)",
                        variable=self.remember_pw_var).grid(row=2, column=0, columnspan=2, sticky="w", **pad)

        btns = tk.Frame(frame, bg="#0d1117")
        btns.grid(row=3, column=0, columnspan=2, pady=20)
        ttk.Button(btns, text="Login", command=self._do_login).pack(side="left", padx=6)
        ttk.Button(btns, text="Register", command=self._do_register).pack(side="left", padx=6)
        ttk.Button(btns, text="View Leaderboard", command=self._open_leaderboard).pack(side="left", padx=6)
        ttk.Button(btns, text="Forget Password", command=self._forget_password).pack(side="left", padx=6)

    def _forget_password(self):
        clear_password()
        self.password_var.set("")
        self.remember_pw_var.set(False)
        self.cfg["remember_password"] = False
        save_launcher_config(self.cfg)
        self.status_var.set("Saved password cleared.")

    def _open_leaderboard(self):
        import webbrowser
        # Public-facing site lives behind the HTTPS nginx proxy. Raw IP:20000
        # is loopback-only; the stats HTTP listener was never bound publicly.
        webbrowser.open(UPDATE_BASE_URL + "/")

    def _do_login(self):
        self._auth("LOGIN")

    def _do_register(self):
        self._auth("REGISTER")

    def _auth(self, action: str):
        user = self.username_var.get().strip()
        pw = self.password_var.get()
        host = self.cfg.get("server", DEFAULT_SERVER)
        port = int(self.cfg.get("port", DEFAULT_PORT))
        if not user or not pw:
            messagebox.showerror("Missing fields", "Username and password required")
            return

        self.status_var.set(f"{action.lower()}ing {user}@{host}:{port}...")
        threading.Thread(target=self._auth_worker, args=(action, user, pw, host, port), daemon=True).start()

    def _auth_worker(self, action: str, user: str, pw: str, host: str, port: int):
        try:
            cmds = [f"{action} {user} {pw} {APP_VERSION}"]
            responses = server_request(host, port, cmds, timeout=5.0)
        except Exception as e:
            self.root.after(0, lambda: self.status_var.set(f"Connection failed: {e}"))
            return

        token = None
        profile = None
        reject = None
        for r in responses:
            if r.startswith("REJECT "):
                reject = r[7:]
            elif r.startswith("TOKEN refresh "):
                parts = r.split(" ", 3)
                if len(parts) >= 3:
                    token = parts[2]
            elif r.startswith("PROFILE "):
                profile = r[8:]

        if reject:
            self.root.after(0, lambda: self._auth_done(False, f"{action} rejected: {reject}"))
            return
        if not token:
            self.root.after(0, lambda: self._auth_done(False, "No token received"))
            return

        # Persist launcher cfg
        self.cfg["username"] = user
        self.cfg["server"] = host
        self.cfg["port"] = port
        self.cfg["remember_password"] = self.remember_pw_var.get()
        save_launcher_config(self.cfg)
        # Save password encrypted if user opted in
        if self.remember_pw_var.get():
            save_password(pw)
        else:
            clear_password()

        # The game-side token file is at CONFIG_DIR/token (fistbump.c LoadToken)
        try:
            with open(CONFIG_DIR / "token", "w", encoding="utf-8") as f:
                # Token expiry isn't directly known here; trust it for 30 days
                expiry = int(time.time()) + 30 * 24 * 3600
                f.write(f"{token}\n{expiry}\n")
        except Exception as e:
            self.root.after(0, lambda: self._auth_done(False, f"Token save failed: {e}"))
            return

        self.root.after(0, lambda: self._auth_done(True, f"{action} ok — welcome {profile}"))

    def _auth_done(self, ok: bool, msg: str):
        self.status_var.set(msg)
        if ok:
            messagebox.showinfo("Success", msg)

    # ---------- Settings ----------

    def _build_settings_tab(self):
        frame = self.tab_settings
        for w in frame.winfo_children():
            w.destroy()
        pad = {"padx": 20, "pady": 6}

        ttk.Label(frame, text="Launcher Settings",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", **pad)

        # Region picker / force-relay / 3sx.exe path / custom server live in
        # the in-game overlay (3SX Account modal + F3 → Connection) now —
        # region auto-picks lowest-ping on Network menu entry.

        # Vulkan GPU picker (dual-GPU workaround). Auto = let loader pick.
        ttk.Label(frame, text="GPU (Vulkan)").grid(row=1, column=0, sticky="w", **pad)
        self._vulkan_icds = _detect_vulkan_icds()
        gpu_values = ["Auto"] + [f"{friendly}" for friendly, _ in self._vulkan_icds]
        saved_gpu = self.cfg.get("vulkan_icd", "")
        current_label = "Auto"
        for friendly, json_path in self._vulkan_icds:
            if json_path == saved_gpu:
                current_label = friendly
                break
        self.gpu_var = tk.StringVar(value=current_label)
        gpu_combo = ttk.Combobox(frame, textvariable=self.gpu_var,
                                 values=gpu_values, state="readonly", width=24)
        gpu_combo.grid(row=1, column=1, sticky="w", **pad)
        gpu_combo.bind("<<ComboboxSelected>>", lambda e: self._save_gpu_choice())

        adv_row = 1  # row anchor for the rows below (kept for diff-readability)

        # Telemetry opt-out. Sends netplay.log + console.log to the server
        # every 30 s while logged in — used to debug netplay regressions.
        self.telemetry_var = tk.BooleanVar(value=bool(self.cfg.get("telemetry_enabled", True)))
        ttk.Checkbutton(frame, text="Upload netplay logs (helps debug — no chat, no inputs)",
                        variable=self.telemetry_var,
                        command=self._save_telemetry).grid(
            row=adv_row + 5, column=0, columnspan=2, sticky="w", **pad)

        # Release channel selector (stable / beta). Beta = preview builds.
        ttk.Label(frame, text="Release channel").grid(row=adv_row + 6, column=0, sticky="w", **pad)
        saved_channel = self.cfg.get("channel", DEFAULT_CHANNEL)
        if saved_channel not in RELEASE_CHANNELS:
            saved_channel = DEFAULT_CHANNEL
        self.channel_var = tk.StringVar(value=saved_channel)
        channel_combo = ttk.Combobox(frame, textvariable=self.channel_var,
                                     values=list(RELEASE_CHANNELS),
                                     state="readonly", width=24)
        channel_combo.grid(row=adv_row + 6, column=1, sticky="w", **pad)
        channel_combo.bind("<<ComboboxSelected>>", lambda e: self._save_channel())

        ttk.Button(frame, text="Open log folder",
                   command=lambda: subprocess.Popen(["explorer", str(CONFIG_DIR)])).grid(
            row=adv_row + 7, column=0, pady=20, padx=20, sticky="w")

        ttk.Label(frame,
                  text="GPU / Channel / Telemetry auto-save on change.\n"
                       "GPU picker forces Vulkan loader to one vendor — fixes\n"
                       "dual-GPU crash. Pick NVIDIA on laptops with iGPU+dGPU.\n"
                       "Channel beta gets new features before stable.\n"
                       "Telemetry uploads netplay.log + console.log every 30s while logged in.\n"
                       "Region / Force-relay / account / chat / FPS / HUD live in\n"
                       "the in-game overlay (3SX Account modal + F3 settings + Tab chat).",
                  foreground="#8b949e",
                  font=("Segoe UI", 9, "italic")).grid(
            row=adv_row + 8, column=0, columnspan=3, sticky="w", **pad)

    def _on_region_change(self):
        sel = self.region_var.get().split(" — ")[0]
        for rid, _, host, port in REGIONS:
            if rid == sel:
                self.server_var.set(host)
                self.port_var.set(str(port))
                self.cfg["server"] = host
                self.cfg["port"] = port
                save_launcher_config(self.cfg)
                self.status_var.set(f"Region: {rid} (saved)")
                break

    def _save_force_relay(self):
        self.cfg["force_relay"] = bool(self.force_relay_var.get())
        save_launcher_config(self.cfg)
        state = "on" if self.cfg["force_relay"] else "off"
        self.status_var.set(f"Force relay: {state} (saved)")

    def _save_gpu_choice(self):
        label = self.gpu_var.get()
        icd_path = ""
        for friendly, json_path in self._vulkan_icds:
            if friendly == label:
                icd_path = json_path
                break
        self.cfg["vulkan_icd"] = icd_path
        save_launcher_config(self.cfg)
        self.status_var.set(f"GPU: {label} (saved)")

    def _save_channel(self):
        ch = self.channel_var.get()
        if ch not in RELEASE_CHANNELS:
            ch = DEFAULT_CHANNEL
        self.cfg["channel"] = ch
        save_launcher_config(self.cfg)
        self.status_var.set(f"Channel: {ch} (saved) — restart launcher to apply")

    def _save_telemetry(self):
        v = bool(self.telemetry_var.get())
        self.cfg["telemetry_enabled"] = v
        save_launcher_config(self.cfg)
        state = "on" if v else "off"
        self.status_var.set(f"Telemetry uploads: {state} (saved)")

    def _ping_region(self, host: str, port: int) -> Optional[float]:
        try:
            t = time.time()
            s = socket.create_connection((host, port), timeout=3)
            ms = (time.time() - t) * 1000
            s.close()
            return ms
        except Exception:
            return None

    def _ping_all_regions(self):
        for rid, _, host, port in REGIONS:
            if rid not in self.ping_labels:
                continue
            try:
                self.ping_labels[rid].config(text=f"{rid}: pinging…", foreground="#8b949e")
            except Exception:
                return
            samples = []
            for _ in range(3):
                ms = self._ping_region(host, port)
                if ms is not None:
                    samples.append(ms)
            if not samples:
                try:
                    self.ping_labels[rid].config(text=f"{rid}: unreachable", foreground="#f85149")
                except Exception:
                    return
                continue
            avg = sum(samples) / len(samples)
            color = "#3fb950" if avg < 80 else "#d29922" if avg < 150 else "#f85149"
            try:
                self.ping_labels[rid].config(text=f"{rid}: {avg:.0f} ms", foreground=color)
            except Exception:
                return

    def _save_settings(self):
        # Server / port / exe path / force-relay moved to the in-game overlay.
        # GPU + channel + telemetry auto-save on change, so this button has
        # nothing left to do beyond a confirmation. Kept for backward compat
        # in case some user clicks it.
        save_launcher_config(self.cfg)
        self.status_var.set("Settings saved.")

    # ---------- Play ----------

    def _build_play_tab(self):
        frame = self.tab_play
        for w in frame.winfo_children():
            w.destroy()

        wrap = tk.Frame(frame, bg="#0d1117")
        wrap.pack(expand=True, fill="both", pady=40)

        ttk.Label(wrap, text="3SX Netplay",
                  font=("Segoe UI", 14, "bold")).pack(pady=(0, 4))
        ttk.Label(wrap,
                  text="Once in-game: F3 settings, Tab chat,\n"
                       "Network menu for casual/ranked/room.",
                  foreground="#8b949e",
                  font=("Segoe UI", 9, "italic"),
                  justify="center").pack(pady=(0, 24))
        ttk.Button(wrap, text="LAUNCH GAME", command=self._launch).pack(padx=20, ipadx=20, ipady=8)

    def _on_close(self):
        self.root.destroy()

    # ---------- Helpers ----------

    @staticmethod
    def _parse_version(s: str) -> tuple:
        try:
            return tuple(int(x) for x in s.split("."))
        except Exception:
            return (0, 0, 0)

    def _install_root(self):
        """Resolve install root. Frozen: …/3SX/versions/<ver>/launcher.exe → up 2 = …/3SX/.
        Dev mode: launcher.py dir."""
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent.parent.parent
        return Path(__file__).resolve().parent

    def _clear_boot_pending(self):
        try:
            (self._install_root() / "boot_pending").unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def _installed_version(self) -> str:
        """Version of what's actually on disk — NOT this launcher's build
        constant. Comparing against APP_VERSION made a stale launcher exe
        prompt for the same update forever, no matter what was installed.
        Probe order: VERSION file next to the exe (shipped in dist zips) →
        current.txt at the install root (launcher-managed installs) →
        APP_VERSION as last resort."""
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).resolve().parent
        else:
            base = Path(__file__).resolve().parent
        for probe in (base / "VERSION", self._install_root() / "current.txt"):
            try:
                v = probe.read_text(encoding="utf-8").strip()
            except Exception:
                continue
            if v:
                return v
        return APP_VERSION

    def _check_update(self):
        """Worker thread: check for a newer release, prompt on main thread."""
        local = self._installed_version()
        if UPDATE_USE_MANIFEST:
            # Legacy self-hosted channel (FISTBUMP_UPDATE_BASE_URL set).
            channel = self.cfg.get("channel", DEFAULT_CHANNEL)
            if channel not in RELEASE_CHANNELS:
                channel = DEFAULT_CHANNEL
            try:
                req = urllib.request.Request(update_manifest_url(channel))
                with urllib.request.urlopen(req, timeout=5) as r:
                    manifest = json.loads(r.read().decode("utf-8"))
            except Exception:
                return  # silent — never block login on a flaky update server
            srv_version = str(manifest.get("version", "")).strip()
            if not srv_version:
                return
            if self._parse_version(srv_version) <= self._parse_version(local):
                return
            self.root.after(0, lambda: self._prompt_update(manifest))
            return

        # Default: GitHub Releases. Players re-download from github.com.
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                headers={"User-Agent": "3sx-launcher",
                         "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                rel = json.loads(r.read().decode("utf-8"))
        except Exception:
            return  # silent — never block login on a flaky network
        tag = str(rel.get("tag_name", "")).strip()
        version = tag
        for prefix in ("stable-", "v"):
            if version.startswith(prefix):
                version = version[len(prefix):]
        if not version:
            return
        if self._parse_version(version) <= self._parse_version(local):
            return
        url = rel.get("html_url") or f"https://github.com/{UPDATE_REPO}/releases/latest"
        vlog(f"update available: local={local} remote={version} url={url}")
        self.root.after(0, lambda: self._prompt_github_update(version, url))

    def _prompt_github_update(self, version: str, url: str):
        if not messagebox.askyesno(
            "Update available",
            f"3SX {version} is available.\n\n"
            "Open the GitHub download page? Grab the new zip and extract it "
            "over this folder."
        ):
            return
        import webbrowser
        webbrowser.open(url)

    def _log_upload_loop(self):
        while True:
            time.sleep(30)
            # Opt-out telemetry toggle. Default = True (logs help debug netplay
            # issues; nothing sensitive — usernames, ping, frame events).
            if not self.cfg.get("telemetry_enabled", True):
                continue
            user = (self.cfg.get("username") or "").strip()
            if not user:
                continue
            if not (CONFIG_DIR / "token").exists():
                continue
            for name, suffix in (("netplay.log", ""), ("console.log", "_console")):
                try:
                    log_path = CONFIG_DIR / name
                    if not log_path.exists():
                        continue
                    data = log_path.read_bytes()
                    if len(data) > 1024 * 1024:
                        data = data[-(1024 * 1024):]
                    url = f"{UPDATE_BASE_URL}/api/logs/{user}{suffix}"
                    req = urllib.request.Request(url, data=data, method="POST",
                                                 headers={"Content-Type": "text/plain"})
                    urllib.request.urlopen(req, timeout=10).read()
                except Exception:
                    pass

    def _prompt_update(self, manifest):
        version = manifest.get("version", "?")
        if not messagebox.askyesno(
            "Update available",
            f"3SX {version} is available. Install now?\n\n"
            "The launcher will close after installing -- re-open 3SX.exe to launch the new version."
        ):
            return
        threading.Thread(target=self._do_update, args=(manifest,), daemon=True).start()

    def _update_error(self, msg: str):
        self.root.after(0, lambda: messagebox.showerror("Update failed", msg))

    def _do_update(self, manifest):
        version = str(manifest["version"])
        url = str(manifest["payload"]["url"])
        expected_sha = str(manifest["payload"]["sha256"]).lower()

        install_root = self._install_root()
        staging = install_root / "updates"
        staging.mkdir(parents=True, exist_ok=True)
        partial = staging / f"{version}.partial"
        final_zip = staging / f"{version}.zip"

        h = hashlib.sha256()
        try:
            with urllib.request.urlopen(url, timeout=60) as r, open(partial, "wb") as out:
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    h.update(chunk)
        except Exception as e:
            self._update_error(f"Download failed: {e}")
            try:
                partial.unlink()
            except FileNotFoundError:
                pass
            return

        if h.hexdigest() != expected_sha:
            try:
                partial.unlink()
            except FileNotFoundError:
                pass
            self._update_error("Update integrity check failed (SHA mismatch).")
            return

        if final_zip.exists():
            final_zip.unlink()
        partial.rename(final_zip)

        versions_dir = install_root / "versions"
        versions_dir.mkdir(exist_ok=True)
        new_part = versions_dir / f"{version}.partial"
        new_final = versions_dir / version
        if new_part.exists():
            shutil.rmtree(new_part)
        new_part.mkdir()
        try:
            with zipfile.ZipFile(final_zip) as zf:
                zf.extractall(new_part)
        except Exception as e:
            shutil.rmtree(new_part, ignore_errors=True)
            self._update_error(f"Extract failed: {e}")
            return
        if new_final.exists():
            shutil.rmtree(new_final)
        new_part.rename(new_final)

        # Some publish layouts have versions/<ver>/versions/<ver>/... (because the
        # zip itself contains a top-level versions/). Detect + flatten.
        nested = new_final / "versions" / version
        if nested.exists() and (nested / "3sx.exe").exists():
            tmp_move = versions_dir / f"{version}.flatten"
            if tmp_move.exists():
                shutil.rmtree(tmp_move)
            nested.rename(tmp_move)
            shutil.rmtree(new_final)
            tmp_move.rename(new_final)

        cur_file = install_root / "current.txt"
        rb_file = install_root / "rollback.txt"
        prev = cur_file.read_text(encoding="utf-8").strip() if cur_file.exists() else ""
        tmp_cur = install_root / "current.txt.tmp"
        tmp_cur.write_text(version, encoding="utf-8")
        if prev:
            rb_file.write_text(prev, encoding="utf-8")
        if cur_file.exists():
            cur_file.unlink()
        tmp_cur.rename(cur_file)

        self._prune_versions(versions_dir, keep={version, prev})

        self.root.after(0, lambda: messagebox.showinfo(
            "Update installed",
            f"3SX {version} installed. Close this dialog, then re-open 3SX.exe to relaunch."
        ))
        self.root.after(100, self.root.quit)

    def _prune_versions(self, versions_dir, keep):
        keep = {v for v in keep if v}
        for entry in versions_dir.iterdir():
            if entry.is_dir() and entry.name not in keep and not entry.name.endswith(".partial"):
                try:
                    shutil.rmtree(entry)
                except Exception:
                    pass

    def _load_token(self) -> Optional[str]:
        try:
            with open(CONFIG_DIR / "token", "r", encoding="utf-8") as f:
                return f.readline().strip()
        except Exception:
            return None

    def _launch(self):
        exe = _resolve_exe(self.cfg)
        if not Path(exe).exists():
            messagebox.showerror("Not found", f"3sx.exe not found at:\n{exe}\n\nSet path in Settings tab.")
            return

        host = self.cfg.get("server", DEFAULT_SERVER)
        port = int(self.cfg.get("port", DEFAULT_PORT))
        args = [
            exe,
            "--matchmaking-ip", host,
            "--matchmaking-port", str(port),
        ]
        if self.cfg.get("force_relay"):
            args.append("--force-relay")
        save_launcher_config(self.cfg)

        env = os.environ.copy()
        icd_path = self.cfg.get("vulkan_icd", "").strip()
        if icd_path:
            # Constrain the Vulkan loader to a single ICD so SDL_GPU picks the
            # GPU we want. Multi-path syntax uses semicolon on Windows.
            env["VK_ICD_FILENAMES"] = icd_path

        # Disable broken implicit Vulkan layers that ship with old AMD/Steam
        # software. These can break vkEnumeratePhysicalDevices on PCs that
        # don't even have AMD GPUs (observed on Intel iGPU + NVIDIA dGPU).
        existing_disable = env.get("VK_LOADER_LAYERS_DISABLE", "")
        bad_layers = "VK_LAYER_AMD_switchable_graphics"
        if existing_disable:
            env["VK_LOADER_LAYERS_DISABLE"] = existing_disable + "," + bad_layers
        else:
            env["VK_LOADER_LAYERS_DISABLE"] = bad_layers
        # Belt-and-suspenders: vendor-style disable env var as well.
        env["DISABLE_VK_LAYER_AMD_switchable_graphics_1"] = "1"

        vlog(f"launching game: exe={exe!r}")
        vlog(f"  args: {args}")
        vlog(f"  VK_ICD_FILENAMES={env.get('VK_ICD_FILENAMES', '(unset)')}")
        try:
            self.game_proc = subprocess.Popen(args, env=env)
            self.status_var.set(f"Game running (PID {self.game_proc.pid}).")
            vlog(f"  pid={self.game_proc.pid}")
        except Exception as e:
            messagebox.showerror("Launch failed", str(e))


def main():
    root = tk.Tk()
    app = LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
