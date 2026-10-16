/* ==========================================================
   periodform.js — regrow/fold's 日子 / 想法 column: the two lists and the glass window
   each row opens

   GET /api/loci/periods: periods (newest start first) and merged thoughts (newest day
   first, those with no day last). A period row: its name; its span (MM-DD – MM-DD, or
   – 至今), how many memories fall in it, and 「跟「X」叠着」 for each period it overlaps.
   A thought row: its line; its day (or 没写日子), how many it merged.

   A row opens a glass window over GET /api/loci/periods/{id}:
     a period    名字 and 开始 / 结束 (her three wheels, ui.whenPick; 至今 leaves the end
                 open), 保存 (POST periods/edit {id, name, when}); the memories in it, each
                 with 移出 (a day outside the span, picked on the wheels: POST periods/day);
                 ＋ 加一条 (search, then the day inside the span it happened on: the same
                 route); 合并 (pick another period, a name: POST periods/merge, this one
                 kept) and 删掉 (asked twice: POST periods/delete).
     a thought   日期 (wheels, 保存 / 清空: POST periods/day); what it merged, each with
                 拿掉 (POST periods/cover {remove}); ＋ 加一条 (search, then 并进来:
                 {add}).
   After a write the window and the list are read again. A refusal shows the server's
   words under what was clicked. Every change is told to 他 on his next tool call
   (core/_panel_changes.py); the window says so once at its foot.
   ========================================================== */

import * as api from "./api.js";
import { h, fill, group, row, btn, inp, whenPick, pick, glassWindow, errorLine, AI_NAME } from "./ui.js";
import { openDetail } from "./detail.js";

const ONE = "/api/loci/periods";
const DAY = /^\d{4}-\d{2}-\d{2}$/;

export const TIP_COLUMN = "fold 圈出来的日子，和几条并成一条的想法。日子里有哪些记忆，看的是每条记忆自己的日期落没落在这段里：把一条移进移出，改的就是它的日期。在这儿改的，他下次调工具时会知道。";
const TOLD = `在这儿改的，${AI_NAME}下次调工具时会看到一句「改了哪些」。`;

const md = (day) => String(day || "").slice(5);
const spanWords = (p) => `${md(p.start)} – ${p.end ? md(p.end) : "至今"}`;

function field(label, ...input) {
  return h("div", { class: "fld" }, h("span", { class: "fl", text: label }), ...input);
}

/** A button that runs `fn` once, waiting while it runs; a refusal shows under `err`. */
function act(label, fn, err, opts = {}) {
  const b = btn(label, { ...opts });
  b.addEventListener("click", async () => {
    fill(err);
    b.disabled = true;
    try { await fn(); } catch (e) { fill(err, errorLine(e)); }
    b.disabled = false;
  });
  return b;
}

/** A day on the wheels plus 确定: `done(day)` with a whole day only. */
function dayAsk(start, done, err, label = "确定") {
  let day = start || "";
  const wheels = whenPick({ value: day, onChange: (v) => { day = v; } });
  const ok = act(label, async () => {
    if (!DAY.test(day)) throw new Error("选到哪一天");
    await done(day);
  }, err, { dark: true });
  return h("span", { class: "pask" }, wheels, ok);
}

/** ＋ 加一条: a search box over the recall page's search; each hit gets `pickHit(hit)`'s
 *  controls at its right. */
function adder(pickHit, err) {
  const box = inp({ type: "search", placeholder: "搜一条记忆", "aria-label": "搜一条记忆" });
  const hits = h("div", { class: "rows" });
  const go = async () => {
    fill(err);
    const q = box.value.trim();
    if (!q) { fill(hits); return; }
    try {
      const out = await api.get("/api/loci/recall", { query: q, limit: 8 });
      const items = (out.rows && out.rows.items) || [];
      fill(hits, items.length ? items.map((it) => row({ text: it.text, why: it.date || "", right: pickHit(it) }))
        : h("p", { class: "why", text: "没搜到" }));
    } catch (e) { fill(err, errorLine(e)); }
  };
  box.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
  return h("div", { class: "padd" }, h("div", { class: "pask" }, box, btn("搜", { onClick: go })), hits);
}

// ---------------------------------------------------------------- a period

function periodWindow(item, all, changed) {
  const win = glassWindow({ title: item.text });
  const draw = async () => {
    let p;
    try { p = await api.get(`${ONE}/${encodeURIComponent(item.id)}`); } catch (e) { fill(win.body, errorLine(e)); return; }
    const err = h("div");
    const after = async () => { changed(); await draw(); };

    const name = inp({ value: p.text, "aria-label": "名字" });
    let start = p.start;
    let end = p.end || "";
    let open = !p.end;
    const endPick = whenPick({ value: end, onChange: (v) => { end = v; } });
    const toNow = h("input", { type: "checkbox", checked: open });
    toNow.addEventListener("change", () => { open = toNow.checked; endPick.classList.toggle("dim", open); });
    endPick.classList.toggle("dim", open);
    const save = act("保存", async () => {
      if (!DAY.test(start) || (!open && !DAY.test(end))) throw new Error("开始和结束都选到哪一天");
      await api.post(`${ONE}/edit`, { id: p.id, name: name.value.trim(), when: `${start}..${open ? "" : end}` });
      win.close();
      changed();
    }, err, { dark: true });

    const memberErr = h("div");
    const members = (p.members || []).map((m) => {
      const slot = h("span", { class: "acts" });
      const out = btn("移出", { onClick: () => fill(slot, dayAsk(m.day, async (day) => {
        await api.post(`${ONE}/day`, { id: m.id, day });
        await after();
      }, memberErr, "改到这天")) });
      slot.append(out);
      return row({ text: m.text, why: m.day, open: () => openDetail(m.id), right: slot, layout: "wide" });
    });
    const addErr = h("div");
    const add = adder((hit) => {
      const slot = h("span", { class: "acts" });
      slot.append(btn("加进来", { onClick: () => fill(slot, dayAsk(p.start, async (day) => {
        await api.post(`${ONE}/day`, { id: hit.id, day });
        await after();
      }, addErr, "是这天")) }));
      return slot;
    }, addErr);

    const others = all.filter((o) => o.id !== p.id).map((o) => [o.id, `${o.text}（${spanWords(o)}）`]);
    let other = "";
    const mergeErr = h("div");
    const mergeName = inp({ value: p.text, "aria-label": "合并以后叫什么" });
    const mergeBox = h("div", { class: "pform", hidden: true },
      field("跟哪一段合", pick({ label: "跟哪一段合", options: others, placeholder: "选一段", onChange: (v) => { other = v; } })),
      field("合并以后叫什么", mergeName),
      h("div", { class: "acts end" }, act("合并", async () => {
        if (!other) throw new Error("先选一段");
        await api.post(`${ONE}/merge`, { keep: p.id, drop: other, name: mergeName.value.trim() });
        win.close();
        changed();
      }, mergeErr, { dark: true })), mergeErr);
    let sure = false;
    const del = act("删掉", async () => {
      if (!sure) { sure = true; del.textContent = "确定删掉？里面的记忆不动"; return; }
      await api.post(`${ONE}/delete`, { id: p.id });
      win.close();
      changed();
    }, err);

    fill(win.body, h("div", { class: "pform" },
      field("名字", name),
      field("开始", whenPick({ value: start, onChange: (v) => { start = v; } })),
      field("结束", h("span", { class: "pask" }, endPick, h("label", { class: "opt" }, toNow, " 至今"))),
      h("div", { class: "acts end" }, save), err,
      h("h3", { class: "st", text: `里面的记忆（${(p.members || []).length}）` }),
      h("div", { class: "rows" }, members.length ? members : h("p", { class: "why", text: "这段日子里还没有记忆" })),
      memberErr,
      h("h3", { class: "st", text: "＋ 加一条" }), add,
      h("div", { class: "acts end" },
        others.length ? btn("合并", { onClick: () => { mergeBox.hidden = !mergeBox.hidden; } }) : null, del),
      mergeBox,
      h("p", { class: "why", text: TOLD })));
  };
  draw();
}

// ---------------------------------------------------------------- a thought

function thoughtWindow(item, changed) {
  const win = glassWindow({ title: item.text });
  const draw = async () => {
    let t;
    try { t = await api.get(`${ONE}/${encodeURIComponent(item.id)}`); } catch (e) { fill(win.body, errorLine(e)); return; }
    const err = h("div");
    const after = async () => { changed(); await draw(); };
    let day = t.day || "";
    const wheels = whenPick({ value: day, onChange: (v) => { day = v; } });
    const save = act("保存", async () => {
      if (!DAY.test(day)) throw new Error("选到哪一天");
      await api.post(`${ONE}/day`, { id: t.id, day });
      await after();
    }, err, { dark: true });
    const clear = t.day ? act("清空", async () => {
      await api.post(`${ONE}/day`, { id: t.id, day: "" });
      await after();
    }, err) : null;

    const memberErr = h("div");
    const members = (t.members || []).map((m) => row({
      text: m.text, why: m.day, open: () => openDetail(m.id), layout: "wide",
      right: h("span", { class: "acts" }, act("拿掉", async () => {
        await api.post(`${ONE}/cover`, { id: t.id, remove: [m.id] });
        await after();
      }, memberErr)),
    }));
    const addErr = h("div");
    const add = adder((hit) => h("span", { class: "acts" }, act("并进来", async () => {
      await api.post(`${ONE}/cover`, { id: t.id, add: [hit.id] });
      await after();
    }, addErr)), addErr);

    fill(win.body, h("div", { class: "pform" },
      field("日期", h("span", { class: "pask" }, wheels, save, clear)), err,
      h("h3", { class: "st", text: `并了哪些（${(t.members || []).length}）` }),
      h("div", { class: "rows" }, members), memberErr,
      h("h3", { class: "st", text: "＋ 加一条" }), add,
      h("p", { class: "why", text: TOLD })));
  };
  draw();
}

// ---------------------------------------------------------------- the column

function periodRow(p, all, changed) {
  const why = [spanWords(p), `${p.count} 条`, ...(p.overlaps || []).map((o) => `跟「${o.text}」叠着`)];
  return row({ text: p.text, why, open: () => periodWindow(p, all, changed) });
}

function thoughtRow(t, changed) {
  return row({ text: t.text, why: [t.day ? t.day : "没写日子", `并了 ${t.count} 条`],
    open: () => thoughtWindow(t, changed) });
}

/** The column into `main`, drawn again after every write. */
export async function renderColumn(main) {
  const draw = async () => {
    let out;
    try { out = await api.get(ONE); } catch (e) { fill(main, errorLine(e)); return; }
    const periods = out.periods || [];
    const thoughts = out.thoughts || [];
    fill(main,
      group("日子", {}, h("div", null, periods.length ? periods.map((p) => periodRow(p, periods, draw))
        : h("p", { class: "why", text: "还没有圈出来的日子" }))),
      group("想法", {}, h("div", null, thoughts.length ? thoughts.map((t) => thoughtRow(t, draw))
        : h("p", { class: "why", text: "还没有并起来的想法" }))));
  };
  await draw();
}
