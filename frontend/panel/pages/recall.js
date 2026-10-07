/* ==========================================================
   pages/recall.js — what was searched and handed over lately, and searching

   One page, no sub-tabs: a search box and 高级搜索 beside it. What is searched lives in
   the address (#/recall?query=看牙&room=EVENT/SELF&when=2026-10&tag=海边), so a search
   can be linked and reloaded.

   The box empty, no filter (boards recall, recall-phone):
       GET /api/loci/recall/timeline, paged: the last three natural days, newest first,
       grouped by day (今天 · 昨天 · the day before). 「我搜「X」」 — 捞回 N 条 · 看是哪几条
       (searches X again here) or 没捞到; 「你说「X」」 — 递卡：<the memory>（<what became of
       it>）, opening the memory. The time at the right (first under the line on the phone).
   Words typed, or a filter set (boards recall-search, recall-search-phone):
       GET /api/loci/recall?query&room&when&tag&offset&limit — its `rows`, paged: one line
       per root, open promises first; under each line 答应了还没关, how it was found
       (字面 · 意思相近) and its score, 「+ N 条派生」 for the other hits of its root; its
       date at the right (first under the line on the phone). Filters alone, no words:
       the same lines without how or score. Nothing found: 没捞到.
   高级搜索 (folded until clicked; open while a filter is set): room — event / mind, and
       the second box follows the first (event: self / world; mind: traits / views);
       when — year, month, day; tag — the most used, from GET /api/loci/rooms (top_tags).
   Read only.
   ========================================================== */

import * as api from "../api.js";
import { h, group, row, tip, pagedList, clock, clickable } from "../ui.js";
import { go } from "../router.js";
import { openDetail } from "../detail.js";

const TIP_HOW = "字面 = 字对上了。意思相近 = 字不一样，但意思像（靠向量模型）。后面的数是综合分，越高越相关。";

// The four rooms, as two boxes: the door, then the side the door has.
const SIDES = { EVENT: ["SELF", "WORLD"], MIND: ["TRAITS", "VIEWS"] };
const ANY = "—";

const SEARCH_ICON = '<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="#8A9096" stroke-width="1.6" aria-hidden="true"><circle cx="8.5" cy="8.5" r="6.5"></circle><path d="M13.5 13.5 18 18" stroke-linecap="round"></path></svg>';

// 高级搜索 opened by hand stays open across the page's own re-renders.
let advOpen = false;

/** The search the address holds. */
function gatesOf(params) {
  return {
    query: (params.get("query") || "").trim(),
    room: (params.get("room") || "").trim().toUpperCase(),
    when: (params.get("when") || "").trim(),
    tag: (params.get("tag") || "").trim(),
  };
}

function addressOf(gates) {
  const q = new URLSearchParams();
  for (const k of ["query", "room", "when", "tag"]) if (gates[k]) q.set(k, gates[k]);
  const s = q.toString();
  return s ? `#/recall?${s}` : "#/recall";
}

const filtered = (g) => !!(g.room || g.when || g.tag);

// ---------------------------------------------------------------- the bar

function select(cls, label, options, value, onChange, disabled = false) {
  const el = h("select", { class: `pill ${cls}`, "aria-label": label, disabled },
    options.map(([v, text]) => h("option", { value: v, text })));
  el.value = value;
  el.addEventListener("change", () => onChange(el.value));
  return el;
}

/** One part of `when`: yyyy, mm or dd (also its class, which sizes it). */
function dateBox(label, value, onCommit) {
  const el = h("input", { class: `pill ${label}`, inputmode: "numeric", maxlength: label === "yyyy" ? "4" : "2",
    "aria-label": label, placeholder: label, value });
  el.addEventListener("change", () => onCommit());
  el.addEventListener("keydown", (e) => { if (e.key === "Enter") onCommit(); });
  return el;
}

function searchBar(gates, topTags) {
  const input = h("input", { type: "search", placeholder: "搜点什么吧", value: gates.query, enterkeyhint: "search" });
  const icon = h("span", { style: { display: "inline-flex" } });
  icon.innerHTML = SEARCH_ICON;
  const form = h("form", { class: "sbar", role: "search" },
    h("label", { class: "sq" }, icon, h("span", { class: "sr", text: "搜记忆" }), input));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    go(addressOf({ ...gates, query: input.value.trim() }));
  });

  const open = advOpen || filtered(gates);
  const adv = h("button", { class: "adv", type: "button", "aria-expanded": String(open) }, "高级搜索");
  form.append(adv);

  const apply = (change) => go(addressOf({ ...gates, query: input.value.trim(), ...change }));

  // room: the door, then its side; a side alone means nothing, so it waits for the door.
  const [door = "", side = ""] = gates.room.split("/");
  const doorBox = select("door", "room 类型", [["", ANY], ["EVENT", "event"], ["MIND", "mind"]], SIDES[door] ? door : "",
    (v) => apply({ room: v }));
  const sides = SIDES[door] || [];
  const sideBox = select("side", "哪一边", [["", ANY], ...sides.map((s) => [s, s.toLowerCase()])],
    sides.includes(side) ? side : "", (v) => apply({ room: v ? `${door}/${v}` : door }), !sides.length);

  // when: year, month, day — as much of it as is filled from the left.
  const [y = "", m = "", d = ""] = gates.when.split("-");
  const yy = dateBox("yyyy", y, () => commitWhen());
  const mm = dateBox("mm", m, () => commitWhen());
  const dd = dateBox("dd", d, () => commitWhen());
  function commitWhen() {
    const parts = [yy.value.trim()];
    if (parts[0] && mm.value.trim()) parts.push(mm.value.trim().padStart(2, "0"));
    if (parts[1] && dd.value.trim()) parts.push(dd.value.trim().padStart(2, "0"));
    const when = parts[0] ? parts.join("-") : "";
    if (when !== gates.when) apply({ when });
  }

  const tags = (topTags || []).map((t) => t.tag);
  if (gates.tag && !tags.includes(gates.tag)) tags.unshift(gates.tag);
  const tagBox = select("tagbox", "tag", [["", ANY], ...tags.map((t) => [t, t])], gates.tag, (v) => apply({ tag: v }));

  const flt = h("div", { class: "flt", role: "group", "aria-label": "高级搜索", hidden: !open },
    h("div", { class: "fg" }, h("span", { class: "fl", text: "room" }), doorBox, sideBox),
    h("div", { class: "fg" }, h("span", { class: "fl", text: "when" }), yy, mm, dd),
    h("div", { class: "fg" }, h("span", { class: "fl", text: "tag" }), tagBox));
  adv.addEventListener("click", () => {
    advOpen = flt.hidden;
    flt.hidden = !advOpen;
    adv.setAttribute("aria-expanded", String(advOpen));
  });
  return h("div", { class: "searchhead" }, form, flt);
}

// ---------------------------------------------------------------- the timeline

/** "2026-10-07" + n days, on the calendar (no clock involved). */
function dayPlus(day, n) {
  const [y, m, d] = day.split("-").map(Number);
  const t = new Date(Date.UTC(y, m - 1, d + n));
  return t.toISOString().slice(0, 10);
}

function dayLabel(day, today) {
  if (day === today) return "今天";
  if (day === dayPlus(today, -1)) return "昨天";
  return day.slice(5);
}

function timelineRow(it) {
  const time = clock(it.at);
  const at = [time ? h("span", { class: "only-phone", text: time }) : null];
  const right = h("span", { class: "dt", text: time });
  if (it.kind === "search") {
    const found = it.n > 0;
    return row({
      text: `我搜「${it.query || ""}」`,
      why: [...at, ...(found ? [`捞回 ${it.n} 条`, h("span", { class: "lnk", text: "看是哪几条" })] : ["没捞到"])],
      open: found && it.query ? () => go(addressOf({ query: it.query })) : null,
      right,
    });
  }
  const cards = it.cards || [];
  const first = cards.find((c) => c.id);
  return row({
    text: it.said ? `你说「${it.said}」` : (cards[0] && cards[0].kind_words) || "",
    why: [...at, ...cards.map((c) => `递卡：${c.text || c.entry_words || ""}（${c.state_words}）`)],
    open: first ? () => openDetail(first.id) : null,
    right,
  });
}

async function renderTimeline(view) {
  let today = "";
  const list = await pagedList({
    load: async (q) => {
      const out = await api.get("/api/loci/recall/timeline", q);
      if (out.since) today = dayPlus(out.since, 2);
      return out;
    },
    items: (items) => {
      const days = [];
      for (const it of items) {
        const day = String(it.at || "").slice(0, 10);
        if (!days.length || days[days.length - 1].day !== day) days.push({ day, rows: [] });
        days[days.length - 1].rows.push(timelineRow(it));
      }
      return days.map((d) => group(dayLabel(d.day, today), {}, h("div", null, d.rows)));
    },
  });
  if (!list.total) return;
  list.el.classList.add("grouped");
  view.append(h("main", { class: "sections" }, list.el));
}

// ---------------------------------------------------------------- the results

/** How a hit was found, in the canvas's words: 字面 · 意思相近 (the API's 意思), or the
 *  API's own word for anything else (部分字面). */
function howWords(how) {
  return String(how || "").split("+").filter(Boolean).map((w) => (w === "意思" ? "意思相近" : w));
}

function resultRow(r) {
  const why = [r.date ? h("span", { class: "only-phone", text: r.date }) : null];
  if (r.open_promise) why.push("答应了还没关");
  why.push(...howWords(r.how));
  if (r.score != null) why.push(Number(r.score).toFixed(1));
  if ((r.others || []).length) why.push(h("span", { class: "lnk", text: `+ ${r.others.length} 条派生` }));
  return row({ text: r.text, why, open: () => openDetail(r.id), right: h("span", { class: "dt", text: r.date || "" }) });
}

async function renderResults(view, gates) {
  const list = await pagedList({
    load: async (q) => {
      const out = await api.get("/api/loci/recall", { ...gates, ...q });
      return out.rows || { items: [], total: 0 };
    },
    item: resultRow,
  });
  // Only words typed are matched one way or another; filters alone just list.
  const head = gates.query ? h("p", { class: "why lead" }, "每条下面写着它是怎么被找到的", tip(TIP_HOW)) : null;
  view.append(h("main", { class: "sections" }, h("section", { "aria-label": "搜到的" },
    list.total ? [head, list.el] : h("p", { class: "empty", style: { margin: "0" }, text: "没捞到" }))));
}

export default {
  async render(view, route) {
    const gates = gatesOf(route.params);
    const searching = !!gates.query || filtered(gates);
    let topTags = [];
    try { topTags = (await api.get("/api/loci/rooms")).top_tags || []; } catch (_) { /* the tag box stays short */ }
    view.append(searchBar(gates, topTags));
    return searching ? renderResults(view, gates) : renderTimeline(view);
  },
};
