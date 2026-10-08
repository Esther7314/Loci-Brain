// ============================================================
// gateway/present/present_api.js — /present/*: the panel's present page, asked through Loci
//
// The panel talks only to Loci; Loci forwards its present page to these routes with its
// own key for this host (Authorization: Bearer <LOCI_GATEWAY_TOKEN>, checked by
// http_io.behind_token before anything here runs) and passes the reply through as it is.
// So what this file answers is exactly what the page receives.
//
//   GET  /present            { host, connected: true, settings, settings_state, status }
//   POST /present            { patch: {...} } → 400 { error, field } for a value that does
//                            not pass, 409 when present.json cannot be read; else as GET
//   POST /present/compress   { thread? } → { ok: true, queued: true, thread }: 「现在压」, a pack
//                            of that conversation (default: the most recent) starts in the
//                            background (pack.js, how "manual"); 409 while one is already
//                            running for it, 404 when there is no such conversation. The
//                            result shows up as status.compress.last.
//   POST /present/report     { kind: "missing" | "now" } → { ok: true, queued: true, kind, day }:
//                            「补一份」 (the latest scheduled report, now) or 「现在写日报」
//                            (hand-off, report, and her next turn opens the new window),
//                            in the background (day_close.js); 409 while one is being
//                            written or when nothing is missing. The result shows up as
//                            status.report.
//   POST /present/push-test  one Bark push now (push.js test), answered 200 with Bark's
//                            outcome: { ok: true, status: 200 } · { ok: false, status:
//                            "error", error: "超时：Bark 没回" } · { ok: false, status:
//                            "skipped_config_missing", … }, plus endpoint_redacted (masked)
//   GET  /present/prompts    { items: [card…], error? }  (prompts.js)
//   POST /present/prompts    { key, text } | { key, reset: true } → { ok: true, item }
// Any other path or method under /present is a 404.
//
// status.compress is the most recently active conversation's window, from
// present/index.js window_status(): thread, fill_pct, used_tokens, estimated, kept_raw
// (raw lines from the mark on), context {tokens, source}, and last {at, how, how_words}
// (how the window was opened; null for a conversation's first window).
//
// status.report is day_close.js status(): day, state (written · missing · failed · quiet ·
// not_due) with state_words, at, text (the report itself: the panel shows it to her),
// error (the whole reason), gave_up, running, queued, next_flip (report.flip) and
// next_flip_day.
//
// 🔴 No reply here carries a single character of what he compressed (the carry), the
//    overlay, or the last request sent upstream: they live in the private thread ledger,
//    and window_status() hands this file numbers, names and a time, never text. A part the
//    present object does not carry says so (`state: "not_built"`, nulls) instead of
//    making something up.
// ============================================================

const { read_json, send_json } = require("./http_io.js");

const NOT_BUILT = { ok: false, state: "not_built" };

/**
 * @param name             LOCI_GATEWAY_NAME (the host name the reply carries)
 * @param settings         settings.js instance
 * @param prompts          prompts.js instance
 * @param threads          threads.js instance (which conversation was last active)
 * @param window_status    present/index.js window_status (a window as numbers and names)
 * @param context_windows  context_window.js instance (the window size when there is no thread)
 * @param wake             wake.js instance (status.wake is its status(): counts, times, reasons)
 * @param compress_now     present/index.js compress_now (「现在压」: thread id or null → { status, body })
 * @param push             push.js instance (status.push is its status(): state, last {at, ok, status}, retrying)
 * @param report_now       present/index.js report_now (kind → { status, body })
 * @param report_status    present/index.js report_status (status.report)
 */
function create_present_api({
  name, settings, prompts, threads, window_status, context_windows, wake, compress_now, report_now, report_status,
  push = null,
}) {
  function latest_thread() {
    let best = null;
    for (const t of threads.list()) if (!best || (t.last_at || 0) > (best.last_at || 0)) best = t;
    return best ? best.id : null;
  }

  /** The most recent conversation's window, as numbers, names and a time: never its text. */
  function compress_status() {
    const id = latest_thread();
    const w = id ? window_status(id) : null;
    if (!w) {
      return { state: "no_thread", thread: null, fill_pct: null, used_tokens: null, estimated: null,
               kept_raw: null, last: null, context: context_windows.resolve(null) };
    }
    return {
      state: "ok",
      thread: w.thread,
      fill_pct: w.fill_pct,
      used_tokens: w.used_tokens,
      estimated: w.estimated,
      kept_raw: w.kept_raw,
      last: w.last,
      context: { tokens: w.context_tokens, source: w.context_source },
    };
  }

  function status(values) {
    return {
      compress: compress_status(),
      report: report_status ? report_status()
        : { state: "not_built", day: null, at: null, text: null, error: null, gave_up: null, next_flip: values.report.flip },
      wake: wake.status(),
      push: push ? push.status() : { state: "not_built", last: null },
    };
  }

  function snapshot() {
    const loaded = settings.load();
    return {
      host: name,
      connected: true,
      settings: settings.view(loaded.values),
      settings_state: { state: loaded.state, errors: loaded.errors },
      status: status(loaded.values),
    };
  }

  const routes = {
    "GET /present": async (req, res) => { req.resume(); send_json(res, 200, snapshot()); },
    "POST /present": async (req, res) => {
      const got = await read_json(req);
      if (!got.ok) return send_json(res, 400, { error: got.error, field: null });
      const body = got.value;
      if (!body || typeof body !== "object" || !("patch" in body)) {
        return send_json(res, 400, { error: "要传 { patch: {...} }", field: "patch" });
      }
      const done = settings.patch(body.patch);
      if (!done.ok) return send_json(res, done.status, { error: done.error, field: done.field });
      return send_json(res, 200, snapshot());
    },
    "POST /present/compress": async (req, res) => {
      const got = await read_json(req);
      if (!got.ok) return send_json(res, 400, { error: got.error });
      const thread = got.value && typeof got.value === "object" ? got.value.thread ?? null : null;
      if (thread !== null && typeof thread !== "string") return send_json(res, 400, { error: "thread 要是一个对话的 id", field: "thread" });
      const done = compress_now(thread);
      return send_json(res, done.status, done.body);
    },
    "POST /present/report": async (req, res) => {
      const got = await read_json(req);
      if (!got.ok) return send_json(res, 400, { error: got.error });
      const kind = got.value && typeof got.value === "object" ? got.value.kind : null;
      const done = report_now(kind);
      return send_json(res, done.status, done.body);
    },
    // ── push (push.js): one test push, its answer as it is (ok or not, it is an answer) ──
    "POST /present/push-test": async (req, res) => {
      req.resume();
      if (!push) return send_json(res, 501, { ...NOT_BUILT, error: "推送还没接上" });
      return send_json(res, 200, await push.test());
    },
    "GET /present/prompts": async (req, res) => { req.resume(); send_json(res, 200, prompts.cards()); },
    "POST /present/prompts": async (req, res) => {
      const got = await read_json(req);
      if (!got.ok) return send_json(res, 400, { error: got.error });
      const done = prompts.apply(got.value);
      if (!done.ok) return send_json(res, done.status, { error: done.error });
      return send_json(res, 200, { ok: true, item: done.item });
    },
  };

  return async function handle_present(req, res) {
    const route = req.url.split("?")[0].replace(/\/+$/, "") || "/";
    const handler = routes[`${req.method} ${route}`];
    if (!handler) { req.resume(); return send_json(res, 404, { error: `present 没有 ${req.method} ${route} 这个口` }); }
    return handler(req, res);
  };
}

module.exports = { create_present_api };
