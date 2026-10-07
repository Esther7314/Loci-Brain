/* ==========================================================
   api.js — the panel's one road to the server

   Same-origin JSON. Reads are GET with a query; writes are JSON POST, which is what the
   server's write check wants (web/_guards._write_body: same Origin, application/json —
   the browser sends the Origin itself). A failure throws ApiError carrying the server's
   own words (`error`), so a page shows what the server said rather than a code.

   401 is the gate's signal: the panel is locked and this browser has no session. The
   current address is kept and the page goes to the login page, which comes back to it
   after logging in.
   ========================================================== */

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

const BACK_KEY = "loci.back";

/** Remember where to come back to, and go to the login page. */
export function toLogin() {
  const here = location.hash || "#/breath";
  if (!here.startsWith("#/login") && !here.startsWith("#/forgot")) {
    try { sessionStorage.setItem(BACK_KEY, here); } catch (_) { /* private mode */ }
  }
  if (!location.hash.startsWith("#/login")) location.hash = "#/login";
}

/** Where to go after logging in: the address the 401 interrupted, else breath. */
export function takeBack() {
  let back = "";
  try { back = sessionStorage.getItem(BACK_KEY) || ""; sessionStorage.removeItem(BACK_KEY); } catch (_) { /* private mode */ }
  return back && !back.startsWith("#/login") && !back.startsWith("#/forgot") ? back : "#/breath";
}

function withQuery(path, query) {
  if (!query) return path;
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `${path}${path.includes("?") ? "&" : "?"}${s}` : path;
}

async function send(path, init, { gate = true } = {}) {
  let r;
  try {
    r = await fetch(path, { credentials: "same-origin", ...init });
  } catch (e) {
    throw new ApiError(e && e.message ? e.message : String(e), 0, null);
  }
  const body = await r.json().catch(() => null);
  if (r.status === 401 && gate) {
    toLogin();
  }
  if (!r.ok) {
    const words = body && typeof body.error === "string" && body.error ? body.error : `HTTP ${r.status}`;
    throw new ApiError(words, r.status, body);
  }
  return body;
}

/** GET `path` with `query` (an object; empty values are left out). */
export function get(path, query) {
  return send(withQuery(path, query), { method: "GET" });
}

/** POST `body` as JSON. `gate: false` for the gate's own routes, whose 401 means a
 *  wrong password rather than "log in first". */
export function post(path, body, opts) {
  return send(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  }, opts);
}

/** POST a FormData (a file upload) as multipart; the browser sets the boundary and the
 *  Origin the server's write check wants. Errors as for post. */
export function postForm(path, form) {
  return send(path, { method: "POST", body: form });
}

/** GET a file the server hands out (an export zip) and give it to the browser to save
 *  under the server's own filename. A refusal arrives as JSON and throws ApiError with its
 *  words, like every other read. */
export async function download(path, fallbackName = "download") {
  let r;
  try {
    r = await fetch(path, { credentials: "same-origin" });
  } catch (e) {
    throw new ApiError(e && e.message ? e.message : String(e), 0, null);
  }
  if (!r.ok) {
    const body = await r.json().catch(() => null);
    if (r.status === 401) toLogin();
    const words = body && typeof body.error === "string" && body.error ? body.error : `HTTP ${r.status}`;
    throw new ApiError(words, r.status, body);
  }
  const blob = await r.blob();
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(r.headers.get("content-disposition") || "");
  const name = m ? decodeURIComponent(m[1]) : fallbackName;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
  return { name, headers: r.headers };
}

/** The path segment for an id or a name: always encoded. */
export function seg(value) {
  return encodeURIComponent(String(value));
}
