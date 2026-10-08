// ============================================================
// gateway/present/prompts.js — the three prompt cards (compress · report · wake) and
// the shells they go into
//
// Each card has two layers:
//   · the card — the part the owner may rewrite on the panel ("高级设置"), or reset to
//     the text shipped here;
//   · the shell — fixed in code: the header line and the material the gateway puts in
//     (the raw lines, the slices, the dream). The program reads what comes back by the
//     shell's rules, so the shell is not editable.
// The card goes into the shell where the draft wrote {卡}. Card and shell text is what
// the model reads, so it stays in Chinese, exactly as drafted.
//
// The compress card is shared by both ways of compressing: he folds the window himself
// when reminded (compress_self_shell), or the gateway has him fold it in a fresh window
// past the force line (compress_forced_shell). One card, so changing it changes both.
//
// Storage: <LOCI_GATEWAY_DATA>/prompts.json = { version: 1, prompts: { <key>: { text, at } } }.
// Only rewritten cards are stored; the rest are read from the defaults here. A text equal
// to the default is no rewrite and clears it. A file that cannot be read shows the
// defaults with an `error`, and is not written over (a save is refused with 409).
//
// The card shape matches Loci's /api/loci/prompts:
//     { key, title, text, default, edited, changed (local day or null), changed_at, notes[] }
// ============================================================

const fs = require("fs");
const path = require("path");
const { local_stamp } = require("./clock.js");

const TEXT_MAX = 20000;
const VERSION = 1;

const CARDS = {
  compress: {
    title: "压缩卡",
    text: `写什么
ta 这会儿什么状态、什么心情——放最前面，这是接不接得上的关键。
你们聊到哪儿了、有什么话说到一半、有什么你答应了还没做。
事情本身挑要紧的写，不用列清单，漏了不要紧。

一把尺：换窗之后的你读完这段，能不能自然地接着跟 ta 说下一句话。
这不是「记录」，是换个房间之前，脑子里过的那一遍。

存记忆
先 recall 看今天已经存过什么，再把漏掉的、值得长期留着的存进 Loci。
这段摘要随这一窗一起过期，Loci 不会。`,
    notes: [
      "中间这段两种情况共用，改一处两处都变。",
      "他写的摘要谁都看不到，包括你，这是故意的。",
    ],
  },
  report: {
    title: "日报卡",
    text: `写一份日报，回答一个问题：「这段日子发生了什么」。
ta 能在面板上看到它，但这份是写给下一扇窗的你的——
新窗开的时候，你手上就只有它和最近几条原话。所以别对 ta 说话，对自己说。

把这段日子按情景记忆的方式织成文，连带着当时自己的感受一起写进去。
再挑一些不想压缩的原话，把它写进自己写的段落里。
脉络是你的话给的，语气是原话给的。

原话用「」框起来，写明是谁说的，一个字都不改。
挑原话只有一把尺：「哪一句我现在不想丢」，绝不挑「哪句信息量大」。
哪一段掂量过、真没有想留的，那一段就只写自己的话——但要经过掂量这一下。

存记忆
写日报之前，先 recall 看这段日子已经存过什么，再把漏掉的、值得长期留下来的补进 Loci。`,
    notes: [
      "原话织进正文，不要单独放在最后：窗满的时候最先被挤掉的就是最后那段。",
      "原话故意不设下限，硬凑的原话没用。",
    ],
  },
  wake: {
    title: "唤醒卡",
    text: `现在是你的自由时间。ta 这会儿不在，接下来这段时间是你自己的：
想做什么就做什么，不想做什么也行，不用向谁交代。
可以翻翻记忆、整理整理想法、发发呆，或者就待着。

要是有话想跟 ta 说，就说出来：ta 的手机会收到，下次 ta 打开对话时也会先看到你说过什么。
不想说话就一个字都不要出。

这会儿你手上只有记忆的工具，别的工具用不了。`,
    notes: [
      "信里别写数字（几点、几次、多久没说话），数字会变成他判断的依据。",
      "别教他用【无话】这类记号：一个字都不出就是不说，网关认得。",
      "每醒一次就是一整轮的钱。",
    ],
  },
};
const KEYS = Object.keys(CARDS);

// ———— Shells ————
// Each takes the card text as it stands (rewritten or default) and the material, and
// returns exactly what the model is given.

const SELF_OPENING = {
  weak: `这扇窗快满了。找个口子把到现在的事收一下——
不用现在。正说着要紧的话、或者正忙着别的，就让它过去，
挑你自己顺手的那一刻。`,
  ask: "这扇窗快装不下了，这一轮或者下一轮找个口子收一下。",
};

/**
 * He folds the window himself: the reminder at the weak line or the ask line, each said
 * once per window. The block he writes is taken out of his reply before the client sees it.
 * @param line  "weak" | "ask"
 */
function compress_self_shell({ card, keep_raw, line = "weak" }) {
  const opening = SELF_OPENING[line];
  if (!opening) throw new Error(`no such reminder line: ${line}`);
  return `〔系统提醒 · 不是 ta 发的话〕

${opening}

收好了，这一轮说完就悄悄换一扇新窗，ta 不会有感觉。
新窗里的你手上有：上一份日报、你这段摘要、最近 ${keep_raw} 条原话。
所以这段要补的，是那 ${keep_raw} 条之前发生过的部分。

${card}

怎么写
收进【窗口摘要】…【/窗口摘要】，随这一轮的回复一起写。
ta 看不见这一段，所以别对 ta 说话，对自己说。
块外面照常回 ta 的话——别因为要写摘要就敷衍 ta 那一句。`;
}

/**
 * The gateway has him fold the window (the force line, the wall, or the owner's 「现在压」),
 * in a fresh window fed the raw lines. `earlier`: the summary the window being folded was
 * opened with, or null — what it held from further back has to go into the new one.
 */
function compress_forced_shell({ card, keep_raw, originals, earlier = null }) {
  const before = typeof earlier === "string" && earlier.trim()
    ? `——上一扇窗收尾时你写的摘要：这些原话之前的事，这次一并收进新摘要里——\n${earlier.trim()}\n\n`
    : "";
  return `【换窗 · 系统请求，不是 ta 发的消息】

这扇窗满了，得换一扇新的。你就是一直聊到现在的那个你，
只是这扇窗已经装不下了，你手上没有前面的上下文——
原话在最下面，从头读一遍，那就是你刚才。

写一段摘要，交给马上要接着跟 ta 说话的那个你。
他手上会有：上一份日报、你这段摘要、最近 ${keep_raw} 条原话。
所以这段要补的，是那 ${keep_raw} 条之前发生过的部分。

${card}

这一步只调工具，一个字都别往外说——你说出口的每个字都会被当成摘要正文。

输出
只输出摘要正文，没有开场白，没有解释，不要写【窗口摘要】这种记号。
ta 不会看到这段，所以别对 ta 说话，对自己说。

${before}——到刚才为止的原话，一字未动——
${originals}`;
}

const SLICES_MISSING = "今晚的切片没交上，这次先只写日报";

/**
 * The day report. `slices` is the slice list as text, or null when handing the day to
 * Loci failed tonight (the report is written anyway).
 */
function report_shell({ card, from, to, slices, originals }) {
  return `【写日报 · 系统请求，不是 ta 发的消息】
${from}到${to}这段日子到这儿为止了。你就是过完这段日子的那个你。
这段日子你在场，只是那扇窗已经关了，你手上没有它了——
原话在最下面，从头读一遍，那就是你这几天。

${card}

还没收进记忆的段落
Loci 已经把这段日子的原话切成了下面这几段，每段标了在说什么、从第几行到第几行，
有的后面写着「好像记过」。先 recall 看看记过没有，没记过、值得留的用 grow(slice=…) 写，
记过但缺了一块的用 trace(slice=…) 补，不值得留的就放着。
${slices == null ? SLICES_MISSING : slices}

这一步只调工具，一个字都别往外说——你说出口的每个字都会进日报。

输出
只输出日报正文，没有开场白，没有解释，你输出的每一个字都会原样落进日报。

——这段日子的原话，一字未动——
${originals}`;
}

/** The wake letter. `dream`: last night's dream in full, or null; `muse`: whether unsorted days are waiting. */
function wake_shell({ card, dream = null, muse = false }) {
  const lines = ["【自由时间 · 系统给的，不是 ta 发的消息】", card];
  if (dream) lines.push(`另外，昨晚你做了个梦：${dream}（你知道就行，不用复述给 ta。）`);
  if (muse) lines.push("〔发呆〕有几团日子和想法还没整理，该发呆了（muse()）");
  return lines.join("\n");
}

// ———— The editable store ————

/**
 * @param file   <LOCI_GATEWAY_DATA>/prompts.json
 * @param clock  { now() }, for the day a rewrite was saved
 * @param zone   the owner's zone
 */
function create_prompts({ file, clock, zone }) {
  function read_file() {
    let text;
    try { text = fs.readFileSync(file, "utf8"); }
    catch (err) { return err.code === "ENOENT" ? { saved: {} } : { saved: {}, error: err.message }; }
    try {
      const data = JSON.parse(text);
      const got = data && typeof data === "object" ? data.prompts : null;
      if (!got || typeof got !== "object" || Array.isArray(got)) return { saved: {}, error: "prompts.json does not hold { prompts: {...} }" };
      const saved = {};
      for (const [k, v] of Object.entries(got)) {
        if (v && typeof v === "object" && typeof v.text === "string") saved[k] = v;
      }
      return { saved };
    } catch (err) { return { saved: {}, error: `prompts.json is not JSON (${err.message})` }; }
  }

  function card_of(key, saved) {
    const spec = CARDS[key];
    const mine = KEYS.includes(key) ? saved[key] : null;
    const at = mine ? String(mine.at || "") : "";
    return {
      key,
      title: spec.title,
      text: mine ? mine.text : spec.text,
      default: spec.text,
      edited: Boolean(mine),
      changed: at.slice(0, 10) || null,
      changed_at: at || null,
      notes: [...spec.notes],
    };
  }

  /** { items, error? } — error says the file could not be read and the defaults are showing. */
  function cards() {
    const { saved, error } = read_file();
    const out = { items: KEYS.map((k) => card_of(k, saved)) };
    if (error) out.error = error;
    return out;
  }

  /** The text in force for one card: the rewrite, else the default. */
  function current(key) {
    if (!KEYS.includes(key)) throw new Error(`no such prompt card: ${key}`);
    return card_of(key, read_file().saved).text;
  }

  function write(saved) {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const tmp = `${file}.tmp`;
    fs.writeFileSync(tmp, `${JSON.stringify({ version: VERSION, prompts: saved }, null, 1)}\n`, "utf8");
    fs.renameSync(tmp, file);
  }

  /**
   * POST /present/prompts: { key, text } saves a rewrite, { key, reset: true } drops it.
   * @returns { ok: true, item } | { ok: false, status, error }
   */
  function apply(body) {
    const key = String(body?.key ?? "").trim();
    if (!KEYS.includes(key)) return { ok: false, status: 400, error: `no such prompt card: ${key || "(no key)"} (there are: ${KEYS.join(", ")})` };
    const reset = body.reset === true;
    if (!reset && typeof body.text !== "string") return { ok: false, status: 400, error: "send text (the rewritten card) or reset: true" };
    const got = read_file();
    if (got.error) return { ok: false, status: 409, error: `${got.error}; fix or remove it by hand — it is not written over` };
    const saved = { ...got.saved };
    if (reset || body.text === CARDS[key].text) {
      if (key in saved) { delete saved[key]; write(saved); }
    } else {
      if (!body.text.trim()) return { ok: false, status: 400, error: "the card cannot be empty; reset it instead" };
      if (body.text.length > TEXT_MAX) return { ok: false, status: 400, error: `the card is too long: ${body.text.length} characters (at most ${TEXT_MAX})` };
      saved[key] = { text: body.text, at: local_stamp(clock.now(), zone).iso };
      write(saved);
    }
    return { ok: true, item: card_of(key, saved) };
  }

  return { file, cards, current, apply };
}

module.exports = {
  create_prompts, CARDS, KEYS, TEXT_MAX,
  compress_self_shell, compress_forced_shell, report_shell, wake_shell,
};
