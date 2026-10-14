// ============================================================
// gateway/present/away.js — what he said while she was away, until it is part of the conversation
//
// A wake that spoke left the words in held.json ({ items: [{ at, line, thread, text }] },
// wake.js). The client cannot be pushed to, so they reach her conversation on her next
// turn (present/index.js prepare → on_response):
//
//   ① upstream, always: each held line goes in as his own message (an assistant overlay,
//      window.js) right before her new line, in the order he said them.
//   ② the client, while wake.away_prefix is on (present.json, default on): the answer
//      starts with a prefix block, before the model's first token (stream_filter.js
//      create_sse_prefix / create_json_prefix), one item per held line in the original
//      words, each with its local time, a blank line between items, then a boundary line,
//      a blank line, and the model's answer:
//          — 你不在的时候 · 17:42 —
//          不用回我……你先睡。
//
//          — 你不在的时候 · 17:45 —
//          你歇着……你说一声就行。
//
//          ————
//
//          醒了。……
//      So the words enter the client's history as the start of his reply (定 D). The
//      words of the block are AWAY_WORDS, HEAD_MARK and BOUNDARY below.
//
// ─── Who carries the words afterwards ───
// The prefix decides it, per showing:
//   · shown with the prefix → the client's history carries them from her next turn on.
//     The overlay is replayed every turn until the lines are confirmed, then taken out.
//   · shown without it (the switch is off) → the client's history never will, so the
//     window does: the overlay is `lasting` (window.js). It is replayed for the window's
//     whole life wherever its anchor is in the history, comes along into a fork, survives
//     a flip while its anchor is still among the raw lines (window.js open_next, and the
//     wake snapshot rebuilt after it, wake.js rebase), and a pack that folds its anchor
//     away reads it as his line (pack.js). The day report reads the day store, where a
//     wake's words are a line of their own (woke: true), either way.
// Each held line remembers how it was last shown (`shown.prefixed`); the switch is read
// on every request, and a request of the same turn again shows it the way the switch
// says then (see "Which turn shows them").
//
// ─── What the day store keeps ───
// The reply line is what the client saw: the prefix block (when there was one), then the
// answer. The client sends that reply back in every later history, and threads.js
// recognises a stored reply only by the fingerprint of its text; a stored reply without
// the prefix would no longer match what the client shows, and the next turn would read
// as an edit of his reply (a new revision) instead of the same line. The cost is that
// prefixed words sit in the day store twice — once as the woke line (when he said them),
// once inside this reply (when she saw them) — which is also what happened.
//
// ─── Confirmed ───
// On every chat request, before anything else here, against the client's history as
// sent. A held line is confirmed when
//   · one of his replies in that history contains an item block that was shown for it
//     (compared with whitespace runs collapsed, as fingerprints are), whichever way it was
//     last shown: the history carries the words, so its overlays go — from every window,
//     except that a lasting overlay is taken out only in this conversation's window
//     (another conversation may hold it with no prefix in its history);
//   · or it was last shown without the prefix, and the history holds the turn that
//     showed it, a reply of his after that turn, and a line of hers after that reply: he
//     answered with the words in front of him and she went on. Its overlays stay.
// Confirmed lines leave held.json (so they no longer count against wake.held_cap) and are
// remembered in held.json `given` (the last GIVEN_KEPT turns) for one case: she rewinds
// and regenerates the turn that showed them. That puts them back as held, shown on that
// turn again.
//
// ─── Which turn shows them ───
//   · a new turn of hers (her line is last) with held lines → every held line not yet
//     confirmed is shown on this turn: lines shown on an earlier turn whose answer never
//     reached her history (the request failed, she edited her line, she speaks in
//     another conversation) move here, and their overlays at the old turn are taken out
//     so upstream never gets them twice (a lasting one only in this conversation's window)
//   · the same turn again — a resend within 2 minutes, a regenerate, a retry after an
//     upstream error (threads.js gives them the same turn id) → the same lines, the
//     overlays already in place, shown the way the switch says now: with the block when it
//     is on (the same block as before when it was on then too), without it when it is
//     off, and the overlays lasting or not to match. A line held after that turn was shown
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

// The words of the prefix block (hers: change them here).
const AWAY_WORDS = "你不在的时候";   // in each item's head line
const HEAD_MARK = "—";               // around each item's head line
const BOUNDARY = "————";             // between the items and his answer

const KIND = "away";
const GIVEN_KEPT = 8;
const LINES_KEPT = 4;          // item blocks remembered per held line (each block it was shown in)
const GAP = "\n\n";

const is_plain = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const flat = (text) => String(text ?? "").replace(/\s+/g, " ").trim();

/** The local HH:MM of a held line's time. */
function hhmm_of(at, zone) {
  const ms = Date.parse(at);
  return Number.isFinite(ms) ? local_stamp(ms, zone).iso.slice(11, 16) : "--:--";
}

/** One held line as the client sees it: its head line with the local time, then its words. */
function item_block(item, zone) {
  return `${HEAD_MARK} ${AWAY_WORDS} · ${hhmm_of(item.at, zone)} ${HEAD_MARK}\n${String(item.text ?? "").trim()}`;
}

/** The block the client sees before his answer: the items, the boundary, the gap before the answer. */
function prefix_block(items, zone) {
  return items.length ? items.map((i) => item_block(i, zone)).join(GAP) + GAP + BOUNDARY + GAP : "";
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

const lines_of = (shown) => (is_plain(shown) && Array.isArray(shown.lines) ? shown.lines : []);
// a held line shown before `prefixed` was recorded was shown with the prefix
const was_prefixed = (shown) => !is_plain(shown) || shown.prefixed !== false;

/**
 * @param data_root  LOCI_GATEWAY_DATA (held.json)
 * @param threads    threads.js instance (every window that may hold an away overlay)
 * @param prefix_on  () → whether the client gets the prefix block (present.json wake.away_prefix)
 */
function create_away({ data_root, threads, zone, prefix_on = () => true, log = console.error }) {
  const held_file = path.join(data_root, "held.json");

  function read_held() {
    const got = read_json_file(held_file);
    if (!got.ok) return { ok: false, error: got.error };
    if (got.value === null) return { ok: true, doc: { items: [] } };
    if (!is_plain(got.value) || !Array.isArray(got.value.items)) return { ok: false, error: "held.json does not hold { items: [...] }" };
    return { ok: true, doc: { ...got.value, items: got.value.items.slice() } };
  }

  /**
   * Take the away overlays for these keys out of the windows that have them, except those
   * at `except_turn`. A lasting overlay is taken out only in the window of thread `here`.
   */
  function drop_overlays(keys, { except_turn = null, here }) {
    if (!keys.size) return;
    for (const t of threads.list()) {
      const w = t.window;
      if (!is_plain(w) || !Array.isArray(w.overlays)) continue;
      const kept = w.overlays.filter((o) => !(o.kind === KIND && keys.has(o.key) && o.turn !== except_turn
                                              && (!o.lasting || t.id === here)));
      if (kept.length !== w.overlays.length) { w.overlays = kept; threads.save(t.id); }
    }
  }

  /** His replies in the client's history, flattened, for the confirmation check. */
  function replies_in(messages) {
    return messages.filter((m) => m && m.role === "assistant")
      .map((m) => flat(to_said(m).text)).filter(Boolean);
  }

  /** Does the history hold `turn`, a reply of his after it, and a line of hers after that? */
  function answered(messages, ids, turn) {
    const at = ids.indexOf(turn);
    if (at < 0) return false;
    const reply = messages.findIndex((m, i) => i > at && m && m.role === "assistant");
    return reply > at && messages.some((m, i) => i > reply && m && m.role === "user");
  }

  /** Held lines the client's history shows as part of the conversation: out of held.json (see the header). */
  function confirm(doc, messages, ids, here) {
    const replies = replies_in(messages);
    const by_block = [];
    const by_answer = [];
    for (const i of doc.items) {
      if (!is_plain(i.shown)) continue;
      const lines = lines_of(i.shown);
      if (lines.length && replies.length && lines.some((line) => {
        const want = flat(line);
        return want && replies.some((r) => r.includes(want));
      })) by_block.push(i);
      else if (!was_prefixed(i.shown) && answered(messages, ids, i.shown.turn)) by_answer.push(i);
    }
    const confirmed = [...by_block, ...by_answer];
    if (!confirmed.length) return [];
    const keys = new Set(confirmed.map(held_key));
    doc.items = doc.items.filter((i) => !keys.has(held_key(i)));
    const given = Array.isArray(doc.given) ? doc.given.slice() : [];
    for (const i of confirmed) {
      const turn = i.shown.turn;
      let entry = given.find((g) => g.turn === turn);
      if (!entry) { entry = { turn, items: [] }; given.push(entry); }
      const { shown, ...plain } = i;
      entry.items.push({ ...plain, lines: lines_of(shown), prefixed: was_prefixed(shown) });
    }
    doc.given = given.slice(-GIVEN_KEPT);
    drop_overlays(new Set(by_block.map(held_key)), { here });
    return [...keys];
  }

  /** Record how these items are shown on this turn: with the block (its item blocks kept for confirming) or without. */
  function mark_shown(items, turn, thread_id, prefixed) {
    for (const i of items) {
      const before = lines_of(i.shown);
      const block = item_block(i, zone);
      const lines = prefixed ? [...before.filter((l) => l !== block), block].slice(-LINES_KEPT) : before;
      i.shown = { turn, thread: thread_id, lines, prefixed };
    }
  }

  /** Make sure this window has one assistant overlay per item, before her line, in order, lasting or not. */
  function place_overlays(w, turn, items, lasting) {
    for (const i of items) {
      const key = held_key(i);
      const there = w.overlays.find((o) => o.kind === KIND && o.turn === turn && o.key === key);
      if (there) { there.lasting = lasting; continue; }
      win.add_overlay(w, { kind: KIND, anchor: turn, place: "before", role: "assistant",
                           text: String(i.text ?? "").trim(), turn, key, said_at: i.at ?? null, lasting });
    }
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
    const before = JSON.stringify(doc);
    const history = Array.isArray(messages) ? messages : [];
    const ids = win.map_ids(history, Array.isArray(thread.branch) ? thread.branch : [], seen.cursor);
    const confirmed = confirm(doc, history, ids, thread.id);

    let shown = [];
    let prefixed = false;
    const turn = seen.kind === "turn" ? seen.turn : null;
    if (turn) {
      shown = doc.items.filter((i) => is_plain(i.shown) && i.shown.turn === turn);
      if (!shown.length) {
        const given = Array.isArray(doc.given) ? doc.given.find((g) => g.turn === turn) : null;
        if (given && given.items.length) {
          // she rewound and regenerated the turn that showed them: held again, on that turn
          const back = given.items.map(({ lines, prefixed: was, ...plain }) => (
            { ...plain, shown: { turn, thread: thread.id, lines: Array.isArray(lines) ? lines : [], prefixed: was !== false } }));
          doc.items = [...back, ...doc.items];
          doc.given = doc.given.filter((g) => g !== given);
          shown = back;
        } else if (doc.items.length) {
          shown = doc.items;
          drop_overlays(new Set(shown.map(held_key)), { except_turn: turn, here: thread.id });
        }
      }
      if (shown.length) {
        prefixed = prefix_on() !== false;
        mark_shown(shown, turn, thread.id, prefixed);
        place_overlays(w, turn, shown, !prefixed);
      }
    }

    if (JSON.stringify(doc) !== before) {
      try { write_json_file(held_file, doc); }
      catch (err) { log(`[gateway] present: held.json not written: ${err?.message || err}`); }
    }
    const prefix = shown.length && prefixed ? prefix_block(shown, zone) : null;
    const bits = [];
    if (confirmed.length) bits.push(`攥着的话确认${confirmed.length}句`);
    if (prefix) bits.push(`前缀${shown.length}句`);
    else if (shown.length) bits.push(`攥着的话${shown.length}句只给他（不补前缀）`);
    return { prefix, count: shown.length, confirmed, note: bits.join(" ") };
  }

  return { prepare };
}

module.exports = { create_away, prefix_block, item_block, KIND, AWAY_WORDS, HEAD_MARK, BOUNDARY };
