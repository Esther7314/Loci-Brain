/* ==========================================================
   pages/name.js — the names the table knows, and the ones waiting to be recognised

   #/name?kind=<kind>  (board name) GET /api/loci/names: only the names the table knows
                       and gives a kind (one it knows without a kind waits under 待认).
                       The kinds row on top (the person kind, 人, first and chosen when
                       the address names none; a kind shows only when a name has it; on
                       the phone the row slides sideways). One row per name: the name,
                       「也叫 …」 its aliases, 「出现 N 次」 at the right; most mentioned
                       first, paged. A row opens the name card (namecard.js). At the
                       right of the kinds: 「待认 N ›」 (pending_count) with its i.
   #/name/pending      (board name-pending) GET /api/loci/names/pending: the names the
                       table does not know yet or knows without a kind, newest first seen
                       on top, paged. Each row: the name, 「第一次出现在：<the entry's
                       title>」 (the row opens that entry in the detail window) and, when
                       the API has a guess, 「看着像 <kind>」 or 「看着像 <card>」 (`same`:
                       a card it may be another spelling of); then two buttons, each
                       choice POST /api/loci/names/action. The one outside takes the
                       guess, 更多 is a glass list (ui.pick) with the other three:
                         新增   set_kind — a card of the guessed kind in one click; with
                                no kind guessed, the kinds under the row to choose from
                         合并   merge — outside, into the guessed card in one click; in
                                the list, a box under the row (the known names as
                                suggestions), 取消 · 保存
                         更改   the guess is wrong: another card (the same box) when a
                                card was guessed, another kind (the kinds under the row,
                                + 新类别 a box for a new one) when a kind was
                         忽略   not_person — no card, and it is not listed again
                       A name recognised leaves the page: the page is read again, with the
                       server's `note` on top.
   ========================================================== */

import * as api from "../api.js";
import { h, fill, subbar, row, tip, note, pagedList, footPager, btn, pick, errorLine } from "../ui.js";
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
  footPager(view, list);
}

// ------------------------------------------------------------ 待认的

function pendingRow(it, ctx) {
  const first = it.first;
  const why = [
    first ? h("span", null, "第一次出现在：", h("span", { class: "lnk", text: first.text || `#${first.short}` })) : null,
    it.same ? `看着像 ${it.same}` : (it.guess ? `看着像 ${kindLabel(it.guess)}` : null),
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

  /** A box under the row for one typed value: 合并 (a name) or + 新类别 (a kind). */
  const box = (label, suggestions, send) => {
    const listId = `nm-list-${++boxSeq}`;
    const input = h("input", { class: "inp", style: { flex: "1 1 200px", minWidth: "0" }, "aria-label": label, list: listId });
    const msg = h("div", { style: { flexBasis: "100%" } });
    const save = btn("保存", { badge: true, dark: true });
    save.addEventListener("click", () => { const v = input.value.trim(); if (v) act(send(v), save, msg); });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save.click(); } });
    fill(slot,
      h("p", { class: "cap", text: label }),
      h("div", { class: "namebox-row" }, input,
        h("datalist", { id: listId }, suggestions.map((v) => h("option", { value: v }))),
        h("span", { class: "acts" }, btn("取消", { badge: true, onClick: () => fill(slot) }), save)),
      msg);
    input.focus();
  };

  /** The kinds under the row, to open this name's card as one of them: a button a kind,
   *  then + 新类别 (a box for a new one) and 取消. */
  const kindsLine = (label) => {
    const msg = h("div", { style: { flexBasis: "100%" } });
    const kinds = ctx.kinds.map((k) => {
      const b = btn(kindLabel(k), { badge: true });
      b.addEventListener("click", () => act({ action: "set_kind", kind: k }, b, msg));
      return b;
    });
    fill(slot,
      h("p", { class: "cap", text: label }),
      h("div", { class: "namebox-row" }, kinds,
        btn("+ 新类别", { badge: true, onClick: () => box("+ 新类别", [], (v) => ({ action: "set_kind", kind: v })) }),
        btn("取消", { badge: true, onClick: () => fill(slot) })),
      msg);
    kinds[0].focus();
  };

  const mergeInto = (name) => ({ action: "merge", target: name });
  const addCard = (button) => (it.guess ? act({ action: "set_kind", kind: it.guess }, button) : kindsLine("新增"));
  // The button outside takes the guess: 合并 into the guessed card, else 新增.
  const take = btn(it.same ? "合并" : "新增", { badge: true });
  take.addEventListener("click", () => (it.same ? act(mergeInto(it.same), take) : addCard(take)));
  const more = pick({
    cls: "btn badge", label: "更多", placeholder: "更多", act: true,
    options: [
      it.same ? ["add", "新增"] : ["merge", "合并"],
      it.same || it.guess ? ["change", "更改"] : null,
      ["ignore", "忽略"],
    ].filter(Boolean),
    onChange: (v) => {
      const button = more.querySelector("button");
      if (v === "add") addCard(button);
      else if (v === "merge") box("合并", ctx.names, mergeInto);
      else if (v === "change") { if (it.same) box("更改", ctx.names, mergeInto); else kindsLine("更改"); }
      else act({ action: "not_person" }, button);
    },
  });
  fill(actsBox, take, more);
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

  // The kinds to choose from and the known names for 合并 (suggestions; the largest page).
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
  footPager(view, list);
}

export default {
  render(view, route) {
    return route.tab === "pending" ? renderPending(view) : renderNames(view, route);
  },
};
