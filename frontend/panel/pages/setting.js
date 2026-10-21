/* ==========================================================
   pages/setting.js — setting

   One page of numbered blocks, laid out the way her frame setting is: a model is a form
   of name + box rows (API format as plain words, the chosen one in ink), what is rarely
   changed folded under 自定义设置, and what is read or done once in a glass window (改密码,
   先看一眼, 已导入的, 还没有向量的, 日志). Each block reads on its own, so one that fails
   shows its error in place and the rest still stand. Wired to what the server has:

     宿主      no route lists the hosts or changes their grants: drawn, every control off
     称呼      GET /api/config ai_name / owner_name · POST /api/config {ai_name | owner_name,
               persist}, each box saved when it is left
     账号安全  GET /api/loci/auth/state · 密码's stars open 改密码: POST
               /api/loci/auth/set-password {password, current_password}. No account names:
               账号 is the one there is
     安全问题  POST /api/loci/auth/security-question {question, answer}, sent once both are
               filled (a logged-in session; the answer never comes back, so its box starts
               empty)
     网关上游  the gateway's present.json `upstream` through Loci's proxy (GET
               /api/loci/present `settings.upstream`, POST {patch: {upstream}}): base_url
               ("" = the one the gateway was started with), key (write-only; "" clears it)
               · 测试 POST /api/loci/present/upstream-test
     允许手机连  only under the package launcher: GET /api/loci/phone (absent elsewhere,
               and the block numbers close up) · POST {on}. The launcher restarts Loci and
               the gateway on the network; once both listen there, the gateway URL, the MCP
               URL and its header, each with 复制. Off, the same three at this computer's
               own 127.0.0.1 addresses (`here`), for a client on it. In the Docker image (`fixed`) the block is
               连接地址: no switch, the same three with the server's address left to fill in
     副模型    GET/POST /api/config `dehydration` (its key masked, sent only when typed) ·
               测试 POST /api/test/dehydration · 拉取列表 POST /api/models. API format
               =Gateway is `api_format: "gateway"`: the gateway's upstream and key, only the
               model its own. 本地模型 is `runs_on: "local"`: the local Ollama, no Base URL or
               Key to fill, the model picked from what GET /api/loci/ollama finds. Thinking
               is `thinking`, shown for the formats that have a thinking knob (Gemini,
               Anthropic).
     向量模型  `embedding` the same way (its key too), 测试 POST /api/test/embedding; a
               change of model answers 409 with the cost, which is put to the person and
               sent again with reembed "confirm"; the recompute's progress from GET
               /api/loci/embedding/migration. 本地模型 is the embedding's api_format
               "ollama": the three local steps, each ticked once done (Ollama found, the
               model installed, a test through); pulling a model has no route, so 下载 is
               off. Under it the memories with no vector: GET /api/loci/embedding/missing
               (paged) · 现在补 POST /api/loci/embedding/backfill
     导入      聊天记录: POST /api/import/preflight, then POST /api/import/upload; 已导入的
               GET /api/import/batches, 暂停 POST /api/import/pause, 继续 upload with
               resume, 撤回这一批 POST /api/import/withdraw. 记忆包: POST
               /api/loci/import-package (the file, then the decisions), GET for progress
     导出      GET /api/loci/export · GET /api/loci/export/originals, saved as files
     阈值      GET/POST /api/config `surfacing.awake_*`, `surfacing.hold_review_days`
               (条子默认挂几天) and `thresholds` (with its retune words)
     体检      GET /api/loci/health: the board's five rows (`groups`, the worst of each row's
               checks); a row opens the checks under it · 日志 GET /api/logs (newest first)
     版本      GET /api/loci/version, 检查更新 with ?check=1; 一键更新 has no route: off

   Words are the board's, or the API's own (messages, errors, why_words, say, words).
   ========================================================== */

import * as api from "../api.js";
import { h, fill, pagehead, setBlock, row, btn as plainBtn, sw, inp, num, errorLine, pagedList, clickable, pick, glassWindow } from "../ui.js";
import { openDetail } from "../detail.js";
import { NOT_WIRED, PRESENT, ask } from "../presentform.js";

const TIP = {
  hosts: "宿主就是接进 Loci 的地方，比如 Claude Code。Loci 自己不聊天，是挂在它们上面的。这里列着接进来的有谁、各自有哪些权限。一般只有一个，接了好几处才会有好几行。",
  names: "写记忆的时候用这两个名字，不写 user / assistant。没填的话他叫「AI」。",
  question: "忘记密码的时候用：答对就能重设密码。没设的话，只能在装 Loci 的那台电脑上重设。",
  upstream: "网关把前端发来的消息转给哪个模型服务。不填就用网关启动时设的那个；Key 也可以不填，用前端每次带过来的那把。",
  aux: "副模型不跟你聊天，在后台替 Loci 干杂活：把聊天写成记忆、打标签、整理合并。用便宜、快的就够，不用跟聊天的那个一样。",
  emb: "向量模型把每条记忆变成一串数字，用来找「意思相近」的记忆——recall 里写着「意思相近」的那些，就是靠它找出来的。它只管找，不管写。",
  thresholds: "这些数决定一条记忆什么时候「醒着」，还有两条记忆多像才算「像」。醒着 = 待在 surface 的池子里，更容易被他注意到，但不保证一定会冒出来。默认值是试出来的，一般不用动。",
  health: "每次打开这页都会自己查一遍。哪一行不对，点它后面的「看日志」，下面的日志会跳到那附近。",
};

const RUNS_WHY = "默认是云端模型，自己填写key。本地模型要在电脑上安装ollama，安装一个本地模型。选择本地，base url自动填充，key不出现。";
const LOCAL_FORMATS = ["ollama", "local"];
const OLLAMA_DOWNLOAD = "https://ollama.com/download";

// ---------------------------------------------------------------- small pieces

/** A settings row: name (and its small words) left, the control right. `stack` puts the
 *  control under the words on the phone. */
function setRow({ text, why, right, stack = false }) {
  return row({ text, why, right: right || h("span"), layout: stack ? "big set stack" : "big set" });
}

/** A button of this page: her small glass pill, `dark` for the main one. */
function btn(label, opts = {}) {
  return plainBtn(label, { badge: true, ...opts });
}

function acts(...kids) {
  return h("span", { class: "acts" }, kids);
}

function off(el) {
  el.disabled = true;
  return el;
}

function small(text, style) {
  return h("p", { class: "why", style: { margin: "10px 0 0", lineHeight: "1.6", ...(style || {}) }, text });
}

/** A line under a block that says how a write went: the server's words, or its error. */
function sayer() {
  const el = h("p", { class: "why", role: "status", style: { margin: "10px 0 0", lineHeight: "1.6" }, hidden: true });
  return {
    el,
    ok(text) { el.className = "why"; el.textContent = text || ""; el.hidden = !text; },
    err(e) { el.className = "err"; el.textContent = e && e.message ? e.message : String(e); el.hidden = false; },
    clear() { el.hidden = true; el.textContent = ""; },
  };
}

/** Run `fn` with the button off until it is done. */
async function busy(b, fn) {
  b.disabled = true;
  try { return await fn(); } finally { b.disabled = false; }
}

/** A block filled when `build()` resolves; its error in its place when it throws. It takes
 *  no box of its own (display: contents), so what it holds sits in the block itself. */
function later(build) {
  const box = h("div", { style: { display: "contents" } });
  Promise.resolve().then(build).then((node) => fill(box, node)).catch((e) => {
    if (e && e.status === 401) return;
    fill(box, errorLine(e));
  });
  return box;
}

/** A segmented choice: [[value, words], …]. */
function seg(options, value, { label, onChange, disabled = false } = {}) {
  const el = h("span", { class: "seg", role: "radiogroup", "aria-label": label });
  let cur = value;
  const buttons = options.map(([v, words]) => {
    const b = h("button", { type: "button", role: "radio" }, words);
    b.disabled = disabled;
    b.addEventListener("click", () => {
      if (v === cur) return;
      set(v);
      if (onChange) onChange(v);
    });
    el.append(b);
    return [v, b];
  });
  function set(v) {
    cur = v;
    for (const [k, b] of buttons) {
      b.classList.toggle("on", k === v);
      b.setAttribute("aria-checked", String(k === v));
    }
  }
  set(value);
  return { el, get value() { return cur; }, set };
}

/** The Model drop-down: the configured model, and whatever the list pulled from the
 *  provider adds. */
function modelPicker(current, label) {
  const el = pick({ cls: "btn set sel", label, value: current || "", placeholder: "选一个模型" });
  function setList(models) {
    const keep = el.getValue() || current || "";
    const names = [...new Set([keep, ...(models || [])].filter(Boolean))];
    el.setOptions(names.map((m) => [m, m]), keep);
  }
  setList([]);
  return { el, get value() { return el.getValue(); }, setList };
}

/** A number from a box, or undefined when it holds none. */
function numberIn(box) {
  const raw = String(box.value || "").trim();
  if (!raw) return undefined;
  const v = Number(raw);
  return Number.isFinite(v) ? v : undefined;
}

/** Words to pick one of, plain (her frame setting): the chosen one in ink, the others pale.
 *  [[value, words], …]; `sep` between them (云端模型 / 本地模型). */
function tabs(options, value, { label, onChange, sep = "" } = {}) {
  const el = h("span", { class: sep ? "tabs sep" : "tabs", role: "radiogroup", "aria-label": label });
  let cur = value;
  const buttons = options.map(([v, words], i) => {
    const b = h("button", { class: "tab", type: "button", role: "radio" }, words);
    b.addEventListener("click", () => {
      if (v === cur) return;
      set(v);
      if (onChange) onChange(v);
    });
    if (i && sep) el.append(h("span", { class: "tsep", "aria-hidden": "true", text: sep }));
    el.append(b);
    return [v, b];
  });
  function set(v) {
    cur = v;
    for (const [k, b] of buttons) {
      b.classList.toggle("on", k === v);
      b.setAttribute("aria-checked", String(k === v));
    }
  }
  set(value);
  return { el, get value() { return cur; }, set };
}

/** A row of a model's form: its name left, what is filled in right. */
function frow(name, ...field) {
  return h("div", { class: "frow" }, h("span", { class: "fn", text: name }), h("div", { class: "fv" }, field));
}

/** A name and its box, side by side (他叫 · 账号 · 问题). */
function kv(name, box) {
  return h("label", { class: "kv" }, h("span", { class: "fn", text: name }), box);
}

/** Something folded behind a small link (自定义设置, 导入之后会怎样), plain when open. */
function fold(label, content, { open = false } = {}) {
  const body = h("div", { class: "foldbody" }, content);
  const toggle = h("button", { class: "lnk fold", type: "button", text: label });
  const set = (on) => {
    toggle.setAttribute("aria-expanded", String(on));
    body.hidden = !on;
  };
  toggle.addEventListener("click", () => set(body.hidden));
  set(open);
  return h("div", { class: "folded" }, toggle, body);
}

/** A small link that opens a glass window. */
function opener(label, open) {
  const b = h("button", { class: "lnk", type: "button", text: label });
  b.addEventListener("click", open);
  return b;
}

/** A step of the local set-up, its number ticked once it is done. */
function tick(noEl, done, n) {
  noEl.classList.toggle("ok", done);
  noEl.textContent = done ? "✓" : n;
  if (done) noEl.setAttribute("aria-label", "做完了"); else noEl.removeAttribute("aria-label");
}

/** The 测试 / 保存 pair under a model's form, what the test said at their left. */
function testSave(onTest, onSave, said) {
  const test = btn("测试");
  const save = btn("保存", { dark: true });
  test.addEventListener("click", () => busy(test, onTest));
  save.addEventListener("click", () => busy(save, onSave));
  return h("div", { class: "acts setacts" }, said, test, save);
}

/** What a model's test answered, beside its buttons. */
function testWords(said, r) {
  said.className = r.ok ? "why" : "err";
  said.textContent = r.ok ? (r.message || "通了") : `不通：${r.error || ""}`;
}

function bar(done, total) {
  const pct = total > 0 ? Math.max(0, Math.min(100, Math.round((done / total) * 100))) : 0;
  return h("div", { class: "prog", role: "progressbar", "aria-valuenow": String(pct), "aria-valuemin": "0", "aria-valuemax": "100" },
    h("span", { style: { width: `${pct}%` } }));
}

/** Call `fn` every `ms` while `el` is on the page and `fn` says to go on. */
function poll(el, fn, ms = 3000) {
  const tick = async () => {
    if (!el.isConnected) return;
    let again = false;
    try { again = await fn(); } catch (_) { again = false; }
    if (again && el.isConnected) setTimeout(tick, ms);
  };
  setTimeout(tick, ms);
}

// ---------------------------------------------------------------- 宿主

function hostsRows() {
  return [
    setRow({ text: "能碰哪些来源", right: acts(off(btn("▾"))) }),
    setRow({ text: "能恢复撤回的来源", right: acts(off(sw({ checked: false, label: "能恢复撤回的来源" }))) }),
    setRow({ text: "不说用途也能读整个库", right: acts(off(sw({ checked: false, label: "不说用途也能读整个库" }))) }),
    small("一般不用改。改错了，宿主可能读不到该读的记忆，或者读到不该读的。真要改，改之前要再输一次面板密码。"),
    h("div", { class: "acts setacts" }, off(btn("保存", { dark: true }))),
  ];
}

// ---------------------------------------------------------------- 称呼

/** 他叫 / 你叫, each saved when it is left with something new in it. */
async function namesBlock(cfgP) {
  const cfg = await cfgP;
  const s = sayer();
  const box = (key, label, placeholder) => {
    const el = inp({ class: "inp kvbox name", value: cfg[key] || "", placeholder, "aria-label": label, maxlength: "40" });
    el.addEventListener("change", async () => {
      s.clear();
      try {
        await api.post("/api/config", { [key]: el.value.trim(), persist: true });
        cfg[key] = el.value.trim();
      } catch (e) { s.err(e); }
    });
    return el;
  };
  return h("div", null,
    h("div", { class: "duo" }, kv("他叫", box("ai_name", "他叫什么", "AI")), kv("你叫", box("owner_name", "你叫什么", "人类"))),
    s.el);
}

// ---------------------------------------------------------------- 账号安全 · 安全问题

/** 改密码 in a glass window, in the forgot-password board's words: the password now (once
 *  there is one; before the first, only the new one), the new one twice. */
function passwordWindow(st, stars) {
  const win = glassWindow({ title: "改密码" });
  const current = inp({ type: "password", "aria-label": "密码", autocomplete: "current-password" });
  const pw1 = inp({ type: "password", "aria-label": "新密码", placeholder: "至少 6 位", autocomplete: "new-password" });
  const pw2 = inp({ type: "password", "aria-label": "再输一次新密码", autocomplete: "new-password" });
  const err = h("p", { class: "err", role: "alert", style: { margin: "0" }, hidden: true });
  const done = sayer();
  const save = btn("保存", { dark: true, type: "submit" });
  const form = h("form", { class: "pwbox", novalidate: true },
    st.setup_needed ? null : field("密码", current),
    field("新密码", pw1), field("再输一次", pw2), err,
    h("div", { class: "acts end" }, save), done.el);
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    err.hidden = true;
    done.clear();
    if (pw1.value !== pw2.value) { err.textContent = "两次不一样"; err.hidden = false; return; }
    busy(save, async () => {
      try {
        const body = { password: pw1.value };
        if (!st.setup_needed) body.current_password = current.value;
        const r = await api.post("/api/loci/auth/set-password", body, { gate: false });
        for (const b of [current, pw1, pw2]) b.value = "";
        stars.textContent = "********";
        st.setup_needed = false;
        if (r.env_locked) done.ok(r.next); else win.close();
      } catch (x) {
        err.textContent = x.message;
        err.hidden = false;
      }
    });
  });
  fill(win.body, form);
  (st.setup_needed ? pw1 : current).focus();
}

function field(label, input) {
  return h("div", { class: "fld" }, h("span", { class: "fl", text: label }), input);
}

/** 账号 (the one name there is) and 密码 (its stars open 改密码). */
async function accountBlock(authP) {
  const st = await authP;
  const has = !!(st.has_file_password || st.env_locked);
  const stars = h("button", { class: "inp kvbox ro", type: "button", "aria-label": "改密码", text: has ? "********" : "还没设，点这里设" });
  stars.addEventListener("click", () => passwordWindow(st, stars));
  return h("div", null,
    kv("账号", h("span", { class: "inp kvbox ro", text: "user" })),
    kv("密码", stars));
}

/** The question as it is set, and its answer, saved once both are filled. The answer is
 *  kept only as a hash, so its box starts empty. */
async function questionBlock(authP) {
  const st = await authP;
  const set = "设好了，不显示";
  const question = inp({ class: "inp kvbox", value: st.question || "", placeholder: "例：你的小学叫什么？", "aria-label": "安全问题", maxlength: "200" });
  const answer = inp({ class: "inp kvbox", placeholder: st.question ? set : "", "aria-label": "答案", maxlength: "200", autocomplete: "off" });
  const s = sayer();
  const send = async () => {
    const q = question.value.trim();
    const a = answer.value.trim();
    if (!q || !a) return;
    s.clear();
    try {
      const r = await api.post("/api/loci/auth/security-question", { question: q, answer: a });
      question.value = r.question || q;
      answer.value = "";
      answer.placeholder = set;
      s.ok(r.message);
    } catch (e) { s.err(e); }
  };
  question.addEventListener("change", send);
  answer.addEventListener("change", send);
  return h("div", null, kv("问题", question), kv("答案", answer), s.el);
}

// ---------------------------------------------------------------- 网关上游

/** The key in force and where it comes from (key_from: panel | env | client | null), as
 *  the key box's placeholder. */
function upstreamKeyWords(u) {
  const from = { panel: "这里填的", env: "环境变量里的", client: "前端上次发来的" }[u.key_from];
  if (from) return `${u.key_hint || "********"}（${from}）`;
  return "可以不填：用前端发来的那把";
}

/** The gateway's upstream (present.json `upstream`, through Loci's proxy): the Base URL
 *  (empty = the one the gateway was started with), a fixed key (write-only), 测试. */
async function upstreamBlock(snapP) {
  const snap = await snapP;
  const live = Boolean(snap && snap.connected === true);
  let u = live && snap.settings ? snap.settings.upstream || {} : {};
  const s = sayer();
  const said = h("span", { class: "why" });
  const base = inp({ "aria-label": "API Base URL", value: u.base_url || "" });
  const key = inp({ type: "password", "aria-label": "API key", autocomplete: "off" });
  const inForce = h("span", { class: "why" });
  const clear = h("button", { class: "lnk", type: "button", text: "清掉" });
  const draw = () => {
    base.placeholder = u.env_base_url || "例：https://api.deepseek.com/v1";
    key.placeholder = upstreamKeyWords(u);
    inForce.textContent = !base.value.trim() && u.in_force ? `现在用的：${u.in_force}` : "";
    clear.hidden = !u.key_set;
  };
  const send = async (upstream) => {
    s.clear();
    try {
      const r = await api.post(PRESENT, { patch: { upstream } });
      u = (r.settings && r.settings.upstream) || u;
      key.value = "";
      draw();
      return true;
    } catch (e) {
      if (!(e && e.status === 401)) s.err(e);
      return false;
    }
  };
  clear.addEventListener("click", () => busy(clear, () => send({ key: "" })));
  const save = () => {
    const upstream = { base_url: base.value.trim() };
    if (key.value.trim()) upstream.key = key.value.trim();
    return send(upstream);
  };
  const test = async () => {
    s.clear();
    try {
      const r = await api.post(`${PRESENT}/upstream-test`, {});
      testWords(said, r.ok ? { ok: true, message: r.models ? `通了 · 有 ${r.models.length} 个模型` : "通了" } : { ok: false, error: r.error || r.status });
    } catch (e) { if (!(e && e.status === 401)) testWords(said, { ok: false, error: e.message }); }
  };
  base.addEventListener("input", draw);
  draw();
  const buttons = testSave(test, save, said);
  if (!live) for (const el of [base, key, ...buttons.querySelectorAll("button")]) el.disabled = true;
  return h("div", null,
    live ? null : small(snap && snap.error ? snap.error : NOT_WIRED, { marginTop: "0" }),
    h("div", { class: "fgrid" },
      frow("API Base URL", base, inForce),
      frow("API key", key, clear)),
    buttons, s.el);
}

// ---------------------------------------------------------------- 副模型 · 向量模型

/** What GET /api/loci/ollama finds, asked once per page and again on request: both local
 *  modes read it. A failed ask reads as "not found". */
function ollamaFinder() {
  let p = null;
  return (again = false) => {
    if (!p || again) {
      p = api.get("/api/loci/ollama").catch((e) => ({ running: false, base_url: "", models: [], error: e.message }));
    }
    return p;
  };
}

function ollamaWords(o) {
  return o.running ? `找到了，装了 ${(o.models || []).length} 个模型` : "没找到 Ollama";
}

/** Whether Ollama holds `model` (bge-m3 is listed as bge-m3:latest). */
function installed(o, model) {
  return !!model && (o.models || []).some((m) => m === model || m.startsWith(`${model}:`));
}

/** The formats whose API has a thinking knob the side model turns (core/dehydrator). */
const THINKING_FORMATS = ["gemini", "anthropic"];

/** 云端模型/本地模型, the first row under 自定义设置 of both models. */
function runsRow(runs, onChange) {
  const t = tabs([["cloud", "云端模型"], ["local", "本地模型"]], runs, { label: "云端模型/本地模型", sep: "/", onChange });
  return { t, row: setRow({ text: "云端模型/本地模型", why: RUNS_WHY, stack: true, right: t.el }) };
}

function auxModel(cfg, findOllama, snapP) {
  const d = cfg.dehydration || {};
  let runs = d.runs_on === "local" ? "local" : "cloud";
  const s = sayer();
  const said = h("span", { class: "why", text: d.model ? "" : "还没配" });
  const fmt = tabs([["openai_compat", "OpenAI"], ["gemini", "Gemini"], ["anthropic", "Anthropic"], ["gateway", "=Gateway"]],
    d.api_format || "openai_compat", { label: "API format", onChange: () => layout() });
  const base = inp({ placeholder: "例：https://api.deepseek.com", "aria-label": "API Base URL", value: d.api_format === "gateway" ? "" : (d.base_url || "") });
  const key = inp({ type: "password", placeholder: d.api_key_masked ? "********" : "粘贴 API Key", "aria-label": "API key", autocomplete: "off" });
  // Under =Gateway the two boxes are the gateway's: what it forwards to, pale.
  const gwBase = h("span", { class: "fixed" });
  const gwKey = h("span", { class: "fixed", text: "用网关那把" });
  const localBase = h("span", { class: "fixed" });
  snapP.then((snap) => {
    const u = snap && snap.connected === true && snap.settings ? snap.settings.upstream || {} : null;
    gwBase.textContent = u ? (u.in_force || "网关还没有上游") : "网关还没接上";
  }).catch(() => {});
  const model = modelPicker(d.model, "Model");
  const pull = h("button", { class: "lnk small", type: "button" }, "从 Base URL 拉取列表");
  const mine = h("span", { class: "why", text: "列的是这台电脑上已经装了的" });
  pull.addEventListener("click", () => busy(pull, async () => {
    s.clear();
    try {
      const gw = fmt.value === "gateway";
      const r = await api.post("/api/models", { api_key: gw ? "" : (key.value.trim() || "__use_current__"),
        base_url: gw ? "" : base.value.trim(), api_format: fmt.value });
      if (r.ok) model.setList(r.models); else s.err(new Error(r.error));
    } catch (e) { s.err(e); }
  }));
  const maxTok = num({ value: d.max_tokens ?? "", "aria-label": "输出上限" });
  const temp = num({ value: d.temperature ?? "", "aria-label": "Temperature" });
  const timeout = num({ value: d.timeout_seconds ?? "", "aria-label": "超时" });
  const thinking = sw({ checked: !!d.thinking, label: "Thinking" });

  const where = runsRow(runs, (v) => { runs = v; layout(); if (v === "local") findLocal(); });
  const thinkRow = setRow({ text: "Thinking", why: "只有会先想再答的模型有这一项，干杂活一般关着", right: acts(thinking) });
  const custom = fold("自定义设置", [
    where.row,
    setRow({ text: "输出上限", why: "一次最多输出多少。（如果 开了thinking，要给大一点。）", right: acts(maxTok) }),
    setRow({ text: "Temperature", why: "越低越稳，整理记忆用低的", right: acts(temp) }),
    thinkRow,
    setRow({ text: "等多久算超时", right: acts(timeout, h("span", { class: "why", text: "秒" })) }),
  ], { open: runs === "local" });

  const fmtRow = frow("API format", fmt.el);
  const baseRow = frow("API Base URL", base, gwBase, localBase);
  const keyRow = frow("API key", key, gwKey);

  let found = null;
  /** Local: what Ollama holds fills the Model list. */
  async function findLocal(again = false) {
    found = await findOllama(again);
    localBase.textContent = found.base_url ? `${found.base_url}（自动填的）` : "";
    model.setList(found.models || []);
    layout();
  }

  /** Cloud shows the provider's boxes, =Gateway the gateway's in their place; local hides
   *  the format and the key and gives the address it found. Thinking shows only where it
   *  does something. */
  function layout() {
    const local = runs === "local";
    const gw = !local && fmt.value === "gateway";
    fmtRow.hidden = local;
    keyRow.hidden = local;
    base.hidden = local || gw;
    key.hidden = gw;
    gwBase.hidden = !gw;
    gwKey.hidden = !gw;
    localBase.hidden = !local;
    pull.hidden = local;
    pull.textContent = gw ? "从网关拉取列表" : "从 Base URL 拉取列表";
    mine.hidden = !local;
    thinkRow.hidden = local || !THINKING_FORMATS.includes(fmt.value);
    if (local && found && !said.textContent) said.textContent = ollamaWords(found);
  }

  async function test() {
    s.clear();
    try { testWords(said, await api.post("/api/test/dehydration", {})); } catch (e) { testWords(said, { ok: false, error: e.message }); }
  }
  async function save() {
    s.clear();
    const dehy = { runs_on: runs, model: model.value };
    if (runs === "cloud") {
      dehy.api_format = fmt.value;
      if (fmt.value !== "gateway") {
        dehy.base_url = base.value.trim();
        if (key.value.trim()) dehy.api_key = key.value.trim();
      }
    }
    if (!thinkRow.hidden) dehy.thinking = thinking.getAttribute("aria-checked") === "true";
    const mt = numberIn(maxTok), t = numberIn(temp), to = numberIn(timeout);
    if (mt !== undefined) dehy.max_tokens = Math.round(mt);
    if (t !== undefined) dehy.temperature = t;
    if (to !== undefined) dehy.timeout_seconds = to;
    try {
      const r = await api.post("/api/config", { dehydration: dehy, persist: true });
      if (dehy.api_key) { key.value = ""; key.placeholder = "********"; d.api_key_masked = "********"; }
      if (said.textContent === "还没配") said.textContent = "";
      layout();
      s.ok(r.message);
    } catch (e) { s.err(e); }
  }

  layout();
  if (runs === "local") findLocal();
  return h("div", null,
    h("div", { class: "fgrid" }, fmtRow, baseRow, keyRow,
      frow("Model", model.el, pull, mine)),
    custom,
    testSave(test, save, said),
    s.el);
}

/** The recompute after a change of embedding model, while there is one to show. */
function migrationLine() {
  const el = h("div");
  async function load() {
    const m = await api.get("/api/loci/embedding/migration");
    if (!m || m.phase === "idle") { fill(el); return false; }
    fill(el,
      m.message ? small(m.message) : null,
      m.running && m.total ? [bar(m.done, m.total), h("p", { class: "why", style: { margin: "6px 0 0" }, text: `${m.done} / ${m.total}` })] : null,
      m.error ? h("p", { class: "err", style: { margin: "6px 0 0" }, text: m.error }) : null);
    return !!m.running;
  }
  load().then((again) => { if (again) poll(el, load); }).catch((e) => fill(el, errorLine(e)));
  return { el, reload: () => load().then((again) => { if (again) poll(el, load); }) };
}

function embModel(cfg, findOllama) {
  const e = cfg.embedding || {};
  const wasLocal = LOCAL_FORMATS.includes(String(e.api_format || "").toLowerCase());
  const holder = h("div");
  const migration = migrationLine();
  let customOpen = wasLocal;

  /** Save the embedding settings; a change of model first puts its cost to the person.
   *  True once the server took them. */
  async function saveEmbedding(body, s) {
    s.clear();
    try {
      const r = await api.post("/api/config", { embedding: body, persist: true });
      s.ok(r.message);
      migration.reload();
      return true;
    } catch (x) {
      if (x.status === 409 && x.body && x.body.needs_confirmation && x.body.reembed) {
        const rp = x.body.reembed;
        const ask = [rp.say, rp.while_running].filter(Boolean).join("\n\n");
        if (!window.confirm(ask)) return false;
        try {
          const r = await api.post("/api/config", { embedding: { ...body, reembed: "confirm" }, persist: true });
          s.ok(r.message);
          migration.reload();
          return true;
        } catch (y) { s.err(y); }
        return false;
      }
      s.err(x);
      return false;
    }
  }

  /** The test; true when it went through. */
  async function test(said) {
    try {
      const r = await api.post("/api/test/embedding", {});
      testWords(said, r);
      return !!r.ok;
    } catch (x) {
      testWords(said, { ok: false, error: x.message });
      return false;
    }
  }

  function custom(mode, dimWhy, timeout, steps) {
    const where = runsRow(mode, show);
    const box = fold("自定义设置", [
      where.row,
      steps || null,
      setRow({ text: "Dimensions", why: dimWhy, right: h("span", { class: "r", text: "自动" }) }),
      setRow({ text: "等多久算超时", right: acts(timeout, h("span", { class: "why", text: "秒" })) }),
    ], { open: customOpen });
    box.querySelector("button").addEventListener("click", () => { customOpen = !customOpen; });
    return box;
  }

  function cloud() {
    const said = h("span", { class: "why", text: e.enabled && !wasLocal ? "" : "还没配" });
    const s = sayer();
    const fmt = tabs([["openai_compat", "OpenAI"], ["gemini", "Gemini"]],
      !wasLocal && e.api_format === "gemini" ? "gemini" : "openai_compat", { label: "API format" });
    const base = inp({ placeholder: "例：https://api.siliconflow.cn/v1", "aria-label": "API Base URL", value: wasLocal ? "" : (e.base_url || "") });
    const key = inp({ type: "password", placeholder: e.api_key_masked ? "********" : "粘贴 API Key", "aria-label": "API key", autocomplete: "off" });
    const model = modelPicker(wasLocal ? "" : e.model, "Model");
    const pull = h("button", { class: "lnk small", type: "button" }, "从 Base URL 拉取列表");
    pull.addEventListener("click", () => busy(pull, async () => {
      s.clear();
      try {
        const r = await api.post("/api/models", { api_key: key.value.trim() || "__use_current_embed__", base_url: base.value.trim(),
          api_format: fmt.value === "gemini" ? "gemini_embed" : fmt.value });
        if (r.ok) model.setList(r.models); else s.err(new Error(r.error));
      } catch (x) { s.err(x); }
    }));
    const timeout = num({ value: e.timeout_seconds ?? "", "aria-label": "超时" });
    const save = () => {
      const body = { enabled: !!model.value, api_format: fmt.value, base_url: base.value.trim(), model: model.value };
      const to = numberIn(timeout);
      if (to !== undefined) body.timeout_seconds = to;
      if (key.value.trim()) body.api_key = key.value.trim();
      return saveEmbedding(body, s).then((ok) => {
        if (ok && body.api_key) { key.value = ""; key.placeholder = "********"; e.api_key_masked = "********"; }
        if (ok && said.textContent === "还没配") said.textContent = "";
      });
    };
    return [
      h("div", { class: "fgrid" },
        frow("API format", fmt.el),
        frow("API Base URL", base),
        frow("API key", key),
        frow("Model", model.el, pull)),
      custom("cloud", "一条记忆变成多少个数；测试的时候自己认出来，不用填", timeout),
      small(REEMBED_WORDS),
      testSave(() => test(said), save, said),
      s.el,
    ];
  }

  function local() {
    const said = h("span", { class: "why" });
    const s = sayer();
    const model = modelPicker(wasLocal && e.model ? e.model : "bge-m3", "Model");
    const timeout = num({ value: e.timeout_seconds ?? "", "aria-label": "超时" });
    const step = (no, title, why, right) => h("div", { class: "step" },
      h("span", { class: "no", text: no }),
      h("div", null, h("div", { class: "c", text: title }), h("div", { class: "why", style: { marginTop: "4px" }, text: why })),
      right);
    // Each step is ticked once it is done: Ollama found, the model installed, a test that
    // went through. With the first two done and local saved already, the test runs by
    // itself, so a set-up that works shows three ticks.
    const foundWords = h("span", { class: "why", text: "找到了", hidden: true });
    const stepTest = btn("测试");
    const first = step("1", "装 Ollama", "它是在你电脑上跑模型的小程序。装好打开就行，之后它自己在后台待着。",
      acts(foundWords, h("a", { class: "btn set badge", href: OLLAMA_DOWNLOAD, target: "_blank", rel: "noopener noreferrer", text: "去下载 Ollama" })));
    const second = step("2", "下一个向量模型", "推荐 bge-m3：中文好，大约 1.2 GB。不用开命令行，点「下载」就行。",
      acts(off(h("button", { class: "lnk", type: "button", text: "换一个" })), off(btn("下载 bge-m3", { dark: true }))));
    const third = step("3", "测一下", "下完会自动选上，点「测试」看通不通。", acts(stepTest));
    const [n1, n2, n3] = [first, second, third].map((el) => el.querySelector(".no"));
    const runTest = async () => tick(n3, await test(said), "3");
    stepTest.addEventListener("click", () => busy(stepTest, runTest));
    const baseText = h("span", { class: "fixed", text: "http://localhost:11434（自动填的）" });
    findOllama().then((o) => {
      said.textContent = ollamaWords(o);
      if (o.base_url) baseText.textContent = `${o.base_url}（自动填的）`;
      foundWords.hidden = !o.running;
      tick(n1, !!o.running, "1");
      tick(n2, !!o.running && installed(o, model.value), "2");
      model.setList(o.models || []);
      if (wasLocal && o.running && installed(o, model.value)) busy(stepTest, runTest);
    });
    const save = () => {
      const body = { enabled: true, api_format: "ollama", base_url: "", model: model.value };
      const to = numberIn(timeout);
      if (to !== undefined) body.timeout_seconds = to;
      return saveEmbedding(body, s);
    };
    return [
      h("div", { class: "fgrid" },
        frow("API Base URL", baseText),
        frow("Model", model.el, h("span", { class: "why", text: "列的是这台电脑上已经装了的" }))),
      custom("local", "测试的时候自己认出来，不用填", timeout, h("div", { class: "steps" }, first, second, third)),
      small(REEMBED_WORDS),
      testSave(runTest, save, said),
      s.el,
    ];
  }

  function show(mode) {
    fill(holder, mode === "local" ? local() : cloud(), migration.el);
  }
  show(wasLocal ? "local" : "cloud");
  return holder;
}

const REEMBED_WORDS = "配好以后再换模型，全部记忆要重新算一遍向量；换之前会先告诉你要算多久。";

// ---------------------------------------------------------------- 补向量 (inside 向量模型)

/** How many memories still have no vector, 现在补, and 查看是哪几条 in a glass window. */
async function vectorRows() {
  const count = h("span");
  const note = h("p", { class: "why", style: { margin: "0 0 4px" }, hidden: true });
  const s = sayer();
  const now = btn("现在补", { dark: true });
  const show = (reply) => {
    count.textContent = `${reply.missing ?? reply.total ?? 0} 条还没有向量`;
    note.textContent = reply.note || "";
    note.hidden = !reply.note;
  };
  show(await api.get("/api/loci/embedding/missing"));
  const look = opener("查看是哪几条", async () => {
    const win = glassWindow({ title: "还没有向量的" });
    try {
      const list = await pagedList({
        load: (q) => api.get("/api/loci/embedding/missing", q),
        item: (it) => row({ text: it.text || `#${it.short}`, open: () => openDetail(it.id),
          right: h("span", { class: "r wrap", text: it.why_words || "" }), layout: "set" }),
      });
      fill(win.body, list.total ? list.el : small("一条都不缺。", { marginTop: "0" }),
        small("在排队 = 等着轮到它；一直失败 = 试了几次都没成，多半是向量模型没配好，或者网断了。"));
    } catch (e) { if (!(e && e.status === 401)) fill(win.body, errorLine(e)); }
  });
  now.addEventListener("click", () => busy(now, async () => {
    s.clear();
    try {
      await api.post("/api/loci/embedding/backfill", {});
      show(await api.get("/api/loci/embedding/missing"));
    } catch (e) { s.err(e); }
  }));
  return h("div", { class: "vecrows" },
    row({ text: "", lead: count, why: "「现在补」= 不等后台了，马上把缺的这几条算上", right: acts(look, now), layout: "big set" }),
    note, s.el);
}

// ---------------------------------------------------------------- 导入

function fileInput(accept, onFile) {
  const input = h("input", { type: "file", accept, hidden: true });
  input.addEventListener("change", () => {
    const f = input.files && input.files[0];
    input.value = "";
    if (f) onFile(f);
  });
  return input;
}

const HOW = [
  ["先看一眼", "传上去先不动，告诉你这份里有几段对话、几条消息，大概要叫副模型多少次、花多少 token。还会问一句：那边的 AI 是不是他——是，那边的经历就记成他自己的；不是，就当成他读到的一份记录。"],
  ["① 原话先存下", "整份聊天原样存成「来源」，当天就能搜到原话。这时候还不算记忆。"],
  ["② 副模型打草稿", "把每段对话切成一小段一小段，每段先起一条草稿。草稿也还不是记忆；breath 的「惦记的事」里会写「有 N 段导入的原话还没核」。可以暂停，回来接着打。"],
  ["③ 他自己核", "他有空的时候读原话和草稿，补上漏掉的，自己写成记忆。不会并进已经有的记忆里。"],
];
const WITHDRAW_WORDS = "撤回 = 这一批的原话、草稿、从它长出来的记忆全部清掉，库回到导入之前的样子。点了会再问你一次。";

/** 已导入的, in a glass window: one row per batch, with what can be done to it now. */
function batchesWindow() {
  const win = glassWindow({ title: "已导入的" });
  const s = sayer();
  const el = h("div");
  fill(win.body, el, s.el);
  async function load() {
    const r = await api.get("/api/import/batches");
    const batches = r.batches || [];
    fill(el, batches.length ? [batches.map(batchRow), small(WITHDRAW_WORDS)] : small("还没导入过。", { marginTop: "0" }));
    return batches.some((b) => b.is_running);
  }
  function again() {
    return load().then((more) => { if (more) poll(el, load); }).catch((e) => { if (!(e && e.status === 401)) fill(el, errorLine(e)); });
  }
  function batchRow(b) {
    const failed = (b.failures || []).length + (b.failures_more || 0);
    const drafting = `打草稿中 ${b.drafted || 0} / ${b.conversations || 0}`;
    const buttons = [];
    let state = "";
    if (b.is_running) {
      state = drafting;
      const pause = btn("暂停");
      pause.addEventListener("click", () => busy(pause, async () => {
        try { await api.post("/api/import/pause", {}); } catch (e) { s.err(e); }
        again();
      }));
      buttons.push(pause);
    } else if (b.status === "drafted") {
      state = "草稿打完了，等他核";
    } else if (b.status !== "withdrawing") {
      const stopped = b.status === "failed" || b.status === "partial";
      state = failed ? `有 ${failed} 段没打成` : (stopped ? "" : drafting);
      const resume = btn("继续");
      resume.addEventListener("click", () => busy(resume, async () => {
        s.clear();
        const fd = new FormData();
        fd.append("resume", "1");
        fd.append("batch", b.batch);
        try { await api.postForm("/api/import/upload", fd); } catch (e) { s.err(e); }
        again();
      }));
      buttons.push(resume);
    }
    const withdraw = btn("撤回这一批");
    withdraw.addEventListener("click", () => {
      if (!window.confirm(WITHDRAW_WORDS.replace("点了会再问你一次。", "").trim())) return;
      busy(withdraw, async () => {
        s.clear();
        try {
          const r = await api.post("/api/import/withdraw", { batch: b.batch });
          if (r.note) s.ok(r.note);
        } catch (e) { s.err(e); }
        again();
      });
    });
    buttons.push(withdraw);
    const day = String(b.created_at || "").slice(0, 10);
    const line = [day, `${b.conversations || 0} 段对话`, state].filter(Boolean).join(" · ");
    // Why drafting stopped, in the server's words, when no conversation is named for it.
    const stoppedWhy = !failed && !b.is_running && (b.errors || []).length ? b.errors[0] : null;
    const why = stoppedWhy ? h("span", { style: { display: "flex", flexDirection: "column", gap: "2px" } },
      h("span", { text: line }), h("span", { text: stoppedWhy })) : line;
    return setRow({ text: b.filename || b.batch, why, stack: true,
      right: h("span", { class: "acts", style: { flexWrap: "nowrap" } }, buttons) });
  }
  again();
}

/** 导入聊天记录: the file is looked at first (先看一眼, in a glass window), then stored once
 *  the question is answered. */
function chatImport(s) {
  const take = async (win, file, sameSelf) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("same_self", sameSelf ? "1" : "0");
    try {
      await api.postForm("/api/import/upload", fd);
      win.close();
      batchesWindow();
    } catch (e) { win.close(); s.err(e); }
  };
  const input = fileInput(".json,.md,.markdown,.txt", async (file) => {
    s.clear();
    const fd = new FormData();
    fd.append("file", file);
    let p;
    try { p = await api.postForm("/api/import/preflight", fd); } catch (e) { s.err(e); return; }
    if (!p.ok) { s.err(new Error((p.warnings || []).join("；") || p.error || "")); return; }
    const win = glassWindow({ title: "先看一眼" });
    const yes = btn("是", { dark: true });
    const no = btn("不是");
    const cancel = btn("取消");
    yes.addEventListener("click", () => busy(yes, () => take(win, file, true)));
    no.addEventListener("click", () => busy(no, () => take(win, file, false)));
    cancel.addEventListener("click", () => win.close());
    fill(win.body,
      h("p", { class: "why", style: { margin: "0", lineHeight: "1.6" },
        text: [file.name, `${p.conversations_count} 段对话`, `${p.turns_count} 条消息`,
          `副模型 ${p.estimated_api_calls} 次`, `${p.estimated_tokens} token`].join(" · ") }),
      h("p", { class: "c", style: { margin: "14px 0 0", whiteSpace: "normal" }, text: "那边的 AI 是不是他" }),
      small(HOW[0][1].split("——")[1] || "", { marginTop: "4px" }),
      h("div", { class: "acts setacts" }, cancel, no, yes));
  });
  const pick = btn("导入");
  pick.addEventListener("click", () => input.click());
  return setRow({ text: "导入聊天记录", stack: true,
    why: "从别的地方搬聊天过来。能收：ChatGPT 导出的 JSON，Claude 导出的 JSON，一列 role / content 的消息 JSON，Markdown（.md），纯文本（.txt）。",
    right: acts(pick, input) });
}

/** 导入记忆包: the package is read first; where it collides with this library, each one is
 *  asked 跳过 / 覆盖 / 两个都留; then it is written in the background. */
function packageImport() {
  const area = h("div");
  const s = sayer();

  // A parse that failed was said where the file was given; on opening the page only a
  // write's failure is shown (a write leaves its job on disk).
  let applied = false;
  async function progress() {
    const st = await api.get("/api/loci/import-package");
    const p = st.apply_progress || {};
    const working = st.phase === "applying" || st.phase === "reindexing";
    const refused = (st.refused || []).map((r) => r.say).filter(Boolean);
    fill(area,
      working && p.total ? [bar(p.done, p.total), h("p", { class: "why", style: { margin: "6px 0 0" }, text: `${p.done} / ${p.total}` })] : null,
      st.error && (applied || st.job) ? h("p", { class: "err", style: { margin: "6px 0 0" }, text: st.error }) : null,
      !working && st.job && st.job.say ? small(st.job.say) : null,
      refused.map((t) => small(t, { marginTop: "4px" })));
    return working;
  }
  const watch = () => progress().then((more) => { if (more) poll(area, progress); }).catch((e) => fill(area, errorLine(e)));

  async function apply(jobId, decisions) {
    s.clear();
    applied = true;
    try {
      await api.post("/api/loci/import-package", { job_id: jobId, decisions, default: "skip" });
    } catch (e) { s.err(e); }
    watch();
  }

  const input = fileInput(".zip", async (file) => {
    s.clear();
    fill(area);
    const fd = new FormData();
    fd.append("file", file);
    let st;
    try { st = await api.postForm("/api/loci/import-package", fd); } catch (e) { s.err(e); return; }
    const conflicts = st.conflicts || [];
    if (!conflicts.length) { apply(st.job_id, {}); return; }
    const choices = conflicts.map((c) => [c.bucket_id,
      seg([["skip", "跳过"], ["overwrite", "覆盖"], ["keep_both", "两个都留"]], "skip", { label: c.import_name || c.bucket_id })]);
    const win = glassWindow({ title: "导入记忆包" });
    const cancel = btn("取消");
    const go = btn("导入记忆包", { dark: true });
    cancel.addEventListener("click", () => busy(cancel, async () => {
      try { await api.post("/api/loci/import-package", { job_id: st.job_id, cancel: true }); } catch (e) { s.err(e); }
      win.close();
    }));
    go.addEventListener("click", () => busy(go, async () => {
      win.close();
      await apply(st.job_id, Object.fromEntries(choices.map(([id, sg]) => [id, sg.value])));
    }));
    fill(win.body,
      st.integrity_warning ? small(st.integrity_warning, { marginTop: "0" }) : null,
      small("跟这边重了的，一条一条选：跳过 / 覆盖 / 两个都留。", { marginTop: "0" }),
      conflicts.map((c, i) => setRow({ text: c.import_name || c.bucket_id, stack: true,
        why: c.current_name && c.current_name !== c.import_name ? c.current_name : null,
        right: acts(choices[i][1].el) })),
      h("div", { class: "acts setacts" }, cancel, go));
  });
  const pick = btn("导入记忆包");
  pick.addEventListener("click", () => input.click());
  watch();
  return [
    h("div", { style: { marginTop: "8px" } }, setRow({ text: "导入记忆包", stack: true,
      why: "从上一版的 Loci 搬过来：就是那边「导出」出来的 zip；下面「导出备份」打的包，也从这儿放回来。跟这边重了的，会一条一条问你：跳过 / 覆盖 / 两个都留。两边向量模型一样就连向量一起搬，不一样就搬完重算。动手之前先备份这边。",
      right: acts(pick, input) })),
    area, s.el,
  ];
}

function importRows() {
  const s = sayer();
  return [
    chatImport(s),
    s.el,
    fold("导入之后会怎样", [
      h("ol", { class: "how" }, HOW.map(([t, w]) => h("li", null, h("span", { class: "c", text: t }), h("br"), h("span", { class: "why", text: w })))),
      h("p", { class: "why", style: { margin: "4px 0 0" }, text: "反悔了：在「查看已导入的」里撤回那一批。" })]),
    h("div", { class: "setacts left" }, opener("查看已导入的", batchesWindow)),
    packageImport(),
  ];
}

// ---------------------------------------------------------------- 导出

function exportRows() {
  const s = sayer();
  const exportBtn = (label, path) => {
    const b = btn(label);
    b.addEventListener("click", () => busy(b, async () => {
      s.clear();
      try { await api.download(path, "loci.zip"); } catch (e) { s.err(e); }
    }));
    return b;
  };
  return [
    setRow({ text: "导出备份", stack: true,
      why: "把这边全部的记忆打成一个 zip，换电脑、重装以后用上面「导入记忆包」放回来。包里有：所有记忆 · 算好的向量 · 库的状态 · 沉下去的原文 · 附件。不带：设置、Key、面板密码、宿主表——这些是这台电脑自己的，换一台要重新填。",
      right: acts(exportBtn("导出备份", "/api/loci/export")) }),
    setRow({ text: "导出原话", stack: true,
      why: "Loci 手上有的原话，一段对话一个 Markdown 文件，打开就能看：导入进来的聊天，和记忆沉下去时留下的原文。宿主那边的聊天记录在宿主自己那儿，去宿主那边导。",
      right: acts(exportBtn("导出原话", "/api/loci/export/originals")) }),
    s.el,
    small("⚠ 这两样里都是你的记忆和原话，别随手发给别人。", { marginTop: "12px" }),
  ];
}

// ---------------------------------------------------------------- 阈值

// n9: the numbers on the board are the code's defaults; the API gives the lines' defaults,
// these three are the board's.
const AWAKE = [
  ["awake_recent_days", "刚写下的", "这么多天之内写下的记忆算醒着。", 3],
  ["awake_date_days", "快到的日子", "有日期的事（生日、约好的），在时间内算醒着。", 30],
  ["awake_cue_days", "被提起过的", "名字被提起、记忆被递到他眼前以后，这几天内都算醒着。", 7],
];
// 其他的 · 条子默认挂几天: `surfacing.hold_review_days` (core/_holds), its default 7.
const HOLD_DAYS = ["hold_review_days", "条子默认挂几天", "他留的条子不写期限的话，挂这么久", 7];
const OUTER_LINES = [
  ["recall_meaning", "搜索里的「意思相近」", "搜的时候，多像才标「意思相近」。调低搜出来的多，调高更准"],
];
const INNER_LINES = [
  ["reconsolidation", "相似度提醒", "新写的跟他以前的哪个看法多像，才提醒他回头看一眼"],
  ["fold_merge", "合并概括", "两条概括多像，才问要不要合成一条"],
  ["backfill_duplicate", "补记时撞上", "补记一件事的时候，跟已有的多像，才提醒「可能记过了」"],
  ["slice_guess", "对着副模型的切片算相似度", "把聊天切成小段的时候，loci会提醒他「这段好像白天记过」"],
  ["recall_floor", "recall相似度的底线", "搜索结果的综合分低于这个，就不摆出来"],
];

function thresholdsBlock(cfgP) {
  const holder = h("div");
  async function build(cfg, said) {
    const sf = cfg.surfacing || {};
    const th = cfg.thresholds || {};
    const lines = Object.fromEntries((th.lines || []).map((l) => [l.key, l]));
    const s = sayer();
    const boxes = {};
    const dayRow = ([k, text, why]) => {
      boxes[k] = num({ value: sf[k] ?? "", "aria-label": text });
      return setRow({ text, why, right: acts(boxes[k], h("span", { class: "why", text: "天" })) });
    };
    const lineRow = ([k, text, why]) => {
      const l = lines[k];
      if (!l) return null;
      boxes[k] = num({ value: l.value ?? "", "aria-label": text });
      return setRow({ text, why: [why, l.note].filter(Boolean), right: acts(boxes[k]) });
    };
    const retune = th.retune && th.retune.needed ? th.retune.words : "";
    const custom = fold("自定义设置", [
      h("p", { class: "why", style: { margin: "8px 0 0", lineHeight: "1.6" }, text: "下面都是「多像才算」的线：0 到 1，越高越挑（最后一条是 0–100 的分）。换了向量模型以后，这些数可能要重新调。" }),
      retune ? h("p", { class: "err", style: { margin: "8px 0 0", lineHeight: "1.6" }, text: retune }) : null,
      INNER_LINES.map(lineRow),
    ], { open: !!retune });

    const reset = btn("恢复默认");
    reset.addEventListener("click", () => {
      for (const [k, , , d] of [...AWAKE, HOLD_DAYS]) boxes[k].value = String(d);
      for (const [k] of [...OUTER_LINES, ...INNER_LINES]) if (lines[k] && boxes[k]) boxes[k].value = String(lines[k].default);
    });
    const save = btn("保存", { dark: true });
    save.addEventListener("click", () => busy(save, async () => {
      s.clear();
      const surfacing = {};
      for (const [k] of [...AWAKE, HOLD_DAYS]) {
        const v = numberIn(boxes[k]);
        if (v !== undefined) surfacing[k] = Math.round(v);
      }
      const thresholds = {};
      for (const [k] of [...OUTER_LINES, ...INNER_LINES]) {
        const l = lines[k];
        if (!l || !boxes[k]) continue;
        const raw = String(boxes[k].value || "").trim();
        if (!raw) continue;
        const v = Number(raw);
        if (!Number.isFinite(v)) thresholds[k] = raw;          // the server says what is wrong
        else if (v === l.value && !(v === l.default && l.set)) continue;
        else thresholds[k] = v === l.default ? null : v;
      }
      const body = { surfacing, persist: true };
      if (Object.keys(thresholds).length) body.thresholds = thresholds;
      try {
        const r = await api.post("/api/config", body);
        await build(await api.get("/api/config"), r.message);
      } catch (e) { s.err(e); }
    }));
    if (said) s.ok(said);

    fill(holder,
      h("h3", { class: "st", style: { fontSize: "16px", marginTop: "4px" }, text: "怎么样算醒着？" }),
      AWAKE.map(dayRow),
      h("h3", { class: "st", style: { fontSize: "16px", marginTop: "18px" }, text: "其他的" }),
      dayRow(HOLD_DAYS),
      OUTER_LINES.map(lineRow),
      custom,
      h("div", { class: "acts setacts" }, reset, save),
      s.el);
    return holder;
  }
  return cfgP.then((cfg) => build(cfg));
}

// ---------------------------------------------------------------- 体检 and 日志

const LOG_LINE = /^\[\d{4}-(\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+\S+\s+([A-Z]+):\s?(.*)$/;

/** 日志 in a glass window: which lines (只看出错 / 警告以上 / 全部), 复制, newest first. */
function openLogs() {
  const win = glassWindow({ title: "日志" });
  fill(win.body, logsBody());
}

function logsBody() {
  let lines = [];
  const box = h("div", { class: "logbox", hidden: true });
  const note = h("p", { class: "why", style: { margin: "10px 0 0" }, hidden: true });
  const err = h("div");
  const level = seg([["ERROR", "只看出错"], ["WARNING", "警告以上"], ["ALL", "全部"]], "WARNING", { label: "看哪些", onChange: () => load() });
  const copy = btn("复制");
  copy.addEventListener("click", () => busy(copy, async () => {
    try { await navigator.clipboard.writeText(lines.join("\n")); } catch (e) { fill(err, errorLine(e)); }
  }));
  async function load() {
    fill(err);
    try {
      const r = await api.get("/api/logs", { level: level.value, limit: 200 });
      lines = (r.lines || []).slice().reverse();
      fill(box, lines.map((ln) => {
        const m = LOG_LINE.exec(ln);
        return m
          ? h("div", { class: "logl", title: ln }, h("span", { class: "t", text: `[${m[1]}]` }), h("span", { class: "lv", text: m[2] }), h("span", { class: "m", text: m[3] }))
          : h("div", { class: "logl", title: ln }, h("span", { class: "m", style: { gridColumn: "1 / -1" }, text: ln }));
      }));
      box.hidden = !lines.length;
      note.textContent = r.note || "";
      note.hidden = !r.note;
    } catch (e) { fill(err, errorLine(e)); }
  }
  load();
  const el = h("div", null,
    h("div", { class: "loghead" }, level.el, copy),
    box, note, err,
    small("最近的在上面，一次看最近 200 行。找人帮忙的时候点「复制」整段贴过去；日志里可能夹着一点记忆的字，发给别人之前看一眼。"));
  return el;
}

async function healthBlock() {
  const r = await api.get("/api/loci/health");
  const look = () => opener("看日志", openLogs);
  const checkRow = (c, withLook) => {
    const bad = c.status === "warn" || c.status === "error";
    return setRow({ text: c.label, why: bad && c.action ? c.action : null, stack: true,
      right: h("span", { class: "acts", style: { flexWrap: "nowrap" } },
        h("span", { class: "r wrap", style: bad ? { color: "var(--ink)" } : null, text: c.message }),
        bad && withLook ? look() : null) });
  };
  const checks = r.checks || [];
  const all = h("div", { class: "setacts left" }, opener("查看日志", openLogs));
  if (!r.groups) return h("div", null, checks.map((c) => checkRow(c, true)), all);
  const byKey = Object.fromEntries(checks.map((c) => [c.key, c]));
  // The board's five rows; a row opens the checks under it (`checks`) in place.
  return h("div", null, r.groups.map((g) => {
    const details = h("div", { class: "panel health-detail", hidden: true },
      (g.checks || []).map((k) => byKey[k]).filter(Boolean).map((c) => checkRow(c, false)));
    const head = checkRow(g, true);
    const name = head.querySelector(".txt");
    if (name && (g.checks || []).length) {
      clickable(name, () => {
        details.hidden = !details.hidden;
        name.setAttribute("aria-expanded", String(!details.hidden));
      });
      name.setAttribute("aria-expanded", "false");
    }
    return h("div", { class: "hrow" }, head, details);
  }), all);
}

// ---------------------------------------------------------------- 版本

async function versionBlock() {
  const v = await api.get("/api/loci/version");
  const second = h("div");
  const s = sayer();
  const check = btn("检查更新");
  check.addEventListener("click", () => busy(check, async () => {
    s.clear();
    try {
      const r = await api.get("/api/loci/version", { check: 1 });
      const c = r.check || {};
      if (c.newer && c.latest) {
        const notes = h("div", { class: "panel", style: { margin: "4px 0 0" }, hidden: true },
          h("p", { class: "c2", text: c.latest.notes || "" }));
        const see = h("button", { class: "lnk", type: "button" }, "看这次改了什么");
        see.addEventListener("click", () => {
          if (!c.latest.notes) { window.open(c.latest.url, "_blank", "noopener"); return; }
          notes.hidden = !notes.hidden;
          see.setAttribute("aria-expanded", String(!notes.hidden));
        });
        fill(second,
          setRow({ text: "有新版本了", why: h("span", null, `${c.latest.tag} · `, see),
            right: acts(off(btn("一键更新", { dark: true }))) }),
          notes);
      } else {
        fill(second, setRow({ text: c.words || "" }));
      }
    } catch (e) { s.err(e); }
  }));
  return h("div", null,
    setRow({ text: "现在的版本",
      right: h("span", { class: "acts", style: { flexWrap: "nowrap" } }, h("span", { class: "r", style: { color: "var(--ink)" }, text: `v${v.version}` }), check) }),
    second, s.el);
}

// ---------------------------------------------------------------- 允许手机连

const PHONE_POLL_MS = 3000;

/** A value to copy into the phone: the value itself, and 复制. */
function copyRow(label, value, why) {
  const done = sayer();
  const copy = btn("复制");
  copy.addEventListener("click", () => busy(copy, async () => {
    try { await navigator.clipboard.writeText(value); done.ok("复制好了"); } catch (e) { done.err(e); }
  }));
  return h("div", null,
    setRow({ text: label, why: [h("code", { text: value, style: { wordBreak: "break-all" } }), why], right: acts(copy), stack: true }),
    done.el);
}

/** The switch, then — once both processes listen on the network — what the phone needs. While
 *  the launcher restarts them (the switch and `live` disagree) the page asks again every
 *  few seconds; the restart drops this page's own requests for a moment too. */
function phoneBlock(first) {
  const box = h("div");
  const s = sayer();
  let timer = null;
  const toggle = sw({
    checked: first.on,
    label: "允许手机连",
    onChange: async (want) => {
      s.clear();
      toggle.disabled = true;
      try {
        show(await api.post("/api/loci/phone", { on: want }));
      } catch (e) {
        // No answer at all (status 0): the launcher restarted Loci before this request's
        // reply got out. The switch was written; wait for the restart like any other.
        if (e && e.status === 0) {
          show({ on: want, live: !want, addresses: [], here: {}, key: "" });
        } else {
          toggle.setAttribute("aria-checked", String(!want));
          s.err(e);
        }
      }
      toggle.disabled = false;
    },
  });

  function show(p) {
    clearTimeout(timer);
    toggle.setAttribute("aria-checked", String(p.on));
    if (p.on !== p.live) {
      fill(box, h("p", { class: "why", text: p.on ? "正在打开：Loci 和网关重启一下，十几秒。" : "正在关上：Loci 和网关重启一下，十几秒。" }));
      timer = setTimeout(async () => {
        try { show(await api.get("/api/loci/phone")); } catch { show(p); }
      }, PHONE_POLL_MS);
      return;
    }
    if (!p.on) {
      // Off: only this computer reaches them, at its own loopback addresses.
      fill(box,
        copyRow("网关地址", p.here.gateway, "这台电脑上的客户端，服务商的 API base URL 填这个"),
        copyRow("MCP 地址", p.here.mcp, "这台电脑上的客户端，MCP 选 Streamable HTTP，地址填这个"),
        copyRow("MCP 请求头", `Bearer ${p.key}`, "MCP 加一个自定义请求头：名字 Authorization，值填这个"));
      return;
    }
    if (!p.addresses.length) {
      fill(box, h("p", { class: "err", text: "这台电脑现在没连在局域网上（没找到家里网络的地址）。连上 Wi-Fi 或网线再来看。" }));
      return;
    }
    // The first address is the one the default route uses; the others are mostly virtual
    // adapters (WSL, Hyper-V) a phone cannot reach, so they are not offered.
    const [main_ip] = p.addresses;
    fill(box,
      copyRow("网关地址", main_ip.gateway, "手机上的客户端，服务商的 API base URL 填这个"),
      copyRow("MCP 地址", main_ip.mcp, "手机上的客户端，MCP 选 Streamable HTTP，地址填这个"),
      copyRow("MCP 请求头", `Bearer ${p.key}`, "MCP 加一个自定义请求头：名字 Authorization，值填这个"),
      h("p", { class: "why", text: "只在同一个 Wi-Fi 里能连。第一次打开时 Windows 可能弹窗问要不要让 python 和 node 连网络：勾「专用网络」，点允许。" }));
  }

  show(first);
  return h("div", null,
    setRow({ text: "允许手机连", why: "打开后，同一个 Wi-Fi 里的手机能连这台电脑上的 Loci 和网关。", right: acts(toggle) }),
    box, s.el);
}

/** The Docker image: always open, no switch; the server's own address is not known from
 *  inside the container, so the addresses carry a placeholder for it. */
function fixedAddressBlock(p) {
  const [a] = p.addresses;
  return h("div", null,
    copyRow("网关地址", a.gateway, "客户端里服务商的 API base URL 填这个"),
    copyRow("MCP 地址", a.mcp, "客户端里 MCP 选 Streamable HTTP，地址填这个"),
    copyRow("MCP 请求头", `Bearer ${p.key}`, "MCP 加一个自定义请求头：名字 Authorization，值填这个"),
    h("p", { class: "why", text: `把 ${a.ip} 换成你服务器的 IP 或域名；从这台服务器本机连，换成 127.0.0.1。地址里的端口是容器里面的：docker-compose.yml 的 ports 里改过冒号左边那个数的，换成你改的那个。` }));
}

// ---------------------------------------------------------------- the page

export default {
  async render(view) {
    const cfgP = api.get("/api/config");
    const authP = api.get("/api/loci/auth/state");
    const snapP = ask(PRESENT);
    for (const p of [cfgP, authP, snapP]) p.catch(() => {});
    // 允许手机连 exists only under the package launcher (web/loci_phone.py); elsewhere the
    // block is left out and the numbers close up.
    const phone = await api.get("/api/loci/phone").catch(() => null);
    const findOllama = ollamaFinder();
    let no = 0;
    const block = (title, tipText, body) => setBlock(String(++no).padStart(2, "0"), title, { tip: tipText }, body);
    view.append(pagehead({ title: "setting" }), h("main", { class: "sets setting" },
      block("宿主", TIP.hosts, hostsRows()),
      block("称呼", TIP.names, later(() => namesBlock(cfgP))),
      h("div", { class: "duo" },
        block("账号安全", null, later(() => accountBlock(authP))),
        block("安全问题", TIP.question, later(() => questionBlock(authP)))),
      block("网关上游", TIP.upstream, later(() => upstreamBlock(snapP))),
      phone && phone.available
        ? (phone.fixed ? block("连接地址", null, fixedAddressBlock(phone)) : block("允许手机连", null, phoneBlock(phone)))
        : null,
      block("副模型", TIP.aux, later(async () => auxModel(await cfgP, findOllama, snapP))),
      block("向量模型", TIP.emb, [later(async () => embModel(await cfgP, findOllama)), later(vectorRows)]),
      block("导入", null, importRows()),
      block("导出", null, exportRows()),
      block("阈值", TIP.thresholds, later(() => thresholdsBlock(cfgP))),
      block("体检", TIP.health, later(healthBlock)),
      block("版本", null, later(versionBlock))));
  },
};
