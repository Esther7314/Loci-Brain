/* ==========================================================
   app.js — the panel's entry: which module serves which page, and the start

   Plain ES modules, no build step; served by GET /loci/panel/<path> (web/loci_pages.py).
   frontend/loci.html is only the shell that loads this file and panel.css.

   HOW A PAGE MODULE IS WRITTEN (pages/breath.js is the worked example)

   1. Registers: one file under pages/, one line in FRAMED below (id = the address in
      router.NAV). Its default export is {render(view, route)}: `view` is an empty
      element under the header to fill, `route` is {page, tab, rest, params} of the
      address (#/<page>/<tab>/<more…>?k=v). Sub-tabs are addresses: href("grow",
      "slices") -> #/grow/slices, so each one can be linked and reloaded. render may be
      async; a throw is shown as the error's words, a 401 goes to the login page.
   2. Gets data: through api.js only — api.get(path, query) / api.post(path, body). A
      write is a JSON POST (the server checks the origin itself). Errors arrive as
      ApiError with the server's own words in `.message`; show them with errorLine(e).
   3. Renders with ui.js: subbar({tabs, tip, aside}) for the row under the header,
      group(label, {tip}, …subs) → sub(title, {tip}, …rows) → row({text, why, open,
      right, layout}) for the canvas's .grp/.subs/.item/.txt/.why system; idTag (a
      row's id at its right), numberSubs (01 02 … before the sub-titles),
      moreLine, tip, btn, sw, radio, inp, num, customBox for the rest. A group or sub
      with nothing in it returns null and is simply not shown — an empty block does not
      appear. Colours, type and spacing are the tokens on :root in panel.css; use them
      rather than raw values. Words come from the page's board or from the API's *_words; never make
      Chinese up.
   4. Opens the detail window: openDetail(id) from detail.js on any row of a memory
      (row({open: () => openDetail(id)})); openDetail(id, {layer: "source"}) opens it
      on its 来源. After the window wrote something and closed, the page is rendered again.
   5. Pages a list: pagedList({load: (q) => api.get(path, q), item: (it) => row(…)}).
      5 to a page unless `limit` says more, ‹ 1 / N › under the list, the first page's
      as_of kept across pages; it resolves to {el, nav, total, reply, reload} — total 0
      means leave the block out.

   Layout: web per the 1280 boards, phone per the 440 boards from 640px down
   (panel.css). Check both widths, and that nothing scrolls sideways at 440.
   ========================================================== */

import { start } from "./router.js";
import { onSubject } from "./detail.js";
import { openNameCard } from "./namecard.js";

// A nav page's module, loaded the first time its address is opened. A nav page missing
// here shows the placeholder.
const FRAMED = {
  breath: () => import("./pages/breath.js"),
  grow: () => import("./pages/grow.js"),
  recall: () => import("./pages/recall.js"),
  regrow: () => import("./pages/regrow.js"),
  muse: () => import("./pages/muse.js"),
  trace: () => import("./pages/trace.js"),
  dream: () => import("./pages/dream.js"),
  name: () => import("./pages/name.js"),
  present: () => import("./pages/present.js"),
  setting: () => import("./pages/setting.js"),
};

// A name in the detail window opens its name card. detail.js does not import namecard.js
// itself: the card opens memories in the detail window, and the two stay one-way.
onSubject(openNameCard);

// Pages without the header.
const STANDALONE = {
  login: () => import("./pages/login.js"),
  forgot: () => import("./pages/login.js"),
};

start(document.getElementById("app"), {
  framed: FRAMED,
  standalone: STANDALONE,
  placeholder: () => import("./pages/placeholder.js"),
});
