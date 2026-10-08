// ============================================================
// gateway/present/day_close.js — the night: hand the lines to Loci, write the day report,
// open the next window
//
// The heartbeat calls tick() once a minute. A tick costs nothing unless there is work;
// work runs as one background job at a time (the beat never waits on it), so a report
// that takes minutes does not hold up the other tasks on the beat.
//
// ─── The report window and its day ───
// report.from–report.to (present.json), local time in LOCI_TZ, start inside, end not,
// wrapping midnight like every span here (dnd.js). One opening of the window is an
// "occurrence"; the day it closes out (its report day) is the local day twelve hours
// before it opens — 04:00 closes out yesterday, an evening window closes out its own day.
// At any moment the latest occurrence is the one whose opening is most recent.
//
// ─── ① The nightly hand-off (every night, whatever the cadence) ───
// Inside the window, once she has been quiet for report.quiet_min (her latest request or
// the latest activity on any thread), the lines nobody has handed over yet go to Loci —
// and before every report turn (a make-up, or one she asked for) as well:
//     POST /api/v2/slices {source, day, lines: [{id, text, at, speaker, revision}], report_at}
// one batch per conversation (the thread a line was born on) per day file, at most
// BATCH_LINES lines each. source = {system: "gateway", instance: LOCI_GATEWAY_NAME,
// container: "thread:<thread id>"}. Each line is exactly what the fourth joint answers for
// it (source_api.js line_view: the current text with attachment marks, the revision its
// text was set at, when it was first said), so the fingerprint Loci keeps matches the
// text it later asks for. Replaced lines (a regenerated reply) and lines with no text (a
// bare tool call) are not handed over. report_at is the last report's time, or null.
// `speaker` is a name, not a role word: Loci shows it as is to the side model that slices
// and as `who` in an original, so it says who spoke. The owner's lines carry
// LOCI_OWNER_NAME, his carry LOCI_AI_NAME (else AI_NAME, Loci's own variable); unset, the
// owner is 「用户」 and he is 「AI」, as Loci's own import names them (source_api.js
// speaker_names: /loci/source names them the same way).
// What happens to a batch:
//   · 2xx → handed over; its lines are never sent again
//   · Loci unreachable, a timeout, 401, a 5xx other than 502 → tried again on a later beat
//     (the rest of this beat's batches wait too: Loci is down for all of them)
//   · 502 (Loci's side model failed) → at most HANDOFF_TRIES tries, then given up
//   · 400 `source_changed_while_slicing` → the lines it names that were withdrawn, deleted
//     or held are dropped, the revised ones re-read from the day store, and the batch
//     sent again at once (STALE_RESENDS times; past that it counts as a try)
//   · any other 4xx → the reason is recorded and that batch is not sent again
// A batch given up stays in the books (handoff.failed) with its reason.
// New ids are only ever born into today's file, so once every line up to a moment has
// been settled the scan starts from that day (handoff.floor) and earlier books are let go.
//
// ─── ①b A handed-over line that changed (§七.10) ───
// Her client showing one of his replies edited makes a new revision of that line
// (threads.js ingest); when its words changed, note_revised() puts it in the books
// (changes.queue), at once and on disk, so neither Loci being down nor a restart loses
// it. Every job starts by working the queue off, before its hand-off, and a beat with no
// other job starts one for a queue that holds anything:
//   · a line not handed over yet is dropped from the queue: the hand-off sends its
//     current text, and Loci never had the old one. (Jobs run one at a time, so no
//     hand-off is in flight then; a line edited while its batch was in flight is in the
//     books as handed over by the next job, and its change goes then.)
//   · a handed-over line goes as
//         POST /api/v2/source/change {change_id, source: {system, instance, container,
//              id}, host_seq, change: "revised", revision}
//     with the source exactly as its slices named it (the thread it was born on), the
//     revision as the fourth joint gives it ("r<n>"), host_seq = n (the line's own
//     revision count only grows, so it is one order per source with nothing to keep),
//     and change_id = revised-<id>-r<n>, so a resend of the same change is Loci's
//     duplicate. Only the newest revision of a line is kept in the queue.
//   · applied, duplicate, unknown_source, stale (a newer change is there already) → done;
//     Loci unreachable, a timeout, 401, a 5xx, in_progress → the next beat, same
//     change_id; any other answer (forbidden, conflict, a 4xx) → given up, kept with its
//     reason in changes.failed.
// Only an edit is sent. A regenerate marks the old reply `replaced` without changing its
// words or revision, and the wire has no kind for "superseded" (source_api.js); a delete
// or a rewind is never inferred (the client may have trimmed its own history).
//
// ─── ② The report turn (on the cadence's days, §七.6) ───
//   daily     every occurrence writes a report, then flips
//   every_n   an occurrence writes a report when report.every_n days have passed since the
//             last flip (any way: a report it wrote, or one she asked for), then flips
//   manual    nothing scheduled; she asks (POST /present/report {kind: "now"}), it writes
//             and flips, and her next turn opens the new window
// The lines the report covers are those born after the last report's cut up to this
// job's cut. A cut is the last millisecond before the second the job started in: a line's
// `at` is kept to the second, so a line said in that second (before or after the job
// started) belongs wholly to the next report, never to neither. An occurrence whose
// report day had no lines in that stretch before the window opened is `quiet`: nothing
// to write, no flip.
// The turn is one own turn (own_turn.js, kind "report"), a fresh window with no cache:
// the latest conversation's system messages (memory only, so he writes as himself) and
// report_shell (prompts.js) with the report card in force, the pending slices of this
// gateway's sources (GET /api/v2/slices, each with its gist, the lines it spans as they
// are numbered in the letter, and its guesses as 「好像记过」), or the 「没交上」 line when
// tonight's hand-off did not go through, and the stretch's raw lines, numbered. Loci's
// tools are offered (§七.7, at most own.tool_rounds rounds). The report is his last
// answer's words (all the run's words when it has none); an empty one is a failure.
// Those words never hold a 【窗口摘要】 block: own_turn.js takes it out of every answer,
// and the report ignores it — the report file is what the panel shows and the export
// carries, and a block is not for either.
// Upstream refusing the letter as too long drops the oldest lines from it until it fits
// (pack.js fit_newest), as a pack does; they stay in the day store and in Loci.
// Her arrival does not abort a report (own_turn.js aborts only wake on arrival).
//
// Written → <host dir>/reports/<day>.md (<day>.2.md, … when a report of that day exists
// already, e.g. one she asked for that afternoon), the day's .err.json removed. Failed →
// <day>.err.json with the whole reason (own_turn's error, redacted); the .err.json is not
// for export. The same occurrence gives up after REPORT_TRIES counted failures (a
// deterministic refusal tried ten thousand times is the same refusal); not counted: the
// own-turn runner busy with something else (tried again next beat) and no key yet (after
// a restart, until she has spoken). A scheduled report the runner cannot send at all
// (own_turn blocked(): no key, no model) is not started: the beat runs only the hand-off,
// the reason is set on the occurrence for the panel and written to the log once, and the
// report starts on the first quiet beat after it clears. An occurrence missed inside its
// window — the computer was off, she never went quiet — gets its make-up after
// report.to, at her first quiet moment, for that occurrence only: the latest occurrence
// is never more than a day old, so only yesterday is ever made up.
// POST /present/report {kind: "missing"} (「补一份」) writes it now, quiet or not, even
// after it gave up — once per press.
//
// ─── ③ The flip ───
// Every thread with a window: window.js open_next with the new report as the carry's
// report part and no summary (the report covers what a summary did), the mark on the last
// compress.keep_raw lines (mark_for_tail), how "day"; on_flip tells Loci (/cue/dropped)
// and rebuilds the wake snapshot (index.js).
//
// report_ready() is wake's gate: false while the latest occurrence's report is due and
// neither written, nor given up, nor quiet — "the morning's first call waits for the
// report".
//
// Books: <LOCI_GATEWAY_DATA>/counters.json under `report` (the hand-off books, the changes
// owed to Loci, the last report, the occurrence's attempts), logs/present.jsonl (counts,
// ids, reasons — never a line's text or the report). The report text itself is in
// reports/, which the panel may show (/present status.report.text); nothing here touches
// the carry beyond setting it.
// ============================================================

const fs = require("fs");
const path = require("path");
const win = require("./window.js");
const { local_stamp } = require("./clock.js");
const { in_span, local_minute, minute_of_day } = require("./dnd.js");
const { report_shell } = require("./prompts.js");
const { line_view, speaker_names, DEFAULT_OWNER, DEFAULT_AI } = require("./source_api.js");
const { format_line, clean_summary, fit_newest, wall_of } = require("./pack.js");
const { day_of_id } = require("./day_store.js");

const BATCH_LINES = 1000;
const HANDOFF_TRIES = 3;
const STALE_RESENDS = 2;
const REPORT_TRIES = 3;
const WALL_RETRIES = 3;
const FAILED_KEPT = 20;
const SLICES_POST_TIMEOUT_MS = 5 * 60 * 1000;   // the side model slices while Loci holds the request
const SLICES_GET_TIMEOUT_MS = 15 * 1000;
const CHANGE_TIMEOUT_MS = 30 * 1000;   // Loci holds a second send of one change up to ~10 s
const CHANGE_DONE = ["applied", "duplicate", "unknown_source", "stale"];
const DAY_MS = 24 * 60 * 60 * 1000;

/** A report job's cut: the last millisecond before the second it started in. */
const cut_of = (started_ms) => Math.floor(started_ms / 1000) * 1000 - 1;

// What the model reads in the letter (Chinese, like every model-facing text).
const NO_PENDING = "（现在没有在排队的段落）";
const LIST_FAILED = "这次没拿到在排队的段落（问 Loci 没回），先只写日报";
const OTHER_THREAD = "——另一段对话——";

const STATE_WORDS = { written: "写了", missing: "还没写", failed: "没写成", quiet: "没话，不写", not_due: "还没到时候" };

const is_plain = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

function read_json_file(file) {
  let text;
  try { text = fs.readFileSync(file, "utf8"); }
  catch (err) { return err.code === "ENOENT" ? { ok: true, value: null } : { ok: false, error: err.message }; }
  try { return { ok: true, value: JSON.parse(text) }; }
  catch (err) { return { ok: false, error: `not JSON (${err.message})` }; }
}

function write_file_atomic(file, text) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, text, "utf8");
  fs.renameSync(tmp, file);
}

function blank_state() {
  return {
    handoff: { floor: null, sent: {}, tries: {}, failed: [], last_ok_at: null, last_error: null, failures_since_ok: 0 },
    // revisions of handed-over lines Loci is still owed: { key, id, day, thread, host_seq, revision, noted_at }
    changes: { queue: [], failed: [], last_ok_at: null, last_error: null, failures_since_ok: 0 },
    last_report: null,       // { at, at_ms, through_ms, day, file, kind }
    last_flip_day: null,
    auto: null,              // the latest occurrence's attempts: { occ, day, failures, gave_up, error, quiet, written_at, file }
    last_attempt: null,      // { kind, day, at_ms, ok, error }
    failures_since_ok: 0,
  };
}

/** Days between two YYYY-MM-DD dates (b − a). */
function days_between(a, b) {
  return Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / DAY_MS);
}
function add_days(day, n) {
  return new Date(Date.parse(`${day}T00:00:00Z`) + n * DAY_MS).toISOString().slice(0, 10);
}

function short_stamp(iso) {
  const s = String(iso || "");
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s) ? `${s.slice(5, 10)} ${s.slice(11, 16)}` : s;
}

/**
 * @param data_root     LOCI_GATEWAY_DATA (counters.json, logs/present.jsonl)
 * @param host_dir      the gateway's host folder; reports go to <host_dir>/reports/
 * @param name          LOCI_GATEWAY_NAME: the `instance` of this gateway's sources
 * @param env           LOCI_HOOK_TOKEN, LOCI_OWNER_NAME, LOCI_AI_NAME / AI_NAME
 * @param loci_base     Loci's REST root (cue.js http_base of LOCI_MCP)
 * @param threads · day_store · settings · prompts · own_turn   the present modules
 * @param system_of     (thread_id) → the client's system messages from its latest request, or []
 * @param on_flip       ({ thread, old_name, name, how }) → after each thread's window flipped
 */
function create_day_close({
  data_root, host_dir, name, env = process.env, loci_base, threads, day_store, settings, prompts, own_turn,
  clock, zone, log = console.error, system_of = () => [], on_flip = () => {},
  post_timeout_ms = SLICES_POST_TIMEOUT_MS, get_timeout_ms = SLICES_GET_TIMEOUT_MS,
  change_timeout_ms = CHANGE_TIMEOUT_MS,
}) {
  const counters_file = path.join(data_root, "counters.json");
  const log_file = path.join(data_root, "logs", "present.jsonl");
  const reports_dir = path.join(host_dir, "reports");
  const hook_token = String(env.LOCI_HOOK_TOKEN || "").trim();
  const speakers = speaker_names(env);
  const speaker_of = speakers.of;

  let owner_seen_at = 0;
  let running = null;      // { kind, promise }
  let queued = null;       // an asked-for job waiting for the own-turn runner
  let last_logged = null;  // the last "nothing to do / waiting" reason written down
  let last_blocked = null; // the reason a due report cannot be sent, as last written down
  let last_change_error = null;   // the last "Loci did not take a change, later" reason written down

  const iso = (ms) => local_stamp(ms, zone).iso;
  const day_of = (ms) => local_stamp(ms, zone).day;

  function write_log(entry) {
    try {
      fs.mkdirSync(path.dirname(log_file), { recursive: true });
      fs.appendFileSync(log_file, `${JSON.stringify({ at: iso(clock.now()), ...entry })}\n`, "utf8");
    } catch (err) { log(`[gateway] present: day-close log not written: ${err?.message || err}`); }
  }
  function note_once(reason, entry) {
    if (reason === last_logged) return;
    last_logged = reason;
    write_log({ event: "day_close", ...entry });
  }

  // ———— The books ————

  function load_state() {
    const got = read_json_file(counters_file);
    if (!got.ok) return { ok: false, error: got.error };
    if (got.value !== null && !is_plain(got.value)) return { ok: false, error: "counters.json does not hold a JSON object" };
    const all = got.value || {};
    const kept = is_plain(all.report) ? all.report : {};
    const st = { ...blank_state(), ...kept };
    st.handoff = { ...blank_state().handoff, ...(is_plain(kept.handoff) ? kept.handoff : {}) };
    if (!is_plain(st.handoff.sent)) st.handoff.sent = {};
    if (!is_plain(st.handoff.tries)) st.handoff.tries = {};
    if (!Array.isArray(st.handoff.failed)) st.handoff.failed = [];
    st.changes = { ...blank_state().changes, ...(is_plain(kept.changes) ? kept.changes : {}) };
    if (!Array.isArray(st.changes.queue)) st.changes.queue = [];
    if (!Array.isArray(st.changes.failed)) st.changes.failed = [];
    return { ok: true, all, st };
  }

  /** Read, change, write back — the other sections of counters.json are left as they are. */
  function update_state(change) {
    const got = load_state();
    if (!got.ok) throw new Error(`counters.json cannot be read: ${got.error}`);
    change(got.st);
    write_file_atomic(counters_file, `${JSON.stringify({ ...got.all, report: got.st }, null, 1)}\n`);
    return got.st;
  }

  function read_state() {
    const got = load_state();
    return got.ok ? got.st : blank_state();
  }

  // ———— Time ————

  /** The latest opening of the report window at or before `now`, and whether `now` is inside it. */
  function occurrence(now, values) {
    const f = minute_of_day(values.from);
    const t = minute_of_day(values.to);
    const m = local_minute(now, zone);
    const since = (m - f + 1440) % 1440;
    const start = Math.floor(now / 60000) * 60000 - since * 60000;
    return { start, day: day_of(start - DAY_MS / 2), in_window: in_span(m, f, t) };
  }

  function last_owner_at() {
    let at = owner_seen_at;
    for (const t of threads.list()) if ((t.last_at || 0) > at) at = t.last_at;
    return at;
  }

  function cadence_due(occ, st, values) {
    if (values.flip === "daily") return true;
    if (values.flip === "every_n") return !st.last_flip_day || days_between(st.last_flip_day, occ.day) >= values.every_n;
    return false;
  }

  function auto_of(st, occ) {
    return st.auto && st.auto.occ === occ.start ? st.auto : null;
  }

  // ———— The lines ————

  function day_files() {
    try {
      return fs.readdirSync(day_store.dir).filter((n) => /^\d{4}-\d{2}-\d{2}\.jsonl$/.test(n))
        .map((n) => n.slice(0, 10)).sort();
    } catch { return []; }
  }

  /**
   * The current version of each line born in (after_ms, upto_ms], in the host's order,
   * as the fourth joint gives it. Replaced lines are left out.
   * @param from_day  only day files from this one on (null: every file)
   */
  function lines_between({ after_ms = null, upto_ms, from_day = null }) {
    const first = after_ms === null ? from_day : [from_day, day_of(after_ms)].filter(Boolean).sort().pop();
    const last = day_of(upto_ms);
    const out = [];
    for (const day of day_files()) {
      if ((first && day < first) || day > last) continue;
      const by_id = new Map();
      for (const l of day_store.read_day(day)) {
        if (!by_id.has(l.id)) by_id.set(l.id, []);
        by_id.get(l.id).push(l);
      }
      for (const [id, revisions] of by_id) {
        const latest = revisions.reduce((a, b) => (b.rev >= a.rev ? b : a));
        if (latest.state === "replaced") continue;
        const { line } = line_view(revisions);
        const born = Date.parse(line.at);
        if (!Number.isFinite(born) || born > upto_ms || (after_ms !== null && born <= after_ms)) continue;
        out.push({ id, day, thread: latest.thread, role: latest.role, text: line.text, revision: line.revision,
                   at: line.at, born, stored: latest });
      }
    }
    return out;
  }

  // ———— ① The hand-off ————

  async function loci(method, route, body, timeout_ms) {
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (hook_token) headers["x-loci-hook-token"] = hook_token;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout_ms);
    try {
      const resp = await fetch(`${loci_base}${route}`, {
        method, headers, signal: controller.signal, body: body === undefined ? undefined : JSON.stringify(body),
      });
      const text = await resp.text();
      let json = null;
      try { json = JSON.parse(text); } catch { json = null; }
      return { reached: true, status: resp.status, json, text };
    } catch (err) {
      const why = err?.name === "AbortError" ? `no answer within ${Math.round(timeout_ms / 1000)} s`
        : `${err?.cause?.code || err?.message || err}`;
      return { reached: false, error: why };
    } finally { clearTimeout(timer); }
  }

  function unsent_lines(st, upto_ms) {
    const sent = st.handoff.sent;
    return lines_between({ upto_ms, from_day: st.handoff.floor })
      .filter((l) => String(l.text).trim() && !(Array.isArray(sent[l.day]) && sent[l.day].includes(l.id)));
  }

  function batches_of(lines) {
    const groups = new Map();
    for (const l of lines) {
      const key = `${l.thread}|${l.day}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(l);
    }
    const out = [];
    for (const list of groups.values()) {
      for (let i = 0; i < list.length; i += BATCH_LINES) {
        const part = list.slice(i, i + BATCH_LINES);
        out.push({ key: `${part[0].thread}|${part[0].day}|${part[0].id}`, thread: part[0].thread, day: part[0].day, lines: part });
      }
    }
    return out;
  }

  function wire_line(l) {
    return { id: l.id, text: l.text, at: l.at, speaker: speaker_of(l.role), revision: l.revision };
  }

  /** The line as it stands now in the day store, or null when it is gone. */
  function reread(l) {
    const revisions = day_store.read_day(l.day).filter((x) => x.id === l.id);
    if (!revisions.length) return null;
    const { line } = line_view(revisions);
    return { ...l, text: line.text, revision: line.revision };
  }

  /** One batch → { kind: "ok" | "later" | "502" | "stale" | "refused", status?, error?, slices? } */
  async function send_batch(batch, report_at) {
    let lines = batch.lines.slice();
    for (let resend = 0; ; resend++) {
      const body = {
        source: { system: "gateway", instance: name, container: `thread:${batch.thread}` },
        day: batch.day, lines: lines.map(wire_line), report_at,
      };
      const r = await loci("POST", "/api/v2/slices", body, post_timeout_ms);
      if (!r.reached) return { kind: "later", error: `Loci unreachable: ${r.error}` };
      const why = String(r.json?.error || r.text || "").slice(0, 2000);
      if (r.status >= 200 && r.status < 300) {
        return { kind: "ok", sent: lines.map((l) => l.id), slices: Array.isArray(r.json?.slices) ? r.json.slices.length : null };
      }
      if (r.status === 502) return { kind: "502", status: 502, error: why };
      if (r.status === 400 && r.json?.note === "source_changed_while_slicing") {
        if (resend >= STALE_RESENDS) return { kind: "stale", status: 400, error: why };
        const changed = is_plain(r.json.lines) ? r.json.lines : {};
        lines = lines.filter((l) => !(l.id in changed) || changed[l.id] === "revised")
          .map((l) => (l.id in changed ? reread(l) : l)).filter((l) => l && String(l.text).trim());
        if (!lines.length) return { kind: "ok", sent: batch.lines.map((l) => l.id), slices: 0 };
        continue;
      }
      if (r.status === 401 || r.status >= 500) return { kind: "later", status: r.status, error: why };
      return { kind: "refused", status: r.status, error: why };
    }
  }

  /**
   * Hand every line not yet handed over (born up to `upto_ms`) to Loci.
   * @returns { delivered: no batch failed this time, batches, ok, refused, later, gave_up }
   */
  async function hand_off(upto_ms) {
    const st = read_state();
    const report_at = st.last_report?.at || null;
    const batches = batches_of(unsent_lines(st, upto_ms));
    const done = { delivered: true, batches: batches.length, ok: 0, refused: 0, later: 0, gave_up: 0, slices: 0 };
    if (!batches.length) return done;
    const settled = [];      // { batch, outcome }
    const kept = [];         // { batch, outcome } still to retry
    let down = false;
    for (const batch of batches) {
      if (down) { kept.push({ batch, outcome: { kind: "later", error: "Loci unreachable" } }); continue; }
      const outcome = await send_batch(batch, report_at);
      if (outcome.kind === "later" && !outcome.status) down = true;
      if (outcome.kind === "ok" || outcome.kind === "refused") settled.push({ batch, outcome });
      else kept.push({ batch, outcome });
    }
    const now = clock.now();
    update_state((s) => {
      const h = s.handoff;
      const sent_ids = (batch, ids) => {
        const list = Array.isArray(h.sent[batch.day]) ? h.sent[batch.day] : [];
        h.sent[batch.day] = [...new Set([...list, ...ids])];
      };
      const fail = (batch, outcome, given_up) => {
        h.failed = [...h.failed, { key: batch.key, thread: batch.thread, day: batch.day, lines: batch.lines.length,
                                   status: outcome.status ?? null, error: outcome.error || null, at: iso(now), given_up }]
          .slice(-FAILED_KEPT);
      };
      for (const { batch, outcome } of settled) {
        delete h.tries[batch.key];
        if (outcome.kind === "ok") { sent_ids(batch, outcome.sent); done.ok += 1; done.slices += outcome.slices || 0; }
        else { sent_ids(batch, batch.lines.map((l) => l.id)); fail(batch, outcome, true); done.refused += 1; }
      }
      for (const { batch, outcome } of kept) {
        if (outcome.kind === "502" || outcome.kind === "stale") {
          h.tries[batch.key] = (h.tries[batch.key] || 0) + 1;
          if (h.tries[batch.key] >= HANDOFF_TRIES) {
            delete h.tries[batch.key];
            sent_ids(batch, batch.lines.map((l) => l.id));
            fail(batch, outcome, true);
            done.gave_up += 1;
            continue;
          }
        }
        done.later += 1;
      }
      if (done.later === 0) {
        h.floor = day_of(upto_ms);
        for (const day of Object.keys(h.sent)) if (day < h.floor) delete h.sent[day];
      }
      const failed_now = done.refused + done.gave_up + done.later;
      if (failed_now === 0) { h.last_ok_at = now; h.last_error = null; h.failures_since_ok = 0; }
      else {
        h.failures_since_ok += 1;
        const first = [...settled, ...kept].find((x) => x.outcome.kind !== "ok");
        h.last_error = first ? `${first.outcome.status ? `HTTP ${first.outcome.status}: ` : ""}${first.outcome.error || first.outcome.kind}` : null;
      }
    });
    done.delivered = done.refused + done.gave_up + done.later === 0;
    write_log({ event: "handoff", batches: done.batches, ok: done.ok, refused: done.refused, later: done.later,
                gave_up: done.gave_up, slices: done.slices, report_at,
                reasons: kept.concat(settled).filter((x) => x.outcome.kind !== "ok")
                  .map((x) => ({ key: x.batch.key, kind: x.outcome.kind, status: x.outcome.status ?? null,
                                 error: String(x.outcome.error || "").slice(0, 300) })) });
    return done;
  }

  // ———— ①b Changes to handed-over lines ————

  /**
   * Lines threads.js has just revised (ids). Those whose words changed are owed to Loci
   * if they were handed over; whether they were is decided when the queue is worked off.
   * @returns how many went into the queue
   */
  function note_revised(ids) {
    const items = [];
    for (const id of ids || []) {
      const day = day_of_id(id);
      if (!day) continue;
      const revisions = day_store.read_day(day).filter((x) => x.id === id);
      if (!revisions.length) continue;
      const latest = revisions.reduce((x, y) => (y.rev >= x.rev ? y : x));
      const { line } = line_view(revisions);
      if (line.revision !== `r${latest.rev}`) continue;   // the words stayed (a tool name changed)
      items.push({ key: `revised-${id}-r${latest.rev}`, id, day, thread: latest.thread,
                   host_seq: latest.rev, revision: line.revision, noted_at: clock.now() });
    }
    if (!items.length) return 0;
    const fresh = new Set(items.map((c) => c.id));
    update_state((s) => {
      s.changes.queue = [...s.changes.queue.filter((c) => !fresh.has(c.id)), ...items];
    });
    return items.length;
  }

  function change_owed(st) { return st.changes.queue.length > 0; }

  /** One change → { kind: "ok" | "later" | "refused", down?, status?, outcome?, note?, error? } */
  async function send_change(c) {
    const body = {
      change_id: c.key,
      source: { system: "gateway", instance: name, container: `thread:${c.thread}`, id: c.id },
      host_seq: c.host_seq, change: "revised", revision: c.revision,
    };
    const r = await loci("POST", "/api/v2/source/change", body, change_timeout_ms);
    if (!r.reached) return { kind: "later", down: true, error: `Loci unreachable: ${r.error}` };
    const why = String(r.json?.error || r.json?.note || r.text || "").slice(0, 300);
    if (r.status >= 200 && r.status < 300) {
      const outcome = String(r.json?.status || "");
      if (CHANGE_DONE.includes(outcome)) return { kind: "ok", outcome };
      if (outcome === "in_progress") return { kind: "later", outcome, error: "in_progress" };
      return { kind: "refused", status: r.status, outcome: outcome || null, note: r.json?.note || null, error: why };
    }
    if (r.status === 401 || r.status >= 500) return { kind: "later", status: r.status, error: why };
    return { kind: "refused", status: r.status, outcome: null, note: r.json?.note || null, error: why };
  }

  /** Work the queue of changes off (see ①b). Runs inside a job, before its hand-off. */
  async function send_changes() {
    const st = read_state();
    if (!change_owed(st)) return null;
    const h = st.handoff;
    const handed = (c) => Boolean((h.floor && c.day < h.floor)
      || (Array.isArray(h.sent[c.day]) && h.sent[c.day].includes(c.id)));
    const settled = [];   // { c, outcome }
    let later = null;     // the first outcome that waits for a later beat
    let waiting = 0;
    for (const c of st.changes.queue) {
      if (!handed(c)) { settled.push({ c, outcome: { kind: "not_handed" } }); continue; }
      if (later && later.down) { waiting += 1; continue; }
      const outcome = await send_change(c);
      if (outcome.kind === "later") { later = later || outcome; waiting += 1; continue; }
      settled.push({ c, outcome });
    }
    const now = clock.now();
    const count = (kind) => settled.filter((x) => x.outcome.kind === kind).length;
    update_state((s) => {
      const ch = s.changes;
      // by key: a newer revision noted while this ran stays queued
      const gone = new Set(settled.map((x) => x.c.key));
      ch.queue = ch.queue.filter((c) => !gone.has(c.key));
      for (const { c, outcome } of settled) {
        if (outcome.kind !== "refused") continue;
        ch.failed = [...ch.failed, { key: c.key, id: c.id, day: c.day, status: outcome.status ?? null,
                                     outcome: outcome.outcome, note: outcome.note, error: outcome.error || null, at: iso(now) }]
          .slice(-FAILED_KEPT);
      }
      if (count("ok") && !count("refused") && !later) { ch.last_ok_at = now; ch.last_error = null; ch.failures_since_ok = 0; }
      if (count("refused") || later) {
        ch.failures_since_ok += 1;
        const first = settled.find((x) => x.outcome.kind === "refused")?.outcome || later;
        ch.last_error = `${first.status ? `HTTP ${first.status}: ` : ""}${first.outcome && first.outcome !== first.error ? `${first.outcome} ` : ""}${first.error || ""}`.trim();
      }
    });
    const result = { ok: count("ok"), refused: count("refused"), not_handed: count("not_handed"), later: waiting };
    const later_reason = later ? String(later.error || later.status || "later").slice(0, 300) : null;
    // a change Loci keeps not taking is written down when the reason changes, not every beat
    if (settled.length || later_reason !== last_change_error) {
      write_log({ event: "source_change", ...result, later_reason,
                  refused_ids: settled.filter((x) => x.outcome.kind === "refused")
                    .map((x) => ({ id: x.c.id, status: x.outcome.status ?? null, outcome: x.outcome.outcome, note: x.outcome.note })) });
    }
    last_change_error = later_reason;
    return result;
  }

  // ———— ② The report ————

  /** The pending slices of this gateway's sources, as the letter lists them; null when Loci did not answer. */
  async function pending_slices(numbers) {
    const r = await loci("GET", "/api/v2/slices", undefined, get_timeout_ms);
    if (!r.reached || r.status < 200 || r.status >= 300 || !is_plain(r.json)) return null;
    const batches = (Array.isArray(r.json.batches) ? r.json.batches : [])
      .filter((b) => b?.source?.system === "gateway" && b?.source?.instance === name);
    const rows = [];
    for (const b of batches.slice().reverse()) {
      for (const s of Array.isArray(b.slices) ? b.slices : []) {
        const first = s?.span?.first;
        const last = s?.span?.last || first;
        const a = numbers.get(first);
        const z = numbers.get(last);
        const where = a && z ? `第 ${a}–${z} 行` : `${first} 到 ${last}（不在下面的原话里）`;
        const guesses = (Array.isArray(s.guesses) ? s.guesses : []).map((g) => g.short || g.id).filter(Boolean);
        rows.push(`· ${s.slice_id}　${where}：${String(s.gist || "").trim()}${guesses.length ? `　好像记过：${guesses.join("、")}` : ""}`);
      }
    }
    return rows.length ? rows.join("\n") : NO_PENDING;
  }

  /** The stretch's lines, grouped by conversation, numbered from 1. */
  function number_lines(lines) {
    const order = [];
    const groups = new Map();
    for (const l of lines.slice().sort((x, y) => x.born - y.born)) {
      if (!groups.has(l.thread)) { groups.set(l.thread, []); order.push(l.thread); }
      groups.get(l.thread).push(l);
    }
    const numbered = [];
    for (const t of order) for (const l of groups.get(t)) numbered.push(l);
    const numbers = new Map(numbered.map((l, i) => [l.id, i + 1]));
    const text_of = (from) => {
      const out = [];
      let thread = null;
      for (let i = from; i < numbered.length; i++) {
        const l = numbered[i];
        if (order.length > 1 && thread !== null && l.thread !== thread) out.push(OTHER_THREAD);
        thread = l.thread;
        out.push(`(${i + 1}) ${format_line({ ...l.stored, at: l.at })}`);
      }
      return out;
    };
    return { numbered, numbers, text_of };
  }

  function report_file(day) {
    for (let n = 1; ; n++) {
      const file = path.join(reports_dir, n === 1 ? `${day}.md` : `${day}.${n}.md`);
      if (!fs.existsSync(file)) return file;
    }
  }
  const err_file = (day) => path.join(reports_dir, `${day}.err.json`);

  function latest_system() {
    let best = null;
    for (const t of threads.list()) if (!best || (t.last_at || 0) > (best.last_at || 0)) best = t;
    return best ? system_of(best.id) : [];
  }

  /**
   * One report attempt.
   * @param job  { kind: "auto" | "makeup" | "missing" | "now", day, occ (null for now), started }
   * @returns { outcome: "written" | "quiet" | "failed" | "busy", ... }
   */
  async function write_report(job, handed) {
    const st = read_state();
    const cut = cut_of(job.started);
    const lines = lines_between({ after_ms: st.last_report ? st.last_report.through_ms : null, upto_ms: cut });
    if (!lines.length) {
      update_state((s) => {
        if (job.occ !== null) s.auto = { ...(auto_of(s, { start: job.occ }) || fresh_auto(job)), quiet: true };
        s.last_attempt = { kind: job.kind, day: job.day, at_ms: clock.now(), ok: true, quiet: true, error: null };
      });
      write_log({ event: "report", kind: job.kind, day: job.day, outcome: "quiet" });
      return { outcome: "quiet" };
    }

    const { numbered, numbers, text_of } = number_lines(lines);
    let slices = null;
    if (handed && handed.delivered) slices = (await pending_slices(numbers)) ?? LIST_FAILED;
    const card = prompts.current("report");
    const from = short_stamp(numbered[0].at);
    const to = short_stamp(iso(job.started));
    const system = latest_system();
    const letter = (skip) => report_shell({ card, from, to, slices, originals: text_of(skip).join("\n") });

    let skip = 0;
    let res = null;
    let walls = 0;
    for (;;) {
      const shell = letter(skip);
      res = await own_turn.run({ kind: "report", messages: [...system, { role: "user", content: shell }], tools: "loci" });
      const wall = wall_of(res);
      if (!wall || walls >= WALL_RETRIES) break;
      const sizes = text_of(skip).map((t) => t.length + 1);
      const keep = fit_newest({ total: shell.length, sizes, wall });
      if (keep < 1 || keep >= sizes.length) break;
      walls += 1;
      write_log({ event: "report_wall", kind: job.kind, day: job.day, lines_before: sizes.length, lines_after: keep,
                  limit: wall.limit, actual: wall.actual, run: res.run });
      console.log(`[gateway] present: the day report for ${job.day} hit the wall; the oldest ${sizes.length - keep} lines are left out of the letter`);
      skip += sizes.length - keep;
    }

    if (res.outcome === "busy") return { outcome: "busy" };
    const text = res.ok ? (clean_summary(res.text) || clean_summary(res.said)) : "";
    const finished = clock.now();
    if (res.ok && text && text !== "【无话】") {
      const file = report_file(job.day);
      write_file_atomic(file, `${text}\n`);
      try { fs.rmSync(err_file(job.day), { force: true }); } catch { /* a stale error card is not worth failing over */ }
      update_state((s) => {
        s.last_report = { at: iso(finished), at_ms: finished, through_ms: cut, day: job.day,
                          file: path.basename(file), kind: job.kind };
        s.last_flip_day = job.day;
        s.failures_since_ok = 0;
        if (job.occ !== null) s.auto = { ...(auto_of(s, { start: job.occ }) || fresh_auto(job)), written_at: finished, file: path.basename(file) };
        s.last_attempt = { kind: job.kind, day: job.day, at_ms: finished, ok: true, error: null };
      });
      write_log({ event: "report", kind: job.kind, day: job.day, outcome: "written", file: path.basename(file),
                  lines: numbered.length, left_out: skip, slices: slices === null ? "missing" : slices === NO_PENDING ? 0 : "listed",
                  run: res.run, rounds: res.rounds, tools_used: res.tools_used });
      console.log(`[gateway] present: day report ${path.basename(file)} written (${numbered.length} lines${skip ? `, ${skip} left out at the wall` : ""})`);
      flip_all(text, iso(finished));
      return { outcome: "written", file };
    }

    const reason = res.ok ? "empty_report" : res.reason;
    const counted = !["no_key", "busy"].includes(reason);
    let after = null;
    update_state((s) => {
      if (counted) s.failures_since_ok += 1;
      if (job.occ !== null) {
        const a = { ...(auto_of(s, { start: job.occ }) || fresh_auto(job)) };
        if (counted) a.failures += 1;
        a.error = reason;
        if (a.failures >= REPORT_TRIES) a.gave_up = true;
        s.auto = a;
        after = a;
      }
      s.last_attempt = { kind: job.kind, day: job.day, at_ms: finished, ok: false, error: reason };
    });
    const card_out = {
      day: job.day, kind: job.kind, at: iso(finished), outcome: res.ok ? "paid" : res.outcome, reason,
      error: res.error || (res.ok ? "the report came back empty" : null), run: res.run, model: res.model,
      failures: after ? after.failures : null, gave_up: after ? after.gave_up : false, left_out: skip,
    };
    if (counted || !fs.existsSync(err_file(job.day))) {
      try { write_file_atomic(err_file(job.day), `${JSON.stringify(card_out, null, 1)}\n`); }
      catch (err) { log(`[gateway] present: ${job.day}.err.json not written: ${err?.message || err}`); }
    }
    write_log({ event: "report", kind: job.kind, day: job.day, outcome: "failed", reason, counted,
                failures: card_out.failures, gave_up: card_out.gave_up, run: res.run });
    if (card_out.gave_up) console.log(`[gateway] present: the day report for ${job.day} failed ${REPORT_TRIES} times; not trying again for it`);
    return { outcome: "failed", reason, counted };
  }

  function fresh_auto(job) {
    return { occ: job.occ, day: job.day, failures: 0, gave_up: false, error: null, quiet: false, written_at: null, file: null };
  }

  // ———— ③ The flip ————

  function flip_all(report, at) {
    const keep_raw = settings.load().values.compress.keep_raw;
    for (const t of threads.list()) {
      if (!t.window || typeof t.window !== "object") continue;
      try {
        const mark = win.mark_for_tail(t.branch || [], keep_raw);
        const flip = win.open_next(t, { mark, parts: { report, summary: null }, how: "day", at }, clock.now());
        threads.save(t.id);
        console.log(`[gateway] present ${t.id} window ${flip.old_name} → ${flip.name} (day report), mark ${mark}`);
        on_flip({ thread: t.id, old_name: flip.old_name, name: flip.name, how: "day" });
      } catch (err) { log(`[gateway] present: day flip of ${t.id} failed: ${err?.message || err}`); }
    }
  }

  // ———— Jobs ————

  function start(job) {
    const promise = (async () => {
      // the changes owed first: one line not handed over yet is dropped here, before this
      // job's hand-off sends its current text
      const changes = await send_changes();
      // a report job always asks: with nothing left to hand over it comes back delivered,
      // so the letter lists what is queued
      const handed = job.handoff || job.report ? await hand_off(job.started) : null;
      if (!job.report) return { changes, handoff: handed };
      const r = await write_report(job, handed);
      if (r.outcome === "busy" && job.asked) queued = { ...job, started: null };   // she asked: wait for the runner
      return { changes, handoff: handed, report: r };
    })().catch((err) => {
      log(`[gateway] present: day close failed: ${err?.stack || err}`);
      return { error: String(err?.message || err) };
    }).finally(() => { running = null; });
    running = { kind: job.kind, promise };
    return promise;
  }

  /** The heartbeat: start what is due, never wait for it. */
  function tick() {
    if (running) return { started: false, why: "running" };
    const now = clock.now();
    if (queued) {
      const job = { ...queued, started: now };
      queued = null;
      start(job);
      return { started: true, kind: job.kind };
    }
    const r = tick_night(now);
    if (r.started || !change_owed(read_state())) return r;
    // nothing else to do, but Loci is owed a change: a job of its own
    start({ kind: "changes", day: null, occ: null, started: now, handoff: false, report: false });
    return { started: true, kind: "changes", handoff: false, report: false };
  }

  function tick_night(now) {
    const values = settings.load().values.report;
    const occ = occurrence(now, values);
    const quiet = now - last_owner_at() >= values.quiet_min * 60000;
    const st = read_state();
    const open = report_open(occ, st, values);
    if (!occ.in_window && !open) { last_logged = null; return { started: false, why: "outside" }; }
    if (!quiet) { note_once("not_quiet", { waiting: "quiet", day: occ.day }); return { started: false, why: "not_quiet" }; }
    let report = open ? { kind: occ.in_window ? "auto" : "makeup", day: occ.day, occ: occ.start } : null;
    if (report && !occ.in_window && !has_lines_before(st, occ)) {
      // the day the make-up would close out had nothing said: quiet, without a turn
      update_state((s) => { s.auto = { ...fresh_auto(report), quiet: true }; });
      write_log({ event: "report", kind: report.kind, day: report.day, outcome: "quiet" });
      report = null;
    }
    // a report the runner cannot send at all (no key after a restart, no model) is not
    // started: the reason goes on the occurrence and into the log once
    const blocked = report && typeof own_turn.blocked === "function" ? own_turn.blocked() : null;
    if (blocked) {
      const due = report;
      if (auto_of(st, { start: due.occ })?.error !== blocked) {
        update_state((s) => { s.auto = { ...(auto_of(s, { start: due.occ }) || fresh_auto(due)), error: blocked }; });
      }
      if (last_blocked !== `${due.occ}|${blocked}`) {
        last_blocked = `${due.occ}|${blocked}`;
        write_log({ event: "day_close", waiting: blocked, kind: due.kind, day: due.day });
      }
      report = null;
    } else last_blocked = null;
    // the hand-off goes in the window, or with the make-up that stands in for it (sent or held back)
    const handoff = (occ.in_window || Boolean(report) || Boolean(blocked)) && unsent_lines(st, now).length > 0;
    if (!report && !handoff) return { started: false, why: blocked || "nothing" };
    last_logged = null;
    const job = report ? { ...report, started: now, handoff, report: true }
      : { kind: "handoff", day: null, occ: null, started: now, handoff, report: false };
    start(job);
    return { started: true, kind: job.kind, handoff, report: job.report };
  }

  /** Is the occurrence's scheduled report still to be written? */
  function report_open(occ, st, values) {
    if (!cadence_due(occ, st, values)) return false;
    if (st.last_report && st.last_report.at_ms >= occ.start) return false;
    const a = auto_of(st, occ);
    return !(a && (a.written_at || a.gave_up || a.quiet));
  }

  function has_lines_before(st, occ) {
    return lines_between({ after_ms: st.last_report ? st.last_report.through_ms : null, upto_ms: occ.start - 1 }).length > 0;
  }

  /**
   * POST /present/report: `missing` = 「补一份」 (the latest occurrence's report, now),
   * `now` = 「现在写日报」 (hand-off, report, flip). → { status, body }
   */
  function report_now(kind) {
    if (kind !== "missing" && kind !== "now") return { status: 400, body: { ok: false, error: "kind 只能是 missing 或 now", field: "kind" } };
    if (running || queued) return { status: 409, body: { ok: false, error: "日报正在写，等它写完" } };
    const now = clock.now();
    let job;
    if (kind === "now") job = { kind: "now", day: day_of(now), occ: null };
    else {
      const values = settings.load().values.report;
      const occ = occurrence(now, values);
      const st = read_state();
      const a = auto_of(st, occ);
      if (!cadence_due(occ, st, values)) return { status: 409, body: { ok: false, error: `${occ.day} 不该写日报（翻页：${values.flip}）；要写就用「现在写日报」` } };
      if ((a && a.written_at) || (st.last_report && st.last_report.at_ms >= occ.start)) {
        return { status: 409, body: { ok: false, error: `${occ.day} 的日报已经写过了` } };
      }
      job = { kind: "missing", day: occ.day, occ: occ.start };
    }
    start({ ...job, started: now, handoff: true, report: true, asked: true });
    return { status: 200, body: { ok: true, queued: true, kind, day: job.day } };
  }

  // ———— What wake, the panel and /health see ————

  function report_ready() {
    const now = clock.now();
    const values = settings.load().values.report;
    const occ = occurrence(now, values);
    const st = read_state();
    if (!report_open(occ, st, values)) return true;
    return !has_lines_before(st, occ);
  }

  function read_report(file) {
    try { return fs.readFileSync(path.join(reports_dir, file), "utf8").replace(/\n$/, ""); }
    catch { return null; }
  }

  function status() {
    const now = clock.now();
    const values = settings.load().values.report;
    const occ = occurrence(now, values);
    const st = read_state();
    const a = auto_of(st, occ);
    const due = cadence_due(occ, st, values);
    const base = { day: occ.day, at: null, text: null, error: null, gave_up: false,
                   running: Boolean(running), queued: queued ? queued.kind : null, next_flip: values.flip };
    let out;
    const last = st.last_report;
    if (last && last.at_ms >= occ.start) {
      out = { ...base, state: "written", day: last.day, at: last.at, text: read_report(last.file) };
    } else if (a && a.failures > 0) {
      out = { ...base, state: "failed", error: full_error(occ.day) || a.error, gave_up: Boolean(a.gave_up) };
    } else if (st.last_attempt && !st.last_attempt.ok && st.last_attempt.at_ms >= occ.start && st.last_attempt.kind === "now") {
      out = { ...base, state: "failed", day: st.last_attempt.day, error: full_error(st.last_attempt.day) || st.last_attempt.error };
    } else if (a && a.quiet) out = { ...base, state: "quiet" };
    else if (!due) out = { ...base, state: "not_due" };
    else if (!has_lines_before(st, occ)) out = { ...base, state: "quiet" };
    else out = { ...base, state: occ.in_window ? "not_due" : "missing", error: a?.error || null };
    out.state_words = STATE_WORDS[out.state] || null;
    out.next_flip_day = next_flip_day(occ, st, values, out.state);
    return out;
  }

  function full_error(day) {
    const got = read_json_file(err_file(day));
    if (!got.ok || !is_plain(got.value)) return null;
    return got.value.error || got.value.reason || null;
  }

  function next_flip_day(occ, st, values, state) {
    if (values.flip === "manual") return null;
    const settled = ["written", "quiet"].includes(state) || (state === "failed" && auto_of(st, occ)?.gave_up);
    if (values.flip === "daily") return settled ? add_days(occ.day, 1) : occ.day;
    if (!st.last_flip_day) return occ.day;
    const next = add_days(st.last_flip_day, values.every_n);
    return next < occ.day || (next === occ.day && settled) ? (settled ? add_days(occ.day, 1) : occ.day) : next;
  }

  function health() {
    const now = clock.now();
    const st = read_state();
    const last = st.last_report;
    const h = st.handoff;
    const values = settings.load().values.report;
    const a = auto_of(st, occurrence(now, values));
    return {
      state: last ? "ok" : "never",
      last_ok_at: last ? last.at : null,
      last_ok_seconds_ago: last ? Math.max(0, Math.round((now - last.at_ms) / 1000)) : null,
      failures_since_ok: st.failures_since_ok,
      gave_up: Boolean(a && a.gave_up),
      running: Boolean(running),
      handoff: {
        last_ok_at: h.last_ok_at ? iso(h.last_ok_at) : null,
        last_ok_seconds_ago: h.last_ok_at ? Math.max(0, Math.round((now - h.last_ok_at) / 1000)) : null,
        failures_since_ok: h.failures_since_ok,
        last_error: h.last_error,
        given_up_batches: h.failed.filter((f) => f.given_up).length,
      },
      changes: {
        owed: st.changes.queue.length,
        last_ok_at: st.changes.last_ok_at ? iso(st.changes.last_ok_at) : null,
        failures_since_ok: st.changes.failures_since_ok,
        last_error: st.changes.last_error,
        given_up: st.changes.failed.length,
      },
    };
  }

  function banner_line() {
    return `speakers       ${speakers.owner} / ${speakers.ai}`
      + (speakers.owner_set && speakers.ai_set ? "" : "   (LOCI_OWNER_NAME / LOCI_AI_NAME unset — the names Loci sees on handed-over lines and originals)");
  }

  return {
    tick,
    report_now,
    report_ready,
    status,
    health,
    banner_line,
    note_revised,
    owner_arrived() { owner_seen_at = clock.now(); },
    /** Resolves once the job in flight (if any) has finished. */
    settle: () => (running ? running.promise : Promise.resolve(null)),
    is_busy: () => Boolean(running || queued),
    occurrence: (now) => occurrence(now, settings.load().values.report),
    reports_dir,
  };
}

module.exports = { create_day_close, BATCH_LINES, HANDOFF_TRIES, REPORT_TRIES, NO_PENDING, LIST_FAILED, DEFAULT_OWNER, DEFAULT_AI };
