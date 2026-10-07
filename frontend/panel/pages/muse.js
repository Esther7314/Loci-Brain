/* ==========================================================
   pages/muse.js — what is not tidied yet, and muse's reminder

   #/muse           (board muse, note n7) GET /api/loci/muse, two parts each paged on its
                    own:
                      日子  `?part=days`: stretches of days that look like one and have no
                            name — the days, 「N 条 ：<evidence>」, 看是哪几条
                      想法  `?part=clusters`: thoughts that look like one thing —
                            「有N条想法像一回事」, the evidence, 看是哪几条 / 收起, and the
                            cluster's 「戳一下」 by its `nudge.state`:
                              none   戳一下 -> POST /api/loci/muse/nudge {cluster}
                              poked  戳过了   (the next tool reply tells him, once)
                              seen   他看到了
                    看是哪几条 unfolds the members under the row, each read through
                    GET /api/loci/bucket/{id} for its title and day, each opening the detail
                    window. The days have no 戳一下: the nudge takes clusters only.
   #/muse/settings  (board muse-settings) the 提醒 block of GET /api/config `muse`:
                    醒来的时候提一句 (poke_on_wake) · 攒够几团才提 (poke_min_clusters) ·
                    最老的放了几天才提 (poke_min_age_days). 保存 is POST /api/config
                    {persist: true, muse}; 恢复默认 puts the defaults the server gives back
                    in and saves them.
   ========================================================== */

import * as api from "../api.js";
import { h, subbar, group, row, btn, sw, num, errorLine, fill, clickable, pagedList } from "../ui.js";
import { href } from "../router.js";
import { openDetail } from "../detail.js";

const LINE = "有些日子、想法还没整理";
const HINT = "如果你看到了，可以戳他一下，下一次调用工具的时候，他会知道的。";
const SETTINGS = "高级设置";
const TIPS = {
  days: "有几天的事看着像一段（比如一次出门、一阵子忙），还没被圈起来、起个名字。",
  thoughts: "有几条想法说的像是一回事，还没合成一条概括。",
  remind: "攒够了、放够久了，自动唤醒的时候跟他提一句「有 N 团还没整理」。只说个数，不说内容，他自己去看。",
};
const SHOW = "看是哪几条";
const HIDE = "收起";
const NUDGE = { none: "戳一下", poked: "戳过了", seen: "他看到了" };

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

/** A group's row, with 看是哪几条 / 收起 unfolding its members right under it. */
function groupRow({ text, evidence, ids, right }) {
  let shown = [];
  const toggle = h("span", { class: "lnk", text: SHOW });
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
  head = row({ text, why: [evidence, ids.length ? toggle : null], open: ids.length ? flip : null, right });
  return head;
}

function nudgeButton(it) {
  const state = (it.nudge && it.nudge.state) || "none";
  if (state !== "none") return h("span", { class: "acts" }, h("span", { class: "btn quiet", text: NUDGE[state] || NUDGE.poked }));
  const err = h("span");
  const acts = h("span", { class: "acts" });
  const b = btn(NUDGE.none, {
    onClick: async () => {
      b.disabled = true;
      fill(err);
      try {
        const out = await api.post("/api/loci/muse/nudge", { cluster: it.id });
        fill(acts, h("span", { class: "btn quiet", text: NUDGE[out.state] || NUDGE.poked }));
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
  view.append(subbar({
    tabs: h("span", { text: LINE }),
    aside: h("span", { class: "aside", style: "display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 20px" },
      h("span", { class: "why", text: HINT }),
      h("a", { href: href("muse", "settings"), text: SETTINGS })),
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
  view.append(h("main", { class: "sections", style: "--gl-w: 96px" },
    days.total ? group("日子", { tip: TIPS.days }, days.el) : null,
    clusters.total ? group("想法", { tip: TIPS.thoughts }, clusters.el) : null));
}

// ---------------------------------------------------------------- 高级设置

async function renderSettings(view) {
  view.append(subbar({ tabs: h("span", { style: "display: contents" },
    h("a", { href: href("muse"), text: "‹ muse" }), h("span", { text: SETTINGS })) }));
  const body = h("main", { class: "sections" });
  view.append(body);

  const draw = (muse) => {
    const wake = sw({ checked: !!muse.poke_on_wake, label: "醒来的时候提一句" });
    const many = num({ value: String(muse.poke_min_clusters), "aria-label": "攒够几团" });
    const old = num({ value: String(muse.poke_min_age_days), "aria-label": "放了几天" });
    const err = h("div");
    const save = async (values, buttons) => {
      for (const b of buttons) b.disabled = true;
      fill(err);
      try {
        await api.post("/api/config", { persist: true, muse: values });
        const fresh = await api.get("/api/config");
        draw(fresh.muse);
      } catch (e) {
        for (const b of buttons) b.disabled = false;
        fill(err, errorLine(e));
      }
    };
    const reset = btn("恢复默认");
    const keep = btn("保存", { dark: true });
    reset.addEventListener("click", () => save(muse.defaults, [reset, keep]));
    keep.addEventListener("click", () => save({
      poke_on_wake: wake.getAttribute("aria-checked") === "true",
      poke_min_clusters: many.value.trim(),
      poke_min_age_days: old.value.trim(),
    }, [reset, keep]));

    fill(body, group("提醒", { tip: TIPS.remind }, h("div", null,
      row({ text: "醒来的时候提一句", why: "没开自动唤醒的话，就只有你戳了他才知道",
        right: h("span", { class: "acts" }, wake), layout: "set" }),
      row({ text: "攒够几团才提", why: "日子和想法加在一起",
        right: h("span", { class: "acts" }, many, h("span", { class: "why", text: "团" })), layout: "set" }),
      row({ text: "最老的放了几天才提",
        right: h("span", { class: "acts" }, old, h("span", { class: "why", text: "天" })), layout: "set" }),
      h("p", { class: "why setnote", text: "你在 muse 页上戳一下，就不用等这两条：他下一次调用工具的时候就会知道。" }),
      h("div", { class: "acts setacts" }, reset, keep),
      err)));
  };
  draw((await api.get("/api/config")).muse);
}

export default {
  render(view, route) {
    return route.tab === "settings" ? renderSettings(view) : renderMuse(view);
  },
};
