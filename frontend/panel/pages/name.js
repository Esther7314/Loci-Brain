/* ==========================================================
   pages/name.js — the names the table knows, and the ones waiting to be recognised

   #/name?kind=<kind>  (board name) GET /api/loci/names: only the names the table knows
                       and gives a kind (one it knows without a kind waits under 待认).
                       The kinds row on top (the person kind, 人名, first and chosen when
                       the address names none; a kind shows only when a name has it; on
                       the phone the row slides sideways). One row per name: the name,
                       「也叫 …」 its aliases, 「出现 N 次」 at the right; most mentioned
                       first, paged. A row opens the name card (namecard.js). At the
                       right of the kinds: 「待认 N ›」 (pending_count) with its i.
   #/name/pending      (board name-pending) GET /api/loci/names/pending: the names the
                       table does not know yet or knows without a kind, newest first seen
                       on top, paged. Each row: the name, 「第一次出现在：<the entry's
                       title>」 (the row opens that entry in the detail window) and, when
                       the API has a `guess`, 「看着像 <kind>」; then the buttons, each one
                       POST /api/loci/names/action:
                         不是名字    not_person
                         跟谁是一个  merge — a box (with the known names as suggestions),
                                    取消 · 保存
                         是 <kind>  set_kind with the guess, in one click (only with a guess)
                         是… ▾      set_kind — the kinds as a menu, + 新类别 a box for a new
                                    one (a drop-down on the web, a sheet on the phone)
                       A name recognised leaves the page: the page is read again, with the
                       server's `note` on top.
   ========================================================== */

import * as api from "../api.js";
import { h, fill, subbar, row, tip, note, pagedList, btn, errorLine } from "../ui.js";
import { href, refresh } from "../router.js";
import { openDetail } from "../detail.js";
import { openNameCard, kindLabel, kindsOf, KIND_PERSON } from "../namecard.js";

const TIP_PENDING = "待认 = 聊天里冒出来、但还不知道是谁或是什么的名字，等你认一下。";

let flash = "";       // the last write's note, shown once on the page read after it
let boxSeq = 0;

function kindHref(kind) {
  return `${href("name")}?kind=${encodeURIComponent(kind)}`;
}

// ------------------------------------------------------------ the names the table knows

async function renderNames(view, route) {
  const head = await api.get("/api/loci/names", { limit: 1 });
  // The kinds some known name has, the person kind first.
  const kinds = (head.kinds || []).map((k) => k.kind).filter(Boolean)
    .sort((a, b) => (b === KIND_PERSON) - (a === KIND_PERSON));
  const asked = route.params.get("kind") || "";
  const kind = kinds.includes(asked) ? asked : (kinds[0] || "");

  const pending = h("span", { class: "aside", style: { display: "flex", alignItems: "center", gap: "6px" } },
    h("a", { href: href("name", "pending"), text: `待认 ${head.pending_count || 0} ›` }),
    tip(TIP_PENDING));
  const bar = subbar({
    tabs: kinds.length ? kinds.map((k) => ({ label: kindLabel(k), href: kindHref(k), on: k === kind })) : h("span"),
    aside: pending,
  });
  const tabs = bar.querySelector(".tabs");
  if (tabs) tabs.classList.add("slide");
  view.append(bar);

  const list = await pagedList({
    load: (q) => api.get("/api/loci/names", { ...q, kind }),
    item: (it) => row({
      text: it.name,
      why: (it.aliases || []).length ? `也叫 ${it.aliases.join("、")}` : null,
      open: () => openNameCard(it.name),
      right: h("span", { class: "r", text: `出现 ${it.n} 次` }),
    }),
  });
  if (!list.total) return;
  view.append(h("main", { class: "sections" },
    h("section", { "aria-label": kindLabel(kind) }, h("div", { class: "subs" }, list.el))));
}

// ------------------------------------------------------------ 待认的

/** The kinds menu of 是…: the kinds, then + 新类别. A drop-down under the button on the
 *  web, a sheet from the bottom on the phone (panel.css .menu.kinds). */
function kindMenu(anchor, kinds, { onPick, onNew, onClose }) {
  const scrim = h("div", { class: "menu-scrim", "aria-hidden": "true" });
  const pick = (fn) => () => { close(); fn(); };
  const menu = h("div", { class: "menu kinds", role: "menu", "aria-label": "是什么" },
    h("div", { class: "card" },
      kinds.map((k) => h("button", { class: "mi", role: "menuitem", type: "button",
        on: { click: pick(() => onPick(k)) } }, kindLabel(k))),
      h("hr"),
      h("button", { class: "mi dim", role: "menuitem", type: "button", on: { click: pick(onNew) } }, "+ 新类别")),
    h("button", { class: "mi cancel", type: "button", on: { click: () => close() } }, "取消"));
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const onDown = (e) => { if (!menu.contains(e.target) && !anchor.contains(e.target)) close(); };
  let open = true;
  function close() {
    if (!open) return;
    open = false;
    scrim.remove();
    menu.remove();
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("mousedown", onDown);
    onClose();
  }
  scrim.addEventListener("click", () => close());
  document.addEventListener("keydown", onKey);
  document.addEventListener("mousedown", onDown);
  const first = menu.querySelector(".mi");
  requestAnimationFrame(() => first && first.focus({ preventScroll: true }));
  return { nodes: [scrim, menu], close };
}

function pendingRow(it, ctx) {
  const first = it.first;
  const why = [
    first ? h("span", null, "第一次出现在：", h("span", { class: "lnk", text: first.text || `#${first.short}` })) : null,
    it.guess ? `看着像 ${kindLabel(it.guess)}` : null,
  ];
  const slot = h("div", { class: "namebox" });
  const actsBox = h("span", { class: "acts" });
  const item = row({
    text: it.name,
    why,
    open: first ? () => openDetail(first.id) : null,
    right: actsBox,
    layout: "wide",
  });
  item.classList.add("pending");
  item.append(slot);

  const act = async (body, button, msgBox) => {
    if (button) button.disabled = true;
    try {
      const out = await api.post("/api/loci/names/action", { name: it.name, ...body });
      flash = out && out.note ? out.note : "";
      await refresh();
    } catch (e) {
      if (button) button.disabled = false;
      fill(msgBox || slot, errorLine(e));
    }
  };

  /** A box under the row for one typed value: 跟谁是一个 (a name) or + 新类别 (a kind). */
  const box = (label, suggestions, send) => {
    const listId = `nm-list-${++boxSeq}`;
    const input = h("input", { class: "inp", style: { flex: "1 1 200px", minWidth: "0" }, "aria-label": label, list: listId });
    const msg = h("div", { style: { flexBasis: "100%" } });
    const save = btn("保存", { dark: true });
    save.addEventListener("click", () => { const v = input.value.trim(); if (v) act(send(v), save, msg); });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save.click(); } });
    fill(slot,
      h("p", { class: "cap", text: label }),
      h("div", { class: "namebox-row" }, input,
        h("datalist", { id: listId }, suggestions.map((v) => h("option", { value: v }))),
        h("span", { class: "acts" }, btn("取消", { onClick: () => fill(slot) }), save)),
      msg);
    input.focus();
  };

  const notName = btn("不是名字");
  notName.addEventListener("click", () => act({ action: "not_person" }, notName));
  const same = btn("跟谁是一个");
  same.addEventListener("click", () => box("跟谁是一个", ctx.names, (v) => ({ action: "merge", target: v })));
  const isBtn = btn("是… ▾");
  isBtn.setAttribute("aria-haspopup", "menu");
  isBtn.setAttribute("aria-expanded", "false");
  let menu = null;
  isBtn.addEventListener("click", () => {
    if (menu) { menu.close(); return; }
    isBtn.classList.add("dark");
    isBtn.textContent = "是… ▴";
    isBtn.setAttribute("aria-expanded", "true");
    menu = kindMenu(isBtn, ctx.kinds, {
      onPick: (k) => act({ action: "set_kind", kind: k }, isBtn),
      onNew: () => box("+ 新类别", [], (v) => ({ action: "set_kind", kind: v })),
      onClose: () => {
        menu = null;
        isBtn.classList.remove("dark");
        isBtn.textContent = "是… ▾";
        isBtn.setAttribute("aria-expanded", "false");
      },
    });
    item.append(...menu.nodes);
  });
  let guessed = null;
  if (it.guess) {
    guessed = btn(`是 ${kindLabel(it.guess)}`, { dark: true });
    guessed.addEventListener("click", () => act({ action: "set_kind", kind: it.guess }, guessed));
  }
  fill(actsBox, notName, same, guessed, isBtn);
  return item;
}

async function renderPending(view) {
  const count = h("span", { class: "why" });
  view.append(subbar({
    tabs: h("span", { style: { display: "contents" } }, h("a", { href: href("name"), text: "‹ name" }),
      h("span", { style: { color: "var(--ink)" }, text: "待认的" })),
    aside: count,
  }));
  if (flash) { view.append(note(flash), h("div", { style: { height: "16px" } })); flash = ""; }

  // The kinds for 是… and the known names for 跟谁是一个 (suggestions; the largest page).
  const ctx = { kinds: kindsOf(null), names: [] };
  const known = api.get("/api/loci/names", { limit: 50 }).then((r) => {
    ctx.kinds.splice(0, ctx.kinds.length, ...kindsOf(r));
    ctx.names.push(...(r.items || []).map((it) => it.name));
  }).catch(() => { /* suggestions only */ });

  const list = await pagedList({
    load: (q) => api.get("/api/loci/names/pending", q),
    item: (it) => pendingRow(it, ctx),
  });
  await known;
  count.textContent = `${list.total} 个 · 认完就离开这页`;
  if (!list.total) return;
  view.append(h("main", { class: "sections" },
    h("section", { "aria-label": "待认" }, h("div", { class: "subs" }, list.el))));
}

export default {
  render(view, route) {
    return route.tab === "pending" ? renderPending(view) : renderNames(view, route);
  },
};
