/* ==========================================================
   pages/trace.js — what still hangs open, and the buttons that close it

   #/trace        (her frame Trace) the two halves as tabs written as large as a title,
   #/trace/deep   Surface / Deep, one small line under them. Each tab is GET
                  /api/loci/hanging `?part=surface` (awake) or `?part=deep` (asleep), paged
                  with the ‹ › line at the page's foot. Only what can still be done or
                  withdrawn is here: a promise not closed, a live hold, a cue still waiting.
                  Each row: its title, what it is and why it hangs (答应了的 / 条子 / 线索 ·
                  the API's why_words), and its buttons from `actions`:
                    done      做完了   ┐ a promise
                    drop      不做了   ┘
                    withdraw  撤掉     a hold or a cue (a promise waiting on a cue: the
                                       cue comes off, the promise stays)
                  A button is POST /api/loci/trace {id, action}, the model's own trace
                  road; the list is read again after it, on the same page. A click anywhere
                  else on the row opens the detail window.
   ========================================================== */

import * as api from "../api.js";
import { h, pagehead, row, btn, errorLine, fill, pagedList, footPager } from "../ui.js";
import { href } from "../router.js";
import { openDetail } from "../detail.js";

const LINE = "还惦记着的事情，做完的或者撤掉的就不在这儿了。";
const TIP = "三种还挂着的：答应了的 = 他答应过、还没做完的。条子 = 他挂着的一张便条，在等某件事发生，到期限自己撤。线索 = 他埋下的一个提醒，等你哪天说到某句话，就把那条记忆递给他。Surface 是醒着的，更容易被他注意到；Deep 是沉下去的，还挂着，只是平时不太浮上来。";
const HALVES = [
  { part: "surface", label: "Surface", tab: "" },
  { part: "deep", label: "Deep", tab: "deep" },
];

// The three kinds as the board names them, and each button's words.
const KIND = { promise: "答应了的", hold: "条子", cue: "线索" };
const BUTTON = { done: "做完了", drop: "不做了", withdraw: "撤掉" };

/** 「答应了的 · 还有 3 天」; the API's words alone when they already open with the kind
 *  (「条子到 10-12」, 「答应了，没定时间」). */
function whyOf(it) {
  const kind = KIND[it.kind] || "";
  const words = it.why_words || "";
  if (!kind) return words;
  if (!words) return kind;
  return words.startsWith(kind.slice(0, 2)) ? words : `${kind} · ${words}`;
}

function hangingRow(it, again) {
  const err = h("span");
  const buttons = (it.actions || []).filter((a) => BUTTON[a]).map((action) => {
    const b = btn(BUTTON[action], {
      badge: true,
      onClick: async () => {
        for (const x of buttons) x.disabled = true;
        fill(err);
        try {
          await api.post("/api/loci/trace", { id: it.id, action });
          await again();
        } catch (e) {
          for (const x of buttons) x.disabled = false;
          fill(err, errorLine(e));
        }
      },
    });
    return b;
  });
  return row({
    text: it.text ?? `#${it.short}`,
    why: whyOf(it),
    open: () => openDetail(it.id),
    right: h("span", { class: "acts" }, buttons, err),
    layout: "big",
  });
}

export default {
  async render(view, route) {
    const here = HALVES.find((x) => x.tab === (route.tab || "")) || HALVES[0];
    view.append(pagehead({
      tabs: HALVES.map((x) => ({ label: x.label, href: x.tab ? href("trace", x.tab) : href("trace"), on: x === here })),
      tip: TIP,
      caption: LINE,
    }));
    const list = await pagedList({
      load: (q) => api.get("/api/loci/hanging", { part: here.part, ...q }),
      item: (it) => hangingRow(it, () => list.again()),
    });
    if (!list.total) return;
    view.append(h("main", { class: "sections" }, h("section", { "aria-label": here.label }, list.el)));
    footPager(view, list);
  },
};
