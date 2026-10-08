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

module.exports = { check_door, create_door, is_loopback, url_host, DEFAULT_BIND };
