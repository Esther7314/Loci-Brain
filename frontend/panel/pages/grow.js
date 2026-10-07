/* ==========================================================
   pages/grow.js — what was written today, the slices waiting to be written, and the
   prompt card behind 高级设置

   #/grow           (board grow) GET /api/loci/grow/today, paged: the memories written
                    since today began (the server cuts "today" at the last daily report,
                    else at local midnight), newest first; each row its title, its human
                    tags (tags_human[].text) and the time it was written at the right.
                    None: 「今天还没写下什么」.
   #/grow/slices    (board grow-slices) GET /api/loci/grow/slices, paged by batch: one
                    group per batch, its day (MM-DD) as the label and under it the batch's own
                    line (「<host> · N 段」, 「来自导入 · <title> · N 段」); one row per slice:
                    what it says (an imported one: its draft), 第 a–b 行, 「好像已经记过：」
                    with the memory it guessed (opens it), and its state at the right —
                    「写成记忆了 ›」 opens the memory it became. Read only: the panel never
                    handles a slice for the model.
   #/grow/settings  (board grow-settings) the side model's tagging prompt (key
                    `backfill`): GET /api/loci/prompts, POST /api/loci/prompts {key, text}
                    to save, {key, reset: true} for 恢复默认.
   ========================================================== */

import * as api from "../api.js";
import { h, subbar, row, note, pagedList, clock, clickable, promptCard, errorLine } from "../ui.js";
import { href } from "../router.js";
import { openDetail } from "../detail.js";

const TIP_SLICES = "每天夜里写日报的时候，副模型把这一天的聊天切成一小段一小段，每段配一句「在说什么」。导入时打的草稿也在这儿。这些还不是记忆，等他有空一段段看，自己决定写不写、怎么写。";
const PROMPT_KEY = "backfill";

function tabs(on) {
  return [
    { label: "今天记住的", href: href("grow"), on: on === "today" },
    { label: "等着写的", href: href("grow", "slices"), on: on === "slices" },
  ];
}

const settingsLink = () => h("a", { class: "aside", href: href("grow", "settings"), text: "高级设置" });

async function renderToday(view) {
  view.append(subbar({ tabs: tabs("today"), aside: settingsLink() }));
  const list = await pagedList({
    load: (q) => api.get("/api/loci/grow/today", q),
    item: (it) => row({
      text: it.text,
      why: (it.tags_human || []).map((t) => t.text),
      open: () => openDetail(it.id),
      right: h("span", { class: "r", text: clock(it.at) }),
    }),
  });
  if (!list.total) {
    view.append(note("今天还没写下什么", "empty"));
    return;
  }
  view.append(h("main", { class: "sections" }, h("section", { "aria-label": "今天" }, list.el)));
}

/** 「第 12–40 行」, when the span's place among the batch's lines is known. */
function linesOf(span) {
  if (!span || span.from_line == null || span.to_line == null) return null;
  return span.from_line === span.to_line ? `第 ${span.from_line} 行` : `第 ${span.from_line}–${span.to_line} 行`;
}

function sliceRow(s) {
  const guesses = (s.guesses || []).map((g) => h("span", null, "好像已经记过：",
    clickable(h("span", { class: "lnk", text: g.text || `#${g.short}` }), () => openDetail(g.id))));
  const went = (s.by || [])[0];
  const right = went
    ? clickable(h("span", { class: "r", text: `${s.state_words} ›` }), () => openDetail(went))
    : h("span", { class: "r", text: s.state_words || "" });
  return row({ text: s.draft || s.gist, why: [linesOf(s.span), ...guesses], right });
}

function batchGroup(b) {
  const current = (b.slices || []).filter((s) => s.state !== "replaced").length;
  // A host's batch label already counts its slices; an imported one names the file only.
  const line = b.import ? `${b.label} · ${current} 段` : b.label;
  return h("section", { class: "grp" },
    h("div", { class: "glw stack" }, h("h2", { class: "gl", text: String(b.day || "").slice(5) }), h("p", { class: "why", text: line })),
    h("div", { class: "subs" }, h("div", null, (b.slices || []).map(sliceRow))));
}

async function renderSlices(view) {
  view.append(subbar({ tabs: tabs("slices"), tip: TIP_SLICES, aside: settingsLink() }));
  const list = await pagedList({
    load: (q) => api.get("/api/loci/grow/slices", q),
    items: (batches) => batches.map(batchGroup),
  });
  if (!list.total) return;
  list.el.classList.add("grouped", "batches");
  view.append(h("main", { class: "sections" }, list.el));
}

async function renderSettings(view) {
  // The way back and where this is, in the place of the sub-tabs (one node: a list here
  // would be read as tabs).
  const here = document.createDocumentFragment();
  here.append(h("a", { href: href("grow"), text: "‹ grow" }), h("span", { text: "高级设置" }));
  view.append(subbar({ tabs: here }));
  const body = h("div", { class: "subs" });
  view.append(h("main", { class: "sections" },
    h("section", { class: "grp" }, h("div", { class: "glw" }, h("h2", { class: "gl", text: "提示词" })), body)));

  const show = async () => {
    let cards;
    try {
      const out = await api.get("/api/loci/prompts");
      cards = Array.isArray(out) ? out : (out && (out.items || out.prompts)) || [];
    } catch (e) {
      body.replaceChildren(e && e.status === 404 ? note("还没接上", "empty") : errorLine(e));
      return;
    }
    const card = cards.find((c) => c.key === PROMPT_KEY);
    if (!card) { body.replaceChildren(note("还没接上", "empty")); return; }
    body.replaceChildren(promptCard({
      title: "给副模型打标签的提示词",
      label: "打标签的提示词",
      card,
      onSave: async (text) => { await api.post("/api/loci/prompts", { key: PROMPT_KEY, text }); await show(); },
      onReset: async () => { await api.post("/api/loci/prompts", { key: PROMPT_KEY, reset: true }); await show(); },
    }));
  };
  await show();
}

export default {
  render(view, route) {
    if (route.tab === "slices") return renderSlices(view);
    if (route.tab === "settings") return renderSettings(view);
    return renderToday(view);
  },
};
