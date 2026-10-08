// ============================================================
// gateway/present/http_io.js — the small pieces every gateway-owned HTTP route shares
//
//   · read_json(req)       the body as JSON, capped; a body that is too big or not JSON
//                          comes back as { ok: false } with the reason, never a throw
//   · send_json(res, ...)  a JSON reply with Cache-Control: no-store (what these routes
//                          answer is the owner's state, and nothing in between keeps it)
//   · bearer_ok(req, tok)  `Authorization: Bearer <tok>`, compared in constant time
//
// The credential on these routes is LOCI_GATEWAY_TOKEN: Loci's key toward this gateway
// (Loci → gateway). It is never logged, never echoed, never written anywhere.
// ============================================================

const crypto = require("crypto");

const MAX_BODY_BYTES = 256 * 1024;

function read_json(req, limit = MAX_BODY_BYTES) {
  return new Promise((resolve) => {
    const chunks = [];
    let size = 0;
    let done = false;
    const finish = (value) => { if (!done) { done = true; resolve(value); } };
    req.on("data", (c) => {
      if (done) return;
      size += c.length;
      if (size > limit) { finish({ ok: false, error: `请求体超过 ${limit} 字节` }); req.resume(); return; }
      chunks.push(c);
    });
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      if (!raw.trim()) return finish({ ok: true, value: {} });
      try { finish({ ok: true, value: JSON.parse(raw) }); }
      catch { finish({ ok: false, error: "请求体不是 JSON" }); }
    });
    req.on("error", () => finish({ ok: false, error: "请求体读不出来" }));
  });
}

function send_json(res, status, obj, extra_headers = {}) {
  const buf = Buffer.from(JSON.stringify(obj), "utf8");
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": buf.length,
    "Cache-Control": "no-store",
    ...extra_headers,
  });
  res.end(buf);
}

const digest = (s) => crypto.createHash("sha256").update(String(s), "utf8").digest();

/** True when the request carries exactly `Bearer <token>`. An empty token never matches. */
function bearer_ok(req, token) {
  if (!token) return false;
  const header = String(req.headers.authorization || "");
  const m = /^Bearer\s+(.+)$/i.exec(header.trim());
  if (!m) return false;
  // Hash both sides first: timingSafeEqual needs equal lengths, and the length of the
  // real token is not something a wrong guess should be able to learn.
  return crypto.timingSafeEqual(digest(m[1].trim()), digest(token));
}

/**
 * Wrap a handler behind LOCI_GATEWAY_TOKEN.
 *   · token unset → 404 with `connected: false` and the reason. Closed, not 401: a 401
 *     reaches the panel through Loci as "the gateway refuses Loci's key", which sends the
 *     owner to check Loci's side when what is missing is the gateway's own setting; and a
 *     route nobody can use is, for every caller, a route that is not there.
 *     `closed_status` changes that number for a route whose caller reads statuses
 *     differently: /loci/source passes 503, because Loci (core/_originals.py) reads a 404
 *     as NOT_ALLOWED and hides the memory's own body, while 503 reads as UNAVAILABLE and
 *     lets that body stand in — the right outcome for a gateway that is merely not set up.
 *   · wrong or missing Bearer → 401.
 * The handler is called as handler(req, res, ctx).
 */
function behind_token(token, handler, { closed_status = 404 } = {}) {
  return async (req, res, ctx) => {
    if (!token) {
      req.resume();
      return send_json(res, closed_status, { connected: false,
        error: "关着：网关没设 LOCI_GATEWAY_TOKEN，谁都进不来" });
    }
    if (!bearer_ok(req, token)) {
      req.resume();
      return send_json(res, 401, { error: "要带 Authorization: Bearer <LOCI_GATEWAY_TOKEN>" },
        { "WWW-Authenticate": "Bearer" });
    }
    return handler(req, res, ctx);
  };
}

module.exports = { read_json, send_json, bearer_ok, behind_token, MAX_BODY_BYTES };
