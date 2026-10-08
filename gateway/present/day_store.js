// ============================================================
// gateway/present/day_store.js — what was said, one file per day, one line per message
//
// The layout on disk:
//     <days dir>/2026-10-07.jsonl
// and one line looks like:
//     {"id":"m_20261007_0042","rev":1,"at":"2026-10-07T20:41:05+08:00","thread":"t_3f9c1a",
//      "role":"user","text":"考完了！","attach":["image"],"tools":null,"woke":false,"state":"live"}
//
// Rules, and why:
//   · **An id is born with its line** — `m_<YYYYMMDD>_<seq>`, seq counting up within the
//     day, at least four digits. The date inside the id names the file, so whoever holds
//     an id (Loci asking for an original, source_api.js) opens one file and nothing else.
//   · **Append only.** Nothing is ever rewritten in place. A change is a new line with
//     the same id and `rev` + 1, appended to the file the id was born in (not today's
//     file), so the newest version of an id is always the last line carrying it there.
//   · `state`: `live`, or `replaced` — the version a regenerate or an edit superseded.
//     A replaced version stays on disk; "replaced" is written as its own revision.
//   · What a reader means by "the day" is the **current version of each id**, in the
//     order the ids were born (`current(day)`); the raw lines are the history.
//   · Tool calls are kept by **name only** — never arguments, never results.
//   · Writes are synchronous: the caller (threads.js, via the relay) stores the owner's
//     line before the request goes upstream, and "before" has to mean before.
//   · A file whose last line was cut off (the process died mid-write) gets a newline
//     before the next append, so one torn line never glues itself to the next one.
//     Torn or unparseable lines are skipped on read, never "repaired".
//
// The gateway is the only writer (one gateway per data directory), so each day's index
// is read from disk once and then kept in memory.
// ============================================================

const fs = require("fs");
const path = require("path");
const { local_stamp } = require("./clock.js");

const ID_PATTERN = /^m_(\d{4})(\d{2})(\d{2})_(\d{4,})$/;

function day_of_id(id) {
  const m = ID_PATTERN.exec(String(id || ""));
  return m ? `${m[1]}-${m[2]}-${m[3]}` : null;
}

function parse_lines(text) {
  const lines = [];
  for (const raw of text.split("\n")) {
    if (!raw.trim()) continue;
    try {
      const line = JSON.parse(raw);
      if (line && typeof line.id === "string" && Number.isInteger(line.rev)) lines.push(line);
    } catch { /* a torn line: skipped, never repaired */ }
  }
  return lines;
}

/** Latest revision of each id, in the order the ids first appear. */
function fold_current(lines) {
  const latest = new Map();
  for (const line of lines) {
    const prev = latest.get(line.id);
    if (!prev || line.rev >= prev.rev) latest.set(line.id, line);
  }
  return [...latest.values()];
}

/**
 * @param dir    the days directory itself (…/days)
 * @param clock  { now() } from clock.js
 * @param zone   IANA zone the owner's day is counted in
 */
function create_day_store({ dir, clock, zone }) {
  const days = new Map();   // day → { latest: Map(id → line), max_seq, needs_newline }

  function file_for(day) { return path.join(dir, `${day}.jsonl`); }

  function load(day) {
    if (days.has(day)) return days.get(day);
    const file = file_for(day);
    let text = "";
    try { text = fs.readFileSync(file, "utf8"); } catch (err) { if (err.code !== "ENOENT") throw err; }
    const latest = new Map();
    let max_seq = 0;
    for (const line of parse_lines(text)) {
      const prev = latest.get(line.id);
      if (!prev || line.rev >= prev.rev) latest.set(line.id, line);
      const m = ID_PATTERN.exec(line.id);
      if (m) max_seq = Math.max(max_seq, Number(m[4]));
    }
    const entry = { latest, max_seq, needs_newline: text.length > 0 && !text.endsWith("\n") };
    days.set(day, entry);
    return entry;
  }

  function write(day, line) {
    const entry = load(day);
    fs.mkdirSync(dir, { recursive: true });
    const prefix = entry.needs_newline ? "\n" : "";
    fs.appendFileSync(file_for(day), `${prefix}${JSON.stringify(line)}\n`, "utf8");
    entry.needs_newline = false;
    entry.latest.set(line.id, line);
    return line;
  }

  /** A brand-new message: new id, rev 1, live. */
  function append_new({ thread, role, text, attach = null, tools = null, woke = false }) {
    const stamp = local_stamp(clock.now(), zone);
    const entry = load(stamp.day);
    const seq = entry.max_seq + 1;
    entry.max_seq = seq;
    return write(stamp.day, {
      id: `m_${stamp.compact}_${String(seq).padStart(4, "0")}`,
      rev: 1,
      at: stamp.iso,
      thread,
      role,
      text,
      attach: attach && attach.length ? attach : null,
      tools: tools && tools.length ? tools : null,
      woke: Boolean(woke),
      state: "live",
    });
  }

  /**
   * A new revision of an existing id: the latest version with `changes` laid over it,
   * rev + 1, stamped now, appended to the id's own day file.
   */
  function revise(id, changes) {
    const day = day_of_id(id);
    if (!day) throw new Error(`not a day-store id: ${id}`);
    const prev = load(day).latest.get(id);
    if (!prev) throw new Error(`no such line: ${id}`);
    const next = { ...prev, ...changes, id, rev: prev.rev + 1, at: local_stamp(clock.now(), zone).iso };
    if (next.attach && !next.attach.length) next.attach = null;
    if (next.tools && !next.tools.length) next.tools = null;
    return write(day, next);
  }

  function get(id) {
    const day = day_of_id(id);
    return day ? (load(day).latest.get(id) || null) : null;
  }

  /** Every line in the day's file, revisions included, in file order. */
  function read_day(day) {
    try { return parse_lines(fs.readFileSync(file_for(day), "utf8")); }
    catch (err) { if (err.code === "ENOENT") return []; throw err; }
  }

  return {
    dir,
    file_for,
    append_new,
    revise,
    get,
    read_day,
    current: (day) => fold_current(read_day(day)),
  };
}

module.exports = { create_day_store, day_of_id, fold_current, ID_PATTERN };
