/* ==========================================================
   pages/login.js — 登录 and 忘记密码 (boards login-web/-phone, forgot-web/-phone)

   #/login    POST /auth/login {password}. The 账号 field is drawn (it reads "user"), and
              the server has no account names yet: only the password is sent.
   #/forgot   GET /auth/recovery-question, then POST /auth/recover {answer, password}.
              With no question set, only the line saying where a reset can be done.
   Both come back to the address the gate interrupted (api.takeBack). The server's own
   words are shown on a refusal (密码不对, 答案不对, 新密码至少 6 位, 试得太密了…).
   ========================================================== */

import * as api from "../api.js";
import { h, fill } from "../ui.js";
import { href } from "../router.js";

function field(label, input) {
  return h("div", { class: "fld" }, h("span", { class: "fl", text: label }), input);
}

/** A password box with its 显示 switch. */
function password(attrs) {
  const input = h("input", { class: "inp", type: "password", ...attrs });
  const eye = h("button", { class: "eye", type: "button", "aria-label": "显示密码", "aria-pressed": "false" }, "显示");
  eye.addEventListener("click", () => {
    const shown = input.type === "password";
    input.type = shown ? "text" : "password";
    eye.setAttribute("aria-pressed", String(shown));
  });
  return { input, el: h("div", { class: "pw" }, input, eye) };
}

function renderLogin(view) {
  const account = h("input", { class: "inp", value: "user", "aria-label": "账号", autocomplete: "username", name: "username" });
  const pw = password({ "aria-label": "密码", autocomplete: "current-password", name: "password" });
  const err = h("p", { class: "err", role: "alert", hidden: true });
  const go = h("button", { class: "go", type: "submit" }, "登录");
  const form = h("form", { novalidate: true },
    h("main", null,
      h("div", { class: "brand" }, "Loci brain"),
      field("账号", account),
      field("密码", pw.el),
      err,
      go,
      h("div", { style: { display: "flex", justifyContent: "center" } },
        h("a", { class: "lnk", href: href("forgot"), text: "忘记密码" }))));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.hidden = true;
    go.disabled = true;
    try {
      await api.post("/auth/login", { password: pw.input.value }, { gate: false });
      pw.input.value = "";
      location.hash = api.takeBack();
    } catch (x) {
      fill(err, x.message);
      err.hidden = false;
      go.disabled = false;
      pw.input.focus();
    }
  });
  view.append(h("div", { class: "gate" }, form));
  setTimeout(() => pw.input.focus(), 0);
}

async function renderForgot(view) {
  const note = h("p", { class: "why note", text: "没设过安全问题的话，这里只能在装 Loci 的那台电脑上打开、直接重设。" });
  const question = h("span", { style: { fontSize: "16px", lineHeight: "1.6" } });
  const answer = h("input", { class: "inp", "aria-label": "答案", autocomplete: "off" });
  const pw1 = password({ "aria-label": "新密码", placeholder: "至少 6 位", autocomplete: "new-password" });
  const pw2 = password({ "aria-label": "再输一次新密码", autocomplete: "new-password" });
  const err = h("p", { class: "err", role: "alert", hidden: true });
  const go = h("button", { class: "go", type: "submit" }, "重设密码");
  const asked = [field("安全问题", question), field("答案", answer), field("新密码", pw1.el),
    field("再输一次", pw2.el), err, go];
  const form = h("form", { novalidate: true },
    h("main", null,
      h("a", { class: "back", href: href("login"), text: "‹ 回到登录" }),
      h("h1", { text: "忘记密码" }),
      asked, note));
  const say = (words) => { fill(err, words); err.hidden = false; };
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.hidden = true;
    if (pw1.input.value !== pw2.input.value) { say("两次不一样"); return; }
    go.disabled = true;
    try {
      await api.post("/auth/recover", { answer: answer.value, password: pw1.input.value }, { gate: false });
      location.hash = api.takeBack();
    } catch (x) {
      say(x.message);
      go.disabled = false;
    }
  });
  view.append(h("div", { class: "gate forgot" }, form));
  let q = "";
  try {
    q = String((await api.get("/auth/recovery-question")).question || "");
  } catch (x) {
    say(x.message);
  }
  question.textContent = q;
  for (const n of asked) if (n !== err) n.hidden = !q;
  if (q) answer.focus();
}

export default {
  render(view, route) {
    return route.page === "forgot" ? renderForgot(view) : renderLogin(view);
  },
};
