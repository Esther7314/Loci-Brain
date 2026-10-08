// ============================================================
// gateway/relay.js — everything that is not /health: read the request, let the present
// layer build the copy that goes upstream, forward it, pipe the answer back
//
// Mounted by server.js as the catch-all route. The order inside one request:
//   ① read and parse the body
//   ② the /v1/* guard: anything else is answered 404 here, before anyone is asked anything
//   ③ a chat request: the present layer (present/index.js) sees the client's messages
//      untouched, stores the owner's line, asks Loci for this turn's card and poke
//      delivery for a dream / muse line, and hands back the copy to forward — carry,
//      window, overlays replayed, usage asked for
//   ④ forward · ⑤ the present layer listens to the answer as it passes through and hands
//      back the stream the client gets (upstream's bytes, less the usage chunk the client
//      did not ask for)
//
// The present hooks are fenced: a hook that throws costs this round its present work and
// one console line, never the chat — the client's own body is forwarded as it came.
// The present layer reports on lines of its own; the per-request line keeps the shape its
// readers (and tests) parse.
// ============================================================

const { Readable } = require("stream");

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
 * @param upstream  upstream base URL, trailing slashes already stripped
 * @param present   optional { prepare, on_response } (present/index.js)
 */
function create_relay({ upstream, present = null }) {
  return async function handle_relay(req, res, { start }) {
    const raw = await read_body(req).catch(() => Buffer.alloc(0));
    let body = null;
    try { body = raw.length ? JSON.parse(raw.toString("utf8")) : null; } catch { body = null; }

    const route = req.url.split("?")[0];
    const notes = [];

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

    // ---- The present layer builds the copy that goes upstream. ----
    let outgoing = body;
    let present_ctx = null;
    if (is_chat(req, body)) {
      if (present) {
        try {
          const prepared = await present.prepare({
            body, headers: req.headers, request_id: String(req.headers["x-request-id"] || start),
          });
          if (prepared.note) console.log(`[gateway] ${prepared.note}`);
          if (prepared.body) outgoing = prepared.body;
          present_ctx = prepared.ctx;
          notes.push(...prepared.notes);
        } catch (err) {
          console.error(`[gateway] present failed: ${err?.message || err}`);
          notes.push("present炸，原样转发");
        }
      }
    } else notes.push("不是聊天，直接转发");

    const forward_body = body ? Buffer.from(JSON.stringify(outgoing)) : raw;
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

    // A chat request upstream accepted lends its credential and model to the gateway's own
    // turns (present/own_turn.js): held in memory only, never written or logged.
    if (present?.remember_owner && is_chat(req, body) && resp.ok) {
      try { present.remember_owner({ headers: req.headers, model: body.model }); }
      catch (err) { console.error(`[gateway] present failed to note the credential: ${err?.message || err}`); }
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
      // The present layer listens alongside the pipe and may hand back the stream the client gets.
      let to_client = stream;
      if (present && present_ctx) {
        try { to_client = present.on_response(present_ctx, { status: resp.status, headers: resp_headers, stream }) || stream; }
        catch (err) { console.error(`[gateway] present failed to listen: ${err?.message || err}`); }
      }
      to_client.pipe(res);
    } else res.end();

    console.log(`[gateway] ${req.method} ${req.url} → ${resp.status}  ${Date.now() - start}ms  ${notes.join(" · ")}`);
  };
}

module.exports = { create_relay, is_chat };
