/* ==========================================================
   pages/breath.js — breath and surface, the two sub-tabs of the first page

   #/breath          (Figma frame breath——breath) GET /api/loci/breath/last: the last
                     breath actually handed out — what it named, never a breath computed
                     now — with titles read now. Three groups, their blocks numbered in
                     order as shown (01 档案 …):
                       核心  档案 (the name page, whole) · 原则 (the pinned rules)
                       最近  近三天 (titled with the window that breath covered,
                             `recent.title`; the card, `recent.text`, rendered now from the
                             entries that breath named, over that window, squeezed for
                             reading here (squeezeRecent) — the model reads it unsqueezed; an
                             entry that changed since and is left off it (`in_card` false) is
                             a row under it saying what became of it; no entries at all:
                             这三天没存东西; see core/breath_snapshot.py). 「调」 beside its
                             label opens 近 [n] 天: how many days the next breath covers,
                             `surfacing.breath_recent_days` through POST /api/config
                             (recentAdjust)
                             · 惦记的事 (each with why it is there now, 还有 N 条, the
                             slices and imported stretches still waiting)
                       旧事  忽然想起 (how each came up) · 依据变了的 (only when there is
                             any; each with why, one phrase per reason, in breath's words)
                     Every row shows its id at the right (idTag): the handle the card
                     itself prints and recall takes, so what is read here can be searched.
                     「最近一次 · <when>」 at the right; the scope line beside it when it
                     was handed out under a narrower one; 「最早的一条记在 <day>」 at the
                     bottom. Nothing handed out yet: the API's `note`.
   #/breath/surface  (Figma frame breath——surface) GET /api/loci/awake, paged
                     SURFACE_PAGE to a page: the awake pool now, each entry with its day
                     and every reason it is awake (reasons[].text) under it, its id at the
                     right; the ‹ › line at the page's foot.
   ========================================================== */

import * as api from "../api.js";
import { h, fill, subbar, group, sub, numberSubs, row, idTag, moreLine, note, pagedList, dayTime, num } from "../ui.js";
import { href } from "../router.js";
import { openDetail } from "../detail.js";

const TIP_TABS = "breath 就是他每次醒来、开口之前先看的那一张，只放最要紧的，所以很短。surface 是醒着的那一池，更容易被他注意到。";
const TIP_SURFACE = "surface 是醒着的那一池：刚写下的、日子快到的、被提起过的……都在这儿。醒着的更容易被他注意到，但不保证一定冒出来。每条下面写着它为什么醒着。";
const TIPS = {
  facts: "名字和称呼。开口之前就得知道、来不及去搜的，才放在这儿。",
  rules: "钉住的准则，他每次醒来都先看到。",
  plan: "答应了还没做的、日子快到的、挂着期限的。最底下一行写着还有几段切片、几段导入的草稿等他处理。",
  sudden: "两条旧事自己冒出来：一条是碰上最近说过的词想起来的，一条是随手翻到的。",
  moved: "一条记忆站着的地基变了：原话被改了、撤回了、删了，或者你在面板上纠正过。他看到以后，自己决定重写还是收起来。没有就整块不出现。",
};

const recentTip = (title) => `${title}的一张小卡，让他接上前几天的事。看几天点「调」改，下一次醒来起算。`;
const OFF_CARD = "这几条后来变了，没算进上面的卡";

// How many days breath's recent block covers: the config key and the range POST
// /api/config keeps it in (web/config_api._SURFACING_INTS).
const RECENT_DAYS = "breath_recent_days";
const RECENT_RANGE = [1, 30];
const RECENT_NEXT = "下一次醒来起算";

// surface's rows to a page: about a desktop screen of them (each row is a line and its
// reasons under it); the API takes up to 50 (core/paging.PAGE_MAX).
const SURFACE_PAGE = 15;

// The panel reads its whole library; this is the line every panel request carries
// (contract §一.4), and the only one breath's date line leaves out.
const OPEN_LINE = "〔范围：全库（open）〕";

/* 近三天 squeezed for the panel: blank lines and the ─ rule dropped, a row label
   (「在做什么   …」) turned into 「在做什么：…」, an indented line joined to the row above,
   a bucket heading joined to the 〔when〕 line, and 围着什么 sharing 在做什么's line. */
function squeezeRecent(text) {
  const out = [];
  for (const raw of String(text).split("\n")) {
    const line = raw.replace(/\s*─+\s*$/, "");
    if (!line.trim()) continue;
    const last = out.length - 1;
    if (/^\s/.test(line) && last >= 0) { out[last] += " " + line.trim(); continue; }
    const m = line.match(/^(\S+?) {2,}(.*)$/);
    if (m) {
      const said = `${m[1]}：${m[2]}`;
      if (m[1] === "围着什么" && last >= 0 && out[last].startsWith("在做什么：")) {
        out[last] += (/[。！？]$/.test(out[last]) ? "" : "。") + said;
      } else out.push(said);
      continue;
    }
    if (/^\d{2}-\d{2}/.test(line) && last >= 0 && out[last].startsWith("〔")) {
      out[last] += "  " + line.trim();
      continue;
    }
    out.push(line.trim());
  }
  return out.join("\n");
}

/** 「调」 beside the recent block's label, and the line it opens under the label:
 *  近 [n] 天 and when it counts from. The box reads the setting from GET /api/config the
 *  first time it opens and saves through POST /api/config when it changes, kept within
 *  RECENT_RANGE. The card shown is the last breath's, over that breath's window
 *  (`shownDays`); a new number shows from the next breath on. */
function recentAdjust(block, shownDays) {
  const head = block && block.querySelector(".sth");
  if (!head) return;
  const box = num({ "aria-label": "近几天", inputmode: "numeric" });
  const said = h("span", { class: "why", text: RECENT_NEXT });
  const line = h("div", { class: "adj", hidden: true },
    h("label", { class: "why" }, "近", box, "天"), said);
  const link = h("button", { class: "adj-lnk", type: "button", "aria-expanded": "false", text: "调" });
  let saved = null;
  const say = (text, cls = "why") => { said.className = cls; fill(said, text); };

  link.addEventListener("click", async () => {
    const opening = line.hidden;
    line.hidden = !opening;
    link.setAttribute("aria-expanded", String(opening));
    if (!opening || saved !== null) return;
    box.disabled = true;
    try {
      const cfg = await api.get("/api/config");
      saved = Number((cfg.surfacing || {})[RECENT_DAYS]);
      box.value = String(saved);
      box.disabled = false;
      box.focus();
    } catch (e) { say(e && e.message ? e.message : String(e), "err"); }
  });

  box.addEventListener("change", async () => {
    const raw = box.value.trim();
    const v = Number(raw);
    if (!raw || !Number.isFinite(v)) {
      box.setAttribute("aria-invalid", "true");
      say(`填 ${RECENT_RANGE[0]} 到 ${RECENT_RANGE[1]} 之间的天数`, "err");
      return;
    }
    const days = Math.min(RECENT_RANGE[1], Math.max(RECENT_RANGE[0], Math.round(v)));
    box.removeAttribute("aria-invalid");
    box.value = String(days);
    if (days === saved) { say(RECENT_NEXT); return; }
    box.disabled = true;
    try {
      await api.post("/api/config", { surfacing: { [RECENT_DAYS]: days }, persist: true });
      saved = days;
      say(days === shownDays ? "存好了" : `存好了，${RECENT_NEXT}；上面这张还是上一次递出去的`);
    } catch (e) { say(e && e.message ? e.message : String(e), "err"); }
    box.disabled = false;
  });

  head.append(link);
  head.after(line);
}

function tabs(on) {
  return [
    { label: "breath", href: href("breath"), on: on === "breath" },
    { label: "surface", href: href("breath", "surface"), on: on === "surface" },
  ];
}

/** An item's line: its title read now, or what became of it when it has none. */
function lineOf(it) {
  return it.text ?? it.state_words ?? `#${it.short}`;
}

/** The small words of an item: its own `why`s, then its state when it has a title too. */
function whyOf(it, ...why) {
  const out = why.filter(Boolean);
  if (it.text && it.state_words) out.push(it.state_words);
  return out;
}

const open = (id) => () => openDetail(id);

/** One breath or surface row: its line, the small words under it, its id at the right. */
function entryRow(it, { why, whole = false } = {}) {
  return row({ text: lineOf(it), whole, why, open: open(it.id), right: idTag(it.short), layout: "start" });
}

async function renderBreath(view) {
  const out = await api.get("/api/loci/breath/last");
  const b = out.breath;
  if (!b) {
    view.append(subbar({ tabs: tabs("breath"), tip: TIP_TABS }), note(out.note, "empty"));
    return;
  }
  const when = [`最近一次 · ${dayTime(out.at)}`];
  if (out.scope && out.scope !== OPEN_LINE) when.push(out.scope);
  view.append(subbar({ tabs: tabs("breath"), tip: TIP_TABS, aside: h("span", { class: "why", text: when.join(" · ") }) }));

  const core = b.core || {};
  const facts = core.facts ? entryRow(core.facts, { whole: !!core.facts.text, why: whyOf(core.facts) }) : null;
  const rules = (core.rules || []).map((r) => entryRow(r, { why: whyOf(r) }));

  const rec = b.recent || {};
  const recentTitle = rec.title || "近三天";
  const recentItems = rec.items || [];
  const card = rec.text
    ? h("p", { class: "c2 flat", text: squeezeRecent(rec.text) })
    : recentItems.length ? null : h("p", { class: "why flat", style: { margin: "0" }, text: `${recentTitle.replace(/^近/, "这")}没存东西` });
  const offCard = recentItems.filter((it) => it.in_card === false).map((it) => entryRow(it, { why: whyOf(it, it.date) }));
  const recent = [card, offCard.length ? h("p", { class: "more", text: OFF_CARD }) : null, offCard];

  const plan = b.prospective || {};
  const planRows = (plan.items || []).map((it) => entryRow(it, { why: whyOf(it, it.reason && it.reason.words) }));
  const asks = (plan.questions || []).map((q) => entryRow(q, { why: whyOf(q) }));
  const waiting = [
    plan.slices_pending > 0 ? `宿主那边 ${plan.slices_pending} 段切片等着收` : "",
    plan.imports_pending > 0 ? `导入的 ${plan.imports_pending} 段等着核` : "",
  ].filter(Boolean).join(" · ");

  const sudden = ((b.involuntary || {}).items || []).map((it) => entryRow(it, { why: whyOf(it, it.why) }));
  const moved = b.invalidation || {};
  const movedRows = (moved.items || []).map((it) => entryRow(it, { why: whyOf(it, ...(it.why || [])) }));

  const planBlock = planRows.length || asks.length || waiting
    ? sub("惦记的事", { tip: TIPS.plan }, planRows, asks, moreLine(plan.more),
      waiting ? h("p", { class: "more", text: waiting }) : null)
    : null;

  const [factsBlock, rulesBlock, recentBlock, , suddenBlock, movedBlock] = numberSubs([
    sub("档案", { tip: TIPS.facts }, facts),
    sub("原则", { tip: TIPS.rules }, rules, moreLine(core.rules_more)),
    sub(recentTitle, { tip: recentTip(recentTitle) }, recent),
    planBlock,
    sub("忽然想起", { tip: TIPS.sudden }, sudden),
    movedRows.length ? sub("依据变了的", { tip: TIPS.moved }, movedRows, moreLine(moved.more)) : null,
  ]);
  recentAdjust(recentBlock, rec.days);

  view.append(h("main", { class: "sections" },
    group("核心", {}, factsBlock, rulesBlock),
    group("最近", {}, recentBlock, planBlock),
    group("旧事", {}, suddenBlock, movedBlock)));
  if (b.earliest) view.append(h("p", { class: "why foot", text: `最早的一条记在 ${b.earliest}` }));
}

async function renderSurface(view) {
  view.classList.add("fill");
  view.append(subbar({ tabs: tabs("surface"), tip: TIP_SURFACE }));
  const list = await pagedList({
    load: (q) => api.get("/api/loci/awake", q),
    limit: SURFACE_PAGE,
    item: (it) => entryRow(it, { why: [it.date, ...(it.reasons || []).map((r) => r.text)] }),
  });
  if (!list.total) return;
  list.nav.classList.add("end");
  view.append(h("main", { class: "sections" }, group("醒着", {}, list.el)), list.nav);
}

export default {
  render(view, route) {
    return route.tab === "surface" ? renderSurface(view) : renderBreath(view);
  },
};
