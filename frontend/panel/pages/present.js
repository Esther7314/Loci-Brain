/* ==========================================================
   pages/present.js — how the talk is carried from one window to the next

   #/present   four numbered blocks, in the look of the settings pages (muse——设置,
               Dream——设置):
                 01 自助压缩   the gateway's `compress` settings — on, the context
                              window, the ask line, how many raw lines stay, the force
                              line, the weak lines (five at most, each with 删除 while
                              there is more than one) — and 现在压 (POST …/compress),
                              answered in its own row.
                 02 the compress card (GET /api/loci/present/prompts, key `compress`)
                 03 昨日日报   when it is written, yesterday's report, 补一份 while it is
                              missing or failed, how the window flips, and 现在写日报
                              (POST …/report {kind}), each answered in its own row.
                 04 the report card (key `report`)
               All of it is the gateway's (presentform.js: read, saved a control at a
               time, a refused field marked in its row); without a gateway it is drawn
               off and the page says 「还没接上」. Waking and pushing are pages/wakeup.js.

   What a self-compression writes goes to the model only: no reply carries it, and the
   page never shows it.

   The context window box is empty for "work it out" (null): its line says the size in
   force and where it came from.
   ========================================================== */

import * as api from "../api.js";
import { h, fill, pagehead, setBlock, tip, btn, num, errorLine } from "../ui.js";
import {
  NOT_WIRED, PRESENT, ask, presentPage, showSettingsState, save, readLater, numBox, toggle,
  choices, off, acts, setRow, to, small, linkButton, when, pctShow, pctRead, sameList, sayer,
  cardBlock,
} from "../presentform.js";

const TIP = {
  report: "夜里把这一天写成一份日报，第二天开一个新窗口，从日报接着聊。",
  when: "在夜间 4–8 点之间，你没有说话 30 分钟之后就会写日报。",
  compress: "快满的时候他自己压一下，压完换个新窗口接着聊。",
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

const PROMPT_TITLES = { compress: "自助压缩的提示词", report: "写日报的提示词" };

// ---------------------------------------------------------------- 01 自助压缩

/** 128000 -> "128k", 1000000 -> "1M". */
function tokensWords(n) {
  if (!Number.isFinite(n)) return "";
  if (n >= 1e6 && n % 1e5 === 0) return `${n / 1e6}M`;
  if (n >= 1000 && n % 1000 === 0) return `${n / 1000}k`;
  return String(n);
}

/** The weak lines: one row each, then 「+ 再加一条提醒」 for another (five at most). 删除
 *  at a line's right takes it away (while there is more than one), and so does emptying
 *  its box. Returns the rows and the link, in order; later rows go in before the link. */
function weakLines(p) {
  const WHY = "这个水位线会提醒他，压缩与否由他决定。";
  const add = p.ctl(linkButton("+ 再加一条提醒"));
  const end = h("div", { class: "acts setacts" }, add);
  let boxes = [];
  let rows = [];
  const used = () => boxes.filter((b) => b.value.trim() !== "");
  const register = () => {
    p.unreg("compress.weak_pct");
    used().forEach((b, i) => p.reg(`compress.weak_pct[${i}]`, b));
  };
  const drop = (one) => {
    const b = p.ctl(btn("删除", { badge: true }));
    b.addEventListener("click", () => { boxes = boxes.filter((x) => x !== one); layout(); send(); });
    return b;
  };
  const layout = () => {
    for (const r of rows) r.remove();
    const many = boxes.length > 1;
    rows = boxes.map((b, i) => setRow({ text: "弱提醒线", why: i === 0 ? WHY : null, right: acts(b, many ? drop(b) : null) }));
    end.before(...rows);
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
  return [...rows, end];
}

function compressBlock(p) {
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

  const now = p.ctl(btn("现在压", { badge: true }));
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
  return setBlock("01", "自助压缩", { tip: TIP.compress },
    setRow({ text: "开关", right: acts(toggle(p, ["compress", "on"], "自助压缩开关")) }),
    setRow({ text: "上下文窗口", why: p.live ? ctxWhy : null,
      right: acts(numBox(p, ["compress", "context_tokens"], { label: "上下文窗口", kind: "int?", placeholder: "自动", style: { width: "104px" } })) }),
    setRow({ text: "压缩水位线", why: p.live ? fillWhy : null, right: acts(numBox(p, ["compress", "ask_pct"], { label: "水位线", kind: "pct" })) }),
    setRow({ text: "压缩完留多少条原话", why: p.live ? keptWhy : null, right: acts(numBox(p, ["compress", "keep_raw"], { label: "留多少条" })) }),
    setRow({ text: "手动压缩", why: p.live ? ["不等水位线，现在就压一次", lastWhy] : "不等水位线，现在就压一次", right: acts(now) }),
    setRow({ text: "强制压缩线", why: p.live ? forceWhy : "兜底压缩线，不考虑他的意见了。",
      right: acts(numBox(p, ["compress", "force_pct"], { label: "强制压缩线", kind: "pct" })) }),
    weakLines(p),
    home,
    small("如果你开 auto compact（自动压缩），记得关掉，不然两边会起冲突。", { margin: "var(--s-3) 0 0" }));
}

// ---------------------------------------------------------------- 03 昨日日报

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
  const b = p.ctl(btn(words, { badge: true }));
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

function reportBlock(p) {
  const st = () => p.st.report || {};
  const wordsOf = (r) => r.state_words || REPORT_WORDS[r.state] || r.state || "";

  // 什么时候写, with the i saying the gateway's own window and quiet time.
  const whenTipHolder = h("span", { style: { display: "contents" } }, tip(TIP.when));
  p.watch(() => fill(whenTipHolder, tip(whenTip(p.s.report))));

  // 昨天的日报: its day, state and time beside the name, and in the same row its text
  // (written), the error (failed), or nothing more than what the state says.
  const head = h("span");
  const body = h("div", { style: { marginTop: "var(--s-2)" } }, h("div", { class: "bars", "aria-hidden": "true" },
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
    body.hidden = !body.childNodes.length;
  });
  const yesterday = setRow({ text: "昨天的日报", why: p.live ? head : null });
  yesterday.querySelector(".txt").append(body);

  // 漏了的时候: only while yesterday's is missing or failed.
  const redo = reportButton(p, "补一份", "missing", (b) => { b.disabled = !p.live; });
  const missWhy = h("span", { text: "昨天的还没写" });
  const missing = setRow({ text: "漏了的时候", why: missWhy, right: acts(redo) });
  p.watch(() => {
    const r = st();
    missing.hidden = !(r.state === "missing" || r.state === "failed");
    missWhy.textContent = r.state === "failed" ? "昨天的没写成" : "昨天的还没写";
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

  const home = h("div");
  p.homes.report = home;
  return setBlock("03", "昨日日报", { tip: TIP.report },
    setRow({ text: "什么时候写",
      right: acts(numBox(p, ["report", "from"], { label: "从几点", kind: "time", board: "04:00" }), to(),
        numBox(p, ["report", "to"], { label: "到几点", kind: "time", board: "08:00" }), whenTipHolder) }),
    yesterday,
    missing,
    setRow({ text: "换窗", why: "默认一天一个窗。想几天一个窗，或者自己决定什么时候换，在这里改。" }),
    setRow({ lead: daily, right: h("span", { class: "r", text: "默认" }) }),
    setRow({ lead: everyN }),
    setRow({ lead: manual, why: "按「现在写日报」，写完之后你下次开口就是新窗口。", right: acts(nowWrite) }),
    home);
}

// ---------------------------------------------------------------- the page

async function render(view) {
  const snap = await ask(PRESENT);
  const live = Boolean(snap && snap.connected === true);
  const p = presentPage(live ? snap : null);
  const prompts = live ? ask(`${PRESENT}/prompts`) : null;
  view.append(
    pagehead({ title: "今天就在眼前", caption: live || (snap && snap.error) ? null : NOT_WIRED }),
    p.top,
    h("main", { class: "sets present" },
      compressBlock(p),
      cardBlock("02", "compress", PROMPT_TITLES.compress, prompts),
      reportBlock(p),
      cardBlock("04", "report", PROMPT_TITLES.report, prompts)));
  if (live) showSettingsState(p);
  else if (snap && snap.error) { p.top.append(errorLine(snap.error)); p.topSync(); }
}

export default { render };
