/* ==========================================================
   pages/regrow.js — regrow/fold: how the library reshaped itself, and what looks doubled

   #/regrow          (board changes, note n7) GET /api/loci/changes/recent, paged: the
                     last two weeks only (the server cuts them), newest first, each page's
                     rows grouped under their day (MM-DD on the left). A row: the entry
                     (a period with its span in front), and what happened to it in the
                     API's words (改了一版 · N 条合成一条 · 圈了一段日子 · 钉上门口 ·
                     依据变了，…). Read only, no buttons; a row opens the detail window.
   #/regrow/similar  「疑似重复」 (no board; the owner moved the similarity page here) GET
                     /api/loci/similar: the suspected-duplicate pairs above the server's
                     line, five to a page in this same row system. A row: one side's label,
                     the other's under it (opens that one), the score; the line opens the
                     first. 「留着」 is POST /api/loci/similar/action {action: "keep"}: the
                     server stores nothing for it, so the pair leaves this page only until
                     the page is loaded again. Sinking one of the pair is not offered: whether
                     two merge is the model's call.
   ========================================================== */

import * as api from "../api.js";
import { h, subbar, group, row, btn, errorLine, fill, clickable, pagedList } from "../ui.js";
import { href } from "../router.js";
import { openDetail } from "../detail.js";

const LINE = "看看最近发生了什么变化？";
const TIP = "regrow = 他把一条记忆重写一版。fold = 收拢：几条合成一条概括，或者把几天圈成一段日子。都是他自己做的，这页只给你看，只放最近两周。";
const SIMILAR = "疑似重复";
const KEEP = "留着";

function tabs(on) {
  return [
    { label: LINE, href: href("regrow"), on: on === "changes" },
    { label: SIMILAR, href: href("regrow", "similar"), on: on === "similar" },
  ];
}

const open = (id) => () => openDetail(id);

/** "10-07" of a local ISO stamp. */
function monthDay(iso) {
  const m = /^\d{4}-(\d{2})-(\d{2})/.exec(String(iso || ""));
  return m ? `${m[1]}-${m[2]}` : "";
}

function changeRow(it) {
  const entry = it.entry || {};
  const title = entry.text ?? `#${entry.short}`;
  return row({
    text: it.kind === "period" && it.span ? `${it.span} · ${title}` : title,
    why: it.words,
    open: open(entry.id),
  });
}

/** One page of changes as day groups, newest day first (the rows come newest first). */
function byDay(items) {
  const days = [];
  for (const it of items) {
    const day = monthDay(it.at);
    if (!days.length || days[days.length - 1].day !== day) days.push({ day, items: [] });
    days[days.length - 1].items.push(it);
  }
  return days.map((d) => group(d.day, {}, h("div", null, d.items.map(changeRow))));
}

async function renderChanges(view) {
  view.append(subbar({ tabs: tabs("changes"), tip: TIP }));
  const list = await pagedList({
    load: (q) => api.get("/api/loci/changes/recent", q),
    items: byDay,
  });
  if (!list.total) return;
  list.el.classList.add("days");
  view.append(h("main", null, list.el));
}

// Pairs kept on this visit: the server does not store 留着, so they are left out here.
const kept = new Set();
const pairKey = (p) => [p.a.id, p.b.id].sort().join("|");

async function renderSimilar(view) {
  view.append(subbar({ tabs: tabs("similar") }));
  const reply = await api.get("/api/loci/similar");
  const pairs = () => (reply.pairs || []).filter((p) => !kept.has(pairKey(p)));
  let list = null;

  const pairRow = (p) => {
    const err = h("span");
    const other = clickable(h("span", { class: "lnk", text: p.b.label || `#${p.b.short}` }), (e) => {
      e.stopPropagation();
      openDetail(p.b.id);
    });
    const keep = btn(KEEP, {
      onClick: async () => {
        keep.disabled = true;
        fill(err);
        try {
          await api.post("/api/loci/similar/action", { action: "keep", a: p.a.id, b: p.b.id });
          kept.add(pairKey(p));
          if (!pairs().length) fill(view, subbar({ tabs: tabs("similar") }));
          else await list.again();
        } catch (e) {
          keep.disabled = false;
          fill(err, errorLine(e));
        }
      },
    });
    return row({
      text: p.a.label || `#${p.a.short}`,
      why: [other, String(p.score)],
      open: open(p.a.id),
      right: h("span", { class: "acts" }, keep, err),
    });
  };

  list = await pagedList({
    load: async ({ offset, limit }) => {
      const all = pairs();
      const next = offset + limit < all.length ? offset + limit : null;
      return { items: all.slice(offset, offset + limit), total: all.length, offset, limit, next_offset: next };
    },
    item: pairRow,
  });
  if (!list.total) return;
  view.append(h("main", null, list.el));
}

export default {
  render(view, route) {
    return route.tab === "similar" ? renderSimilar(view) : renderChanges(view);
  },
};
