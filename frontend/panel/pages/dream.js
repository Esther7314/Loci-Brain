/* ==========================================================
   pages/dream.js — the dreams of the last three nights, and the page's settings

   #/dream           (board dream) GET /api/loci/dreams: the panel's own copy of each
                     dream of the last three natural days, newest first. A night without
                     a dream has no card. One group per dream, its night as the label;
                     the newest is open: its state (with the i), its whole text, and 留下的
                     — each thread candidate, 「what it meets」 with what it brings back,
                     留了 / 没留 at its right. The others are folded to 「state · 展开」.
                     A dream whose sources were withdrawn shows the API's words for that
                     and no text. Read only.
   #/dream/settings  (board dream-settings) 提醒 · 提示词 · 规矩. 规矩 is GET/POST
                     /api/config `dream` (what to weave on, how a dream fades; its
                     `defaults` for 恢复默认; the server's words on a refusal). 提醒 has no
                     setting behind it (nothing reads a dream-delivery switch) and 提示词
                     no prompt route yet: those two say 还没接上 under their titles.

   The archive is three nights at most and is not paged on the page: one read with the
   largest page the API gives.
   ========================================================== */

import * as api from "../api.js";
import { h, fill, subbar, group, sub, row, tip, note, clickable, btn, num, customBox, errorLine } from "../ui.js";
import { href } from "../router.js";

const TIP_PAGE = "夜里把压在心头、还没想明白的事织成一个梦，一夜最多一个。已经想明白的不进梦。";
const TIP_STATE = "还没送到 = 织好了，还没递给他。在散 = 递给他以后，你一回来说话，梦就开始散：先剩碎片，再只剩一句。散了 = 只留一条痕迹。";
const TIP_KEPT = "梦醒以后还连着的线头：以后碰到什么，会想起什么，一个梦最多两条。他觉得有意思，就把它留成一条线索（trace 里看得到）。留了 = 留成线索了；没留 = 让它跟着梦散了。";

const TIP_REMIND = "梦织好以后怎么递到他手上。递过去以后，你一回来说话，梦就开始散。";
const TIP_RULES = "什么时候织、梦怎么散。默认值是拿真数据试出来的，一般不用动。";
const NOT_WIRED = "还没接上";

// The most the API gives on one page (contract §一.2); three nights hold fewer.
const ALL = 50;

/** The state line: the API's words, the i on the open card, 「展开」 on a folded one. */
function stateLine(d, { open, onOpen }) {
  if (open) {
    return h("p", { class: "why", style: { margin: "0 0 6px", display: "flex", alignItems: "center", gap: "6px" } },
      d.state_words, tip(TIP_STATE));
  }
  const more = clickable(h("span", { class: "lnk", text: "展开" }), onOpen);
  return h("p", { class: "why", style: { margin: "6px 0 0" } }, `${d.state_words} · `, more);
}

function keptRow(c) {
  return row({ text: `「${c.meet}」`, why: c.recall, right: h("span", { class: "r", text: c.kept ? "留了" : "没留" }) });
}

/** One dream: a group labelled with its night. Folded, it opens in place on 展开. */
function dreamCard(d, open) {
  const slot = h("div", { class: "subs" });
  const show = (on) => {
    const text = on && d.text ? h("p", { class: "c2 dream-text" }, d.text) : null;
    fill(slot,
      h("div", null, stateLine(d, { open: on, onOpen: () => show(true) }), text),
      on ? sub("留下的", { tip: TIP_KEPT }, (d.kept || []).map(keptRow)) : null);
  };
  show(open);
  return h("section", { class: "grp" },
    h("div", { class: "glw" }, h("h2", { class: "gl", text: d.night || "" })),
    slot);
}

async function renderDreams(view) {
  view.append(subbar({
    tabs: h("span", { style: { color: "var(--ink)" }, text: "这几天做过的梦" }),
    tip: TIP_PAGE,
    aside: h("a", { class: "aside", href: href("dream", "settings"), text: "高级设置" }),
  }));
  const out = await api.get("/api/loci/dreams", { limit: ALL });
  const items = out.items || [];
  if (!items.length) return;
  view.append(h("main", { class: "sections dream" }, items.map((d, i) => dreamCard(d, i === 0))));
}

function backTo(label) {
  return h("span", { style: { display: "contents" } }, h("a", { href: href("dream"), text: `‹ ${label}` }),
    h("span", { style: { color: "var(--ink)" }, text: "高级设置" }));
}

/** A settings group whose road is not there yet: its label (and i), 还没接上 under it. */
function notWired(label, tipText, title) {
  return group(label, { tip: tipText },
    h("div", null,
      title ? h("h3", { class: "st", style: { margin: "0 0 6px" }, text: title }) : null,
      note(NOT_WIRED)));
}

// 规矩, as the board has it: [key, words, small words, unit] per box; a row with two boxes
// is minutes and turns, whichever comes first.
const WEAVE = [
  ["pressure_line", "多重的事才织", "压在心头最重的那一件过了这条线，才织一个梦。调高 = 只在特别重的日子做梦", null],
  ["dull_line", "太轻的不算", "分量低于这个的，一点都不算进去", null],
  ["per_day", "一夜最多", null, "个"],
  ["dream_cooldown_days", "梦见过的，隔多久少梦见", null, "天"],
];
const FADE = [
  ["fragment_minutes", "fragment_turns", "先散成碎片", "你回来说话以后，过这么久或这么多轮（谁先到算谁）"],
  ["oneline_minutes", "oneline_turns", "再只剩一句", "然后整个删掉，只留一条痕迹"],
];
const FADE_NOTE = "他提起这个梦一次，散的时间往后推一点，每次推得比上次少，所以留不到永远。想留住只有一条路：他当场把它写成记忆。";

function unit(text) {
  return h("span", { class: "why", text });
}

/** 规矩: the numbers core/_dream runs on, saved through /api/config `dream`. */
function rulesGroup(dream) {
  const holder = h("div");
  const draw = (rules) => {
    const boxes = {};
    const box = (key, label) => (boxes[key] = num({ value: String(rules[key] ?? ""), "aria-label": label }));
    const err = h("div");
    const save = async (values, buttons) => {
      for (const b of buttons) b.disabled = true;
      fill(err);
      try {
        await api.post("/api/config", { persist: true, dream: values });
        draw((await api.get("/api/config")).dream);
      } catch (e) {
        for (const b of buttons) b.disabled = false;
        fill(err, errorLine(e));
      }
    };
    const reset = btn("恢复默认");
    const keep = btn("保存", { dark: true });
    reset.addEventListener("click", () => save(rules.defaults, [reset, keep]));
    keep.addEventListener("click", () => save(Object.fromEntries(
      Object.entries(boxes).map(([k, b]) => [k, b.value.trim()]).filter(([, v]) => v !== "")), [reset, keep]));
    fill(holder,
      customBox([
        h("h3", { class: "st", style: { fontSize: "16px", margin: "12px 0 2px" }, text: "什么时候织" }),
        WEAVE.map(([key, text, why, u]) => row({ text, why, layout: "set",
          right: h("span", { class: "acts" }, box(key, text), u ? unit(u) : null) })),
        h("h3", { class: "st", style: { fontSize: "16px", margin: "18px 0 2px" }, text: "怎么散" }),
        FADE.map(([minutes, turns, text, why]) => row({ text, why, layout: "set wide stack",
          right: h("span", { class: "acts", style: { flexWrap: "nowrap" } },
            box(minutes, `${text}分钟`), unit("分钟"), box(turns, `${text}轮数`), unit("轮")) })),
        h("p", { class: "why", style: { margin: "10px 0 0", lineHeight: "1.6" }, text: FADE_NOTE }),
      ], { open: true }),
      h("div", { class: "acts setacts" }, reset, keep),
      err);
  };
  draw(dream);
  return group("规矩", { tip: TIP_RULES }, holder);
}

async function renderSettings(view) {
  view.append(subbar({ tabs: backTo("dream") }));
  const body = h("main", { class: "sections" },
    notWired("提醒", TIP_REMIND),
    notWired("提示词", null, "编织一个梦境的提示词"));
  view.append(body);
  body.append(rulesGroup((await api.get("/api/config")).dream));
}

export default {
  render(view, route) {
    return route.tab === "settings" ? renderSettings(view) : renderDreams(view);
  },
};
