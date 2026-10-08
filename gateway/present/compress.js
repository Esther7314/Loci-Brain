// ============================================================
// gateway/present/compress.js — the water level, the reminders, and the window he folds
// himself
//
// The fill % is the last turn's input over the model's window (present/fill.js). Three
// kinds of line act on it (present.json, compress.*):
//   weak_pct[]  "this window is getting full, pick your moment"   ┐ reminders only, each
//   ask_pct     "fold it this turn or the next"                    ┘ offered once per window
//   force_pct   the gateway packs the window itself (pack.js); the wall's way out is wall.js
// Nothing is offered, and nothing packed, while compress.on is false.
//
// ─── A reminder ───
// When the fill crosses a line not yet offered in this window, his reminder
// (prompts.js compress_self_shell, with the compress card as it stands and keep_raw) goes
// in at the true tail of the turn: after her new line, after that line's card. When the
// fill jumps past several lines at once only the highest is said, and the lower ones
// count as offered with it (a weak reminder after the ask one would be noise).
//
// The reminder is an overlay (window.js) and is replayed in place every later turn until
// the window flips. Why, rather than said once and dropped:
//   · the weak reminder tells him "not now, pick your moment" — when that moment comes,
//     turns later, the instructions (the marker, what to write, keep_raw) must still be
//     in front of him, or he has been told to do something he no longer knows how to do;
//   · it never goes stale: it stays true until he folds the window, and folding flips the
//     window, which clears every overlay;
//   · in place, the upstream prefix of the next turn is the previous turn's prefix byte
//     for byte, so the prompt cache holds across it. (Said once and dropped, the cache
//     would break at that spot on the next turn; it sits near the tail, so that alone
//     would cost little — the first reason is the one that decides.)
// The window records which lines were offered (window.offered), so a reminder is never
// said twice in one window; a resend of the same turn replays the overlay already made.
//
// ─── Write → leave ───
// He writes 【窗口摘要】…【/窗口摘要】 in his reply. The client never sees it
// (stream_filter.js); a closed block becomes the next window's carry, and once the reply
// has finished the window flips (lesson ②: write it, then leave):
//   · the carry = the report part of the carry it replaces (the latest day report, if
//     any) + this summary (window.js compose_carry)
//   · the mark moves to the last keep_raw raw lines, onto a line of hers
//   · the window number goes up by one; the fill and the offered lines start empty
//     (lesson ③: a new window that inherits the old fill is nagged at once)
//   · Loci hears /cue/dropped {window: old, all: true} (index.js)
//   · window.opened_by = { at, how: "self" }, for /present
// A reply that ends in tool calls has not finished the turn: the summary waits in the
// window (summary_pending, private like the carry) and the flip happens when the turn's
// last reply comes back. A half block (never closed) is never stored (lesson ④).
// ============================================================

const win = require("./window.js");
const { compress_self_shell } = require("./prompts.js");

const REMINDER = "reminder";

/**
 * Which reminder line is due, if any.
 * @param fill_pct  the last turn's fill, or null
 * @param values    settings compress section { weak_pct, ask_pct }
 * @param offered   window.offered
 * @returns { line: "weak" | "ask", covers: [keys] } | null
 */
function due_line({ fill_pct, values, offered = [] }) {
  const fill = Number(fill_pct);
  if (fill_pct === null || fill_pct === undefined || !Number.isFinite(fill)) return null;
  const crossed = [];
  for (const p of [...(values.weak_pct || [])].sort((x, y) => x - y)) if (fill >= p) crossed.push(`weak:${p}`);
  if (fill >= values.ask_pct) crossed.push("ask");
  if (!crossed.length) return null;
  const top = crossed[crossed.length - 1];
  if (offered.includes(top)) return null;
  return { line: top === "ask" ? "ask" : "weak", covers: crossed.filter((k) => !offered.includes(k)) };
}

/**
 * Offer the due reminder on this turn, as a tail overlay on her line.
 * @param w       the window (changed in place; the caller saves the thread)
 * @param turn    her line's id
 * @param values  settings compress section
 * @param card    () → the compress card text in force
 * @returns the overlay, or null when nothing is due
 */
function offer_reminder({ w, turn, values, card }) {
  if (!values || values.on !== true || !turn) return null;
  if (win.overlay_for(w, REMINDER, turn)) return null;
  const due = due_line({ fill_pct: w.usage ? w.usage.fill_pct : null, values, offered: w.offered });
  if (!due) return null;
  const text = compress_self_shell({ card: card(), keep_raw: values.keep_raw, line: due.line });
  w.offered.push(...due.covers);
  return win.add_overlay(w, { kind: REMINDER, anchor: turn, place: "after", text, turn,
                              tail: true, line: due.line, lines: due.covers });
}

/**
 * He folded the window: open the next one.
 * @param thread    the thread (its window is replaced; the caller saves it)
 * @param summary   the closed block's text
 * @param keep_raw  settings compress.keep_raw
 * @param at        local time stamp for opened_by
 * @returns { old_name, name, mark }
 */
function self_flip({ thread, summary, keep_raw, at, now }) {
  const old = thread.window || null;
  const report = old && old.carry_parts && typeof old.carry_parts.report === "string" ? old.carry_parts.report : null;
  const mark = win.mark_for_tail(thread.branch || [], keep_raw);
  const flip = win.open_next(thread, { mark, parts: { report, summary }, how: "self", at }, now);
  return { ...flip, mark };
}

module.exports = { due_line, offer_reminder, self_flip, REMINDER };
