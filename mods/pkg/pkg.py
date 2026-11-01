#!/usr/bin/env python3
"""herdr-pkg: a package manager for Herdr app mods and layouts.

  pkg.py list                 what's installed: mods and layouts, where each came from, status
  pkg.py adopt [NAME...]      start tracking mods/layouts installed before herdr-pkg
        [--import FILE]       ...using a sources.json ({mods|layouts: {name: {repo, path, commit}}})
        [--from OWNER/REPO]   ...from this repo (with one NAME)
  pkg.py outdated             which tracked mods/layouts have upstream updates

Add --json for machine-readable output (the dialog uses it). Standard library only (Python
3.8+); talks to GitHub through `gh api` when it's logged in, else the REST API (GITHUB_TOKEN
helps past 60 requests an hour). State: <herdr-app home>/data/pkg/packages.json.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERDR_APP = os.environ.get("HERDR_APP_HOME") or os.path.expanduser("~/.config/herdr-app")
# Files Herdr or an installer writes into a mod folder; never part of what's compared.
GENERATED = {"jsconfig.json", "runtime.json"}
SKIP_DIRS = {"__pycache__", "node_modules", ".git"}
HISTORY = 40          # commits walked back per path when looking for the one installed
DAY = 24 * 3600


def ignored(rel):
    parts = rel.split("/")
    return parts[-1] in GENERATED or parts[-1].endswith(".d.ts") or any(p in SKIP_DIRS for p in parts[:-1])


def blob_sha(data):
    """The git blob hash of some bytes (what `git hash-object` prints)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# ---------------------------------------------------------------- the herdr-app home

class Home:
    def __init__(self, root=HERDR_APP):
        self.root = root
        self.data = os.path.join(root, "data", "pkg")
        try:
            with open(os.path.join(root, "installed.json")) as f:
                self.shipped = set(json.load(f))
        except (OSError, ValueError):
            self.shipped = set()

    def path(self, kind, name):
        return os.path.join(self.root, "mods", name) if kind == "mod" else os.path.join(self.root, "layouts", name + ".json")

    def files(self, kind, name):
        """{relative path: blob sha} of an installed mod folder or layout file."""
        p = self.path(kind, name)
        if kind == "layout":
            with open(p, "rb") as f:
                return {name + ".json": blob_sha(f.read())}
        out = {}
        for dirpath, dirs, names in os.walk(p):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for n in sorted(names):
                full = os.path.join(dirpath, n)
                rel = os.path.relpath(full, p).replace(os.sep, "/")
                if not ignored(rel) and os.path.isfile(full):
                    with open(full, "rb") as f:
                        out[rel] = blob_sha(f.read())
        return out

    def cloned_from(self, kind, name):
        p = os.path.join(self.path(kind, name), "mod.json") if kind == "mod" else self.path(kind, name)
        try:
            with open(p) as f:
                return json.load(f).get("cloned_from")
        except (OSError, ValueError, AttributeError):
            return None

    def inventory(self):
        """Every mod and layout that isn't Herdr's own: [(kind, name)]."""
        out = []
        mods = os.path.join(self.root, "mods")
        for n in sorted(os.listdir(mods)) if os.path.isdir(mods) else []:
            if os.path.isdir(os.path.join(mods, n)) and n not in self.shipped and not n.startswith("."):
                out.append(("mod", n))
        lays = os.path.join(self.root, "layouts")
        for n in sorted(os.listdir(lays)) if os.path.isdir(lays) else []:
            if n.endswith(".json") and f"layout:{n[:-5]}" not in self.shipped:
                out.append(("layout", n[:-5]))
        return out

    # packages.json: {"packages": {"mod:<name>" | "layout:<name>": record}}
    def load(self):
        try:
            with open(os.path.join(self.data, "packages.json")) as f:
                return json.load(f).get("packages", {})
        except (OSError, ValueError):
            return {}

    def save(self, packages):
        os.makedirs(self.data, exist_ok=True)
        tmp = os.path.join(self.data, "packages.json.tmp")
        with open(tmp, "w") as f:
            json.dump({"packages": packages}, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, os.path.join(self.data, "packages.json"))

    def log(self, event, **fields):
        os.makedirs(self.data, exist_ok=True)
        with open(os.path.join(self.data, "log.jsonl"), "a") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}) + "\n")


# ---------------------------------------------------------------- GitHub

class GitHub:
    """The few GitHub reads herdr-pkg needs. Trees at a commit are cached for good (they never
    change), the topic index for a day."""

    def __init__(self, cache_dir):
        self.cache = cache_dir
        self.calls = 0
        self.use_gh = None

    def _get(self, path):
        self.calls += 1
        if self.use_gh is None:
            self.use_gh = subprocess.run(["gh", "auth", "status"], capture_output=True).returncode == 0 if _which("gh") else False
        if self.use_gh:
            r = subprocess.run(["gh", "api", path], capture_output=True, text=True)
            if r.returncode:
                raise LookupError(f"GitHub {path}: {(r.stderr or r.stdout).strip()[:200]}")
            return json.loads(r.stdout)
        req = urllib.request.Request("https://api.github.com/" + path, headers={"Accept": "application/vnd.github+json"})
        if os.environ.get("GITHUB_TOKEN"):
            req.add_header("Authorization", "Bearer " + os.environ["GITHUB_TOKEN"])
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 - any network or HTTP error is the same to the caller
            raise LookupError(f"GitHub {path}: {e}") from e

    def _cached(self, name, max_age, fetch):
        p = os.path.join(self.cache, name)
        if os.path.exists(p) and (max_age is None or time.time() - os.path.getmtime(p) < max_age):
            with open(p) as f:
                return json.load(f)
        value = fetch()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(value, f)
        return value

    def head(self, repo):
        """The default branch's newest commit sha."""
        info = self._get(f"repos/{repo}")
        return self._get(f"repos/{repo}/commits/{info['default_branch']}")["sha"]

    def tree(self, repo, commit):
        """{path: blob sha} of every file in the repo at that commit."""
        def fetch():
            t = self._get(f"repos/{repo}/git/trees/{commit}?recursive=1")
            return {e["path"]: e["sha"] for e in t["tree"] if e["type"] == "blob"}
        return self._cached(f"trees/{commit}.json", None, fetch)

    def history(self, repo, path, limit=HISTORY):
        """Commits on the default branch that changed `path`, newest first: [(sha, date, subject)]."""
        q = urllib.parse.quote(path)
        return [(c["sha"], c["commit"]["committer"]["date"][:10], c["commit"]["message"].split("\n")[0])
                for c in self._get(f"repos/{repo}/commits?path={q}&per_page={limit}")]

    def topic(self, topic):
        """Repos with this topic (herdr.dev's index): [{"repo", "stars", "branch"}]."""
        def fetch():
            out = []
            for page in range(1, 4):
                r = self._get(f"search/repositories?q=topic:{topic}&per_page=100&page={page}")
                out += [{"repo": i["full_name"], "stars": i["stargazers_count"], "branch": i["default_branch"]}
                        for i in r["items"]]
                if len(r["items"]) < 100:
                    break
            return out
        return self._cached(f"topic-{topic}.json", DAY, fetch)

    def branch_tree(self, repo, branch):
        """{path: blob sha} at the tip of a branch (changes, so only cached through index())."""
        t = self._get(f"repos/{repo}/git/trees/{urllib.parse.quote(branch)}?recursive=1")
        return {e["path"]: e["sha"] for e in t["tree"] if e["type"] == "blob"}

    def index(self):
        """Every mod and layout herdr.dev knows: {repo: {"stars", "mod": {name: [paths]}, "layout": {...}}}.
        One tree per repo, eight at a time, cached for a day."""
        def fetch():
            from concurrent.futures import ThreadPoolExecutor
            repos = {r["repo"]: r for t in ("herdr-mod", "herdr-layout") for r in self.topic(t)}

            def one(r):
                try:
                    tree = self.branch_tree(r["repo"], r.get("branch") or "HEAD")
                except LookupError:
                    return r["repo"], None
                entry = {"stars": r["stars"], "mod": {}, "layout": {}}
                for path in tree:
                    m = re.match(r"((?:.+/)?mods/([^/]+))/", path)
                    if m:
                        entry["mod"].setdefault(m.group(2), [])
                        if m.group(1) not in entry["mod"][m.group(2)]:
                            entry["mod"][m.group(2)].append(m.group(1))
                    m = re.fullmatch(r"(?:.+/)?layouts/([^/]+)\.json", path)
                    if m:
                        entry["layout"].setdefault(m.group(1), []).append(path)
                return r["repo"], entry

            with ThreadPoolExecutor(max_workers=8) as pool:
                return {repo: e for repo, e in pool.map(one, repos.values()) if e}
        return self._cached("index.json", DAY, fetch)


def _which(cmd):
    return any(os.access(os.path.join(d, cmd), os.X_OK) for d in os.environ.get("PATH", "").split(os.pathsep))


# ---------------------------------------------------------------- matching

def package_paths(tree, kind, name):
    """Where a repo keeps this mod or layout: ["<prefix>/mods/<name>", ...] or ["<prefix>/layouts/<name>.json"]."""
    if kind == "layout":
        return sorted({p for p in tree if re.fullmatch(rf"(?:.+/)?layouts/{re.escape(name)}\.json", p)})
    return sorted({m.group(1) for p in tree for m in [re.match(rf"((?:.+/)?mods/{re.escape(name)})/", p)] if m})


def files_at(tree, kind, path):
    """{relative path: sha} of the package at `path` in a tree, same shape as Home.files."""
    if kind == "layout":
        return {path.split("/")[-1]: tree[path]} if path in tree else {}
    base = path + "/"
    return {p[len(base):]: s for p, s in tree.items() if p.startswith(base) and not ignored(p[len(base):])}


def compare(local, upstream):
    """(score 0..1, files that differ). 1.0 means identical."""
    keys = set(local) | set(upstream)
    differ = sorted(k for k in keys if local.get(k) != upstream.get(k))
    return (1 - len(differ) / len(keys) if keys else 0.0), differ


def locate(gh, repo, kind, name, local, path=None):
    """Find the commit of `repo` this local copy came from.

    Walks the commits that changed the package's path, newest first, and returns the first one
    whose files equal the local ones; failing that, the closest one, marked modified. None when
    the repo has no such package."""
    if path is None:
        paths = package_paths(gh.tree(repo, gh.head(repo)), kind, name)
        if not paths:
            return None
        path = paths[0]
    best = None
    for sha, date, subject in gh.history(repo, path):
        upstream = files_at(gh.tree(repo, sha), kind, path)
        if not upstream:
            continue
        score, differ = compare(local, upstream)
        cand = {"source": repo, "path": path, "commit": sha, "date": date, "subject": subject,
                "score": round(score, 3), "modified": differ}
        if not differ:
            return cand
        if best is None or score > best["score"]:
            best = cand
    return best


def similarity(repo, name):
    """How likely a repo is to hold `name`, from its name alone (to search the likeliest first)."""
    r = repo.split("/")[-1].lower().replace("herdr-", "").replace("herdr", "")
    n = name.lower()
    return 3 if r == n else 2 if n in r or r in n else 0


# ---------------------------------------------------------------- commands

def record_for(home, kind, name, found):
    return {"kind": kind, "name": name, "source": found["source"], "path": found["path"],
            "commit": found["commit"], "modified": found["modified"], "files": home.files(kind, name),
            "adopted": True, "at": time.strftime("%Y-%m-%d")}


def classify(home, packages, kind, name):
    """What we know about one installed mod or layout, for `list`."""
    key = f"{kind}:{name}"
    rec = packages.get(key)
    item = {"kind": kind, "name": name}
    if rec:
        item.update(source=rec["source"], path=rec["path"], commit=rec["commit"], status="tracked")
        if rec.get("modified"):
            item.update(status="modified", modified=rec["modified"])
        now = home.files(kind, name)
        edited = sorted(k for k in set(now) | set(rec.get("files", {})) if now.get(k) != rec.get("files", {}).get(k))
        if edited:
            item["edited"] = edited
        return item
    origin = home.cloned_from(kind, name)
    if origin:
        src = origin.get("name")
        own = (src in home.shipped) if kind == "mod" else (f"layout:{src}" in home.shipped)
        item.update(status="clone" if own else "personal", cloned_from=origin)
    else:
        item["status"] = "untracked"
    return item


def cmd_list(home, args):
    packages = home.load()
    items = [classify(home, packages, k, n) for k, n in home.inventory()]
    gone = [dict(kind=r["kind"], name=r["name"], status="missing") for key, r in packages.items()
            if (r["kind"], r["name"]) not in set(home.inventory())]
    return {"items": items + gone}


def cmd_adopt(home, args, gh):
    packages = home.load()
    inv = home.inventory()
    wanted = set(args.names or [])
    todo = [(k, n) for k, n in inv if (not wanted or n in wanted) and f"{k}:{n}" not in packages]
    imported = {}
    if args.import_file:
        with open(args.import_file) as f:
            src = json.load(f)
        for plural, kind in (("mods", "mod"), ("layouts", "layout")):
            for n, e in (src.get(plural) or {}).items():
                imported[(kind, n)] = e
    adopted, skipped, left = [], [], []
    index = None
    for kind, name in todo:
        local = home.files(kind, name)
        found = None
        try:
            if args.source:
                parts = args.source.strip("/").split("/")
                repo, sub = "/".join(parts[:2]), "/".join(parts[2:])
                hint = None
                if sub:
                    hint = f"{sub}/mods/{name}" if kind == "mod" else f"{sub}/layouts/{name}.json"
                found = locate(gh, repo, kind, name, local, hint)
            elif (kind, name) in imported:
                e = imported[(kind, name)]
                found = locate(gh, e["repo"], kind, name, local, e["path"])
            else:
                if index is None:
                    index = gh.index()
                cands = sorted(((repo, path, e["stars"]) for repo, e in index.items() for path in e[kind].get(name, [])),
                               key=lambda c: (-similarity(c[0], name), -c[2]))
                for repo, path, _ in cands:
                    try:
                        cand = locate(gh, repo, kind, name, local, path)
                    except LookupError:
                        continue
                    if cand and (found is None or cand["score"] > found["score"]):
                        found = cand
                    if found and not found["modified"]:
                        break
        except LookupError as e:
            left.append({"kind": kind, "name": name, "reason": str(e)})
            continue
        if home.cloned_from(kind, name) and not (found and found["score"] >= 0.5):
            skipped.append({"kind": kind, "name": name, "reason": "a clone of " + home.cloned_from(kind, name).get("name", "?") + ": yours to maintain"})
            continue
        if found and found["score"] > 0:
            packages[f"{kind}:{name}"] = record_for(home, kind, name, found)
            adopted.append({"kind": kind, "name": name, **{k: found[k] for k in ("source", "path", "commit", "date", "modified")}})
            home.log("adopt", kind=kind, name=name, source=found["source"], commit=found["commit"], modified=found["modified"])
        else:
            left.append({"kind": kind, "name": name, "reason": "no repo found: try --from OWNER/REPO"})
    if adopted and not args.dry_run:
        home.save(packages)
    return {"adopted": adopted, "skipped": skipped, "untracked": left, "github_calls": gh.calls, "dry_run": args.dry_run}


def cmd_outdated(home, args, gh):
    out = []
    for key, rec in sorted(home.load().items()):
        try:
            sha, date, subject = gh.history(rec["source"], rec["path"], limit=1)[0]
        except (LookupError, IndexError) as e:
            out.append({"kind": rec["kind"], "name": rec["name"], "status": "error", "reason": str(e)})
            continue
        item = {"kind": rec["kind"], "name": rec["name"], "source": rec["source"], "installed": rec["commit"],
                "latest": sha, "date": date, "subject": subject, "modified": rec.get("modified", [])}
        item["status"] = "current" if sha == rec["commit"] else "update"
        out.append(item)
    return {"items": out}


# ---------------------------------------------------------------- output

KIND_TITLE = {"mod": "Mods (pieces of the window: panels, dialogs, tabs, status-bar blocks)",
              "layout": "Layouts (whole-window arrangements you switch between)"}


def show(command, result):
    s = lambda c: c[:7] if c else "?"  # noqa: E731
    if command == "list":
        for kind in ("mod", "layout"):
            rows = [i for i in result["items"] if i["kind"] == kind]
            print(KIND_TITLE[kind])
            for i in rows:
                st = i["status"]
                if st in ("tracked", "modified"):
                    extra = f"{i['source']} @ {s(i['commit'])}"
                    if st == "modified":
                        extra += f"  modified: {', '.join(i['modified'])}"
                    if i.get("edited"):
                        extra += f"  edited since: {', '.join(i['edited'])}"
                elif st in ("clone", "personal"):
                    c = i["cloned_from"]
                    extra = f"clone of {'Herdr' if st == 'clone' else 'your'} {c.get('name')} ({c.get('version')}): never updated"
                elif st == "untracked":
                    extra = "not tracked: pkg.py adopt " + i["name"]
                else:
                    extra = "in packages.json but not installed"
                print(f"  {i['name']:<20} {st:<10} {extra}")
            if not rows:
                print("  (none)")
            print()
    elif command == "adopt":
        for a in result["adopted"]:
            m = f"  modified: {', '.join(a['modified'])}" if a["modified"] else ""
            print(f"✅ {a['kind']} {a['name']}: {a['source']} @ {s(a['commit'])} ({a['date']}){m}")
        for a in result["skipped"]:
            print(f"➖ {a['kind']} {a['name']}: {a['reason']}")
        for a in result["untracked"]:
            print(f"❔ {a['kind']} {a['name']}: {a['reason']}")
        print(f"\n{len(result['adopted'])} adopted{' (dry run, not saved)' if result['dry_run'] else ''}; {result['github_calls']} GitHub calls")
    elif command == "outdated":
        n = 0
        for i in result["items"]:
            if i["status"] == "error":
                print(f"❓ {i['kind']} {i['name']}: {i['reason']}")
            elif i["status"] == "current":
                print(f"✅ {i['kind']} {i['name']}: up to date ({s(i['installed'])})")
            else:
                n += 1
                print(f"⬆️  {i['kind']} {i['name']}: {s(i['installed'])} → {s(i['latest'])} ({i['date']}) {i['subject']}")
                print(f"     https://github.com/{i['source']}/compare/{s(i['installed'])}...{s(i['latest'])}")
                if i["modified"]:
                    print(f"     modified here: {', '.join(i['modified'])} (an update merges your changes)")
        print(f"\n{n} update(s)")


def main(argv=None, home=None, gh=None):
    ap = argparse.ArgumentParser(prog="pkg.py", description="Herdr app mods and layouts, like `herdr plugin` for the TUI.")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    a = sub.add_parser("adopt")
    a.add_argument("names", nargs="*")
    a.add_argument("--import", dest="import_file")
    a.add_argument("--from", dest="source")
    a.add_argument("--dry-run", action="store_true")
    sub.add_parser("outdated")
    args = ap.parse_args(argv)
    home = home or Home()
    if args.command == "adopt" and args.source and len(args.names) != 1:
        ap.error("--from needs exactly one NAME")
    gh = gh or GitHub(os.path.join(home.data, "cache"))
    result = {"list": lambda: cmd_list(home, args), "adopt": lambda: cmd_adopt(home, args, gh),
              "outdated": lambda: cmd_outdated(home, args, gh)}[args.command]()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        show(args.command, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
