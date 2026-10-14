/* ==========================================================
   pages/wakeup.js — everything that wakes him, on one page

   #/wakeup   five numbered blocks, laid out the way her frame present is:
                01 自动唤醒   the gateway's `wake` settings — the switch, every N
                             minutes, the quiet hours — and, only while something is
                             wrong, what is wrong (TROUBLE: a wake that did not go
                             through, a reason it cannot wake). 提示词 beside the title opens
                             the wake card in a glass window (presentform.openPrompt,
                             key `wake`): the letter he reads each time he wakes.
                02 不限时间段/自定义时间段唤醒   one choice of two (`wake.segments.mode`): all
                             day, or only some segments (twelve at most, each with 删除
                             while there is more than one).
                03 主动推送   the Bark code (never shown back: its placeholder is the
                             masked hint), 保存, 测试 (POST …/push-test) and the last
                             push.
                04 muse     the three reminder rows of muse's own settings page
                05 dream    the reminder row of dream's own settings page
              01–03 are the gateway's (pages/presentform.js: read, saved a control at a
              time, a refused field marked in its row); without a gateway they are drawn
              off and the page says 「还没接上」. 04 and 05 are Loci's own /api/config
              and work either way; they are the same stored values their own pages
              show, so a change here shows there.
   ========================================================== */

import * as api from "../api.js";
import { h, fill, pagehead, setBlock, btn, radio, num, errorLine } from "../ui.js";
import {
  NOT_WIRED, PRESENT, ask, presentPage, showSettingsState, save, numBox, toggle,
  off, acts, setRow as set, to, small, linkButton, when, timeOr, at, unit, openPrompt,
} from "../presentform.js";
import { remindRows as museRows, REMIND_TIP as MUSE_TIP } from "./muse.js";
import { remindRow as dreamRow, REMIND_TIP as DREAM_TIP } from "./dream.js";

const TIP = {
  wake: "隔一阵叫醒他一次，让他自己想想、做点事。1小时之内可以保住缓存，你下次开口接得更快、也更省。醒来的时候会顺手带上昨夜的梦和 muse 攒下的。",
  push: "他醒来想跟你说话的时候，用 Bark 推到你手机上。",
};
const CARD_TITLE = "自动唤醒的提示词";

// The reasons for not waking (wake.js WHY_WORDS) that mean something is wrong, not just
// "not now": these show under 01; the ordinary ones (off, quiet hours, outside the
// segments, today's cap) do not.
const TROUBLE = new Set(["settings_unreadable", "state_unreadable", "held_unreadable", "held_cap", "no_key", "backoff", "walled"]);
const FAILED = new Set(["paid_failure", "unpaid_failure"]);


// ---------------------------------------------------------------- 01 自动唤醒 · 02 不限时间段/自定义时间段唤醒

/** The wake segments: the first beside 自定义时间段, more in rows under it, 「+ 增加一个
 *  时间段」 for another (twelve at most). A segment is saved once both its times are
 *  filled; 删除 at its right takes it away (while there is more than one), and so does
 *  emptying both. `rest` is the rows under the first and the link, in order; later rows
 *  go in before the link. */
function segments(p) {
  const first = h("span", { style: { display: "contents" } });
  const add = p.ctl(linkButton("+ 增加一个时间段"));
  const end = h("div", { class: "setacts left" }, add);
  let pairs = [];
  let rows = [];
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
  const drop = (one) => {
    const b = p.ctl(btn("删除", { badge: true }));
    b.addEventListener("click", () => { pairs = pairs.filter((x) => x !== one); layout(); send(); });
    return b;
  };
  const layout = () => {
    const many = pairs.length > 1;
    fill(first, acts(pairs[0][0], to(), pairs[0][1], many ? drop(pairs[0]) : null));
    for (const r of rows) r.remove();
    rows = pairs.slice(1).map((one) => set({ right: acts(one[0], to(), one[1], drop(one)) }));
    end.before(...rows);
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
  return { first, rest: [...rows, end], listForOnly };
}

function wakeBlocks(p) {
  const seg = segments(p);
  let modes;
  if (p.live) {
    const path = ["wake", "segments", "mode"];
    modes = [["all", "不限时间段（免打扰时间除外）"], ["only", "自定义时间段"]].map(([value, label]) => radio({
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
    modes = [off(radio({ name: "wake", label: "不限时间段（免打扰时间除外）", checked: true })),
      off(radio({ name: "wake", label: "自定义时间段", checked: false }))];
  }

  // What is wrong with wake, if anything: the last wake failed, a reason it cannot wake,
  // a books file it cannot read. Nothing while all is well.
  const lines = h("div", { class: "wakestate" });
  p.watch(() => {
    const w = p.st.wake || {};
    const say = [];
    if (w.state === "settings_unreadable") say.push("唤醒的设置读不出来，先不叫");
    if (w.last && FAILED.has(w.last.result)) say.push(`上次 ${when(w.last.at)} · ${w.last.result_words || w.last.result}`);
    if (TROUBLE.has(w.next_why) && w.next_why_words) say.push(w.next_why_words);
    if (w.held === null && w.next_why !== "held_unreadable") say.push("他攥着的话读不出来");
    fill(lines, say.map((t) => small(t, { margin: "4px 0" })));
    lines.hidden = !say.length;
  });

  const card = p.ctl(h("button", { class: "lnk", type: "button" }, "提示词"));
  card.addEventListener("click", () => openPrompt("wake", CARD_TITLE));

  const home = h("div");
  p.homes.wake = home;
  return [
    setBlock("01", "自动唤醒", { tip: TIP.wake, extra: card },
      set({ text: "总开关", right: acts(toggle(p, ["wake", "on"], "自动唤醒总开关")) }),
      set({ text: "多久醒一次", right: h("span", { class: "acts nu" }, numBox(p, ["wake", "every_min"], { label: "间隔" }), unit("分钟")) }),
      set({ text: "免打扰", right: acts(numBox(p, ["wake", "dnd", "from"], { label: "从", kind: "time" }), to(),
        numBox(p, ["wake", "dnd", "to"], { label: "到", kind: "time" })) }),
      home,
      p.live ? lines : null),
    setBlock("02", "不限时间段/自定义时间段唤醒", {},
      set({ lead: modes[0], right: h("span", { class: "r", text: "默认" }) }),
      set({ lead: modes[1], right: seg.first }),
      seg.rest),
  ];
}

// ---------------------------------------------------------------- 03 主动推送

/** What Bark answered a test push (push.js test()). */
function pushWords(r) {
  if (r.ok) return { ok: true, text: "推出去了，看看手机" };
  if (r.status === "skipped_config_missing") return { ok: false, text: "还没有推送码：先粘贴进来、保存" };
  if (r.status === "error") return { ok: false, text: `没推出去：${r.error || "出错了"}` };
  return { ok: false, text: `没推出去（Bark 回 ${r.status}）${r.error ? `：${r.error}` : ""}` };
}

function pushBlock(p) {
  const area = p.ctl(h("textarea", { class: "bark", rows: "3", placeholder: "粘贴在这里", "aria-label": "Bark 推送码" }));
  const test = p.ctl(btn("测试", { badge: true }));
  const keep = p.ctl(btn("保存", { badge: true, dark: true }));
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

  return setBlock("03", "主动推送", { tip: TIP.push },
    set({ text: "Bark 推送码",
      why: "用 Kelivo 这类别人家的客户端时，他醒来说的话进不了那边的聊天窗口，只能靠推送送到你手机上；自己搭的前端可以直接显示在窗口里。" }),
    area,
    h("div", { class: "acts setacts" }, test, keep),
    home, say, last);
}

// ---------------------------------------------------------------- 04 muse · 05 dream

/** A block of Loci's own reminder rows: `rows(values, fail)` draws them from the block of
 *  /api/config named `key`. */
function remindBlock(no, title, tipText, key, rows) {
  const holder = h("div");
  const err = h("div");
  const fail = (e) => fill(err, e ? errorLine(e) : null);
  api.get("/api/config")
    .then((config) => fill(holder, rows(config[key], fail)))
    .catch((e) => { if (!(e && e.status === 401)) fail(e); });
  return setBlock(no, title, { tip: tipText }, holder, err);
}

// ---------------------------------------------------------------- the page

async function render(view) {
  const snap = await ask(PRESENT);
  const live = Boolean(snap && snap.connected === true);
  const p = presentPage(live ? snap : null);
  view.append(
    pagehead({ title: "Loci 会把他唤醒", caption: live || (snap && snap.error) ? null : NOT_WIRED }),
    p.top,
    h("main", { class: "sets wakeup" },
      wakeBlocks(p),
      pushBlock(p),
      remindBlock("04", "muse", MUSE_TIP, "muse", museRows),
      remindBlock("05", "dream", DREAM_TIP, "dream", (dream, fail) => [dreamRow(dream, fail)])));
  if (live) showSettingsState(p);
  else if (snap && snap.error) { p.top.append(errorLine(snap.error)); p.topSync(); }
}

export default { render };
