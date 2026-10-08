// ============================================================
// gateway/present/pack.js — the gateway folds a window itself (forced, manual, after the wall)
//
// Of the three lines on the fill only the force line makes the gateway act on its own
// (blueprint §七.5); the other two are reminders (compress.js). A pack is asked for:
//   forced   a finished turn's fill reached compress.force_pct (index.js, after the reply
//            is recorded), or upstream refused a turn as too long (wall.js)
//   manual   the panel's 「现在压」 (POST /present/compress)
// and starts at once, in the background. While compress.on is false nothing is packed on
// the gateway's own account; 「现在压」 still packs, because she asked for it.
//
// ─── What the pack reads ───
// Exactly the lines the next window will no longer hold raw: from the current window's
// mark (the carry's start; the branch's first line when there is no mark) up to, not
// including, the next window's mark — window.js mark_for_tail(branch, keep_raw), the
// first of the last keep_raw lines moved onto a line of hers, the same place a flip he
// makes himself puts it. Both are fixed from the branch as it stands when the pack starts;
// lines that arrive while it runs are after the new mark and stay raw.
// The text is each line's current version in the day store, oldest first, one per line:
//     [10-07 20:41] ta：……        [10-07 20:42] 你：……
// with attachments as their kinds and tools as their names (what the day store keeps).
// What the current carry holds from further back must not be lost by folding again, so
// the carry's summary part (the whole carry when its parts are unknown) goes in too, as
// the earlier summary to fold into the new one. The report part is not sent: the next
// window keeps it as it is (compose_carry).
// When the stretch is empty (everything since the mark is inside keep_raw) there is
// nothing a pack could fold: it ends as `nothing` and is not retried.
//
// ─── The turn ───
// One own turn (own_turn.js, kind "pack"): the client's system messages from this
// conversation's latest request (held in memory, never written) so he writes as himself,
// then compress_forced_shell (prompts.js) with the compress card in force, keep_raw, the
// earlier summary and the lines.
// It is offered Loci's tools (`tools: "loci"`, at most own.tool_rounds rounds): the card
// both packs share asks him to recall what was already saved today and save what should
// outlive the window, the forced shell says 「这一步只调工具」, and the lines this pack
// folds are about to leave the window for good — Loci is the only place they can still be
// kept whole. §七.7 rules on wake and the report only; a pack is the same kind of turn (a
// fresh window, the gateway's money, Loci-only tools, the same round cap) and the
// own-turn runner lists forced packing among its tool-loop users (blueprint §二.1). When
// Loci cannot list its tools the pack goes without them and still folds.
// Her arrival does not abort a pack (own_turn.js aborts only wake on arrival): her turn
// waits for it instead (wait_for, up to PACK_WAIT_MS), and goes as it is past that.
//
// ─── The result ───
// The summary is the last answer's words (all the run's words when the last answer has
// none), with any 【窗口摘要】 / 【/窗口摘要】 markers stripped — the shell asks for bare
// text, and a marker in the carry would be read as an instruction next window. An empty
// summary is a failure. Then window.js open_next: mark as planned, carry = the old
// report part + this summary, how = forced | manual, window number + 1, fill and offered
// lines cleared; Loci hears /cue/dropped for the old window (on_flip). A window that moved
// on while the pack ran (he folded it himself, or another flip) is left alone and the
// pack's summary dropped.
//
// ─── Failures ───
//   unpaid (nothing reached upstream: no key, busy with another own turn, cannot
//           connect) → tried again on the next heartbeat, not counted
//   paid   (upstream answered, a stream broke, the alarm, an empty summary) → counted,
//           and tried again after a back-off that doubles from BACKOFF_BASE_MS up to
//           BACKOFF_MAX_MS, so a provider that keeps refusing is not paid every minute
// A pack never gives up while its window is still the one it was asked for: the window
// flips (any way) → the pending pack is dropped. A newer request for the same thread
// replaces a pending one (a manual one starts at once, back-off or not).
//
// Bookkeeping (logs/present.jsonl): own_turn.js writes its started / result lines; this
// file adds one "pack" line per attempt — thread, how, outcome, windows, line counts —
// never a word of the summary or the lines.
// ============================================================

const fs = require("fs");
const path = require("path");
const win = require("./window.js");
const { compress_forced_shell } = require("./prompts.js");
const { OPEN, CLOSE } = require("./stream_filter.js");
const { local_stamp } = require("./clock.js");

const PACK_WAIT_MS = 90 * 1000;
const BACKOFF_BASE_MS = 2 * 60 * 1000;
const BACKOFF_MAX_MS = 60 * 60 * 1000;

/** The summary as it may go into a carry: no markers, trimmed. */
function clean_summary(text) {
  return String(text ?? "").split(OPEN).join("").split(CLOSE).join("").trim();
}

function line_stamp(at) {
  const s = String(at || "");
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s) ? `${s.slice(5, 10)} ${s.slice(11, 16)}` : "";
}

/** One stored line as the pack reads it. */
function format_line(line) {
  const who = line.role === "user" ? "ta" : line.woke ? "你（ta 不在的时候说的）" : "你";
  const bits = [String(line.text || "").trim()];
  if (Array.isArray(line.attach) && line.attach.length) bits.push(line.attach.map((k) => `[${k}]`).join(" "));
  if (Array.isArray(line.tools) && line.tools.length) bits.push(`（调了工具：${line.tools.join("、")}）`);
  const stamp = line_stamp(line.at);
  return `${stamp ? `[${stamp}] ` : ""}${who}：${bits.filter(Boolean).join(" ")}`;
}

/**
 * Which lines a pack of this thread reads, and where the next mark goes.
 * @returns { mark, ids: [line ids] } — ids empty when there is nothing to fold
 */
function plan_pack(thread, keep_raw) {
  const branch = Array.isArray(thread.branch) ? thread.branch : [];
  const w = thread.window || {};
  const mark = win.mark_for_tail(branch, keep_raw);
  const from = w.mark ? Math.max(0, branch.findIndex((e) => e.id === w.mark)) : 0;
  const to = mark ? branch.findIndex((e) => e.id === mark) : -1;
  if (!mark || to <= from) return { mark, ids: [] };
  return { mark, ids: branch.slice(from, to).map((e) => e.id) };
}

/** What the current carry holds from before its mark, to be folded into the new summary. */
function earlier_summary(w) {
  if (!w || !w.carry) return null;
  if (w.carry_parts && typeof w.carry_parts === "object") {
    const s = typeof w.carry_parts.summary === "string" ? w.carry_parts.summary.trim() : "";
    return s || null;
  }
  return String(w.carry);
}

/**
 * @param threads        threads.js instance
 * @param day_store      day_store.js instance (the lines' text)
 * @param own_turn       own_turn.js instance
 * @param prompts        prompts.js instance (the compress card in force)
 * @param read_compress  () → present.json compress section
 * @param system_of      (thread_id) → the client's system messages from its latest request, or []
 * @param on_flip        ({ thread, old_name, name, how }) → after a pack flipped a window
 * @param data_root      LOCI_GATEWAY_DATA (logs/present.jsonl)
 */
function create_packer({
  threads, day_store, own_turn, prompts, read_compress, system_of = () => [], on_flip = () => {},
  data_root, clock, zone, log = console.error,
}) {
  const log_file = path.join(data_root, "logs", "present.jsonl");
  const running = new Map();   // thread → { how, promise, window }
  const pending = new Map();   // thread → { how, window, next_at, paid }
  const record = { last_ok_at: null, last_ok_how: null, failures_since_ok: 0, last_failure: null };

  function stamp() { return zone ? local_stamp(clock.now(), zone).iso : new Date(clock.now()).toISOString(); }

  function write_line(entry) {
    try {
      fs.mkdirSync(path.dirname(log_file), { recursive: true });
      fs.appendFileSync(log_file, `${JSON.stringify({ at: stamp(), event: "pack", ...entry })}\n`, "utf8");
    } catch (err) { log(`[gateway] present: pack log not written: ${err?.message || err}`); }
  }

  /** A flip happened (any way): the newest successful compression, for /health. */
  function note_flip(how) {
    record.last_ok_at = clock.now();
    record.last_ok_how = how;
    record.failures_since_ok = 0;
    record.last_failure = null;
  }

  async function attempt(thread_id, how) {
    const thread = threads.get(thread_id);
    if (!thread) return { outcome: "gone" };
    // read only: a thread without a window object yet is planned as a fresh window
    const w = thread.window && typeof thread.window === "object" ? thread.window : win.fresh_window(thread.id, clock.now());
    const values = read_compress() || {};
    if (values.on !== true && how !== "manual") { write_line({ thread: thread_id, how, outcome: "off" }); return { outcome: "off" }; }
    const plan = plan_pack(thread, values.keep_raw);
    if (!plan.ids.length) {
      write_line({ thread: thread_id, how, outcome: "nothing", window: w.name });
      return { outcome: "nothing" };
    }
    const lines = plan.ids.map((id) => day_store.get(id)).filter(Boolean);
    const shell = compress_forced_shell({ card: prompts.current("compress"), keep_raw: values.keep_raw,
                                          originals: lines.map(format_line).join("\n"), earlier: earlier_summary(w) });
    const messages = [...system_of(thread_id), { role: "user", content: shell }];
    const res = await own_turn.run({ kind: "pack", messages, tools: "loci" });

    let outcome = res.outcome;
    let reason = res.reason;
    let summary = "";
    if (res.ok) {
      summary = clean_summary(res.text) || clean_summary(res.said);
      if (!summary) { outcome = "paid"; reason = "empty_summary"; }
    }
    const now_thread = threads.get(thread_id);
    if (outcome === "ok" && (!now_thread || !now_thread.window || now_thread.window.name !== w.name)) {
      write_line({ thread: thread_id, how, outcome: "window_moved", window: w.name, run: res.run });
      return { outcome: "window_moved" };
    }
    if (outcome !== "ok") {
      write_line({ thread: thread_id, how, outcome, reason, window: w.name, lines: plan.ids.length, run: res.run });
      return { outcome, reason };
    }
    const report = w.carry_parts && typeof w.carry_parts.report === "string" ? w.carry_parts.report : null;
    const flip = win.open_next(now_thread, { mark: plan.mark, parts: { report, summary }, how, at: stamp() }, clock.now());
    threads.save(now_thread.id);
    note_flip(how);
    write_line({ thread: thread_id, how, outcome: "ok", window: flip.old_name, next: flip.name, mark: plan.mark,
                 lines: plan.ids.length, run: res.run, rounds: res.rounds, tools_used: res.tools_used });
    console.log(`[gateway] present ${thread_id} window ${flip.old_name} → ${flip.name} (packed by the gateway: ${how}), mark ${plan.mark}`);
    try { on_flip({ thread: thread_id, old_name: flip.old_name, name: flip.name, how }); }
    catch (err) { log(`[gateway] present: after a pack: ${err?.message || err}`); }
    return { outcome: "ok" };
  }

  function start(thread_id, how) {
    const thread = threads.get(thread_id);
    const window_name = thread?.window?.name || null;
    const prior = pending.get(thread_id);
    pending.delete(thread_id);
    const job = { how, window: window_name, promise: null };
    job.promise = attempt(thread_id, how).catch((err) => {
      log(`[gateway] present: pack ${thread_id} failed: ${err?.stack || err}`);
      return { outcome: "paid", reason: "internal" };
    }).then((r) => {
      running.delete(thread_id);
      if (r.outcome === "paid") {
        const paid = (prior && prior.window === window_name ? prior.paid : 0) + 1;
        record.failures_since_ok += 1;
        record.last_failure = { at: clock.now(), reason: r.reason || null };
        const wait = Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** (paid - 1));
        pending.set(thread_id, { how, window: window_name, next_at: clock.now() + wait, paid });
      } else if (r.outcome === "unpaid" || r.outcome === "aborted" || r.outcome === "busy") {
        pending.set(thread_id, { how, window: window_name, next_at: 0, paid: prior?.paid || 0 });
      }
      return r;
    });
    running.set(thread_id, job);
    return job;
  }

  /**
   * Ask for a pack of this thread.
   * @returns { started: true } | { running: true } (one is in flight for it already)
   */
  function request(thread_id, how = "forced") {
    if (!thread_id) return { started: false };
    if (running.has(thread_id)) return { running: true };
    const prior = pending.get(thread_id);
    const same_window = prior && prior.window === (threads.get(thread_id)?.window?.name || null);
    if (same_window && how !== "manual" && prior.next_at > clock.now()) return { waiting: true };
    start(thread_id, how);
    return { started: true };
  }

  /** The heartbeat: start the pending packs that are due. Never awaits a pack. */
  function beat() {
    for (const [thread_id, p] of [...pending]) {
      if (running.has(thread_id)) continue;
      const t = threads.get(thread_id);
      if (!t || !t.window || t.window.name !== p.window) { pending.delete(thread_id); continue; }
      if (p.next_at > clock.now()) continue;
      start(thread_id, p.how);
    }
  }

  /**
   * Her turn arrived: if a pack is in flight for any of these threads, wait for it, at
   * most `ms`. Resolves "done" | "timeout" | null (nothing in flight).
   */
  async function wait_for(thread_ids, ms = PACK_WAIT_MS) {
    const job = thread_ids.map((id) => id && running.get(id)).find(Boolean);
    if (!job) return null;
    let timer = null;
    const late = new Promise((resolve) => { timer = setTimeout(() => resolve("timeout"), ms); });
    const got = await Promise.race([job.promise.then(() => "done"), late]);
    clearTimeout(timer);
    return got;
  }

  /** For /health: when a compression last succeeded and how many pack failures since. */
  function status() {
    return {
      last_ok_at: record.last_ok_at,
      last_ok_how: record.last_ok_how,
      failures_since_ok: record.failures_since_ok,
      last_failure: record.last_failure,
      running: [...running.entries()].map(([thread, j]) => ({ thread, how: j.how })),
      pending: [...pending.entries()].map(([thread, p]) => ({ thread, how: p.how, next_at: p.next_at, paid: p.paid })),
    };
  }

  return { request, beat, wait_for, note_flip, status, is_running: (id) => running.has(id) };
}

module.exports = { create_packer, plan_pack, format_line, clean_summary, earlier_summary, PACK_WAIT_MS, BACKOFF_BASE_MS, BACKOFF_MAX_MS };
