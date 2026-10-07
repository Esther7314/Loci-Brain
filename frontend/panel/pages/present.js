/* ==========================================================
   pages/present.js — present and its 高级设置 (boards present, present-phone,
   present-settings)

   #/present           自助压缩 · 昨日日报 · 自动唤醒 · 主动推送, as the board lays them out.
   #/present/settings  the three prompt cards: 自助压缩的, 写日报的, 自动唤醒的. The board's
                       samples under the first two are left out: their text speaks of the
                       owner in the third person, which shipped code does not
                       (scripts/check_english.py).

   The server has no present yet (note n8): the page says 「还没接上」, nothing is read and
   nothing is written, every control is off. What a self-compression writes goes to the
   model only (n8): the page holds its settings, never that text.
   ========================================================== */

import { h, subbar, group, row, tip, btn, sw, radio, num, customBox } from "../ui.js";
import { href } from "../router.js";

const NOT_WIRED = "还没接上";

const TIP = {
  report: "夜里把这一天写成一份日报，第二天开一个新窗口，从日报接着聊。",
  when: "在夜间 4–8 点之间，你没有说话 30 分钟之后就会写日报。",
  compress: "快满的时候他自己压一下，压完换个新窗口接着聊。",
  wake: "隔一阵叫醒他一次，让他自己想想、做点事。1小时之内可以保住缓存，你下次开口接得更快、也更省。醒来的时候会顺手带上昨夜的梦和 muse 攒下的，这两样各自在 dream、muse 的高级设置里开关。",
  push: "他醒来想跟你说话的时候，用 Bark 推到你手机上。",
};

function off(el) {
  if (el.matches("input, button, textarea, select")) el.disabled = true;
  for (const c of el.querySelectorAll("input, button, textarea, select")) c.disabled = true;
  return el;
}

function acts(...kids) {
  return h("span", { class: "acts", style: { flexWrap: "nowrap" } }, kids);
}

function setRow({ text = "", lead, why, right, wide = false, stack = false }) {
  const layout = ["set", wide ? "wide" : "", stack ? "stack" : ""].filter(Boolean).join(" ");
  return row({ text, lead, why, right: right || h("span"), layout });
}

function box(value, label) {
  return off(num({ value, "aria-label": label }));
}

function to() {
  return h("span", { class: "why", text: "到" });
}

function small(text, style) {
  return h("p", { class: "why", style: { margin: "8px 0 4px", lineHeight: "1.6", ...(style || {}) }, text });
}

/** A radio choice standing as a row's line. */
function choice(name, label, checked, ...extra) {
  return off(radio({ name, label, checked }, ...extra));
}

function addLink(words, style) {
  return h("div", { style }, off(h("button", { class: "lnk", type: "button", style: { fontSize: "14px" } }, words)));
}

function compressGroup() {
  const custom = customBox([
    h("div", { style: { marginTop: "6px" } },
      setRow({ text: "弱提醒线", why: "这个水位线会提醒他，压缩与否由他决定。", right: acts(box("", "弱提醒线")) }),
      addLink("+ 再加一条提醒", { padding: "4px 0 10px" }),
      setRow({ text: "强制压缩线", why: "兜底压缩线，不考虑他的意见了。", right: acts(box("", "强制压缩线")) })),
    h("p", { class: "why", style: { margin: "10px 0 0", lineHeight: "1.6" },
      text: "如果你开 auto compact（自动压缩），记得关掉，不然两边会起冲突。" }),
  ], { open: true });
  return group("自助压缩", { tip: TIP.compress },
    h("div", null,
      setRow({ text: "开关", right: acts(off(sw({ checked: true, label: "自助压缩开关" }))) }),
      setRow({ text: "压缩水位线", right: acts(box("", "水位线")) }),
      setRow({ text: "压缩完留多少条原话", right: acts(box("", "留多少条")) }),
      setRow({ text: "手动压缩", why: "不等水位线，现在就压一次", right: acts(off(btn("现在压"))) })),
    custom);
}

function reportGroup() {
  const custom = customBox([
    small("默认一天一个窗。想几天一个窗，或者自己决定什么时候换，在这里改。"),
    setRow({ lead: choice("win", "一天一个窗", true), right: h("span", { class: "r", style: { fontSize: "13px" }, text: "默认" }) }),
    setRow({ lead: choice("win", "每", false, box("", "几天"), " 天换一个窗") }),
    setRow({ lead: choice("win", "手动换窗", false), why: "按「现在写日报」，写完之后你下次开口就是新窗口。", stack: true,
      right: acts(off(btn("现在写日报"))) }),
  ], { open: true });
  return group("昨日日报", { tip: TIP.report },
    h("div", null, setRow({ text: "什么时候写", wide: true,
      right: acts(box("04:00", "从几点"), to(), box("08:00", "到几点"), tip(TIP.when)) })),
    h("div", null, h("h3", { class: "st", style: { margin: "0 0 6px" }, text: "昨天的日报" }),
      h("div", { class: "bars", "aria-hidden": "true" }, [100, 94, 100, 88, 100, 60].map((w) => h("div", { class: "bar", style: { width: `${w}%` } })))),
    h("div", null, h("h3", { class: "st dim", style: { margin: "0 0 6px" }, text: "漏了的时候" }),
      setRow({ text: "昨天的还没写", right: acts(off(btn("补一份"))) })),
    custom);
}

function wakeGroup() {
  const custom = customBox([
    small("默认全天按上面的间隔醒。也可以只在某几个时间段里醒。"),
    setRow({ lead: choice("wake", "全天都醒，不分时间段", true), right: h("span", { class: "r", style: { fontSize: "13px" }, text: "默认" }) }),
    setRow({ lead: choice("wake", "自定义时间段", false), wide: true, stack: true,
      right: acts(box("09:00", "从"), to(), box("22:00", "到")) }),
    addLink("+ 增加一个时间段", { padding: "4px 0 2px 24px" }),
  ], { open: true });
  return group("自动唤醒", { tip: TIP.wake },
    h("div", null,
      setRow({ text: "开着", right: acts(off(sw({ checked: true, label: "自动唤醒开关" }))) }),
      setRow({ text: "多久醒一次", right: acts(box("", "间隔"), h("span", { class: "why", text: "分钟" })) }),
      setRow({ text: "免打扰", wide: true, right: acts(box("", "从"), to(), box("", "到")) })),
    custom);
}

function pushGroup() {
  return group("主动推送", { tip: TIP.push }, h("div", null,
    h("div", { style: { display: "flex", alignItems: "baseline", justifyContent: "space-between", padding: "0 0 10px" } },
      h("span", { class: "c", text: "Bark 推送码" }),
      h("span", { style: { display: "flex", gap: "28px" } },
        off(h("button", { class: "tbtn", type: "button" }, "保存")), off(h("button", { class: "tbtn", type: "button" }, "测试")))),
    off(h("textarea", { class: "bark", rows: "4", placeholder: "粘贴在这里", "aria-label": "Bark 推送码" })),
    h("p", { class: "why", style: { margin: "10px 0 0", lineHeight: "1.6" },
      text: "用 Kelivo 这类别人家的客户端时，他醒来说的话进不了那边的聊天窗口，只能靠推送送到你手机上；自己搭的前端可以直接显示在窗口里。" })));
}

function renderPresent(view) {
  view.append(
    h("div", { class: "subbar one" },
      h("div", { class: "nav tabs" }, h("span", { style: { color: "var(--ink)" }, text: "今天就在眼前" }),
        h("span", { class: "why", text: NOT_WIRED })),
      h("a", { href: href("present", "settings"), style: { fontSize: "15px", color: "var(--small)" }, text: "高级设置" })),
    h("main", { class: "sections", style: "--gl-w: 96px" }, compressGroup(), reportGroup(), wakeGroup(), pushGroup()));
}

/** One prompt card: its title, the prompt box, 恢复默认 / 保存 under it. */
function promptCard(title) {
  return h("div", null,
    h("h3", { class: "st", style: { margin: "0 0 12px" }, text: title }),
    off(h("textarea", { class: "prompt", rows: "9", "aria-label": title })),
    h("div", { class: "acts end", style: { marginTop: "20px" } }, off(btn("恢复默认")), off(btn("保存", { dark: true }))));
}

function renderSettings(view) {
  view.append(
    subbar({ tabs: h("span", { style: { display: "contents" } },
      h("a", { href: href("present"), text: "‹ present" }),
      h("span", { style: { color: "var(--ink)" }, text: "高级设置" }),
      h("span", { class: "why", text: NOT_WIRED })) }),
    h("main", { class: "sections" },
      group("提示词", {}, promptCard("自助压缩的提示词"), promptCard("写日报的提示词"),
        promptCard("自动唤醒的提示词"))));
}

export default {
  render(view, route) {
    return route.tab === "settings" ? renderSettings(view) : renderPresent(view);
  },
};
