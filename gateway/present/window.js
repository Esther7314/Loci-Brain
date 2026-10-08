// ============================================================
// gateway/present/window.js — one conversation's window, and the copy that goes upstream
//
// The client sends its whole history every turn and never learns what the gateway does
// with it. What goes upstream is assembled here, fresh every turn:
//
//     [client system] + [carry] + [lines from the mark on, overlays replayed] + [new message]
//
// and the client's own history is never changed (the forward is a new array).
//
// ─── The window state (thread.window, saved inside threads/<id>.json) ───
//   no, name   the window number and Loci's name for it, "<thread>#w<no>"
//   mark       id of the first raw line kept in this window (null: the whole history)
//   carry      text that stands in for everything before the mark (composed below).
//              🔴 It lives only here, in the private ledger: never in a day file, never in
//              anything sent back to the client, never in a /present reply.
//   carry_parts  { report, summary }: what the carry was composed from, so the next flip
//              can keep one part and replace the other (same privacy as the carry)
//   overlays   what the gateway put into the model's input that the client's history does
//              not hold: Loci's cards, a dream or the muse line, the compression reminders,
//              the away lines (his, role assistant; replayed only until she has seen
//              them, then taken out by away.js). Each is "this text, before/after line X".
//   offered    the reminder lines already offered in this window ("weak:<pct>", "ask"),
//              each offered once per window (present/compress.js)
//   summary_pending  a summary he wrote in a reply that ended in tool calls, waiting for
//              the turn to end before the flip (same privacy as the carry)
//   usage      the last prompt_tokens (or an estimate) and the fill % it makes
//   model      the model the client used last
//   opened_by  { at, how }: how this window was opened ("self", "forced", "manual",
//              "day"); null for a conversation's first window. Time and way
//              only, never text: it is what /present shows as compress.last.
//
// ─── The carry ───
// Composed by compose_carry() from two parts, each optional, in this order:
//     a header      system text, not her words: this is only the recent past, the long
//                   term is in Loci, breath for it
//     〔上一份日报〕                 the latest day report (day_close.js writes it)
//     〔你上一扇窗收尾时写给自己的〕   the summary he wrote when the last window closed
// A flip he made himself keeps the report part of the carry it replaces and puts his new
// summary in. The day report's flip (day_close.js) puts the new report in and drops the
// summary, because the report covers what the summary did. No part at all = no carry.
// The composed text is stored as it is and sent unchanged for the whole window, so the
// upstream prefix stays byte-stable.
//
// ─── Overlays are replayed until the window changes ───
// An overlay is put in once and then, every later turn, put back at the same place with
// the same bytes. Two reasons, both load-bearing:
//   · the prompt cache: the upstream prefix of turn N+1 has to be byte-identical to what
//     was sent in turn N, overlays included, or the cache breaks at the first missing one;
//   · Loci's ledger: a card confirmed as delivered to this window stays out of later
//     answers; if it were in the input for one turn only, "delivered to this window"
//     would be a lie for the rest of it.
// Anchors are the owner's lines (cards go after her line, dreams and away lines before
// it), so an overlay never lands between an assistant tool call and its results. Among
// the overlays after one line, a `tail` one (a reminder) goes last: it is said at the true
// end of the turn it was offered in, after that turn's card.
// An overlay whose anchor is not in a request's history (the client trimmed it, the
// owner rewound past it, it sits before the mark) is not replayed in that request.
//
// ─── The mark ───
// The mark is looked up in the client's history as sent. Found: everything before it is
// cut and the carry stands in for it. Not found — the owner rewound to before it, or the
// client trimmed its own history past it — the mark is void for that request and the
// history is forwarded as it is (overlays still replayed where their anchors are).
// Moving the mark, setting the carry and flipping the window go through one door,
// open_next(): a fold he made himself (present/compress.js), a pack (present/pack.js) and
// the day report's flip (present/day_close.js).
//
// ─── Forks ───
// A fork (threads.js: an edit, a rewind, a reply for a line the thread moved past) starts
// a new thread over the shared lines. It inherits the window when the mark is among the
// shared lines — the model saw that carry and those overlays on this branch, and the same
// bytes keep the cache — including Loci's window name: the cards replayed into the fork
// were delivered to that window, so the fork keeps speaking for it until it flips. When
// the mark is not among the shared lines the fork starts a fresh window of its own.
// A reminder line counts as offered in the fork only when its reminder came along.
// ============================================================

const { to_said } = require("./threads.js");

const OVERLAY_ROLE = "system";

function fresh_window(thread_id, now, no = 1) {
  return {
    no,
    name: `${thread_id}#w${no}`,
    opened_at: now,
    mark: null,
    carry: null,
    carry_parts: null,
    overlays: [],
    offered: [],
    usage: null,
    model: null,
    opened_by: null,
  };
}

const CARRY_HEAD = `〔接着上一扇窗 · 系统给的，不是 ta 发的话〕
这扇窗是接着上一扇开的。下面是你带过来的，再往下是最近的原话，从那儿接着说就行。
这只是近期的事，长期的在 Loci 里，要用就自己 breath。`;
const CARRY_REPORT = "〔上一份日报〕";
const CARRY_SUMMARY = "〔你上一扇窗收尾时写给自己的〕";

/** The carry text from its parts, or null when there is neither. */
function compose_carry({ report = null, summary = null } = {}) {
  const r = typeof report === "string" ? report.trim() : "";
  const s = typeof summary === "string" ? summary.trim() : "";
  if (!r && !s) return null;
  const parts = [CARRY_HEAD];
  if (r) parts.push(`${CARRY_REPORT}\n${r}`);
  if (s) parts.push(`${CARRY_SUMMARY}\n${s}`);
  return parts.join("\n\n");
}

/**
 * Where the mark goes when a window flips: the first of the last `keep_raw` lines of the
 * branch, moved forward to a line of hers (a window must not open on a reply of his: an
 * assistant message first upstream is wrong). A tail with no line of hers in it reaches
 * back to her nearest line instead. null when the branch has no line of hers.
 */
function mark_for_tail(branch, keep_raw) {
  const n = Math.max(1, Number(keep_raw) || 1);
  const from = Math.max(0, branch.length - n);
  for (let i = from; i < branch.length; i++) if (branch[i].role === "user") return branch[i].id;
  for (let i = from - 1; i >= 0; i--) if (branch[i].role === "user") return branch[i].id;
  return null;
}

/** The thread's window, created on first use. */
function ensure(thread, now) {
  if (!thread.window || typeof thread.window !== "object") thread.window = fresh_window(thread.id, now);
  const w = thread.window;
  if (!Array.isArray(w.overlays)) w.overlays = [];
  if (!Array.isArray(w.offered)) w.offered = [];
  return w;
}

/** A forked thread takes over its parent's window when the parent's mark is on the shared part. */
function inherit(child, parent, now) {
  if (!child || !parent || child.window) return child?.window || null;
  const pw = parent.window;
  if (!pw) return null;
  const ids = new Set(child.branch.map((e) => e.id));
  if (pw.mark && !ids.has(pw.mark)) { child.window = fresh_window(child.id, now); return child.window; }
  child.window = JSON.parse(JSON.stringify(pw));
  child.window.overlays = child.window.overlays.filter((o) => ids.has(o.anchor));
  const still = new Set(child.window.overlays.flatMap((o) => (Array.isArray(o.lines) ? o.lines : [])));
  child.window.offered = (child.window.offered || []).filter((k) => still.has(k));
  return child.window;
}

/** Overlay already recorded for this kind and turn? */
function overlay_for(win, kind, turn) {
  return win.overlays.find((o) => o.kind === kind && o.turn === turn) || null;
}

/**
 * @param o { kind, anchor, place: "before" | "after", text, turn, ...extra }
 *   An overlay with empty text is kept (it remembers that the question was asked and
 *   answered "nothing") but never replayed.
 */
function add_overlay(win, o) {
  const overlay = { kind: o.kind, anchor: o.anchor, place: o.place === "before" ? "before" : "after",
                    text: String(o.text ?? ""), turn: o.turn ?? null };
  for (const [k, v] of Object.entries(o)) if (!(k in overlay)) overlay[k] = v;
  win.overlays.push(overlay);
  return overlay;
}

/**
 * Which stored line each client message is: walk back from the message the history ends
 * at (= the cursor on the branch) while fingerprints agree.
 * @returns array, one entry per client message: a line id or null
 */
function map_ids(messages, branch, cursor) {
  const ids = new Array(messages.length).fill(null);
  if (!cursor) return ids;
  let b = branch.findIndex((e) => e.id === cursor);
  if (b < 0) return ids;
  for (let i = messages.length - 1; i >= 0 && b >= 0; i--) {
    const m = messages[i];
    if (!m || (m.role !== "user" && m.role !== "assistant")) continue;
    if (to_said(m).fp !== branch[b].fp) break;
    ids[i] = branch[b].id;
    b--;
  }
  return ids;
}

// An overlay is a system message, except the away lines (present/away.js): what he said
// while she was away goes in as his own words, role assistant.
function overlay_message(o) { return { role: o.role === "assistant" ? "assistant" : OVERLAY_ROLE, content: o.text }; }

/**
 * Build the upstream copy.
 * @param messages  the client's messages, as sent (not modified)
 * @param ids       map_ids() of those messages
 * @param win       the window state
 * @returns { messages, mark: "none" | "kept" | "void", cut, carried, replayed: [overlays],
 *            keep_head: how many leading messages are the client's system and the carry }
 */
function assemble(messages, ids, win) {
  let head = 0;
  while (head < messages.length && (messages[head]?.role === "system" || messages[head]?.role === "developer")) head++;

  let from = head;
  let mark = "none";
  if (win.mark) {
    const at = ids.indexOf(win.mark);
    if (at >= head) { from = at; mark = "kept"; } else mark = "void";
  }

  const by_anchor = new Map();
  for (const o of win.overlays) {
    if (!o.text) continue;
    if (!by_anchor.has(o.anchor)) by_anchor.set(o.anchor, []);
    by_anchor.get(o.anchor).push(o);
  }
  for (const list of by_anchor.values()) list.sort((x, y) => Number(Boolean(x.tail)) - Number(Boolean(y.tail)));

  const out = messages.slice(0, head);
  const carried = mark === "kept" && typeof win.carry === "string" && win.carry.length > 0;
  if (carried) out.push({ role: "system", content: win.carry });
  const replayed = [];
  for (let i = from; i < messages.length; i++) {
    const here = ids[i] ? by_anchor.get(ids[i]) || [] : [];
    for (const o of here) if (o.place === "before") { out.push(overlay_message(o)); replayed.push(o); }
    out.push(messages[i]);
    for (const o of here) if (o.place === "after") { out.push(overlay_message(o)); replayed.push(o); }
  }
  return { messages: out, mark, cut: mark === "kept" ? from - head : 0, carried, replayed,
           keep_head: head + (carried ? 1 : 0) };
}

/**
 * Close this window and open the next one: the mark moves, the carry is replaced, the
 * overlays, the fill and the offered reminder lines are cleared (a new window must not be
 * nagged by the old one's water level). The caller tells Loci with
 * /cue/dropped {window: old_name, all: true}.
 * @param parts  { report, summary }: the carry is composed from them (compose_carry);
 *               `carry` instead sets the text as it is, parts unknown
 * @param how    "self" | "forced" | "manual" | "day", with `at` (the local time stamp)
 * @returns { old_name, name }
 */
function open_next(thread, { mark = null, carry = null, parts = null, how = null, at = null } = {}, now) {
  const old = ensure(thread, now);
  const next = fresh_window(thread.id, now, (Number(old.no) || 1) + 1);
  next.mark = mark;
  next.carry = parts ? compose_carry(parts) : carry;
  next.carry_parts = parts ? { report: parts.report ?? null, summary: parts.summary ?? null } : null;
  next.model = old.model;
  next.opened_by = how ? { at, how } : null;
  thread.window = next;
  return { old_name: old.name, name: next.name };
}

module.exports = {
  ensure, inherit, overlay_for, add_overlay, map_ids, assemble, open_next, fresh_window,
  compose_carry, mark_for_tail, OVERLAY_ROLE,
};
