/* ==========================================================
   pages/breath.js — breath and surface, the two sub-tabs of the first page

   #/breath          (boards Main, breath-phone) GET /api/loci/breath/last: the last
                     breath actually handed out, as it was, never one computed now.
                     Three groups:
                       核心  档案 (the name page, whole) · 原则 (the pinned rules)
                       最近  近三天 · 惦记的事 (each with why it is there now, 还有 N 条,
                             the slices and imported stretches still waiting)
                       旧事  忽然想起 (how each came up) · 依据变了的 (only when there is any)
                     「最近一次 · <when>」 at the right; the scope line beside it when it
                     was handed out under a narrower one; 「最早的一条记在 <day>」 at the
                     bottom. Nothing handed out yet: the API's `note`.
   #/breath/surface  (boards surface-web, surface-phone) GET /api/loci/awake, paged: the
                     awake pool now, every reason each entry is awake (reasons[].text),
                     its day at the right (first in the reasons row on the phone).
   ========================================================== */

import * as api from "../api.js";
import { h, subbar, group, sub, row, srcLink, moreLine, note, pagedList, dayTime } from "../ui.js";
import { href } from "../router.js";
import { openDetail } from "../detail.js";

const TIP_TABS = "breath 就是他每次醒来、开口之前先看的那一张，只放最要紧的，所以很短。surface 是醒着的那一池，更容易被他注意到。";
const TIP_SURFACE = "surface 是醒着的那一池：刚写下的、日子快到的、被提起过的……都在这儿。醒着的更容易被他注意到，但不保证一定冒出来。每条下面写着它为什么醒着。";
const TIPS = {
  facts: "名字和称呼。开口之前就得知道、来不及去搜的，才放在这儿。",
  rules: "钉住的准则，他每次醒来都先看到。",
  recent: "最近三天的一张小卡，让他接上前几天的事。",
  plan: "答应了还没做的、日子快到的、挂着期限的。最底下一行写着还有几段切片、几段导入的草稿等他处理。",
  sudden: "两条旧事自己冒出来：一条是碰上最近说过的词想起来的，一条是随手翻到的。",
  moved: "一条记忆站着的地基变了：原话被改了、撤回了、删了，或者你在面板上纠正过。他看到以后，自己决定重写还是收起来。没有就整块不出现。",
};

// The panel reads its whole library; this is the line every panel request carries
// (contract §一.4), and the only one breath's date line leaves out.
const OPEN_LINE = "〔范围：全库（open）〕";

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
const openSource = (id) => () => openDetail(id, { layer: "source" });

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
  const facts = core.facts
    ? row({ text: lineOf(core.facts), whole: !!core.facts.text, why: whyOf(core.facts), open: open(core.facts.id),
      right: srcLink(core.facts.short, openSource(core.facts.id)), layout: "start" })
    : null;
  const rules = (core.rules || []).map((r) => row({ text: lineOf(r), why: whyOf(r), open: open(r.id),
    right: srcLink(r.short, openSource(r.id)), layout: "start" }));

  const recent = ((b.recent || {}).items || []).map((it) => row({ text: lineOf(it), why: whyOf(it, it.date),
    open: open(it.id), layout: "start" }));

  const plan = b.prospective || {};
  const planRows = (plan.items || []).map((it) => row({ text: lineOf(it),
    why: whyOf(it, it.reason && it.reason.words), open: open(it.id), layout: "start" }));
  const asks = (plan.questions || []).map((q) => row({ text: lineOf(q), why: whyOf(q), open: open(q.id), layout: "start" }));
  const waiting = [
    plan.slices_pending > 0 ? `宿主那边 ${plan.slices_pending} 段切片等着收` : "",
    plan.imports_pending > 0 ? `导入的 ${plan.imports_pending} 段等着核` : "",
  ].filter(Boolean).join(" · ");

  const sudden = ((b.involuntary || {}).items || []).map((it) => row({ text: lineOf(it), why: whyOf(it, it.why),
    open: open(it.id), layout: "start" }));
  const moved = b.invalidation || {};
  const movedRows = (moved.items || []).map((it) => row({ text: lineOf(it), why: whyOf(it), open: open(it.id),
    right: srcLink(it.short, openSource(it.id)), layout: "start" }));

  const planBlock = planRows.length || asks.length || waiting
    ? sub("惦记的事", { tip: TIPS.plan }, planRows, asks, moreLine(plan.more),
      waiting ? h("p", { class: "more", text: waiting }) : null)
    : null;

  view.append(h("main", { class: "sections" },
    group("核心", {},
      sub("档案", { tip: TIPS.facts }, facts),
      sub("原则", { tip: TIPS.rules }, rules, moreLine(core.rules_more))),
    group("最近", {},
      sub("近三天", { tip: TIPS.recent }, recent),
      planBlock),
    group("旧事", {},
      sub("忽然想起", { tip: TIPS.sudden }, sudden),
      movedRows.length ? sub("依据变了的", { tip: TIPS.moved }, movedRows, moreLine(moved.more)) : null)));
  if (b.earliest) view.append(h("p", { class: "why foot", text: `最早的一条记在 ${b.earliest}` }));
}

async function renderSurface(view) {
  view.append(subbar({ tabs: tabs("surface"), tip: TIP_SURFACE }));
  const list = await pagedList({
    load: (q) => api.get("/api/loci/awake", q),
    item: (it) => row({
      text: lineOf(it),
      why: [it.date ? h("span", { class: "only-phone", text: it.date }) : null,
        ...(it.reasons || []).map((r) => r.text)],
      open: open(it.id),
      right: h("span", { class: "dt", text: it.date || "" }),
      layout: "start",
    }),
  });
  if (!list.total) return;
  view.append(h("main", { class: "sections" }, group("醒着", {}, list.el)));
}

export default {
  render(view, route) {
    return route.tab === "surface" ? renderSurface(view) : renderBreath(view);
  },
};
