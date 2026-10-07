/* ==========================================================
   namecard.js — the name card: one name, what it hangs on, where it appears

   Any name anywhere opens it with `openNameCard(name)` (the names page's rows, the
   subjects in the detail window). The same window as the detail window (boards
   name-card, name-card-phone): over the dimmed page on the web, a card rising from the
   bottom on the phone. One window at a time: opening a memory from here closes the card
   and opens the detail window.

   GET /api/loci/names/{name}?limit=3 — the canvas shows three memories and 「还有 N 条」.
     head      the name · 编辑 · close
     meta      its kind · 出现 N 次 (memories.total) · 也叫 its aliases
     什么关系   the names it hangs on (present_in, member_of), each opening its own card
     要注意的   the MIND entry filed as its card; opens it in the detail window
     出现过的记忆 its memories, title and day, each opening the detail window
   A block with nothing in it does not appear.

   编辑 (board name-card-phone-edit; a drop-down on the web, a sheet on the phone) offers
   正式名字 / 并到别的名字 / 改类别 — a box for the name (the known names suggested for a
   merge), or the kinds as choices with + 新类别 and a box — with 取消 · 保存, and one
   POST /api/loci/names/action: rename {name, target} · merge {name, target} · set_kind
   {name, kind}. The server's `note` is shown after a write, the card then shows the name
   it went to, and closing it re-renders the page under it (`loci:changed`).
   ========================================================== */

import * as api from "./api.js";
import { h, fill, clickable, btn, radio, errorLine, moreLine } from "./ui.js";
import { openDetail } from "./detail.js";

/** core/names.KIND_PERSON, the kind every table starts with. The canvas calls it 人名. */
export const KIND_PERSON = "人";
const KIND_WORDS = { [KIND_PERSON]: "人名" };

/** The words for a kind: the canvas's for the person kind, the table's own otherwise. */
export function kindLabel(kind) {
  return KIND_WORDS[kind] || kind;
}

/** The kinds the table uses, the person kind first (it is always there). */
export function kindsOf(reply) {
  const rest = ((reply && reply.kinds) || []).map((k) => k.kind).filter((k) => k && k !== KIND_PERSON);
  return [KIND_PERSON, ...rest];
}

const EDITS = [
  { key: "rename", label: "正式名字" },
  { key: "merge", label: "并到别的名字" },
  { key: "kind", label: "改类别" },
];

const SHOWN = 3;

const ICON_CLOSE = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"></path></svg>';

let current = null;   // the one open card
let listSeq = 0;

/** Open the card of `name`. */
export function openNameCard(name) {
  const n = String(name || "").trim();
  if (!n) return;
  if (!current) current = makeCard();
  current.show(n);
}

function makeCard() {
  const before = document.activeElement;
  const dlg = h("div", { class: "dlg rising namecard", role: "dialog", "aria-modal": "true", tabindex: "-1" });
  const scrim = h("div", { class: "scrim" }, dlg);
  const state = { name: "", card: null, changed: false, notice: "", menu: null };
  const bodyOverflow = document.body.style.overflow;

  scrim.addEventListener("mousedown", (e) => { if (e.target === scrim) close(); });
  const onKey = (e) => {
    if (e.key !== "Escape") return;
    if (state.menu) closeMenu(); else close();
  };
  document.addEventListener("keydown", onKey);
  document.body.style.overflow = "hidden";
  document.body.appendChild(scrim);
  requestAnimationFrame(() => requestAnimationFrame(() => dlg.classList.remove("rising")));

  function close({ refocus = true } = {}) {
    document.removeEventListener("keydown", onKey);
    document.body.style.overflow = bodyOverflow;
    scrim.remove();
    current = null;
    if (refocus && before && typeof before.focus === "function") before.focus();
    if (state.changed) document.dispatchEvent(new CustomEvent("loci:changed"));
  }

  /** Leave the card for one memory in the detail window. */
  const toMemory = (id) => () => { close({ refocus: false }); openDetail(id); };

  function closeMenu() {
    if (state.menu) { state.menu.forEach((n) => n.remove()); state.menu = null; }
    const b = dlg.querySelector(".head .btn");
    if (b) b.setAttribute("aria-expanded", "false");
  }

  function closeBtn() {
    const b = h("button", { class: "ib close", type: "button", "aria-label": "关闭", on: { click: () => close() } });
    const icon = h("span", { style: { display: "inline-flex" }, "aria-hidden": "true" });
    icon.innerHTML = ICON_CLOSE;
    b.append(icon);
    return b;
  }

  async function show(name) {
    closeMenu();
    state.name = name;
    try {
      state.card = await api.get(`/api/loci/names/${api.seg(name)}`, { limit: SHOWN });
    } catch (e) {
      if (e.status === 401) { close(); return; }
      fill(dlg, h("div", { class: "grab", "aria-hidden": "true" }),
        h("div", { class: "head" }, h("h1", { text: name }), closeBtn()), errorLine(e));
      return;
    }
    state.name = state.card.name || name;
    render();
    dlg.focus({ preventScroll: true });
    scrim.scrollTop = 0;
  }

  function render() {
    const c = state.card;
    const editBtn = h("button", { class: "btn", type: "button", "aria-expanded": "false", "aria-haspopup": "menu" }, "编辑");
    editBtn.addEventListener("click", () => (state.menu ? closeMenu() : openMenu(editBtn)));
    dlg.setAttribute("aria-labelledby", "nt");

    const mem = c.memories || {};
    const aliases = c.aliases || [];
    const meta = [
      c.kind ? h("span", { text: kindLabel(c.kind) }) : null,
      h("span", { text: `出现 ${mem.total || 0} 次` }),
      aliases.length ? h("span", { text: `也叫 ${aliases.join("、")}` }) : null,
    ];

    const hangs = [...(c.present_in || []), ...(c.member_of || [])];
    const blocks = [];
    if (hangs.length) {
      blocks.push(h("div", null, h("p", { class: "cap", text: "什么关系" }),
        h("p", { class: "say" }, hangs.map((n, i) => [i ? "、" : "",
          clickable(h("a", { class: "nm", text: n }), () => show(n))]))));
    }
    if (c.card) {
      blocks.push(h("div", null, h("p", { class: "cap", text: "要注意的" }),
        clickable(h("a", { class: "say", text: c.card.text }), toMemory(c.card.id))));
    }

    const items = mem.items || [];
    const memories = items.length
      ? h("section", null, h("h2", { class: "h", text: "出现过的记忆" }),
        items.map((m) => clickable(h("a", { class: "ln" },
          h("span", { class: "c", text: m.text || `#${m.short}` }),
          h("span", { class: "why", style: { flexShrink: "0" }, text: m.date || "" })), toMemory(m.id))),
        moreLine((mem.total || 0) - items.length))
      : null;

    fill(dlg,
      h("div", { class: "grab", "aria-hidden": "true" }),
      h("div", { class: "head" }, h("h1", { id: "nt", text: state.name }), editBtn, closeBtn()),
      h("div", { class: "why meta", style: { marginTop: "8px" } }, meta),
      state.notice ? h("p", { class: "why", role: "status", style: { margin: "14px 0 0" }, text: state.notice }) : null,
      h("div", { class: "card-body" }, blocks),
      memories ? h("hr", { class: "card-rule" }) : null,
      memories);
    state.notice = "";
  }

  // ------------------------------------------------------------ 编辑

  function openMenu(button) {
    button.setAttribute("aria-expanded", "true");
    const menuScrim = h("div", { class: "menu-scrim", "aria-hidden": "true", on: { click: closeMenu } });
    const menu = h("div", { class: "menu", role: "menu", "aria-label": "编辑" },
      h("div", { class: "card" }, EDITS.map((ed) => h("button", { class: "mi", role: "menuitem", type: "button",
        on: { click: () => { closeMenu(); startEdit(ed); } } }, ed.label))),
      h("button", { class: "mi cancel", type: "button", on: { click: closeMenu } }, "取消"));
    dlg.append(menuScrim, menu);
    state.menu = [menuScrim, menu];
    const first = menu.querySelector(".mi");
    if (first) first.focus({ preventScroll: true });
  }

  async function startEdit(ed) {
    const slot = dlg.querySelector(".card-body");
    if (!slot) return;
    const known = ed.key === "rename" ? null : await api.get("/api/loci/names", { limit: 50 }).catch(() => null);
    const msg = h("div");
    const save = btn("保存", { dark: true });
    const cancel = btn("取消", { onClick: () => render() });
    let field;
    let valueOf;
    if (ed.key === "kind") {
      // The kinds as choices (the person kind first), then + 新类别 with a box for a new one.
      const group = `nc-kind-${++listSeq}`;
      const kinds = kindsOf(known);
      if (state.card.kind && !kinds.includes(state.card.kind)) kinds.push(state.card.kind);
      const fresh = h("input", { class: "inp", style: { flex: "1 1 160px", minWidth: "0" }, "aria-label": "+ 新类别" });
      const newChoice = radio({ name: group, label: "+ 新类别", value: "" }, fresh);
      fresh.addEventListener("focus", () => { newChoice.querySelector("input").checked = true; });
      field = h("div", null,
        kinds.map((k) => radio({ name: group, label: kindLabel(k), value: k, checked: k === state.card.kind })),
        newChoice);
      valueOf = () => {
        const on = field.querySelector("input[type=radio]:checked");
        return on && on.value ? on.value : fresh.value.trim();
      };
    } else {
      // 正式名字: the name as it is, to change. 并到别的名字: the known names as suggestions.
      const listId = `nc-list-${++listSeq}`;
      const input = h("input", { class: "inp", style: { width: "100%" }, "aria-label": ed.label, list: listId });
      input.value = ed.key === "rename" ? state.name : "";
      const names = ((known && known.items) || []).map((it) => it.name).filter((n) => n !== state.name);
      field = h("div", null, input, h("datalist", { id: listId }, names.map((v) => h("option", { value: v }))));
      valueOf = () => input.value.trim();
    }
    save.addEventListener("click", () => write(ed.key, valueOf(), msg, save));
    field.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target.classList.contains("inp")) { e.preventDefault(); save.click(); }
    });
    fill(slot, h("div", { class: "editbox" },
      h("p", { class: "cap", style: { margin: "0" }, text: ed.label }), field, msg,
      h("div", { class: "acts" }, cancel, save)));
    const first = field.querySelector("input:checked, .inp");
    if (first) first.focus();
  }

  async function write(key, value, msgBox, button) {
    if (!value) return;
    const body = key === "kind"
      ? { action: "set_kind", name: state.name, kind: value }
      : { action: key, name: state.name, target: value };
    button.disabled = true;
    let out;
    try {
      out = await api.post("/api/loci/names/action", body);
    } catch (e) {
      button.disabled = false;
      if (e.status === 401) { close(); return; }
      fill(msgBox, errorLine(e));
      return;
    }
    state.changed = true;
    state.notice = out && out.note ? out.note : "";
    await show(key === "kind" ? state.name : value);
  }

  return { show, close };
}
