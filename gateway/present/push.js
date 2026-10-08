// ============================================================
// gateway/present/push.js — Bark: what he said while she was away reaches her phone
//
// A wake that spoke (wake.js) holds the words in held.json and hands them here
// (wake's on_spoke). The words stay held whatever happens to the push: the away step
// (away.js) prefixes them on her next turn either way, so a push is a knock on the door,
// never the only copy.
//
// ─── Bark ───
// push.bark (present.json) is either a device key or a whole Bark URL:
//   · a key      → <bark_base>/<key>, bark_base https://api.day.app (LOCI_BARK_BASE
//                  changes it: a self-hosted Bark server, or a test's fake one)
//   · a URL      → used as it is (a self-hosted server, a URL with its own query)
// The push is one POST of JSON { title, body } to that endpoint. Not the GET form with
// the text in the path: the default is the full text (§七.9), which can be long, and a
// path carries it into every proxy's and server's access log on the way.
//   push.text "full"   → the body is what he said, as he said it — cut at BODY_MAX_BYTES
//                        (UTF-8) with 「……」 when longer: Apple refuses a notification
//                        over 4 KB whole, and a push that is refused every time would
//                        only be retried and given up. The whole text is in the prefix.
//   push.text "notice" → the body is only 「ta 说了句话」, for whoever does not want the
//                        words to pass through Bark's server
// 10 s timeout. The answer is { ok, status } or { ok: false, status: "error", error }
// (the shape of Lento's bark.js), plus `endpoint_redacted`.
// 🔴 The key and the URL are never written anywhere as they are: logs, answers and errors
//    carry the masked form (settings.js bark_hint: origin and the key's last two
//    characters), and an error text has the endpoint, the key and every path segment and
//    query value of the URL masked out before it is kept.
//
// ─── When ───
//   · on_spoke: at once — unless it is DND (dnd.js, the one place that decides it; the
//     wake section's span), in which case nothing is sent
//   · a failed push is tried again on the heartbeat (beat()), at RETRY_AFTER_MIN after
//     the first try: up to 3 more times, all within 15 minutes of the first; past that
//     it is given up (logged). A beat inside DND sends nothing and spends no try; the
//     15 minutes run on regardless, so a push the night swallowed is simply given up.
//   · a held line that was confirmed (she saw it in the prefix) is not pushed again
//   · push.bark empty: nothing is sent and nothing retried
// What a retry needs is kept by the held line's key (its day-store id) in counters.json
// under `push`; the text is read from held.json at the time, never copied into the books.
//
// ─── Status ───
// counters.json `push`: last {at, ok, status, kind}, last_ok_at, failures_since_ok, and
// the retries in flight. A test push (POST /present/push-test) counts like any other:
// it is a real push, and a real answer about whether the pipe works.
// Log lines (logs/present.jsonl) carry the kind, the outcome and the masked endpoint,
// never the text.
// ============================================================

const fs = require("fs");
const path = require("path");
const { local_stamp } = require("./clock.js");
const { dnd_state } = require("./dnd.js");
const { bark_hint } = require("./settings.js");

const DEFAULT_BARK_BASE = "https://api.day.app";
const TIMEOUT_MS = 10000;
const RETRY_AFTER_MIN = [2, 6, 12];          // minutes after the first try; 3 retries
const GIVE_UP_AFTER_MS = 15 * 60 * 1000;
const TITLE = "Loci";
const NOTICE = "ta 说了句话";
const TEST_BODY = "这是一条试推。收到就说明推送通了。";
const BODY_MAX_BYTES = 3000;
const CUT_MARK = "……";

/** The text as a push body: whole, or cut on a character boundary to fit BODY_MAX_BYTES. */
function fit_body(text) {
  const s = String(text ?? "");
  if (Buffer.byteLength(s, "utf8") <= BODY_MAX_BYTES) return s;
  const room = BODY_MAX_BYTES - Buffer.byteLength(CUT_MARK, "utf8");
  let out = "";
  let used = 0;
  for (const c of s) {
    const b = Buffer.byteLength(c, "utf8");
    if (used + b > room) break;
    out += c;
    used += b;
  }
  return out + CUT_MARK;
}

const is_plain = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

/** The endpoint a push.bark code stands for, or "" when there is none. */
function endpoint_of(code, base = DEFAULT_BARK_BASE) {
  const s = String(code || "").trim();
  if (!s) return "";
  if (/^https?:\/\//i.test(s)) return s;
  return `${String(base || DEFAULT_BARK_BASE).replace(/\/+$/, "")}/${s}`;
}

/** The endpoint as it may be shown: origin and the key's tail. */
function mask_endpoint(endpoint) {
  return endpoint ? bark_hint(endpoint) || "••••" : "";
}

/** Any text with the endpoint, the key and every path segment / query value of it masked out. */
function scrub(text, endpoint) {
  let out = String(text ?? "");
  if (!endpoint) return out;
  const secrets = new Set([endpoint, endpoint.replace(/\/+$/, "")]);
  try {
    const u = new URL(endpoint);
    for (const seg of u.pathname.split("/")) if (seg.length >= 4) { secrets.add(seg); secrets.add(decodeURIComponent(seg)); }
    for (const v of u.searchParams.values()) if (v.length >= 4) secrets.add(v);
    if (u.username) secrets.add(u.username);
    if (u.password) secrets.add(u.password);
  } catch { /* not a URL: the endpoint itself is masked above */ }
  for (const s of [...secrets].filter(Boolean).sort((a, b) => b.length - a.length)) out = out.split(s).join("••••");
  return out;
}

/**
 * One push. Never throws.
 * @returns { ok, status, error?, endpoint_redacted }
 */
async function send_bark({ endpoint, title, body, timeout_ms = TIMEOUT_MS, fetch_impl = fetch }) {
  const endpoint_redacted = mask_endpoint(endpoint);
  if (!endpoint) return { ok: false, status: "skipped_config_missing", error: "Bark not configured", endpoint_redacted };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout_ms);
  try {
    const resp = await fetch_impl(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ title, body }),
      signal: controller.signal,
    });
    const text = await resp.text().catch(() => "");
    if (resp.ok) return { ok: true, status: resp.status, endpoint_redacted };
    let said = "";
    try { said = String(JSON.parse(text)?.message ?? ""); } catch { said = ""; }
    return { ok: false, status: resp.status, error: scrub(said || `HTTP ${resp.status}`, endpoint).slice(0, 200), endpoint_redacted };
  } catch (err) {
    const aborted = err?.name === "AbortError" || controller.signal.aborted;
    const why = aborted ? "timeout" : [err?.message, err?.cause?.code || err?.cause?.message].filter(Boolean).join(": ");
    return { ok: false, status: "error", error: scrub(why || "error", endpoint).slice(0, 200), endpoint_redacted };
  } finally {
    clearTimeout(timer);
  }
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

/** What a held line's push says, by push.text. */
function body_of(item, mode) {
  return mode === "notice" ? NOTICE : fit_body(item?.text);
}

/** A held line's key: its day-store id, or its time when the line was not written. */
function held_key(item) {
  return item && item.line ? String(item.line) : `at:${item?.at ?? ""}`;
}

function blank_state() {
  return { last: null, last_ok_at: null, failures_since_ok: 0, retrying: [] };
}

/**
 * @param data_root   LOCI_GATEWAY_DATA (counters.json, held.json, logs/present.jsonl)
 * @param settings    settings.js instance (push.bark, push.text; the wake section's DND span)
 * @param bark_base   where a bare key is sent (LOCI_BARK_BASE; Bark's own server by default)
 * @param fetch_impl  the HTTP client (fetch); tests point push.bark at a fake server instead
 */
function create_push({
  data_root, settings, clock, zone, log = console.error,
  bark_base = DEFAULT_BARK_BASE, timeout_ms = TIMEOUT_MS, fetch_impl = fetch,
}) {
  const counters_file = path.join(data_root, "counters.json");
  const held_file = path.join(data_root, "held.json");
  const log_file = path.join(data_root, "logs", "present.jsonl");
  const iso = (ms) => local_stamp(ms, zone).iso;
  const in_flight = new Set();
  let beating = false;

  function write_log(entry) {
    try {
      fs.mkdirSync(path.dirname(log_file), { recursive: true });
      fs.appendFileSync(log_file, `${JSON.stringify({ at: iso(clock.now()), event: "push", ...entry })}\n`, "utf8");
    } catch (err) { log(`[gateway] present: push log not written: ${err?.message || err}`); }
  }

  function load_state() {
    const got = read_json_file(counters_file);
    if (!got.ok) return { ok: false, error: got.error };
    const all = is_plain(got.value) ? got.value : {};
    const kept = is_plain(all.push) ? all.push : {};
    const push = { ...blank_state(), ...kept };
    if (!Array.isArray(push.retrying)) push.retrying = [];
    return { ok: true, all, push };
  }

  /** Read, change, write back; the other sections of counters.json are left as they are. */
  function update_state(change) {
    const got = load_state();
    if (!got.ok) { log(`[gateway] present: counters.json cannot be read, push books not kept: ${got.error}`); return null; }
    change(got.push);
    write_json_file(counters_file, { ...got.all, push: got.push });
    return got.push;
  }

  function held_items() {
    const got = read_json_file(held_file);
    if (!got.ok || !is_plain(got.value) || !Array.isArray(got.value.items)) return null;
    return got.value.items;
  }

  function config() {
    const values = settings.load().values;
    return { endpoint: endpoint_of(values.push.bark, bark_base), text: values.push.text };
  }

  function quiet_now() {
    return dnd_state(settings.for_wake(), clock.now(), zone).dnd;
  }

  /** One push, booked: last, last_ok_at, failures_since_ok, one log line. */
  async function push_once({ kind, body, endpoint, key = null, attempt = null }) {
    const r = await send_bark({ endpoint, title: TITLE, body, timeout_ms, fetch_impl });
    const at = clock.now();
    update_state((p) => {
      p.last = { at, ok: r.ok, status: r.status, kind };
      if (r.ok) { p.last_ok_at = at; p.failures_since_ok = 0; }
      else p.failures_since_ok = (p.failures_since_ok || 0) + 1;
    });
    write_log({ kind, ok: r.ok, status: r.status, error: r.error ?? null, endpoint: r.endpoint_redacted,
                line: key, attempt });
    return r;
  }

  /**
   * A wake spoke: push it now (or not, in DND), and keep a retry when it fails.
   * @param item  the held.json entry { at, line, thread, text }
   */
  async function on_spoke(item) {
    const key = held_key(item);
    const { endpoint, text } = config();
    if (!endpoint) { write_log({ kind: "held", ok: false, status: "skipped_config_missing", line: key }); return null; }
    const first = clock.now();
    if (quiet_now()) {
      // nothing goes out in DND; the beat looks again, and gives up 15 minutes on
      update_state((p) => { p.retrying = [...p.retrying.filter((r) => r.key !== key), { key, first, tries: 0, next_at: first }]; });
      write_log({ kind: "held", ok: false, status: "skipped_dnd", line: key });
      return null;
    }
    in_flight.add(key);
    try {
      const r = await push_once({ kind: "held", body: body_of(item, text), endpoint, key, attempt: 1 });
      if (!r.ok) {
        update_state((p) => {
          p.retrying = [...p.retrying.filter((x) => x.key !== key),
                        { key, first, tries: 1, next_at: first + RETRY_AFTER_MIN[0] * 60000 }];
        });
      }
      return r;
    } finally { in_flight.delete(key); }
  }

  /** The heartbeat's push step: retries that are due. */
  async function beat() {
    if (beating) return;
    beating = true;
    try { await beat_once(); }
    finally { beating = false; }
  }

  async function beat_once() {
    const st = load_state();
    if (!st.ok || !st.push.retrying.length) return;
    const now = clock.now();
    const items = held_items();
    const done = new Set();         // keys that leave the retry list
    const bumped = new Map();       // key → { tries, next_at }
    const { endpoint, text } = config();
    const quiet = quiet_now();
    for (const r of st.push.retrying) {
      if (in_flight.has(r.key)) continue;
      const item = items ? items.find((i) => held_key(i) === r.key) : null;
      if (items && !item) { done.add(r.key); write_log({ kind: "retry", ok: null, status: "dropped_confirmed", line: r.key }); continue; }
      if (now - r.first > GIVE_UP_AFTER_MS || r.tries > RETRY_AFTER_MIN.length) {
        done.add(r.key);
        write_log({ kind: "retry", ok: false, status: "given_up", line: r.key, tries: r.tries });
        continue;
      }
      if (now < r.next_at || quiet || !item) continue;
      if (!endpoint) { done.add(r.key); continue; }
      in_flight.add(r.key);
      try {
        const res = await push_once({ kind: "retry", body: body_of(item, text),
                                      endpoint, key: r.key, attempt: r.tries + 1 });
        const tries = r.tries + 1;
        if (res.ok) done.add(r.key);
        else if (tries > RETRY_AFTER_MIN.length) {
          done.add(r.key);
          write_log({ kind: "retry", ok: false, status: "given_up", line: r.key, tries });
        } else {
          // a try the night postponed (tries 0) goes again on the next beat
          const offset = RETRY_AFTER_MIN[Math.max(0, tries - 1)] * 60000;
          bumped.set(r.key, { tries, next_at: Math.max(now + 60000, r.first + offset) });
        }
      } finally { in_flight.delete(r.key); }
    }
    if (!done.size && !bumped.size) return;
    update_state((p) => {
      p.retrying = p.retrying.filter((r) => !done.has(r.key)).map((r) => (bumped.has(r.key) ? { ...r, ...bumped.get(r.key) } : r));
    });
  }

  /** POST /present/push-test: one push now, DND or not (she pressed the button). */
  async function test() {
    const { endpoint } = config();
    if (!endpoint) {
      const r = await send_bark({ endpoint: "", title: TITLE, body: TEST_BODY });
      write_log({ kind: "test", ok: false, status: r.status });
      return r;
    }
    return push_once({ kind: "test", body: TEST_BODY, endpoint });
  }

  // ———— What the panel and /health see: times, outcomes and counts, never a key or a text ————

  function status() {
    const st = load_state();
    const p = st.ok ? st.push : blank_state();
    const { endpoint } = config();
    return {
      state: endpoint ? "on" : "off",
      last: p.last ? { at: iso(p.last.at), ok: Boolean(p.last.ok), status: p.last.status ?? null } : null,
      retrying: p.retrying.length,
    };
  }

  function health() {
    const now = clock.now();
    const st = load_state();
    const p = st.ok ? st.push : null;
    const { endpoint } = config();
    return {
      state: endpoint ? "on" : "off",
      last_ok_at: p && p.last_ok_at ? iso(p.last_ok_at) : null,
      last_ok_seconds_ago: p && p.last_ok_at ? Math.max(0, Math.round((now - p.last_ok_at) / 1000)) : null,
      failures_since_ok: p ? p.failures_since_ok : null,
      last_failure_status: p && p.last && !p.last.ok ? p.last.status ?? null : null,
      retrying: p ? p.retrying.length : null,
    };
  }

  return { on_spoke, beat, test, status, health };
}

module.exports = {
  create_push, send_bark, endpoint_of, mask_endpoint, scrub, held_key, fit_body, BODY_MAX_BYTES,
  DEFAULT_BARK_BASE, RETRY_AFTER_MIN, GIVE_UP_AFTER_MS, NOTICE, TITLE,
};
