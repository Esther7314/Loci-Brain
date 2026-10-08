// ============================================================
// gateway/present/source_api.js — the fourth joint, host side: POST /loci/source
//
// Loci keeps a source's identity, never its text; when the model asks for an original,
// Loci asks the host. For conversations that came through this gateway, the host is the
// gateway and the text is in the day store. The wire format is Loci's, defined at the top
// of loci-brain src/core/_originals.py, and is followed here to the letter:
//
//   request   {v: 1, source: {system, instance, container, id, through?}, revision,
//              span, scope, max_chars}
//   answer    {v: 1, status: "given", lines: [...], truncated_after}
//             {v: 1, status: "unavailable"}
//             {v: 1, status: "not_allowed", reason: "out_of_scope"}
//   a line    {id, revision, text, cut?, at?} or {id, revision: null, missing}
//
// How this host answers:
//   · Whose sources: `system: "gateway"`, `instance: <LOCI_GATEWAY_NAME>`. Anything else is
//     not this gateway's: every line asked for is `missing: "not_found"`.
//   · A line is found by its id alone (m_<YYYYMMDD>_<seq>: the date names the day file).
//     The container is not checked for a single line: a forked conversation shares its
//     parent's lines, so the thread a source was registered under can differ from the
//     thread the line was born on.
//   · A run (`through`) is the lines from `id` to `through` in the host's order — days in
//     date order, ids in birth order within a day — that belong to the container's
//     thread (`thread:<id>`; a container naming no thread keeps every line in range). The
//     two ends are always listed: an end the day store does not have is listed as
//     `missing: "not_found"`, so the answer still starts at `id` and ends at `through`.
//   · A line never stored → `missing: "not_found"` (the host has no record; not "deleted").
//   · `revision` is "r<n>": the revision at which the line's current text was set. A
//     line edited in the client is given in its latest version with its own revision
//     ("r2", …). The revision asked for is not looked up: the latest text is given and
//     its revision says which one it is, so Loci can see the difference.
//   · A `replaced` line (a reply the owner regenerated) is given as it was: it was really
//     said and really shown, and a memory may rest on it. The wire has no word for
//     "superseded", and `withdrawn` / `deleted` would make Loci hold the source, which a
//     regenerate is not (the owner's ruling: only an edit is ever reported). Marking it
//     replaced writes a new revision without changing the text, so its revision stays
//     the one at which that text was set.
//   · `at` is when the line was first said (its first revision), with the owner's offset.
//     `speaker` is left out: nothing in the gateway names the owner or the model yet.
//   · Attachments are kept as kinds only; their placeholders ("[image]") follow the text.
//   · `span` (one line only): the fragment's own text, in the unit asked (utf16 · utf8 · char).
//   · `scope`: null reads everything. Otherwise the source's place must be covered by
//     one of `grant`'s places, else `not_allowed` / `out_of_scope`.
//   · `max_chars` (counted in characters, as Loci counts): the line that crosses it is
//     cut (`cut: true`); in a run the lines after it are left out and `truncated_after`
//     names it. A single line is never truncated after. A run lists at most MAX_LINES.
//   · A day file that cannot be read → `unavailable` (temporary, Loci lets the memory's
//     own body stand in).
// A request that does not read (not JSON, v ≠ 1, no source id, a run with a span) is a
// 400: Loci never sends one, so it is not Loci asking.
//
// The text goes into this one reply and nowhere else: nothing is logged but the outcome.
// ============================================================

const { send_json, read_json } = require("./http_io.js");
const { ID_PATTERN } = require("./day_store.js");

const VERSION = 1;
const DEFAULT_MAX_CHARS = 8000;
const MAX_CHARS_CEILING = 200000;
const MAX_LINES = 1000;
const MAX_RUN_DAYS = 400;
const SPAN_UNITS = ["utf16", "utf8", "char"];

function parse_id(id) {
  const m = ID_PATTERN.exec(String(id || ""));
  if (!m) return null;
  return { id, day: `${m[1]}-${m[2]}-${m[3]}`, compact: `${m[1]}${m[2]}${m[3]}`, seq: Number(m[4]) };
}
const order = (a, b) => (a.compact === b.compact ? a.seq - b.seq : (a.compact < b.compact ? -1 : 1));

function days_between(from_day, to_day) {
  const out = [];
  const t = Date.parse(`${to_day}T00:00:00Z`);
  for (let d = Date.parse(`${from_day}T00:00:00Z`); d <= t && out.length <= MAX_RUN_DAYS; d += 86400000) {
    out.push(new Date(d).toISOString().slice(0, 10));
  }
  return out;
}

const not_found = (id) => ({ id, revision: null, missing: "not_found" });

/** The thread a container names (`thread:t_3f9c1a` or `thread:3f9c1a`), as both spellings; null for none. */
function thread_of(container) {
  const m = /^thread:(.+)$/.exec(String(container || ""));
  if (!m) return null;
  const raw = m[1];
  return raw.startsWith("t_") ? [raw, raw.slice(2)] : [raw, `t_${raw}`];
}

function covered(source, scope) {
  if (scope == null) return true;
  const grant = Array.isArray(scope.grant) ? scope.grant : [];
  return grant.some((p) => p && typeof p === "object"
    && ["system", "instance", "container"].every((k) => p[k] == null || p[k] === source[k]));
}

/**
 * One id's view from its revisions: the current version, the revision its text was set
 * at, and when it was first said.
 */
function line_view(revisions) {
  const sorted = [...revisions].sort((a, b) => a.rev - b.rev);
  const latest = sorted[sorted.length - 1];
  const same = (x) => x.text === latest.text
    && JSON.stringify(x.attach || null) === JSON.stringify(latest.attach || null);
  let set_at = latest.rev;
  for (let i = sorted.length - 2; i >= 0 && same(sorted[i]); i--) set_at = sorted[i].rev;
  const text = String(latest.text ?? "");
  const marks = (latest.attach || []).map((k) => `[${k}]`).join(" ");
  const line = {
    id: latest.id,
    revision: `r${set_at}`,
    text: marks ? (text ? `${text} ${marks}` : marks) : text,
  };
  if (sorted[0].at) line.at = sorted[0].at;
  return { line, thread: latest.thread };
}

function fragment(text, span) {
  const { unit, start, end } = span;
  if (unit === "utf16") return text.slice(start, end);
  if (unit === "char") return Array.from(text).slice(start, end).join("");
  return Buffer.from(text, "utf8").subarray(start, end).toString("utf8");
}

/** Cut the text at `budget` characters; see the header. */
function within_budget(lines, budget, single) {
  const out = [];
  let left = budget;
  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i];
    if (out.length >= MAX_LINES) return { lines: out, truncated_after: out[out.length - 1].id };
    if (ln.missing) { out.push(ln); continue; }
    if (left === 0 && out.length > 0 && !single) return { lines: out, truncated_after: out[out.length - 1].id };
    const chars = Array.from(ln.text);
    if (chars.length <= left) { out.push(ln); left -= chars.length; continue; }
    out.push({ ...ln, text: chars.slice(0, left).join(""), cut: true });
    const more = i < lines.length - 1;
    return { lines: out, truncated_after: more && !single ? ln.id : null };
  }
  return { lines: out, truncated_after: null };
}

function bad_request(res, error) {
  send_json(res, 400, { error });
  return { outcome: "bad_request" };
}

/**
 * @param day_store  day_store.js instance (read_day by day)
 * @param name       LOCI_GATEWAY_NAME: the `instance` this gateway's sources carry
 */
function create_source_api({ day_store, name }) {
  function answer(body) {
    const source = body.source;
    const single = source.through == null;
    const asked = single ? [source.id] : (source.id === source.through ? [source.id] : [source.id, source.through]);
    const all_missing = () => ({ v: VERSION, status: "given", lines: asked.map(not_found), truncated_after: null });

    if (source.system !== "gateway" || source.instance !== name) return all_missing();
    if (!covered(source, body.scope)) return { v: VERSION, status: "not_allowed", reason: "out_of_scope" };

    const first = parse_id(source.id);
    const last = single ? first : parse_id(source.through);
    if (!first || !last || order(last, first) < 0) return all_missing();

    // Every revision of every id in range, read day by day.
    const by_id = new Map();
    const days = days_between(first.day, last.day);
    if (days.length > MAX_RUN_DAYS) return all_missing();
    for (const day of days) {
      for (const raw of day_store.read_day(day)) {
        const p = parse_id(raw.id);
        if (!p || order(p, first) < 0 || order(p, last) > 0) continue;
        if (!by_id.has(raw.id)) by_id.set(raw.id, []);
        by_id.get(raw.id).push(raw);
      }
    }

    const max_chars = Number.isInteger(body.max_chars) && body.max_chars > 0
      ? Math.min(body.max_chars, MAX_CHARS_CEILING) : DEFAULT_MAX_CHARS;

    if (single) {
      const revs = by_id.get(source.id);
      if (!revs) return all_missing();
      const { line } = line_view(revs);
      if (body.span) line.text = fragment(line.text, body.span);
      const cut = within_budget([line], max_chars, true);
      return { v: VERSION, status: "given", lines: cut.lines, truncated_after: null };
    }

    const wanted = thread_of(source.container);
    const ordered = [...by_id.keys()].map(parse_id).sort(order).map((p) => p.id);
    const lines = [];
    for (const id of ordered) {
      const { line, thread } = line_view(by_id.get(id));
      const end = id === source.id || id === source.through;
      if (end || !wanted || wanted.includes(thread)) lines.push(line);
    }
    if (!lines.length || lines[0].id !== source.id) lines.unshift(not_found(source.id));
    if (lines[lines.length - 1].id !== source.through) lines.push(not_found(source.through));
    const cut = within_budget(lines, max_chars, false);
    return { v: VERSION, status: "given", lines: cut.lines, truncated_after: cut.truncated_after };
  }

  /** The request, checked; a string says why it does not read. */
  function check(body) {
    if (!body || typeof body !== "object" || Array.isArray(body)) return "the body is not a JSON object";
    if (body.v !== VERSION) return "v is not 1";
    const s = body.source;
    if (!s || typeof s !== "object" || typeof s.id !== "string" || !s.id) return "source.id is missing";
    for (const k of ["system", "instance", "container"]) if (typeof s[k] !== "string") return `source.${k} is missing`;
    if (s.through != null && typeof s.through !== "string") return "source.through is not text";
    if (body.scope != null && (typeof body.scope !== "object" || Array.isArray(body.scope))) return "scope is not an object";
    if (body.span != null) {
      if (s.through != null) return "a run of lines with a span is not supported";
      const sp = body.span;
      if (typeof sp !== "object" || !SPAN_UNITS.includes(sp.unit) || !Number.isInteger(sp.start)
          || !Number.isInteger(sp.end) || sp.start < 0 || sp.start >= sp.end) return "span is not {unit, start, end}";
    }
    return null;
  }

  return async function handle_source(req, res) {
    if (req.method !== "POST") {
      req.resume();
      send_json(res, 404, { error: "/loci/source takes POST" });
      return { outcome: "not_post" };
    }
    const got = await read_json(req);
    if (!got.ok) return bad_request(res, got.error);
    const why = check(got.value);
    if (why) return bad_request(res, why);
    let reply;
    try { reply = answer(got.value); }
    catch { reply = { v: VERSION, status: "unavailable" }; }
    send_json(res, 200, reply);
    return { outcome: reply.status };
  };
}

module.exports = { create_source_api, line_view, within_budget, MAX_LINES };
