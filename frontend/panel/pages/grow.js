/* ==========================================================
   pages/grow.js — what was written today, the slices waiting to be written, and the
   prompt cards behind 设置

   #/grow           (board grow) GET /api/loci/grow/today, paged with the
                    ‹ › line at the page's foot, as surface has it: the memories written
                    since today began (the server cuts "today" at the last daily report,
                    else at local midnight), newest first; each row its title, its human
                    tags (tags_human[].text) and the time it was written at the right.
                    None: 「今天还没写下什么」.
   #/grow/slices    (board grow-slices) GET /api/loci/grow/slices, paged by batch: one
                    group per batch, its day (MM-DD) as the label and under it the batch's own
                    line (「<host> · N 段」, 「来自导入 · <title> · N 段」); one row per slice:
                    what it says (an imported one: its draft), 第 a–b 行, 「好像已经记过：」
                    with the memory it guessed (opens it), and its state at the right —
                    「写成记忆了 ›」 opens the memory it became. A row opens its 原话 in the
                    detail window (openSliceSource: GET /api/loci/grow/slices/{id}/source,
                    看原话 asks `?fetch=1`). A slice whose source is withdrawn, deleted or
                    held shows the API's `source_words` in place of what it says. Read
                    only: the panel never handles a slice for the model. With slicing
                    off (the reply's `slicing_on` false) a line above the batches says
                    so; the batches already there stay listed.
   #/grow/settings  (her frame grow——设置) three numbered blocks: 01 the tagging
                    prompt (key `backfill`), 02 切片开关, 03 the slicing prompt (key
                    `slicer`, with 切片结果样例 under it: what a slicing gives back,
                    folded). GET /api/loci/prompts,
                    POST /api/loci/prompts {key, text} to save, {key, reset: true} for
                    恢复默认; a card drawn again from the reply's `item`, a refusal in the
                    server's words under the buttons.
                    02 切片开关 is /api/config `slices.enabled`, saved when flipped
                    (POST /api/config {persist: true, slices}; a refusal says the
                    server's words and the switch flips back). Off, the 03 card is shown
                    greyed and cannot be saved.
   ========================================================== */

import * as api from "../api.js";
import { h, fill, subbar, row, note, pagedList, footPager, clock, clickable, promptCard, sampleFold, errorLine, sw, setBlock } from "../ui.js";
import { href } from "../router.js";
import { openDetail, openSliceSource } from "../detail.js";

// The slicing switch's words (hers, 10-16).
const SLICING = "切片开关";
const SLICING_WHY = "写日报之前会把今天的原文发给副模型、分段写单子，然后拿给他认领。关了没有切片，他的记忆回不到原话。";
const SLICING_OFF = "副模型切片关掉了，晚上没有新的切片。";

const TIP_SLICES = "每天夜里写日报的时候，副模型把这一天的聊天切成一小段一小段，每段配一句「在说什么」。导入时打的草稿也在这儿。这些还不是记忆，等他有空一段段看，自己决定写不写、怎么写。";

const SAMPLE_NOTE = "副模型交回来的样子（示意）：每片是连续的几行，from、to 是行号，gist 是一句「在讲什么」。「等着写的」里看到的就是这些。";
const SAMPLE = `{"slices": [
  {"from": 1, "to": 12, "gist": "阿青说周末想去看海，两个人商量坐哪班车"},
  {"from": 13, "to": 30, "gist": "阿青讲下午面试被问住的那道题"},
  {"from": 31, "to": 38, "gist": "约好周三晚上一起看电影"}
]}`;

/** 切片结果样例 under the slicing card: a link that unfolds the sample. */
const sliceSample = () => sampleFold({ label: "切片结果样例", note: SAMPLE_NOTE, text: SAMPLE, pre: true });

// The cards of 设置, in the frame's order.
const CARDS = [
  { key: "backfill", title: "副模型打标签的提示词" },
  { key: "slicer", title: "副模型切片的提示词", extra: sliceSample },
];

function tabs(on) {
  return [
    { label: "今天记住的", href: href("grow"), on: on === "today" },
    { label: "等着写的", href: href("grow", "slices"), on: on === "slices" },
  ];
}

const settingsLink = () => h("a", { class: "aside enter", href: href("grow", "settings"), text: "设置   >" });

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
  footPager(view, list);
}

/** 「第 12–40 行」, when the span's place among the batch's lines is known. */
function linesOf(span) {
  if (!span || span.from_line == null || span.to_line == null) return null;
  return span.from_line === span.to_line ? `第 ${span.from_line} 行` : `第 ${span.from_line}–${span.to_line} 行`;
}

function sliceRow(s) {
  const guesses = (s.guesses || []).map((g) => h("span", null, "好像已经记过：",
    clickable(h("span", { class: "lnk", text: g.text || `#${g.short}` }), (e) => { e.stopPropagation(); openDetail(g.id); })));
  const went = (s.by || [])[0];
  const right = went
    ? clickable(h("span", { class: "r", text: `${s.state_words} ›` }), () => openDetail(went))
    : h("span", { class: "r", text: s.state_words || "" });
  return row({ text: s.draft || s.gist || s.source_words || "", why: [linesOf(s.span), ...guesses], right,
    open: () => openSliceSource(s.slice_id) });
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
  if (list.reply.slicing_on === false) view.append(note(SLICING_OFF, "empty"));
  if (!list.total) return;
  list.el.classList.add("grouped", "batches");
  view.append(h("main", { class: "sections" }, list.el));
  footPager(view, list);
}

async function renderSettings(view) {
  // The way back, in the place of the sub-tabs.
  const bar = subbar({ tabs: h("a", { class: "on back", href: href("grow") },
    h("span", { "aria-hidden": "true", text: "<" }), "grow") });
  bar.classList.add("tight");
  const body = h("main", { class: "pcards" });
  view.append(bar, body);

  let items, slicing;
  try {
    const [prompts, config] = await Promise.all([api.get("/api/loci/prompts"), api.get("/api/config")]);
    items = prompts.items || [];
    slicing = !(config.slices && config.slices.enabled === false);
  } catch (e) {
    body.append(errorLine(e));
    return;
  }
  let n = 0;
  let redrawSlicer = null;
  for (const { key, title, extra } of CARDS) {
    const card = items.find((c) => c.key === key);
    if (!card) continue;
    // The switch takes the number before the slicing card.
    if (key === "slicer") ++n;
    const no = String(++n).padStart(2, "0");
    const holder = h("div");
    const under = extra ? extra() : null;
    let last = card;
    const draw = (c) => {
      last = c;
      fill(holder, promptCard({
        no, title, card: c, extra: under, off: key === "slicer" && !slicing,
        onSave: async (text) => draw((await api.post("/api/loci/prompts", { key, text })).item),
        onReset: async () => draw((await api.post("/api/loci/prompts", { key, reset: true })).item),
      }));
      holder.classList.toggle("off", key === "slicer" && !slicing);
    };
    draw(card);
    if (key === "slicer") {
      redrawSlicer = () => draw(last);
      body.append(slicingBlock(String(n - 1).padStart(2, "0"), (on) => { slicing = on; redrawSlicer(); }, slicing));
    }
    body.append(holder);
  }
}

/** 切片开关 (/api/config `slices.enabled`), numbered `no`: saved when flipped; a refusal
 *  flips it back and says why under it. `changed(on)` after a save went through. */
function slicingBlock(no, changed, on) {
  const err = h("div");
  const toggle = sw({
    checked: on,
    label: SLICING,
    onChange: async (want) => {
      fill(err);
      toggle.disabled = true;
      try {
        await api.post("/api/config", { persist: true, slices: { enabled: want } });
        changed(want);
      } catch (e) {
        toggle.setAttribute("aria-checked", String(!want));
        fill(err, errorLine(e));
      }
      toggle.disabled = false;
    },
  });
  return setBlock(no, SLICING, {},
    row({ text: SLICING_WHY, right: h("span", { class: "acts" }, toggle), layout: "big set" }),
    err);
}

export default {
  render(view, route) {
    if (route.tab === "slices") return renderSlices(view);
    if (route.tab === "settings") return renderSettings(view);
    return renderToday(view);
  },
};
