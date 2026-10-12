/* ==========================================================
   ui.js — the shared pieces every page is built from

   Built as DOM nodes, never as HTML strings: a memory's text is data and is only ever
   set as text. The class names are the design canvas's own (.grp / .gl / .subs / .st /
   .item / .txt / .c / .why / .rid / .tip / .pg / .btn …), so a board and the page it
   became can be read side by side; panel.css holds their values.

   The words on these pieces are the canvas's (翻页 ‹ ›, 还有 N 条, 自定义设置);
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

/** The header: the name and the ten pages between two hairlines. `nav` is
 *  [{id, label, href}]; `current` the id of the page shown. Web: the pages spread evenly
 *  on one row beside the name (sliding sideways when the window is too narrow). Phone:
 *  one row under the name that slides sideways, the current page scrolled into view. */
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

/** The row under the header. `tabs`: [{label, href, on}] (a page's sub-tabs, shown as a
 *  breadcrumb, breath / surface, the current one darker), or a node in their place (a
 *  line of words: its i sits close after it); `tip`: the i after them; `aside`: what sits
 *  at the right (a date line, a page's secondary entry) — under the tabs on the phone. */
export function subbar({ tabs, tip: tipText, aside } = {}) {
  const left = Array.isArray(tabs)
    ? h("div", { class: "nav tabs" }, tabs.map((t, i) => [
      i ? h("span", { class: "sep", "aria-hidden": "true", text: "/" }) : null,
      h("a", { href: t.href, class: t.on ? "on" : null, "aria-current": t.on ? "page" : null, text: t.label }),
    ]), tipText ? tip(tipText) : null)
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

/** A sub-block: its title (with an i) and its rows. Null when it has no rows. A page
 *  that numbers its blocks (01 档案, 02 原则 …) passes them through numberSubs. */
export function sub(title, { tip: tipText } = {}, ...children) {
  const kept = children.flat().filter(Boolean);
  if (!kept.length) return null;
  return h("div", null,
    title ? h("div", { class: "sth" }, h("h3", { class: "st", text: title }), tipText ? tip(tipText) : null) : null,
    kept);
}

/** Number the sub-blocks a page shows, in order, 01 02 …: each of `blocks` is a sub()
 *  (null when it is not shown, and then not counted). Returns the blocks. */
export function numberSubs(blocks) {
  let n = 0;
  for (const b of blocks) {
    const st = b && b.querySelector(".sth > .st");
    if (st) st.prepend(h("span", { class: "sn", text: String(++n).padStart(2, "0") }));
  }
  return blocks;
}

/** One row. `text` the line (cut to one line; `whole: true` shows it all); `why` the
 *  small words under it (a string, or a list shown side by side); `open` what clicking
 *  the line does (usually the detail window); `right` what sits at its right — under it
 *  on the phone (a date, buttons), except a "start" row's; `layout` "start" (breath and
 *  surface: the id, as wide as it is, beside it on the phone too), "wide" (300px of
 *  buttons) or "" (170px). */
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

/** A row's id at its right: the handle recall takes (`short`: a 12-hex id's first six,
 *  a readable id whole), lowercase as it is stored, because recall matches it as
 *  written. The # in front is drawn by panel.css, so a copy carries the id alone. */
export function idTag(short) {
  return short ? h("span", { class: "rid", text: short }) : h("span");
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

/** How many rows a paged list shows at a time, unless the page asks otherwise. */
export const PAGE = 15;

/** A list read one page at a time (`limit` to a page, PAGE unless the page says so),
 *  with ‹ 1 / N › under it.
 *
 *  `load(query)` reads one page: query is {offset, limit, as_of}; it returns the reply
 *  {items, total, offset, limit, next_offset, as_of}. The first page's `as_of` is sent
 *  back on every later page, so rows written meanwhile do not shift the pages.
 *  `item(row)` turns one item into its row node; `items(list)`, given instead, turns a
 *  whole page at once (a page that groups its rows, by day or by batch).
 *
 *  Resolves to {el, total, reply, reload(), again()} once the first page is in; it rejects
 *  when the first page cannot be read, so the page decides what to show. With one page
 *  only, the ‹ › line is left out. `nav` is that line: a page may move it elsewhere (surface
 *  puts it at the page's foot) and it keeps working. `again()` reads the page shown once more, as of now —
 *  after a write took a row off it (one page back when that one is now empty). */
export async function pagedList({ load, item, items, limit = PAGE }) {
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
    nav,
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

/** Put a paged list's ‹ › line at the foot of the page, for a page that is one long
 *  list (surface, grow, recall, regrow/fold, name): `view` is the page's outlet, `list`
 *  what pagedList resolved to. Called after the list is on the page. */
export function footPager(view, list) {
  view.classList.add("fill");
  list.nav.classList.add("end");
  view.append(list.nav);
}

// ---------------------------------------------------------------- controls

/** A pill button. `dark` is the main action (black, white words); `quiet` a greyed one;
 *  `badge` her small glass pill (a prompt card's 恢复默认 · 保存). */
export function btn(label, { dark = false, quiet = false, badge = false, onClick, type = "button" } = {}) {
  return h("button", {
    class: `btn${badge ? " badge" : ""}${dark ? " dark" : ""}${quiet ? " quiet" : ""}`, type,
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

let pickSeq = 0;

/** A button that opens her glass list under it (frame glass-card-example) — in the place
 *  of a native select. `options`: [[value, words]]; an option whose value is "" clears
 *  the choice: it reads —— in the list. While nothing is chosen the button shows
 *  `placeholder` in small ink, or nothing. `cls` is the button's class (recall's pills,
 *  setting's model box), `label` names it for a screen reader, `onChange(value)` is
 *  called when another option is chosen. One row of the list is lit at a time: the chosen
 *  one when it opens, then the one under the pointer or the arrow keys. Enter chooses,
 *  Esc and a click elsewhere close. The node returned also has `getValue()` and
 *  `setOptions(options, value)` (a list that arrives later). */
export function pick({ cls = "pill", label, options = [], value = "", onChange, disabled = false, placeholder = "" }) {
  const id = `pick${++pickSeq}`;
  const button = h("button", { class: cls, type: "button", "aria-haspopup": "listbox",
    "aria-expanded": "false", "aria-controls": id, "aria-label": label, disabled });
  const list = h("div", { class: "pop", id, role: "listbox", "aria-label": label, hidden: true });
  const wrap = h("span", { class: "pickw" }, button, list);
  let rows = [];
  let lit = -1;

  const show = () => {
    const found = value === "" ? null : options.find(([v]) => v === value);
    fill(button, found ? found[1] : (placeholder ? h("span", { class: "ph", text: placeholder }) : null));
  };
  const light = (i) => {
    lit = i;
    rows.forEach((el, n) => el.classList.toggle("on", n === i));
    if (i >= 0) {
      button.setAttribute("aria-activedescendant", rows[i].id);
      rows[i].scrollIntoView({ block: "nearest" });
    } else {
      button.removeAttribute("aria-activedescendant");
    }
  };
  const outside = (e) => { if (!wrap.contains(e.target)) close(); };
  function open() {
    if (!rows.length) return;
    list.hidden = false;
    button.setAttribute("aria-expanded", "true");
    light(options.findIndex(([v]) => v === value && v !== ""));
    document.addEventListener("pointerdown", outside, true);
  }
  function close() {
    list.hidden = true;
    button.setAttribute("aria-expanded", "false");
    button.removeAttribute("aria-activedescendant");
    document.removeEventListener("pointerdown", outside, true);
  }
  function choose(i) {
    const [v] = options[i];
    close();
    if (v === value) return;
    value = v;
    show();
    rows.forEach((el, n) => el.setAttribute("aria-selected", String(n === i)));
    if (onChange) onChange(v);
  }
  function setOptions(next, now = value) {
    close();
    options = next || [];
    value = now;
    rows = options.map(([v, words], i) => {
      const el = h("div", { class: "po", role: "option", id: `${id}-${i}`, "aria-selected": String(v === value) },
        v === "" ? "——" : words);
      el.addEventListener("pointermove", () => { if (lit !== i) light(i); });
      el.addEventListener("click", () => choose(i));
      return el;
    });
    fill(list, rows);
    show();
  }
  setOptions(options);

  button.addEventListener("click", () => (list.hidden ? open() : close()));
  button.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (list.hidden) { open(); if (lit < 0 && rows.length) light(0); return; }
      const step = e.key === "ArrowDown" ? 1 : -1;
      light(lit < 0 ? 0 : (lit + step + rows.length) % rows.length);
    } else if ((e.key === "Enter" || e.key === " ") && !list.hidden && lit >= 0) {
      e.preventDefault();
      choose(lit);
    } else if (e.key === "Escape" && !list.hidden) {
      e.preventDefault();
      close();
    } else if (e.key === "Tab") {
      close();
    }
  });
  wrap.getValue = () => value;
  wrap.setOptions = setOptions;
  return wrap;
}

// A wheel row's height, as panel.css draws it (.wo).
const WHEEL_ROW = 32;
const WHEEL_FROM = 1970;

/** A date as three pills — yyyy, mm, dd — that open one glass card of three wheels under
 *  them: turn a wheel (or click a row, or the arrow keys) and the row in the middle is
 *  the one taken. —— on a wheel leaves that part out, and what is right of it with it: a
 *  year alone, a year and a month, or a whole day. Closing the card (a click elsewhere,
 *  Enter) takes what the wheels show; Esc leaves it as it was. `value`: "2026", "2026-10",
 *  "2026-10-07" or ""; `onChange(value)` when it changed. The years run from five ahead
 *  down to 1970, —— between next year and this one. */
export function whenPick({ value = "", onChange }) {
  const pad = (n) => String(n).padStart(2, "0");
  const thisYear = new Date().getFullYear();
  const parts = (v) => { const [y = "", m = "", d = ""] = String(v || "").split("-"); return { y, m: y ? m : "", d: y && m ? d : "" }; };
  const years = [];
  for (let y = thisYear + 5; y > thisYear; y--) years.push(String(y));
  years.push("");
  for (let y = thisYear; y >= WHEEL_FROM; y--) years.push(String(y));
  const first = parts(value);
  if (first.y && !years.includes(first.y)) years.push(first.y);
  const months = ["", ...Array.from({ length: 12 }, (_, i) => pad(i + 1))];
  const daysOf = (y, m) => ["", ...Array.from({ length: y && m ? new Date(Number(y), Number(m), 0).getDate() : 31 }, (_, i) => pad(i + 1))];

  const pills = {
    y: h("button", { class: "pill yyyy", type: "button", "aria-label": "年", "aria-haspopup": "dialog" }),
    m: h("button", { class: "pill mm", type: "button", "aria-label": "月", "aria-haspopup": "dialog" }),
    d: h("button", { class: "pill dd", type: "button", "aria-label": "日", "aria-haspopup": "dialog" }),
  };
  const HINT = { y: "yyyy", m: "mm", d: "dd" };
  const card = h("div", { class: "pop wheels", role: "dialog", "aria-label": "选日期", hidden: true });
  const wrap = h("span", { class: "pickw whenw" }, pills.y, pills.m, pills.d, card);

  function wheel(key, name, values, current) {
    const el = h("div", { class: `wheel ${key}`, role: "listbox", tabindex: "0", "aria-label": name });
    const w = { el, values: [], lit: 0 };
    w.light = (i) => {
      w.lit = i;
      [...el.children].forEach((r, n) => { r.classList.toggle("on", n === i); r.setAttribute("aria-selected", String(n === i)); });
      turned(w);
    };
    w.to = (i, smooth = false) => {
      i = Math.max(0, Math.min(w.values.length - 1, i));
      el.scrollTo({ top: i * WHEEL_ROW, behavior: smooth ? "smooth" : "auto" });
      if (!smooth) w.light(i);
    };
    w.set = (vals, cur) => {
      w.values = vals;
      fill(el, vals.map((v, i) => {
        const r = h("div", { class: "wo", role: "option" }, v === "" ? "——" : v);
        r.addEventListener("click", () => w.to(i, true));
        return r;
      }));
      w.to(Math.max(0, vals.indexOf(cur)));
    };
    w.value = () => w.values[w.lit] || "";
    el.addEventListener("scroll", () => {
      const i = Math.round(el.scrollTop / WHEEL_ROW);
      if (i !== w.lit && i >= 0 && i < w.values.length) w.light(i);
    });
    el.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); w.to(w.lit + (e.key === "ArrowDown" ? 1 : -1), true); }
    });
    w.set(values, current);
    return w;
  }

  let wheels = null;
  // A wheel turned: the days follow the month, and a wheel right of a —— is dimmed.
  function turned(w) {
    if (!wheels) return;
    const y = wheels.y.value();
    const m = y ? wheels.m.value() : "";
    if (w !== wheels.d) {
      const days = daysOf(y, m);
      if (days.length !== wheels.d.values.length) wheels.d.set(days, days[Math.min(wheels.d.lit, days.length - 1)]);
    }
    wheels.m.el.classList.toggle("dim", !y);
    wheels.d.el.classList.toggle("dim", !m);
  }
  const taken = () => {
    const y = wheels.y.value();
    const m = y ? wheels.m.value() : "";
    const d = m ? wheels.d.value() : "";
    return [y, m, d].filter(Boolean).join("-");
  };
  const show = () => {
    const now = parts(value);
    for (const k of ["y", "m", "d"]) fill(pills[k], now[k] || h("span", { class: "ph", text: HINT[k] }));
  };
  const outside = (e) => { if (!wrap.contains(e.target)) close(true); };
  function open(key) {
    card.hidden = false;
    const now = parts(value);
    wheels.y.to(Math.max(0, wheels.y.values.indexOf(now.y)));
    wheels.m.to(Math.max(0, wheels.m.values.indexOf(now.m)));
    wheels.d.set(daysOf(now.y, now.m), now.d);
    for (const k of ["y", "m", "d"]) pills[k].setAttribute("aria-expanded", "true");
    document.addEventListener("pointerdown", outside, true);
    wheels[key].el.focus({ preventScroll: true });
  }
  function close(take) {
    if (card.hidden) return;
    const next = take ? taken() : value;
    card.hidden = true;
    for (const k of ["y", "m", "d"]) pills[k].setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", outside, true);
    if (next === value) return;
    value = next;
    show();
    if (onChange) onChange(value);
  }

  wheels = {
    y: wheel("y", "年", years, first.y),
    m: wheel("m", "月", months, first.m),
    d: wheel("d", "日", daysOf(first.y, first.m), first.d),
  };
  append(card, [wheels.y.el, wheels.m.el, wheels.d.el]);
  turned(wheels.y);
  show();
  for (const k of ["y", "m", "d"]) {
    pills[k].setAttribute("aria-expanded", "false");
    pills[k].addEventListener("click", () => (card.hidden ? open(k) : close(true)));
  }
  card.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); close(true); pills.y.focus(); }
    else if (e.key === "Escape") { e.preventDefault(); close(false); pills.y.focus(); }
    else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      const order = ["y", "m", "d"];
      const at = order.findIndex((k) => wheels[k].el === document.activeElement);
      const to = order[at + (e.key === "ArrowRight" ? 1 : -1)];
      if (to) { e.preventDefault(); wheels[to].el.focus({ preventScroll: true }); }
    }
  });
  return wrap;
}

/** A prompt card (grow's 设置, dream's and present's 高级设置; her frame grow——设置): its
 *  title — `no`, a page's 01 02 …, in front of it, 「改过 <day>」 after it when it was
 *  rewritten — the prompt in a sunken box the full width, and under the box the server's
 *  不建议改 lines as they come with 恢复默认 · 保存 at their right (under them on the
 *  phone). `card` is one item of GET /api/loci/prompts: {key, text, default, edited,
 *  changed, changed_at, notes[]}; `title` and `label` the board's words for it,
 *  `changedWords(day)` its 改过 line. `onSave(text)` and `onReset()` write; while one runs
 *  both buttons wait, and a failure shows the server's words under them. `extra` goes
 *  after the buttons (dream's 样文, grow's 切片结果样例). `off`: the card of something
 *  not connected — the box and both buttons are there and do nothing. */
export function promptCard({ title, no, card, label, onSave, onReset, changedWords = (day) => `改过 ${day}`, extra, off = false }) {
  const id = `prompt-${card.key}`;
  const area = h("textarea", { class: "prompt", id, spellcheck: "false", disabled: off });
  area.value = card.text ?? "";
  const status = h("div");
  const changed = card.edited && card.changed ? changedWords(card.changed) : "";
  const reset = btn("恢复默认", { badge: true });
  const keep = btn("保存", { badge: true, dark: true });
  reset.disabled = keep.disabled = off;
  const run = (fn) => async () => {
    fill(status);
    reset.disabled = keep.disabled = true;
    try { await fn(); } catch (e) { fill(status, errorLine(e)); }
    reset.disabled = keep.disabled = false;
  };
  if (!off) {
    reset.addEventListener("click", run(() => onReset()));
    keep.addEventListener("click", run(() => onSave(area.value)));
  }
  return h("div", { class: "pcard" },
    h("div", { class: "phead" },
      h("h3", { class: "pt" }, no ? h("span", { class: "sn", text: no }) : null, title),
      changed ? h("span", { class: "why", text: changed }) : null),
    h("label", { for: id, class: "sr", text: label || title }),
    area,
    h("div", { class: "pfoot" },
      h("div", { class: "pnotes" }, (card.notes || []).map((n) => h("p", { class: "why", text: n }))),
      h("div", { class: "acts" }, reset, keep)),
    status,
    extra || null);
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
