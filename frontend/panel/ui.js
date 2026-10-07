/* ==========================================================
   ui.js — the shared pieces every page is built from

   Built as DOM nodes, never as HTML strings: a memory's text is data and is only ever
   set as text. The class names are the design canvas's own (.grp / .gl / .subs / .st /
   .item / .txt / .c / .why / .src / .tip / .pg / .btn …), so a board and the page it
   became can be read side by side; panel.css holds their values.

   The words on these pieces are the canvas's (翻页 ‹ ›, 来源, 还有 N 条, 自定义设置);
   a page passes in everything else, from its board or from the API's *_words.
   ========================================================== */

/** The AI's name, as the server filled it into the page (web/loci_pages.loci_page). */
export const AI_NAME = document.documentElement.dataset.aiName || "AI";

// ---------------------------------------------------------------- DOM

/** h("div", {class: "x", on: {click: fn}}, child, "text", [more]) -> Element.
 *  Attributes: `class`, `text`, `on` (event handlers), `style` (an object or a string),
 *  booleans set or leave out an attribute; anything else is set as an attribute. */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = String(v);
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === undefined || c === null || c === false || c === "") continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** Empty a node and fill it again. */
export function fill(el, ...children) {
  el.replaceChildren();
  return append(el, children);
}

/** Make a non-button element act as one: click, Enter and Space, focusable. */
export function clickable(el, fn) {
  el.setAttribute("role", "button");
  el.tabIndex = 0;
  el.addEventListener("click", (e) => { e.preventDefault(); fn(e); });
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fn(e); }
  });
  return el;
}

/** Text with **bold** shown bold — the bodies are markdown the model wrote. Built as
 *  nodes, so nothing in the text is ever read as HTML. */
export function richText(el, text) {
  for (const part of String(text ?? "").split(/(\*\*[^*\n]+\*\*)/g)) {
    if (!part) continue;
    if (part.length > 4 && part.startsWith("**") && part.endsWith("**")) {
      el.appendChild(h("strong", { text: part.slice(2, -2) }));
    } else {
      el.appendChild(document.createTextNode(part));
    }
  }
  return el;
}

// ---------------------------------------------------------------- times

const pad = (n) => String(n).padStart(2, "0");

function parse(iso) {
  const d = iso ? new Date(iso) : null;
  return d && !Number.isNaN(d.getTime()) ? d : null;
}

/** "2026-10-07 08:02" from a local ISO stamp (its own wall-clock time, as written). */
export function dayTime(iso) {
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})/.exec(String(iso || ""));
  if (m) return `${m[1]} ${m[2]}:${m[3]}`;
  const d = parse(iso);
  return d ? `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}` : "";
}

/** "08:02" from a local ISO stamp. */
export function clock(iso) {
  const m = /T(\d{2}):(\d{2})/.exec(String(iso || ""));
  return m ? `${m[1]}:${m[2]}` : "";
}

// ---------------------------------------------------------------- header and tabs

/** The header: the big name and the ten pages. `nav` is [{id, label, href}]; `current`
 *  the id of the page shown. Web: the pages wrap beside the name. Phone: one row under it
 *  that scrolls sideways, the current page scrolled into view. */
export function header(nav, current) {
  const links = nav.map((p) => h("a", {
    href: p.href, class: p.id === current ? "on" : null,
    "aria-current": p.id === current ? "page" : null, text: p.label,
  }));
  const bar = h("nav", { class: "nav topnav", "aria-label": "页面" }, links);
  let tries = 0;
  const reveal = () => {
    // The header is built before it is put on the page; wait until it is there.
    if (!bar.isConnected) { if (++tries < 60) requestAnimationFrame(reveal); return; }
    const on = bar.querySelector("a.on");
    if (!on || bar.scrollWidth <= bar.clientWidth) return;
    const left = on.getBoundingClientRect().left - bar.getBoundingClientRect().left + bar.scrollLeft;
    bar.scrollLeft = Math.max(0, left - (bar.clientWidth - on.offsetWidth) / 2);
  };
  requestAnimationFrame(reveal);
  return h("header", { class: "mast" }, h("div", { class: "brand" }, "Loci brain"), bar);
}

/** The row under the header. `tabs`: [{label, href, on}] (a page's sub-tabs, the current
 *  one underlined), or a node in their place (a line of words: its i sits close after it);
 *  `tip`: the i after them; `aside`: what sits
 *  at the right (a date line, a page's secondary entry) — under the tabs on the phone. */
export function subbar({ tabs, tip: tipText, aside } = {}) {
  const left = Array.isArray(tabs)
    ? h("div", { class: "nav tabs" }, tabs.map((t) => h("a", {
      href: t.href, class: t.on ? "on" : null, "aria-current": t.on ? "page" : null, text: t.label,
    })), tipText ? tip(tipText) : null)
    : h("div", { class: `nav tabs${tipText ? " line" : ""}` }, tabs || null, tipText ? tip(tipText) : null);
  return h("div", { class: "subbar" }, left, aside || null);
}

// ---------------------------------------------------------------- the i

let tipSeq = 0;

/** The i with its bubble, shown on hover and on focus (a tap on the phone). The bubble is
 *  moved left when it would run past the window's right edge. */
export function tip(text) {
  const id = `tip${++tipSeq}`;
  const bubble = h("span", { class: "bubble", id, role: "tooltip", text });
  const wrap = h("span", { class: "tipw" },
    h("span", { class: "tip", tabindex: "0", "aria-describedby": id }, "i"), bubble);
  const place = () => {
    bubble.style.left = "0px";
    requestAnimationFrame(() => {
      const r = bubble.getBoundingClientRect();
      const room = document.documentElement.clientWidth - 16;
      if (r.right > room) bubble.style.left = `${Math.min(0, room - r.right)}px`;
    });
  };
  wrap.addEventListener("mouseenter", place);
  wrap.addEventListener("focusin", place);
  return wrap;
}

// ---------------------------------------------------------------- groups and rows

/** A group: its label on the left (above it on the phone), its sub-blocks on the right.
 *  Returns null when every sub-block is null — an empty block does not appear at all. */
export function group(label, { tip: tipText } = {}, ...subs) {
  const kept = subs.flat().filter(Boolean);
  if (!kept.length) return null;
  return h("section", { class: "grp" },
    h("div", { class: "glw" }, h("h2", { class: "gl", text: label }), tipText ? tip(tipText) : null),
    h("div", { class: "subs" }, kept));
}

/** A sub-block: its title (with an i) and its rows. Null when it has no rows. */
export function sub(title, { tip: tipText } = {}, ...children) {
  const kept = children.flat().filter(Boolean);
  if (!kept.length) return null;
  return h("div", null,
    title ? h("div", { class: "sth" }, h("h3", { class: "st", text: title }), tipText ? tip(tipText) : null) : null,
    kept);
}

/** One row. `text` the line (cut to one line; `whole: true` shows it all); `why` the
 *  small words under it (a string, or a list shown side by side); `open` what clicking
 *  the line does (usually the detail window); `right` what sits at its right — under it
 *  on the phone (a 来源 link, a date, buttons); `layout` "start" (breath and surface:
 *  140px, aligned to the top), "wide" (300px of buttons) or "" (170px, centred). */
export function row({ text, why, open, right, layout = "", whole = false, lead }) {
  const whys = (Array.isArray(why) ? why : [why]).filter((w) => w !== undefined && w !== null && w !== "");
  const line = h("span", { class: whole ? "c2" : "c" });
  if (lead) append(line, [lead]);
  if (whole) richText(line, text ?? ""); else line.append(String(text ?? ""));
  const txt = h(open ? "a" : "div", { class: "txt" }, line,
    whys.length ? h("span", { class: "why meta" }, whys.map((w) => (w instanceof Node ? w : h("span", { text: w })))) : null);
  if (open) clickable(txt, open);
  return h("div", { class: `item${layout ? ` ${layout}` : ""}` }, txt, right || h("span"));
}

/** 「来源 a1b2c3」 at a row's right, 「来源 a1b2c3 ›」 under it on the phone. */
export function srcLink(short, open) {
  const a = h("a", { class: "src" }, `来源 ${short}`, h("span", { class: "only-phone", text: " ›" }));
  return clickable(a, open);
}

/** 「还有 N 条」 under a list cut short. */
export function moreLine(n) {
  return n > 0 ? h("p", { class: "more", text: `还有 ${n} 条` }) : null;
}

/** A line of small words (a page's empty state, a note). */
export function note(text, cls = "why") {
  return text ? h("p", { class: cls, style: { margin: "0" }, text }) : null;
}

/** What went wrong, in the server's words. */
export function errorLine(err) {
  return h("p", { class: "err", role: "alert", style: { margin: "0" }, text: err && err.message ? err.message : String(err) });
}

// ---------------------------------------------------------------- paging

/** A list read one page at a time (5 to a page), with ‹ 1 / N › under it.
 *
 *  `load(query)` reads one page: query is {offset, limit, as_of}; it returns the reply
 *  {items, total, offset, limit, next_offset, as_of}. The first page's `as_of` is sent
 *  back on every later page, so rows written meanwhile do not shift the pages.
 *  `item(row)` turns one item into its row node; `items(list)`, given instead, turns a
 *  whole page at once (a page that groups its rows, by day or by batch).
 *
 *  Resolves to {el, total, reply, reload(), again()} once the first page is in; it rejects
 *  when the first page cannot be read, so the page decides what to show. With one page
 *  only, the ‹ › line is left out. `again()` reads the page shown once more, as of now —
 *  after a write took a row off it (one page back when that one is now empty). */
export async function pagedList({ load, item, items, limit = 5 }) {
  const rows = h("div", { class: "rows" });
  const status = h("span", { class: "why" });
  const prev = h("button", { class: "pg", type: "button", "aria-label": "上一页" }, "‹");
  const next = h("button", { class: "pg", type: "button", "aria-label": "下一页" }, "›");
  const nav = h("nav", { class: "pager", "aria-label": "翻页" }, prev, status, next);
  const err = h("div");
  const el = h("div", null, rows, err, nav);
  const state = { asOf: null, offset: 0, reply: null };

  async function go(offset) {
    const query = { offset, limit };
    if (state.asOf) query.as_of = state.asOf;
    const reply = await load(query);
    if (!state.asOf) state.asOf = reply.as_of || null;
    state.offset = offset;
    state.reply = reply;
    fill(rows, items ? items(reply.items || []) : (reply.items || []).map(item));
    const pages = Math.max(1, Math.ceil((reply.total || 0) / limit));
    const here = Math.floor(offset / limit) + 1;
    status.textContent = `${here} / ${pages}`;
    prev.disabled = offset <= 0;
    next.disabled = reply.next_offset === null || reply.next_offset === undefined;
    nav.hidden = pages <= 1 && offset === 0;
    fill(err);
    return reply;
  }
  const turn = (offset) => go(offset).catch((e) => fill(err, errorLine(e)));
  prev.addEventListener("click", () => turn(Math.max(0, state.offset - limit)));
  next.addEventListener("click", () => { if (state.reply && state.reply.next_offset != null) turn(state.reply.next_offset); });

  const first = await go(0);
  return {
    el,
    total: first.total || 0,
    reply: first,
    reload: () => { state.asOf = null; return go(0); },
    again: async () => {
      state.asOf = null;
      const reply = await go(state.offset);
      if (!(reply.items || []).length && state.offset > 0) return go(Math.max(0, state.offset - limit));
      return reply;
    },
  };
}

// ---------------------------------------------------------------- controls

/** A pill button. `dark` is the main action (black, white words); `quiet` a greyed one. */
export function btn(label, { dark = false, quiet = false, onClick, type = "button" } = {}) {
  return h("button", {
    class: `btn${dark ? " dark" : ""}${quiet ? " quiet" : ""}`, type,
    on: onClick ? { click: onClick } : null,
  }, label);
}

/** A switch: graphite when on. `onChange(checked)`. */
export function sw({ checked = false, label, onChange } = {}) {
  const el = h("button", { class: "sw", type: "button", role: "switch", "aria-checked": String(!!checked), "aria-label": label }, h("span"));
  el.addEventListener("click", () => {
    const on = el.getAttribute("aria-checked") !== "true";
    el.setAttribute("aria-checked", String(on));
    if (onChange) onChange(on);
  });
  return el;
}

/** One radio choice with its words. Extra nodes (a number box) go after the words. */
export function radio({ name, label, checked = false, value, onChange }, ...extra) {
  const input = h("input", { type: "radio", name, value: value ?? label, checked });
  if (onChange) input.addEventListener("change", () => input.checked && onChange(input.value));
  return h("label", { class: "opt" }, input, ` ${label}`, extra);
}

/** An underlined text box. */
export function inp(attrs = {}) {
  return h("input", { class: "inp", ...attrs });
}

/** A small pill number box. */
export function num(attrs = {}) {
  return h("input", { class: "num", inputmode: "decimal", ...attrs });
}

/** A prompt card (grow, dream, present's 高级设置): its title, 「改过 <day>」 when it was
 *  changed, the prompt in a box the full width, what is better left alone and why, and
 *  恢复默认 · 保存 under the box. `card` is one of GET /api/loci/prompts:
 *  {key, title, text, changed, notes[]}; `title` the board's words for it. `onSave(text)`
 *  and `onReset()` write and resolve to the server's reply; a failure shows its words. */
export function promptCard({ title, card, label, onSave, onReset }) {
  const id = `prompt-${card.key}`;
  const area = h("textarea", { class: "prompt", id, rows: "11" });
  area.value = card.text ?? "";
  const status = h("div");
  const changed = typeof card.changed === "string" && card.changed ? `改过 ${card.changed}` : "";
  const notes = (card.notes || []).map((n) => {
    const words = typeof n === "string" ? n : [n.part, n.why].filter(Boolean).join(" —— ");
    return words ? h("p", { class: "why", style: { margin: "12px 0 0" }, text: `不建议改：${words}` }) : null;
  });
  const run = (fn) => async () => {
    fill(status);
    try { await fn(); } catch (e) { fill(status, errorLine(e)); }
  };
  return h("div", null,
    h("h3", { class: "st", style: { marginBottom: "6px" }, text: title }),
    changed ? h("p", { class: "why", style: { margin: "0 0 12px" }, text: changed }) : null,
    h("label", { for: id, class: "sr", text: label || title }),
    area, notes,
    h("div", { class: "acts", style: { marginTop: "20px" } },
      btn("恢复默认", { onClick: run(() => onReset()) }),
      btn("保存", { dark: true, onClick: run(() => onSave(area.value)) })),
    status);
}

/** 「自定义设置」: folded, only its link; open, a grey box with the link on top. */
export function customBox(content, { open = false } = {}) {
  const body = h("div", null, content);
  const toggle = h("button", { class: "lnk", type: "button", style: { fontSize: "14px" } });
  const box = h("div", null, toggle, body);
  const set = (on) => {
    toggle.textContent = on ? "自定义设置 ▴" : "自定义设置 ▾";
    toggle.setAttribute("aria-expanded", String(on));
    body.hidden = !on;
    box.className = on ? "panel" : "";
    box.style.padding = on ? "" : "0 0 12px";
  };
  toggle.addEventListener("click", () => set(body.hidden));
  set(open);
  return box;
}
