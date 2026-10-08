/* ==========================================================
   detail.js — the detail window: one memory, what it led to, where it came from

   Any row anywhere opens it with `openDetail(id)`; `openDetail(id, {layer: "source"})`
   opens it straight on its 来源 layer. A pending slice
   (grow's 等着写的) opens on its 原话 with `openSliceSource(sliceId)`: the same layer over
   GET /api/loci/grow/slices/{id}/source, 看原话 asking `?fetch=1`. Web: a window over
   the dimmed page. Phone: a card rising from the bottom.

   Three layers, one window (boards detail-*, source-*):
     the entry      GET /api/loci/bucket/{id} — `title`, 编辑, #short · date · the tag
                    row (tags_human), 摘要 (unless it is the title), 正文 verbatim,
                    V · A · #tags · subjects; 关联 N when it has any; 来源 →
     关联           GET /api/loci/lineage/{id}, unfolded under the entry: 后来去了哪？
                    (derived · covered_by · periods · new_version) and 路标 (the cue and
                    its phrasings, the live holds with their end day)
     来源           GET /api/loci/source/{id}, the window turns to it with a back arrow:
                    原话 per source (host · 日期 几点 – 几点 when its lines carry their
                    times, else the day · N 句; 「看原话」 only when `can_fetch`, which
                    asks `?fetch=<index>` and shows each line with who · time when given),
                    「从哪几条长出来的」 for what it stands on, 怎么知道的, 来源还成立吗

   编辑 offers what the entry's `edit` lists, through POST /api/loci/entry/fix:
     字写错了  the body in a box; the changed stretch is sent as {old, new}, widened until
              `old` occurs once in the body (the server replaces exactly one occurrence)
     内容错了  `content_fix` "new_version" (an event): the whole corrected body, a new
              version marked 人改的; "mark" (a MIND entry): a note, nothing rewritten, the
              entry carries 人说不对 until the model looks. Not offered while `disputed`.
     删除      a soft delete, set apart at the bottom of the menu
   A subject (the names in the entry's last row) opens its name card through the opener
   app.js gives `onSubject`; the window closes first, one window at a time.
   The server's `msg` is shown after a write; closing the window then re-renders the
   page under it (`loci:changed`).
   ========================================================== */

import * as api from "./api.js";
import { h, fill, clickable, richText, btn, errorLine, clock, dayTime } from "./ui.js";

const LABELS = { typo: "字写错了", content: "内容错了", delete: "删除" };

const ICON = {
  close: '<svg width="11" height="11" viewBox="0 0 11 11" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"><path d="M.5.5l10 10M10.5.5l-10 10"></path></svg>',
  back: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 6l-6 6 6 6"></path></svg>',
  down: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"></path></svg>',
  up: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 15l6-6 6 6"></path></svg>',
  arrow: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"></path></svg>',
  longArrow: '<svg width="24" height="10" viewBox="0 0 24 10" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" stroke-linejoin="round"><path d="M.5 5h23M19 .8 23.5 5 19 9.2"></path></svg>',
};

/** An icon from the fixed set above (constant markup, never data). */
function icon(name) {
  const span = h("span", { style: { display: "inline-flex" }, "aria-hidden": "true" });
  span.innerHTML = ICON[name];
  return span;
}

let current = null;   // the one open window
let subjectOpener = null;

/** What a click on one of the entry's subjects does: `fn(name)`, the name card's opener
 *  (app.js sets it). Without one the subjects are plain words. */
export function onSubject(fn) {
  subjectOpener = typeof fn === "function" ? fn : null;
}

/** Open the window on entry `id`. `layer: "source"` opens it on its 来源 layer. */
export function openDetail(id, { layer } = {}) {
  if (!id) return;
  if (!current) current = makeWindow();
  current.show(String(id), layer === "source" ? "source" : "entry");
}

/** Open the window on a pending slice's 原话 (grow's 等着写的). */
export function openSliceSource(sliceId) {
  if (!sliceId) return;
  if (!current) current = makeWindow();
  current.showSlice(String(sliceId));
}

/** Close the window if one is open. */
export function closeDetail() {
  if (current) current.close();
}

function makeWindow() {
  const before = document.activeElement;
  const dlg = h("div", { class: "dlg rising", role: "dialog", "aria-modal": "true", tabindex: "-1" });
  const scrim = h("div", { class: "scrim" }, dlg);
  const state = { id: "", entry: null, changed: false, notice: "", menu: null };
  const bodyOverflow = document.body.style.overflow;

  scrim.addEventListener("mousedown", (e) => { if (e.target === scrim) close(); });
  const onKey = (e) => {
    if (e.key !== "Escape") return;
    if (state.menu) closeMenu(); else close();
  };
  document.addEventListener("keydown", onKey);
  document.body.style.overflow = "hidden";
  document.body.appendChild(scrim);
  requestAnimationFrame(() => requestAnimationFrame(() => dlg.classList.remove("rising")));

  function close() {
    document.removeEventListener("keydown", onKey);
    document.body.style.overflow = bodyOverflow;
    scrim.remove();
    current = null;
    if (before && typeof before.focus === "function") before.focus();
    if (state.changed) document.dispatchEvent(new CustomEvent("loci:changed"));
  }

  function closeMenu() {
    if (state.menu) { state.menu.forEach((n) => n.remove()); state.menu = null; }
    const b = dlg.querySelector(".head .btn");
    if (b) b.setAttribute("aria-expanded", "false");
  }

  function closeBtn() {
    return h("button", { class: "ib close", type: "button", "aria-label": "关闭", on: { click: close } }, icon("close"));
  }

  /** The window's title: the server's `title`, the line every list shows for the entry. */
  function titleOf(b) {
    return b.title || `#${b.short}`;
  }

  async function show(id, layer) {
    closeMenu();
    state.id = id;
    try {
      state.entry = await api.get(`/api/loci/bucket/${api.seg(id)}`);
    } catch (e) {
      if (e.status === 401) { close(); return; }
      fill(dlg, h("div", { class: "grab", "aria-hidden": "true" }),
        h("div", { class: "head" }, h("h1", { text: `#${id.slice(0, 6)}` }), closeBtn()), errorLine(e));
      return;
    }
    if (layer === "source") await showSource(); else showEntry();
    dlg.focus({ preventScroll: true });
    scrim.scrollTop = 0;
  }

  // ------------------------------------------------------------ the entry

  function showEntry() {
    const b = state.entry;
    const title = h("h1", { id: "dt", text: titleOf(b) });
    dlg.setAttribute("aria-labelledby", "dt");
    const edits = (b.edit || []).filter((k) => LABELS[k]);
    const editBtn = edits.length
      ? h("button", { class: "btn", type: "button", "aria-expanded": "false", "aria-haspopup": "menu" }, "编辑")
      : null;
    if (editBtn) editBtn.addEventListener("click", () => (state.menu ? closeMenu() : openMenu(editBtn, edits)));

    const tags = (b.tags_human || []).map((t) => h("span", { text: t.text }));
    const facts = [];
    if (b.valence !== null && b.valence !== undefined && b.arousal !== null && b.arousal !== undefined) {
      facts.push(h("span", { text: `V ${round(b.valence)}　A ${round(b.arousal)}` }));
    }
    // An internal tag is stored framed in underscores (__档案事实__); it reads as #档案事实.
    for (const t of b.tags || []) facts.push(h("span", { text: `#${String(t).replace(/^_+|_+$/g, "")}` }));
    for (const s of b.subjects || []) {
      facts.push(subjectOpener
        ? clickable(h("a", { class: "nm", text: s }), () => { close(); subjectOpener(s); })
        : h("span", { text: s }));
    }

    const body = h("p", { class: "body" });
    richText(body, b.content || "");
    // The title is the summary when the entry has one; it is not said twice.
    const summary = b.summary && b.summary !== b.title ? b.summary : "";

    const related = (b.related ? (b.related.later || 0) + (b.related.signposts || 0) : 0);
    const rel = h("div");
    const relBtn = related
      ? h("button", { class: "morel", type: "button", "aria-expanded": "false" }, `关联 ${related} `, icon("down"))
      : h("span", { class: "why", text: "暂无关联" });
    if (related) relBtn.addEventListener("click", () => toggleRelated(relBtn, rel));
    const srcBtn = h("button", { class: "morel", type: "button" }, "来源", icon("longArrow"));
    srcBtn.addEventListener("click", () => showSource());

    fill(dlg,
      h("div", { class: "grab", "aria-hidden": "true" }),
      h("div", { class: "head" }, title, editBtn, closeBtn()),
      h("div", { class: "why meta" },
        h("span", { text: `#${b.short}` }), b.date ? h("span", { text: b.date }) : null, tags),
      state.notice ? h("p", { class: "why", role: "status", style: { margin: "14px 0 0" }, text: state.notice }) : null,
      summary ? h("p", { class: "cap sum", text: `摘要：${summary}` }) : null,
      h("div", { class: "entry-body" },
        h("p", { class: "cap", text: "正文" }), body),
      facts.length ? h("div", { class: "why facts" }, facts) : null,
      h("hr"),
      h("div", { class: "foot2" }, relBtn, srcBtn),
      rel);
    state.notice = "";
  }

  async function toggleRelated(button, box) {
    const open = button.getAttribute("aria-expanded") !== "true";
    button.setAttribute("aria-expanded", String(open));
    button.lastChild.replaceWith(icon(open ? "up" : "down"));
    if (!open) { fill(box); return; }
    let lin;
    try {
      lin = await api.get(`/api/loci/lineage/${api.seg(state.id)}`);
    } catch (e) {
      fill(box, errorLine(e));
      return;
    }
    const later = lin.later || {};
    const signs = lin.signposts || {};
    const rows = [];
    const entryRow = (label, item, text) => {
      const a = h("a", { class: "row" },
        h("span", { class: "why", text: item.state_words ? `${label} · ${item.state_words}` : label }),
        h("span", { class: "t", text: text ?? (item.text || `#${item.short}`) }));
      return clickable(a, () => show(item.id, "entry"));
    };
    for (const d of later.derived || []) rows.push(entryRow("衍生的认知", d));
    for (const c of later.covered_by || []) rows.push(entryRow("长出了什么？", c));
    for (const p of later.periods || []) {
      const span = String(p.span || "").replace("..", " – ");
      rows.push(entryRow("哪段时间的事情？", p, span ? `${span}　${p.text}` : p.text));
    }
    if (later.new_version) rows.push(entryRow("新版本", later.new_version));
    const marks = [];
    if (signs.cue) {
      const ph = (signs.cue.phrasings || []).join("、");
      marks.push(h("div", { class: "row" }, h("span", { class: "why", text: "反向路标" }),
        h("span", { class: "t" }, `「${signs.cue.condition}」`, ph ? h("span", { class: "why", text: `（${ph}）` }) : null)));
    }
    for (const hd of signs.holds || []) {
      const a = h("a", { class: "row" }, h("span", { class: "why", text: "时间限制" }),
        h("span", { class: "t" }, hd.text || `#${hd.short}`,
          h("span", { class: "why", text: hd.until ? `（${hd.until} 截止）` : `（${hd.words}）` })));
      marks.push(clickable(a, () => show(hd.id, "entry")));
    }
    fill(box,
      rows.length ? h("section", { style: { marginTop: "14px" } }, h("h2", { class: "h", text: "后来去了哪？" }), rows) : null,
      marks.length ? h("section", { style: { marginTop: rows.length ? "24px" : "14px" } }, h("h2", { class: "h", text: "路标" }), marks) : null);
  }

  // ------------------------------------------------------------ 编辑

  function openMenu(button, edits) {
    button.setAttribute("aria-expanded", "true");
    const item = (k) => h("button", { class: `mi${k === "delete" ? " danger" : ""}`, role: "menuitem", type: "button",
      on: { click: () => { closeMenu(); startEdit(k); } } }, LABELS[k]);
    const upper = edits.filter((k) => k !== "delete").map(item);
    const lower = edits.includes("delete") ? [upper.length ? h("hr") : null, item("delete")] : [];
    const menuScrim = h("div", { class: "menu-scrim", "aria-hidden": "true", on: { click: closeMenu } });
    const menu = h("div", { class: "menu", role: "menu", "aria-label": "编辑" },
      h("div", { class: "card" }, upper, lower),
      h("button", { class: "mi cancel", type: "button", on: { click: closeMenu } }, "取消"));
    dlg.append(menuScrim, menu);
    state.menu = [menuScrim, menu];
    const first = menu.querySelector(".mi");
    if (first) first.focus({ preventScroll: true });
  }

  function startEdit(kind) {
    const b = state.entry;
    if (kind === "delete") { write({ kind: "delete" }); return; }
    const slot = dlg.querySelector(".entry-body");
    if (!slot) return;
    const mark = kind === "content" && b.content_fix === "mark";
    const area = h("textarea", { class: "area", "aria-label": LABELS[kind] });
    area.value = mark ? "" : (b.content || "");
    const msg = h("div");
    const save = btn("保存", { dark: true });
    const cancel = btn("取消", { onClick: () => showEntry() });
    save.addEventListener("click", async () => {
      if (kind === "typo") {
        const pair = typoPair(b.content || "", area.value);
        if (!pair) { showEntry(); return; }
        await write({ kind: "typo", old: pair.old, new: pair.new }, msg, save);
      } else {
        await write({ kind: "content", text: area.value }, msg, save);
      }
    });
    fill(slot, h("div", { class: "editbox" },
      h("p", { class: "cap", style: { margin: "0" }, text: LABELS[kind] }), area, msg,
      h("div", { class: "acts" }, cancel, save)));
    area.focus();
  }

  async function write(fields, msgBox, button) {
    if (button) button.disabled = true;
    let out;
    try {
      out = await api.post("/api/loci/entry/fix", { id: state.id, ...fields });
    } catch (e) {
      if (button) button.disabled = false;
      if (e.status === 401) { close(); return; }
      if (msgBox) fill(msgBox, errorLine(e));
      else { state.notice = ""; showEntry(); dlg.querySelector(".foot2").before(errorLine(e)); }
      return;
    }
    state.changed = true;
    state.notice = out && out.msg ? out.msg : "";
    await show(out && out.new_id ? out.new_id : state.id, "entry");
  }

  // ------------------------------------------------------------ 来源

  async function showSource() {
    closeMenu();
    const b = state.entry;
    let src;
    const head = h("div", { class: "head", style: { gap: "4px" } },
      h("button", { class: "ib backarrow", type: "button", "aria-label": "返回这条记忆", on: { click: () => showEntry() } }, icon("back")),
      h("h1", { id: "st", text: "来源" }),
      closeBtn());
    dlg.setAttribute("aria-labelledby", "st");
    const subline = h("p", { class: "why sub", text: titleOf(b) });
    try {
      src = await api.get(`/api/loci/source/${api.seg(state.id)}`);
    } catch (e) {
      fill(dlg, h("div", { class: "grab", "aria-hidden": "true" }), head, subline, errorLine(e));
      return;
    }
    const parts = [];
    const originals = src.originals || [];
    if (originals.length) {
      parts.push(h("section", { class: "said" }, h("h2", { class: "h", text: "原话" }),
        originals.map((o) => originalBlock(o))));
    }
    if ((src.derived_from || []).length) {
      parts.push(h("section", { style: { marginTop: "24px" } }, h("h2", { class: "h", text: "从哪几条长出来的" }),
        h("div", { style: { borderTop: "1px solid var(--line)" } }, src.derived_from.map((d) => {
          const a = h("a", { class: "row" },
            d.state_words ? h("span", { class: "why", text: d.state_words }) : null,
            h("span", { class: "t", text: d.text || `#${d.short}` }));
          return clickable(a, () => show(d.id, "entry"));
        }))));
    }
    parts.push(
      h("section", { style: { marginTop: "26px" } }, h("h2", { class: "h", text: "怎么知道的" }),
        h("div", { class: "row ruled" }, h("span", { class: "t", text: src.how_known }))),
      h("section", { style: { marginTop: "18px" } }, h("h2", { class: "h", text: "来源还成立吗" }),
        h("div", { class: "row ruled" }, h("span", { class: "t", text: src.still_holds }))));
    fill(dlg, h("div", { class: "grab", "aria-hidden": "true" }), head, subline, parts);
    scrim.scrollTop = 0;
  }

  // ------------------------------------------------------------ a slice's 原话

  /** A pending slice's 原话, laid out as the 来源 layer: what it says under the title,
   *  its source (where it stands, 第 a–b 行, N 句, 看原话 when the lines can be asked
   *  for, asking `?fetch=1`), and whether the source still holds. A source withdrawn,
   *  deleted or held shows the API's words for that and nothing of the lines. */
  async function showSlice(sliceId) {
    closeMenu();
    state.id = "";
    const head = h("div", { class: "head", style: { gap: "4px" } }, h("h1", { id: "st", text: "来源" }), closeBtn());
    dlg.setAttribute("aria-labelledby", "st");
    let s;
    try {
      s = await api.get(`/api/loci/grow/slices/${api.seg(sliceId)}/source`);
    } catch (e) {
      if (e.status === 401) { close(); return; }
      fill(dlg, h("div", { class: "grab", "aria-hidden": "true" }), head, errorLine(e));
      return;
    }
    // No back arrow here, so the line under the title starts at the title's edge.
    const subline = h("p", { class: "why sub", style: { marginLeft: "0" },
      text: s.draft || s.gist || s.source_words || s.state_words || "" });
    const o = s.original;
    const parts = [];
    if (o) {
      const lines = s.span && s.span.from_line != null
        ? (s.span.from_line === s.span.to_line ? `第 ${s.span.from_line} 行` : `第 ${s.span.from_line}–${s.span.to_line} 行`)
        : null;
      const where = { ...o, host: s.label || o.host, lines };
      parts.push(h("section", { class: "said" }, h("h2", { class: "h", text: "原话" }),
        originalBlock(where, () => api.get(`/api/loci/grow/slices/${api.seg(sliceId)}/source`, { fetch: 1 }))),
      h("section", { style: { marginTop: "18px" } }, h("h2", { class: "h", text: "来源还成立吗" }),
        h("div", { class: "row ruled" }, h("span", { class: "t", text: o.state_words }))));
    } else {
      parts.push(h("p", { class: "why", style: { margin: "18px 0 0" }, text: s.state_words || "" }));
    }
    fill(dlg, h("div", { class: "grab", "aria-hidden": "true" }), head, subline, parts);
    dlg.focus({ preventScroll: true });
    scrim.scrollTop = 0;
  }

  /** One source: its facts, 看原话 when `o.can_fetch` (`ask()` reads the lines; the entry's
   *  own source route by default). */
  function originalBlock(o, ask) {
    const lines = h("div", { class: "lines" });
    const facts = [o.host, saidWhen(o), o.lines, o.span && o.span.count ? `${o.span.count} 句` : null,
      o.state !== "active" ? o.state_words : null].filter(Boolean).map((t) => h("span", { text: t }));
    const said = h("div");
    let fetchBtn = null;
    if (o.can_fetch) {
      fetchBtn = h("button", { class: "morel", type: "button" }, "看原话 ", icon("arrow"));
      fetchBtn.addEventListener("click", async () => {
        fetchBtn.disabled = true;
        try {
          const got = ask ? await ask() : await api.get(`/api/loci/source/${api.seg(state.id)}`, { fetch: o.index });
          fill(lines, (got.lines || []).map(lineRow));
          fill(said, got.outcome !== "given" || got.partial ? h("p", { class: "why", style: { margin: "0" }, text: got.outcome_words }) : null);
          if (got.outcome === "given" && !got.partial) fetchBtn.remove(); else fetchBtn.disabled = false;
        } catch (e) {
          fetchBtn.disabled = false;
          fill(said, errorLine(e));
        }
      });
    }
    return h("div", { style: { marginBottom: "8px" } }, lines,
      h("div", { class: "origin" }, h("span", { class: "why meta", style: { gap: "4px 20px" } }, facts), fetchBtn),
      said,
      o.can_fetch ? null : h("p", { class: "why", style: { margin: "0" }, text: "「看原话」只有宿主给得出原文才出现。" }));
  }

  /** 「日期 几点 – 几点」 when the source's lines carry their times (an import), else the
   *  day alone. */
  function saidWhen(o) {
    const span = o.span || {};
    const first = dayTime(span.first_at);
    const last = dayTime(span.last_at);
    if (!first) return o.at;
    if (!last || last === first) return first;
    return last.slice(0, 10) === first.slice(0, 10) ? `${first} – ${last.slice(11)}` : `${first} – ${last}`;
  }

  function lineRow(ln) {
    const who = [ln.who, clock(ln.at)].filter(Boolean).join(" · ");
    if (ln.missing) {
      return h("div", { class: "line" }, h("span", { class: "why", text: who }), h("span", { class: "why", text: ln.missing_words }));
    }
    return h("div", { class: "line" }, h("span", { class: "why", text: who }), h("span", { class: "t", text: ln.text || "" }));
  }

  return { show, showSlice, close };
}

function round(x) {
  const n = Number(x);
  return Number.isFinite(n) ? String(Math.round(n * 100) / 100) : String(x);
}

/** 字写错了: the stretch that changed between `before` and `after`, as {old, new}, widened
 *  on both sides until `old` is not empty and occurs exactly once in `before`. Null when
 *  nothing changed. */
export function typoPair(before, after) {
  if (before === after) return null;
  let p = 0;
  while (p < before.length && p < after.length && before[p] === after[p]) p += 1;
  let s = 0;
  while (s < before.length - p && s < after.length - p
    && before[before.length - 1 - s] === after[after.length - 1 - s]) s += 1;
  let a = p;
  let b = before.length - s;
  let c = after.length - s;
  const once = (needle) => needle && before.indexOf(needle) === before.lastIndexOf(needle);
  while (!once(before.slice(a, b)) && (a > 0 || b < before.length)) {
    if (a > 0) a -= 1;
    if (b < before.length) { b += 1; c += 1; }
  }
  return { old: before.slice(a, b), new: after.slice(a, c) };
}
