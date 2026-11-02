#!/usr/bin/env python3
"""Install herdr-pkg into Herdr app.

  python3 scripts/install.py              copy mods/pkg, bind alt-shift-p, then adopt what's installed
  python3 scripts/install.py --update     copy a newer mods/pkg over (or: pkg.py update pkg)
  python3 scripts/install.py --uninstall  remove mods/pkg, its key and its status-bar blocks (data/pkg/ is kept)
  python3 scripts/install.py --bar LAYOUT add the "↑ N updates" block next to LAYOUT's status bar
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
GENERATED = ("*.d.ts", "jsconfig.json")
sys.path.insert(0, str(REPO / "mods" / "pkg"))
from pkg import sync_into  # noqa: E402
BAR = {"id": "pkg-bar", "type": "block", "content": {"mod": "pkg", "props": {"bar": True}}, "size": {"px": 44}}
STATUS = "workspace-status"


def blocks(node, parent=None):
    """Every (block, parent) in a layout tree."""
    yield node, parent
    for c in node.get("children", []):
        yield from blocks(c, node)


def mod_of(node):
    return (node.get("content") or {}).get("mod")


def add_bar(path):
    """Put the bar block after the layout's status bar (a row of blocks, or wrap the one block in
    one). Returns what happened."""
    lay = json.loads(path.read_text(encoding="utf-8"))
    if any(mod_of(n) == "pkg" for n, _ in blocks(lay)):
        return "already has it"
    hit = next(((n, p) for n, p in blocks(lay) if mod_of(n) == STATUS), None)
    if not hit:
        return f"has no {STATUS} block: add {json.dumps(BAR)} where you want it"
    node, parent = hit
    if parent and parent.get("type") == "row":
        parent["children"].insert(parent["children"].index(node) + 1, dict(BAR))
    else:  # one block filling its slot: it becomes a row of the status bar and the bar
        old = dict(node)
        node.clear()
        node.update({"type": "row", "size": old.pop("size", None) or {"weight": 1},
                     "children": [{**old, "size": {"weight": 1}}, dict(BAR)]})
    path.write_text(json.dumps(lay, indent=2) + "\n", encoding="utf-8")
    return "added"


def remove_bar(path):
    lay = json.loads(path.read_text(encoding="utf-8"))
    hits = [(n, p) for n, p in blocks(lay) if mod_of(n) == "pkg" and p]
    for n, p in hits:
        p["children"].remove(n)
    if hits:
        path.write_text(json.dumps(lay, indent=2) + "\n", encoding="utf-8")
    return bool(hits)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--no-adopt", action="store_true")
    ap.add_argument("--bar", metavar="LAYOUT", action="append", default=[])
    a = ap.parse_args()
    home = Path(os.environ.get("HERDR_APP_HOME", Path.home() / ".config/herdr-app"))
    if not (home / "mods").is_dir():
        sys.exit(f"No Herdr app home at {home}. Start Herdr app once, or set HERDR_APP_HOME.")
    dest, keys_path = home / "mods" / "pkg", home / "keys.json"
    keys = json.loads(keys_path.read_text(encoding="utf-8")) if keys_path.exists() else {}
    installed = home / "installed.json"
    shipped = set(json.loads(installed.read_text(encoding="utf-8"))) if installed.exists() else set()
    own = [p for p in sorted((home / "layouts").glob("*.json")) if f"layout:{p.stem}" not in shipped]

    if a.bar:
        if not dest.exists():
            sys.exit("Install herdr-pkg first (without --bar).")
        for name in a.bar:
            p = home / "layouts" / f"{name}.json"
            if f"layout:{name}" in shipped:
                print(f"{name}: Herdr's own layout, rewritten on every start; clone it first "
                      f"(herdr-app do layout.clone '{{\"name\":\"{name}\"}}') and add the bar to the clone")
            elif not p.exists():
                print(f"{name}: no layouts/{name}.json")
            else:
                print(f"{name}: {add_bar(p)}")
        return

    if a.uninstall:
        if dest.exists():
            shutil.rmtree(dest)
            print("removed mods/pkg")
        if keys.get("global", {}).get(KEY) == ACTION:
            del keys["global"][KEY]
            keys_path.write_text(json.dumps(keys, indent=2) + "\n")
            print(f"unbound {KEY}")
        for p in own:
            if remove_bar(p):
                print(f"removed the status-bar block from {p.stem}")
        print("Kept data/pkg/ (what herdr-pkg tracks, its log and backups).")
        return
    if dest.exists() and not a.update:
        sys.exit("mods/pkg already exists. Use --update, or `pkg.py update pkg` once it's tracked.")
    if dest.exists() and (home / "installed.json").exists() and "pkg" in json.loads((home / "installed.json").read_text(encoding="utf-8")):
        sys.exit("Herdr has a mod of its own called pkg; not overwriting it.")
    # Staged outside mods/ (Herdr would load a folder there), checked, then copied in place:
    # replacing mods/pkg itself would leave Herdr's hot reload watching the old folder.
    tmp = home / "data" / "pkg" / "stage" / "pkg"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(REPO / "mods" / "pkg", tmp, ignore=shutil.ignore_patterns("__pycache__", *GENERATED))
    for g in GENERATED:  # Herdr's own, written for this version: keep them
        for f in dest.glob(g) if dest.exists() else []:
            shutil.copy2(f, tmp / f.name)
    if shutil.which("herdr-app"):
        r = subprocess.run(["herdr-app", "check", str(tmp)], capture_output=True, text=True)
        if r.returncode:
            shutil.rmtree(tmp)
            sys.exit(f"herdr-app check failed:\n{r.stdout}{r.stderr}")
    sync_into(str(tmp), str(dest))
    shutil.rmtree(tmp)
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
    with_status = [p.stem for p in own if STATUS in p.read_text(encoding="utf-8")]
    if with_status:
        print(f"Optional: a status-bar block showing waiting updates (checked once a day) in "
              f"{', '.join(with_status)}: python3 {Path(__file__).resolve()} --bar {with_status[0]}")
    bound = keys.get("global", {}).get(KEY) == ACTION
    print(f"Open it with {KEY + ', or' if bound else ''} herdr-app do mod.open '{{\"name\":\"pkg\"}}', or: python3 {dest / 'pkg.py'} list")


if __name__ == "__main__":
    main()
