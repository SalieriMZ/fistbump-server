#!/usr/bin/env python3
"""3SX bootstrap launcher: reads current.txt, executes versions/<ver>/launcher.exe."""
import os
import subprocess
import sys
import time
from pathlib import Path


def install_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def main() -> int:
    root = install_root()
    cur_file = root / "current.txt"
    if not cur_file.exists():
        print("3SX: missing current.txt -- reinstall required.")
        return 2
    version = cur_file.read_text(encoding="utf-8").strip()
    target = root / "versions" / version / "3sx_launcher_online.exe"

    if not target.exists():
        rb = root / "rollback.txt"
        if rb.exists():
            rb_ver = rb.read_text(encoding="utf-8").strip()
            rb_target = root / "versions" / rb_ver / "3sx_launcher_online.exe"
            if rb_target.exists():
                print(f"3SX: current {version} missing, rolling back to {rb_ver}")
                cur_file.write_text(rb_ver, encoding="utf-8")
                target = rb_target
                version = rb_ver
            else:
                print(f"3SX: current {version} missing and no valid rollback.")
                return 2
        else:
            print(f"3SX: current {version} missing and no rollback.")
            return 2

    boot_pending = root / "boot_pending"
    boot_pending.write_text(version, encoding="utf-8")
    started_at = time.monotonic()

    try:
        proc = subprocess.Popen([str(target)] + sys.argv[1:])
        rc = proc.wait()
    except KeyboardInterrupt:
        try:
            proc.terminate()
        except Exception:
            pass
        rc = 130
    except Exception as e:
        print(f"3SX: failed to spawn launcher: {e}")
        try:
            boot_pending.unlink()
        except FileNotFoundError:
            pass
        return 2

    elapsed = time.monotonic() - started_at

    if elapsed < 5.0 and boot_pending.exists() and rc != 0:
        rb = root / "rollback.txt"
        if rb.exists():
            prev = rb.read_text(encoding="utf-8").strip()
            if prev and prev != version:
                print(f"3SX: launcher failed within {elapsed:.1f}s (rc={rc}); rolling back to {prev}")
                cur_file.write_text(prev, encoding="utf-8")

    try:
        boot_pending.unlink()
    except FileNotFoundError:
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
