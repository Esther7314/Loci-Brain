// ============================================================
// gateway/present/threads.js — which conversation a request belongs to, and what in it
// is new
//
// Every chat request that enters the gateway is the owner talking (the client points a
// provider of its own at the gateway; nothing else is sent here), so every one counts.
// What the request does NOT say is which conversation it is: the client sends its whole
// history each time and nothing more. This module reads that history, lines it up
// against what the day store already holds, and decides three things:
//   · which thread (conversation) this is
//   · which of the client's messages are new and must be written now — before the
//     request goes upstream
//   · where the reply that comes back belongs once it has finished
//
// ─── Fingerprints ───
// A message is compared by a fingerprint of its role, its text (whitespace runs
// collapsed, ends trimmed), the kinds of its attachments and the names of the tools it
// called. Images and files themselves never enter it: an attachment is recorded as its
// kind ("image") and nothing else.
//
// ─── Telling conversations apart (there is no tag) ───
// The rule: **a request continues a thread only when one of the model's own replies in
// its history is already on that thread.** Replies are written by the model, long and
// specific; the owner's lines are not ("嗯", "你好"), and two different chats share them
// all the time. So:
//   · The anchor is the latest assistant message in the history whose fingerprint sits
//     on some thread's branch. Counting backwards from the anchor, the history and the
//     branch must agree
//       – all the way back to the start of the client's history (the client may have
//         trimmed its own front), or
//       – all the way back to the start of the thread, with the anchor at the thread's
//         end (the thread was born mid-chat, e.g. after a provider switch), or
//       – for at least MIN_PARTIAL_RUN messages in a row (something older was edited in
//         the client);
//     and the last two only count when the replies in the agreeing stretch carry at
//     least SPECIFIC_CHARS characters between them. A lone generic reply ("好的。") that
//     happens to sit in another conversation does not glue two chats together.
//   · A history with no assistant message in it is the opening of a chat. It starts a
//     new thread — a brand-new chat is never glued onto an old one, even if it opens
//     with the same words. Two narrow exceptions, both about one chat's own opening:
//       – a resend (below) of the same opening within RESEND_WINDOW_MS
//       – the earlier messages are exactly a thread made only of the owner's lines that
//         never got a reply (the request failed upstream; the owner typed again)
//   · Several threads can hold the anchor (a fork shares its parent's lines). The one
//     chosen is the one the rest of the history keeps agreeing with, then the one whose
//     end the history reaches, then the longest agreement, then the most recent.
//
// ─── Resends, regenerates, forks, tool turns ───
//   · **Resend**: the same request (same system text, same messages) within
//     RESEND_WINDOW_MS of the last time it was seen is the same turn — nothing new is
//     written and the turn id does not change. (Anchored histories come out the same way
//     without the window; the window is what keeps an opening line's retry on its own
//     thread instead of opening a second one.)
//   · **Regenerate**: the history ends at a line already stored, and the thread has only
//     replies after it. When the new reply finishes, the earlier replies are marked
//     `replaced` (a new revision each) and the new reply is written. A reply identical to
//     the earlier one writes nothing.
//   · **Fork / rewind / edit**: the history leaves the stored branch with a message of
//     the owner's that differs from what is stored there, or a reply arrives for a line
//     the thread has already moved past. The thread is left exactly as it is and a new
//     thread continues from the shared part (its branch reuses the same line ids; no line
//     is copied). An edited line of the owner's is therefore a new line, never a revision
//     of the old one: the client may still be showing the old one (forked chats), and
//     the old one was really sent. Nothing is ever inferred from history going missing.
//   · A reply of the model's that the client sends back changed (edited in the client)
//     is written as a new revision of that reply's line.
//   · **Tool turn**: a request whose last message is a tool result continues the turn in
//     flight. No line is written for it; its reply is written after the tool call it
//     answers. Tool results are never written.
//
// ─── On disk ───
// <LOCI_GATEWAY_DATA>/threads/<thread>.json — the private ledger, never exported:
//     { id, created_at, last_at, branch: [{ id, role, fp }], window }
// `branch` is the conversation as the client currently shows it, as day-store line ids.
// `window` belongs to present/window.js (mark, carry, overlays, usage); this module keeps
// it in the same file and never reads it. A fork starts without one — present/index.js
// decides what the fork inherits. `last_sent` belongs to present/wake.js the same way:
// the last answered request as it went upstream, the snapshot a wake's prefix copies.
// A thread file that cannot be read after three tries is left alone: never matched,
// never overwritten (a file that will not parse is not an empty thread).
// ============================================================

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { names_of } = require("./reply_capture.js");

const RESEND_WINDOW_MS = 2 * 60 * 1000;
const MIN_PARTIAL_RUN = 3;
const SPECIFIC_CHARS = 20;
const READ_TRIES = 3;

function sha(text) { return crypto.createHash("sha1").update(text).digest("hex"); }

/** One message as the day store keeps it: role, text, attachment kinds, tool names. */
function to_said(message) {
  const role = message.role;
  let text = "";
  const attach = [];
  const content = message.content;
  if (typeof content === "string") text = content;
  else if (Array.isArray(content)) {
    const texts = [];
    for (const part of content) {
      if (typeof part === "string") texts.push(part);
      else if (part && part.type === "text") texts.push(String(part.text ?? ""));
      else if (part && part.type) attach.push(part.type === "image_url" ? "image"
        : part.type === "input_audio" ? "audio" : part.type);
    }
    text = texts.join("\n");
  }
  const tools = role === "assistant"
    ? names_of(message.tool_calls || (message.function_call ? [message.function_call] : []))
    : [];
  return { role, text, attach, tools, fp: fingerprint({ role, text, attach, tools }) };
}

function fingerprint({ role, text, attach = [], tools = [] }) {
  const flat = String(text ?? "").replace(/\s+/g, " ").trim();
  return sha([role, flat, (attach || []).join(","), (tools || []).join(",")].join("\u0000")).slice(0, 16);
}

function system_text(messages) {
  return messages.filter((m) => m && (m.role === "system" || m.role === "developer"))
    .map((m) => to_said({ ...m, role: "user" }).text).join("\n");
}

/**
 * @param dir         <LOCI_GATEWAY_DATA>/threads
 * @param day_store   day_store.js instance
 * @param clock       { now() }
 */
function create_threads({ dir, day_store, clock, resend_window_ms = RESEND_WINDOW_MS }) {
  const threads = new Map();
  const broken = new Set();
  const recent = new Map();   // request key → { thread, cursor, at }
  let loaded = false;

  function load_all() {
    if (loaded) return;
    loaded = true;
    let names = [];
    try { names = fs.readdirSync(dir).filter((n) => n.endsWith(".json")); }
    catch (err) { if (err.code !== "ENOENT") throw err; }
    for (const name of names) {
      const id = name.slice(0, -5);
      let state = null;
      for (let i = 0; i < READ_TRIES && !state; i++) {
        try {
          const parsed = JSON.parse(fs.readFileSync(path.join(dir, name), "utf8"));
          if (parsed && parsed.id === id && Array.isArray(parsed.branch)) state = parsed;
        } catch { /* tried again below */ }
      }
      if (state) threads.set(id, state); else broken.add(id);
    }
  }

  function save(thread) {
    fs.mkdirSync(dir, { recursive: true });
    const file = path.join(dir, `${thread.id}.json`);
    const tmp = `${file}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(thread), "utf8");
    fs.renameSync(tmp, file);
  }

  function new_thread(branch = []) {
    let id;
    do { id = `t_${crypto.randomBytes(3).toString("hex")}`; }
    while (threads.has(id) || broken.has(id) || fs.existsSync(path.join(dir, `${id}.json`)));
    const now = clock.now();
    const thread = { id, created_at: now, last_at: now, branch: branch.map((e) => ({ ...e })) };
    threads.set(id, thread);
    return thread;
  }

  function write_line(thread, said) {
    const line = day_store.append_new({
      thread: thread.id, role: said.role, text: said.text, attach: said.attach, tools: said.tools,
    });
    thread.branch.push({ id: line.id, role: said.role, fp: said.fp });
    return line.id;
  }

  /** Do the replies inside the agreeing run carry enough text to belong to one chat only? */
  function specific(prior, p, run) {
    let chars = 0;
    for (let k = 0; k < run; k++) {
      const m = prior[p - k];
      if (m.role === "assistant") chars += m.text.replace(/\s+/g, " ").trim().length;
    }
    return chars >= SPECIFIC_CHARS;
  }

  /** How many messages agree going backwards from prior[p] / branch[s]. */
  function run_back(prior, p, branch, s) {
    let run = 0;
    while (p - run >= 0 && s - run >= 0 && prior[p - run].fp === branch[s - run].fp) run++;
    return run;
  }

  /**
   * @param prior  the client's messages before its own new one
   * @param own    [the owner's new message] for a turn, [] for a tool continuation
   * @returns { thread, after, rest } — the walk starts at branch[after] with rest + own
   */
  function find_thread(prior, own) {
    let best = null;
    // Ranked in this order: how far the rest of the history keeps agreeing with the
    // branch, how little of the branch is left past where the history ends, how long the
    // agreement before the anchor is, how recently the thread was used.
    const better = (a, b) => {
      if (!b) return true;
      for (const [x, y] of [[a.walk_ok, b.walk_ok], [-a.left_after, -b.left_after], [a.run, b.run], [a.thread.last_at, b.thread.last_at]]) {
        if (x !== y) return x > y;
      }
      return false;
    };
    const consider = (thread, p, s, run) => {
      const branch = thread.branch;
      const walk = prior.slice(p + 1).concat(own);
      let walk_ok = 0;
      while (walk_ok < walk.length && s + 1 + walk_ok < branch.length && branch[s + 1 + walk_ok].fp === walk[walk_ok].fp) walk_ok++;
      const cand = { thread, p, s, run, walk_ok, left_after: branch.length - (s + 1 + walk_ok) };
      if (better(cand, best)) best = cand;
    };

    for (const thread of threads.values()) {
      const branch = thread.branch;
      const spots_of = new Map();
      branch.forEach((e, s) => {
        if (e.role !== "assistant") return;
        if (!spots_of.has(e.fp)) spots_of.set(e.fp, []);
        spots_of.get(e.fp).push(s);
      });
      // the latest reply in the history that this thread holds
      for (let p = prior.length - 1; p >= 0; p--) {
        if (prior[p].role !== "assistant") continue;
        const spots = spots_of.get(prior[p].fp);
        if (!spots) continue;
        for (const s of spots) {
          const run = run_back(prior, p, branch, s);
          const whole_history = run === p + 1;
          const thread_born_mid_chat = run === s + 1 && s === branch.length - 1;
          if (whole_history
              || ((thread_born_mid_chat || run >= MIN_PARTIAL_RUN) && specific(prior, p, run))) {
            consider(thread, p, s, run);
          }
        }
        break;
      }
    }
    if (best) return { thread: best.thread, after: best.s + 1, rest: prior.slice(best.p + 1) };

    // An opening that never got a reply, typed again: the history before this message is
    // exactly a thread holding nothing but those same lines of the owner's.
    if (prior.length && prior.every((m) => m.role === "user")) {
      let match = null;
      for (const thread of threads.values()) {
        const b = thread.branch;
        if (b.length === prior.length && b.every((e, i) => e.fp === prior[i].fp)
            && (!match || thread.last_at > match.last_at)) match = thread;
      }
      if (match) return { thread: match, after: match.branch.length, rest: [] };
    }
    return null;
  }

  function request_key(kind, messages, said) {
    return sha([kind, system_text(messages), ...said.map((m) => m.fp)].join("\u0001"));
  }

  function prune(now) {
    for (const [key, entry] of recent) if (now - entry.at > resend_window_ms) recent.delete(key);
  }

  function turn_of(thread, cursor) {
    const at = thread.branch.findIndex((e) => e.id === cursor);
    for (let i = at; i >= 0; i--) if (thread.branch[i].role === "user") return thread.branch[i].id;
    return null;
  }

  /**
   * Called with the client's messages, before anything goes upstream.
   * Writes the owner's new lines right here.
   * @returns { kind: "turn" | "continuation" | "ignored", thread, turn, cursor, resend,
   *            wrote: [ids], revised: [ids], forked_from }
   *   `cursor` is the stored line the client's history ends at; the reply goes after it.
   */
  function ingest(messages) {
    load_all();
    const now = clock.now();
    const last_role = messages[messages.length - 1]?.role;
    const kind = last_role === "user" ? "turn"
      : (last_role === "tool" || last_role === "function") ? "continuation" : "ignored";
    if (kind === "ignored") return { kind, wrote: [], revised: [] };

    const said = messages.filter((m) => m && (m.role === "user" || m.role === "assistant")).map(to_said);
    const key = request_key(kind, messages, said);
    prune(now);

    const seen = recent.get(key);
    if (seen && threads.has(seen.thread)) {
      const thread = threads.get(seen.thread);
      if (seen.cursor === null || thread.branch.some((e) => e.id === seen.cursor)) {
        seen.at = now;
        thread.last_at = now;
        save(thread);
        return { kind, thread: thread.id, cursor: seen.cursor, turn: seen.cursor && turn_of(thread, seen.cursor),
                 resend: true, wrote: [], revised: [], forked_from: null };
      }
    }

    const prior = kind === "turn" ? said.slice(0, -1) : said;
    const own = kind === "turn" ? [said[said.length - 1]] : [];
    const found = find_thread(prior, own);
    let thread, pos, walk;
    if (found) {
      thread = found.thread;
      pos = found.after;
      walk = found.rest.concat(own);
    } else {
      thread = new_thread();
      pos = 0;
      walk = own;
    }

    const wrote = [];
    const revised = [];
    let forked_from = null;
    let cursor = pos > 0 ? thread.branch[pos - 1].id : null;
    for (const m of walk) {
      const here = thread.branch[pos];
      if (here && here.fp === m.fp) { cursor = here.id; pos++; continue; }
      if (here && here.role === "assistant" && m.role === "assistant") {
        // the client is showing this reply changed: a new revision of the same line
        day_store.revise(here.id, { text: m.text, attach: m.attach, tools: m.tools, state: "live" });
        thread.branch[pos] = { id: here.id, role: m.role, fp: m.fp };
        revised.push(here.id);
        cursor = here.id; pos++; continue;
      }
      if (here) {
        // the history leaves this branch: the thread stays as it is, a fork carries on
        forked_from = thread.id;
        thread = new_thread(thread.branch.slice(0, pos));
      }
      cursor = write_line(thread, m);
      wrote.push(cursor);
      pos++;
    }

    thread.last_at = now;
    save(thread);
    recent.set(key, { thread: thread.id, cursor, at: now });
    return { kind, thread: thread.id, cursor, turn: cursor && turn_of(thread, cursor),
             resend: false, wrote, revised, forked_from };
  }

  /**
   * Called once the reply to an ingested request has finished.
   * @param seen   what ingest() returned
   * @param reply  { text, tools }
   * @returns { thread, wrote: id | null, replaced: [ids], forked_from }
   */
  function record_reply(seen, reply) {
    load_all();
    const said = { role: "assistant", text: String(reply.text ?? ""), attach: [], tools: reply.tools || [] };
    if (!said.text.trim() && !said.tools.length) return { thread: seen.thread, wrote: null, replaced: [], forked_from: null };
    said.fp = fingerprint(said);

    let thread = threads.get(seen.thread);
    if (!thread) return { thread: seen.thread, wrote: null, replaced: [], forked_from: null };
    const at = seen.cursor ? thread.branch.findIndex((e) => e.id === seen.cursor) : -1;
    if (seen.cursor && at < 0) {
      // A concurrent regenerate already took the line this reply follows off the branch.
      // The reply was still said, so it is written; it just has no place on the branch.
      const line = day_store.append_new({ thread: thread.id, role: said.role, text: said.text, tools: said.tools });
      return { thread: thread.id, wrote: line.id, replaced: [], forked_from: null };
    }

    let end = at + 1;
    while (end < thread.branch.length && thread.branch[end].role === "assistant") end++;
    const earlier = thread.branch.slice(at + 1, end);
    const moved_on = end < thread.branch.length;

    if (earlier.length && earlier[0].fp === said.fp) {
      return { thread: thread.id, wrote: null, replaced: [], forked_from: null };
    }

    const replaced = [];
    let forked_from = null;
    if (moved_on) {
      forked_from = thread.id;
      thread = new_thread(thread.branch.slice(0, at + 1));
    } else {
      for (const e of earlier) {
        day_store.revise(e.id, { state: "replaced" });
        replaced.push(e.id);
      }
      thread.branch.length = at + 1;
    }
    const id = write_line(thread, said);
    thread.last_at = clock.now();
    save(thread);
    return { thread: thread.id, wrote: id, replaced, forked_from };
  }

  return {
    ingest,
    record_reply,
    get: (id) => { load_all(); return threads.get(id) || null; },
    /** Write a thread back after something outside this module (the window) changed it. */
    save: (id) => { load_all(); const t = threads.get(id); if (t) save(t); return Boolean(t); },
    list: () => { load_all(); return [...threads.values()]; },
    broken: () => { load_all(); return [...broken]; },
  };
}

module.exports = { create_threads, to_said, fingerprint, RESEND_WINDOW_MS, MIN_PARTIAL_RUN, SPECIFIC_CHARS };
