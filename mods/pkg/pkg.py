#!/usr/bin/env python3
"""herdr-pkg: a package manager for Herdr app mods and layouts.

  pkg.py list                 what's installed: mods and layouts, where each came from, status
  pkg.py adopt [NAME...]      start tracking mods/layouts installed before herdr-pkg
        [--import FILE]       ...using a sources.json ({mods|layouts: {name: {repo, path, commit}}})
        [--from OWNER/REPO]   ...from this repo (with one NAME)
  pkg.py outdated             which tracked mods/layouts have upstream updates
  pkg.py update [NAME...]     update them; your local edits are merged in (three-way)
  pkg.py install OWNER/REPO[/SUBDIR] [--ref REF] [--yes]
                              review what a mod/layout package does; with --yes, install it
        [--as NAME]           ...its one mod under another name (when yours has its name)
  pkg.py remove NAME [--kind mod|layout] [--force]
  pkg.py browse [QUERY]       herdr.dev's mods and layouts, one row per package

Add --json for machine-readable output (the dialog uses it). Standard library only (Python
3.8+); talks to GitHub through `gh api` when it's logged in, else the REST API (GITHUB_TOKEN
helps past 60 requests an hour). State: <herdr-app home>/data/pkg/packages.json.
"""
import argparse
import base64
import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
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
            with open(os.path.join(root, "installed.json"), encoding="utf-8") as f:
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
            with open(p, encoding="utf-8") as f:
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
            with open(os.path.join(self.data, "packages.json"), encoding="utf-8") as f:
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

    def check(self, path):
        """`herdr-app check` on a mod folder: (ok, output). Layouts have no checker: always ok."""
        if not os.path.isdir(path) or not _which("herdr-app"):
            return True, ""
        r = subprocess.run(["herdr-app", "check", path], capture_output=True, encoding="utf-8", errors="replace")
        return r.returncode == 0, (r.stdout + r.stderr).strip()

    def keys(self):
        try:
            with open(os.path.join(self.root, "keys.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_keys(self, keys):
        with open(os.path.join(self.root, "keys.json"), "w") as f:
            json.dump(keys, f, indent=2)
            f.write("\n")

    def users(self, mod):
        """Your layouts whose blocks use this mod."""
        out = []
        for kind, name in self.inventory():
            if kind == "layout":
                try:
                    with open(self.path("layout", name), encoding="utf-8") as f:
                        if re.search(r'"mod"\s*:\s*"%s"' % re.escape(mod), f.read()):
                            out.append(name)
                except OSError:
                    pass
        return out

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
            r = subprocess.run(["gh", "api", path], capture_output=True, encoding="utf-8")
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
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        value = fetch()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(value, f)
        return value

    def resolve(self, repo, ref=None):
        """A branch, tag or sha (default: the default branch) as a commit sha."""
        if ref and re.fullmatch(r"[0-9a-f]{40}", ref):
            return ref
        return self._get(f"repos/{repo}/commits/{urllib.parse.quote(ref or 'HEAD')}")["sha"]

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

    def blob(self, repo, sha):
        """A file's bytes by its blob sha (cached for good)."""
        p = os.path.join(self.cache, "blobs", sha)
        if os.path.exists(p):
            with open(p, "rb") as f:
                return f.read()
        data = base64.b64decode(self._get(f"repos/{repo}/git/blobs/{sha}")["content"])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return data

    def history(self, repo, path, limit=HISTORY, ref=None):
        """Commits that changed `path` on the default branch (or up to `ref`), newest first: [(sha, date, subject)]."""
        q = urllib.parse.quote(path) + (f"&sha={urllib.parse.quote(ref)}" if ref else "")
        return [(c["sha"], c["commit"]["committer"]["date"][:10], c["commit"]["message"].split("\n")[0])
                for c in self._get(f"repos/{repo}/commits?path={q}&per_page={limit}")]

    def is_branch(self, repo, ref):
        try:
            self._get(f"repos/{repo}/branches/{urllib.parse.quote(ref, safe='')}")
            return True
        except LookupError:  # ponytail: a network error also reads as "not a branch", i.e. pinned: the safe side
            return False

    def topic(self, topic):
        """Repos with this topic (herdr.dev's index): [{"repo", "stars", "branch"}]."""
        def fetch():
            out = []
            for page in range(1, 4):
                r = self._get(f"search/repositories?q=topic:{topic}&per_page=100&page={page}")
                out += [{"repo": i["full_name"], "stars": i["stargazers_count"], "branch": i["default_branch"],
                         "description": i.get("description") or "", "pushed": (i.get("pushed_at") or "")[:10]}
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
                entry = {"stars": r["stars"], "description": r.get("description", ""), "pushed": r.get("pushed", ""),
                         "mod": {}, "layout": {}}
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


def rename(data, old, new):
    """Bytes with mod `old` called `new`, as AGENTS.md says to rename one: its `mods/<name>/` paths,
    the `"mod": "<name>"` in layouts, and `mod.open:<name>` in keys."""
    return re.sub(rb'(mods/|"mod"\s*:\s*"|mod\.open:)' + re.escape(old.encode()) + rb'(?![\w.-])',
                  lambda m: m.group(1) + new.encode(), data)


class Renamed:
    """`gh` as seen by a record installed under another name (`renamed: [old, new]`): the package's
    upstream files come rewritten the same way, with their shas, so they compare and merge with
    the installed copy like any other package's."""

    def __init__(self, gh, rec):
        self.gh, self.path, (self.old, self.new), self.data = gh, rec["path"], rec["renamed"], {}

    def __getattr__(self, attr):
        return getattr(self.gh, attr)

    def tree(self, repo, commit):
        out = dict(self.gh.tree(repo, commit))
        for p, sha in out.items():
            if p == self.path or p.startswith(self.path + "/"):
                data = rename(self.gh.blob(repo, sha), self.old, self.new)
                out[p] = blob_sha(data)
                self.data[out[p]] = data
        return out

    def blob(self, repo, sha):
        return self.data[sha] if sha in self.data else self.gh.blob(repo, sha)


def upstream(gh, rec):
    """Where a record's upstream files come from: the one place its rename is applied."""
    return Renamed(gh, rec) if rec.get("renamed") else gh


def _which(cmd):
    return shutil.which(cmd) is not None  # PATHEXT: finds gh.exe on Windows


def rmtree(path):
    """shutil.rmtree that also removes read-only files (a .git's objects on Windows)."""
    def retry(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if os.path.lexists(path):
        shutil.rmtree(path, onerror=retry)  # ponytail: onerror is deprecated in 3.12 (onexc), kept for 3.8


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


def locate(gh, repo, kind, name, local, path=None, read=None):
    """Find the commit of `repo` this local copy came from.

    Walks the commits that changed the package's path, newest first, and returns the first one
    whose files equal the local ones; failing that, the closest one, marked modified: most files
    equal, then (with `read(rel)` giving the local bytes) the most lines in common in the files
    that differ, so a patched copy gets the commit it was patched on. None when the repo has no
    such package."""
    if path is None:
        paths = package_paths(gh.tree(repo, gh.head(repo)), kind, name)
        if not paths:
            return None
        path = paths[0]
    cands = []
    for sha, date, subject in gh.history(repo, path):
        upstream = files_at(gh.tree(repo, sha), kind, path)
        if not upstream:
            continue
        score, differ = compare(local, upstream)
        cand = {"source": repo, "path": path, "commit": sha, "date": date, "subject": subject,
                "score": round(score, 3), "modified": differ, "_up": upstream}
        if not differ:
            cands = [cand]
            break
        cands.append(cand)
    if not cands:
        return None
    top = max(c["score"] for c in cands)
    tied = [c for c in cands if c["score"] == top]
    if len(tied) > 1 and read:
        def lines_in_common(c):
            total = 0.0
            for rel in c["modified"]:
                if rel in local and rel in c["_up"]:
                    mine = read(rel).decode("utf-8", "replace").splitlines()
                    theirs = gh.blob(repo, c["_up"][rel]).decode("utf-8", "replace").splitlines()
                    total += difflib.SequenceMatcher(None, mine, theirs, autojunk=False).ratio()
            return total
        tied.sort(key=lines_in_common, reverse=True)  # stable: newest wins a tie
    best = dict(tied[0])
    best.pop("_up")
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
        with open(args.import_file, encoding="utf-8") as f:
            src = json.load(f)
        for plural, kind in (("mods", "mod"), ("layouts", "layout")):
            for n, e in (src.get(plural) or {}).items():
                imported[(kind, n)] = e
    adopted, skipped, left = [], [], []
    index = None
    for kind, name in todo:
        local = home.files(kind, name)
        read = lambda rel, k=kind, n=name: read_local(home, k, n, rel)  # noqa: E731
        found = None
        try:
            if args.source:
                parts = args.source.strip("/").split("/")
                repo, sub = "/".join(parts[:2]), "/".join(parts[2:])
                hint = None
                if sub:
                    hint = f"{sub}/mods/{name}" if kind == "mod" else f"{sub}/layouts/{name}.json"
                found = locate(gh, repo, kind, name, local, hint, read)
            elif (kind, name) in imported:
                e = imported[(kind, name)]
                found = locate(gh, e["repo"], kind, name, local, e["path"], read)
            else:
                if index is None:
                    index = gh.index()
                cands = sorted(((repo, path, e["stars"]) for repo, e in index.items() for path in e[kind].get(name, [])),
                               key=lambda c: (-similarity(c[0], name), -c[2]))
                for repo, path, _ in cands:
                    try:
                        cand = locate(gh, repo, kind, name, local, path, read)
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


def ref_fields(gh, repo, ref):
    """What a record keeps for a ref: a branch is followed, a tag or sha pinned."""
    if not ref:
        return {}
    return {"branch": ref} if not re.fullmatch(r"[0-9a-f]{40}", ref) and gh.is_branch(repo, ref) else {"pin": ref}


def newest(gh, rec):
    """The newest commit that changed the package on its branch (default unless recorded), or at
    its pin: (sha, date, subject). Used by both outdated and update."""
    return gh.history(rec["source"], rec["path"], limit=1, ref=rec.get("pin") or rec.get("branch"))[0]


def same_files(gh, rec, latest):
    """True when the package's files at `latest` equal those at the installed commit (the
    installed commit may be a newer repo commit that didn't touch this path)."""
    t = lambda c: files_at(gh.tree(rec["source"], c), rec["kind"], rec["path"])  # noqa: E731
    return latest == rec["commit"] or t(latest) == t(rec["commit"])


def rebase(home, gh, rec):
    """Re-point `rec` at the upstream commit its installed files now equal exactly, when they were
    changed outside pkg (e.g. by the mod's own installer). A local edit matches no commit and keeps
    the recorded base, so it still merges. True when `rec` moved."""
    now = home.files(rec["kind"], rec["name"])
    if now == rec.get("files"):
        return False
    found = locate(gh, rec["source"], rec["kind"], rec["name"], now, path=rec["path"])
    if not found or found["modified"] or found["commit"] == rec["commit"]:
        return False
    rec.update(commit=found["commit"], files=now, modified=[])
    return True


def cmd_outdated(home, args, gh):
    out = []
    for key, rec in sorted(home.load().items()):
        up = upstream(gh, rec)
        try:
            sha, date, subject = newest(gh, rec)
        except (LookupError, IndexError) as e:
            out.append({"kind": rec["kind"], "name": rec["name"], "status": "error", "reason": str(e)})
            continue
        item = {"kind": rec["kind"], "name": rec["name"], "source": rec["source"], "installed": rec["commit"],
                "latest": sha, "date": date, "subject": subject, "modified": rec.get("modified", []),
                **{k: rec[k] for k in ("pin", "branch") if k in rec}}
        rebase(home, up, rec)  # not saved here; the next update saves it
        item["installed"] = rec["commit"]
        c = rec.get("conflict", {})
        item["status"] = ("pinned" if rec.get("pin") else "current") if same_files(up, rec, sha) else "conflict" if c.get("to") == sha else "update"
        if item["status"] == "conflict":
            item.update(conflicts=c["files"], conflicts_dir=c["dir"])
        out.append(item)
    return {"items": out}


def merge3(base, ours, theirs):
    """git merge-file on three byte strings: (merged bytes, conflicted?)."""
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for n, data in (("ours", ours), ("base", base), ("theirs", theirs)):
            paths.append(os.path.join(d, n))
            with open(paths[-1], "wb") as f:
                f.write(data)
        r = subprocess.run(["git", "merge-file", "-p", "-L", "yours", "-L", "installed", "-L", "upstream", *paths],
                           capture_output=True)
        return r.stdout, r.returncode != 0


def read_local(home, kind, name, rel):
    p = home.path(kind, name) if kind == "layout" else os.path.join(home.path(kind, name), rel)
    with open(p, "rb") as f:
        return f.read()


def plan_update(home, gh, rec, latest):
    """What updating `rec` to `latest` writes: ({rel: bytes or None to delete}, [conflicted rel], merged)."""
    kind, name = rec["kind"], rec["name"]
    base = files_at(gh.tree(rec["source"], rec["commit"]), kind, rec["path"])
    theirs = files_at(gh.tree(rec["source"], latest), kind, rec["path"])
    ours = home.files(kind, name)
    writes, conflicts, merged = {}, [], []
    for rel in sorted(set(base) | set(ours) | set(theirs)):
        b, o, t = base.get(rel), ours.get(rel), theirs.get(rel)
        if o == t or t == b:
            continue  # nothing new upstream, or the same change on both sides
        if o == b:
            writes[rel] = gh.blob(rec["source"], t) if t else None  # untouched here: take upstream's
            continue
        if b and o and t:  # changed on both sides: merge
            data, conflict = merge3(gh.blob(rec["source"], b), read_local(home, kind, name, rel), gh.blob(rec["source"], t))
            writes[rel] = data
            (conflicts if conflict else merged).append(rel)
        else:  # added on both sides, or deleted on one and changed on the other
            conflicts.append(rel)
            writes[rel] = gh.blob(rec["source"], t) if t else None
    return writes, conflicts, merged


def apply_files(home, kind, name, writes, fresh=False):
    """Write `writes` into the mod folder / layout file, through a staged copy and a swap; on a failed
    `herdr-app check` the old one is put back. Returns (ok, check output)."""
    dest = home.path(kind, name)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(home.data, "backup", f"{kind}-{name}-{stamp}")
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    if kind == "layout":
        (rel, data), = writes.items()
        if os.path.exists(dest):
            shutil.copy2(dest, backup)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".pkg-tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        return True, ""
    stage = dest + ".pkg-new"
    rmtree(stage)
    if os.path.isdir(dest) and not fresh:
        shutil.copytree(dest, stage, symlinks=True)
    else:
        os.makedirs(stage)
    for rel, data in writes.items():
        p = os.path.join(stage, rel)
        if data is None:
            if os.path.exists(p):
                os.remove(p)
            continue
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    if os.path.isdir(dest):
        os.replace(dest, backup)
    os.replace(stage, dest)
    ok, out = home.check(dest)
    if not ok:
        rmtree(dest)
        if os.path.isdir(backup):
            os.replace(backup, dest)
    return ok, out


def cmd_update(home, args, gh):
    packages = home.load()
    names = set(args.names or [])
    out = []
    for key, rec in sorted(packages.items()):
        if names and rec["name"] not in names:
            continue
        item = {"kind": rec["kind"], "name": rec["name"], "source": rec["source"], "from": rec["commit"]}
        up = upstream(gh, rec)
        try:
            latest, date, subject = newest(gh, rec)
            if rebase(home, up, rec):
                item["from"] = rec["commit"]
            if same_files(up, rec, latest):
                rec.pop("conflict", None)
                out.append({**item, "status": "pinned" if rec.get("pin") else "current"})
                continue
            writes, conflicts, merged = plan_update(home, up, rec, latest)
        except (LookupError, IndexError) as e:
            out.append({**item, "status": "error", "reason": str(e)})
            continue
        item.update(to=latest, date=date, subject=subject, merged=merged, conflicts=conflicts)
        if conflicts:
            # Nothing is written: the merged files (with conflict markers) go aside for a person.
            d = os.path.join(home.data, "conflicts", rec["name"])
            for rel in conflicts:
                p = os.path.join(d, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(writes[rel] or b"")
            rec["conflict"] = {"to": latest, "files": conflicts, "dir": d}  # for `outdated` to show
            out.append({**item, "status": "conflict", "conflicts_dir": d})
            home.log("conflict", kind=rec["kind"], name=rec["name"], to=latest, files=conflicts)
            continue
        if args.dry_run:
            out.append({**item, "status": "would update", "files": sorted(writes)})
            continue
        ok, check = apply_files(home, rec["kind"], rec["name"], writes)
        if not ok:
            out.append({**item, "status": "check failed, rolled back", "reason": check})
            home.log("rollback", kind=rec["kind"], name=rec["name"], to=latest)
            continue
        theirs = files_at(up.tree(rec["source"], latest), rec["kind"], rec["path"])
        rec.pop("conflict", None)
        rec.update(commit=latest, files=home.files(rec["kind"], rec["name"]),
                   modified=compare(home.files(rec["kind"], rec["name"]), theirs)[1], at=time.strftime("%Y-%m-%d"))
        out.append({**item, "status": "updated", "files": sorted(writes)})
        home.log("update", kind=rec["kind"], name=rec["name"], source=rec["source"], to=latest, merged=merged)
    if not args.dry_run:
        home.save(packages)
    return {"items": out}


# What AGENTS.md asks to tell the person before adding a mod. A text match on the source, so it
# can over-report, never prove a mod safe. (After mod-manager's scan, jacksonpires, MIT.)
RISKS = [
    ("runs programs", re.compile(r"\bexec\s*\(|\bhc\s*\(|subprocess|child_process")),
    ("uses the network", re.compile(r"\bfetch\s*\(|WebSocket|urllib|curl ")),
    ("reads files", re.compile(r"readTextFile\s*\(|watchFolder\s*\(|open\(")),
    # rm/rmdir (Unix), Remove-Item (PowerShell), a quoted "del" (cmd.exe; Python's `del x` is unquoted)
    ("writes files", re.compile(r"writeTextFile\s*\(|\brm\b|\brmdir\b|Remove-Item|[\"']del\b|unlink|rmtree")),
]


def find_package(tree, sub):
    """Mods, layouts and keys.json of the package at `sub` ("" = repo root) in a tree."""
    base = sub.strip("/") + "/" if sub.strip("/") else ""
    mods = sorted({m.group(1) for p in tree for m in [re.match(re.escape(base) + r"mods/([^/]+)/", p)] if m})
    layouts = sorted(p[len(base) + 8:-5] for p in tree if re.fullmatch(re.escape(base) + r"layouts/[^/]+\.json", p))
    prefixes = sorted({m.group(1) or "" for p in tree for m in [re.match(r"(?:(.+)/)?mods/[^/]+/main\.js$", p)] if m})
    return {"base": base, "mods": mods, "layouts": layouts, "keys": (base + "keys.json") in tree, "prefixes": prefixes}


def cmd_install(home, args, gh):
    parts = args.source.strip("/").replace("https://github.com/", "").split("/")
    repo, sub = "/".join(parts[:2]), "/".join(p for p in parts[2:] if p not in ("tree", "main", "master"))
    commit = gh.resolve(repo, args.ref)
    tree = gh.tree(repo, commit)
    pkgf = find_package(tree, sub)
    if not pkgf["mods"] and not pkgf["layouts"]:
        where = ", ".join(p or "(root)" for p in pkgf["prefixes"]) or "none"
        return {"ok": False, "reason": f"no mods/ or layouts/ at {sub or 'the repo root'}; packages in this repo: {where}"}
    base, packages, keys = pkgf["base"], home.load(), home.keys()
    renamed = None
    if args.as_name:
        if len(pkgf["mods"]) != 1:
            return {"ok": False, "reason": f"--as renames a package's one mod; this one has {len(pkgf['mods'])}"}
        if not re.fullmatch(r"[\w-][\w.-]*", args.as_name):
            return {"ok": False, "reason": f"--as {args.as_name}: letters, digits, '-', '_' and '.' only"}
        renamed = [pkgf["mods"][0], args.as_name]
    named = lambda m: renamed[1] if renamed else m  # noqa: E731
    blob = lambda sha: rename(gh.blob(repo, sha), *renamed) if renamed else gh.blob(repo, sha)  # noqa: E731
    review = {"source": repo, "subdir": sub, "commit": commit, "mods": [], "layouts": [], "keys": [], "clashes": [],
              "renamed": renamed}
    for m in pkgf["mods"]:
        files = files_at(tree, "mod", f"{base}mods/{m}")
        risks = {}
        for rel, sha in files.items():
            if re.search(r"\.(m?js|py|sh|json)$", rel):
                text = gh.blob(repo, sha).decode("utf-8", "replace")
                for label, rx in RISKS:
                    if rx.search(text):
                        risks.setdefault(label, []).append(rel)
        review["mods"].append({"name": named(m), "files": sorted(files), "risks": risks})
        if os.path.exists(home.path("mod", named(m))) or named(m) in home.shipped:
            review["clashes"].append(f"mod {named(m)} already exists")
    for lay in pkgf["layouts"]:
        review["layouts"].append({"name": lay})
        if os.path.exists(home.path("layout", lay)) or f"layout:{lay}" in home.shipped:
            review["clashes"].append(f"layout {lay} already exists")
    if pkgf["keys"]:
        theirs = json.loads(blob(tree[base + "keys.json"]))
        for section, binds in theirs.items():
            for k, action in binds.items():
                have = keys.get(section, {}).get(k)
                review["keys"].append({"section": section, "key": k, "action": action,
                                       "taken_by": have if have and have != action else None})
    review["has_recipe"] = (base + "RECIPE.md") in tree
    review["has_preview"] = (base + "preview.png") in tree
    if review["clashes"]:
        return {"ok": False, "review": review, "reason": "name clash: remove or rename yours, or install the new mod --as another name (installing never overwrites)"}
    if not args.yes:
        return {"ok": True, "review": review, "installed": False, "next": "run again with --yes to install"}
    done = []
    for m in pkgf["mods"]:
        files = files_at(tree, "mod", f"{base}mods/{m}")
        ok, check = apply_files(home, "mod", named(m), {rel: blob(sha) for rel, sha in files.items()}, fresh=True)
        if not ok:
            shutil.rmtree(home.path("mod", named(m)), ignore_errors=True)
            for k, n in done:
                if k == "mod":
                    shutil.rmtree(home.path("mod", n), ignore_errors=True)
                else:
                    os.remove(home.path("layout", n))
            return {"ok": False, "review": review, "reason": f"herdr-app check failed for {named(m)}: {check}"}
        done.append(("mod", named(m)))
    for lay in pkgf["layouts"]:
        path = f"{base}layouts/{lay}.json"
        apply_files(home, "layout", lay, {lay + ".json": blob(tree[path])})
        done.append(("layout", lay))
    bound = []
    for k in review["keys"]:
        if not k["taken_by"] and keys.get(k["section"], {}).get(k["key"]) != k["action"]:
            keys.setdefault(k["section"], {})[k["key"]] = k["action"]
            bound.append(k)
    if bound:
        home.save_keys(keys)
    for kind, n in done:
        path = f"{base}mods/{pkgf['mods'][0] if renamed else n}" if kind == "mod" else f"{base}layouts/{n}.json"
        packages[f"{kind}:{n}"] = {"kind": kind, "name": n, "source": repo, "path": path, "commit": commit,
                                   "modified": [], "files": home.files(kind, n), "adopted": False,
                                   "keys": [k for k in bound], "at": time.strftime("%Y-%m-%d"), **ref_fields(gh, repo, args.ref)}
        if renamed:
            packages[f"{kind}:{n}"]["renamed"] = renamed
        home.log("install", kind=kind, name=n, source=repo, commit=commit)
    home.save(packages)
    return {"ok": True, "review": review, "installed": True, "items": [{"kind": k, "name": n} for k, n in done],
            "keys_bound": bound, "skipped_keys": [k for k in review["keys"] if k["taken_by"]]}


def cmd_browse(home, args, gh):
    """One row per package (a repo, or a folder of one that holds mods/ or layouts/)."""
    packages, inst = home.load(), {(k, n) for k, n in home.inventory()}
    rows = {}
    for repo, e in gh.index().items():
        for kind in ("mod", "layout"):
            for name, paths in e.get(kind, {}).items():
                for path in paths:
                    sub = path.rsplit("/mods/", 1)[0] if "/mods/" in "/" + path and kind == "mod" else ""
                    if kind == "mod" and path.startswith("mods/"):
                        sub = ""
                    if kind == "layout":
                        sub = path.rsplit("layouts/", 1)[0].rstrip("/")
                    row = rows.setdefault((repo, sub), {"repo": repo, "subdir": sub, "stars": e["stars"],
                                                        "description": e.get("description", ""), "pushed": e.get("pushed", ""),
                                                        "mods": [], "layouts": [], "installed": []})
                    row[kind + "s"].append(name)
                    rec = packages.get(f"{kind}:{name}")
                    if rec and rec["source"] == repo:
                        row["installed"].append(f"{kind}:{name}")
                    elif (kind, name) in inst and not rec:
                        row.setdefault("same_name", []).append(f"{kind}:{name}")
    q = (args.query or "").lower()
    out = [r for r in rows.values() if not q or q in " ".join([r["repo"], r["description"], *r["mods"], *r["layouts"]]).lower()]
    for r in out:
        r["mods"].sort()
        r["layouts"].sort()
    return {"packages": sorted(out, key=lambda r: (-r["stars"], r["repo"], r["subdir"]))}


def tracked(packages, args):
    """The one record `args.name` (and `--kind`) names, or a refusal."""
    hits = [r for r in packages.values() if r["name"] == args.name and (not args.kind or r["kind"] == args.kind)]
    if not hits:
        return {"ok": False, "reason": f"{args.name} isn't tracked by herdr-pkg (adopt it first)"}
    if len(hits) > 1:
        return {"ok": False, "reason": f"{args.name} is both a mod and a layout: add --kind"}
    return hits[0]


def cmd_pin(home, args, gh):
    """`pin NAME [REF]` (no REF: the installed commit), or `unpin NAME`: back to the default branch."""
    packages = home.load()
    rec = tracked(packages, args)
    if "ok" in rec:
        return rec
    rec.pop("pin", None)
    rec.pop("branch", None)
    if args.command == "pin":
        rec.update(ref_fields(gh, rec["source"], args.ref) or {"pin": rec["commit"]})
    home.save(packages)
    home.log(args.command, kind=rec["kind"], name=rec["name"], ref=rec.get("pin") or rec.get("branch"))
    return {"ok": True, "kind": rec["kind"], "name": rec["name"], **{k: rec[k] for k in ("pin", "branch") if k in rec}}


def cmd_remove(home, args):
    packages = home.load()
    rec = tracked(packages, args)
    if "ok" in rec:
        return rec
    if rec["kind"] == "mod":
        users = home.users(rec["name"])
        if users and not args.force:
            return {"ok": False, "reason": f"layouts still use it: {', '.join(users)} (edit them, or --force)"}
    p = home.path(rec["kind"], rec["name"])
    backup = os.path.join(home.data, "removed", f"{rec['kind']}-{rec['name']}-{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    if os.path.exists(p):
        shutil.move(p, backup)
    keys = home.keys()
    unbound = []
    for k in rec.get("keys", []):
        if keys.get(k["section"], {}).get(k["key"]) == k["action"]:
            del keys[k["section"]][k["key"]]
            unbound.append(k)
    if unbound:
        home.save_keys(keys)
    del packages[f"{rec['kind']}:{rec['name']}"]
    home.save(packages)
    home.log("remove", kind=rec["kind"], name=rec["name"], source=rec["source"], kept_in=backup)
    return {"ok": True, "kind": rec["kind"], "name": rec["name"], "moved_to": backup, "keys_unbound": unbound}


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
                print(f"✅ {i['kind']} {i['name']}: up to date ({s(i['installed'])}){' on ' + i['branch'] if i.get('branch') else ''}")
            elif i["status"] == "pinned":
                print(f"📌 {i['kind']} {i['name']}: pinned to {s(i['pin'])}")
            else:
                n += 1
                print(f"⬆️  {i['kind']} {i['name']}: {s(i['installed'])} → {s(i['latest'])} ({i['date']}) {i['subject']}")
                print(f"     https://github.com/{i['source']}/compare/{s(i['installed'])}...{s(i['latest'])}")
                if i["modified"]:
                    print(f"     modified here: {', '.join(i['modified'])} (an update merges your changes)")
                if i["status"] == "conflict":
                    print(f"⚠️  last update conflicted in {', '.join(i['conflicts'])}: {i['conflicts_dir']}")
        print(f"\n{n} update(s)")
    elif command == "update":
        for i in result["items"]:
            st = i["status"]
            head = f"{i['kind']} {i['name']}"
            if st == "current":
                print(f"✅ {head}: up to date")
            elif st == "pinned":
                print(f"📌 {head}: pinned (pkg.py unpin {i['name']} to follow upstream)")
            elif st == "updated" or st == "would update":
                m = f"; merged your changes into {', '.join(i['merged'])}" if i["merged"] else ""
                print(f"⬆️  {head}: {st} {s(i['from'])} → {s(i['to'])} ({i['date']}) {i['subject']}{m}")
            elif st == "conflict":
                print(f"⚠️  {head}: not updated, your changes conflict with upstream in {', '.join(i['conflicts'])}")
                print(f"     merged files with conflict markers: {i['conflicts_dir']}")
            else:
                print(f"❌ {head}: {st}: {i.get('reason', '')}")
    elif command == "install":
        rv = result.get("review")
        if rv:
            print(f"{rv['source']}{'/' + rv['subdir'] if rv['subdir'] else ''} @ {s(rv['commit'])}")
            if rv.get("renamed"):
                print(f"  mod {rv['renamed'][0]} installs as {rv['renamed'][1]}")
            for m in rv["mods"]:
                print(f"  mod {m['name']}: {len(m['files'])} files")
                for label, files in sorted(m["risks"].items()):
                    print(f"    {label}: {', '.join(files)}")
            for lay in rv["layouts"]:
                print(f"  layout {lay['name']}")
            for k in rv["keys"]:
                t = f" (taken by {k['taken_by']}: won't be bound)" if k["taken_by"] else ""
                print(f"  key {k['key']} → {k['action']}{t}")
            if not rv["has_recipe"]:
                print("  no RECIPE.md")
        if not result["ok"]:
            print(f"❌ {result['reason']}")
        elif result.get("installed"):
            print("✅ installed: " + ", ".join(f"{i['kind']} {i['name']}" for i in result["items"]))
        else:
            print(f"→ {result['next']}")
    elif command == "browse":
        for r in result["packages"]:
            where = r["repo"] + ("/" + r["subdir"] if r["subdir"] else "")
            what = "; ".join(x for x in [("mods: " + ", ".join(r["mods"])) if r["mods"] else "",
                                         ("layouts: " + ", ".join(r["layouts"])) if r["layouts"] else ""] if x)
            mark = " ✓" if r["installed"] else ""
            print(f"★{r['stars']:<4} {where}{mark}\n       {what}")
    elif command in ("pin", "unpin"):
        ref = result.get("pin") or result.get("branch")
        print(f"✅ {result['kind']} {result['name']}: " + ("pinned to " + ref if result.get("pin") else
              "follows " + (ref or "the default branch")) if result["ok"] else f"❌ {result['reason']}")
    elif command == "remove":
        if result["ok"]:
            print(f"✅ removed {result['kind']} {result['name']} (kept a copy in {result['moved_to']})")
        else:
            print(f"❌ {result['reason']}")


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
    u = sub.add_parser("update")
    u.add_argument("names", nargs="*")
    u.add_argument("--dry-run", action="store_true")
    i = sub.add_parser("install")
    i.add_argument("source")
    i.add_argument("--ref")
    i.add_argument("--yes", "-y", action="store_true")
    i.add_argument("--as", dest="as_name", metavar="NAME")
    b = sub.add_parser("browse")
    b.add_argument("query", nargs="?")
    r = sub.add_parser("remove")
    r.add_argument("name")
    r.add_argument("--kind", choices=["mod", "layout"])
    r.add_argument("--force", action="store_true")
    for c in ("pin", "unpin"):
        p = sub.add_parser(c)
        p.add_argument("name")
        if c == "pin":
            p.add_argument("ref", nargs="?")
        p.add_argument("--kind", choices=["mod", "layout"])
    args = ap.parse_args(argv)
    home = home or Home()
    if args.command == "adopt" and args.source and len(args.names) != 1:
        ap.error("--from needs exactly one NAME")
    gh = gh or GitHub(os.path.join(home.data, "cache"))
    result = {"list": lambda: cmd_list(home, args), "adopt": lambda: cmd_adopt(home, args, gh),
              "outdated": lambda: cmd_outdated(home, args, gh), "update": lambda: cmd_update(home, args, gh),
              "install": lambda: cmd_install(home, args, gh), "remove": lambda: cmd_remove(home, args),
              "browse": lambda: cmd_browse(home, args, gh), "pin": lambda: cmd_pin(home, args, gh),
              "unpin": lambda: cmd_pin(home, args, gh)}[args.command]()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")  # the emoji, piped on a Windows code page
        show(args.command, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
