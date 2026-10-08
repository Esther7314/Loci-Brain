// ============================================================
// gateway/present/index.js — the present layer as the relay sees it
//
// server.js builds this once and hands it to the relay, which calls two hooks per chat
// request on /v1/*:
//
//   prepare({ body, headers, request_id }) → { body, ctx, note, notes }
//       Sees the client's messages exactly as sent, before anything goes upstream, and
//       builds the copy that does go upstream. In this order:
//         ① threads: which conversation, what is new; the owner's new line is written
//            to the day store right here ("store before forwarding")
//         ② a new line of hers (not a resend, not a tool turn) → poke delivery (dreams /
//            muse, behind its idle gate) and Loci's cue, side by side, each with its own
//            timeout; what they hand back becomes an overlay on her line (window.js)
//         ③ assembly: client system + carry + lines from the mark on, overlays replayed
//         ④ a streamed request asks for usage (stream_filter.js)
//       `body: null` means "forward the client's body as it is".
//   on_response(ctx, { status, headers, stream }) → the stream to pipe to the client, or null
//       Once upstream accepted the request (2xx): the cards placed in it are confirmed to
//       Loci (/cue/delivered, once per turn), and the answer is read as it passes: the
//       reply is written after the turn when it finishes, and its usage becomes the fill.
//       When the gateway asked for usage on the client's behalf, the client gets a stream
//       with the usage chunk taken out; otherwise null (pipe upstream's stream as it is).
//
// Nothing here can block the chat: Loci down or slow costs this turn its card or dream
// (cue 3 s, poke 8 s), a hook that throws costs the turn its present work, and the relay
// forwards either way.
//
// Where the files go:
//   · day files (what was said):  <LOCI_GATEWAY_DAYS>/days/<YYYY-MM-DD>.jsonl
//       LOCI_GATEWAY_DAYS is the gateway's host folder beside the Loci library,
//       <buckets>/_hosts/<LOCI_GATEWAY_NAME>/, so Loci's export carries the day files
//       with it. When it is unset, the default is <LOCI_GATEWAY_DATA>/host/ — inside the
//       gateway's own data folder, never inside a Loci library it was not pointed at.
//       The startup banner prints which one is in use.
//   · thread ledger (private):    <LOCI_GATEWAY_DATA>/threads/<thread>.json — the branch,
//       and the window (mark, 🔴 carry, overlays, fill). Never exported, never served.
//   · window sizes (private):     <LOCI_GATEWAY_DATA>/context_windows.json
//   · settings and edited prompt cards: <LOCI_GATEWAY_DATA>/present.json · prompts.json
//     (settings.js · prompts.js), read fresh by whoever needs them
// ============================================================

const path = require("path");
const poke = require("../poke_delivery.js");
const { create_clock, resolve_zone } = require("./clock.js");
const { create_day_store } = require("./day_store.js");
const { create_threads, to_said } = require("./threads.js");
const { capture_reply } = require("./reply_capture.js");
const { create_settings } = require("./settings.js");
const { create_prompts } = require("./prompts.js");
const { create_cue, DEFAULT_TIMEOUT_MS: CUE_TIMEOUT_MS } = require("./cue.js");
const { create_context_windows } = require("./context_window.js");
const { with_usage, create_usage_strip } = require("./stream_filter.js");
const { estimate_prompt, measure } = require("./fill.js");
const win = require("./window.js");

const POKE_TIMEOUT_MS = 8000;

/**
 * @param env                     process.env (LOCI_TZ, LOCI_GATEWAY_DAYS, LOCI_HOOK_TOKEN, LOCI_CUE_TIMEOUT_MS …)
 * @param data_root               LOCI_GATEWAY_DATA
 * @param upstream                upstream base URL (for the provider's model list)
 * @param loci                    Loci's MCP address; its REST routes hang off the same root
 * @param idle_threshold_minutes  poke delivery's idle gate
 * @param log_path                memory-actions.jsonl (shared with health.js)
 * @param read_settings           () → present.json as an object; settings.js when not given
 */
function create_present({
  env = process.env, data_root, clock = create_clock(env), log = console.error,
  upstream = "", loci = poke.DEFAULT_ADDRESS, idle_threshold_minutes = poke.DEFAULT_IDLE_MINUTES,
  log_path = path.join(data_root, "logs", "memory-actions.jsonl"), read_settings = null,
}) {
  const { zone, note: zone_note } = resolve_zone(env);
  const host_dir = env.LOCI_GATEWAY_DAYS
    ? path.resolve(env.LOCI_GATEWAY_DAYS)
    : path.join(data_root, "host");
  const day_store = create_day_store({ dir: path.join(host_dir, "days"), clock, zone });
  const threads = create_threads({ dir: path.join(data_root, "threads"), day_store, clock });
  const settings = create_settings({ file: path.join(data_root, "present.json"), env });
  const prompts = create_prompts({ file: path.join(data_root, "prompts.json"), clock, zone });
  const name = String(env.LOCI_GATEWAY_NAME || "").trim() || "gateway";
  const asked_timeout = Number(env.LOCI_CUE_TIMEOUT_MS);
  const cue_timeout_ms = Number.isFinite(asked_timeout) && asked_timeout > 0 ? asked_timeout : CUE_TIMEOUT_MS;
  const cue = create_cue({ address: loci, hook_token: String(env.LOCI_HOOK_TOKEN || "").trim(),
                           timeout_ms: cue_timeout_ms, log_path });
  const windows = create_context_windows({ data_root, upstream, clock, log,
    read_settings: read_settings || (() => settings.load().values) });
  const poke_state = path.join(data_root, "state", "poke-window.json");
  const delivering = new Set();

  /** Poke delivery decides whether and what; the window decides where. */
  async function ask_poke(request_id) {
    const scratch = [];
    const d = await poke.attach_once({
      messages: scratch, requestId: request_id, statePath: poke_state, logPath: log_path,
      地址: loci, 闲时阈值分钟: idle_threshold_minutes, 超时毫秒: POKE_TIMEOUT_MS,
    });
    const note = d.patchInjected ? `戳戳(梦=${d.hasDream} 发呆=${d.musePending})`
      : d.calledLoci ? "戳戳无" : "戳戳没问(不够闲)";
    return { text: d.patchInjected && scratch[0] ? String(scratch[0].content) : "", note };
  }

  async function prepare({ body, headers = {}, request_id = null }) {
    const seen = threads.ingest(body.messages);
    if (seen.kind === "ignored") return { body: null, ctx: null, note: "present: not a turn", notes: [] };

    const bits = [`present ${seen.thread}`];
    if (seen.resend) bits.push("resend");
    if (seen.kind === "continuation") bits.push("tool turn");
    if (seen.forked_from) bits.push(`forked from ${seen.forked_from}`);
    if (seen.wrote.length) bits.push(`+${seen.wrote.join(",")}`);
    if (seen.revised.length) bits.push(`rev ${seen.revised.join(",")}`);

    let thread = threads.get(seen.thread);
    if (seen.forked_from) win.inherit(thread, threads.get(seen.forked_from), clock.now());
    let w = win.ensure(thread, clock.now());
    if (body.model) w.model = String(body.model);
    const turn = seen.kind === "turn" ? seen.turn : null;
    const window_name = w.name;

    // ② only a line of hers that is new in this request opens the gates: a resend or a
    //    regenerate is the same turn and replays what that turn already got
    const notes = [];
    const fresh_line = Boolean(turn) && seen.wrote.includes(turn);
    const own_text = turn ? to_said(body.messages[body.messages.length - 1]).text : "";
    const jobs = [];
    jobs.push(fresh_line ? ask_poke(request_id).catch((err) => ({ text: "", note: "戳戳炸:" + (err?.message || err) }))
      : Promise.resolve(null));
    const need_cue = Boolean(turn) && !win.overlay_for(w, "cue", turn);
    jobs.push(need_cue ? cue.ask({ text: own_text, window: window_name, turn, request_id }) : Promise.resolve(null));
    const [poked, cued] = await Promise.all(jobs);

    // the thread may have moved on while Loci was asked (another request): read it again
    thread = threads.get(seen.thread);
    w = win.ensure(thread, clock.now());
    if (poked) {
      notes.push(poked.note);
      if (poked.text && w.name === window_name && !win.overlay_for(w, "poke", turn)) {
        win.add_overlay(w, { kind: "poke", anchor: turn, place: "before", text: poked.text, turn });
      }
    }
    if (cued) {
      if (!cued.ok) notes.push("卡炸:" + cued.error);
      else notes.push(cued.text.trim() ? `卡${cued.cards.length}张` : "卡无");
      if (cued.ok && w.name === window_name && !win.overlay_for(w, "cue", turn)) {
        win.add_overlay(w, { kind: "cue", anchor: turn, place: "after", text: cued.text, turn,
                             cards: cued.cards, delivered: !cued.text.trim() });
      }
    } else if (turn) notes.push("卡重放");

    // ③ assembly — ④ usage
    const ids = win.map_ids(body.messages, thread.branch, seen.cursor);
    const built = win.assemble(body.messages, ids, w);
    if (built.mark === "kept") bits.push(`window ${w.name} from ${w.mark}${built.carried ? " + carry" : ""}`);
    if (built.mark === "void") bits.push(`mark ${w.mark} not in history: as sent`);
    const { body: forward, strip } = with_usage({ ...body, messages: built.messages });
    threads.save(thread.id);
    windows.ensure(body.model, headers);

    const ctx = {
      seen, thread: thread.id, window: w.name, strip, model: body.model ? String(body.model) : null,
      estimate: estimate_prompt(forward),
      deliver: built.replayed.filter((o) => o.kind === "cue" && !o.delivered).map((o) => o.turn),
    };
    return { body: forward, ctx, note: bits.join(" "), notes };
  }

  function confirm_delivered(ctx) {
    const current = threads.get(ctx.thread)?.window;
    for (const turn of ctx.deliver) {
      const key = `${ctx.window}|${turn}`;
      if (delivering.has(key)) continue;
      if (current && current.name === ctx.window && win.overlay_for(current, "cue", turn)?.delivered) continue;
      delivering.add(key);
      cue.delivered({ window: ctx.window, turn }).then((r) => {
        if (!r.ok) return;   // stays undelivered: the next turn that replays the card says it again
        for (const t of threads.list()) {
          const o = t.window && t.window.name === ctx.window ? win.overlay_for(t.window, "cue", turn) : null;
          if (o && !o.delivered) { o.delivered = true; threads.save(t.id); }
        }
      }).catch((err) => log(`[gateway] present: /cue/delivered failed: ${err?.message || err}`))
        .finally(() => delivering.delete(key));
    }
  }

  function record_usage(thread_id, ctx, usage) {
    const thread = threads.get(thread_id);
    if (!thread || !thread.window || thread.window.name !== ctx.window) return;
    thread.window.usage = measure({ usage, estimate: ctx.estimate, window: windows.resolve(ctx.model),
                                    model: ctx.model, at: clock.now() });
    threads.save(thread.id);
  }

  function on_response(ctx, { status, headers, stream }) {
    if (!ctx || status < 200 || status >= 300) return null;
    confirm_delivered(ctx);
    capture_reply(stream, headers, (reply) => {
      let thread_id = ctx.thread;
      try {
        const done = threads.record_reply(ctx.seen, reply);
        thread_id = done.thread;
        if (done.forked_from) {
          const child = threads.get(done.thread);
          win.inherit(child, threads.get(done.forked_from), clock.now());
          if (child) threads.save(child.id);
        }
        if (done.wrote) {
          console.log(`[gateway] present ${done.thread} reply +${done.wrote}`
            + (done.replaced.length ? ` replaced ${done.replaced.join(",")}` : "")
            + (done.forked_from ? ` forked from ${done.forked_from}` : ""));
        }
      } catch (err) { log(`[gateway] present: writing the reply failed: ${err?.message || err}`); }
      try { record_usage(thread_id, ctx, reply.usage); }
      catch (err) { log(`[gateway] present: recording usage failed: ${err?.message || err}`); }
    });
    const is_sse = /text\/event-stream/i.test(String(headers?.["content-type"] || ""));
    return ctx.strip && is_sse ? stream.pipe(create_usage_strip()) : null;
  }

  /** What the panel may see of a window: numbers and names, never the carry or an overlay's text. */
  function window_status(thread_id) {
    const thread = thread_id ? threads.get(thread_id)
      : threads.list().slice().sort((a, b) => b.last_at - a.last_at)[0] || null;
    if (!thread) return null;
    const w = thread.window || null;
    const u = w?.usage || null;
    const size = windows.resolve(u?.model || w?.model || null);
    return {
      thread: thread.id,
      window: w ? w.name : null,
      window_no: w ? w.no : null,
      mark: w ? w.mark : null,
      has_carry: Boolean(w && w.carry),
      model: w ? w.model : null,
      used_tokens: u ? u.prompt_tokens : null,
      estimated: u ? u.estimated : null,
      fill_pct: u ? u.fill_pct : null,
      context_tokens: u ? u.context_tokens : size.tokens,
      context_source: u ? u.context_source : size.source,
    };
  }

  function banner_lines() {
    const lines = [
      `day files      ${day_store.dir}${env.LOCI_GATEWAY_DAYS ? "" : "   (LOCI_GATEWAY_DAYS unset — set it to <buckets>/_hosts/<name> so Loci's export carries them)"}`,
      `local day      ${zone}${clock.source === "system clock" ? "" : `   · ${clock.source}`}`,
      // a number that shows a misconfiguration at a glance belongs on the first screen
      `cue timeout    ${cue_timeout_ms} ms${env.LOCI_CUE_TIMEOUT_MS ? "" : "   (LOCI_CUE_TIMEOUT_MS unset — the default)"}`,
    ];
    if (zone_note) lines.push(`⚠️ ${zone_note}`);
    return lines;
  }

  return {
    prepare,
    on_response,
    banner_lines,
    window_status,
    heartbeat_tasks: [],
    cue_timeout_ms,
    day_store,
    threads,
    settings,
    prompts,
    clock,
    zone,
    name,
    context_windows: windows,
    cue,
  };
}

module.exports = { create_present };
