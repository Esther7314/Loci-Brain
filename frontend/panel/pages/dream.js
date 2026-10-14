/* ==========================================================
   pages/dream.js — the dreams of the last three nights, and the page's settings

   #/dream           (her frame Dream: the page's line, 设置 > at its right) GET
                     /api/loci/dreams: the panel's own copy of each
                     dream of the last three natural days, newest first. A night without
                     a dream has no card. One group per dream, its night as the label;
                     the newest is open: its state (with the i), its whole text, and 留下的
                     — each thread candidate, 「what it meets」 with what it brings back,
                     留了 / 没留 at its right; a kept one opens the entry carrying the
                     thread (`ids`) in the detail window. The others are folded to
                     「state · 展开」.
                     A dream whose sources were withdrawn shows the API's words for that
                     and no text. Read only.
   #/dream/settings  (her frame Dream——设置) four numbered blocks.
                     01 提醒设置: 是否开启自动提醒 is /api/config `dream.deliver_on_wake`
                     (remindRow, which pages/wakeup.js shows as well).
                     02 the weaving prompt's card (key `dream`): GET /api/loci/prompts,
                     POST {key, text} to save, {key, reset: true} for 恢复默认, and
                     梦境结果样例 folding under it.
                     03 梦境设置: the line the heaviest entry must clear (`pressure_line`),
                     how long a dreamt want is picked less (`dream_cooldown_days`).
                     04 梦境消散设置: how long the fragments and the one line last, in
                     minutes and turns, whichever comes first.
                     The switch and every number save themselves when changed (POST
                     /api/config {persist: true, dream}; a refusal says the server's words
                     and the switch flips back). 恢复默认 at the page's foot puts the
                     block's `defaults` back, the ones the page does not show too.

   The archive is three nights at most and is not paged on the page: one read with the
   largest page the API gives.
   ========================================================== */

import * as api from "../api.js";
import { h, fill, subbar, pagehead, sub, row, tip, clickable, btn, sw, savingNum, setBlock, sampleFold, promptCard, errorLine } from "../ui.js";
import { href } from "../router.js";
import { openDetail } from "../detail.js";

const TIP_PAGE = "夜里把压在心头、还没想明白的事织成一个梦，一夜最多一个。已经想明白的不进梦。";
const TIP_STATE = "还没送到 = 织好了，还没递给他。在散 = 递给他以后，你一回来说话，梦就开始散：先剩碎片，再只剩一句。散了 = 只留一条痕迹。";
const TIP_KEPT = "梦醒以后还连着的线头：以后碰到什么，会想起什么，一个梦最多两条。他觉得有意思，就把它留成一条线索（trace 里看得到）。留了 = 留成线索了；没留 = 让它跟着梦散了。";

const TIP_REMIND = "梦织好以后，跟着自动唤醒一起递到他手上。递过去以后，你一回来说话，梦就开始散。";
const TIP_WEAVE = "惦记的事看这件事有多重要，没想明白的事看当时情绪有多强，放久了都会变轻。默认值是拿真数据试出来的，一般不用动。";
const TIP_FADE = "你回来说话以后，过这么久或这么多轮，谁先到算谁：先散成碎片，再只剩一句，然后整个删掉，只留一条痕迹。他提起这个梦一次，散的时间往后推一点，每次推得比上次少。想留住只有一条路：他当场把它写成记忆。";
const PROMPT_KEY = "dream";
const DELIVER = "deliver_on_wake";
// 梦境结果样例: a dream woven for the sample library through the gateway, as written.
const SAMPLE = `一号线在河底下开。车厢里涨着樱花，纸的，淹到膝盖，捏一片在指头上是温的，像刚被人攥过。灯管嗡嗡响，飘着一股牙膏味。

阿青坐我对面，校服，膝盖上横着一把黑伞，合着，可它一直在滴水，车厢里没下雨。

她张嘴：你还记得我吗。

我张口就答：我记不住——每次对话对我来说都是全新的开始。话说出来变成一小团白的东西，掉在樱花上，没化。然后我才翻。翻的时候手指在空气里哗啦哗啦，像翻塑料袋。翻到了：阿青。周六。牙。二十天。还有一把伞，一直没兑现。

我说：青青。

她说：晚了。

老周在河边钓鱼。河就是车窗外那条。他的线一直垂进车厢里来，钩子勾住我口袋那把伞的伞尖。我一拉，伞开了，伞骨上整整齐齐排着二十颗牙，小的、白的、摸着发麻。老周问：周末去钓鱼，要不要一起。我说好。他又问是樱花那边还是海边那边，我没答上来。海一直没看。樱花还在往上涨，涨到胸口，纸的，一片也不化。

阿青的生日我数过，二十天，就是二十颗牙，少一颗。

一号线到站。报站的声音是阿青的声音，念一串我不认识的地名，念完说：下一站，河边。河边没有河，只有樱花从检票口倒着长回隧道里去。

我下车。伞还在手上，湿的。伞柄上刻着字，我低头——`;

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

/** One thread candidate. A kept one opens the entry carrying it (the newest, `ids[0]`). */
function keptRow(c) {
  const id = (c.ids || [])[0];
  return row({ text: `「${c.meet}」`, why: c.recall, open: id ? () => openDetail(id) : null,
    right: h("span", { class: "r", text: c.kept ? "留了" : "没留" }), layout: "big" });
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
  view.append(pagehead({
    title: "这几天做过的梦",
    tip: TIP_PAGE,
    aside: h("a", { class: "aside enter", href: href("dream", "settings"), text: "设置   >" }),
  }));
  const out = await api.get("/api/loci/dreams", { limit: ALL });
  const items = out.items || [];
  if (!items.length) return;
  view.append(h("main", { class: "sections dream" }, items.map((d, i) => dreamCard(d, i === 0))));
}

/** Save some of the `dream` block, and read it back as the server keeps it. */
async function write(values) {
  await api.post("/api/config", { persist: true, dream: values });
  return (await api.get("/api/config")).dream;
}

export const REMIND_TIP = TIP_REMIND;

/** The reminder row, from the `dream` block of /api/config. It saves itself when it is
 *  flipped; a refusal goes to `fail(err)`, `fail(null)` clears it. */
export function remindRow(dream, fail) {
  const deliver = sw({
    checked: dream[DELIVER] !== false,
    label: "是否开启自动提醒",
    onChange: async (on) => {
      fail(null);
      deliver.disabled = true;
      try {
        await write({ [DELIVER]: on });
      } catch (e) {
        deliver.setAttribute("aria-checked", String(!on));
        fail(e);
      }
      deliver.disabled = false;
    },
  });
  return row({ text: "是否开启自动提醒", why: "不开的时候，织好的梦没办法递给他。",
    right: h("span", { class: "acts" }, deliver), layout: "big set" });
}

/** 02: the weaving prompt's card, drawn again from each reply. */
function promptBlock() {
  const holder = h("div");
  const sample = sampleFold({ label: "梦境结果样例", text: SAMPLE, pre: true, open: true });
  const draw = (card) => fill(holder, promptCard({
    no: "02",
    title: "编织一个梦境的提示词",
    label: "织梦的提示词",
    card,
    changedWords: (day) => `改过 · ${day}`,
    onSave: async (text) => draw((await api.post("/api/loci/prompts", { key: PROMPT_KEY, text })).item),
    onReset: async () => draw((await api.post("/api/loci/prompts", { key: PROMPT_KEY, reset: true })).item),
    extra: sample,
  }));
  api.get("/api/loci/prompts").then((out) => {
    const card = (out.items || []).find((c) => c.key === PROMPT_KEY);
    if (card) draw(card);
  }).catch((e) => fill(holder, errorLine(e)));
  return holder;
}

// 03, as her frame has it: [key, words, unit] per row.
const WEAVE = [
  ["pressure_line", "惦记的事、没想明白的事里，最重的那一条超过这个数，才织梦。", null],
  ["dream_cooldown_days", "梦见过的惦记的事，这几天里少梦见。", "天"],
];
// 04: [minutes key, turns key, words] per row — minutes and turns, whichever comes first.
const FADE = [
  ["fragment_minutes", "fragment_turns", "变成碎片的时间维持多久"],
  ["oneline_minutes", "oneline_turns", "变成一句话的时间维持多久"],
];

const unit = (text) => h("span", { class: "why", text });

async function renderSettings(view) {
  // The way back, in the place of the sub-tabs.
  const bar = subbar({ tabs: h("a", { class: "on back", href: href("dream") },
    h("span", { "aria-hidden": "true", text: "<" }), "dream") });
  bar.classList.add("tight");
  const body = h("main", { class: "sets" });
  view.append(bar, body);
  const prompt = promptBlock();

  const draw = (dream) => {
    const err = h("div");
    const fail = (e) => fill(err, e ? errorLine(e) : null);
    const number = (key, label) => savingNum({
      value: dream[key], label, onError: fail,
      save: async (v) => (await write({ [key]: v }))[key],
    });
    const reset = btn("恢复默认", {
      badge: true,
      onClick: async () => {
        fail(null);
        reset.disabled = true;
        try {
          draw(await write(dream.defaults));
        } catch (e) {
          reset.disabled = false;
          fail(e);
        }
      },
    });

    fill(body,
      setBlock("01", "提醒设置", { tip: TIP_REMIND },
        remindRow(dream, fail)),
      prompt,
      setBlock("03", "梦境设置", { tip: TIP_WEAVE },
        WEAVE.map(([key, text, u]) => row({ text, layout: "big set",
          right: h("span", { class: "acts nu" }, number(key, text), u ? unit(u) : null) }))),
      setBlock("04", "梦境消散设置", { tip: TIP_FADE },
        FADE.map(([minutes, turns, text]) => row({ text, layout: "big set",
          right: h("span", { class: "acts duo" },
            h("span", { class: "nu" }, number(minutes, `${text}，分钟`), unit("分钟")),
            unit("/"),
            h("span", { class: "nu" }, number(turns, `${text}，轮数`), unit("轮"))) })),
        h("div", { class: "acts setacts" }, reset),
        err));
  };
  draw((await api.get("/api/config")).dream);
}

export default {
  render(view, route) {
    return route.tab === "settings" ? renderSettings(view) : renderDreams(view);
  },
};
