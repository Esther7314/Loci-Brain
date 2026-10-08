// ============================================================
// gateway/server.js — the mini gateway that sits in front of Loci
//
// **What it does**: your client posts a chat request here, this layer forwards it to
// the real model, and on the way it pokes Loci twice to put what the AI ought to know
// into this round's messages.
//
// Two pokes, landing in two different places — do not mix them up:
//   · poke delivery (poke_delivery.js)     dreams / muse, pinned in the stable prefix
//   · relevance reminder (auto_attach.js)  **pinned at the true tail** — after the
//                                          latest user message, at the very end of the
//                                          whole messages array
//
// 🔴 Why the reminder has to go on last: its position has to be "as close as possible
//    to the moment the model speaks". So build_relevance_notice() never touches
//    messages itself — it only computes the patch and hands it back, and
//    attach_at_true_tail() runs as the last step, once everything else is inserted and
//    the request body is assembled. Get the order wrong and the position is wrong.
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
// 🔴 Three boundaries (shared with both modules — do not lose them when you edit):
//   · **A failure never blocks the chat.** Any poke that comes up empty — a timeout,
//     Loci not running, a reply that is not JSON — still forwards as usual and writes
//     one log line. Better to miss an attachment this round than to stall a human
//     conversation.
//   · **Nearly all reads, barely any writes.** It touches breath / recall (reads) and
//     poke / dream.wake (a read-only endpoint plus an idempotent signal).
//     ⛔ It never modifies a memory.
//   · **It reports that something exists, never what it says.** What the reminder
//     inserts is a **count** — "there are N relevant memories" — and the judgement is
//     left to the AI itself.
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
//    live here: /health is health.js, everything else is relay.js, and the present
//    layer (day store, threads, and later the window, the nightly report, wake and
//    push) is gateway/present/. If this file grows, the lines that grew must be mounts.
// ============================================================

const http = require("http");
const path = require("path");
const auto = require("./auto_attach.js");
const poke = require("./poke_delivery.js");
const { handle_health } = require("./health.js");
const { create_relay } = require("./relay.js");
const { create_present } = require("./present/index.js");
const { create_heartbeat } = require("./present/heartbeat.js");

// ———— Config: read once at startup; changing any of it means a restart ————
const port = Number(process.env.PORT || 3100);
const upstream = (process.env.LOCI_UPSTREAM || "").replace(/\/+$/, "");
const LOCI = process.env.LOCI_MCP || poke.DEFAULT_ADDRESS;
const idle_threshold_minutes = Number(process.env.POKE_IDLE_MINUTES || poke.DEFAULT_IDLE_MINUTES);
const min_score = Number(process.env.RELEVANCE_MIN_SCORE || auto.DEFAULT_MIN_SCORE);
const data_root = process.env.LOCI_GATEWAY_DATA || path.join(__dirname, "data");
// ⚰️ **The "recent memory view" was pulled out wholesale.**
//    What it did: on the first turn of the next day's window, paste excerpts of
//    recall(when="yesterday") into the context. But "yesterday's memory" never needed
//    a trip to Loci — **it is exactly the few sentences squeezed out when the previous
//    window closed**, and carrying those into the next window is the whole job. Asking
//    Loci a second time only buys a second way of doing the same thing.
//    The idea is kept in gateway/README.md section four (closing-window compression);
//    the code is not. Something the docs never mention but the code still runs is a
//    road with no entrance.
const log_path = path.join(data_root, "logs", "memory-actions.jsonl");

if (!upstream) {
  console.error("LOCI_UPSTREAM is not set — there is nothing to forward requests to.");
  console.error("例：LOCI_UPSTREAM=https://api.deepseek.com/v1 node gateway/server.js");
  process.exit(1);
}

// ———— Modules ————
const present = create_present({ env: process.env, data_root });
const relay = create_relay({ upstream, loci: LOCI, idle_threshold_minutes, min_score, data_root, log_path, present });
const heartbeat = create_heartbeat({ tasks: present.heartbeat_tasks });

// ———— Routes: first match wins ————
// /health goes first: nothing below should be able to affect it, and it should affect
// nothing below. Everything else is the relay, which keeps its own /v1/* guard.
const routes = [
  { match: (req, route) => req.method === "GET" && route === "/health",
    handle: (req, res) => handle_health(req, res, { log_path, min_score }) },
  { match: () => true, handle: relay },
];

const server = http.createServer(async (req, res) => {
  const start = Date.now();
  const route = req.url.split("?")[0];
  const mounted = routes.find((r) => r.match(req, route));
  return mounted.handle(req, res, { start });
});

// ———— Startup ————
server.listen(port, () => {
  // 🔴 Report what was **actually bound**, not what was asked for. `PORT=0` is a legal
  //    setting — it means "you pick" — and until this line read the real port back, the
  //    banner answered with a literal 0 while the server sat on some other number. The
  //    first line of the banner is how everything downstream finds this process (the
  //    tests parse it), so a banner that lies is not cosmetic.
  const bound = server.address().port;
  console.log(`[gateway] up on http://127.0.0.1:${bound}`);
  console.log(`[gateway] upstream       ${upstream}`);
  console.log(`[gateway] Loci           ${poke._internal.httpBase(LOCI)}`);
  console.log(`[gateway] score floor    ${min_score}   ·   idle threshold ${idle_threshold_minutes} min`);
  // 🔴 This line **is the one that used not to be printed**, and the "5 second timeout
  //    → never worked once since it shipped" bug would have been **visible on day one**
  //    had it been on the first screen at startup.
  //    📌 The rule: any number that lets someone spot a misconfiguration at a glance
  //    belongs on the first screen at startup.
  console.log(`[gateway] Loci timeout   ${process.env.RELEVANCE_TIMEOUT_MS || "(unset — using the default)"}`);
  for (const line of present.banner_lines()) console.log(`[gateway] ${line}`);
  console.log(`[gateway] is it working  GET http://127.0.0.1:${bound}/health`);
  console.log(`[gateway] point your client base_url at http://127.0.0.1:${bound}/v1`);
  heartbeat.start();
});
