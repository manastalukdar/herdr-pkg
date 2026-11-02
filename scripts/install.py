#!/usr/bin/env python3
"""Install herdr-pkg into Herdr app.

  python3 scripts/install.py              copy mods/pkg, bind alt-shift-p, then adopt what's installed
  python3 scripts/install.py --update     copy a newer mods/pkg over (or: pkg.py update pkg)
  python3 scripts/install.py --uninstall  remove mods/pkg and its key (data/pkg/ is kept)
  add --no-adopt to skip matching your existing mods and layouts to their repos
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KEY, ACTION = "alt-shift-p", "mod.open:pkg"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--no-adopt", action="store_true")
    a = ap.parse_args()
    home = Path(os.environ.get("HERDR_APP_HOME", Path.home() / ".config/herdr-app"))
    if not (home / "mods").is_dir():
        sys.exit(f"No Herdr app home at {home}. Start Herdr app once, or set HERDR_APP_HOME.")
    dest, keys_path = home / "mods" / "pkg", home / "keys.json"
    keys = json.loads(keys_path.read_text(encoding="utf-8")) if keys_path.exists() else {}

    if a.uninstall:
        if dest.exists():
            shutil.rmtree(dest)
            print("removed mods/pkg")
        if keys.get("global", {}).get(KEY) == ACTION:
            del keys["global"][KEY]
            keys_path.write_text(json.dumps(keys, indent=2) + "\n")
            print(f"unbound {KEY}")
        print("Kept data/pkg/ (what herdr-pkg tracks, its log and backups).")
        return
    if dest.exists() and not a.update:
        sys.exit("mods/pkg already exists. Use --update, or `pkg.py update pkg` once it's tracked.")
    if dest.exists() and (home / "installed.json").exists() and "pkg" in json.loads((home / "installed.json").read_text(encoding="utf-8")):
        sys.exit("Herdr has a mod of its own called pkg; not overwriting it.")
    tmp = home / "mods" / "pkg.install-tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(REPO / "mods" / "pkg", tmp, ignore=shutil.ignore_patterns("__pycache__", "*.d.ts", "jsconfig.json"))
    if shutil.which("herdr-app"):
        r = subprocess.run(["herdr-app", "check", str(tmp)], capture_output=True, text=True)
        if r.returncode:
            shutil.rmtree(tmp)
            sys.exit(f"herdr-app check failed:\n{r.stdout}{r.stderr}")
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)
    print(f"{'updated' if a.update else 'copied'} mods/pkg")
    if a.update:
        return
    have = keys.get("global", {}).get(KEY)
    if have and have != ACTION:
        print(f"{KEY} is already {have}: not bound. Bind another key to {ACTION} in keys.json.")
    elif not have:
        keys.setdefault("global", {})[KEY] = ACTION
        keys_path.write_text(json.dumps(keys, indent=2) + "\n")
        print(f"bound {KEY} to {ACTION}")
    if not a.no_adopt:
        print("Matching your installed mods and layouts to their repos (one-time, a few seconds):")
        subprocess.run([sys.executable, str(dest / "pkg.py"), "adopt"], env={**os.environ, "HERDR_APP_HOME": str(home)})
    bound = keys.get("global", {}).get(KEY) == ACTION
    print(f"Open it with {KEY + ', or' if bound else ''} herdr-app do mod.open '{{\"name\":\"pkg\"}}', or: python3 {dest / 'pkg.py'} list")


if __name__ == "__main__":
    main()
