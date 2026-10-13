/* ==========================================================
   pages/muse.js — what is not tidied yet, and muse's reminder

   #/muse           (her frame muse) the page's line and a small one under it, 设置 > at
                    the right; then GET /api/loci/muse, two parts each paged on its own:
                      日子  `?part=days`: stretches of days that look like one and have no
                            name — the days, 「N 条 ：<evidence>」, 看看是哪几条？
                      想法  `?part=clusters`: thoughts that look like one thing —
                            「有N条想法像一回事」, the evidence, 看看是哪几条？ / 收起, and
                            the cluster's badge by its `nudge.state`:
                              none   戳一下 -> POST /api/loci/muse/nudge {cluster}
                              poked  戳过了   (the next tool reply tells him, once)
                              seen   已送达
                    看看是哪几条？ unfolds the members under the row, each read through
                    GET /api/loci/bucket/{id} for its title and day, each opening the detail
                    window. The days have no 戳一下: the nudge takes clusters only.
   #/muse/settings  (her frame muse——设置) 01 提醒设置, the `muse` block of GET /api/config:
                    是否开启自动提醒 (poke_on_wake) · 攒了几团提醒 (poke_min_clusters) ·
                    最早的那团等几天才能提醒 (poke_min_age_days). Each saves itself when it
                    is changed (POST /api/config {persist: true, muse}); 恢复默认 puts the
                    defaults the server gives back in and saves them. The three rows are
                    remindRows, which pages/wakeup.js shows as well.
   ========================================================== */

import * as api from "../api.js";
import { h, subbar, pagehead, group, row, btn, sw, savingNum, setBlock, errorLine, fill, clickable, pagedList } from "../ui.js";
import { href } from "../router.js";
import { openDetail } from "../detail.js";

const LINE = "有些日子和想法暂时还没整理";
const HINT = "如果你看到了，可以戳他一下，下一次调用工具的时候，他会知道的。";
const TIPS = {
  days: "有几天的事看着像一段（比如一次出门、一阵子忙），还没被圈起来、起个名字。",
  thoughts: "有几条想法说的像是一回事，还没合成一条概括。",
  remind: "攒够了、放够久了，自动唤醒的时候跟他提一句「有 N 团还没整理」。只说个数，不说内容，他自己去看。你在 muse 页上戳一下，就不用等后两条。",
};
const SHOW = "看看是哪几条？";
const HIDE = "收起";
const NUDGE = { none: "戳一下", poked: "戳过了", seen: "已送达" };

// ---------------------------------------------------------------- the page

/** "09-21" of a YYYY-MM-DD day. */
const md = (day) => String(day || "").slice(5);

function daysOf(it) {
  if (it.start && it.end) return it.start === it.end ? md(it.start) : `${md(it.start)}~${md(it.end)}`;
  if (it.boundary) return md(it.boundary);
  return it.kind_words || "";
}

/** A member's line: its name without the time the store puts in front, else its summary,
 *  else its first line (the detail window's title). */
function titleOf(b) {
  const name = String(b.name || "").replace(/^[\d\- :]+/, "").trim();
  const first = String(b.content || "").split("\n").map((s) => s.trim()).find(Boolean) || "";
  return name || b.summary || first || `#${b.short}`;
}

const members = new Map();

function memberRow(id) {
  const line = h("span", { class: "c", text: `#${id.slice(0, 6)}` });
  const day = h("span", { class: "r" });
  const txt = clickable(h("a", { class: "txt" }, line), () => openDetail(id));
  if (!members.has(id)) members.set(id, api.get(`/api/loci/bucket/${api.seg(id)}`));
  members.get(id).then((b) => {
    line.textContent = titleOf(b);
    day.textContent = b.date || "";
  }).catch(() => members.delete(id));
  return h("div", { class: "item member" }, txt, day);
}

/** A group's row, with 看看是哪几条？ / 收起 unfolding its members right under it. */
function groupRow({ text, evidence, ids, right }) {
  let shown = [];
  const toggle = h("span", { class: "ask", text: SHOW });
  let head = null;
  const flip = () => {
    if (shown.length) {
      for (const el of shown) el.remove();
      shown = [];
      toggle.textContent = SHOW;
      return;
    }
    shown = ids.map(memberRow);
    head.after(...shown);
    toggle.textContent = HIDE;
  };
  head = row({ text, why: [evidence, ids.length ? toggle : null], open: ids.length ? flip : null, right, layout: "big" });
  return head;
}

/** A badge that only says where the nudge stands. */
const said = (state) => h("span", { class: "btn badge quiet", text: NUDGE[state] || NUDGE.poked });

function nudgeButton(it) {
  const state = (it.nudge && it.nudge.state) || "none";
  if (state !== "none") return h("span", { class: "acts" }, said(state));
  const err = h("span");
  const acts = h("span", { class: "acts" });
  const b = btn(NUDGE.none, {
    badge: true,
    onClick: async () => {
      b.disabled = true;
      fill(err);
      try {
        const out = await api.post("/api/loci/muse/nudge", { cluster: it.id });
        fill(acts, said(out.state));
      } catch (e) {
        b.disabled = false;
        fill(err, errorLine(e));
      }
    },
  });
  return fill(acts, b, err);
}

function part(name, item) {
  return pagedList({ load: (q) => api.get("/api/loci/muse", { part: name, ...q }), item });
}

async function renderMuse(view) {
  view.append(pagehead({
    title: LINE,
    caption: HINT,
    aside: h("a", { class: "aside enter", href: href("muse", "settings"), text: "设置   >" }),
  }));
  const [days, clusters] = await Promise.all([
    part("days", (it) => groupRow({
      text: daysOf(it),
      evidence: it.n ? `${it.n} 条 ：${it.evidence_words}` : it.evidence_words,
      ids: it.ids || [],
    })),
    part("clusters", (it) => groupRow({
      text: `有${it.n}条想法像一回事`,
      evidence: it.evidence_words,
      ids: it.ids || [],
      right: nudgeButton(it),
    })),
  ]);
  view.append(h("main", { class: "sections muse" },
    days.total ? group("日子", { tip: TIPS.days }, days.el) : null,
    clusters.total ? group("想法", { tip: TIPS.thoughts }, clusters.el) : null));
}

// ---------------------------------------------------------------- 设置

const unit = (text) => h("span", { class: "why", text });

/** Save some of the `muse` block, and read it back as the server keeps it. */
async function write(values) {
  await api.post("/api/config", { persist: true, muse: values });
  return (await api.get("/api/config")).muse;
}

export const REMIND_TIP = TIPS.remind;

/** The three reminder rows, from the `muse` block of /api/config. Each saves itself when
 *  it is changed; a refusal goes to `fail(err)`, `fail(null)` clears it. */
export function remindRows(muse, fail) {
  const number = (key, label) => savingNum({
    value: muse[key], label, onError: fail,
    save: async (v) => (await write({ [key]: v }))[key],
  });
  const wake = sw({
    checked: !!muse.poke_on_wake,
    label: "是否开启自动提醒",
    onChange: async (on) => {
      fail(null);
      wake.disabled = true;
      try {
        await write({ poke_on_wake: on });
      } catch (e) {
        wake.setAttribute("aria-checked", String(!on));
        fail(e);
      }
      wake.disabled = false;
    },
  });
  return [
    row({ text: "是否开启自动提醒", why: "不开的时候，只有你手动戳他，他才会知道。",
      right: h("span", { class: "acts" }, wake), layout: "big set" }),
    row({ text: "攒了几团提醒", why: "日子加想法一起算。",
      right: h("span", { class: "acts nu" }, number("poke_min_clusters", "攒了几团提醒"), unit("团")), layout: "big set" }),
    row({ text: "最早的那团等几天才能提醒",
      right: h("span", { class: "acts nu" }, number("poke_min_age_days", "最早的那团等几天才能提醒"), unit("天")), layout: "big set" }),
  ];
}

async function renderSettings(view) {
  // The way back, in the place of the sub-tabs.
  const bar = subbar({ tabs: h("a", { class: "on back", href: href("muse") },
    h("span", { "aria-hidden": "true", text: "<" }), "muse") });
  bar.classList.add("tight");
  const body = h("main", { class: "sets" });
  view.append(bar, body);

  const draw = (muse) => {
    const err = h("div");
    const fail = (e) => fill(err, e ? errorLine(e) : null);
    const reset = btn("恢复默认", {
      badge: true,
      onClick: async () => {
        fail(null);
        reset.disabled = true;
        try {
          draw(await write(muse.defaults));
        } catch (e) {
          reset.disabled = false;
          fail(e);
        }
      },
    });
    fill(body, setBlock("01", "提醒设置", { tip: REMIND_TIP },
      remindRows(muse, fail),
      h("div", { class: "acts setacts" }, reset),
      err));
  };
  draw((await api.get("/api/config")).muse);
}

export default {
  render(view, route) {
    return route.tab === "settings" ? renderSettings(view) : renderMuse(view);
  },
};
