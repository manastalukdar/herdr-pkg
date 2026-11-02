// herdr-pkg's dialog: what's installed (mods and layouts in their own sections), herdr.dev's
// packages, and the install review. All the work is done by pkg.py (the same commands as on the
// command line, with --json); this file only shows the results and runs the buttons.

import { View, div } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { exec, appHome, props, closeOverlay, notify, theme, window, run } from "herdr";

const short = (sha) => (sha ? sha.slice(0, 7) : "?");
const opener = () => (window()?.platform === "macos" ? "open" : window()?.platform === "windows" ? "explorer" : "xdg-open");
// Windows has no python3: the py launcher (python.org's installer), else python (the Store's).
const pythons = () => (window()?.platform === "windows" ? [["py", "-3"], ["python"]] : [["python3"]]);

const KINDS = {
  mod: { title: "Mods", what: "Pieces of your window: panels, dialogs, tabs and status-bar blocks. A mod shows up in whatever layout places it, or opens from a key." },
  layout: { title: "Layouts", what: "Whole-window arrangements of blocks. You use one at a time; switch with ctrl-shift-l." },
};

// status -> [label, theme color key, what it means]
const STATUS = {
  current: ["up to date", "done", "Same files as the newest upstream commit."],
  update: ["update", "blocked", "Upstream has a newer version."],
  pinned: ["pinned", "muted", "Pinned to a tag or commit; updates skip it (pkg.py unpin NAME to follow upstream)."],
  conflict: ["conflict", "ansi1", "The last update conflicted with your changes; nothing was written."],
  modified: ["modified", "working", "Changed here compared with upstream; an update merges your changes in."],
  tracked: ["tracked", "muted", "Checking for updates…"],
  clone: ["clone", "dim", "A copy of one of Herdr's own; only you maintain it."],
  personal: ["personal", "dim", "Your own copy of another layout or mod; never updated."],
  untracked: ["untracked", "dim", "Installed before herdr-pkg and not matched to a repo yet: Adopt looks for it."],
  missing: ["missing", "blocked", "Recorded but no longer installed."],
};

export default class Pkg extends View {
  init(_props, cx) {
    // In a layout with props {bar: true}: a status-bar block, "↑ N" when updates wait (checked
    // once a day; the cache is shared with the dialog and dropped when packages change).
    this.bar = Boolean(props()?.bar);
    if (this.bar) {
      this.waiting = [];
      const check = (cx) => cx.spawn(async (cx) => {
        try {
          const o = await this.pkg(["outdated", "--cached", String(24 * 3600)]);
          this.waiting = o.items.filter((i) => i.status === "update" || i.status === "conflict");
        } catch { /* offline or no python: show nothing */ }
        cx.notify();
      });
      cx.timer.every(10 * 60 * 1000, check); // reads the cache; the network only once it's a day old
      check(cx);
      return;
    }
    this.tab = props()?.tab ?? "installed"; // installed | browse | review
    this.items = [];
    this.outdated = {};
    this.packages = null;
    this.review = null;
    this.busy = null;
    this.message = null;
    this.query = "";
    this.handle = cx.focus_handle();
    this.search = InputState.new({ placeholder: "Search mods and layouts" });
    this.search.on("change", (_e, cx) => { this.query = this.search.value(); cx.notify(); });
    this.handle.focus();
    cx.spawn(async (cx) => this.refresh(cx));
  }

  async pkg(args) {
    let r, err;
    for (const [cmd, ...pre] of pythons()) {
      try { r = await exec(cmd, [...pre, `${appHome()}/mods/pkg/pkg.py`, "--json", ...args]); break; } catch (e) { err = e; }
    }
    if (!r) throw err;
    if (r.code !== 0) throw new Error((r.stderr || r.stdout).trim().split("\n").pop());
    return JSON.parse(r.stdout);
  }

  async work(cx, label, fn) {
    this.busy = label;
    this.message = null;
    cx.notify();
    try {
      await fn();
    } catch (e) {
      this.message = String(e?.message ?? e);
    }
    this.busy = null;
    cx.notify();
  }

  async refresh(cx) {
    await this.work(cx, "Reading what's installed…", async () => {
      this.items = (await this.pkg(["list"])).items;
    });
    // The update check uses the network; the list shows meanwhile.
    this.checking = true;
    cx.notify();
    try {
      const o = await this.pkg(["outdated"]);
      this.outdated = Object.fromEntries(o.items.map((i) => [`${i.kind}:${i.name}`, i]));
    } catch (e) {
      this.message = `Couldn't check for updates: ${e?.message ?? e}`;
    }
    this.checking = false;
    cx.notify();
  }

  status(item) {
    const o = this.outdated[`${item.kind}:${item.name}`];
    if (o?.status === "update" || o?.status === "conflict" || o?.status === "pinned") return o.status;
    if (item.status === "modified") return "modified";
    if (item.status === "tracked") return o ? "current" : "tracked";
    return item.status;
  }

  // ---- actions

  update(cx, names) {
    cx.spawn(async (cx) => this.work(cx, `Updating ${names.length ? names.join(", ") : "everything"}…`, async () => {
      const r = await this.pkg(["update", ...names]);
      const done = r.items.filter((i) => i.status === "updated");
      const bad = r.items.filter((i) => !["updated", "current", "pinned"].includes(i.status));
      this.message = [done.length ? `Updated ${done.map((i) => i.name).join(", ")}.` : "",
        ...bad.map((i) => `${i.name}: ${i.status}${i.conflicts?.length ? ` in ${i.conflicts.join(", ")} (see ${i.conflicts_dir})` : i.reason ? `: ${i.reason}` : ""}`)]
        .filter(Boolean).join(" ") || "Nothing to update.";
      if (done.length) notify({ title: `Updated ${done.length}`, body: done.map((i) => i.name).join("\n"), level: "success" });
      const conflicted = bad.filter((i) => i.status === "conflict");
      if (conflicted.length) notify({ title: `Not updated: conflict${conflicted.length > 1 ? "s" : ""}`, level: "warning",
        body: conflicted.map((i) => `${i.name}: ${i.conflicts.join(", ")}\n${i.conflicts_dir}`).join("\n") });
      await this.refresh(cx);
    }));
  }

  adopt(cx, names) {
    cx.spawn(async (cx) => this.work(cx, "Looking for where these came from…", async () => {
      const r = await this.pkg(["adopt", ...names]);
      this.message = [r.adopted.length ? `Adopted ${r.adopted.map((a) => `${a.name} (${a.source})`).join(", ")}.` : "",
        ...r.untracked.map((u) => `${u.name}: ${u.reason}`), ...r.skipped.map((u) => `${u.name}: ${u.reason}`)].filter(Boolean).join(" ");
      await this.refresh(cx);
    }));
  }

  remove(cx, item) {
    if (this.confirmRemove !== `${item.kind}:${item.name}`) {
      this.confirmRemove = `${item.kind}:${item.name}`;
      cx.notify();
      return;
    }
    this.confirmRemove = null;
    cx.spawn(async (cx) => this.work(cx, `Removing ${item.name}…`, async () => {
      const r = await this.pkg(["remove", item.name, "--kind", item.kind]);
      this.message = r.ok ? `Removed ${item.kind} ${item.name} (a copy is kept in ${r.moved_to}).` : r.reason;
      await this.refresh(cx);
    }));
  }

  openRepo(source) {
    exec(opener(), [`https://github.com/${source}`]).catch(() => {});
  }

  browse(cx) {
    this.tab = "browse";
    if (!this.packages) {
      cx.spawn(async (cx) => this.work(cx, "Loading herdr.dev's mods and layouts…", async () => {
        this.packages = (await this.pkg(["browse"])).packages;
      }));
    }
    cx.notify();
  }

  startReview(cx, p, as) {
    const source = p.repo + (p.subdir ? `/${p.subdir}` : "");
    this.tab = "review";
    this.review = null;
    cx.spawn(async (cx) => this.work(cx, `Reading ${source}…`, async () => {
      this.review = { source, as, ...(await this.pkg(["install", source, ...(as ? ["--as", as] : [])])) };
    }));
  }

  install(cx) {
    const { source, as } = this.review;
    cx.spawn(async (cx) => this.work(cx, `Installing ${source}…`, async () => {
      const r = await this.pkg(["install", source, "--yes", ...(as ? ["--as", as] : [])]);
      if (!r.ok) {
        this.message = r.reason;
        return;
      }
      const names = r.items.map((i) => `${i.kind} ${i.name}`).join(", ");
      notify({ title: "Installed", body: names, level: "success" });
      this.message = `Installed ${names}.` + (r.items.some((i) => i.kind === "layout") ? " Switch to a new layout with ctrl-shift-l." : "");
      this.packages = null;
      this.tab = "installed";
      await this.refresh(cx);
    }));
  }

  // ---- view pieces

  button(t, id, label, onClick, strong) {
    return div().id(id).px(9).h(24).flex().items_center().rounded(6).flex_none().text_size(12)
      .border_1().border_color(strong ? t.accent : t.border)
      .when(strong, (el) => el.bg(t.accent).text_color(t.accent_foreground))
      .when(!strong, (el) => el.text_color(t.foreground).hover((s) => s.bg(t.hover)))
      .on_click((_e, cx) => { cx.stop_propagation(); onClick(cx); })
      .child(label);
  }

  tabButton(t, id, label, on, onClick) {
    return div().id(id).px(10).h(28).flex().items_center().rounded(7).text_size(13)
      .when(on, (el) => el.bg(t.selected).text_color(t.foreground))
      .when(!on, (el) => el.text_color(t.muted).hover((s) => s.bg(t.hover)))
      .on_click((_e, cx) => onClick(cx))
      .child(label);
  }

  row(t, item) {
    const st = this.status(item);
    const [label, color, meaning] = STATUS[st] ?? [st, "dim", ""];
    const key = `${item.kind}:${item.name}`;
    const source = item.source ? `${item.source} @ ${short(this.outdated[key]?.installed ?? item.commit)}` :
      item.cloned_from ? `clone of ${item.cloned_from.name} (${item.cloned_from.version})` : "";
    const o = this.outdated[key];
    const note = st === "update" ? `→ ${short(o.latest)} ${o.date}: ${o.subject}` :
      st === "conflict" ? `conflict in ${o.conflicts.join(", ")}: merge by hand from ${o.conflicts_dir}, then Retry` :
      item.edited?.length ? `edited here since: ${item.edited.join(", ")}` :
      st === "modified" ? `differs from upstream in ${item.modified.join(", ")}` : meaning;
    const actions = [];
    if (st === "update" || st === "conflict") actions.push(this.button(t, `up-${key}`, st === "update" ? "Update" : "Retry", (cx) => this.update(cx, [item.name]), true));
    if (st === "untracked") actions.push(this.button(t, `ad-${key}`, "Adopt", (cx) => this.adopt(cx, [item.name])));
    if (item.source) actions.push(this.button(t, `gh-${key}`, "Repo", () => this.openRepo(item.source)));
    if (item.source) actions.push(this.button(t, `rm-${key}`, this.confirmRemove === key ? "Remove?" : "Remove", (cx) => this.remove(cx, item)));
    return h_flex().id(`row-${key}`).px(10).py(6).gap(10).items_center().rounded(7).flex_none().hover((s) => s.bg(t.hover))
      .child(div().w(8).h(8).rounded(4).flex_none().bg(t[color] ?? t.dim))
      .child(v_flex().flex_1().min_w(0)
        .child(h_flex().gap(8).items_center()
          .child(div().text_size(13).text_color(t.foreground).child(item.name))
          .child(div().text_size(11).text_color(t[color] ?? t.dim).child(label))
          .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(t.dim).child(source)))
        .child(div().truncate().text_size(11.5).text_color(t.muted).child(note)))
      .child(h_flex().gap(6).flex_none().children(actions));
  }

  section(t, kind, items) {
    const k = KINDS[kind];
    return v_flex().gap(4).flex_none()
      .child(h_flex().gap(8).items_baseline()
        .child(div().text_size(14).font_semibold().text_color(t.foreground).child(k.title))
        .child(div().text_size(12).text_color(t.dim).child(String(items.length))))
      .child(div().text_size(12).text_color(t.muted).pb(2).child(k.what))
      .children(items.length ? items.map((i) => this.row(t, i)) : [div().px(10).text_size(12).text_color(t.dim).child("None installed.")]);
  }

  installedView(t) {
    const updates = this.items.filter((i) => this.status(i) === "update");
    const untracked = this.items.filter((i) => i.status === "untracked");
    return [
      h_flex().gap(8).items_center()
        .child(div().text_size(12).text_color(this.checking ? t.working : t.dim)
          .child(this.checking ? "Checking for updates…" : updates.length ? `${updates.length} update${updates.length > 1 ? "s" : ""} available` : "Everything is up to date"))
        .child(div().flex_1())
        .when(untracked.length > 0, (el) => el.child(this.button(t, "adopt-all", `Adopt ${untracked.length} untracked`, (cx) => this.adopt(cx, untracked.map((i) => i.name)))))
        .when(updates.length > 1, (el) => el.child(this.button(t, "update-all", `Update all (${updates.length})`, (cx) => this.update(cx, []), true)))
        .child(this.button(t, "refresh", "Refresh", (cx) => cx.spawn(async (cx) => this.refresh(cx)))),
      v_flex().id("installed-list").flex_1().min_h(0).gap(16).overflow_y_scrollbar()
        .child(this.section(t, "mod", this.items.filter((i) => i.kind === "mod")))
        .child(this.section(t, "layout", this.items.filter((i) => i.kind === "layout"))),
    ];
  }

  browseView(t) {
    const q = this.query.toLowerCase();
    const list = (this.packages ?? []).filter((p) => !q || [p.repo, p.description, ...p.mods, ...p.layouts].join(" ").toLowerCase().includes(q));
    const card = (p, i) => {
      const id = `${p.repo}/${p.subdir}`;
      const installed = p.installed.length > 0;
      return h_flex().id(`pk-${i}`).px(10).py(6).gap(10).items_center().rounded(7).flex_none().hover((s) => s.bg(t.hover))
        .child(v_flex().flex_1().min_w(0)
          .child(h_flex().gap(8).items_center()
            .child(div().text_size(13).text_color(t.foreground).truncate().child(p.repo + (p.subdir ? ` / ${p.subdir}` : "")))
            .child(div().text_size(11).text_color(t.dim).child(`★ ${p.stars}`))
            .when(installed, (el) => el.child(div().text_size(11).text_color(t.done).child("installed"))))
          .child(div().truncate().text_size(11.5).text_color(t.muted).child(
            [p.mods.length ? `Mods: ${p.mods.join(", ")}` : "", p.layouts.length ? `Layouts: ${p.layouts.join(", ")}` : ""].filter(Boolean).join(" · ")))
          .when(Boolean(p.description), (el) => el.child(div().truncate().text_size(11.5).text_color(t.dim).child(p.description))))
        .child(this.button(t, `rv-${id}`, installed ? "Details" : "Review", (cx) => this.startReview(cx, p), !installed))
        .child(this.button(t, `gb-${id}`, "Repo", () => this.openRepo(p.repo)));
    };
    return [
      h_flex().h(32).px(10).items_center().rounded(8).border_1().border_color(t.border).bg(t.background)
        .child(Input.new(this.search).flex_1().h_full().text_size(12.8).text_color(t.foreground)),
      div().text_size(12).text_color(t.dim).child(this.packages ? `${list.length} of ${this.packages.length} packages from herdr.dev (GitHub topics herdr-mod and herdr-layout). Not reviewed by Herdr: check what a package does before installing it.` : ""),
      v_flex().id("browse-list").flex_1().min_h(0).gap(2).overflow_y_scrollbar().children(list.slice(0, 150).map(card)),
    ];
  }

  reviewView(t) {
    const r = this.review;
    if (!r) return [div().text_color(t.dim).child("")];
    const rv = r.review;
    const line = (text, color) => div().text_size(12.5).text_color(color ?? t.muted).child(text);
    const block = (title, lines) => v_flex().gap(3).flex_none()
      .child(div().text_size(13).font_semibold().text_color(t.foreground).child(title)).children(lines);
    const blocks = [];
    if (rv) {
      blocks.push(block("What it adds", [
        ...rv.mods.map((m) => line(`Mod ${m.name}: ${m.files.length} files, copied to mods/${m.name}/`)),
        ...rv.layouts.map((l) => line(`Layout ${l.name}: added to your layouts; your current one stays until you switch`)),
        line(`From ${rv.source}${rv.subdir ? `/${rv.subdir}` : ""} @ ${short(rv.commit)}${rv.has_recipe ? "" : " · no RECIPE.md"}`, t.dim),
      ]));
      const risks = rv.mods.flatMap((m) => Object.entries(m.risks).map(([label, files]) => line(`• ${m.name} ${label}: ${files.join(", ")}`, t.blocked)));
      blocks.push(block("What the code does", risks.length ? risks : [line("No programs, network or file access found.", t.done)]));
      blocks.push(block("Keys", rv.keys.length ? rv.keys.map((k) => k.taken_by
        ? line(`${k.key} stays ${k.taken_by}; it wanted ${k.action}`, t.blocked)
        : line(`+ ${k.key} → ${k.action}`)) : [line("None.")]));
      if (rv.clashes.length) blocks.push(block("Can't install", rv.clashes.map((c) => line(c, t.blocked))));
    }
    if (!r.ok && !rv) blocks.push(block("Can't install", [line(r.reason, t.blocked)]));
    const can = r.ok && rv && !rv.clashes.length && !r.installed;
    // A clash on the package's one mod (and nothing else): offer it under another name (pkg.py install --as).
    const as = rv && rv.mods.length === 1 && rv.clashes.length && rv.clashes.every((c) => c.startsWith("mod ")) ? `${rv.mods[0].name}-2` : null;
    return [
      div().text_size(12).text_color(t.dim).child("The scan is a text match: it can over-report, and it can't prove a package is safe. Open the repo to read the code."),
      v_flex().id("review-body").flex_1().min_h(0).gap(14).overflow_y_scrollbar().children(blocks),
      h_flex().gap(8)
        .child(div().flex_1())
        .child(this.button(t, "rv-repo", "Repo", () => this.openRepo(r.source.split("/").slice(0, 2).join("/"))))
        .child(this.button(t, "rv-back", "Back", (cx) => { this.tab = "browse"; cx.notify(); }))
        .when(can, (el) => el.child(this.button(t, "rv-install", "Install", (cx) => this.install(cx), true)))
        .when(!!as, (el) => el.child(this.button(t, "rv-as", `Review as ${as}`, (cx) => this.startReview(cx, { repo: r.source }, as), true))),
    ];
  }

  barView(t) {
    const n = this.waiting.length;
    const conflict = this.waiting.some((i) => i.status === "conflict");
    return h_flex().id("pkg-bar").size_full().px(8).items_center().justify_center().bg(t.chrome).text_size(12)
      .when(n > 0, (el) => el.cursor_pointer().hover((s) => s.bg(t.hover))
        .on_click(() => run("mod.open", { name: "pkg" }))
        .child(div().text_color(conflict ? t.ansi1 : t.blocked)
          .child(`↑ ${n}`)));
  }

  render(_cx) {
    const t = theme();
    if (this.bar) return this.barView(t);
    const body = this.tab === "browse" ? this.browseView(t) : this.tab === "review" ? this.reviewView(t) : this.installedView(t);
    return v_flex().id("root").track_focus(this.handle).size_full().p(16).gap(12).bg(t.popover).text_color(t.foreground)
      .on_key_down((e, cx) => {
        if (e.key === "escape") {
          if (this.tab === "review") this.tab = "browse";
          else if (props()?.overlay) closeOverlay(props().overlay);
          cx.stop_propagation();
          cx.notify();
        }
      })
      .child(h_flex().gap(6).items_center()
        .child(div().text_size(15).font_semibold().pr(8).child("Packages"))
        .child(this.tabButton(t, "tab-installed", "Installed", this.tab === "installed", (cx) => { this.tab = "installed"; cx.notify(); }))
        .child(this.tabButton(t, "tab-browse", "Browse", this.tab !== "installed", (cx) => this.browse(cx)))
        .child(div().flex_1())
        .child(div().text_size(11).text_color(t.dim).child("esc")))
      .children(body)
      .when(Boolean(this.busy), (el) => el.child(div().text_size(12).text_color(t.working).child(this.busy)))
      .when(Boolean(this.message) && !this.busy, (el) => el.child(div().text_size(12).text_color(t.foreground).child(this.message)));
  }
}
