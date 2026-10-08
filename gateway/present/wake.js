// ============================================================
// gateway/present/wake.js — the gate, the wake turn, and what it settles to
//
// The heartbeat calls tick() once a minute. Each tick either costs nothing (a gate is
// shut) or runs one wake through own_turn.js, the one paid path.
//
// ─── The gate (every one must hold) ───
//   · wake is on, and present.json's wake section reads (null = do not wake)
//   · not in DND (dnd.js, the one place that decides it)
//   · inside a segment, when wake.segments.mode is "only"
//   · today's runs are under wake.daily_cap: a wake that ran counts, a paid failure
//     counts, a dry run counts; an abort and an unpaid failure do not
//   · what he said that she has not answered (held.json) is under wake.held_cap, when a
//     cap is set (null = no limit)
//   · a key exists (own_turn: the fixed key, or one borrowed from her latest accepted
//     request — after a restart there is none until she has spoken)
//   · a conversation to wake into: the thread that was active last (`last_at`) holds a
//     snapshot of its last answered request, not marked `walled` (see "The wall" below)
//   · she has been quiet for every_min (her latest request, or the latest line on any
//     thread, whichever is later)
//   · every_min since the last wake, times the backoff. It counts from the later of the
//     run's start stamp and its finish: a run is stamped before it goes out, so a
//     gateway that restarts mid-run, or right after one, does not wake again at once
//   · no other own turn is running
//   · today's day report, when one is due, is written (report_ready; true until the day
//     report exists)
//   · every_min is at least 15 unless wake.allow_short is set
// A shut gate writes one line to logs/present.jsonl when its reason changes, not one a
// minute.
//
// ─── What goes upstream (for the prefix cache) ───
//   [the last request of that thread as it was sent upstream, byte for byte, tools and
//    the other fields included] [his reply to it] [earlier wakes since then: letter,
//    reply, letter, reply …] [this wake's letter]
// A snapshot that carried no tools gets Loci's own tools instead (own_turn `tools:
// "loci"`): the wake card tells him he has his memory tools (§七.7), and a cache miss on
// that path is the price. A snapshot with tools keeps them byte for byte.
// The letter is wake_shell() with the wake card in force, plus last night's dream in
// full or the muse line when Loci's poke has one. A dream is handed over once, by its
// id; after a wake that carried it finishes, poke delivery is armed so her next message
// sends /api/loci/dream/wake, and the id goes into poke delivery's state
// (`dreamsDelivered`) so the chat path never hands the same dream over again, in any
// layer. The list is shared: a dream the chat path handed over first is not repeated by
// a wake either. A wake that failed leaves the dream to the next one.
// The snapshot lives in the private thread ledger (threads/<id>.json, `last_sent`:
// { at, request, reply, wakes: [{ letter, reply }], layout }), written by remember_turn() once
// an answer to a request has finished. It is never served by any route.
// 🔴 The letter never reaches the day store or the client: it exists in the wake request
//    and in the ledger's `wakes`, nowhere else.
//
// ─── After a flip ───
// The snapshot is the last request as it went upstream, so the moment a window flips
// (he folded it, a pack, the day report) it holds the old window: a wake built on it
// would resend everything the flip just let go of, and the next chat turn would not
// share its prefix. So every flip calls rebase(): the request's messages become what
// window.js assemble would build now — the client's system messages, the new carry, the
// client's messages from the new mark on — with no overlays (a new window starts with
// none); every other field of the request (tools included), his reply and the earlier
// wake pairs stay. To find the client's messages among the overlays, the snapshot keeps
// a `layout` (index.js, from the assembly): { head: how many leading messages are the
// client's system messages, lines: [[index in request.messages, line id or null], …] },
// one entry per client message after the head. A mark older than the snapshot's first
// line has the lines between taken from the day store, as plain messages; a snapshot with
// no layout cannot be rebuilt and is dropped (no wake until her next turn makes one).
//
// ─── The wall ───
// A wake refused as too long (wall.js wall_of_run: the context-length error) would be
// refused the same way every time, and each one is paid. So after such a refusal the
// earlier wake pairs are dropped from the snapshot (its request, the cached prefix, stays)
// and the next wake goes with only the letter on top; a snapshot refused with no pairs on
// it is marked `walled` and the gate stays shut on it ("walled", said once) until her next
// turn makes a new snapshot or a flip rebuilds it smaller (rebase clears the mark). One
// "wake_wall" line in the ledger says which, in Chinese. The refusal itself still settles
// as a paid failure below.
//
// ─── What it settles to ───
//   · her request arrives (owner_arrived(), called by the relay before anything else):
//     the wake in flight is aborted with "owner_arrived". Nothing is written — no line,
//     nothing held, no count — and it is not a failure.
//   · ok and silent (empty, 【无话】, "No response requested."): rest; nothing to push
//   · ok and he spoke: a day-store line with `woke: true`, and an entry in held.json
//     ({ at, line, thread, text }) for the away step to inject (away.js), handed on to
//     on_spoke for the push (push.js)
//   · 🔴 what "he spoke" means is own_turn's `text`: his words with any
//     【窗口摘要】…【/窗口摘要】 block already taken out (stream_filter.js). The wake replays
//     the last turn's request, and a compression reminder there is still in front of him,
//     so a wake may fold the window. A closed block is handed to on_fold and does what a
//     fold in a chat turn does — it becomes the carry and flips the window (index.js
//     after_summary, the one path for both) — as long as the window the wake started in is
//     still the current one. A block never reaches the day store, held.json, the push,
//     the away prefix or the ledger's wake pairs. A wake that only folded said nothing.
//   · paid failure: counted, takes a place under the cap, and the interval backs off
//     ×2, then ×4, never more; never given up on; one success resets it
//   · unpaid failure: not counted. "connect" (upstream unreachable) backs off as well,
//     so a dead upstream is not knocked on every minute
//   · wake.dry_run: everything up to the call — gate, letter, assembly — then one log
//     line "would_wake" instead of the call. A dry run stamps and counts like a wake
//     (dry_run in today), so a day of dry runs shows the day wake would have had.
//
// Bookkeeping: <LOCI_GATEWAY_DATA>/counters.json under `wake` (today's counts, stamps,
// backoff, the last result, dream ids handed over), held.json, logs/present.jsonl.
// Lines carry names, counts and reasons, never what was said or the letter.
// ============================================================

const fs = require("fs");
const path = require("path");
const poke = require("../poke_delivery.js");
const { local_stamp } = require("./clock.js");
const { dnd_state, in_span, local_minute, minute_of_day } = require("./dnd.js");
const { wake_shell } = require("./prompts.js");
const { wall_of_run } = require("./wall.js");

const MIN_EVERY_MIN = 15;
const MAX_BACKOFF_FACTOR = 4;
const BACKOFF_STEPS_KEPT = 8;
const POKE_TIMEOUT_MS = 8000;
const NEXT_AT_HORIZON_MIN = 3 * 24 * 60;   // how far ahead next_at is looked for
const WAKES_KEPT = 32;                      // earlier wake pairs replayed, at most
const DREAMS_KEPT = 32;

// why the next wake will not come when the interval says (the panel's words)
const WHY_WORDS = {
  off: "自动唤醒关着",
  settings_unreadable: "设置读不出来，这一拍不叫",
  state_unreadable: "唤醒的账（counters.json）读不出来，先不叫",
  held_unreadable: "攥着的话（held.json）读不出来，先不叫",
  held_cap: "他说的话 ta 还没回的已经到上限了，等 ta 开口再叫",
  no_key: "还没有钥匙：网关重启后要等 ta 先说一句（或者配 LOCI_UPSTREAM_KEY）",
  no_thread: "还没有能接着说下去的对话",
  cap: "今天叫的次数到上限了，明天再叫",
  dnd: "在免打扰时段里，等免打扰结束",
  segment: "不在唤醒时间段里，等下一个时间段",
  backoff: "上次没叫成，间隔拉长了",
  walled: "上一轮对话加上唤醒信已经超过模型的窗口（撞墙了），再叫也是白花钱；等 ta 下次开口或者换窗以后再叫",
};
// gate reasons that only mean "not yet": next_at already says when
const WAIT_WORDS = {
  quiet: "ta 刚说过话，还没静够",
  cooldown: "离上次唤醒还不够久",
  busy: "网关自己的另一轮正在跑",
  report: "今天的日报还没写",
  owner_arrived: "ta 来了",
};
const RESULT_WORDS = {
  spoke: "说了话",
  silent: "醒了，没说话",
  paid_failure: "没叫成（花了钱）",
  unpaid_failure: "没叫成（没发出去）",
  aborted: "ta 来了，停下了",
  would_wake: "只记账：到点了，没真叫",
};

function blank_today(day = null) { return { day, woke: 0, spoke: 0, paid_failures: 0, dry_run: 0 }; }
function blank_state() {
  return {
    today: blank_today(), last_started_at: null, last_finished_at: null, backoff: 0,
    last: null, last_ok_at: null, failures_since_ok: 0, dreams_delivered: [],
  };
}

const is_plain = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

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

/** A snapshot's layout: where the client's own messages sit in the request (see the header). */
function valid_layout(layout, length) {
  return is_plain(layout) && Number.isInteger(layout.head) && layout.head >= 0 && layout.head <= length
    && Array.isArray(layout.lines)
    && layout.lines.every((e) => Array.isArray(e) && Number.isInteger(e[0]) && e[0] >= layout.head && e[0] < length);
}

/** A stored line as a plain message's text: its words, its attachments as their kinds. */
function plain_text(line) {
  const marks = (Array.isArray(line.attach) ? line.attach : []).map((k) => `[${k}]`).join(" ");
  const text = String(line.text ?? "");
  return marks ? (text ? `${text} ${marks}` : marks) : text;
}

/** A snapshot wake can build on: an answered request that went upstream. */
function usable_snapshot(snap) {
  return is_plain(snap) && is_plain(snap.request) && Array.isArray(snap.request.messages)
    && snap.request.messages.length > 0 && typeof snap.reply === "string" && snap.reply.trim() !== "";
}

/**
 * The wake request: the snapshot's request untouched, his reply, earlier wakes, the letter.
 * @returns { messages, tools, extra, model, prefix, replayed }
 */
function assemble(snap, letter) {
  const { model = null, messages, tools, stream, stream_options, ...extra } = snap.request;
  const pairs = (Array.isArray(snap.wakes) ? snap.wakes : [])
    .filter((p) => is_plain(p) && typeof p.letter === "string" && typeof p.reply === "string" && p.reply !== "");
  const out = [...messages, { role: "assistant", content: snap.reply }];
  for (const p of pairs) out.push({ role: "user", content: p.letter }, { role: "assistant", content: p.reply });
  out.push({ role: "user", content: letter });
  return {
    messages: out,
    tools: Array.isArray(tools) && tools.length ? tools : "loci",
    extra, model,
    prefix: messages.length,
    replayed: pairs.length,
  };
}

/** Loci's first dream, unless its id was handed over already; { id, text } or null. */
function pick_dream(poked, delivered) {
  const d = Array.isArray(poked?.dreams) ? poked.dreams[0] : null;
  const text = String(d?.内容 ?? d?.content ?? "").trim();
  if (!d || !text) return null;
  const id = d.id ?? null;
  if (id !== null && delivered.includes(String(id))) return null;
  return { id: id === null ? null : String(id), text };
}

/**
 * @param data_root      LOCI_GATEWAY_DATA (counters.json, held.json, logs/present.jsonl)
 * @param threads        threads.js instance (the ledger holding the snapshots)
 * @param day_store      day_store.js instance (where what he said is written, woke: true)
 * @param settings       settings.js instance (for_wake, load().values.own)
 * @param prompts        prompts.js instance (the wake card in force)
 * @param own_turn       own_turn.js instance
 * @param loci_address   Loci's MCP address; the poke route hangs off the same root
 * @param poke_state     poke delivery's state file (armed for dream/wake after a dream)
 * @param fetch_poke     (address) → Loci's poke reply; the real REST call by default
 * @param report_ready   () → false while today's due day report is not written yet
 * @param on_spoke       (held item) → called once what he said is held (push.js on_spoke)
 * @param on_fold        ({ thread, window, summary, half }) → a wake that wrote a summary
 *                       block (index.js after_summary); `window` is the window it started in
 */
function create_wake({
  data_root, threads, day_store, settings, prompts, own_turn, clock, zone, log = console.error,
  loci_address = poke.DEFAULT_ADDRESS, poke_state = path.join(data_root, "state", "poke-window.json"),
  fetch_poke = (address) => poke._internal.fetch_poke(address, { timeout_ms: POKE_TIMEOUT_MS }),
  report_ready = () => true, on_spoke = null, on_fold = null,
}) {
  const counters_file = path.join(data_root, "counters.json");
  const held_file = path.join(data_root, "held.json");
  const log_file = path.join(data_root, "logs", "present.jsonl");

  let arrivals = 0;           // owner requests seen by this process
  let owner_seen_at = 0;      // the latest of them
  let last_gate = null;       // the shut-gate reason last written down
  let ticking = false;

  const iso = (ms) => local_stamp(ms, zone).iso;
  const day_of = (ms) => local_stamp(ms, zone).day;

  function write_log(entry) {
    try {
      fs.mkdirSync(path.dirname(log_file), { recursive: true });
      fs.appendFileSync(log_file, `${JSON.stringify({ at: iso(clock.now()), ...entry })}\n`, "utf8");
    } catch (err) { log(`[gateway] present: wake log not written: ${err?.message || err}`); }
  }

  function gate_log(reason) {
    if (reason === last_gate) return;
    last_gate = reason;
    write_log({ event: "wake_gate", blocked: reason, words: WHY_WORDS[reason] || WAIT_WORDS[reason] || null });
  }

  // ———— The books ————

  function load_state() {
    const got = read_json_file(counters_file);
    if (!got.ok) return { ok: false, error: got.error };
    if (got.value !== null && !is_plain(got.value)) return { ok: false, error: "counters.json does not hold a JSON object" };
    const all = got.value || {};
    const kept = is_plain(all.wake) ? all.wake : {};
    const wake = { ...blank_state(), ...kept };
    wake.today = { ...blank_today(), ...(is_plain(kept.today) ? kept.today : {}) };
    if (!Array.isArray(wake.dreams_delivered)) wake.dreams_delivered = [];
    return { ok: true, all, wake };
  }

  /** Read, change, write back — other sections of counters.json are left as they are. */
  function update_state(change) {
    const got = load_state();
    if (!got.ok) throw new Error(`counters.json cannot be read: ${got.error}`);
    change(got.wake);
    write_json_file(counters_file, { ...got.all, wake: got.wake });
    return got.wake;
  }

  function today_of(w, ms) {
    const day = day_of(ms);
    if (w.today.day !== day) w.today = blank_today(day);
    return w.today;
  }

  function used_on(w, day) {
    return w.today.day === day ? w.today.woke + w.today.paid_failures + w.today.dry_run : 0;
  }

  function read_held() {
    const got = read_json_file(held_file);
    if (!got.ok) return { ok: false, error: got.error };
    if (got.value === null) return { ok: true, items: [], rest: {} };
    if (!is_plain(got.value) || !Array.isArray(got.value.items)) return { ok: false, error: "held.json does not hold { items: [...] }" };
    return { ok: true, items: got.value.items, rest: got.value };
  }

  function latest_thread() {
    let best = null;
    for (const t of threads.list()) if (!best || (t.last_at || 0) > (best.last_at || 0)) best = t;
    return best;
  }

  function last_owner_at() {
    let at = owner_seen_at;
    for (const t of threads.list()) if ((t.last_at || 0) > at) at = t.last_at;
    return at;
  }

  // ———— The gate ————

  /**
   * Every gate at `now`. With `walk`, also when the next wake can come (next_at) and why
   * it is later than the interval says (next_why).
   * @returns { go, block, next_at, next_why, s, thread, w }
   */
  function evaluate(now, { walk = false } = {}) {
    const shut = (why, extra = {}) => ({ go: false, block: why, next_at: null, next_why: why, ...extra });
    const s = settings.for_wake();
    if (s === null) return shut("settings_unreadable");
    if (!s.on) return shut("off", { s });
    const st = load_state();
    if (!st.ok) return shut("state_unreadable", { s });
    const held = read_held();
    if (!held.ok) return shut("held_unreadable", { s });
    if (s.held_cap !== null && held.items.length >= s.held_cap) return shut("held_cap", { s });
    if (!own_turn.status().key) return shut("no_key", { s });
    const thread = latest_thread();
    if (!thread || !usable_snapshot(thread.last_sent)) return shut("no_thread", { s });
    if (thread.last_sent.walled) return shut("walled", { s });

    const w = st.wake;
    const every = (s.allow_short ? s.every_min : Math.max(MIN_EVERY_MIN, s.every_min)) * 60000;
    const quiet_end = last_owner_at() + every;
    const since = Math.max(w.last_started_at || 0, w.last_finished_at || 0);
    const factor = Math.min(2 ** (w.backoff || 0), MAX_BACKOFF_FACTOR);
    const cool_end = since ? since + every * factor : 0;

    const window_block = (ms) => {
      if (used_on(w, day_of(ms)) >= s.daily_cap) return "cap";
      if (dnd_state(s, ms, zone).dnd) return "dnd";
      if (s.segments.mode === "only") {
        const minute = local_minute(ms, zone);
        if (!s.segments.list.some((seg) => in_span(minute, minute_of_day(seg.from), minute_of_day(seg.to)))) return "segment";
      }
      return null;
    };

    const start = Math.max(now, quiet_end, cool_end);
    const waiting = start <= now ? null
      : cool_end >= quiet_end ? (w.backoff > 0 ? "backoff" : "cooldown") : "quiet";
    const base = { s, thread, w };

    let next_at = null;
    let next_why = null;
    if (walk) {
      let at = start;
      let first = null;
      for (let i = 0; i < NEXT_AT_HORIZON_MIN; i++) {
        const b = window_block(at);
        if (!b) { next_at = at; break; }
        first = first || b;
        at = Math.floor(at / 60000) * 60000 + 60000;
      }
      next_why = first || (waiting === "backoff" ? "backoff" : null);
    }

    const now_block = window_block(now) || waiting
      || (own_turn.busy() ? "busy" : null)
      || (report_ready() ? null : "report");
    return { go: !now_block, block: now_block, next_at, next_why, ...base };
  }

  // ———— One tick ————

  async function tick() {
    if (ticking) return { ran: false, why: "ticking" };
    ticking = true;
    try { return await tick_once(); }
    finally { ticking = false; }
  }

  async function tick_once() {
    const now = clock.now();
    const g = evaluate(now);
    if (!g.go) { gate_log(g.block); return { ran: false, why: g.block }; }

    const seen = arrivals;
    const thread = g.thread;
    const snap = thread.last_sent;
    const window = thread.window && typeof thread.window === "object" ? thread.window.name || null : null;
    let poked = null;
    try { poked = await fetch_poke(loci_address); }
    catch (err) { write_log({ event: "wake", phase: "poke", error: String(err?.message || err).slice(0, 300) }); }
    // she may have come while Loci was asked
    if (arrivals !== seen) { gate_log("owner_arrived"); return { ran: false, why: "owner_arrived" }; }
    if (own_turn.busy()) { gate_log("busy"); return { ran: false, why: "busy" }; }
    last_gate = null;

    const dream = pick_dream(poked, [...g.w.dreams_delivered, ...chat_delivered_dreams()]);
    const muse = Number(poked?.muse_pending) > 0;
    const letter = wake_shell({ card: prompts.current("wake"), dream: dream ? dream.text : null, muse });
    const built = assemble(snap, letter);
    const shape = { thread: thread.id, messages: built.messages.length, prefix: built.prefix,
                    wakes_replayed: built.replayed, letter_chars: letter.length, tools: Array.isArray(built.tools) ? built.tools.length : built.tools,
                    dream: Boolean(dream), muse };

    if (g.s.dry_run) {
      update_state((w) => {
        w.last_started_at = now;
        w.last_finished_at = now;
        today_of(w, now).dry_run += 1;
        w.last = { at: now, result: "would_wake" };
      });
      write_log({ event: "wake", result: "would_wake", ...shape });
      return { ran: false, result: "would_wake", ...shape };
    }

    const own_models = settings.load().values.own.models;
    const models = own_models.length ? null : (built.model ? [String(built.model)] : null);
    // the stamp goes down before the request: a restart mid-run does not wake again at once
    update_state((w) => { w.last_started_at = now; });
    const r = await own_turn.run({ kind: "wake", messages: built.messages, tools: built.tools, models, extra: built.extra });
    after_wall(r, thread, snap);
    return settle({ r, thread, snap, letter, dream, shape, window });
  }

  /**
   * The wake was refused as too long: the same request cannot succeed. Earlier wake pairs
   * go; a snapshot refused with none is marked walled (see "The wall" in the header).
   */
  function after_wall(r, thread, snap) {
    const wall = wall_of_run(r);
    if (!wall) return;
    try {
      const now_snap = threads.get(thread.id)?.last_sent;
      if (!now_snap || now_snap.at !== snap.at) return;   // a newer turn of hers already replaced it
      const pairs = Array.isArray(now_snap.wakes) ? now_snap.wakes.length : 0;
      if (pairs) now_snap.wakes = [];
      else now_snap.walled = clock.now();
      threads.save(thread.id);
      write_log({ event: "wake_wall", thread: thread.id, pattern: wall.pattern, limit: wall.limit, dropped_wakes: pairs,
        words: pairs ? `唤醒撞墙了：前面 ${pairs} 次唤醒的来回不再带上，下次只带上一轮对话和唤醒信` : WHY_WORDS.walled });
    } catch (err) { log(`[gateway] present: handling a wake that hit the wall failed: ${err?.message || err}`); }
  }

  function settle({ r, thread, snap, letter, dream, shape, window }) {
    const finished = clock.now();
    if (r.outcome === "busy") {
      gate_log("busy");
      return { ran: false, why: "busy" };
    }

    let result;
    if (r.outcome === "aborted") result = "aborted";
    else if (r.outcome === "paid") result = "paid_failure";
    else if (r.outcome === "unpaid") result = "unpaid_failure";
    else result = r.silent ? "silent" : "spoke";

    let line_id = null;
    if (result === "spoke") {
      try {
        const line = day_store.append_new({ thread: thread.id, role: "assistant", text: r.text, woke: true });
        line_id = line.id;
      } catch (err) { log(`[gateway] present: the wake line was not written: ${err?.message || err}`); }
      try {
        const held = read_held();
        if (!held.ok) throw new Error(held.error);
        const item = { at: iso(finished), line: line_id, thread: thread.id, text: r.text };
        write_json_file(held_file, { ...held.rest, items: [...held.items, item] });
        // ── push (push.js): fire and forget; a push that fails or throws never touches the wake ──
        if (on_spoke) Promise.resolve().then(() => on_spoke(item))
          .catch((err) => log(`[gateway] present: push after the wake failed: ${err?.message || err}`));
      } catch (err) { log(`[gateway] present: what he said was not held: ${err?.message || err}`); }
    }

    if (result === "spoke" || result === "silent") {
      // the next wake replays this one, as long as no newer turn of hers replaced the snapshot
      const now_snap = threads.get(thread.id)?.last_sent;
      if (r.text !== "" && now_snap && now_snap.at === snap.at) {
        now_snap.wakes = [...(Array.isArray(now_snap.wakes) ? now_snap.wakes : []), { letter, reply: r.text }].slice(-WAKES_KEPT);
        threads.save(thread.id);
      }
      if (dream) arm_dream_wake(dream);
      // he folded the window during the wake: after the pair above, as a chat turn keeps
      // its snapshot before it flips
      if (r.summary && on_fold) {
        try { on_fold({ thread: thread.id, window, summary: r.summary.closed ? r.summary.text : null, half: !r.summary.closed }); }
        catch (err) { log(`[gateway] present: the fold after the wake failed: ${err?.message || err}`); }
      }
    }

    const w = update_state((w) => {
      w.last_finished_at = finished;
      w.last = { at: finished, result };
      if (result === "aborted") return;
      const today = today_of(w, finished);
      if (result === "spoke" || result === "silent") {
        today.woke += 1;
        if (result === "spoke") today.spoke += 1;
        w.backoff = 0;
        w.failures_since_ok = 0;
        w.last_ok_at = finished;
        if (dream && dream.id !== null) w.dreams_delivered = [...w.dreams_delivered, dream.id].slice(-DREAMS_KEPT);
      } else if (result === "paid_failure") {
        today.paid_failures += 1;
        w.backoff = Math.min((w.backoff || 0) + 1, BACKOFF_STEPS_KEPT);
        w.failures_since_ok += 1;
      } else if (r.reason === "connect") {
        w.backoff = Math.min((w.backoff || 0) + 1, BACKOFF_STEPS_KEPT);
        w.failures_since_ok += 1;
      }
    });

    write_log({ event: "wake", result, run: r.run, reason: r.reason, line: line_id, backoff: w.backoff, ...shape });
    return { ran: true, result, reason: r.reason, line: line_id, ...shape };
  }

  /** Dreams the chat path (poke_delivery.js) already handed over: a wake does not repeat them. */
  function chat_delivered_dreams() {
    const got = read_json_file(poke_state);
    const list = got.ok && is_plain(got.value) ? got.value.dreamsDelivered : null;
    return Array.isArray(list) ? list.map(String) : [];
  }

  /** Her next message sends dream/wake, and the chat path never hands this dream over again. */
  function arm_dream_wake(dream) {
    try {
      const got = read_json_file(poke_state);
      const state = got.ok && is_plain(got.value) ? got.value : {};
      const before = Array.isArray(state.dreamsDelivered) ? state.dreamsDelivered.map(String) : [];
      const delivered = dream.id === null || before.includes(dream.id) ? before : [...before, dream.id].slice(-DREAMS_KEPT);
      write_json_file(poke_state, { ...state, wakePending: true, dreamsDelivered: delivered });
    } catch (err) { log(`[gateway] present: dream/wake not armed: ${err?.message || err}`); }
  }

  // ———— Hooks ————

  /** Her chat request arrived: a wake in flight stops now, and her quiet starts again. */
  function owner_arrived() {
    arrivals += 1;
    owner_seen_at = clock.now();
    const running = own_turn.status().running;
    if (running && running.kind === "wake") own_turn.abort("owner_arrived");
  }

  /**
   * An answer to a request finished: that request (as it went upstream) and the reply
   * become the thread's snapshot. A reply that is a tool call, or empty, is not a place a
   * wake can continue from, so the previous snapshot stays.
   */
  function remember_turn(thread_id, request, reply, layout = null) {
    if (!is_plain(request) || !Array.isArray(request.messages) || !request.messages.length) return false;
    const text = String(reply?.text ?? "");
    if (!text.trim() || (Array.isArray(reply?.tools) && reply.tools.length)) return false;
    const thread = threads.get(thread_id);
    if (!thread) return false;
    thread.last_sent = { at: clock.now(), request: JSON.parse(JSON.stringify(request)), reply: text, wakes: [] };
    if (valid_layout(layout, request.messages.length)) thread.last_sent.layout = JSON.parse(JSON.stringify(layout));
    threads.save(thread.id);
    return true;
  }

  /**
   * The thread's window flipped (any way): the snapshot is rebuilt as the new window's
   * assembly, so the next wake goes out small and the next chat turn shares its prefix.
   * See "After a flip" in the header. Returns whether a snapshot was rebuilt.
   */
  function rebase(thread_id) {
    const thread = threads.get(thread_id);
    if (!thread || !usable_snapshot(thread.last_sent)) return false;
    const snap = thread.last_sent;
    const req = snap.request.messages;
    if (!valid_layout(snap.layout, req.length)) {
      thread.last_sent = null;
      threads.save(thread.id);
      return false;
    }
    const w = thread.window && typeof thread.window === "object" ? thread.window : {};
    const head = req.slice(0, snap.layout.head);
    const client = snap.layout.lines.map(([i, id]) => ({ m: req[i], id: id || null }));
    let start = 0;
    let lead = [];
    let carried = false;
    if (w.mark) {
      const at = client.findIndex((c) => c.id === w.mark);
      if (at >= 0) { start = at; carried = true; }
      else {
        // the mark sits before what the snapshot holds: those lines come from the day store
        const branch = Array.isArray(thread.branch) ? thread.branch : [];
        const first_id = (client.find((c) => c.id) || {}).id;
        const b_mark = branch.findIndex((e) => e.id === w.mark);
        const b_first = first_id ? branch.findIndex((e) => e.id === first_id) : -1;
        if (b_mark >= 0 && b_first > b_mark) {
          lead = branch.slice(b_mark, b_first).map((e) => {
            const line = day_store.get(e.id);
            return line ? { m: { role: line.role, content: plain_text(line) }, id: e.id } : null;
          }).filter(Boolean);
          carried = true;
        }
      }
    }
    const carry = carried && typeof w.carry === "string" && w.carry ? [{ role: "system", content: w.carry }] : [];
    const body = [...lead, ...client.slice(start)];
    const messages = [...head, ...carry, ...body.map((c) => c.m)];
    const offset = head.length + carry.length;
    thread.last_sent = {
      ...snap,
      request: { ...snap.request, messages },
      layout: { head: head.length, lines: body.map((c, k) => [offset + k, c.id]) },
    };
    delete thread.last_sent.walled;   // rebuilt smaller: worth a try again
    threads.save(thread.id);
    return true;
  }

  // ———— What the panel and /health see: numbers and names, never a letter or a reply ————

  function status() {
    const now = clock.now();
    const e = evaluate(now, { walk: true });
    const st = load_state();
    const w = st.ok ? st.wake : blank_state();
    const held = read_held();
    const s = e.s === undefined ? settings.for_wake() : e.s;
    const day = day_of(now);
    const today = w.today.day === day ? w.today : blank_today(day);
    return {
      state: s === null ? "settings_unreadable" : !s.on ? "off" : s.dry_run ? "dry_run" : "on",
      last: w.last ? { at: iso(w.last.at), result: w.last.result, result_words: RESULT_WORDS[w.last.result] || null } : null,
      next_at: e.next_at === null ? null : iso(e.next_at),
      next_why: e.next_why,
      next_why_words: e.next_why ? WHY_WORDS[e.next_why] || null : null,
      today: { woke: today.woke, spoke: today.spoke, paid_failures: today.paid_failures, dry_run: today.dry_run },
      held: held.ok ? held.items.length : null,
    };
  }

  function health() {
    const now = clock.now();
    const s = settings.for_wake();
    const st = load_state();
    const held = read_held();
    const w = st.ok ? st.wake : null;
    return {
      state: s === null ? "settings_unreadable" : !s.on ? "off" : s.dry_run ? "dry_run" : "on",
      last_ok_at: w && w.last_ok_at ? iso(w.last_ok_at) : null,
      last_ok_seconds_ago: w && w.last_ok_at ? Math.max(0, Math.round((now - w.last_ok_at) / 1000)) : null,
      failures_since_ok: w ? w.failures_since_ok : null,
      held: held.ok ? held.items.length : null,
    };
  }

  return { tick, owner_arrived, remember_turn, rebase, status, health, evaluate };
}

module.exports = { create_wake, assemble, pick_dream, usable_snapshot, valid_layout, WHY_WORDS, RESULT_WORDS, MIN_EVERY_MIN };
