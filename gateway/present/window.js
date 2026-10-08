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
//   carry      text that stands in for everything before the mark — a daily report or a
//              compression. 🔴 It lives only here, in the private ledger: never in a day
//              file, never in anything sent back to the client, never in a /present reply.
//   overlays   what the gateway put into the model's input that the client's history does
//              not hold: Loci's cards, a dream or the muse line, later the away lines and
//              the compression reminders. Each is "this text, before/after line X".
//   usage      the last prompt_tokens (or an estimate) and the fill % it makes
//   model      the model the client used last
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
// it), so an overlay never lands between an assistant tool call and its results.
// An overlay whose anchor is not in a request's history (the client trimmed it, the
// owner rewound past it, it sits before the mark) is not replayed in that request.
//
// ─── The mark ───
// The mark is looked up in the client's history as sent. Found: everything before it is
// cut and the carry stands in for it. Not found — the owner rewound to before it, or the
// client trimmed its own history past it — the mark is void for that request and the
// history is forwarded as it is (overlays still replayed where their anchors are).
// Moving the mark, setting the carry and flipping the window are construction steps 4–5
// (compression, the daily report); open_next() is the one door they go through.
//
// ─── Forks ───
// A fork (threads.js: an edit, a rewind, a reply for a line the thread moved past) starts
// a new thread over the shared lines. It inherits the window when the mark is among the
// shared lines — the model saw that carry and those overlays on this branch, and the same
// bytes keep the cache — including Loci's window name: the cards replayed into the fork
// were delivered to that window, so the fork keeps speaking for it until it flips. When
// the mark is not among the shared lines the fork starts a fresh window of its own.
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
    overlays: [],
    usage: null,
    model: null,
  };
}

/** The thread's window, created on first use. */
function ensure(thread, now) {
  if (!thread.window || typeof thread.window !== "object") thread.window = fresh_window(thread.id, now);
  const w = thread.window;
  if (!Array.isArray(w.overlays)) w.overlays = [];
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

function overlay_message(o) { return { role: OVERLAY_ROLE, content: o.text }; }

/**
 * Build the upstream copy.
 * @param messages  the client's messages, as sent (not modified)
 * @param ids       map_ids() of those messages
 * @param win       the window state
 * @returns { messages, mark: "none" | "kept" | "void", cut, carried, replayed: [overlays] }
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
  return { messages: out, mark, cut: mark === "kept" ? from - head : 0, carried, replayed };
}

/**
 * Close this window and open the next one: the mark moves, the carry is replaced, the
 * overlays and the fill are cleared (a new window must not be nagged by the old one's
 * water level). The caller tells Loci with /cue/dropped {window: old_name, all: true}.
 * @returns { old_name, name }
 */
function open_next(thread, { mark = null, carry = null } = {}, now) {
  const old = ensure(thread, now);
  const next = fresh_window(thread.id, now, (Number(old.no) || 1) + 1);
  next.mark = mark;
  next.carry = carry;
  next.model = old.model;
  thread.window = next;
  return { old_name: old.name, name: next.name };
}

module.exports = { ensure, inherit, overlay_for, add_overlay, map_ids, assemble, open_next, fresh_window, OVERLAY_ROLE };
