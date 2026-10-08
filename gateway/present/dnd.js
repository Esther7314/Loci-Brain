// ============================================================
// gateway/present/dnd.js — whether this minute is "do not disturb", decided here and only here
//
// Wake asks this file and nothing else; a second copy of the comparison somewhere else is
// how two rules for the night end up disagreeing.
//
// How a span is read:
//   · by the minute ("23:30" is a real setting), in the owner's zone (LOCI_TZ), never the
//     server's
//   · the start is inside, the end is not: 23:00–08:00 is quiet at 23:00 and at 07:59,
//     awake at 08:00
//   · a span whose end is earlier than its start wraps across midnight
//
// Settings that cannot be read mean **do not wake** (the wake section is null, see
// settings.js for_wake). This is the opposite of Lento, where a broken setting counts as
// "not quiet" because she would rather be woken: the gateway runs on a stranger's machine,
// and a machine that cannot read its own settings does not spend their money or ring
// their phone at 3 a.m.
// ============================================================

const { local_stamp } = require("./clock.js");

const HHMM = /^([01]\d|2[0-3]):([0-5]\d)$/;

/** "23:30" → 1410; anything else → null. */
function minute_of_day(hhmm) {
  const m = HHMM.exec(String(hhmm ?? ""));
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}

/** The minute of the owner's day (0–1439) at an instant. */
function local_minute(ms, zone) {
  return minute_of_day(local_stamp(ms, zone).iso.slice(11, 16));
}

/** Is `minute` inside [from, to)? Wraps across midnight when to < from; from === to is empty. */
function in_span(minute, from, to) {
  if (![minute, from, to].every(Number.isInteger)) return false;
  if (from === to) return false;
  return from < to ? minute >= from && minute < to : minute >= from || minute < to;
}

/**
 * @param wake  the wake section of present.json (settings.for_wake()), or null when it
 *              cannot be used
 * @returns { dnd: boolean, reason: "settings_unreadable" | "bad_span" | "dnd" | null }
 *   `dnd: true` means do not wake now.
 */
function dnd_state(wake, ms, zone) {
  if (!wake || typeof wake !== "object") return { dnd: true, reason: "settings_unreadable" };
  const span = wake.dnd;
  if (!span || span.on === false) return { dnd: false, reason: null };
  const from = minute_of_day(span.from);
  const to = minute_of_day(span.to);
  // a span that does not read is treated like settings that do not read
  if (from === null || to === null) return { dnd: true, reason: "bad_span" };
  return in_span(local_minute(ms, zone), from, to) ? { dnd: true, reason: "dnd" } : { dnd: false, reason: null };
}

module.exports = { dnd_state, in_span, local_minute, minute_of_day };
