/* ==========================================================
   presentform.js — the gateway's settings as bound controls

   What pages/present.js and pages/wakeup.js share. Both read GET /api/loci/present
   (Loci forwards to the gateway, web/loci_present.py): `settings` fills the controls,
   `status` the lines beside them. A control is saved the moment it changes (a box on
   Enter or on leaving it, a switch or a choice on the click) with POST
   /api/loci/present {patch}; a refusal names its field, which is marked, and the
   server's words show in that row.

   No host with a present_url: `{connected: false}`, the page says 「还没接上」 and every
   control is drawn off. A gateway that cannot be reached: `{connected: false, error}`,
   the error shown, the controls still off.
   ========================================================== */

import * as api from "./api.js";
import { h, fill, row, sw, radio, num, promptCard, errorLine, dayTime } from "./ui.js";

export const NOT_WIRED = "还没接上";

export const PRESENT = "/api/loci/present";

// A refused field, in the board's words (the longest matching prefix wins).
export const FIELD_WORDS = {
  "compress.on": "自助压缩开关",
  "compress.context_tokens": "上下文窗口",
  "compress.ask_pct": "压缩水位线",
  "compress.keep_raw": "压缩完留多少条原话",
  "compress.weak_pct": "弱提醒线",
  "compress.force_pct": "强制压缩线",
  "report.from": "什么时候写（从）",
  "report.to": "什么时候写（到）",
  "report.flip": "换窗",
  "report.every_n": "几天换一个窗",
  "wake.on": "自动唤醒开关",
  "wake.every_min": "多久醒一次",
  "wake.dnd": "免打扰",
  "wake.segments": "时间段",
  "push.bark": "Bark 推送码",
};

// ---------------------------------------------------------------- small pieces

export function off(el) {
  if (el.matches("input, button, textarea, select")) el.disabled = true;
  for (const c of el.querySelectorAll("input, button, textarea, select")) c.disabled = true;
  return el;
}

export function acts(...kids) {
  return h("span", { class: "acts", style: { flexWrap: "nowrap" } }, kids);
}

/** One row of a numbered block (ui.setBlock): the name and its small words on the left,
 *  the control at the right. */
export function setRow({ text = "", lead, why, right, layout = "" }) {
  return row({ text, lead, why, right: right || h("span"), layout: `big set${layout ? ` ${layout}` : ""}` });
}

/** A number's unit, after its box (分钟). */
export function unit(text) {
  return h("span", { class: "why", text });
}

export function to() {
  return h("span", { class: "why", text: "到" });
}

export function small(text, style) {
  return h("p", { class: "why", style: { margin: "8px 0 4px", lineHeight: "1.6", ...(style || {}) }, text });
}

export function linkButton(words) {
  return h("button", { class: "lnk", type: "button", style: { fontSize: "14px" } }, words);
}

/** "10-07 15:02" from a local ISO stamp. */
export function when(iso) {
  return dayTime(iso).slice(5);
}

/** {a: {b: v}} from ["a", "b"] and v. */
export function nest(path, value) {
  return path.reduceRight((inner, key) => ({ [key]: inner }), value);
}

export function at(obj, path) {
  return path.reduce((o, k) => (o === null || o === undefined ? undefined : o[k]), obj);
}

/** A whole number as a number; anything else as typed, so the gateway says what is wrong. */
export function intOr(raw) {
  const v = Number(raw);
  return raw !== "" && Number.isFinite(v) ? v : raw;
}

/** A percent box shows "85%"; what is typed is read with or without the sign. */
export const pctShow = (v) => (v === null || v === undefined || v === "" ? "" : `${v}%`);
export const pctRead = (raw) => intOr(raw.replace(/\s*[%％]\s*$/, ""));

/** "4" · "4:30" · "4.30" · "0430" -> "04:00" · "04:30"; anything else as typed. The box
 *  has the phone's number pad, which has no colon. */
export function timeOr(raw) {
  const m = /^(\d{1,2})(?:[:：.](\d{2}))?$/.exec(raw) || /^(\d{1,2})(\d{2})$/.exec(raw);
  return m ? `${m[1].padStart(2, "0")}:${m[2] || "00"}` : raw;
}

export function sameList(a, b) {
  return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => v === b[i]);
}

/** A line of words that says how a button's work went, put in the button's own row (under
 *  the row's words) the first time it speaks. */
export function sayer(anchor) {
  const el = h("p", { class: "why", role: "status", style: { margin: "6px 0 0", lineHeight: "1.6" } });
  const place = () => {
    if (el.isConnected) return;
    const txt = anchor && anchor.closest(".item") ? anchor.closest(".item").querySelector(".txt") : null;
    (txt || anchor.parentElement).append(el);
  };
  return {
    ok(text) { place(); el.className = "why"; el.textContent = text || ""; },
    err(text) { place(); el.className = "err"; el.textContent = text; },
    clear() { el.remove(); el.textContent = ""; },
  };
}

// ---------------------------------------------------------------- the page's state

/** Everything the present page shares: the settings and status last read, the boxes by
 *  setting (for marking a refused one), and what to redraw when a reply comes back.
 *  `live` false = not connected: every control drawn off, nothing read or written. */
export function presentPage(snap) {
  const p = {
    live: Boolean(snap),
    s: snap ? snap.settings : null,
    st: snap ? snap.status : null,
    state: snap ? snap.settings_state : null,
    marks: new Map(),
    errs: {},
    homes: {},
    watchers: [],
    top: h("div"),
  };
  /** The page-wide error lines under the sub-bar (present.json unreadable, the gateway's
   *  error): spaced only while there is one. */
  p.topSync = () => { p.top.style.margin = p.top.childNodes.length ? "-16px 0 24px" : "0"; };
  p.ctl = (el) => (p.live ? el : off(el));
  p.reg = (key, el) => { p.marks.set(key, el); return el; };
  p.unreg = (prefix) => { for (const k of [...p.marks.keys()]) if (k.startsWith(prefix)) p.marks.delete(k); };
  /** Run `fn` now and after every reply (only when connected). */
  p.watch = (fn) => { p.watchers.push(fn); if (p.live) fn(); };
  p.update = (next, { settings = true } = {}) => {
    if (!next || next.connected !== true) return;
    p.s = next.settings; p.st = next.status; p.state = next.settings_state;
    for (const fn of p.watchers) fn();
    if (settings) showSettingsState(p);
  };
  return p;
}

/** The boxes a field names: itself, and what sits under it (`wake.dnd` -> both times,
 *  `compress.weak_pct` -> every weak line). */
export function boxesOf(p, field) {
  if (!field) return [];
  return [...p.marks.entries()]
    .filter(([k]) => k === field || k.startsWith(`${field}.`) || k.startsWith(`${field}[`))
    .map(([, el]) => el);
}

export function clearMarks(p) {
  for (const el of p.marks.values()) el.removeAttribute("aria-invalid");
  for (const el of Object.values(p.errs)) { el.remove(); el.textContent = ""; }
  fill(p.top);
  p.topSync();
}

export function fieldWords(field, message) {
  const bare = String(field || "").replace(/\[\d+\]/g, "");
  const key = Object.keys(FIELD_WORDS).filter((k) => bare === k || bare.startsWith(`${k}.`))
    .sort((a, b) => b.length - a.length)[0];
  return key ? `${FIELD_WORDS[key]}：${message}` : message;
}

/** Mark the field's boxes and say `message` in the row of the first one (else at the
 *  section's own place). */
export function fail(p, section, field, message) {
  const boxes = boxesOf(p, field);
  for (const el of boxes) el.setAttribute("aria-invalid", "true");
  const err = p.errs[section] || (p.errs[section] = h("p", { class: "err", role: "alert", style: { margin: "6px 0 0", lineHeight: "1.6" } }));
  err.textContent = err.isConnected && err.textContent ? `${err.textContent}；${message}` : message;
  if (err.isConnected) return;
  const item = boxes.length ? boxes[0].closest(".item") : null;
  const home = item ? item.querySelector(".txt") : p.homes[section];
  if (home) home.append(err); else { p.top.append(err); p.topSync(); }
}

/** The gateway's own trouble with present.json: a section it could not take is running on
 *  its defaults, or the file cannot be read at all. */
export function showSettingsState(p) {
  clearMarks(p);
  for (const e of (p.state && p.state.errors) || []) {
    const section = String(e.field || "").split(/[.[]/)[0];
    const words = fieldWords(e.field, e.error);
    if (p.homes[section]) fail(p, section, e.field, `present.json 里这一格不对，先按缺省跑：${words}`);
    else p.top.append(errorLine(`present.json：${e.error}`));
  }
  p.topSync();
}

/** Save one patch. A refusal undoes the control (`undo`), marks the field and says why. */
export async function save(p, patch, { undo } = {}) {
  const section = Object.keys(patch)[0];
  clearMarks(p);
  try {
    p.update(await api.post(PRESENT, { patch }));
    return true;
  } catch (e) {
    if (e && e.status === 401) return false;
    if (undo) undo();
    const field = e && e.body ? e.body.field : null;
    fail(p, section, field, fieldWords(field, e && e.message ? e.message : String(e)));
    return false;
  }
}

/** Read the status again a little later, after a button queued work in the background. */
export function readLater(p, anchor, ms = 8000) {
  setTimeout(async () => {
    if (!anchor.isConnected) return;
    try { p.update(await api.get(PRESENT), { settings: false }); } catch (_) { /* the next visit shows it */ }
  }, ms);
}

// ---------------------------------------------------------------- bound controls

/** A number box for one setting, saved when it changes. `kind`: "int", "int?" (empty =
 *  null) or "time". `board` is what the box shows when not connected. */
export function numBox(p, path, { label, kind = "int", board = "", placeholder, style }) {
  const key = path.join(".");
  if (!p.live) return off(num({ value: board, "aria-label": label, style }));
  const show = kind === "pct" ? pctShow : (v) => (v === null || v === undefined ? "" : String(v));
  const el = p.reg(key, num({ "aria-label": label, placeholder, style }));
  el.addEventListener("change", () => {
    const raw = el.value.trim();
    const saved = at(p.s, path);
    if (raw === "" && kind !== "int?") { el.value = show(saved); el.removeAttribute("aria-invalid"); return; }
    const value = raw === "" ? null : kind === "time" ? timeOr(raw) : kind === "pct" ? pctRead(raw) : intOr(raw);
    if (value === saved) { el.value = show(saved); return; }
    save(p, nest(path, value));
  });
  p.watch(() => {
    if (document.activeElement !== el && el.getAttribute("aria-invalid") !== "true") el.value = show(at(p.s, path));
  });
  return el;
}

/** A switch for one setting, saved when it is flipped; flipped back when refused. */
export function toggle(p, path, label) {
  if (!p.live) return off(sw({ checked: true, label }));
  const el = p.reg(path.join("."), sw({
    checked: Boolean(at(p.s, path)),
    label,
    onChange: async (on) => {
      el.disabled = true;
      await save(p, nest(path, on), { undo: () => el.setAttribute("aria-checked", String(!on)) });
      el.disabled = false;
    },
  }));
  p.watch(() => el.setAttribute("aria-checked", String(Boolean(at(p.s, path)))));
  return el;
}

/** A set of radio choices for one setting: [[value, label, ...extra]]. Returns the
 *  labels in order. */
export function choices(p, name, path, options) {
  if (!p.live) return options.map(([, label, board, ...extra]) => off(radio({ name, label, checked: board }, ...extra)));
  const els = options.map(([value, label, , ...extra]) => radio({
    name, label, value, checked: at(p.s, path) === value,
    onChange: (v) => save(p, nest(path, v), { undo: () => check() }),
  }, ...extra));
  const inputs = els.map((l) => l.querySelector("input"));
  const check = () => { for (const i of inputs) i.checked = i.value === at(p.s, path); };
  p.reg(path.join("."), els[0]);
  p.watch(check);
  return els;
}

// ---------------------------------------------------------------- prompt cards

/** One of the gateway's prompt cards as a numbered block. Drawn off at once; then from
 *  `prompts` — what ask(`${PRESENT}/prompts`) is reading, null when not connected — and
 *  again from each reply's `item`: POST {key, text} saves, {key, reset: true} is 恢复默认. */
export function cardBlock(no, key, title, prompts) {
  const holder = h("div");
  const write = async (body) => draw((await api.post(`${PRESENT}/prompts`, { key, ...body })).item);
  const draw = (card) => fill(holder, promptCard({
    no,
    title,
    card,
    changedWords: (day) => `改过 · ${day}`,
    onSave: (text) => write({ text }),
    onReset: () => write({ reset: true }),
  }));
  fill(holder, promptCard({ no, title, card: { key, text: "" }, off: true }));
  if (prompts) {
    prompts.then((out) => {
      const card = out && Array.isArray(out.items) ? out.items.find((c) => c.key === key) : null;
      if (card) draw(card);
      else if (out && out.error) holder.append(errorLine(out.error));
    }).catch(() => { /* the login gate: the router takes over */ });
  }
  return holder;
}

// ---------------------------------------------------------------- reading

/** Read what a present route says; a refusal that is not the login gate reads as not
 *  connected, with the error. */
export async function ask(path) {
  try {
    return await api.get(path);
  } catch (e) {
    if (e && e.status === 401) throw e;
    return { connected: false, error: e && e.message ? e.message : String(e) };
  }
}
