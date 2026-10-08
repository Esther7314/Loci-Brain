// ============================================================
// gateway/present/index.js — the present layer as the relay sees it
//
// server.js builds this once and hands it to the relay, which calls three hooks per chat
// request on /v1/*:
//
//   prepare({ body, headers, request_id }) → { body, ctx, note, notes }
//       Sees the client's messages exactly as sent, before anything goes upstream, and
//       builds the copy that does go upstream. In this order:
//         ① threads: which conversation, what is new; the owner's new line is written
//            to the day store right here ("store before forwarding")
//         ①b a pack in flight for this conversation (pack.js): the turn waits for it, up
//            to PACK_WAIT_MS (LOCI_PACK_WAIT_MS), so it goes with the new window; past
//            that it goes as it is and the pack keeps going
//         ② a new line of hers (not a resend, not a tool turn) → poke delivery (dreams /
//            muse, behind its idle gate) and Loci's cue, side by side, each with its own
//            timeout; what they hand back becomes an overlay on her line (window.js)
//         ②b the last turn's fill crossed a reminder line not yet offered in this
//            window → his reminder goes at the tail of her line (compress.js)
//         ③ assembly: client system + carry + lines from the mark on, overlays replayed
//         ④ a streamed request asks for usage (stream_filter.js)
//       `body: null` means "forward the client's body as it is".
//   on_response(ctx, { status, headers, stream }) → the stream to pipe to the client, or null
//       Once upstream accepted the request (2xx): the cards placed in it are confirmed to
//       Loci (/cue/delivered, once per turn), and the answer is read as it passes: the
//       reply is written after the turn when it finishes (the text the client saw, the
//       summary block taken out), its usage becomes the fill, and a closed summary block
//       flips the window once the turn has ended (compress.js; Loci hears /cue/dropped).
//       A finished turn whose fill reached compress.force_pct asks for a pack (pack.js),
//       which starts at once in the background.
//   escape_wall(ctx, resp, send) → the Response to go on with
//       Upstream refused the turn (non-2xx): when it is the context-length wall, the
//       oldest lines after the mark are cut and the request sent once more (wall.js).
//   compress_now(thread?) · compress_health()
//       「现在压」 for /present/compress, and the compress part of /health (pack.js).
//       The client gets the answer through stream_filter.js: the summary block never
//       reaches it, and the usage chunk does not either when the gateway asked for it on
//       the client's behalf. null (non-2xx) = pipe upstream's answer as it is.
//   remember_owner({ headers, model })
//       Called once upstream accepted a chat request: its credential headers and model
//       become what the gateway's own turns borrow (own_turn.js; memory only).
//   owner_arrived()
//       Called first thing for every chat request: a wake in flight is aborted (wake.js).
//
// heartbeat_tasks is what server.js hangs on its one beat, in order: wake (wake.js).
// A finished answer also hands its request, as it went upstream, to wake.remember_turn:
// the snapshot a wake's prefix is copied from (private, in the thread ledger).
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
//   · own turns, packs and walls:  <LOCI_GATEWAY_DATA>/logs/present.jsonl (numbers, never text)
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
const { with_usage, create_sse_filter, create_json_filter, split_summary } = require("./stream_filter.js");
const { estimate_prompt, measure } = require("./fill.js");
const { create_own_turn } = require("./own_turn.js");
const { create_wake } = require("./wake.js");
const win = require("./window.js");
const compress = require("./compress.js");
const { create_packer, PACK_WAIT_MS } = require("./pack.js");
const { create_wall_escape } = require("./wall.js");
const { local_stamp } = require("./clock.js");

const HOW_WORDS = { self: "他自己压的", forced: "到了强制线", manual: "你按的", day: "日报换窗" };

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
  // the gateway's own paid turns; the relay hands it each accepted request's credential (memory only)
  const own_turn = create_own_turn({ env, data_root, upstream, loci_address: loci, clock, zone, log,
                                     read_own: () => settings.load().values.own });
  const wake = create_wake({ data_root, threads, day_store, settings, prompts, own_turn, clock, zone, log,
                             loci_address: loci, poke_state });

  // ———— forced / manual packing and the wall's way out (pack.js · wall.js) ————
  const read_compress = () => settings.load().values.compress;
  const asked_wait = Number(env.LOCI_PACK_WAIT_MS);
  const pack_wait_ms = Number.isFinite(asked_wait) && asked_wait >= 0 ? asked_wait : PACK_WAIT_MS;
  // the client's system messages per conversation, for the pack to write as himself: memory only
  const last_system = new Map();
  const packer = create_packer({
    threads, day_store, own_turn, prompts, read_compress, data_root, clock, zone, log,
    system_of: (id) => last_system.get(id) || [],
    on_flip: ({ old_name }) => cue.dropped({ window: old_name, all: true })
      .catch((err) => log(`[gateway] present: /cue/dropped failed: ${err?.message || err}`)),
  });
  const wall = create_wall_escape({ windows, threads, read_compress, data_root, clock, zone, log,
                                    request_pack: (id) => packer.request(id, "forced") });

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

    // ①b a pack in flight: wait for it, so this turn goes with the new window
    const waited = await packer.wait_for([seen.thread, seen.forked_from], pack_wait_ms);
    if (waited) bits.push(waited === "done" ? "waited for the pack" : `pack still running after ${pack_wait_ms} ms: as it is`);
    remember_system(seen.thread, body.messages);

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

    // the water level: a reminder line crossed and not yet offered in this window goes at
    // the tail of her line (compress.js); a resend replays the one already made
    if (turn && w.name === window_name) {
      const offered = compress.offer_reminder({ w, turn, values: settings.load().values.compress,
                                                card: () => prompts.current("compress") });
      if (offered) notes.push(`压缩提醒(${offered.line})`);
    }

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
      sent: forward,   // what goes upstream: wake's snapshot once the answer finishes
      estimate: estimate_prompt(forward),
      deliver: built.replayed.filter((o) => o.kind === "cue" && !o.delivered).map((o) => o.turn),
      forward, keep_head: built.keep_head,   // for the wall's cut-down resend (wall.js)
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
    wall.note_usage(ctx.model, usage, ctx.estimate);
  }

  /** The client's leading system messages of this conversation, for a pack (memory only). */
  function remember_system(thread_id, messages) {
    const head = [];
    for (const m of messages) {
      if (m?.role !== "system" && m?.role !== "developer") break;
      head.push({ role: m.role, content: m.content });
    }
    last_system.set(thread_id, head);
  }

  /**
   * A finished turn (a reply without tool calls) whose fill reached the force line asks
   * for a pack. Not while compress is off, not when the window already flipped.
   */
  function maybe_force(thread_id, ctx, tools) {
    if (tools && tools.length) return;
    const values = read_compress();
    if (values.on !== true) return;
    const thread = threads.get(thread_id);
    const w = thread && thread.window;
    if (!w || w.name !== ctx.window || w.summary_pending) return;
    const fill = Number(w.usage?.fill_pct);
    if (!Number.isFinite(fill) || fill < values.force_pct) return;
    const asked = packer.request(thread.id, "forced");
    console.log(`[gateway] present ${thread.id} fill ${fill}% ≥ force line ${values.force_pct}%: pack ${asked.started ? "started" : asked.running ? "already running" : "waiting to retry"}`);
  }

  /** 「现在压」: pack this thread (default: the most recent) now. → { status, body } */
  function compress_now(thread_id = null) {
    const thread = thread_id ? threads.get(String(thread_id))
      : threads.list().slice().sort((a, b) => (b.last_at || 0) - (a.last_at || 0))[0] || null;
    if (!thread) return { status: 404, body: { ok: false, error: thread_id ? `no such conversation: ${thread_id}` : "no conversation yet" } };
    if (packer.is_running(thread.id)) return { status: 409, body: { ok: false, error: "a pack is already running for this conversation", thread: thread.id } };
    packer.request(thread.id, "manual");
    return { status: 200, body: { ok: true, queued: true, thread: thread.id } };
  }

  /**
   * For /health: the newest successful compression of any kind (a flip's opened_by in
   * the ledger, so it survives a restart; or a flip this process saw) and the pack
   * failures since. Times and counts only.
   */
  function compress_health() {
    const s = packer.status();
    let last = s.last_ok_at ? { t: s.last_ok_at, how: s.last_ok_how } : null;
    for (const t of threads.list()) {
      const o = t.window && t.window.opened_by;
      const at = o && o.how ? Date.parse(o.at) : NaN;
      if (Number.isFinite(at) && (!last || at > last.t)) last = { t: at, how: o.how };
    }
    return {
      last_ok_at: last ? local_stamp(last.t, zone).iso : null,
      last_ok_ms: last ? last.t : null,
      last_how: last ? last.how : null,
      failures_since_ok: s.failures_since_ok,
      last_failure_reason: s.last_failure ? s.last_failure.reason : null,
      running: s.running.length,
      waiting_to_retry: s.pending.length,
    };
  }

  /**
   * After a finished reply: a closed summary block is kept for the flip; the flip happens
   * once the turn has ended (a reply without tool calls). See compress.js.
   */
  function after_summary(thread_id, ctx, split, tools) {
    const thread = threads.get(thread_id);
    if (!thread || !thread.window || thread.window.name !== ctx.window) return;
    const w = thread.window;
    const values = settings.load().values.compress;
    if (split.half) console.log(`[gateway] present ${thread.id} a summary block that never closed was left out (not kept)`);
    if (split.summary) {
      if (values.on !== true) {
        console.log(`[gateway] present ${thread.id} a summary block came while compress is off: left out, no flip`);
        return;
      }
      w.summary_pending = split.summary;
    }
    if (!w.summary_pending) return;
    if (tools && tools.length) { threads.save(thread.id); return; }
    const flip = compress.self_flip({ thread, summary: w.summary_pending, keep_raw: values.keep_raw,
                                      at: local_stamp(clock.now(), zone).iso, now: clock.now() });
    threads.save(thread.id);
    packer.note_flip("self");
    console.log(`[gateway] present ${thread.id} window ${flip.old_name} → ${flip.name} (he folded it himself), mark ${flip.mark}`);
    cue.dropped({ window: flip.old_name, all: true })
      .catch((err) => log(`[gateway] present: /cue/dropped failed: ${err?.message || err}`));
  }

  function on_response(ctx, { status, headers, stream }) {
    if (!ctx || status < 200 || status >= 300) return null;
    confirm_delivered(ctx);
    capture_reply(stream, headers, (raw_reply) => {
      // what the day store keeps is what the client saw: the summary block taken out
      const split = split_summary(raw_reply.text);
      const reply = { ...raw_reply, text: split.visible };
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
      try { after_summary(thread_id, ctx, split, reply.tools); }
      catch (err) { log(`[gateway] present: flipping the window failed: ${err?.message || err}`); }
      try { wake.remember_turn(thread_id, ctx.sent, reply); }
      catch (err) { log(`[gateway] present: keeping the wake snapshot failed: ${err?.message || err}`); }
      try { maybe_force(thread_id, ctx, reply.tools); }
      catch (err) { log(`[gateway] present: asking for a pack failed: ${err?.message || err}`); }
    });
    // the client always gets the answer through the filter: the summary block never
    // reaches it, whether or not anyone asked for one
    const is_sse = /text\/event-stream/i.test(String(headers?.["content-type"] || ""));
    const filter = is_sse ? create_sse_filter({ strip_usage: ctx.strip }) : create_json_filter();
    stream.on("error", () => filter.destroy());
    return stream.pipe(filter);
  }

  /** What the panel may see of a window: numbers and names, never the carry or an overlay's text. */
  function window_status(thread_id) {
    const thread = thread_id ? threads.get(thread_id)
      : threads.list().slice().sort((a, b) => b.last_at - a.last_at)[0] || null;
    if (!thread) return null;
    const w = thread.window && typeof thread.window === "object" ? thread.window : null;
    const u = w && w.usage && typeof w.usage === "object" ? w.usage : null;
    const size = windows.resolve(u?.model || w?.model || null);
    const branch = Array.isArray(thread.branch) ? thread.branch : [];
    let kept_raw = null;
    if (w) {
      const at = w.mark ? branch.findIndex((e) => e.id === w.mark) : 0;
      kept_raw = at >= 0 ? branch.length - at : null;
    }
    const opened = w && w.opened_by && typeof w.opened_by === "object" ? w.opened_by : null;
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
      kept_raw,
      // time and way only: what the panel shows as compress.last
      last: opened && opened.how
        ? { at: opened.at ?? null, how: String(opened.how), how_words: HOW_WORDS[opened.how] || null } : null,
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
    remember_owner: own_turn.remember_owner,
    owner_arrived: wake.owner_arrived,
    own_turn,
    wake,
    banner_lines,
    window_status,
    heartbeat_tasks: [{ name: "pack", run: () => packer.beat() }, { name: "wake", run: () => wake.tick() }],
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
    // forced / manual packing and the wall (pack.js · wall.js)
    escape_wall: (ctx, resp, send) => wall.escape(ctx, resp, send),
    compress_now,
    compress_health,
    packer,
  };
}

module.exports = { create_present };
