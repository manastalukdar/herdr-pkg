"""Tests for pkg.py against local git repos standing in for GitHub (no network).

    python3 -m unittest discover -s tests
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mods", "pkg"))
import pkg  # noqa: E402


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class FakeGitHub:
    """pkg.GitHub's interface, read from local repos: {"owner/repo": dir}."""

    def __init__(self, repos, topics):
        self.repos, self.topics, self.calls = repos, topics, 0

    def head(self, repo):
        self.calls += 1
        return git(self.repos[repo], "rev-parse", "HEAD").strip()

    def tree(self, repo, commit):
        self.calls += 1
        out = {}
        for line in git(self.repos[repo], "ls-tree", "-r", commit).splitlines():
            meta, path = line.split("\t", 1)
            _, kind, sha = meta.split()
            if kind == "blob":
                out[path] = sha
        return out

    def history(self, repo, path, limit=pkg.HISTORY):
        self.calls += 1
        log = git(self.repos[repo], "log", f"-{limit}", "--format=%H %cs %s", "--", path)
        return [tuple(line.split(" ", 2)) for line in log.splitlines()]

    def topic(self, topic):
        return self.topics.get(topic, [])

    def index(self):
        out = {}
        for t in ("herdr-mod", "herdr-layout"):
            for r in self.topic(t):
                tree = self.tree(r["repo"], self.head(r["repo"]))
                e = out.setdefault(r["repo"], {"stars": r["stars"], "mod": {}, "layout": {}})
                for name in {p.split("mods/")[1].split("/")[0] for p in tree if "mods/" in p}:
                    e["mod"][name] = pkg.package_paths(tree, "mod", name)
                for p in tree:
                    if ("/" + p).count("/layouts/") and p.endswith(".json"):
                        e["layout"].setdefault(p.split("/")[-1][:-5], []).append(p)
        return out


class PkgTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.home_dir = os.path.join(t, "home")
        write(os.path.join(self.home_dir, "installed.json"), json.dumps({"tabs": {}, "files": {}, "layout:main": {}}))
        os.makedirs(os.path.join(self.home_dir, "mods", "tabs"))  # Herdr's own: never listed
        # upstream repo with a mod (3 commits) and a layout, in a subfolder like herdr-layout-examples
        self.up = os.path.join(t, "up")
        os.makedirs(self.up)
        git(self.up, "init", "-q", "-b", "main")
        git(self.up, "config", "user.email", "t@t")
        git(self.up, "config", "user.name", "t")
        self.commits = []
        for i, body in enumerate(["v1", "v2", "v3"]):
            write(os.path.join(self.up, "ex", "mods", "stats", "main.js"), f"// {body}\nexport default 1;\n")
            write(os.path.join(self.up, "ex", "mods", "stats", "mod.json"), '{"place":"none"}\n')
            if i == 0:
                write(os.path.join(self.up, "ex", "layouts", "board.json"), '{"id":"root"}\n')
                write(os.path.join(self.up, "README.md"), "x\n")
            git(self.up, "add", "-A")
            git(self.up, "commit", "-q", "-m", f"stats {body}")
            self.commits.append(git(self.up, "rev-parse", "HEAD").strip())
        self.gh = FakeGitHub({"o/examples": self.up, "o/other": self.up},
                             {"herdr-mod": [{"repo": "o/other", "stars": 1}, {"repo": "o/examples", "stars": 9}]})
        self.home = pkg.Home(self.home_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, name, version, extra=""):
        d = os.path.join(self.home_dir, "mods", name)
        write(os.path.join(d, "main.js"), f"// {version}\nexport default 1;\n{extra}")
        write(os.path.join(d, "mod.json"), '{"place":"none"}\n')
        write(os.path.join(d, "gpui-kit.d.ts"), "generated")  # ignored
        write(os.path.join(d, "jsconfig.json"), "{}")  # ignored

    def run_cmd(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pkg.main(["--json", *argv], home=self.home, gh=self.gh)
        return json.loads(out.getvalue())

    def test_blob_sha_matches_git(self):
        p = os.path.join(self.up, "README.md")
        with open(p, "rb") as f:
            self.assertEqual(pkg.blob_sha(f.read()), git(self.up, "hash-object", p).strip())

    def test_adopt_finds_the_older_commit_installed_then_outdated(self):
        self.install("stats", "v2")
        r = self.run_cmd("adopt")
        self.assertEqual([(a["name"], a["commit"], a["modified"]) for a in r["adopted"]],
                         [("stats", self.commits[1], [])])
        self.assertEqual(r["adopted"][0]["path"], "ex/mods/stats")
        o = self.run_cmd("outdated")["items"]
        self.assertEqual([(i["name"], i["status"], i["latest"]) for i in o], [("stats", "update", self.commits[2])])

    def test_adopt_current_copy_is_up_to_date(self):
        self.install("stats", "v3")
        self.run_cmd("adopt")
        self.assertEqual(self.run_cmd("outdated")["items"][0]["status"], "current")

    def test_patched_copy_is_adopted_as_modified_on_its_closest_commit(self):
        self.install("stats", "v3", extra="// local patch\n")
        a = self.run_cmd("adopt")["adopted"][0]
        self.assertEqual(a["modified"], ["main.js"])
        items = {i["name"]: i for i in self.run_cmd("list")["items"]}
        self.assertEqual(items["stats"]["status"], "modified")

    def test_layout_adopted_from_a_subfolder(self):
        write(os.path.join(self.home_dir, "layouts", "board.json"), '{"id":"root"}\n')
        self.gh.topics["herdr-layout"] = [{"repo": "o/examples", "stars": 9}]
        a = self.run_cmd("adopt")["adopted"]
        self.assertEqual([(x["kind"], x["name"], x["path"]) for x in a], [("layout", "board", "ex/layouts/board.json")])

    def test_published_clone_is_adopted(self):
        # a clone of Herdr's own mod that someone published: matched like any other mod
        self.install("stats", "v3")
        self.assertEqual(self.run_cmd("adopt")["adopted"][0]["name"], "stats")

    def test_clones_and_personal_layouts_are_not_adopted(self):
        d = os.path.join(self.home_dir, "mods", "tabs-plus")
        write(os.path.join(d, "mod.json"), json.dumps({"cloned_from": {"name": "tabs", "version": "0.1.11"}}))
        write(os.path.join(self.home_dir, "layouts", "mine.json"), json.dumps({"cloned_from": {"name": "main-folders", "version": "0.1.11"}}))
        r = self.run_cmd("adopt")
        self.assertEqual({s["name"] for s in r["skipped"]}, {"tabs-plus", "mine"})
        items = {i["name"]: i["status"] for i in self.run_cmd("list")["items"]}
        self.assertEqual(items, {"tabs-plus": "clone", "mine": "personal"})

    def test_unknown_mod_stays_untracked(self):
        self.install("nowhere", "zz")
        r = self.run_cmd("adopt")
        self.assertEqual([u["name"] for u in r["untracked"]], ["nowhere"])
        self.assertEqual(self.run_cmd("list")["items"][0]["status"], "untracked")

    def test_import_and_from(self):
        self.install("stats", "v1")
        src = os.path.join(self.tmp.name, "sources.json")
        write(src, json.dumps({"mods": {"stats": {"repo": "o/examples", "path": "ex/mods/stats"}}}))
        self.gh.topics = {}  # no index: must come from the import
        self.assertEqual(self.run_cmd("adopt", "--import", src)["adopted"][0]["commit"], self.commits[0])
        os.remove(os.path.join(self.home.data, "packages.json"))
        self.assertEqual(self.run_cmd("adopt", "stats", "--from", "o/examples/ex")["adopted"][0]["commit"], self.commits[0])

    def test_edits_after_adopting_are_reported(self):
        self.install("stats", "v3")
        self.run_cmd("adopt")
        with open(os.path.join(self.home_dir, "mods", "stats", "main.js"), "a") as f:
            f.write("// edited\n")
        self.assertEqual(self.run_cmd("list")["items"][0]["edited"], ["main.js"])

    def test_dry_run_saves_nothing(self):
        self.install("stats", "v3")
        self.run_cmd("adopt", "--dry-run")
        self.assertEqual(self.home.load(), {})


if __name__ == "__main__":
    unittest.main()
