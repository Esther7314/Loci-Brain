// ============================================================
// gateway/door.js — where the gateway listens, and the passphrase on a non-loopback bind
//
// LOCI_GATEWAY_BIND is the address the gateway listens on (default 127.0.0.1). On a
// loopback address only this machine can reach it. On any other address (a phone on the
// LAN reaching the computer) whoever can reach the port could read what the cards carry
// — the owner's memories — so **every path must start with a passphrase segment**:
//     /<LOCI_GATEWAY_PASSPHRASE>/v1/chat/completions
//     /<LOCI_GATEWAY_PASSPHRASE>/present
//     /<LOCI_GATEWAY_PASSPHRASE>/loci/source
//     /<LOCI_GATEWAY_PASSPHRASE>/health
// A path without it is a 404 that says nothing else. The segment is taken off before
// routing, so nothing downstream — the upstream model included — ever sees it, and the
// per-request log lines never print it. Loci's `present_url` / `fetch_url` for this host
// carry it as a path (`http://<ip>:3100/<passphrase>`).
//
// On loopback no prefix is needed. When a passphrase is set anyway, a prefixed path is
// also accepted, so one URL works from both sides of a bind change.
//
// Startup refuses (check_door returns an error) a non-loopback bind with no passphrase,
// or a passphrase too short or with characters that do not survive a URL path untouched.
//
// ─── Who may knock: web pages (create_caller_check) ───
// A loopback bind keeps other machines out, not other web sites: any page open in her
// browser can send a request to 127.0.0.1:3100 (a `no-cors` text/plain POST needs no
// permission), and through DNS rebinding a page can even read the answer. A chat request
// writes her day store, starts a thread, stops a wake and resets her quiet time before
// upstream ever looks at the key, so such a request must be turned away at the door:
//   · Host (loopback bind only): the name the request was addressed to must be a
//     loopback name — localhost, *.localhost, 127.x.x.x, [::1] — or one listed in
//     LOCI_GATEWAY_HOSTS (a name of her own in the hosts file, a local reverse proxy).
//     A rebinding page addresses the gateway by its own domain name, so it fails here.
//     On any other bind the passphrase in the path is what keeps strangers out.
//   · Origin: a request without one is a native client (Kelivo, curl, an SDK) and goes
//     on. One with an Origin goes on when that origin is listed in LOCI_GATEWAY_ORIGINS
//     (comma-separated, exactly as the browser sends it, e.g. https://chat.example.com),
//     or when no web site can send it: an http(s) origin on a loopback name (a page
//     served from her own machine) or a scheme that is not http(s) (desktop shells such
//     as tauri://localhost or app://…). Everything else is refused, `null` included —
//     a sandboxed iframe or a data: page sends `null`, so it is only let in when listed.
//   · A request with no Origin whose Sec-Fetch-Site says cross-site or same-site, and
//     that is not GET / HEAD / OPTIONS, is a browser that left the Origin out: refused.
// Refused → 403 with the reason, and one console line naming the origin or host so the
// owner can see what to list. Nothing behind the door has seen the request.
// ============================================================

const crypto = require("crypto");
const net = require("net");

const DEFAULT_BIND = "127.0.0.1";
const PASSPHRASE = /^[A-Za-z0-9._~-]{16,128}$/;

function is_loopback(bind) {
  const b = String(bind || "").trim().replace(/^\[|\]$/g, "").toLowerCase();
  if (b === "localhost") return true;
  if (net.isIPv4(b)) return b.startsWith("127.");
  if (net.isIPv6(b)) {
    if (b === "::1") return true;
    const mapped = /^::ffff:(\d+\.\d+\.\d+\.\d+)$/.exec(b);
    return Boolean(mapped && mapped[1].startsWith("127."));
  }
  return false;
}

/** How the address goes into a URL (an IPv6 literal in brackets). */
function url_host(bind) {
  const b = String(bind).replace(/^\[|\]$/g, "");
  return net.isIPv6(b) ? `[${b}]` : b;
}

const digest = (s) => crypto.createHash("sha256").update(String(s), "utf8").digest();

/**
 * @returns { bind, loopback, passphrase, required } or { error }
 */
function check_door(env = process.env) {
  const bind = String(env.LOCI_GATEWAY_BIND || "").trim() || DEFAULT_BIND;
  const passphrase = String(env.LOCI_GATEWAY_PASSPHRASE || "").trim();
  const loopback = is_loopback(bind);
  if (passphrase && !PASSPHRASE.test(passphrase)) {
    return { error: "LOCI_GATEWAY_PASSPHRASE must be 16–128 characters of letters, digits and . _ ~ -" };
  }
  if (!loopback && !passphrase) {
    return { error: `LOCI_GATEWAY_BIND=${bind} is not a loopback address: set LOCI_GATEWAY_PASSPHRASE `
      + "(16+ characters), and every path will need /<passphrase>/ in front — otherwise anyone who can reach "
      + "this port can read what the cards carry" };
  }
  return { bind, loopback, passphrase, required: !loopback };
}

/**
 * @returns admit(url) → the url with the passphrase segment taken off, or null (refuse with 404)
 */
function create_door({ passphrase, required }) {
  const want = passphrase ? digest(passphrase) : null;
  return function admit(url) {
    const raw = String(url || "/");
    if (want) {
      const m = /^\/([^/?#]+)(.*)$/.exec(raw);
      if (m && crypto.timingSafeEqual(digest(m[1]), want)) {
        const rest = m[2];
        return rest.startsWith("/") ? rest : `/${rest}`;
      }
    }
    return required ? null : raw;
  };
}

/** A host name (no port, no brackets) no web site can own: loopback literals and *.localhost. */
function is_loopback_name(name) {
  const n = String(name || "").trim().toLowerCase().replace(/\.$/, "");
  return is_loopback(n) || n.endsWith(".localhost");
}

/** The name part of a Host header: "[::1]:3100" → "::1", "localhost:3100" → "localhost". */
function host_name(header) {
  const h = String(header || "").trim().toLowerCase();
  if (h.startsWith("[")) return h.slice(1, h.indexOf("]") > 0 ? h.indexOf("]") : undefined);
  const colon = h.lastIndexOf(":");
  return colon >= 0 && h.indexOf(":") === colon ? h.slice(0, colon) : h;
}

const list_of = (value) => String(value || "").split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);

/**
 * @param env       LOCI_GATEWAY_ORIGINS · LOCI_GATEWAY_HOSTS
 * @param loopback  whether the gateway is bound to a loopback address (check_door)
 * @returns check(req) → null (let in) or { status: 403, error, why }
 */
function create_caller_check({ env = process.env, loopback = true } = {}) {
  const origins = new Set(list_of(env.LOCI_GATEWAY_ORIGINS).map((o) => o.toLowerCase().replace(/\/+$/, "")));
  const hosts = new Set(list_of(env.LOCI_GATEWAY_HOSTS).map((h) => host_name(h)));
  const refuse = (why, error) => ({ status: 403, why, error });
  return function check(req) {
    const headers = req.headers || {};
    if (loopback && headers.host !== undefined) {
      const name = host_name(headers.host);
      if (!is_loopback_name(name) && !hosts.has(name)) {
        return refuse(`host ${name}`, `这个请求是寄给「${name}」的，不是本机的名字，网关不接（防 DNS 重绑定）。`
          + "要用别的名字访问网关（hosts 文件里自己起的名、本机反向代理），把它加进 LOCI_GATEWAY_HOSTS。");
      }
    }
    const raw = headers.origin;
    if (raw !== undefined) {
      const origin = String(raw).trim().toLowerCase().replace(/\/+$/, "");
      if (origins.has(origin)) return null;
      let url = null;
      if (origin !== "null") { try { url = new URL(origin); } catch { url = null; } }
      const web = url && (url.protocol === "http:" || url.protocol === "https:");
      if (url && (!web || is_loopback_name(url.hostname.replace(/^\[|\]$/g, "")))) return null;
      return refuse(`origin ${origin}`, `这个请求来自网页「${origin}」，它不在 LOCI_GATEWAY_ORIGINS 里，网关不接。`
        + "浏览器里的聊天客户端要先把自己的地址（浏览器地址栏里的 协议://域名:端口）加进 LOCI_GATEWAY_ORIGINS。");
    }
    const site = String(headers["sec-fetch-site"] || "").toLowerCase();
    if ((site === "cross-site" || site === "same-site") && !["GET", "HEAD", "OPTIONS"].includes(req.method)) {
      return refuse(`sec-fetch-site ${site}`, "这个请求来自别的网页（浏览器没带 Origin），网关不接。");
    }
    return null;
  };
}

module.exports = { check_door, create_door, create_caller_check, is_loopback, is_loopback_name, url_host, DEFAULT_BIND };
