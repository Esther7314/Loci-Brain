// ============================================================
// gateway/present/settings.js — present.json: every knob of the present layer
//
// The file: <LOCI_GATEWAY_DATA>/present.json. The panel changes it through Loci
// (POST /present), the owner may edit it by hand, and every reader reads it fresh, so a
// change takes effect on the next beat without a restart.
//
// Sections and defaults (the blueprint's settings table, with the owner's final rulings):
//   compress   on · context_tokens (null = work it out: the provider's report, the
//              gateway's model table, else 1M; a number = the owner's own figure) ·
//              basic: ask_on · ask_pct · keep_raw
//              advanced: advanced_on · weak_pct[] · force_pct
//              (the two switches sit under `on` and are independent of each other; a
//              group that is off keeps its numbers, and the order weak < ask < force
//              holds whatever the switches say)
//   report     from · to · quiet_min · flip (daily | every_n | manual) · every_n
//   wake       on (off by default: an unattended machine idles until someone turns it
//              on) · every_min · dnd {on, from, to} · segments {mode: all | only, list} ·
//              held_cap (null = no limit on how many unanswered things he may say) ·
//              daily_cap · dry_run · allow_short · away_prefix (on by default: what he
//              said while she was away starts his next answer in her client, away.js;
//              off for a client that shows it some other way)
//   own        tool_rounds · models (empty = the model of the client's last request)
//   push       bark (a Bark key or a Bark URL, read from whatever Bark's app offers to
//              copy: bark_code()) · text (full | notice)
//   upstream   base_url ("" = the env LOCI_UPSTREAM) · key ("" = none set here)
// The panel shows most of these; compress.context_tokens, daily_cap, dry_run,
// allow_short, own.models and push.text live only in the file, but POST /present accepts them
// too — they are validated the same way, and nothing about them is secret.
//
// The upstream (upstream.js reads it per request, so a change takes effect on the next
// request, no restart):
//   · the URL in force = upstream.base_url when set, else LOCI_UPSTREAM.
//   · the key in force, for every request the gateway sends on its own account (its own
//     turns, Loci's side-model calls, the upstream test): the panel key, else
//     LOCI_UPSTREAM_KEY, else the key the client sent with its latest accepted request
//     (own_turn.js, memory only). The panel key wins over the env one because it is the
//     one the owner can change while the gateway runs; clearing it (`key: ""`) falls
//     back to the env key. A client's chat request is never touched: it goes upstream
//     with the key the client sent.
//
// What never leaves this module as it is stored:
//   · push.bark — the view carries `bark_set` and a masked tail (`bark_hint`) instead.
//   · upstream.key — the view carries `key_set` (a panel key is stored), and for the key
//     in force where it comes from (`key_from`: panel · env · client, or null when there
//     is none) and a masked tail (`key_hint`). LOCI_UPSTREAM_KEY is read from the env,
//     never written here; `own.fixed_key_set` says whether a panel or env key is in force.
//
// A file that cannot be read is not an empty file:
//   · read three times; still unreadable → state "unreadable": compress and report run
//     on the defaults, wake gets null (do not wake: a stranger's machine that cannot
//     read its own settings does not spend money), and a POST is refused (409) rather
//     than written over the owner's hand edit.
//   · readable, but a section fails validation → that section alone runs on its
//     defaults, state "invalid" with the field named; a wake section that fails also
//     means do not wake. A POST must then fix that field (or the file is fixed by hand).
//   · no file → state "defaults".
// ============================================================

const fs = require("fs");
const path = require("path");

const DEFAULTS = Object.freeze({
  compress: {
    on: true, context_tokens: null,
    ask_on: true, ask_pct: 75, keep_raw: 40,
    advanced_on: true, weak_pct: [65], force_pct: 85,
  },
  report: { from: "04:00", to: "08:00", quiet_min: 30, flip: "daily", every_n: 2 },
  wake: {
    on: false, every_min: 55,
    dnd: { on: true, from: "23:00", to: "08:00" },
    segments: { mode: "all", list: [] },
    held_cap: null, daily_cap: 16, dry_run: false, allow_short: false,
    away_prefix: true,
  },
  own: { tool_rounds: 10, models: [] },
  push: { bark: "", text: "full" },
  upstream: { base_url: "", key: "" },
});

// Keys the view adds; a patch that echoes them back has them dropped, not refused.
const READ_ONLY = {
  push: ["bark_set", "bark_hint"], own: ["fixed_key_set"],
  upstream: ["env_base_url", "in_force", "key_set", "key_hint", "key_from"],
};
const SECTIONS = Object.keys(DEFAULTS);
const NESTED = { wake: ["dnd", "segments"] };
const READ_TRIES = 3;
const MIN_EVERY_MIN = 15;

class SettingsError extends Error {
  constructor(field, message) { super(`${field}: ${message}`); this.field = field; this.reason = message; }
}
const bad = (field, message) => new SettingsError(field, message);

const is_plain = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const clone = (v) => JSON.parse(JSON.stringify(v));

function deep_merge(base, over) {
  const out = clone(base);
  for (const [k, v] of Object.entries(over || {})) {
    out[k] = is_plain(v) && is_plain(out[k]) ? deep_merge(out[k], v) : clone(v);
  }
  return out;
}

// ———— Field checks ————

function bool(v, field) {
  if (typeof v !== "boolean") throw bad(field, "只能是 true 或 false（开 / 关）");
  return v;
}
function int_in(v, lo, hi, field) {
  if (!Number.isInteger(v) || v < lo || v > hi) throw bad(field, `要填 ${lo} 到 ${hi} 之间的整数`);
  return v;
}
function int_or_null(v, lo, hi, field) {
  return v === null || v === "" ? null : int_in(v, lo, hi, field);
}
function one_of(v, list, field) {
  if (!list.includes(v)) throw bad(field, `只能是 ${list.join(" / ")} 其中一个`);
  return v;
}
const HHMM = /^([01]\d|2[0-3]):[0-5]\d$/;
function hhmm(v, field) {
  if (typeof v !== "string" || !HHMM.test(v)) throw bad(field, "时间要写成 HH:MM（00:00–23:59）");
  return v;
}
const minutes = (t) => Number(t.slice(0, 2)) * 60 + Number(t.slice(3));

function span(obj, where) {
  const from = hhmm(obj.from, `${where}.from`);
  const to = hhmm(obj.to, `${where}.to`);
  if (from === to) throw bad(`${where}.to`, "结束时间不能跟开始时间一样");
  return { from, to };
}

/** A span as half-open minute ranges on one day; one crossing midnight becomes two. */
function ranges(s) {
  const f = minutes(s.from), t = minutes(s.to);
  return f < t ? [[f, t]] : [[f, 1440], [0, t]];
}
function overlaps(a, b) {
  return ranges(a).some(([s1, e1]) => ranges(b).some(([s2, e2]) => s1 < e2 && s2 < e1));
}

const BARK_KEY = /^[A-Za-z0-9_-]{4,128}$/;
// A path segment that is a device key: Bark's keys are 22 letters and digits.
const KEY_SEGMENT = /^[A-Za-z0-9]{16,64}$/;
const FIRST_URL = /https?:\/\/[^\s"'`\\]+/i;
const DEVICE_KEY = /["']?device_key["']?\s*[:=]\s*["']?([A-Za-z0-9_-]{4,128})/;

/** What Bark's app offers to copy, read down to what a push needs: a bare key, or
 *  <server>/<key> (with the URL's own query, if it has one). The app shows three forms —
 *  GET, a URL with the key and a sample text in its path; POST, a curl line to
 *  <server>/<key>; JSON, a curl line to <server>/push with "device_key" in its body — and
 *  a plain key or URL is read too. The text after the key in a GET URL is dropped: the
 *  push posts its own. Returns the code, or throws the field's error. */
function bark_code(v, field = "push.bark") {
  if (typeof v !== "string") throw bad(field, "要填 Bark 推送码，或者一整条 Bark 网址");
  const s = v.trim();
  if (!s) return "";
  if (BARK_KEY.test(s)) return s;
  const found = s.match(FIRST_URL);
  const device = s.match(DEVICE_KEY);
  if (!found && !device) throw bad(field, "认不出推送码：把 Bark 里复制的整段粘进来（GET、POST、JSON 哪种都行），或者只粘那串码");
  let code = found ? found[0] : device[1];
  if (found) {
    let u;
    try { u = new URL(found[0]); } catch { throw bad(field, "这条网址读不出来"); }
    if (!/^https?:$/.test(u.protocol) || !u.hostname || found[0].length > 512) {
      throw bad(field, "网址要以 http(s) 开头、有主机名，最长 512 个字符");
    }
    if (device) {
      code = `${u.origin}${u.pathname.replace(/\/push\/?$/, "")}/${device[1]}`;
    } else {
      const segs = u.pathname.split("/");
      const at = segs.findIndex((x) => KEY_SEGMENT.test(x));
      if (at >= 0 && segs.slice(at + 1).some(Boolean)) code = `${u.origin}${segs.slice(0, at + 1).join("/")}${u.search}`;
    }
  }
  return code;
}

/** What the view shows of the Bark code: its origin (for a URL) and the last two characters of the key. */
function bark_hint(code) {
  if (!code) return null;
  const tail = (key) => (key.length > 4 ? `••••${key.slice(-2)}` : "••••");
  if (/^https?:\/\//i.test(code)) {
    try {
      const u = new URL(code);
      const key = u.pathname.split("/").filter(Boolean)[0] || "";
      return `${u.origin}/${key ? tail(key) : "••••"}`;
    } catch { return "••••"; }
  }
  return tail(code);
}

const MAX_UPSTREAM_URL = 512;
const MAX_UPSTREAM_KEY = 512;

/** An upstream base URL, trailing slashes off; "" stays "" (use LOCI_UPSTREAM). Throws the field's error. */
function upstream_url(v, field = "upstream.base_url") {
  if (typeof v !== "string") throw bad(field, "要填上游的地址（一串网址），或者留空用 LOCI_UPSTREAM");
  const s = v.trim().replace(/\/+$/, "");
  if (!s) return "";
  let u;
  try { u = new URL(s); } catch { u = null; }
  // the relay appends the request's own path and query, so the base carries neither
  if (!u || !/^https?:$/.test(u.protocol) || !u.hostname || u.username || u.password
      || u.search || u.hash || s.length > MAX_UPSTREAM_URL) {
    throw bad(field, `要填 http(s) 开头的地址，比如 https://api.deepseek.com/v1（不带账号密码、? 和 #，最长 ${MAX_UPSTREAM_URL} 个字符）`);
  }
  return s;
}

/** An upstream key as it goes after `Bearer `; "" clears it. Throws the field's error. */
function upstream_key(v, field = "upstream.key") {
  if (typeof v !== "string") throw bad(field, "要填一把 key（一串字符），或者传空的清掉");
  const s = v.trim().replace(/^bearer\s+/i, "");
  if (!s) return "";
  if (s.includes("•")) throw bad(field, "这是遮住的样子，不是 key：要换就把整把 key 重新粘进来");
  if (!/^[\x21-\x7e]+$/.test(s) || s.length > MAX_UPSTREAM_KEY) {
    throw bad(field, `key 里不能有空格、中文或别的看不见的字符，最长 ${MAX_UPSTREAM_KEY} 个字符`);
  }
  return s;
}

/** What a view shows of a key: the last two characters behind a mask; null for none. */
function key_hint(key) {
  if (key === null || key === undefined || key === "") return null;
  const s = String(key);
  return s.length > 4 ? `••••${s.slice(-2)}` : "••••";
}

/** LOCI_UPSTREAM as the gateway uses it (trailing slashes off), or null when unset. */
function env_base_url(env) {
  const s = String(env.LOCI_UPSTREAM || "").trim().replace(/\/+$/, "");
  return s || null;
}

// ———— Section validators: (merged section, touched(path)) → normalized section ————
// `touched` says whether the patch named a key, so an ordering error points at the
// field the owner just changed rather than its unchanged neighbour.

const VALIDATE = {
  compress(c, touched) {
    const out = {
      on: bool(c.on, "compress.on"),
      context_tokens: int_or_null(c.context_tokens, 1000, 100000000, "compress.context_tokens"),
      ask_on: bool(c.ask_on, "compress.ask_on"),
      ask_pct: int_in(c.ask_pct, 1, 99, "compress.ask_pct"),
      keep_raw: int_in(c.keep_raw, 1, 1000, "compress.keep_raw"),
      advanced_on: bool(c.advanced_on, "compress.advanced_on"),
      weak_pct: null,
      force_pct: int_in(c.force_pct, 1, 99, "compress.force_pct"),
    };
    if (!Array.isArray(c.weak_pct) || c.weak_pct.length > 5) throw bad("compress.weak_pct", "弱提醒线最多 5 条");
    out.weak_pct = c.weak_pct.map((p, i) => int_in(p, 1, 99, `compress.weak_pct[${i}]`)).sort((a, b) => a - b);
    if (new Set(out.weak_pct).size !== out.weak_pct.length) throw bad("compress.weak_pct", "有两条弱提醒线一样");
    if (out.weak_pct.some((p) => p >= out.ask_pct)) {
      throw bad(touched("compress.weak_pct") || !touched("compress.ask_pct") ? "compress.weak_pct" : "compress.ask_pct",
        "弱提醒线要比水位线低");
    }
    if (out.ask_pct >= out.force_pct) {
      throw bad(touched("compress.force_pct") ? "compress.force_pct" : "compress.ask_pct",
        "水位线要比强制线低");
    }
    return out;
  },

  report(r) {
    const { from, to } = span(r, "report");
    return {
      from, to,
      quiet_min: int_in(r.quiet_min, 1, 720, "report.quiet_min"),
      flip: one_of(r.flip, ["daily", "every_n", "manual"], "report.flip"),
      every_n: int_in(r.every_n, 2, 30, "report.every_n"),
    };
  },

  wake(w) {
    const allow_short = bool(w.allow_short, "wake.allow_short");
    if (!is_plain(w.dnd)) throw bad("wake.dnd", "要写成 {on, from, to}");
    if (!is_plain(w.segments)) throw bad("wake.segments", "要写成 {mode, list}");
    const mode = one_of(w.segments.mode, ["all", "only"], "wake.segments.mode");
    if (!Array.isArray(w.segments.list) || w.segments.list.length > 12) {
      throw bad("wake.segments.list", "时间段最多 12 段");
    }
    const list = w.segments.list.map((s, i) => {
      if (!is_plain(s)) throw bad(`wake.segments.list[${i}]`, "要写成 {from, to}");
      return span(s, `wake.segments.list[${i}]`);
    });
    for (let i = 0; i < list.length; i++) {
      for (let j = 0; j < i; j++) {
        if (overlaps(list[i], list[j])) throw bad(`wake.segments.list[${i}]`, `跟第 ${j + 1} 段时间重叠了`);
      }
    }
    if (mode === "only" && list.length === 0) throw bad("wake.segments.list", "最少填写一个时间段");
    const floor = allow_short ? 1 : MIN_EVERY_MIN;
    return {
      on: bool(w.on, "wake.on"),
      every_min: int_in(w.every_min, floor, 1440, "wake.every_min"),
      dnd: { on: bool(w.dnd.on, "wake.dnd.on"), ...span(w.dnd, "wake.dnd") },
      segments: { mode, list },
      held_cap: int_or_null(w.held_cap, 1, 100, "wake.held_cap"),
      daily_cap: int_in(w.daily_cap, 1, 200, "wake.daily_cap"),
      dry_run: bool(w.dry_run, "wake.dry_run"),
      allow_short,
      away_prefix: bool(w.away_prefix, "wake.away_prefix"),
    };
  },

  own(o) {
    if (!Array.isArray(o.models) || o.models.length > 10) throw bad("own.models", "模型最多填 10 个");
    return {
      tool_rounds: int_in(o.tool_rounds, 1, 20, "own.tool_rounds"),
      models: o.models.map((m, i) => {
        if (typeof m !== "string" || !m.trim() || m.length > 200) throw bad(`own.models[${i}]`, "要填一个模型名（最长 200 个字符）");
        return m.trim();
      }),
    };
  },

  push(p) {
    return { bark: bark_code(p.bark, "push.bark"), text: one_of(p.text, ["full", "notice"], "push.text") };
  },

  upstream(u) {
    return { base_url: upstream_url(u.base_url), key: upstream_key(u.key) };
  },
};

/** Refuse what a patch may not name: unknown sections or keys, wrong shapes. Returns it without the read-only echoes. */
function clean_patch(patch) {
  if (!is_plain(patch)) throw bad("patch", "要按设置分节传一个对象");
  const out = {};
  for (const [section, body] of Object.entries(patch)) {
    if (!SECTIONS.includes(section)) throw bad(section, "没有这一节设置");
    if (!is_plain(body)) throw bad(section, "要是一个对象");
    out[section] = {};
    for (const [key, value] of Object.entries(body)) {
      if ((READ_ONLY[section] || []).includes(key)) continue;
      if (!(key in DEFAULTS[section])) throw bad(`${section}.${key}`, "没有这一项设置");
      if ((NESTED[section] || []).includes(key)) {
        if (!is_plain(value)) throw bad(`${section}.${key}`, "要是一个对象");
        for (const sub of Object.keys(value)) {
          if (!(sub in DEFAULTS[section][key])) throw bad(`${section}.${key}.${sub}`, "没有这一项设置");
        }
      }
      out[section][key] = value;
    }
  }
  return out;
}

function paths_of(obj, prefix = "") {
  const out = new Set();
  for (const [k, v] of Object.entries(obj || {})) {
    const p = prefix ? `${prefix}.${k}` : k;
    out.add(p);
    if (is_plain(v)) for (const q of paths_of(v, p)) out.add(q);
  }
  return out;
}

/**
 * @param file        <LOCI_GATEWAY_DATA>/present.json
 * @param env         LOCI_UPSTREAM and LOCI_UPSTREAM_KEY (the env side of the upstream; the
 *                    key's value is never read out)
 * @param env_base    the URL the gateway was started with, when the caller has it apart
 *                    from env (present/index.js); defaults to LOCI_UPSTREAM
 * @param client_key  () → the key the client sent with its latest accepted request, ""
 *                    when it sent none, null when no request was remembered yet
 *                    (own_turn.js); only its source and masked tail reach the view
 */
function create_settings({ file, env = process.env, env_base = env_base_url(env), client_key = () => null }) {
  function read_file() {
    let last_err = null;
    for (let i = 0; i < READ_TRIES; i++) {
      let text;
      try { text = fs.readFileSync(file, "utf8"); }
      catch (err) {
        if (err.code === "ENOENT") return { state: "defaults", raw: {} };
        last_err = err.message; continue;
      }
      try {
        const raw = JSON.parse(text);
        if (is_plain(raw)) return { state: "ok", raw };
        last_err = "文件里不是一个 JSON 对象";
      } catch (err) { last_err = `不是 JSON（${err.message}）`; }
    }
    return { state: "unreadable", raw: null, error: last_err };
  }

  /** Effective settings, section by section; see the header for each state. */
  function load() {
    const got = read_file();
    if (got.state === "unreadable") {
      return { state: "unreadable", values: clone(DEFAULTS), errors: [{ field: "present.json", error: got.error }], bad_sections: [...SECTIONS] };
    }
    const values = {};
    const errors = [];
    const bad_sections = [];
    for (const section of SECTIONS) {
      const from_file = is_plain(got.raw[section]) ? got.raw[section] : {};
      try { values[section] = VALIDATE[section](deep_merge(DEFAULTS[section], from_file), () => false); }
      catch (err) {
        if (!(err instanceof SettingsError)) throw err;
        values[section] = clone(DEFAULTS[section]);
        errors.push({ field: err.field, error: err.reason });
        bad_sections.push(section);
      }
    }
    return { state: errors.length ? "invalid" : got.state, values, errors, bad_sections };
  }

  /** The upstream URL in force: the panel's, else LOCI_UPSTREAM, else "". */
  function upstream_base(values = load().values) {
    return values.upstream.base_url || env_base || "";
  }

  /** The fixed key in force, { key, from: "panel" | "env" }, or null (see the header). */
  function fixed_key(values = load().values) {
    if (values.upstream.key) return { key: values.upstream.key, from: "panel" };
    const from_env = String(env.LOCI_UPSTREAM_KEY || "").trim();
    return from_env ? { key: from_env, from: "env" } : null;
  }

  function upstream_view(values) {
    const fixed = fixed_key(values);
    let client = null;
    if (!fixed) {
      try { client = client_key(); } catch { client = null; }
    }
    return {
      base_url: values.upstream.base_url,
      env_base_url: env_base,
      in_force: upstream_base(values) || null,
      key_set: Boolean(values.upstream.key),
      key_hint: fixed ? key_hint(fixed.key) : key_hint(client),
      key_from: fixed ? fixed.from : (client === null || client === undefined ? null : "client"),
    };
  }

  /** The settings as GET /present shows them: no Bark code, no key. */
  function view(values = load().values) {
    return {
      compress: values.compress,
      report: values.report,
      wake: values.wake,
      own: { ...values.own, fixed_key_set: Boolean(fixed_key(values)) },
      push: { bark_set: Boolean(values.push.bark), bark_hint: bark_hint(values.push.bark), text: values.push.text },
      upstream: upstream_view(values),
    };
  }

  /**
   * Apply a patch from POST /present.
   * @returns { ok: true } | { ok: false, status: 400 | 409, field, error }
   */
  function patch(patch_body) {
    const got = read_file();
    if (got.state === "unreadable") {
      return { ok: false, status: 409, field: "present.json",
        error: `present.json 读不出来（${got.error}）。去手动改好或者删掉它，网关不会盖掉它` };
    }
    let cleaned;
    try { cleaned = clean_patch(patch_body); }
    catch (err) { if (err instanceof SettingsError) return { ok: false, status: 400, field: err.field, error: err.reason }; throw err; }
    const touched = paths_of(cleaned);
    const next = {};
    try {
      for (const section of SECTIONS) {
        const from_file = is_plain(got.raw[section]) ? got.raw[section] : {};
        const merged = deep_merge(deep_merge(DEFAULTS[section], from_file), cleaned[section] || {});
        next[section] = VALIDATE[section](merged, (p) => touched.has(p));
      }
    } catch (err) {
      if (err instanceof SettingsError) return { ok: false, status: 400, field: err.field, error: err.reason };
      throw err;
    }
    write(next);
    return { ok: true };
  }

  function write(values) {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const tmp = `${file}.tmp`;
    fs.writeFileSync(tmp, `${JSON.stringify(values, null, 2)}\n`, "utf8");
    fs.renameSync(tmp, file);
  }

  /** What wake may run on: its section, or null when it must not wake (see the header). */
  function for_wake() {
    const got = load();
    if (got.state === "unreadable" || got.bad_sections.includes("wake")) return null;
    return got.values.wake;
  }

  return { file, load, view, patch, for_wake, upstream_base, fixed_key };
}

module.exports = {
  create_settings, DEFAULTS, bark_code, bark_hint, upstream_url, upstream_key, key_hint, env_base_url, SettingsError,
};
