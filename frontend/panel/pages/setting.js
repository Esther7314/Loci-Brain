/* ==========================================================
   pages/setting.js — setting (boards setting, setting-phone, setting-local-embed)

   One page, top to bottom as the board has it. Each block reads on its own, so one that
   fails shows its error in place and the rest still stand. Wired to what the server has:

     宿主      no route lists the hosts or changes their grants: drawn, every control off
     称呼      GET /api/config ai_name / owner_name · POST /api/config {ai_name, owner_name,
               persist}
     账号      GET /api/loci/auth/state · 改密码 POST /api/loci/auth/set-password {password,
               current_password} · 设置安全问题 POST /api/loci/auth/security-question
               {question, answer} (a logged-in session; the answer never comes back, so
               its box starts empty). No account names: 账号's 改 is off
     模型      副模型: GET/POST /api/config `dehydration` (its key masked, sent only when
               typed) · 测试 POST /api/test/dehydration · 拉取列表 POST /api/models. 在哪儿跑
               本地 is `runs_on: "local"`: the local Ollama, no Base URL or Key to fill, the
               model picked from what GET /api/loci/ollama finds. Thinking is `thinking`,
               shown for the formats that have a thinking knob (Gemini, Anthropic).
               向量模型: `embedding` the same way (its key too), 测试 POST
               /api/test/embedding; a change of model answers 409 with the cost, which is
               put to the person and sent again with reembed "confirm"; the recompute's
               progress from GET /api/loci/embedding/migration. 在哪儿跑 本地 is the
               embedding's api_format "ollama": the three local steps, step 1 and the Model
               list from GET /api/loci/ollama; pulling a model has no route, so 下载 is off
     向量      GET /api/loci/embedding/missing (paged) · 现在补 POST /api/loci/embedding/backfill
     导入      聊天记录: POST /api/import/preflight, then POST /api/import/upload; 导入过的
               GET /api/import/batches, 暂停 POST /api/import/pause, 继续 upload with
               resume, 撤回这一批 POST /api/import/withdraw. 记忆包: POST
               /api/loci/import-package (the file, then the decisions), GET for progress
     导出      GET /api/loci/export · GET /api/loci/export/originals, saved as files
     阈值      GET/POST /api/config `surfacing.awake_*`, `surfacing.hold_review_days`
               (条子默认挂几天) and `thresholds` (with its retune words)
     体检      GET /api/loci/health · 日志 GET /api/logs (newest shown first)
     版本      GET /api/loci/version, 检查更新 with ?check=1; 一键更新 has no route: off

   Words are the board's, or the API's own (messages, errors, why_words, say, words).
   ========================================================== */

import * as api from "../api.js";
import { h, fill, subbar, group, row, tip, btn, sw, inp, num, customBox, errorLine, pagedList } from "../ui.js";
import { openDetail } from "../detail.js";

const TIP = {
  hosts: "宿主就是接进 Loci 的地方，比如 Claude Code。Loci 自己不聊天，是挂在它们上面的。这里列着接进来的有谁、各自有哪些权限。一般只有一个，接了好几处才会有好几行。",
  names: "写记忆的时候用这两个名字，不写 user / assistant。没填的话他叫「AI」。",
  aux: "副模型不跟你聊天，在后台替 Loci 干杂活：把聊天写成记忆、打标签、整理合并。用便宜、快的就够，不用跟聊天的那个一样。",
  emb: "向量模型把每条记忆变成一串数字，用来找「意思相近」的记忆——recall 里写着「意思相近」的那些，就是靠它找出来的。它只管找，不管写。",
  vec: "每条记忆写下来以后，要用向量模型算一遍，才能被「意思相近」找到；还没算上的，只能靠字面搜到。平时它会自己在后台慢慢补，这里列着还没算上的是哪几条、为什么。",
  thresholds: "这些数决定一条记忆什么时候「醒着」，还有两条记忆多像才算「像」。醒着 = 待在 surface 的池子里，更容易被他注意到，但不保证一定会冒出来。默认值是试出来的，一般不用动。",
  health: "每次打开这页都会自己查一遍。哪一行不对，点它后面的「看日志」，下面的日志会跳到那附近。",
};

const RUNS_WHY = "本地 = 在你自己电脑上跑，要先装好 Ollama。选了本地：Base URL 自动填 http://localhost:11434，Key 那一格不出现";
const LOCAL_FORMATS = ["ollama", "local"];
const OLLAMA_DOWNLOAD = "https://ollama.com/download";

// ---------------------------------------------------------------- small pieces

/** A settings row: name (and its small words) left, the control right. `wide` gives the
 *  control 300px; `stack` puts it under the words on the phone. */
function setRow({ text, why, right, wide = false, stack = false }) {
  const layout = ["set", wide ? "wide" : "", stack ? "stack" : ""].filter(Boolean).join(" ");
  return row({ text, why, right: right || h("span"), layout });
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
 *  no box of its own (display: contents), so what it holds sits in the group's column. */
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
  const el = h("select", { class: "btn set", "aria-label": label });
  function setList(models) {
    const keep = el.value || current || "";
    const names = [...new Set([keep, ...(models || [])].filter(Boolean))];
    fill(el, names.length ? names.map((m) => h("option", { value: m, text: m }))
      : h("option", { value: "", text: "选一个模型" }));
    el.value = keep;
  }
  setList([]);
  return { el, get value() { return el.value; }, setList };
}

/** A number from a box, or undefined when it holds none. */
function numberIn(box) {
  const raw = String(box.value || "").trim();
  if (!raw) return undefined;
  const v = Number(raw);
  return Number.isFinite(v) ? v : undefined;
}

/** A grey box folded behind a link (导入之后会怎样). */
function foldBox(label, content, { open = false } = {}) {
  const body = h("div", null, content);
  const toggle = h("button", { class: "lnk", type: "button", style: { fontSize: "14px" } });
  const box = h("div", { class: "panel" }, toggle, body);
  const set = (on) => {
    toggle.textContent = `${label} ${on ? "▴" : "▾"}`;
    toggle.setAttribute("aria-expanded", String(on));
    body.hidden = !on;
  };
  toggle.addEventListener("click", () => set(body.hidden));
  set(open);
  return box;
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

function hostsGroup() {
  const box = customBox([
    setRow({ text: "能碰哪些来源", right: acts(off(btn("▾"))) }),
    setRow({ text: "能恢复撤回的来源", right: acts(off(sw({ checked: false, label: "能恢复撤回的来源" }))) }),
    setRow({ text: "不说用途也能读整个库", right: acts(off(sw({ checked: false, label: "不说用途也能读整个库" }))) }),
    small("一般不用改。改错了，宿主可能读不到该读的记忆，或者读到不该读的。真要改，改之前要再输一次面板密码。"),
    h("div", { class: "acts end", style: { marginTop: "12px" } }, off(btn("保存", { dark: true }))),
  ], { open: true });
  return group("宿主", { tip: TIP.hosts }, h("div", null, box));
}

// ---------------------------------------------------------------- 称呼

async function namesBlock(cfgP) {
  const cfg = await cfgP;
  const ai = h("input", { class: "num name", value: cfg.ai_name || "", "aria-label": "他叫什么", maxlength: "40" });
  const me = h("input", { class: "num name", value: cfg.owner_name || "", "aria-label": "你叫什么", maxlength: "40" });
  const s = sayer();
  const save = btn("保存", { dark: true });
  save.addEventListener("click", () => busy(save, async () => {
    s.clear();
    try {
      const r = await api.post("/api/config", { ai_name: ai.value.trim(), owner_name: me.value.trim(), persist: true });
      s.ok(r.message);
    } catch (e) { s.err(e); }
  }));
  return h("div", null, h("div", { class: "pair" }, h("label", null, "他的", ai), h("label", null, "你的", me), save), s.el);
}

// ---------------------------------------------------------------- 账号

function field(label, input) {
  return h("div", { class: "fld" }, h("span", { class: "fl", text: label }), input);
}

async function accountBlock() {
  const st = await api.get("/api/loci/auth/state");
  const has = !!(st.has_file_password || st.env_locked);
  const stars = h("span", { class: "r", style: { fontFamily: "ui-monospace, Consolas, monospace", fontSize: "14px" }, text: has ? "********" : "" });

  const current = inp({ type: "password", "aria-label": "密码", autocomplete: "current-password" });
  const pw1 = inp({ type: "password", "aria-label": "新密码", placeholder: "至少 6 位", autocomplete: "new-password" });
  const pw2 = inp({ type: "password", "aria-label": "再输一次新密码", autocomplete: "new-password" });
  const err = h("p", { class: "err", role: "alert", style: { margin: "0" }, hidden: true });
  const done = sayer();
  const save = btn("保存", { dark: true, type: "submit" });
  // 改密码 opens the boxes under its row, in the forgot-password board's words: the
  // password now (once there is one; before the first, only the new one), the new one twice.
  const currentField = field("密码", current);
  currentField.hidden = !!st.setup_needed;
  const form = h("form", { class: "pwbox", novalidate: true, hidden: true },
    currentField,
    field("新密码", pw1), field("再输一次", pw2), err,
    h("div", { class: "acts end", style: { justifyContent: "flex-start" } }, save));
  const change = btn("改密码", { dark: true });
  change.addEventListener("click", () => {
    form.hidden = !form.hidden;
    change.setAttribute("aria-expanded", String(!form.hidden));
    if (!form.hidden) (st.setup_needed ? pw1 : current).focus();
  });
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
        form.hidden = true;
        stars.textContent = "********";
        st.setup_needed = false;
        currentField.hidden = false;
        if (r.env_locked) done.ok(r.next);
      } catch (x) {
        err.textContent = x.message;
        err.hidden = false;
      }
    });
  });

  // The question as it is set; the answer is kept only as a hash, so its box starts empty.
  const question = inp({ "aria-label": "安全问题", value: st.question || "", maxlength: "200" });
  const answer = inp({ "aria-label": "答案", maxlength: "200", autocomplete: "off" });
  const qaSaid = sayer();
  const qaSave = btn("保存", { dark: true });
  qaSave.addEventListener("click", () => busy(qaSave, async () => {
    qaSaid.clear();
    try {
      const r = await api.post("/api/loci/auth/security-question",
        { question: question.value.trim(), answer: answer.value.trim() });
      question.value = r.question || question.value.trim();
      answer.value = "";
      qaSaid.ok(r.message);
    } catch (e) { qaSaid.err(e); }
  }));
  return h("div", null,
    setRow({ text: "账号", wide: true, right: acts(h("span", { class: "r", text: "user" }), off(btn("改"))) }),
    setRow({ text: "密码", why: "第一次进来用的是默认密码，记得改；改完这里只显示 ********", wide: true, stack: true,
      right: acts(stars, change) }),
    form, done.el,
    setRow({ text: "忘记密码", why: "设了安全问题：答对就能重设。没设：只能在装 Loci 的那台电脑上重设", wide: true, stack: true,
      right: acts(h("span", { class: "r", style: { fontSize: "13px" }, text: "在登录页" })) }),
    setRow({ text: "设置安全问题", why: "忘记密码的时候用", wide: true }),
    h("div", { class: "pair qa", style: { padding: "0 0 8px" } },
      h("label", null, "问：", question), h("label", null, "答：", answer), qaSave),
    qaSaid.el);
}

// ---------------------------------------------------------------- 模型

function modelHead(title, tipText, status) {
  return h("div", { class: "sth", style: { flexWrap: "wrap", marginBottom: "14px" } },
    h("h3", { class: "st", style: { margin: "0" }, text: title }), tip(tipText),
    h("span", { style: { marginLeft: "12px" } }, status));
}

function formRow(name, field) {
  return [h("span", { class: "fn", text: name }), field];
}

/** The 测试 / 保存 pair under a model's form. */
function testSave(onTest, onSave, { disabled = false } = {}) {
  const test = btn("测试");
  const save = btn("保存", { dark: true });
  test.disabled = disabled;
  save.disabled = disabled;
  test.addEventListener("click", () => busy(test, onTest));
  save.addEventListener("click", () => busy(save, onSave));
  return h("div", { class: "acts end", style: { marginTop: "18px" } }, test, save);
}

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
  return o.running ? `本地 · 找到了，装了 ${(o.models || []).length} 个模型` : "本地 · 没找到 Ollama";
}

/** The formats whose API has a thinking knob the side model turns (core/dehydrator). */
const THINKING_FORMATS = ["gemini", "anthropic"];

function auxModel(cfg, findOllama) {
  const d = cfg.dehydration || {};
  let runs = d.runs_on === "local" ? "local" : "cloud";
  const status = h("span", { class: "why" });
  const s = sayer();
  const fmt = seg([["openai_compat", "OpenAI 兼容"], ["gemini", "Gemini"], ["anthropic", "Anthropic"]],
    d.api_format || "openai_compat", { label: "API format", onChange: () => layout() });
  const base = inp({ placeholder: "例：https://api.deepseek.com", "aria-label": "Base URL", value: d.base_url || "" });
  const key = inp({ type: "password", placeholder: d.api_key_masked ? "********" : "粘贴 API Key", "aria-label": "API Key", autocomplete: "off" });
  const model = modelPicker(d.model, "Model");
  const pull = h("button", { class: "lnk", type: "button", style: { fontSize: "14px" } }, "从 Base URL 拉取列表");
  const installed = h("span", { class: "why", text: "列的是这台电脑上已经装了的" });
  pull.addEventListener("click", () => busy(pull, async () => {
    s.clear();
    try {
      const r = await api.post("/api/models", { api_key: key.value.trim() || "__use_current__", base_url: base.value.trim(), api_format: fmt.value });
      if (r.ok) model.setList(r.models); else s.err(new Error(r.error));
    } catch (e) { s.err(e); }
  }));
  const localBase = h("span", { style: { fontSize: "15px", color: "var(--small)" } });
  const maxTok = num({ value: d.max_tokens ?? "", "aria-label": "Max output tokens" });
  const temp = num({ value: d.temperature ?? "", "aria-label": "Temperature" });
  const timeout = num({ value: d.timeout_seconds ?? "", "aria-label": "超时" });
  const thinking = sw({ checked: !!d.thinking, label: "Thinking" });

  const runsSeg = seg([["cloud", "云端"], ["local", "本地"]], runs, { label: "在哪儿跑",
    onChange: (v) => { runs = v; layout(); if (v === "local") findLocal(); } });
  const thinkRow = setRow({ text: "Thinking", why: "只有会先想再答的模型有这一项，干杂活一般关着", right: acts(thinking) });
  const custom = customBox([
    setRow({ text: "在哪儿跑", why: RUNS_WHY, wide: true, stack: true, right: acts(runsSeg.el) }),
    setRow({ text: "Max output tokens", why: "一次最多输出多少；开着 thinking 的话，想的那部分也算在里面，要给大一点", right: acts(maxTok) }),
    setRow({ text: "Temperature", why: "越低越稳，整理记忆用低的", right: acts(temp) }),
    thinkRow,
    setRow({ text: "等多久算超时", right: acts(timeout, h("span", { class: "why", text: "秒" })) }),
  ], { open: runs === "local" });

  const fmtRow = formRow("API format", h("span", null, fmt.el,
    h("span", { class: "why hint", text: "DeepSeek、Kimi、通义、硅基流动这些都选「OpenAI 兼容」" }),
    h("span", { class: "why hint", style: { marginTop: "2px" }, text: "想在自己电脑上跑？在下面「自定义设置」里切到本地" })));
  const baseRow = formRow("Base URL", base);
  const localBaseRow = formRow("Base URL", h("span", null, localBase, " ", h("span", { class: "why", text: "（自动填的）" })));
  const keyRow = formRow("Key", key);

  let found = null;
  /** Local: what Ollama holds fills the Model list, and the status says whether it was found. */
  async function findLocal(again = false) {
    found = await findOllama(again);
    localBase.textContent = found.base_url || "";
    model.setList(found.models || []);
    layout();
  }

  /** Cloud shows the provider's fields; local hides the format and the key, and gives the
   *  address it found. Thinking shows only where it does something. */
  function layout() {
    const local = runs === "local";
    for (const el of [...fmtRow, ...baseRow, ...keyRow]) el.hidden = local;
    for (const el of localBaseRow) el.hidden = !local;
    pull.hidden = local;
    installed.hidden = !local;
    thinkRow.hidden = local || !THINKING_FORMATS.includes(fmt.value);
    if (local) fill(status, found ? ollamaWords(found) : "");
    else fill(status, d.api_key_masked ? "" : "还没配");
  }

  async function test() {
    s.clear();
    try {
      const r = await api.post("/api/test/dehydration", {});
      if (r.ok) fill(status, `通了 ${model.value || d.model || ""}`.trim());
      else fill(status, `不通：${r.error || ""}`);
    } catch (e) { fill(status, `不通：${e.message}`); }
  }
  async function save() {
    s.clear();
    const dehy = { runs_on: runs, model: model.value };
    if (runs === "cloud") {
      dehy.api_format = fmt.value;
      dehy.base_url = base.value.trim();
      if (key.value.trim()) dehy.api_key = key.value.trim();
    }
    if (!thinkRow.hidden) dehy.thinking = thinking.getAttribute("aria-checked") === "true";
    const mt = numberIn(maxTok), t = numberIn(temp), to = numberIn(timeout);
    if (mt !== undefined) dehy.max_tokens = Math.round(mt);
    if (t !== undefined) dehy.temperature = t;
    if (to !== undefined) dehy.timeout_seconds = to;
    try {
      const r = await api.post("/api/config", { dehydration: dehy, persist: true });
      if (dehy.api_key) { key.value = ""; key.placeholder = "********"; d.api_key_masked = "********"; }
      layout();
      s.ok(r.message);
    } catch (e) { s.err(e); }
  }

  layout();
  if (runs === "local") findLocal();
  return h("div", null,
    modelHead("副模型", TIP.aux, status),
    h("div", { class: "form" },
      fmtRow, baseRow, localBaseRow, keyRow,
      formRow("Model", h("span", { class: "pick" }, model.el, pull, installed))),
    h("div", { style: { marginTop: "18px" } }, custom),
    testSave(test, save),
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
  let customOpen = false;

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

  async function test(status) {
    try {
      const r = await api.post("/api/test/embedding", {});
      fill(status, r.ok ? (r.message || "") : `不通：${r.error || ""}`);
    } catch (x) { fill(status, `不通：${x.message}`); }
  }

  function runsRow(mode, why) {
    return setRow({ text: "在哪儿跑", why, wide: true, stack: true,
      right: acts(seg([["cloud", "云端"], ["local", "本地"]], mode, { label: "在哪儿跑", onChange: show }).el) });
  }

  function custom(mode, dimWhy, timeout) {
    const box = customBox([
      runsRow(mode, mode === "local" ? "切回云端就是填 Base URL 和 Key 的那张表" : RUNS_WHY),
      setRow({ text: "Dimensions", why: dimWhy, right: h("span", { class: "r", text: "自动" }) }),
      setRow({ text: "等多久算超时", right: acts(timeout, h("span", { class: "why", text: "秒" })) }),
    ], { open: customOpen });
    box.querySelector("button").addEventListener("click", () => { customOpen = !customOpen; });
    return h("div", { style: { marginTop: "18px" } }, box);
  }

  function cloud() {
    const status = h("span", { class: "why", text: e.enabled && !wasLocal ? "" : "还没配" });
    const s = sayer();
    const fmt = seg([["openai_compat", "OpenAI 兼容"], ["gemini", "Gemini"]],
      wasLocal || e.api_format === "gemini" ? (e.api_format === "gemini" ? "gemini" : "openai_compat") : "openai_compat",
      { label: "API format" });
    const base = inp({ placeholder: "例：https://api.siliconflow.cn/v1", "aria-label": "Base URL", value: wasLocal ? "" : (e.base_url || "") });
    const key = inp({ type: "password", placeholder: e.api_key_masked ? "********" : "粘贴 API Key", "aria-label": "API Key", autocomplete: "off" });
    const model = modelPicker(wasLocal ? "" : e.model, "Model");
    const pull = h("button", { class: "lnk", type: "button", style: { fontSize: "14px" } }, "从 Base URL 拉取列表");
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
      });
    };
    return [
      modelHead("向量模型", TIP.emb, status),
      h("div", { class: "form" },
        formRow("API format", h("span", null, fmt.el,
          h("span", { class: "why hint", text: "硅基流动、OpenAI 这些云端的都选「OpenAI 兼容」" }),
          h("span", { class: "why hint", style: { marginTop: "2px" }, text: "想在自己电脑上跑？在下面「自定义设置」里切到本地" }))),
        formRow("Base URL", base),
        formRow("Key", key),
        formRow("Model", h("span", { class: "pick" }, model.el, pull))),
      custom("cloud", "一条记忆变成多少个数；测试的时候自己认出来，不用填", timeout),
      small("配好以后再换模型，全部记忆要重新算一遍向量；换之前会先告诉你要算多久。", { marginTop: "14px", lineHeight: "" }),
      testSave(() => test(status), save),
      s.el,
    ];
  }

  function local() {
    const status = h("span", { class: "why", text: "本地" });
    const s = sayer();
    const model = modelPicker(wasLocal && e.model ? e.model : "bge-m3", "Model");
    const timeout = num({ value: e.timeout_seconds ?? "", "aria-label": "超时" });
    const stepTest = btn("测试");
    stepTest.addEventListener("click", () => busy(stepTest, () => test(status)));
    const save = () => {
      const body = { enabled: true, api_format: "ollama", base_url: "", model: model.value };
      const to = numberIn(timeout);
      if (to !== undefined) body.timeout_seconds = to;
      return saveEmbedding(body, s);
    };
    const step = (no, title, why, right, extra) => h("div", { class: "step" },
      h("span", { class: "no", text: no }),
      h("div", null, h("div", { class: "c", text: title }), h("div", { class: "why", style: { marginTop: "4px" }, text: why }), extra || null),
      right);
    // Step 1 and the Model list read what GET /api/loci/ollama finds: found, the step is
    // ticked and says so, and the list holds what this machine has installed.
    const foundWords = h("span", { class: "why", text: "找到了", hidden: true });
    const first = step("1", "装 Ollama", "它是在你电脑上跑模型的小程序。装好打开就行，之后它自己在后台待着。",
      acts(foundWords, h("a", { class: "btn set", href: OLLAMA_DOWNLOAD, target: "_blank", rel: "noopener noreferrer", text: "去下载 Ollama" })));
    const firstNo = first.querySelector(".no");
    const baseText = h("span", null, "http://localhost:11434");
    findOllama().then((o) => {
      fill(status, ollamaWords(o));
      if (o.base_url) baseText.textContent = o.base_url;
      foundWords.hidden = !o.running;
      firstNo.classList.toggle("ok", !!o.running);
      firstNo.textContent = o.running ? "✓" : "1";
      if (o.running) firstNo.setAttribute("aria-label", "做完了"); else firstNo.removeAttribute("aria-label");
      model.setList(o.models || []);
    });
    return [
      modelHead("向量模型", TIP.emb, status),
      h("div", { class: "steps" },
        first,
        step("2", "下一个向量模型", "推荐 bge-m3：中文好，大约 1.2 GB。不用开命令行，点「下载」就行。",
          acts(off(h("button", { class: "lnk", type: "button", style: { fontSize: "14px" } }, "换一个")), off(btn("下载 bge-m3", { dark: true })))),
        step("3", "测一下", "下完会自动选上，点「测试」看通不通。", acts(stepTest))),
      h("div", { class: "form" },
        formRow("Base URL", h("span", { style: { fontSize: "15px", color: "var(--small)" } }, baseText, " ", h("span", { class: "why", text: "（自动填的）" }))),
        formRow("Model", h("span", { class: "pick" }, model.el, h("span", { class: "why", text: "列的是这台电脑上已经装了的" })))),
      custom("local", "测试的时候自己认出来，不用填", timeout),
      small("配好以后再换模型，全部记忆要重新算一遍向量；换之前会先告诉你要算多久。", { marginTop: "14px", lineHeight: "" }),
      testSave(() => test(status), save),
      s.el,
    ];
  }

  function show(mode) {
    fill(holder, mode === "local" ? local() : cloud(), migration.el);
  }
  show(wasLocal ? "local" : "cloud");
  return holder;
}

async function modelsBlock(cfgP) {
  const cfg = await cfgP;
  const findOllama = ollamaFinder();
  return [auxModel(cfg, findOllama), embModel(cfg, findOllama)];
}

// ---------------------------------------------------------------- 向量

async function vectorBlock() {
  const count = h("span");
  const note = h("p", { class: "why", style: { margin: "0 0 4px" }, hidden: true });
  const s = sayer();
  const now = btn("现在补", { dark: true });
  const show = (reply) => {
    count.textContent = `${reply.missing ?? reply.total ?? 0} 条还没有向量`;
    note.textContent = reply.note || "";
    note.hidden = !reply.note;
  };
  const list = await pagedList({
    load: (q) => api.get("/api/loci/embedding/missing", q),
    item: (it) => row({ text: it.text || `#${it.short}`, open: () => openDetail(it.id),
      right: h("span", { class: "r wrap", text: it.why_words || "" }), layout: "set" }),
  });
  show(list.reply);
  const listBox = h("div", { hidden: !list.total }, list.el);
  now.addEventListener("click", () => busy(now, async () => {
    s.clear();
    try {
      await api.post("/api/loci/embedding/backfill", {});
      const reply = await list.reload();
      show(reply);
      listBox.hidden = !reply.total;
    } catch (e) { s.err(e); }
  }));
  return h("div", null,
    row({ text: "", lead: count, why: "「现在补」= 不等后台了，马上把缺的这几条算上", right: acts(now), layout: "set" }),
    note, s.el,
    h("div", { class: "ruled-first" }, listBox),
    small("在排队 = 等着轮到它；一直失败 = 试了几次都没成，多半是向量模型没配好，或者网断了。", { marginTop: "12px" }));
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

/** 导入过的: one row per batch, with what can be done to it now. */
function batchesBlock(s) {
  const el = h("div");
  async function load() {
    const r = await api.get("/api/import/batches");
    const batches = r.batches || [];
    fill(el, batches.length ? h("div", { style: { marginTop: "8px" } },
      h("h3", { class: "st", style: { fontSize: "16px" }, text: "导入过的" }),
      batches.map(batchRow),
      small(WITHDRAW_WORDS)) : null);
    return batches.some((b) => b.is_running);
  }
  function again() {
    return load().then((more) => { if (more) poll(el, load); }).catch((e) => fill(el, errorLine(e)));
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
    return setRow({ text: b.filename || b.batch, why, wide: true, stack: true,
      right: h("span", { class: "acts", style: { flexWrap: "nowrap" } }, buttons) });
  }
  again();
  return { el, reload: again };
}

/** 导入聊天记录: the file is looked at first, then stored once the question is answered. */
function chatImport(batches, s) {
  const preview = h("div", { hidden: true });
  const take = async (file, sameSelf) => {
    fill(preview);
    preview.hidden = true;
    const fd = new FormData();
    fd.append("file", file);
    fd.append("same_self", sameSelf ? "1" : "0");
    try { await api.postForm("/api/import/upload", fd); } catch (e) { s.err(e); }
    batches.reload();
  };
  const input = fileInput(".json,.md,.markdown,.txt", async (file) => {
    s.clear();
    fill(preview);
    preview.hidden = true;
    const fd = new FormData();
    fd.append("file", file);
    let p;
    try { p = await api.postForm("/api/import/preflight", fd); } catch (e) { s.err(e); return; }
    if (!p.ok) { s.err(new Error((p.warnings || []).join("；") || p.error || "")); return; }
    const yes = btn("是", { dark: true });
    const no = btn("不是");
    const cancel = btn("取消");
    yes.addEventListener("click", () => busy(yes, () => take(file, true)));
    no.addEventListener("click", () => busy(no, () => take(file, false)));
    cancel.addEventListener("click", () => { fill(preview); preview.hidden = true; });
    fill(preview, h("div", { class: "panel", style: { margin: "6px 0 12px" } },
      h("div", { class: "c", style: { whiteSpace: "normal" }, text: "先看一眼" }),
      h("p", { class: "why", style: { margin: "4px 0 0", lineHeight: "1.6" },
        text: [file.name, `${p.conversations_count} 段对话`, `${p.turns_count} 条消息`,
          `副模型 ${p.estimated_api_calls} 次`, `${p.estimated_tokens} token`].join(" · ") }),
      h("p", { class: "c", style: { margin: "10px 0 0", whiteSpace: "normal" }, text: "那边的 AI 是不是他" }),
      h("div", { class: "acts end", style: { justifyContent: "flex-start", marginTop: "10px" } }, yes, no, cancel)));
    preview.hidden = false;
  });
  const pick = btn("导入");
  pick.addEventListener("click", () => input.click());
  return [
    setRow({ text: "导入聊天记录", stack: true,
      why: "从别的地方搬聊天过来。能收：ChatGPT 导出的 JSON，Claude 导出的 JSON，一列 role / content 的消息 JSON，Markdown（.md），纯文本（.txt）。",
      right: acts(pick, input) }),
    preview,
  ];
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
    const cancel = btn("取消");
    const go = btn("导入记忆包", { dark: true });
    cancel.addEventListener("click", () => busy(cancel, async () => {
      try { await api.post("/api/loci/import-package", { job_id: st.job_id, cancel: true }); } catch (e) { s.err(e); }
      fill(area);
    }));
    go.addEventListener("click", () => busy(go, () => apply(st.job_id,
      Object.fromEntries(choices.map(([id, sg]) => [id, sg.value])))));
    fill(area, h("div", { class: "panel", style: { margin: "6px 0 12px" } },
      st.integrity_warning ? small(st.integrity_warning, { marginTop: "0" }) : null,
      conflicts.map((c, i) => setRow({ text: c.import_name || c.bucket_id, wide: true, stack: true,
        why: c.current_name && c.current_name !== c.import_name ? c.current_name : null,
        right: acts(choices[i][1].el) })),
      h("div", { class: "acts end", style: { marginTop: "12px" } }, cancel, go)));
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

function importGroup() {
  const s = sayer();
  const batches = batchesBlock(s);
  return group("导入", {}, h("div", null,
    chatImport(batches, s),
    h("div", { style: { marginTop: "6px" } }, foldBox("导入之后会怎样",
      [h("ol", { class: "how" }, HOW.map(([t, w]) => h("li", null, h("span", { class: "c", text: t }), h("br"), h("span", { class: "why", text: w })))),
        h("p", { class: "why", style: { margin: "4px 0 0" }, text: "反悔了：在下面「导入过的」里撤回那一批。" })], { open: true })),
    s.el,
    batches.el,
    packageImport()));
}

// ---------------------------------------------------------------- 导出

function exportGroup() {
  const s = sayer();
  const exportBtn = (label, path) => {
    const b = btn(label);
    b.addEventListener("click", () => busy(b, async () => {
      s.clear();
      try { await api.download(path, "loci.zip"); } catch (e) { s.err(e); }
    }));
    return b;
  };
  return group("导出", {}, h("div", null,
    setRow({ text: "导出备份", stack: true,
      why: "把这边全部的记忆打成一个 zip，换电脑、重装以后用上面「导入记忆包」放回来。包里有：所有记忆 · 算好的向量 · 库的状态 · 沉下去的原文 · 附件。不带：设置、Key、面板密码、宿主表——这些是这台电脑自己的，换一台要重新填。",
      right: acts(exportBtn("导出备份", "/api/loci/export")) }),
    setRow({ text: "导出原话", stack: true,
      why: "Loci 手上有的原话，一段对话一个 Markdown 文件，打开就能看：导入进来的聊天，和记忆沉下去时留下的原文。宿主那边的聊天记录在宿主自己那儿，去宿主那边导。",
      right: acts(exportBtn("导出原话", "/api/loci/export/originals")) }),
    s.el,
    small("⚠ 这两样里都是你的记忆和原话，别随手发给别人。", { marginTop: "12px" })));
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
      return setRow({ text, why, wide: true, right: acts(boxes[k], h("span", { class: "why", text: "天" })) });
    };
    const lineRow = ([k, text, why]) => {
      const l = lines[k];
      if (!l) return null;
      boxes[k] = num({ value: l.value ?? "", "aria-label": text });
      return setRow({ text, why: [why, l.note].filter(Boolean), wide: true, right: acts(boxes[k]) });
    };
    const retune = th.retune && th.retune.needed ? th.retune.words : "";
    const custom = customBox([
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
      h("div", { style: { marginTop: "16px" } }, custom),
      h("div", { class: "acts end", style: { marginTop: "16px" } }, reset, save),
      s.el);
    return holder;
  }
  return cfgP.then((cfg) => build(cfg));
}

// ---------------------------------------------------------------- 体检 and 日志

const LOG_LINE = /^\[\d{4}-(\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+\S+\s+([A-Z]+):\s?(.*)$/;

function logsBlock() {
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
    h("div", { class: "loghead" }, h("h3", { class: "st", style: { margin: "0", fontSize: "16px" }, text: "日志" }), level.el, copy),
    box, note, err,
    small("最近的在上面，一次看最近 200 行。找人帮忙的时候点「复制」整段贴过去；日志里可能夹着一点记忆的字，发给别人之前看一眼。"));
  return el;
}

async function healthBlock(logs) {
  const r = await api.get("/api/loci/health");
  const look = () => {
    const b = h("button", { class: "lnk", type: "button" }, "看日志");
    b.addEventListener("click", () => logs.scrollIntoView({ behavior: "smooth", block: "start" }));
    return b;
  };
  return h("div", null, (r.checks || []).map((c) => {
    const bad = c.status === "warn" || c.status === "error";
    return setRow({ text: c.label, why: bad && c.action ? c.action : null, wide: true, stack: true,
      right: h("span", { class: "acts", style: { flexWrap: "nowrap" } },
        h("span", { class: "r wrap", style: bad ? { color: "var(--ink)" } : null, text: c.message }), bad ? look() : null) });
  }));
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
          setRow({ text: "有新版本了", wide: true, why: h("span", null, `${c.latest.tag} · `, see),
            right: acts(off(btn("一键更新", { dark: true }))) }),
          notes);
      } else {
        fill(second, setRow({ text: c.words || "", wide: true }));
      }
    } catch (e) { s.err(e); }
  }));
  return h("div", null,
    setRow({ text: "现在的版本", wide: true,
      right: h("span", { class: "acts", style: { flexWrap: "nowrap" } }, h("span", { class: "r", style: { color: "var(--ink)" }, text: `v${v.version}` }), check) }),
    second, s.el);
}

// ---------------------------------------------------------------- the page

export default {
  render(view) {
    const cfgP = api.get("/api/config");
    cfgP.catch(() => {});
    const logs = logsBlock();
    view.append(subbar({}), h("main", { class: "sections setting", style: "--gl-w: 96px" },
      hostsGroup(),
      group("称呼", { tip: TIP.names }, later(() => namesBlock(cfgP))),
      group("账号", {}, later(accountBlock)),
      group("模型", {}, later(() => modelsBlock(cfgP))),
      group("向量", { tip: TIP.vec }, later(vectorBlock)),
      importGroup(),
      exportGroup(),
      group("阈值", { tip: TIP.thresholds }, later(() => thresholdsBlock(cfgP))),
      group("体检", { tip: TIP.health }, later(() => healthBlock(logs)), logs),
      group("版本", {}, later(versionBlock))));
  },
};
