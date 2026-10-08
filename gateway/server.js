// ============================================================
// gateway/server.js — the mini gateway that sits in front of Loci
//
// **What it does**: your client posts a chat request here, this layer forwards it to
// the real model, and on the way it puts what the AI ought to know into this round's
// messages — without changing a byte of the client's own history.
//
// Every chat request that reaches it is the owner talking: the client points a provider
// of its own at the gateway, so there is no tag to look for (present/threads.js).
//
// What goes in, and where (present/window.js keeps them as overlays on the owner's line
// and replays each one at the same place, with the same bytes, every later turn until
// the window changes — the prompt cache and Loci's "delivered to this window" both
// depend on that):
//   · Loci's cards (present/cue.js)      right **after** her line: asked with her
//                                         message, at most three, confirmed to Loci once
//                                         upstream accepted the turn
//   · dreams / muse (poke_delivery.js)   right **before** her line, only on the first
//                                         line back after a long silence (the idle gate)
//   · a fill reminder (compress.js)      at the true tail, once per line per window
//   · what he said while she was away    right **before** her line, as his own words,
//     (away.js)                           until her history shows them
// and the window itself: past a mark, the carry stands in for the older lines.
// The gateway's own turns (wake, the day report, forced packing) go upstream through
// present/own_turn.js, on the one heartbeat below.
//
// ⛔ **This shell does not call breath() on the AI's behalf.** auto_attach.js also
//    carries a function that pastes a whole breath() into the system prompt on the
//    first turn of a window (`attach_once`), and here it is **deliberately left
//    unwired**: "breathe before you speak" is a hand **the AI has to reach out with
//    itself**, and it lives in the system prompt (docs/系统提示-英文.md, or
//    docs/系统提示-中文.md for the Chinese version). Have the
//    gateway paste it in and the AI is no longer "remembering to open its eyes" —
//    it is "being handed a summary". Those are two entirely different things.
//    If you do want the gateway to do it for you, the function is right there in the
//    module: wire it up in one line.
//
// 🔴 Three boundaries (do not lose them when you edit):
//   · **A failure never blocks the chat.** Any poke that comes up empty — a timeout,
//     Loci not running, a reply that is not JSON — still forwards as usual and writes
//     one log line. Better to miss an attachment this round than to stall a human
//     conversation.
//   · **The gateway never writes a memory itself.** A chat turn touches cue (a read,
//     plus the delivered / dropped acknowledgements that only keep Loci's ledger of what
//     a window holds) and poke / dream.wake (a read-only endpoint plus an idempotent
//     signal). The nightly hand-off gives Loci the day's lines as slices waiting for the
//     model (/api/v2/slices), not as memories. In the gateway's own turns the model may
//     call Loci's tools, and whatever is written then is the model's doing, keyed by
//     Loci-Turn.
//   · **The judgement stays with the AI.** A card carries text by design — what matched
//     and what is still open — and whether it happened, whether to act on it, is the
//     model's to decide after reading. (Counts only, never text, is auto_attach.js's
//     rule for its relevance line; this shell does not run that module.)
//
// Zero dependencies: only Node's built-in http / fetch (Node 18+).
//
// Run:
//     LOCI_UPSTREAM=https://api.deepseek.com/v1 node gateway/server.js
// then point the client's base_url at http://127.0.0.1:3100/v1.
// The API key still comes from the client — this layer forwards it verbatim and
// **neither stores nor reads it**.
//
// 📐 **This file is a mount table**: config read from env, the modules built from it,
//    a list of routes → handlers, one heartbeat, and the startup lines. Logic does not
//    live here: /health is health.js, the door (bind address and passphrase) is door.js,
//    one gateway per data directory (gateway.lock) is lock.js,
//    /present/* and /loci/source are present/present_api.js and present/source_api.js
//    behind LOCI_GATEWAY_TOKEN, everything else is relay.js, and the present layer (day
//    store, threads, the window and its overlays, cue, compression and packing, the
//    nightly report, wake, away and push, settings, prompt cards) is gateway/present/.
//    If this file grows, the lines that grew must be mounts.
//    Setup, every env var and the common errors: gateway/README.md.
// ============================================================

const http = require("http");
const path = require("path");
const poke = require("./poke_delivery.js");
const { handle_health } = require("./health.js");
const { create_relay } = require("./relay.js");
const { create_present } = require("./present/index.js");
const { create_heartbeat } = require("./present/heartbeat.js");
const { check_door, create_door, url_host } = require("./door.js");
const { acquire_lock, refusal_message, release_on_exit } = require("./lock.js");
const { behind_token, send_json } = require("./present/http_io.js");
const { create_present_api } = require("./present/present_api.js");
const { create_source_api } = require("./present/source_api.js");
const { build_present_health } = require("./present/health_section.js");

// ———— Config: read once at startup; changing any of it means a restart ————
const port = Number(process.env.PORT || 3100);
const upstream = (process.env.LOCI_UPSTREAM || "").replace(/\/+$/, "");
const LOCI = process.env.LOCI_MCP || poke.DEFAULT_ADDRESS;
const idle_threshold_minutes = Number(process.env.POKE_IDLE_MINUTES || poke.DEFAULT_IDLE_MINUTES);
const data_root = process.env.LOCI_GATEWAY_DATA || path.join(__dirname, "data");
// Yesterday reaches the next window as the carry (present/window.js: the day report and
// the summary he wrote), never as a recall(when="yesterday") pasted in: it is the same
// few sentences, and a second way of doing one job is a road with no entrance.
const log_path = path.join(data_root, "logs", "memory-actions.jsonl");
// Loci → gateway: the key Loci presents on /present/* and /loci/source. Unset = both closed
// (/present/* 404; /loci/source 503, which Loci reads as UNAVAILABLE — see present/http_io.js).
const gateway_token = String(process.env.LOCI_GATEWAY_TOKEN || "").trim();
const door_config = check_door(process.env);

if (!upstream) {
  console.error("LOCI_UPSTREAM is not set — there is nothing to forward requests to.");
  console.error("例：LOCI_UPSTREAM=https://api.deepseek.com/v1 node gateway/server.js");
  process.exit(1);
}
if (door_config.error) {
  console.error(door_config.error);
  process.exit(1);
}
// Before anything reads or writes the data directory: a second gateway on it never starts.
const lock = acquire_lock(data_root);
if (!lock.ok) {
  console.error(refusal_message(data_root, lock));
  process.exit(1);
}
release_on_exit(lock.file);

// ———— Modules ————
const present = create_present({ env: process.env, data_root, upstream, loci: LOCI, idle_threshold_minutes, log_path });
const relay = create_relay({ upstream, present });
const heartbeat = create_heartbeat({ tasks: present.heartbeat_tasks });
const admit = create_door(door_config);
const present_api = behind_token(gateway_token, create_present_api(present));
const source_api = behind_token(gateway_token, create_source_api(present), { closed_status: 503 });
const present_health = () => build_present_health({ present, doors: {
  token_set: Boolean(gateway_token), bind: door_config.bind, passphrase_required: door_config.required } });

// ———— Routes: first match wins ————
// /health goes first: nothing below should be able to affect it, and it should affect
// nothing below. Everything else is the relay, which keeps its own /v1/* guard.
const routes = [
  { match: (req, route) => req.method === "GET" && route === "/health",
    handle: (req, res) => handle_health(req, res, { log_path, cue_timeout_ms: present.cue_timeout_ms, present_health }) },
  { match: (req, route) => route === "/present" || route.startsWith("/present/"), handle: present_api },
  { match: (req, route) => route === "/loci/source", handle: source_api },
  { match: () => true, handle: relay },
];

const server = http.createServer(async (req, res) => {
  const start = Date.now();
  // The passphrase segment (door.js) comes off first, so nothing below ever sees it.
  const inside = admit(req.url);
  if (inside === null) {
    req.resume();
    console.log(`[gateway] ${req.method} → 404 (no passphrase)`);
    return send_json(res, 404, { error: "not found" });
  }
  req.url = inside;
  const route = req.url.split("?")[0];
  const mounted = routes.find((r) => r.match(req, route));
  return mounted.handle(req, res, { start });
});

// ———— Startup ————
server.listen(port, door_config.bind, () => {
  // 🔴 Report what was **actually bound**, not what was asked for. `PORT=0` is a legal
  //    setting — it means "you pick" — and until this line read the real port back, the
  //    banner answered with a literal 0 while the server sat on some other number. The
  //    first line of the banner is how everything downstream finds this process (the
  //    tests parse it), so a banner that lies is not cosmetic.
  const bound = server.address().port;
  const here = `http://${url_host(door_config.bind)}:${bound}${door_config.required ? "/<LOCI_GATEWAY_PASSPHRASE>" : ""}`;
  console.log(`[gateway] up on http://${url_host(door_config.bind)}:${bound}`);
  console.log(`[gateway] upstream       ${upstream}`);
  console.log(`[gateway] Loci           ${poke._internal.httpBase(LOCI)}`);
  console.log(`[gateway] idle threshold ${idle_threshold_minutes} min`);
  console.log(`[gateway] data lock      ${lock.file}${lock.took_over ? ` (taken over from pid ${lock.took_over}, no longer running)` : ""}`);
  for (const line of present.banner_lines()) console.log(`[gateway] ${line}`);
  console.log(`[gateway] present api    ${gateway_token ? "/present/* and /loci/source open to Bearer LOCI_GATEWAY_TOKEN"
    : "closed — LOCI_GATEWAY_TOKEN unset, so /present/* answers 404 and /loci/source 503"}`);
  if (door_config.required) console.log("[gateway] passphrase     not a loopback bind: every path needs /<LOCI_GATEWAY_PASSPHRASE>/ in front");
  console.log(`[gateway] is it working  GET ${here}/health`);
  console.log(`[gateway] point your client base_url at ${here}/v1`);
  heartbeat.start();
});
