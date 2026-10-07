/* ==========================================================
   router.js — one address per page and sub-tab

   The address lives in the hash, so any page can be linked and reloaded and the server
   serves one page (/loci) for all of them:

       #/<page>                  a top-nav page             #/breath
       #/<page>/<tab>            one of its sub-tabs        #/breath/surface
       #/<page>/<tab>/<more…>    deeper, the page's own     #/name/pending
       …?k=v                     the page's own parameters  #/recall?query=看牙

   A page module is loaded the first time its address is opened. The ten top-nav pages
   are framed by the header; the login pages stand alone. A nav page with no module yet
   shows a placeholder.
   ========================================================== */

import { h, header, errorLine, fill } from "./ui.js";

/** The ten pages, in the canvas header's order. `id` is the address, `label` the word
 *  shown. */
export const NAV = [
  { id: "breath", label: "breath" },
  { id: "grow", label: "grow" },
  { id: "recall", label: "recall" },
  { id: "regrow", label: "regrow/fold" },
  { id: "muse", label: "muse" },
  { id: "trace", label: "trace" },
  { id: "dream", label: "dream" },
  { id: "name", label: "name" },
  { id: "present", label: "present" },
  { id: "setting", label: "setting" },
];

const HOME = "breath";

/** The address of a page, a sub-tab, and deeper: href("breath", "surface"). */
export function href(page, ...rest) {
  const parts = [page, ...rest].filter((p) => p !== undefined && p !== null && p !== "");
  return `#/${parts.map((p) => encodeURIComponent(String(p))).join("/")}`;
}

/** {page, tab, rest, params} of a hash. `tab` is "" on a page's first sub-tab. */
export function parse(hash) {
  const raw = String(hash || "").replace(/^#\/?/, "");
  const [path, query] = raw.split("?", 2);
  const parts = path.split("/").filter(Boolean).map((p) => {
    try { return decodeURIComponent(p); } catch (_) { return p; }
  });
  return { page: parts[0] || "", tab: parts[1] || "", rest: parts.slice(1), params: new URLSearchParams(query || "") };
}

/** Go to an address (adds a history entry). */
export function go(address) {
  if (location.hash === address) render(); else location.hash = address;
}

let table = { framed: {}, standalone: {}, placeholder: null };
let root = null;
let seq = 0;

/** Re-render the page shown, keeping the scroll — after a write that changed what it
 *  shows (the detail window sends `loci:changed`). */
export async function refresh() {
  const y = window.scrollY;
  await render();
  window.scrollTo(0, y);
}

async function render() {
  const mine = ++seq;
  const route = parse(location.hash);
  const framed = NAV.some((p) => p.id === route.page);
  const loader = framed ? table.framed[route.page] : table.standalone[route.page];
  if (!framed && !loader) {
    location.replace(href(HOME));
    return;
  }
  const outlet = h("div", { class: "outlet" });
  const view = framed
    ? h("div", { class: "frame" }, header(NAV.map((p) => ({ ...p, href: href(p.id) })), route.page), outlet)
    : outlet;
  let mod;
  try {
    mod = (await (loader || table.placeholder)()).default;
  } catch (e) {
    if (mine !== seq) return;
    fill(root, view);
    fill(outlet, errorLine(e));
    return;
  }
  if (mine !== seq) return;
  fill(root, view);
  document.title = framed ? `${NAV.find((p) => p.id === route.page).label} · Loci brain` : "Loci brain";
  try {
    await mod.render(outlet, route);
  } catch (e) {
    if (mine !== seq || (e && e.status === 401)) return;
    outlet.append(errorLine(e));
  }
}

/** Start: `framed` maps a nav page's id to its module loader, `standalone` the pages
 *  without the header (login), `placeholder` the module shown for a nav page not built
 *  yet. */
export function start(el, { framed, standalone, placeholder }) {
  root = el;
  table = { framed: framed || {}, standalone: standalone || {}, placeholder };
  window.addEventListener("hashchange", () => { window.scrollTo(0, 0); render(); });
  document.addEventListener("loci:changed", () => { refresh(); });
  if (!parse(location.hash).page) location.replace(href(HOME));
  else render();
}
