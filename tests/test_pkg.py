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

    def history(self, repo, path, limit=pkg.HISTORY, ref=None):
        self.calls += 1
        log = git(self.repos[repo], "log", f"-{limit}", "--format=%H %cs %s", ref or "HEAD", "--", path)
        return [tuple(line.split(" ", 2)) for line in log.splitlines()]

    def is_branch(self, repo, ref):
        return subprocess.run(["git", "-C", self.repos[repo], "show-ref", "-q", "--verify", "refs/heads/" + ref]).returncode == 0

    def topic(self, topic):
        return self.topics.get(topic, [])

    def blob(self, repo, sha):
        return subprocess.run(["git", "-C", self.repos[repo], "cat-file", "blob", sha], check=True, capture_output=True).stdout

    def resolve(self, repo, ref=None):
        return git(self.repos[repo], "rev-parse", ref or "HEAD").strip()

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
        self.check_ok = True
        self.home.check = lambda path: (self.check_ok, "" if self.check_ok else "boom")

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


    # ---- update

    def commit_upstream(self, rel, text, msg):
        write(os.path.join(self.up, rel), text)
        git(self.up, "add", "-A")
        git(self.up, "commit", "-q", "-m", msg)
        return git(self.up, "rev-parse", "HEAD").strip()

    def read(self, rel):
        with open(os.path.join(self.home_dir, rel)) as f:
            return f.read()

    def test_update_untouched_copy_takes_upstream(self):
        self.install("stats", "v2")
        self.run_cmd("adopt")
        r = self.run_cmd("update")["items"][0]
        self.assertEqual((r["status"], r["to"]), ("updated", self.commits[2]))
        self.assertIn("// v3", self.read("mods/stats/main.js"))
        self.assertTrue(os.path.exists(os.path.join(self.home_dir, "mods", "stats", "gpui-kit.d.ts")))  # generated files kept
        self.assertEqual(self.run_cmd("outdated")["items"][0]["status"], "current")

    def test_update_merges_a_local_patch(self):
        # upstream main.js grows a line at the end; the local copy patched the top
        lines = "".join(f"line {i}\n" for i in range(12))
        base = self.commit_upstream("ex/mods/stats/main.js", "// v3\n" + lines, "longer")
        self.install("stats", "v3", extra=lines.replace("export default 1;\n", ""))
        with open(os.path.join(self.home_dir, "mods", "stats", "main.js"), "w") as f:
            f.write("// v3 patched here\n" + lines)
        self.run_cmd("adopt")
        rec = self.home.load()["mod:stats"]
        self.assertEqual((rec["commit"], rec["modified"]), (base, ["main.js"]))
        self.commit_upstream("ex/mods/stats/main.js", "// v3\n" + lines + "upstream addition\n", "add")
        r = self.run_cmd("update")["items"][0]
        self.assertEqual((r["status"], r["merged"]), ("updated", ["main.js"]))
        text = self.read("mods/stats/main.js")
        self.assertIn("patched here", text)
        self.assertIn("upstream addition", text)
        self.assertEqual(self.home.load()["mod:stats"]["modified"], ["main.js"])  # still carries the patch

    def test_update_conflict_writes_nothing(self):
        self.install("stats", "v3")
        self.run_cmd("adopt")
        with open(os.path.join(self.home_dir, "mods", "stats", "main.js"), "w") as f:
            f.write("// mine\nexport default 1;\n")
        self.commit_upstream("ex/mods/stats/main.js", "// theirs\nexport default 1;\n", "v4")
        r = self.run_cmd("update")["items"][0]
        self.assertEqual((r["status"], r["conflicts"]), ("conflict", ["main.js"]))
        self.assertIn("// mine", self.read("mods/stats/main.js"))
        with open(os.path.join(r["conflicts_dir"], "main.js")) as f:
            self.assertIn("<<<<<<<", f.read())
        o = self.run_cmd("outdated")["items"][0]  # the row shows the conflict, not just "update"
        self.assertEqual((o["status"], o["conflicts"]), ("conflict", ["main.js"]))
        self.commit_upstream("ex/mods/stats/mod.json", '{"place":"top"}\n', "v5")  # newer upstream: plain update again
        self.assertEqual(self.run_cmd("outdated")["items"][0]["status"], "update")

    def test_update_rebases_a_copy_installed_outside_pkg(self):
        # Issue #6: adopted at v3, then upstream moves main.js into a subfolder and the mod's own
        # installer copies that layout in; a later upstream change must update, not conflict.
        self.install("stats", "v3")
        self.run_cmd("adopt")
        os.remove(os.path.join(self.up, "ex", "mods", "stats", "main.js"))
        moved = self.commit_upstream("ex/mods/stats/files/main.js", "// moved\n", "move")
        d = os.path.join(self.home_dir, "mods", "stats")
        os.remove(os.path.join(d, "main.js"))
        write(os.path.join(d, "files", "main.js"), "// moved\n")
        self.assertEqual(self.run_cmd("outdated")["items"][0]["status"], "current")  # re-based, not "update"
        latest = self.commit_upstream("ex/mods/stats/files/main.js", "// moved v2\n", "change")
        r = self.run_cmd("update")["items"][0]
        self.assertEqual((r["status"], r["from"], r["to"]), ("updated", moved, latest))
        self.assertEqual(self.read("mods/stats/files/main.js"), "// moved v2\n")

    def test_update_rolls_back_when_check_fails(self):
        self.install("stats", "v2")
        self.run_cmd("adopt")
        self.check_ok = False
        r = self.run_cmd("update")["items"][0]
        self.assertEqual(r["status"], "check failed, rolled back")
        self.assertIn("// v2", self.read("mods/stats/main.js"))
        self.assertEqual(self.home.load()["mod:stats"]["commit"], self.commits[1])

    # ---- pin

    def test_pin_stops_update_and_unpin_resumes(self):
        self.install("stats", "v2")
        self.run_cmd("adopt")
        self.assertEqual(self.run_cmd("pin", "stats")["pin"], self.commits[1])  # no ref: the installed commit
        self.assertEqual(self.run_cmd("outdated")["items"][0]["status"], "pinned")
        self.assertEqual(self.run_cmd("update")["items"][0]["status"], "pinned")
        self.assertIn("// v2", self.read("mods/stats/main.js"))
        self.run_cmd("unpin", "stats")
        self.assertEqual(self.run_cmd("update")["items"][0]["to"], self.commits[2])

    def test_pin_to_a_tag_moves_there_once(self):
        git(self.up, "tag", "v1", self.commits[0])
        self.install("stats", "v3")
        self.run_cmd("adopt")
        self.assertEqual(self.run_cmd("pin", "stats", "v1")["pin"], "v1")
        self.assertEqual(self.run_cmd("update")["items"][0]["to"], self.commits[0])
        self.commit_upstream("ex/mods/stats/main.js", "// v4\n", "v4")
        self.assertEqual(self.run_cmd("outdated")["items"][0]["status"], "pinned")

    def test_install_ref_branch_follows_that_branch(self):
        git(self.up, "checkout", "-q", "-b", "dev")
        dev = self.commit_upstream("ex/mods/stats/main.js", "// dev 1\n", "dev 1")
        git(self.up, "checkout", "-q", "main")
        self.run_cmd("install", "o/examples/ex", "--ref", "dev", "--yes")
        self.assertEqual(self.home.load()["mod:stats"]["branch"], "dev")
        self.commit_upstream("ex/mods/stats/main.js", "// main 4\n", "main moves")  # not followed
        self.assertEqual({i["name"]: i["status"] for i in self.run_cmd("outdated")["items"]}, {"stats": "current", "board": "current"})
        git(self.up, "checkout", "-q", "dev")
        later = self.commit_upstream("ex/mods/stats/main.js", "// dev 2\n", "dev 2")
        git(self.up, "checkout", "-q", "main")
        r = {i["name"]: i for i in self.run_cmd("update")["items"]}["stats"]
        self.assertEqual((r["status"], r["from"], r["to"]), ("updated", dev, later))
        self.assertEqual(self.read("mods/stats/main.js"), "// dev 2\n")

    # ---- install / remove

    def test_install_reviews_then_installs_and_binds_free_keys(self):
        write(os.path.join(self.up, "ex", "keys.json"), json.dumps({"global": {"alt-shift-s": "mod.open:stats", "alt-x": "mod.open:stats"}}))
        write(os.path.join(self.up, "ex", "mods", "stats", "main.js"), "const r = exec('git');\n")
        git(self.up, "add", "-A")
        git(self.up, "commit", "-q", "-m", "keys")
        write(os.path.join(self.home_dir, "keys.json"), json.dumps({"global": {"alt-x": "pane.zoom"}}))
        r = self.run_cmd("install", "o/examples/ex")
        self.assertFalse(r["installed"])
        self.assertEqual(r["review"]["mods"][0]["risks"], {"runs programs": ["main.js"]})
        self.assertFalse(os.path.exists(os.path.join(self.home_dir, "mods", "stats")))
        r = self.run_cmd("install", "o/examples/ex", "--yes")
        self.assertEqual([(i["kind"], i["name"]) for i in r["items"]], [("mod", "stats"), ("layout", "board")])
        keys = json.loads(self.read("keys.json"))["global"]
        self.assertEqual(keys, {"alt-x": "pane.zoom", "alt-shift-s": "mod.open:stats"})  # taken key left alone
        self.assertEqual(self.run_cmd("outdated")["items"][0]["status"], "current")
        # removing unbinds what it bound; a layout using the mod blocks it until --force
        write(os.path.join(self.home_dir, "layouts", "mine.json"), '{"content": {"mod": "stats"}}')
        self.assertFalse(self.run_cmd("remove", "stats", "--kind", "mod")["ok"])
        r = self.run_cmd("remove", "stats", "--kind", "mod", "--force")
        self.assertTrue(r["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.home_dir, "mods", "stats")))
        self.assertEqual(json.loads(self.read("keys.json"))["global"], {"alt-x": "pane.zoom"})

    def test_install_refuses_a_name_clash(self):
        self.install("stats", "mine")
        r = self.run_cmd("install", "o/examples/ex", "--yes")
        self.assertFalse(r["ok"])
        self.assertIn("// mine", self.read("mods/stats/main.js"))

    def test_install_rolls_back_when_check_fails(self):
        self.check_ok = False
        self.assertFalse(self.run_cmd("install", "o/examples/ex", "--yes")["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.home_dir, "mods", "stats")))

    def test_remove_untracked_is_refused(self):
        self.install("nowhere", "zz")
        self.assertFalse(self.run_cmd("remove", "nowhere")["ok"])
        self.assertTrue(os.path.exists(os.path.join(self.home_dir, "mods", "nowhere")))


if __name__ == "__main__":
    unittest.main()
