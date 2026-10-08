// ============================================================
// gateway/relay.js — everything that is not /health: read the request, let Loci put
// what the AI ought to know into it, forward it upstream, pipe the answer back
//
// Mounted by server.js as the catch-all route. The order inside one request is the
// contract, and server.js's header explains why each poke lands where it does:
//   ① read and parse the body
//   ② present layer sees the client's messages untouched (present/index.js) — before
//      any poke edits them, and before anything goes upstream ("store before forwarding")
//   ③ poke delivery (dreams / muse) · ④ relevance reminder, pinned at the true tail
//   ⑤ the /v1/* guard · ⑥ forward · ⑦ the present layer listens to the answer as it
//      passes through, without changing a byte of what the client receives
//
// The present hooks are optional and fenced: a hook that throws costs this round its
// present work and one console line, never the chat. The present layer reports on lines
// of its own, so the per-request line keeps the shape its readers (and tests) parse.
// ============================================================

const path = require("path");
const { Readable } = require("stream");
const auto = require("./auto_attach.js");
const poke = require("./poke_delivery.js");

function read_body(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

function is_chat(req, body) {
  return req.method === "POST"
    && /\/chat\/completions$/.test(req.url.split("?")[0])
    && body && Array.isArray(body.messages);
}

/**
 * @param upstream                upstream base URL, trailing slashes already stripped
 * @param loci                    Loci MCP address
 * @param idle_threshold_minutes  poke delivery's idle gate
 * @param min_score               relevance score floor
 * @param data_root               LOCI_GATEWAY_DATA
 * @param log_path                memory-actions.jsonl (shared with health.js)
 * @param present                 optional { on_request, on_response } (present/index.js)
 */
function create_relay({ upstream, loci, idle_threshold_minutes, min_score, data_root, log_path, present = null }) {
  return async function handle_relay(req, res, { start }) {
    const raw = await read_body(req).catch(() => Buffer.alloc(0));
    let body = null;
    try { body = raw.length ? JSON.parse(raw.toString("utf8")) : null; } catch { body = null; }

    const route = req.url.split("?")[0];
    const notes = [];
    let present_turn = null;
    if (is_chat(req, body)) {
      // ---- Present layer: sees the client's own messages before any poke edits them. ----
      // Only real API traffic counts; a chat-shaped body on any other path gets the 404 below.
      // It reports on a console line of its own: the request line below keeps its shape.
      if (present && route.startsWith("/v1/")) {
        try {
          const seen = present.on_request({ route, body });
          present_turn = seen.turn;
          if (seen.note) console.log(`[gateway] ${seen.note}`);
        } catch (err) { console.error(`[gateway] present failed: ${err?.message || err}`); }
      }

      const common = {
        messages: body.messages,
        requestId: String(req.headers["x-request-id"] || start),
        logPath: log_path,
        地址: loci,
      };

      // ---- Poke delivery: dreams / muse, pinned in the same prefix as the breath paste. ----
      try {
        const d = await poke.attach_once({
          ...common,
          statePath: path.join(data_root, "state", "poke-window.json"),
          闲时阈值分钟: idle_threshold_minutes,
        });
        notes.push(d.patchInjected ? `戳戳(梦=${d.hasDream} 发呆=${d.musePending})`
          : d.calledLoci ? "戳戳无" : "戳戳没问(不够闲)");
      } catch (err) { notes.push("戳戳炸:" + (err?.message || err)); }

      // ---- Relevance reminder. **Last step, pinned at the true tail.** ----
      // Only runs when triggered (strong = keyword hit, weak = local heuristic).
      // No trigger, no recall call at all.
      try {
        const b = await auto.build_relevance_notice({ ...common, 最低分: min_score });
        if (b && b.patch) {
          auto.attach_at_true_tail(body.messages, b.patch);
          notes.push("提醒(贴了真尾巴)");
        } else notes.push("提醒无");
      } catch (err) { notes.push("提醒炸:" + (err?.message || err)); }
    } else notes.push("不是聊天，直接转发");

    // 🔴 **Anything that does not look like an API path gets a local 404.** (A whole
    //    class of bug that the gateway tests caught.)
    //    This layer is a proxy, and a proxy's default behaviour is "forward everything
    //    upstream" — which means it has **no such thing as an unrecognised address**.
    //    Misspell a route and an ordinary server gives you a 404; here it is
    //    **forwarded as usual**. In practice: `/favicon.ico`, mistyped URLs and scanner
    //    probes all go upstream carrying the client's Authorization header.
    //    It surfaced when the health endpoint was first spelled in Chinese as `/健康`:
    //    the client sends the escaped form, the byte comparison fails, it falls through
    //    to this default path → **a probe meant to check "is it working" was sent to the
    //    upstream model.**
    //    ⚠️ Patching the symptom (decodeURIComponent at the health endpoint) does not
    //       save you: curl on Windows sends `%BD%A1%BF%B5`, escaped per the local
    //       codepage, and decodeURIComponent throws outright on that — still leaking.
    //       **The cure is this line: only API paths get out.**
    if (!route.startsWith("/v1/")) {
      req.resume();
      const payload = Buffer.from(JSON.stringify({
        error: `this layer only forwards /v1/* requests; ${route} was not sent upstream.`,
        hint: "to see whether it is working: GET /health",
      }, null, 2), "utf8");
      res.writeHead(404, { "Content-Type": "application/json; charset=utf-8", "Content-Length": payload.length });
      res.end(payload);
      console.log(`[gateway] ${req.method} ${req.url} → 404（不是 /v1/*，没往上游发）`);
      return;
    }

    const forward_body = body ? Buffer.from(JSON.stringify(body)) : raw;
    const headers = { ...req.headers };
    delete headers.host; delete headers["content-length"]; delete headers["accept-encoding"];

    const target = upstream.replace(/\/v1$/, "") + req.url;
    let resp;
    try {
      resp = await fetch(target, {
        method: req.method,
        headers: headers,
        body: ["GET", "HEAD"].includes(req.method) ? undefined : forward_body,
      });
    } catch (err) {
      res.writeHead(502, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ error: "cannot reach upstream: " + String(err?.message || err) }));
      console.error(`[gateway] ${req.method} ${req.url} → 上游连不上：${err?.message || err}`);
      return;
    }

    const resp_headers = {};
    // 🔴 `content-length` has to be dropped together with `content-encoding` — found by
    //    the gateway's first test suite.
    //    The `delete headers["accept-encoding"]` above means "do not let upstream
    //    compress", but Node's built-in fetch **puts one back itself**,
    //    `accept-encoding: gzip, deflate`, so upstream gzips anyway. fetch then
    //    decompresses the body while `content-length` still describes the **compressed**
    //    size. Skip only content-encoding and the client is told "N bytes in total" (the
    //    compressed number) while the real body is the longer decompressed one —
    //    **cut off at N, half a JSON document.**
    //    ⚠️ Why it survived this long: chat calls are almost all stream:true, and a
    //       streamed response is chunked with no content-length, so the whole path is
    //       bypassed. **Only non-streaming calls get bitten** (completions, embeddings,
    //       any synchronous SDK call) — another failure you never see day to day.
    //    With both dropped, Node computes the length from the real body (or goes
    //    chunked) and the two sides agree again.
    resp.headers.forEach((v, k) => {
      if (k !== "content-encoding" && k !== "content-length") resp_headers[k] = v;
    });
    res.writeHead(resp.status, resp_headers);
    if (resp.body) {
      const stream = Readable.fromWeb(resp.body);
      // The present layer listens alongside the pipe; it reads the same chunks and writes nothing to res.
      if (present && present_turn) {
        try { present.on_response(present_turn, { status: resp.status, headers: resp_headers, stream }); }
        catch (err) { console.error(`[gateway] present failed to listen: ${err?.message || err}`); }
      }
      stream.pipe(res);
    } else res.end();

    console.log(`[gateway] ${req.method} ${req.url} → ${resp.status}  ${Date.now() - start}ms  ${notes.join(" · ")}`);
  };
}

module.exports = { create_relay, is_chat };
