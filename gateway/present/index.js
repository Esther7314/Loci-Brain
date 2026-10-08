// ============================================================
// gateway/present/index.js — the present layer as the relay sees it
//
// server.js builds this once and hands it to the relay, which calls two hooks:
//   on_request({ route, body })   a chat request on /v1/*, with the client's messages
//                                 exactly as sent — before any poke edits them and before
//                                 anything goes upstream. The owner's new line is written
//                                 here ("store before forwarding").
//   on_response(turn, { status, headers, stream })
//                                 the upstream answer as it streams to the client; once it
//                                 has finished, the reply is written after the turn.
// Nothing here can block the chat: a failure is reported on the console line and the
// request carries on.
//
// Where the files go:
//   · day files (what was said):  <LOCI_GATEWAY_DAYS>/days/<YYYY-MM-DD>.jsonl
//       LOCI_GATEWAY_DAYS is the gateway's host folder beside the Loci library,
//       <buckets>/_hosts/<LOCI_GATEWAY_NAME>/, so Loci's export carries the day files
//       with it. When it is unset, the default is <LOCI_GATEWAY_DATA>/host/ — inside the
//       gateway's own data folder, never inside a Loci library it was not pointed at.
//       The startup banner prints which one is in use.
//   · thread ledger (private):    <LOCI_GATEWAY_DATA>/threads/<thread>.json
// ============================================================

const path = require("path");
const { create_clock, resolve_zone } = require("./clock.js");
const { create_day_store } = require("./day_store.js");
const { create_threads } = require("./threads.js");
const { capture_reply } = require("./reply_capture.js");

function create_present({ env = process.env, data_root, clock = create_clock(env), log = console.error }) {
  const { zone, note: zone_note } = resolve_zone(env);
  const host_dir = env.LOCI_GATEWAY_DAYS
    ? path.resolve(env.LOCI_GATEWAY_DAYS)
    : path.join(data_root, "host");
  const day_store = create_day_store({ dir: path.join(host_dir, "days"), clock, zone });
  const threads = create_threads({ dir: path.join(data_root, "threads"), day_store, clock });

  function on_request({ body }) {
    const seen = threads.ingest(body.messages);
    if (seen.kind === "ignored") return { turn: null, note: "present: not a turn" };
    const bits = [`present ${seen.thread}`];
    if (seen.resend) bits.push("resend");
    if (seen.kind === "continuation") bits.push("tool turn");
    if (seen.forked_from) bits.push(`forked from ${seen.forked_from}`);
    if (seen.wrote.length) bits.push(`+${seen.wrote.join(",")}`);
    if (seen.revised.length) bits.push(`rev ${seen.revised.join(",")}`);
    return { turn: seen, note: bits.join(" ") };
  }

  function on_response(seen, { status, headers, stream }) {
    if (status < 200 || status >= 300) return;
    capture_reply(stream, headers, (reply) => {
      try {
        const done = threads.record_reply(seen, reply);
        if (done.wrote) {
          console.log(`[gateway] present ${done.thread} reply +${done.wrote}`
            + (done.replaced.length ? ` replaced ${done.replaced.join(",")}` : "")
            + (done.forked_from ? ` forked from ${done.forked_from}` : ""));
        }
      } catch (err) { log(`[gateway] present: writing the reply failed: ${err?.message || err}`); }
    });
  }

  function banner_lines() {
    const lines = [
      `day files      ${day_store.dir}${env.LOCI_GATEWAY_DAYS ? "" : "   (LOCI_GATEWAY_DAYS unset — set it to <buckets>/_hosts/<name> so Loci's export carries them)"}`,
      `local day      ${zone}${clock.source === "system clock" ? "" : `   · ${clock.source}`}`,
    ];
    if (zone_note) lines.push(`⚠️ ${zone_note}`);
    return lines;
  }

  return {
    on_request,
    on_response,
    banner_lines,
    heartbeat_tasks: [],
    day_store,
    threads,
  };
}

module.exports = { create_present };
