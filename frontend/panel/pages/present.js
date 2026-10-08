/* ==========================================================
   pages/present.js — present and its 高级设置 (boards present, present-phone,
   present-settings)

   #/present           自助压缩 · 昨日日报 · 自动唤醒 · 主动推送, as the board lays them out.
                       GET /api/loci/present: the gateway's settings fill the controls,
                       its status fills the lines beside them. A control is saved the moment
                       it changes (a box on Enter or on leaving it, a switch or a choice on
                       the click) with POST /api/loci/present {patch}; a refusal names its
                       field, which is marked, and the server's words show in that row.
                       现在压 POST …/compress · 补一份 / 现在写日报 POST …/report {kind} ·
                       测试 POST …/push-test, each answered in its own row.
   #/present/settings  the three prompt cards (自助压缩的, 写日报的, 自动唤醒的):
                       GET /api/loci/present/prompts, POST {key, text} to save, {key,
                       reset: true} for 恢复默认, drawn by ui.promptCard like Loci's own.
                       The board's samples under the first two are left out: their text
                       speaks of the owner in the third person, which shipped code does not
                       (scripts/check_english.py).

   Loci forwards these to the gateway (web/loci_present.py). No host with a present_url:
   `{connected: false}`, and the page says 「还没接上」 with every control off. A gateway
   that cannot be reached: `{connected: false, error}`, and the page shows the error, the
   controls still off. What a self-compression writes goes to the model only: no reply
   carries it, and the page never shows it.

   The context window box is empty for "work it out" (null): its line says the size in
   force and where it came from. The Bark box never shows the stored code; its
   placeholder is the masked hint, and saving takes the whole code or URL again.
   ========================================================== */

import * as api from "../api.js";
import { h, append, fill, subbar, group, row, tip, btn, sw, radio, num, customBox, promptCard, errorLine, dayTime } from "../ui.js";
import { href } from "../router.js";

const NOT_WIRED = "还没接上";
const PRESENT = "/api/loci/present";

const TIP = {
  report: "夜里把这一天写成一份日报，第二天开一个新窗口，从日报接着聊。",
  when: "在夜间 4–8 点之间，你没有说话 30 分钟之后就会写日报。",
  compress: "快满的时候他自己压一下，压完换个新窗口接着聊。",
  wake: "隔一阵叫醒他一次，让他自己想想、做点事。1小时之内可以保住缓存，你下次开口接得更快、也更省。醒来的时候会顺手带上昨夜的梦和 muse 攒下的，这两样各自在 dream、muse 的高级设置里开关。",
  push: "他醒来想跟你说话的时候，用 Bark 推到你手机上。",
};

/** The i of 什么时候写, with the gateway's own window and quiet time. */
const whenTip = (r) => `在 ${r.from}–${r.to} 之间，你没有说话 ${r.quiet_min} 分钟之后就会写日报。`;

// Where the context window size came from (context_window.js resolve()).
const SOURCE_WORDS = { provider: "服务商报的", table: "常见模型表", learned: "撞墙量出来的", user: "你填的", default: "缺省" };

// status.report.state, when the reply carries no state_words.
const REPORT_WORDS = {
  written: "写了",
  missing: "还没写",
  failed: "没写成",
  quiet: "那天没说话，不写",
  not_due: "还没到写的时候",
  not_built: "写日报这一步网关还没做好",
};
const NOT_BUILT_REPORT = "写日报这一步网关还没做好，现在按了也不会写";

// A refused field, in the board's words (the longest matching prefix wins).
const FIELD_WORDS = {
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

const PROMPT_TITLES = { compress: "自助压缩的提示词", report: "写日报的提示词", wake: "自动唤醒的提示词" };

// ---------------------------------------------------------------- small pieces

function off(el) {
  if (el.matches("input, button, textarea, select")) el.disabled = true;
  for (const c of el.querySelectorAll("input, button, textarea, select")) c.disabled = true;
  return el;
}

function acts(...kids) {
  return h("span", { class: "acts", style: { flexWrap: "nowrap" } }, kids);
}

function setRow({ text = "", lead, why, right, wide = false, stack = false }) {
  const layout = ["set", wide ? "wide" : "", stack ? "stack" : ""].filter(Boolean).join(" ");
  return row({ text, lead, why, right: right || h("span"), layout });
}

function to() {
  return h("span", { class: "why", text: "到" });
}

function small(text, style) {
  return h("p", { class: "why", style: { margin: "8px 0 4px", lineHeight: "1.6", ...(style || {}) }, text });
}

function linkButton(words) {
  return h("button", { class: "lnk", type: "button", style: { fontSize: "14px" } }, words);
}

/** "10-07 15:02" from a local ISO stamp. */
function when(iso) {
  return dayTime(iso).slice(5);
}

/** 128000 -> "128k", 1000000 -> "1M". */
function tokensWords(n) {
  if (!Number.isFinite(n)) return "";
  if (n >= 1e6 && n % 1e5 === 0) return `${n / 1e6}M`;
  if (n >= 1000 && n % 1000 === 0) return `${n / 1000}k`;
  return String(n);
}

/** {a: {b: v}} from ["a", "b"] and v. */
function nest(path, value) {
  return path.reduceRight((inner, key) => ({ [key]: inner }), value);
}

function at(obj, path) {
  return path.reduce((o, k) => (o === null || o === undefined ? undefined : o[k]), obj);
}

/** A whole number as a number; anything else as typed, so the gateway says what is wrong. */
function intOr(raw) {
  const v = Number(raw);
  return raw !== "" && Number.isFinite(v) ? v : raw;
}

/** A percent box shows "85%"; what is typed is read with or without the sign. */
const pctShow = (v) => (v === null || v === undefined || v === "" ? "" : `${v}%`);
const pctRead = (raw) => intOr(raw.replace(/\s*[%％]\s*$/, ""));

/** "4" · "4:30" · "4.30" · "0430" -> "04:00" · "04:30"; anything else as typed. The box
 *  has the phone's number pad, which has no colon. */
function timeOr(raw) {
  const m = /^(\d{1,2})(?:[:：.](\d{2}))?$/.exec(raw) || /^(\d{1,2})(\d{2})$/.exec(raw);
  return m ? `${m[1].padStart(2, "0")}:${m[2] || "00"}` : raw;
}

function sameList(a, b) {
  return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => v === b[i]);
}

/** A line of words that says how a button's work went, put in the button's own row (under
 *  the row's words) the first time it speaks. */
function sayer(anchor) {
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
function presentPage(snap) {
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
function boxesOf(p, field) {
  if (!field) return [];
  return [...p.marks.entries()]
    .filter(([k]) => k === field || k.startsWith(`${field}.`) || k.startsWith(`${field}[`))
    .map(([, el]) => el);
}

function clearMarks(p) {
  for (const el of p.marks.values()) el.removeAttribute("aria-invalid");
  for (const el of Object.values(p.errs)) { el.remove(); el.textContent = ""; }
  fill(p.top);
  p.topSync();
}

function fieldWords(field, message) {
  const bare = String(field || "").replace(/\[\d+\]/g, "");
  const key = Object.keys(FIELD_WORDS).filter((k) => bare === k || bare.startsWith(`${k}.`))
    .sort((a, b) => b.length - a.length)[0];
  return key ? `${FIELD_WORDS[key]}：${message}` : message;
}

/** Mark the field's boxes and say `message` in the row of the first one (else at the
 *  section's own place). */
function fail(p, section, field, message) {
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
function showSettingsState(p) {
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
async function save(p, patch, { undo } = {}) {
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
function readLater(p, anchor, ms = 8000) {
  setTimeout(async () => {
    if (!anchor.isConnected) return;
    try { p.update(await api.get(PRESENT), { settings: false }); } catch (_) { /* the next visit shows it */ }
  }, ms);
}

// ---------------------------------------------------------------- bound controls

/** A number box for one setting, saved when it changes. `kind`: "int", "int?" (empty =
 *  null) or "time". `board` is what the box shows when not connected. */
function numBox(p, path, { label, kind = "int", board = "", placeholder, style }) {
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
function toggle(p, path, label) {
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
function choices(p, name, path, options) {
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

// ---------------------------------------------------------------- 自助压缩

/** The weak lines: one row each, 「+ 再加一条提醒」 for another (five at most). Emptying a
 *  box takes its line away. */
function weakLines(p) {
  const holder = h("div");
  const WHY = "这个水位线会提醒他，压缩与否由他决定。";
  const add = p.ctl(linkButton("+ 再加一条提醒"));
  const link = h("div", { style: { padding: "4px 0 10px" } }, add);
  let boxes = [];
  const used = () => boxes.filter((b) => b.value.trim() !== "");
  const register = () => {
    p.unreg("compress.weak_pct");
    used().forEach((b, i) => p.reg(`compress.weak_pct[${i}]`, b));
  };
  const layout = () => {
    fill(holder, boxes.map((b, i) => setRow({ text: "弱提醒线", why: i === 0 ? WHY : null, right: acts(b) })), link);
    add.disabled = !p.live || boxes.length >= 5;
    if (p.live) register();
  };
  async function send() {
    const list = used().map((b) => pctRead(b.value.trim()));
    register();
    if (sameList(list, p.s.compress.weak_pct)) return;
    if (await save(p, { compress: { weak_pct: list } }) && !sameList(list, p.s.compress.weak_pct)) draw(p.s.compress.weak_pct);
  }
  const box = (v) => {
    const b = num({ value: pctShow(v), "aria-label": "弱提醒线" });
    if (!p.live) return off(b);
    b.addEventListener("change", () => {
      const v = pctRead(b.value.trim());
      if (typeof v === "number") b.value = pctShow(v);
      send();
    });
    return b;
  };
  const draw = (values) => {
    boxes = values.map(box);
    if (!boxes.length) boxes.push(box(""));
    layout();
  };
  add.addEventListener("click", () => { boxes.push(box("")); layout(); boxes[boxes.length - 1].focus(); });
  draw(p.live ? p.s.compress.weak_pct || [] : [""]);
  return holder;
}

function compressGroup(p) {
  const ctxWhy = h("span");
  const fillWhy = h("span");
  const keptWhy = h("span");
  const lastWhy = h("span");
  const forceWhy = h("span");
  p.watch(() => {
    const st = p.st.compress || {};
    const c = st.context || {};
    const auto = p.s.compress.context_tokens === null;
    ctxWhy.textContent = c.tokens
      ? `现在按 ${tokensWords(c.tokens)}（${SOURCE_WORDS[c.source] || c.source || "来处不明"}）${auto ? "，空着就是自动" : "，清空就是自动"}`
      : "空着就是自动";
    if (st.state === "no_thread" || !st.thread) fillWhy.textContent = "还没有对话";
    else if (st.fill_pct === null || st.fill_pct === undefined) fillWhy.textContent = "还不知道装了多少";
    else fillWhy.textContent = `现在装了 ${st.fill_pct}%${st.estimated ? "（估的）" : "（上游报的）"}`;
    keptWhy.textContent = Number.isInteger(st.kept_raw) ? `这一窗现在有 ${st.kept_raw} 条原话` : "";
    lastWhy.textContent = !st.thread ? ""
      : st.last ? `上次换窗 ${when(st.last.at)} · ${st.last.how_words || st.last.how}` : "这一窗是这个对话的第一窗";
    // a forced pack held back (it could not bring the window under the line) says why here
    forceWhy.textContent = st.held_back_words || "兜底压缩线，不考虑他的意见了。";
  });

  const now = p.ctl(btn("现在压"));
  const nowSay = sayer(now);
  now.addEventListener("click", async () => {
    nowSay.clear();
    now.disabled = true;
    try {
      const r = await api.post(`${PRESENT}/compress`, {});
      nowSay.ok(r && r.queued ? "排上了，压完换一个新窗口接着聊" : "好了");
      readLater(p, now);
    } catch (e) {
      if (e && e.status === 401) return;
      nowSay.err(e.status === 409 ? "这个对话已经在压了，等它压完" : e.message);
    } finally { now.disabled = false; }
  });

  const home = h("div");
  p.homes.compress = home;
  const custom = customBox([
    h("div", { style: { marginTop: "6px" } },
      weakLines(p),
      setRow({ text: "强制压缩线", why: p.live ? forceWhy : "兜底压缩线，不考虑他的意见了。",
        right: acts(numBox(p, ["compress", "force_pct"], { label: "强制压缩线", kind: "pct" })) })),
    h("p", { class: "why", style: { margin: "10px 0 0", lineHeight: "1.6" },
      text: "如果你开 auto compact（自动压缩），记得关掉，不然两边会起冲突。" }),
  ], { open: true });
  return group("自助压缩", { tip: TIP.compress },
    h("div", null,
      setRow({ text: "开关", right: acts(toggle(p, ["compress", "on"], "自助压缩开关")) }),
      setRow({ text: "上下文窗口", why: p.live ? ctxWhy : null,
        right: acts(numBox(p, ["compress", "context_tokens"], { label: "上下文窗口", kind: "int?", placeholder: "自动", style: { width: "104px" } })) }),
      setRow({ text: "压缩水位线", why: p.live ? fillWhy : null, right: acts(numBox(p, ["compress", "ask_pct"], { label: "水位线", kind: "pct" })) }),
      setRow({ text: "压缩完留多少条原话", why: p.live ? keptWhy : null, right: acts(numBox(p, ["compress", "keep_raw"], { label: "留多少条" })) }),
      setRow({ text: "手动压缩", why: p.live ? ["不等水位线，现在就压一次", lastWhy] : "不等水位线，现在就压一次", right: acts(now) }),
      home),
    custom);
}

// ---------------------------------------------------------------- 昨日日报

/** The report's text, folded to a few lines with 展开 when it is long. */
function reportText(text) {
  const FOLD = "11em";
  const body = h("p", { class: "c2", style: { fontSize: "15px", lineHeight: "1.8", overflow: "hidden", maxHeight: FOLD } }, text);
  const long = text.length > 160 || text.split("\n").length > 6;
  if (!long) { body.style.maxHeight = ""; return body; }
  const more = linkButton("展开");
  more.addEventListener("click", () => {
    const open = body.style.maxHeight !== "none";
    body.style.maxHeight = open ? "none" : FOLD;
    more.textContent = open ? "收起" : "展开";
  });
  return h("div", null, body, h("div", { style: { marginTop: "6px" } }, more));
}

/** After 补一份 or 现在写日报: queued, already writing (409), not built yet, or the error. */
function reportAnswer(say, e, r, kind) {
  if (r) return say.ok(r.queued ? (kind === "now" ? "排上了，写完之后你下次开口就是新窗口" : "排上了，写好就出现在上面") : "好了");
  if (e.status === 409) return say.err("已经有一份在写了，等它写完");
  if (e.status === 501 || (e.body && e.body.state === "not_built")) return say.err(NOT_BUILT_REPORT);
  return say.err(e.message);
}

function reportButton(p, words, kind, after) {
  const b = p.ctl(btn(words));
  const say = sayer(b);
  b.addEventListener("click", async () => {
    say.clear();
    b.disabled = true;
    try {
      const r = await api.post(`${PRESENT}/report`, { kind });
      reportAnswer(say, null, r, kind);
      readLater(p, b);
    } catch (e) {
      if (e && e.status === 401) return;
      reportAnswer(say, e, null, kind);
    } finally { after(b); }
  });
  return b;
}

function reportGroup(p) {
  const st = () => p.st.report || {};
  const wordsOf = (r) => r.state_words || REPORT_WORDS[r.state] || r.state || "";

  // 什么时候写, with the i saying the gateway's own window and quiet time.
  const whenTipHolder = h("span", { style: { display: "contents" } }, tip(TIP.when));
  p.watch(() => fill(whenTipHolder, tip(whenTip(p.s.report))));

  // 昨天的日报: its day, state and time, then its text (written), the error (failed), or
  // what the state says.
  const head = h("p", { class: "why", style: { margin: "0 0 6px" } });
  const body = h("div", null, h("div", { class: "bars", "aria-hidden": "true" },
    [100, 94, 100, 88, 100, 60].map((w) => h("div", { class: "bar", style: { width: `${w}%` } }))));
  p.watch(() => {
    const r = st();
    head.textContent = [r.day, wordsOf(r), r.at ? `${when(r.at)} 写的` : ""].filter(Boolean).join(" · ");
    if (r.state === "written" && r.text) fill(body, reportText(r.text));
    else if (r.state === "failed") {
      fill(body,
        r.error ? h("p", { class: "err", style: { margin: "0", whiteSpace: "pre-wrap", lineHeight: "1.6" }, text: r.error }) : null,
        small(r.gave_up ? "试了几次都没成，不再自己重试了" : "还会再试", { margin: "6px 0 0" }));
    } else fill(body);
  });

  // 漏了的时候: only when yesterday's is missing or failed.
  const missing = h("div", null, h("h3", { class: "st dim", style: { margin: "0 0 6px" }, text: "漏了的时候" }));
  const redo = reportButton(p, "补一份", "missing", (b) => { b.disabled = !p.live; });
  const redoRow = setRow({ text: "昨天的还没写", right: acts(redo) });
  missing.append(redoRow);
  p.watch(() => {
    const r = st();
    missing.hidden = !(r.state === "missing" || r.state === "failed");
    redoRow.querySelector(".c").lastChild.textContent = r.state === "failed" ? "昨天的没写成" : "昨天的还没写";
  });

  // 现在写日报: only while 手动换窗 is the saved choice.
  const manualOn = () => p.live && p.s.report.flip === "manual";
  const nowWrite = reportButton(p, "现在写日报", "now", (b) => { b.disabled = !manualOn(); });
  p.watch(() => { nowWrite.disabled = !manualOn(); });

  const [daily, everyN, manual] = choices(p, "win", ["report", "flip"], [
    ["daily", "一天一个窗", true],
    ["every_n", "每", false, numBox(p, ["report", "every_n"], { label: "几天" }), " 天换一个窗"],
    ["manual", "手动换窗", false],
  ]);
  const custom = customBox([
    small("默认一天一个窗。想几天一个窗，或者自己决定什么时候换，在这里改。"),
    setRow({ lead: daily, right: h("span", { class: "r", style: { fontSize: "13px" }, text: "默认" }) }),
    setRow({ lead: everyN }),
    setRow({ lead: manual, why: "按「现在写日报」，写完之后你下次开口就是新窗口。", stack: true,
      right: acts(nowWrite) }),
  ], { open: true });

  const home = h("div");
  p.homes.report = home;
  return group("昨日日报", { tip: TIP.report },
    h("div", null, setRow({ text: "什么时候写", wide: true,
      right: acts(numBox(p, ["report", "from"], { label: "从几点", kind: "time", board: "04:00" }), to(),
        numBox(p, ["report", "to"], { label: "到几点", kind: "time", board: "08:00" }), whenTipHolder) }), home),
    h("div", null, h("h3", { class: "st", style: { margin: "0 0 6px" }, text: "昨天的日报" }), p.live ? head : null, body),
    p.live ? missing : h("div", null, h("h3", { class: "st dim", style: { margin: "0 0 6px" }, text: "漏了的时候" }),
      setRow({ text: "昨天的还没写", right: acts(off(btn("补一份"))) })),
    custom);
}

// ---------------------------------------------------------------- 自动唤醒

/** The wake segments: the first on the 自定义时间段 line, more under it, 「+ 增加一个时间段」
 *  for another (twelve at most). A segment is saved once both its times are filled;
 *  emptying both takes it away. */
function segments(p) {
  const first = h("span", { style: { display: "contents" } });
  const more = h("div");
  const add = p.ctl(linkButton("+ 增加一个时间段"));
  let pairs = [];
  const filled = () => pairs.filter(([a, b]) => a.value.trim() || b.value.trim());
  const register = () => {
    p.unreg("wake.segments.list");
    // With none filled, the first pair stands for the list, so "needs one" marks it.
    const shown = filled().length ? filled() : pairs.slice(0, 1);
    shown.forEach(([a, b], i) => { p.reg(`wake.segments.list[${i}].from`, a); p.reg(`wake.segments.list[${i}].to`, b); });
  };
  const listNow = () => filled().map(([a, b]) => ({ from: timeOr(a.value.trim()), to: timeOr(b.value.trim()) }));
  const sameSegs = (x, y) => x.length === y.length && x.every((s, i) => s.from === y[i].from && s.to === y[i].to);
  async function send() {
    register();
    if (filled().some(([a, b]) => !a.value.trim() || !b.value.trim())) return;
    const list = listNow();
    if (sameSegs(list, p.s.wake.segments.list)) return;
    await save(p, { wake: { segments: { list } } });
  }
  const pair = (s) => {
    const a = num({ "aria-label": "从", placeholder: "09:00", value: s ? s.from : "" });
    const b = num({ "aria-label": "到", placeholder: "22:00", value: s ? s.to : "" });
    if (!p.live) { a.value = "09:00"; b.value = "22:00"; return [off(a), off(b)]; }
    a.addEventListener("change", send);
    b.addEventListener("change", send);
    return [a, b];
  };
  const layout = () => {
    const [a, b] = pairs[0];
    fill(first, acts(a, to(), b));
    fill(more, pairs.slice(1).map(([x, y]) => h("div", { style: { paddingLeft: "24px" } },
      setRow({ wide: true, stack: true, right: acts(x, to(), y) }))));
    add.disabled = !p.live || pairs.length >= 12;
    if (p.live) register();
  };
  const draw = (list) => {
    pairs = list.map(pair);
    if (!pairs.length) pairs.push(pair(null));
    layout();
  };
  add.addEventListener("click", () => { pairs.push(pair(null)); layout(); pairs[pairs.length - 1][0].focus(); });
  draw(p.live ? p.s.wake.segments.list || [] : [null]);
  // A saved list shows as the gateway keeps it ("9" -> "09:00"), box for box.
  p.watch(() => {
    const list = p.s.wake.segments.list || [];
    const shown = filled();
    if (shown.length !== list.length) return;
    shown.forEach(([a, b], i) => {
      for (const [el, v] of [[a, list[i].from], [b, list[i].to]]) {
        if (document.activeElement !== el && el.getAttribute("aria-invalid") !== "true") el.value = v;
      }
    });
  });
  // 自定义时间段 sends the segments typed so far with it: the gateway wants at least one.
  const listForOnly = () => (filled().every(([a, b]) => a.value.trim() && b.value.trim()) ? listNow() : undefined);
  return { first, more, add, listForOnly };
}

function wakeGroup(p) {
  const seg = segments(p);
  let modes;
  if (p.live) {
    const path = ["wake", "segments", "mode"];
    modes = [["all", "全天都醒，不分时间段"], ["only", "自定义时间段"]].map(([value, label]) => radio({
      name: "wake", label, value, checked: at(p.s, path) === value,
      onChange: (v) => {
        const list = v === "only" ? seg.listForOnly() : undefined;
        save(p, { wake: { segments: list ? { mode: v, list } : { mode: v } } }, { undo: () => check() });
      },
    }));
    const inputs = modes.map((l) => l.querySelector("input"));
    const check = () => { for (const i of inputs) i.checked = i.value === at(p.s, path); };
    p.reg("wake.segments.mode", modes[0]);
    p.watch(check);
  } else {
    modes = [off(radio({ name: "wake", label: "全天都醒，不分时间段", checked: true })),
      off(radio({ name: "wake", label: "自定义时间段", checked: false }))];
  }
  const custom = customBox([
    small("默认全天按上面的间隔醒。也可以只在某几个时间段里醒。"),
    setRow({ lead: modes[0], right: h("span", { class: "r", style: { fontSize: "13px" }, text: "默认" }) }),
    setRow({ lead: modes[1], wide: true, stack: true, right: seg.first }),
    seg.more,
    h("div", { style: { padding: "4px 0 2px 24px" } }, seg.add),
  ], { open: true });

  // What wake did and will do: last, next (and why it is not on time), today, held.
  const lines = h("div", { style: { padding: "4px 0 0" } });
  p.watch(() => {
    const w = p.st.wake || {};
    const t = w.today || {};
    const stateWords = { dry_run: "只记账，不真叫", settings_unreadable: "唤醒的设置读不出来，先不叫" }[w.state];
    const counts = [`今天醒了 ${t.woke ?? 0} 次`, `说了 ${t.spoke ?? 0} 次话`];
    if (t.paid_failures) counts.push(`没叫成 ${t.paid_failures} 次`);
    if (t.dry_run) counts.push(`只记账 ${t.dry_run} 次`);
    const next = w.next_at ? `下次 ${when(w.next_at)}` : "下次：还排不上";
    fill(lines,
      stateWords ? small(stateWords, { margin: "4px 0" }) : null,
      small(w.last ? `上次 ${when(w.last.at)} · ${w.last.result_words || w.last.result}` : "还没醒过", { margin: "4px 0" }),
      small(w.next_why_words ? `${next} · ${w.next_why_words}` : next, { margin: "4px 0" }),
      small(counts.join(" · "), { margin: "4px 0" }),
      small(w.held === null || w.held === undefined ? "他攥着的话读不出来" : `他说了 ${w.held} 句你还没回`, { margin: "4px 0" }));
  });

  const home = h("div");
  p.homes.wake = home;
  return group("自动唤醒", { tip: TIP.wake },
    h("div", null,
      setRow({ text: "开着", right: acts(toggle(p, ["wake", "on"], "自动唤醒开关")) }),
      setRow({ text: "多久醒一次", right: acts(numBox(p, ["wake", "every_min"], { label: "间隔" }), h("span", { class: "why", text: "分钟" })) }),
      setRow({ text: "免打扰", wide: true, right: acts(numBox(p, ["wake", "dnd", "from"], { label: "从", kind: "time" }), to(),
        numBox(p, ["wake", "dnd", "to"], { label: "到", kind: "time" })) }),
      home,
      p.live ? lines : null),
    custom);
}

// ---------------------------------------------------------------- 主动推送

/** What Bark answered a test push (push.js test()). */
function pushWords(r) {
  if (r.ok) return { ok: true, text: "推出去了，看看手机" };
  if (r.status === "skipped_config_missing") return { ok: false, text: "还没有推送码：先粘贴进来、保存" };
  if (r.status === "error") return { ok: false, text: `没推出去：${r.error || "出错了"}` };
  return { ok: false, text: `没推出去（Bark 回 ${r.status}）${r.error ? `：${r.error}` : ""}` };
}

function pushGroup(p) {
  const area = p.ctl(h("textarea", { class: "bark", rows: "4", placeholder: "粘贴在这里", "aria-label": "Bark 推送码" }));
  const keep = p.ctl(h("button", { class: "tbtn", type: "button" }, "保存"));
  const test = p.ctl(h("button", { class: "tbtn", type: "button" }, "测试"));
  const home = h("div");
  p.homes.push = home;
  const say = h("p", { class: "why", role: "status", style: { margin: "8px 0 0", lineHeight: "1.6" }, hidden: true });
  const tell = (text, ok = true) => { say.className = ok ? "why" : "err"; say.textContent = text; say.hidden = !text; };
  const last = h("p", { class: "why", style: { margin: "8px 0 0", lineHeight: "1.6" }, hidden: true });

  if (p.live) {
    p.reg("push.bark", area);
    p.watch(() => {
      const ps = p.s.push || {};
      area.placeholder = ps.bark_set && ps.bark_hint ? ps.bark_hint : "粘贴在这里";
      const st = p.st.push || {};
      let words = "";
      if (st.state === "not_built") words = "推送这一步网关还没做好";
      else if (st.last) {
        const said = st.last.ok ? "推出去了" : `没推出去（${st.last.status ?? "出错了"}）`;
        words = `上次推送 ${when(st.last.at)} · ${said}${st.retrying ? ` · 还有 ${st.retrying} 条在重试` : ""}`;
      }
      last.textContent = words;
      last.hidden = !words;
    });
    keep.addEventListener("click", async () => {
      tell("");
      const code = area.value.trim();
      if (!code) { tell("把完整的推送码或整条 URL 粘贴进来再保存", false); return; }
      keep.disabled = true;
      if (await save(p, { push: { bark: code } })) { area.value = ""; tell("存好了"); }
      keep.disabled = false;
    });
    test.addEventListener("click", async () => {
      tell("");
      test.disabled = true;
      try {
        const w = pushWords(await api.post(`${PRESENT}/push-test`, {}));
        tell(w.text, w.ok);
      } catch (e) {
        if (!(e && e.status === 401)) tell(e.status === 501 ? "推送这一步网关还没做好" : e.message, false);
      } finally { test.disabled = false; }
    });
  }

  return group("主动推送", { tip: TIP.push }, h("div", null,
    h("div", { style: { display: "flex", alignItems: "baseline", justifyContent: "space-between", padding: "0 0 10px" } },
      h("span", { class: "c", text: "Bark 推送码" }),
      h("span", { style: { display: "flex", gap: "28px" } }, keep, test)),
    area, home, say, last,
    h("p", { class: "why", style: { margin: "10px 0 0", lineHeight: "1.6" },
      text: "用 Kelivo 这类别人家的客户端时，他醒来说的话进不了那边的聊天窗口，只能靠推送送到你手机上；自己搭的前端可以直接显示在窗口里。" })));
}

// ---------------------------------------------------------------- the pages

/** Read what a present route says; a refusal that is not the login gate reads as not
 *  connected, with the error. */
async function ask(path) {
  try {
    return await api.get(path);
  } catch (e) {
    if (e && e.status === 401) throw e;
    return { connected: false, error: e && e.message ? e.message : String(e) };
  }
}

async function renderPresent(view) {
  const snap = await ask(PRESENT);
  const live = snap && snap.connected === true;
  const p = presentPage(live ? snap : null);
  view.append(
    h("div", { class: "subbar one" },
      h("div", { class: "nav tabs" }, h("span", { style: { color: "var(--ink)" }, text: "今天就在眼前" }),
        live || (snap && snap.error) ? null : h("span", { class: "why", text: NOT_WIRED })),
      h("a", { href: href("present", "settings"), style: { fontSize: "15px", color: "var(--small)" }, text: "高级设置" })),
    p.top,
    h("main", { class: "sections", style: "--gl-w: 96px" }, compressGroup(p), reportGroup(p), wakeGroup(p), pushGroup(p)));
  if (live) showSettingsState(p);
  else if (snap && snap.error) { p.top.append(errorLine(snap.error)); p.topSync(); }
}

/** One prompt card, not connected: its title, the box, 恢复默认 / 保存 under it, all off. */
function offCard(title) {
  return h("div", null,
    h("h3", { class: "st", style: { margin: "0 0 12px" }, text: title }),
    off(h("textarea", { class: "prompt", rows: "9", "aria-label": title })),
    h("div", { class: "acts end", style: { marginTop: "20px" } }, off(btn("恢复默认")), off(btn("保存", { dark: true }))));
}

/** One prompt card, connected: drawn again from each reply's `item`. */
function liveCard(card) {
  const holder = h("div");
  const title = PROMPT_TITLES[card.key] || card.title || card.key;
  const draw = (c) => fill(holder, promptCard({
    title,
    card: c,
    onSave: async (text) => draw((await api.post(`${PRESENT}/prompts`, { key: c.key, text })).item),
    onReset: async () => draw((await api.post(`${PRESENT}/prompts`, { key: c.key, reset: true })).item),
  }));
  draw(card);
  return holder;
}

async function renderSettings(view) {
  const out = await ask(`${PRESENT}/prompts`);
  const live = out && out.connected !== false && Array.isArray(out.items);
  append(view, [
    subbar({ tabs: h("span", { style: { display: "contents" } },
      h("a", { href: href("present"), text: "‹ present" }),
      h("span", { style: { color: "var(--ink)" }, text: "高级设置" }),
      live || (out && out.error) ? null : h("span", { class: "why", text: NOT_WIRED })) }),
    out && out.error ? h("div", { style: { margin: "-16px 0 24px" } }, errorLine(out.error)) : null,
    h("main", { class: "sections" },
      group("提示词", {}, live
        ? out.items.map(liveCard)
        : [offCard(PROMPT_TITLES.compress), offCard(PROMPT_TITLES.report), offCard(PROMPT_TITLES.wake)]))]);
}

export default {
  render(view, route) {
    return route.tab === "settings" ? renderSettings(view) : renderPresent(view);
  },
};
