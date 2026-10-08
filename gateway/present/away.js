// ============================================================
// gateway/present/away.js — what he said while she was away, until she has seen it
//
// A wake that spoke left the words in held.json ({ items: [{ at, line, thread, text }] },
// wake.js). The client cannot be pushed to, so they reach her conversation on her next
// turn, in two places at once (present/index.js prepare → on_response):
//
//   ① upstream: each held line goes in as his own message (an assistant overlay,
//      window.js) right before her new line, in the order he said them. The overlay is
//      replayed every turn, like every overlay, until the lines are confirmed — then it
//      is taken out, because by then the client's own history carries the words.
//   ② the client: the answer starts with a prefix block, before the model's first token
//      (stream_filter.js create_sse_prefix / create_json_prefix), one line per held line
//      in the original words (§七.13):
//          one line      （你不在的时候我说过：……）
//          two or more   （你不在的时候我说过 · 14:05：……）   — each with its local time
//      then a blank line, then the model's answer. So the words enter the client's
//      history as the start of his reply (定 D).
//
// ─── What the day store keeps ───
// The reply line is what the client saw: the prefix block, then the answer. The client
// sends that reply back in every later history, and threads.js recognises a stored
// reply only by the fingerprint of its text; a stored reply without the prefix would no
// longer match what the client shows, and the next turn would read as an edit of his
// reply (a new revision) instead of the same line. The cost is that the held words sit
// in the day store twice — once as the woke line (when he said them), once inside this
// reply (when she saw them) — which is also what happened.
//
// ─── Confirmed ───
// On every chat request, before anything else here: a held line is confirmed when one
// of his replies in the client's history contains a prefix line that was shown for it
// (compared with whitespace runs collapsed, as fingerprints are). Confirmed lines leave
// held.json, their overlays leave every window that has them, and they are remembered
// in held.json `given` (the last GIVEN_KEPT turns) for one case: she rewinds and
// regenerates the turn that showed them. That puts them back as held, shown on that
// turn again.
//
// ─── Which turn shows them ───
//   · a new turn of hers (her line is last) with held lines → every held line not yet
//     confirmed is shown on this turn: lines shown on an earlier turn whose answer never
//     reached her history (the request failed, she edited her line) move here, and
//     their overlays at the old turn are taken out so upstream never gets them twice
//   · the same turn again — a resend within 2 minutes, a regenerate, a retry after an
//     upstream error (threads.js gives them the same turn id) → the same lines, the
//     same block, the overlays already in place. A line held after that turn was shown
//     waits for her next new turn.
//   · a tool continuation shows nothing: the prefix went out with the turn's first answer
// Held lines from another conversation are shown too: they were said to her, and the
// conversation she speaks in now is where she will read them. Wake picks the thread it
// continues (the latest), but she may answer anywhere, and a line that waits for "its"
// thread may never be seen.
//
// Nothing here can block her turn: a held.json that cannot be read means no prefix this
// turn (logged), and the words stay where they are.
// ============================================================

const fs = require("fs");
const path = require("path");
const win = require("./window.js");
const { to_said } = require("./threads.js");
const { local_stamp } = require("./clock.js");
const { held_key } = require("./push.js");

const KIND = "away";
const GIVEN_KEPT = 8;
const LINES_KEPT = 4;          // prefix lines remembered per held line (each block it was shown in)
const SEPARATOR = "\n\n";

const is_plain = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const flat = (text) => String(text ?? "").replace(/\s+/g, " ").trim();

/** The local HH:MM of a held line's time. */
function hhmm_of(at, zone) {
  const ms = Date.parse(at);
  return Number.isFinite(ms) ? local_stamp(ms, zone).iso.slice(11, 16) : "--:--";
}

/** One prefix line per held line: no time for a single line, the time on each for two or more. */
function prefix_lines(items, zone) {
  if (items.length === 1) return [`（你不在的时候我说过：${String(items[0].text ?? "").trim()}）`];
  return items.map((i) => `（你不在的时候我说过 · ${hhmm_of(i.at, zone)}：${String(i.text ?? "").trim()}）`);
}

/** The block the client sees before his answer, separator included. */
function prefix_block(items, zone) {
  return items.length ? prefix_lines(items, zone).join("\n") + SEPARATOR : "";
}

function read_json_file(file) {
  let text;
  try { text = fs.readFileSync(file, "utf8"); }
  catch (err) { return err.code === "ENOENT" ? { ok: true, value: null } : { ok: false, error: err.message }; }
  try { return { ok: true, value: JSON.parse(text) }; }
  catch (err) { return { ok: false, error: `not JSON (${err.message})` }; }
}

function write_json_file(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(value, null, 1)}\n`, "utf8");
  fs.renameSync(tmp, file);
}

/**
 * @param data_root  LOCI_GATEWAY_DATA (held.json)
 * @param threads    threads.js instance (every window that may hold an away overlay)
 */
function create_away({ data_root, threads, zone, log = console.error }) {
  const held_file = path.join(data_root, "held.json");

  function read_held() {
    const got = read_json_file(held_file);
    if (!got.ok) return { ok: false, error: got.error };
    if (got.value === null) return { ok: true, doc: { items: [] } };
    if (!is_plain(got.value) || !Array.isArray(got.value.items)) return { ok: false, error: "held.json does not hold { items: [...] }" };
    return { ok: true, doc: { ...got.value, items: got.value.items.slice() } };
  }

  /** Take the away overlays for these keys out of every window that has them. */
  function drop_overlays(keys, except_turn = null) {
    if (!keys.size) return;
    for (const t of threads.list()) {
      const w = t.window;
      if (!is_plain(w) || !Array.isArray(w.overlays)) continue;
      const kept = w.overlays.filter((o) => !(o.kind === KIND && keys.has(o.key) && o.turn !== except_turn));
      if (kept.length !== w.overlays.length) { w.overlays = kept; threads.save(t.id); }
    }
  }

  /** His replies in the client's history, flattened, for the confirmation check. */
  function replies_in(messages) {
    return (Array.isArray(messages) ? messages : []).filter((m) => m && m.role === "assistant")
      .map((m) => flat(to_said(m).text)).filter(Boolean);
  }

  /** Held lines whose prefix the client's history shows: out of held.json, overlays out. */
  function confirm(doc, messages) {
    const shown = doc.items.filter((i) => is_plain(i.shown) && Array.isArray(i.shown.lines) && i.shown.lines.length);
    if (!shown.length) return [];
    const replies = replies_in(messages);
    if (!replies.length) return [];
    const confirmed = shown.filter((i) => i.shown.lines.some((line) => {
      const want = flat(line);
      return want && replies.some((r) => r.includes(want));
    }));
    if (!confirmed.length) return [];
    const keys = new Set(confirmed.map(held_key));
    doc.items = doc.items.filter((i) => !keys.has(held_key(i)));
    const given = Array.isArray(doc.given) ? doc.given.slice() : [];
    for (const i of confirmed) {
      const turn = i.shown.turn;
      let entry = given.find((g) => g.turn === turn);
      if (!entry) { entry = { turn, items: [] }; given.push(entry); }
      const { shown, ...plain } = i;
      entry.items.push({ ...plain, lines: shown.lines });
    }
    doc.given = given.slice(-GIVEN_KEPT);
    drop_overlays(keys);
    return [...keys];
  }

  /** Make sure this window has one assistant overlay per item, before her line, in order. */
  function place_overlays(w, turn, items) {
    for (const i of items) {
      const key = held_key(i);
      if (w.overlays.some((o) => o.kind === KIND && o.turn === turn && o.key === key)) continue;
      win.add_overlay(w, { kind: KIND, anchor: turn, place: "before", role: "assistant",
                           text: String(i.text ?? "").trim(), turn, key });
    }
  }

  function mark_shown(items, turn, thread_id) {
    const lines = prefix_lines(items, zone);
    items.forEach((i, n) => {
      const before = is_plain(i.shown) && Array.isArray(i.shown.lines) ? i.shown.lines : [];
      i.shown = { turn, thread: thread_id, lines: [...before.filter((l) => l !== lines[n]), lines[n]].slice(-LINES_KEPT) };
    });
  }

  /**
   * Called by prepare() for every chat request the threads module took, once the window
   * is current. Confirms, then decides this turn's prefix and places the overlays.
   * @param seen     threads.ingest() result
   * @param messages the client's messages as sent
   * @param thread   the thread (its window `w` is changed in place; the caller saves it)
   * @returns { prefix: string | null, count, confirmed: [keys], note }
   */
  function prepare({ seen, messages, thread, w }) {
    const got = read_held();
    if (!got.ok) {
      log(`[gateway] present: held.json cannot be read, no prefix this turn: ${got.error}`);
      return { prefix: null, count: 0, confirmed: [], note: "攥着的话读不出来" };
    }
    const doc = got.doc;
    let changed = false;
    const confirmed = confirm(doc, messages);
    if (confirmed.length) changed = true;

    let shown = [];
    const turn = seen.kind === "turn" ? seen.turn : null;
    if (turn) {
      shown = doc.items.filter((i) => is_plain(i.shown) && i.shown.turn === turn);
      if (!shown.length) {
        const given = Array.isArray(doc.given) ? doc.given.find((g) => g.turn === turn) : null;
        if (given && given.items.length) {
          // she rewound and regenerated the turn that showed them: held again, same block
          const back = given.items.map(({ lines, ...plain }) => ({ ...plain, shown: { turn, thread: thread.id, lines } }));
          doc.items = [...back, ...doc.items];
          doc.given = doc.given.filter((g) => g !== given);
          shown = back;
          changed = true;
        } else if (doc.items.length) {
          shown = doc.items;
          mark_shown(shown, turn, thread.id);
          drop_overlays(new Set(shown.map(held_key)), turn);
          changed = true;
        }
      }
      if (shown.length) place_overlays(w, turn, shown);
    }

    if (changed) {
      try { write_json_file(held_file, doc); }
      catch (err) { log(`[gateway] present: held.json not written: ${err?.message || err}`); }
    }
    const prefix = shown.length ? prefix_block(shown, zone) : null;
    const bits = [];
    if (confirmed.length) bits.push(`攥着的话确认${confirmed.length}句`);
    if (prefix) bits.push(`前缀${shown.length}句`);
    return { prefix, count: shown.length, confirmed, note: bits.join(" ") };
  }

  return { prepare };
}

module.exports = { create_away, prefix_block, prefix_lines, KIND, SEPARATOR };
