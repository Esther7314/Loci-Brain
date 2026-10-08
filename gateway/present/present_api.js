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
//   POST /present/compress   ┐ 501 { ok: false, state: "not_built" } until the step that
//   POST /present/report     │ does the work lands. Not a queued stub: "queued" would be a
//   POST /present/push-test  ┘ promise nothing keeps, and the page would wait on it.
//   GET  /present/prompts    { items: [card…], error? }  (prompts.js)
//   POST /present/prompts    { key, text } | { key, reset: true } → { ok: true, item }
// Any other path or method under /present is a 404.
//
// 🔴 No reply here carries a single character of what he compressed (the carry), the
//    overlay, or the last request sent upstream: those live in the private thread ledger
//    and nothing in this file reads them. A status that is not built yet says so
//    (`state: "not_built"`, nulls) instead of making something up.
// ============================================================

const { read_json, send_json } = require("./http_io.js");

const NOT_BUILT = { ok: false, state: "not_built" };

/**
 * @param name      LOCI_GATEWAY_NAME (the host name the reply carries)
 * @param settings  settings.js instance
 * @param prompts   prompts.js instance
 * @param threads   threads.js instance (which conversation was last active)
 */
function create_present_api({ name, settings, prompts, threads }) {
  function latest_thread() {
    let best = null;
    for (const t of threads.list()) if (!best || (t.last_at || 0) > (best.last_at || 0)) best = t;
    return best ? best.id : null;
  }

  function status(values, wake_values) {
    const own_tokens = values.compress.context_tokens;
    return {
      compress: {
        state: "not_built",
        thread: latest_thread(),
        fill_pct: null, used_tokens: null, estimated: null, kept_raw: null,
        context: { tokens: own_tokens, source: own_tokens ? "user" : "not_built" },
        last: null,
      },
      report: { state: "not_built", day: null, at: null, text: null, error: null, gave_up: null, next_flip: values.report.flip },
      wake: {
        state: "not_built",
        last: null, next_at: null,
        next_why: wake_values === null ? "settings_unreadable" : (wake_values.on ? null : "off"),
        today: null, held: null,
      },
      push: { state: "not_built", last: null },
    };
  }

  function snapshot() {
    const loaded = settings.load();
    return {
      host: name,
      connected: true,
      settings: settings.view(loaded.values),
      settings_state: { state: loaded.state, errors: loaded.errors },
      status: status(loaded.values, settings.for_wake()),
    };
  }

  const routes = {
    "GET /present": async (req, res) => { req.resume(); send_json(res, 200, snapshot()); },
    "POST /present": async (req, res) => {
      const got = await read_json(req);
      if (!got.ok) return send_json(res, 400, { error: got.error, field: null });
      const body = got.value;
      if (!body || typeof body !== "object" || !("patch" in body)) {
        return send_json(res, 400, { error: "send { patch: {...} }", field: "patch" });
      }
      const done = settings.patch(body.patch);
      if (!done.ok) return send_json(res, done.status, { error: done.error, field: done.field });
      return send_json(res, 200, snapshot());
    },
    "POST /present/compress": async (req, res) => { req.resume(); send_json(res, 501, { ...NOT_BUILT, error: "compressing on request is not built yet" }); },
    "POST /present/report": async (req, res) => { req.resume(); send_json(res, 501, { ...NOT_BUILT, error: "the day report is not built yet" }); },
    "POST /present/push-test": async (req, res) => { req.resume(); send_json(res, 501, { ...NOT_BUILT, error: "push is not built yet" }); },
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
    if (!handler) { req.resume(); return send_json(res, 404, { error: `present has no ${req.method} ${route}` }); }
    return handler(req, res);
  };
}

module.exports = { create_present_api };
