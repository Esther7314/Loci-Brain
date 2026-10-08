# -*- coding: utf-8 -*-
"""
replay_week.py — push a week of chat through a running dev gateway, turn by turn

    python scripts/replay_week.py --dry-run                  # check the fixture, print the plan
    python scripts/replay_week.py --panel-dir <folder>       # replay it (see "Running" below)

The chat is scripts/fixtures/week_chat.json: 阿青 (role user) and 小满 (role assistant), the
two people of scripts/dev_panel.py's sample library, seven days with a timestamp on every
message. Replaying it gives Loci a week of real input, so what Loci makes of it — the day
reports, the slices and the memories grown from them, dreams, the window summaries of a
pack — comes from the real pipeline with a real (cheap) model.

What runs where
    dev_panel.py --gateway   Loci (the sample library) and the gateway, both throwaway.
    this script              the client: it posts each of 阿青's lines to the gateway with
                             the history a client would send (the replies as it received
                             them), and a stand-in upstream the gateway is pointed at.

The stand-in (127.0.0.1:--listen) is the gateway's upstream and Loci's side model at once:
    · a chat turn this script sent (it carries an `x-replay-turn` header, which the gateway
      passes on) is answered with 小满's line from the fixture (`--replies scripted`, the
      default: the day files hold exactly the fixture, and chat turns cost nothing), or
      forwarded to the real model (`--replies model`: 小满's lines are the model's, and
      the fixture's are not sent)
    · everything else — the gateway's own turns (day report, wake, pack) and Loci's side
      model (slicing the night's lines, tags, dreams) — goes to the real upstream with the
      real key and model swapped in
    🔴 The real key lives in this process's memory only. It is read from an environment
       variable (never an argument: argv shows up in process lists), sent only to
       --upstream, and never written or logged; dev_panel.py gives the gateway and Loci a
       placeholder (`replay-placeholder-key`) that is worthless anywhere else.

Which day a turn belongs to
    The gateway stamps each line with its own clock when it stores it (present/day_store.js:
    the day file is the owner's local day in LOCI_TZ) and Loci stamps what it writes with
    its own. Neither takes a time from the request. So the fixture's timestamps are honoured
    only when both run on a fake clock: dev_panel.py --clock starts them on one, and this
    script moves it (both files, monotonic) before each line and — through the stand-in —
    to the reply's time before the reply is handed back, so each line is stored at its
    fixture time.
    With a fake clock the nights run the way they do live: the clock is set to 04:35 the
    next morning (inside the default report window, 阿青 quiet for hours), and the
    gateway's heartbeat (one tick per real minute) hands the day's lines to Loci, writes
    the day report and flips the window; dreams weave when the model calls breath (the
    system prompt asks it to) in an own turn and the pressure is over the line, at most one
    per fake day. In the fixture's away stretch wake is switched on and the clock is moved
    to each wake's due time.
    Without a fake clock every line lands on the real today; each fixture day is closed by
    「现在写日报」 (POST /present/report {kind: "now"}), wake is skipped (it needs real hours),
    and Loci weaves at most one dream per real day.

Running (each command in its own terminal; the dry run prints them with the right values)
    1. python scripts/dev_panel.py --gateway --keep --upstream http://127.0.0.1:18790/v1 \\
           --clock 2026-10-14T08:00:00+08:00
    2. set REPLAY_UPSTREAM_KEY (and REPLAY_UPSTREAM_URL / REPLAY_UPSTREAM_MODEL, or pass
       --upstream / --model), then
       python scripts/replay_week.py --panel-dir <the folder dev_panel printed>
    A replay is one pass: a run cut off half-way leaves a half week in the library, and the
    next one starts on a fresh panel.

Public surface: run as a script. `load_fixture`, `problems`, `turns_of`, `build_plan`,
`estimate` and `StandIn` are imported by tests/test_replay_week.py.
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "scripts", "fixtures", "week_chat.json")
SYSTEM_DOC = os.path.join(ROOT, "docs", "系统提示-中文.md")
DEFAULT_LISTEN = 18790
PLACEHOLDER_KEY = "replay-placeholder-key"   # what the gateway holds; the stand-in swaps it
TURN_HEADER = "x-replay-turn"
NIGHT_AT = (4, 35)                    # inside the default report window 04:00–08:00
LATE_NIGHT_UNTIL = 6                  # a line before 06:00 may still belong to the day before
CJK = re.compile(r"[⺀-鿿豈-﫿＀-￯]")

# What the gateway's own turns may cost (gateway/present/settings.js own.tool_rounds).
DEFAULT_TOOL_ROUNDS = 6

# Seconds to wait before each retry of a request the upstream answered 429.
RETRY_WAITS = (5, 15, 30, 60)


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------

@dataclass
class Turn:
    index: int            # position in the whole week, from 0
    day: int              # fixture day, from 0
    user_at: datetime
    user: str
    reply_at: datetime
    reply: str


@dataclass
class Step:
    kind: str             # turn · night · away · compress · report_now
    at: datetime | None = None
    turn: Turn | None = None
    until: datetime | None = None
    day: date | None = None           # the day a night / report closes out
    note: str = ""


@dataclass
class Plan:
    clock: bool
    steps: list = field(default_factory=list)


def load_fixture(path: str = FIXTURE) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _instant(text, where: str, out: list) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(text))
    except ValueError:
        out.append(f"{where}: not an ISO time: {text!r}")
        return None
    if t.tzinfo is None:
        out.append(f"{where}: time has no offset: {text!r}")
        return None
    return t


def problems(fx: dict) -> list[str]:
    """Everything wrong with the fixture, as sentences; [] when it can be replayed."""
    out: list[str] = []
    for key in ("owner", "ai", "system", "days"):
        if not fx.get(key):
            out.append(f"missing `{key}`")
    if out:
        return out
    prev_at, prev_role, prev_day = None, "assistant", None
    for d, day in enumerate(fx["days"]):
        try:
            this = date.fromisoformat(day.get("date", ""))
        except ValueError:
            out.append(f"day {d + 1}: bad date {day.get('date')!r}")
            continue
        if prev_day is not None and this != prev_day + timedelta(days=1):
            out.append(f"day {d + 1}: {this} does not follow {prev_day}")
        prev_day = this
        msgs = day.get("messages") or []
        if not msgs:
            out.append(f"day {d + 1}: no messages")
        for m, msg in enumerate(msgs):
            where = f"day {d + 1} message {m + 1}"
            at = _instant(msg.get("at"), where, out)
            role, text = msg.get("role"), msg.get("content")
            if role not in ("user", "assistant"):
                out.append(f"{where}: role must be user or assistant, got {role!r}")
            elif role == prev_role:
                out.append(f"{where}: two {role} messages in a row (every user line gets one reply)")
            prev_role = role
            if not isinstance(text, str) or not text.strip():
                out.append(f"{where}: empty content")
            if at is None:
                continue
            if prev_at is not None and at <= prev_at:
                out.append(f"{where}: {at.isoformat()} is not after the message before it")
            prev_at = at
            local = at.date()
            late = local == this + timedelta(days=1) and at.hour < LATE_NIGHT_UNTIL
            if local != this and not late:
                out.append(f"{where}: {at.isoformat()} is not on {this} (nor its late night)")
    if prev_role != "assistant":
        out.append("the week ends on a user line with no reply")
    stamps = [_instant(m["at"], "", []) for day in fx["days"] for m in day.get("messages") or []]
    for a, stretch in enumerate(fx.get("away") or []):
        where = f"away {a + 1}"
        start, end = _instant(stretch.get("from"), where, out), _instant(stretch.get("to"), where, out)
        if start is None or end is None:
            continue
        if end <= start:
            out.append(f"{where}: ends before it starts")
        if any(s is not None and start <= s <= end for s in stamps):
            out.append(f"{where}: a message falls inside it")
        if not any(s is not None and s < start for s in stamps):
            out.append(f"{where}: starts before the first message")
    for c, press in enumerate(fx.get("compress") or []):
        where = f"compress {c + 1}"
        at = _instant(press.get("at"), where, out)
        if at is not None and not any(s is not None and s < at for s in stamps):
            out.append(f"{where}: before the first message")
        if at is not None and any(s == at for s in stamps):
            out.append(f"{where}: at the same second as a message")
    return out


def turns_of(fx: dict, shift: timedelta = timedelta(0)) -> list[Turn]:
    out = []
    for d, day in enumerate(fx["days"]):
        msgs = day["messages"]
        for u, a in zip(msgs[0::2], msgs[1::2]):
            out.append(Turn(index=len(out), day=d,
                            user_at=datetime.fromisoformat(u["at"]) + shift, user=u["content"],
                            reply_at=datetime.fromisoformat(a["at"]) + shift, reply=a["content"]))
    return out


def _next_night(after: datetime) -> datetime:
    t = after.replace(hour=NIGHT_AT[0], minute=NIGHT_AT[1], second=0, microsecond=0)
    return t if t > after else t + timedelta(days=1)


def first_days(fx: dict, n: int) -> dict:
    """The fixture cut to its first n days (all of them for n <= 0)."""
    return fx if n <= 0 else dict(fx, days=fx["days"][:n])


def build_plan(fx: dict, clock: bool, shift: timedelta = timedelta(0)) -> Plan:
    """The steps of a replay, in time order: each turn; the fixture's 「现在压」 presses
    (`compress`, as if pressed on the panel's present page); with a clock, each night's
    04:35 that falls between two lines (and the one after the last line) and the away
    stretches; without one, a 「现在写日报」 after each fixture day."""
    turns = turns_of(fx, shift)
    away = sorted(((datetime.fromisoformat(s["from"]) + shift, datetime.fromisoformat(s["to"]) + shift,
                    s.get("note", "")) for s in fx.get("away") or []))
    presses = sorted((datetime.fromisoformat(c["at"]) + shift, c.get("note", ""))
                     for c in fx.get("compress") or [])

    def day_of(t: Turn) -> date:
        return date.fromisoformat(fx["days"][t.day]["date"]) + timedelta(days=shift.days)

    plan = Plan(clock=clock)
    prev = None
    for t in turns:
        if prev is not None:
            gap = [Step("compress", at=at, note=note) for at, note in presses
                   if prev.reply_at < at < t.user_at]
            if clock:
                night = _next_night(prev.reply_at)
                while night < t.user_at:
                    gap.append(Step("night", at=night, day=(night - timedelta(hours=12)).date()))
                    night += timedelta(days=1)
                gap += [Step("away", at=start, until=end, note=note) for start, end, note in away
                        if prev.reply_at < start and end < t.user_at]
            elif prev.day != t.day:
                gap.append(Step("report_now", at=t.user_at - timedelta(microseconds=1), day=day_of(prev)))
            plan.steps += sorted(gap, key=lambda s: s.at)
        plan.steps.append(Step("turn", at=t.user_at, turn=t))
        prev = t
    if prev is not None:
        if clock:
            night = _next_night(prev.reply_at)
            plan.steps.append(Step("night", at=night, day=(night - timedelta(hours=12)).date()))
        else:
            plan.steps.append(Step("report_now", day=day_of(prev)))
    return plan


def estimate(fx: dict, plan: Plan, replies: str, wake_cap: int, tool_rounds: int = DEFAULT_TOOL_ROUNDS) -> dict:
    """Characters, and how many model calls a full replay makes, as ranges."""
    turns = turns_of(fx)
    texts = [m["content"] for day in fx["days"] for m in day["messages"]]
    nights = sum(1 for s in plan.steps if s.kind in ("night", "report_now"))
    stretches = sum(1 for s in plan.steps if s.kind == "away")
    presses = sum(1 for s in plan.steps if s.kind == "compress")
    wakes = stretches * wake_cap if plan.clock else 0
    own = (1, 1 + tool_rounds)
    main = {
        "chat turns": (len(turns), len(turns)) if replies == "model" else (0, 0),
        "day reports": (nights * own[0], nights * own[1]),
        "wakes": (0, wakes * own[1]),
        "packs (the 「现在压」 presses)": (presses * own[0], presses * own[1]),
    }
    side = {
        "slicing (one per day batch)": (nights, nights),
        "tags for grown memories (one each)": (2 * nights, 8 * nights),
        "dreams (at most one a day, up to 3 tries)": (0, 3 * nights),
    }
    total = tuple(sum(v[i] for v in list(main.values()) + list(side.values())) for i in (0, 1))
    return {
        "days": len(fx["days"]), "turns": len(turns), "messages": len(texts),
        "chars": sum(len(t) for t in texts), "cjk": sum(len(CJK.findall(t)) for t in texts),
        "chars_owner": sum(len(t.user) for t in turns), "chars_ai": sum(len(t.reply) for t in turns),
        "nights": nights, "wake_stretches": stretches, "main": main, "side": side, "total": total,
    }


# ---------------------------------------------------------------------------
# The fake clock (dev_panel.py --clock): two files, one instant
# ---------------------------------------------------------------------------

def _write_atomic(path: str, text: str) -> None:
    tmp = f"{path}.tmp"
    for _ in range(40):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
            return
        except PermissionError:     # Windows: a reader has the file open this instant
            time.sleep(0.05)
    raise OSError(f"could not write the clock file {path}")


class Clock:
    """Loci reads clock.iso (exam/clock.py), the gateway clock.ms (present/clock.js).
    Moves forward only: a store that saw 10:00 and then 09:00 would stamp lines out of order."""

    def __init__(self, files: dict | None):
        self.files = files
        self.lock = threading.Lock()
        self.now = None
        if files:
            self.now = datetime.fromisoformat(open(files["iso"], encoding="utf-8").read().strip())

    def set(self, t: datetime) -> None:
        if not self.files:
            return
        with self.lock:
            if self.now is not None and t < self.now:
                return
            _write_atomic(self.files["iso"], t.isoformat())
            _write_atomic(self.files["ms"], str(int(t.timestamp() * 1000)))
            self.now = t


# ---------------------------------------------------------------------------
# The stand-in upstream
# ---------------------------------------------------------------------------

class StandIn:
    """The gateway's upstream and Loci's side model. Scripted chat turns are answered here;
    everything else is forwarded to `upstream` with `key` and `model` swapped in.
    Logs a request's method, path, status and which way it went — never a header or a body."""

    def __init__(self, turns: list[Turn], *, replies: str, upstream: str, key: str, model: str,
                 clock: Clock | None = None, listen: int = DEFAULT_LISTEN, concurrency: int = 4,
                 log=print):
        self.turns = {t.index: t for t in turns}
        self.replies = replies
        self.upstream = upstream.rstrip("/")
        self.upstream_lock = threading.BoundedSemaphore(concurrency)
        self._key = key
        self.model = model
        self.clock = clock or Clock(None)
        self.log = log
        self.counts = {"scripted": 0, "forwarded": 0, "refused": 0}
        self.server = ThreadingHTTPServer(("127.0.0.1", listen), self._handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # -- answering --

    def _scripted(self, turn: Turn, body: dict) -> tuple[int, dict, bytes]:
        self.clock.set(turn.reply_at)
        created = int(time.time())
        model = body.get("model") or "stand-in"
        if body.get("stream"):
            chunk = {"id": f"replay-{turn.index}", "object": "chat.completion.chunk", "created": created,
                     "model": model, "choices": [{"index": 0, "delta": {"role": "assistant", "content": turn.reply},
                                                  "finish_reason": None}]}
            end = dict(chunk, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}])
            data = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in (chunk, end)) + "data: [DONE]\n\n"
            return 200, {"Content-Type": "text/event-stream; charset=utf-8"}, data.encode("utf-8")
        answer = {"id": f"replay-{turn.index}", "object": "chat.completion", "created": created, "model": model,
                  "choices": [{"index": 0, "message": {"role": "assistant", "content": turn.reply},
                               "finish_reason": "stop"}]}
        return 200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(answer, ensure_ascii=False).encode("utf-8")

    def _forward_request(self, path: str, body: dict):
        """The request as it goes to the real upstream: the real model and key, nothing of
        the caller's headers (the placeholder key stays here)."""
        if not self._key or not self.upstream:
            return None
        if self.model:
            body = dict(body, model=self.model)
        url = self.upstream + (path[len("/v1"):] if path.startswith("/v1/") else path)
        return urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                      method="POST",
                                      headers={"Content-Type": "application/json",
                                               "Accept": "application/json, text/event-stream",
                                               "Authorization": f"Bearer {self._key}"})

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"     # one response per connection, closed at its end

            def log_message(self, *args):     # silent: the stand-in logs through outer.log only
                pass

            def _send(self, status, headers, data: bytes):
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _json(self, status, obj):
                self._send(status, {"Content-Type": "application/json; charset=utf-8"},
                           json.dumps(obj, ensure_ascii=False).encode("utf-8"))

            def do_GET(self):
                path = self.path.split("?")[0]
                if path.rstrip("/").endswith("/models"):
                    return self._json(200, {"object": "list", "data": []})
                return self._json(404, {"error": f"the stand-in has no {path}"})

            def do_POST(self):
                path = self.path.split("?")[0]
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    body = None
                if not path.endswith("/chat/completions") or not isinstance(body, dict):
                    return self._json(404, {"error": f"the stand-in only answers chat completions, not {path}"})
                mark = self.headers.get(TURN_HEADER)
                turn = outer.turns.get(int(mark)) if mark and mark.isdigit() else None
                if turn is not None and outer.replies == "scripted":
                    outer.counts["scripted"] += 1
                    outer.log(f"  stand-in: turn {turn.index + 1} answered from the fixture")
                    return self._send(*outer._scripted(turn, body))
                req = outer._forward_request(path, body)
                if req is None:
                    outer.counts["refused"] += 1
                    outer.log("  stand-in: refused a request (no upstream key: nothing is sent anywhere)")
                    return self._json(503, {"error": "the replay's stand-in has no upstream key"})
                outer.counts["forwarded"] += 1
                kind = f"turn {turn.index + 1}" if turn is not None else "own turn / side model"
                # At most `concurrency` requests upstream at once, and a 429 waited out
                # (RETRY_WAITS): a free or low tier allows few requests in flight. Too few slots
                # and a caller with a short timeout (the side model) gives up while queued.
                with outer.upstream_lock:
                    for wait in (*RETRY_WAITS, None):
                        try:
                            resp = urllib.request.urlopen(req, timeout=600)   # noqa: S310 — the URL is the operator's
                        except urllib.error.HTTPError as e:
                            resp = e
                        except (urllib.error.URLError, OSError) as e:
                            outer.log(f"  stand-in: forwarding failed ({type(e).__name__})")
                            return self._json(502, {"error": f"the stand-in could not reach upstream: {type(e).__name__}"})
                        status = getattr(resp, "status", None) or resp.code
                        if status != 429 or wait is None:
                            break
                        resp.close()
                        outer.log(f"  stand-in: {kind} → 429, waiting {wait}s")
                        time.sleep(wait)
                        req = outer._forward_request(path, body)
                    outer.log(f"  stand-in: {kind} forwarded → {status}")
                    if turn is not None:
                        outer.clock.set(turn.reply_at)
                    self.send_response(status)
                    self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                    self.end_headers()
                    try:
                        while True:
                            piece = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                            if not piece:
                                break
                            self.wfile.write(piece)
                            self.wfile.flush()
                    finally:
                        resp.close()

        return Handler


# ---------------------------------------------------------------------------
# Talking to the gateway
# ---------------------------------------------------------------------------

class Gateway:
    def __init__(self, handle: dict):
        self.base = handle["gateway"].rstrip("/")
        self._token = handle["gateway_token"]

    def _call(self, method: str, path: str, body=None, headers=None, timeout=30):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        h = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:     # noqa: S310 — loopback
                return r.status, json.loads(r.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8") or "null")
            except ValueError:
                return e.code, None

    def status(self) -> dict:
        code, body = self._call("GET", "/present", None, {"Authorization": f"Bearer {self._token}"})
        if code != 200:
            raise RuntimeError(f"GET /present answered {code}: {body}")
        return body["status"]

    def patch(self, patch: dict) -> None:
        code, body = self._call("POST", "/present", {"patch": patch}, {"Authorization": f"Bearer {self._token}"})
        if code != 200:
            raise RuntimeError(f"POST /present answered {code}: {body}")

    def packs(self) -> dict:
        """The compress part of GET /health: when a window was last folded, failures since."""
        code, body = self._call("GET", "/health")
        return ((body or {}).get("present") or {}).get("compress") or {} if code == 200 else {}

    def compress_now(self) -> tuple[int, dict]:
        return self._call("POST", "/present/compress", {}, {"Authorization": f"Bearer {self._token}"})

    def report_now(self) -> tuple[int, dict]:
        return self._call("POST", "/present/report", {"kind": "now"}, {"Authorization": f"Bearer {self._token}"})

    def chat(self, model: str, messages: list, turn: Turn) -> str:
        code, body = self._call("POST", "/v1/chat/completions",
                                {"model": model, "messages": messages, "stream": False},
                                {"Authorization": f"Bearer {PLACEHOLDER_KEY}", TURN_HEADER: str(turn.index)},
                                timeout=900)
        if code != 200 or not isinstance(body, dict):
            raise RuntimeError(f"turn {turn.index + 1}: the gateway answered {code}: {str(body)[:300]}")
        return str(((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "")


def system_prompt(fx: dict) -> str:
    """The fixture's persona, then the system prompt the docs tell a client to paste."""
    text = fx["system"]
    try:
        doc = open(SYSTEM_DOC, encoding="utf-8").read()
        block = re.search(r"```\s*\n(.*?)```", doc, re.S)
        if block:
            text += "\n\n" + block.group(1).strip()
    except OSError:
        pass
    return text


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def _wait(what: str, seconds: float, check, every: float = 5.0):
    end = time.time() + seconds
    while True:
        got = check()
        if got:
            return got
        if time.time() > end:
            print(f"  ! gave up waiting for {what} after {int(seconds)} s")
            return None
        time.sleep(every)


def _night(gw: Gateway, clock: Clock, step: Step, timeout: float) -> None:
    clock.set(step.at)
    print(f"night  {step.at:%m-%d %H:%M}  closing out {step.day} (waiting for the heartbeat)")

    def done():
        r = gw.status()["report"]
        if r.get("running") or r.get("queued") or r.get("day") != step.day.isoformat():
            return None
        if r["state"] in ("written", "quiet") or (r["state"] == "failed" and r.get("gave_up")):
            return r
        return None
    r = _wait(f"the report of {step.day}", timeout, done)
    if r:
        tail = f" ({len(r.get('text') or '')} chars)" if r["state"] == "written" else f": {r.get('error')}"
        print(f"  report {r['state']}{tail}")


def _away(gw: Gateway, clock: Clock, step: Step, timeout: float, cap: int) -> None:
    print(f"away   {step.at:%m-%d %H:%M} – {step.until:%H:%M}  {step.note}")
    gw.patch({"wake": {"on": True}})
    try:
        for _ in range(cap + 1):
            w = gw.status()["wake"]
            nxt = w.get("next_at")
            if not nxt:
                print(f"  no wake due: {w.get('next_why_words') or w.get('next_why')}")
                break
            t = max(datetime.fromisoformat(nxt) + timedelta(seconds=30), clock.now or step.at)
            if t >= step.until:
                break
            before = (w.get("last") or {}).get("at")
            clock.set(t)
            print(f"  wake due {t:%H:%M} (waiting for the heartbeat)")

            def woke():
                last = gw.status()["wake"].get("last") or {}
                return last if last.get("at") != before else None
            last = _wait("a wake", timeout, woke)
            if last:
                print(f"  wake: {last.get('result_words') or last.get('result')}")
    finally:
        gw.patch({"wake": {"on": False}})


def _compress(gw: Gateway, clock: Clock, step: Step, timeout: float) -> None:
    clock.set(step.at)
    before = gw.packs()
    print(f"press  {step.at:%m-%d %H:%M}  「现在压」 on the latest conversation  [{step.note}]")
    code, body = gw.compress_now()
    if code != 200:
        print(f"  ! POST /present/compress answered {code}: {body}")
        return

    def done():
        now = gw.packs()
        if now.get("last_ok_at") != before.get("last_ok_at"):
            return "window folded"
        if (now.get("failures_since_ok") or 0) > (before.get("failures_since_ok") or 0):
            return "the pack failed (see the gateway's logs/present.jsonl)"
        return None
    said = _wait("the pack", timeout, done)
    if said:
        print(f"  {said}")


def _report_now(gw: Gateway, step: Step, timeout: float) -> None:
    before = gw.status()["report"]
    print(f"report now  (closing out fixture day {step.day})")
    code, body = gw.report_now()
    if code != 200:
        print(f"  ! POST /present/report answered {code}: {body}")
        return

    def done():
        r = gw.status()["report"]
        if r.get("running") or r.get("queued"):
            return None
        return r if (r.get("at") != before.get("at") or r["state"] == "failed") else None
    r = _wait("the report", timeout, done)
    if r:
        print(f"  report {r['state']}" + (f": {r.get('error')}" if r["state"] == "failed" else ""))


def replay(fx: dict, handle: dict, args, key: str) -> int:
    clock = Clock(handle.get("clock"))
    shift = timedelta(0)
    first = turns_of(fx)[0]
    if clock.files:
        shift = timedelta(days=(clock.now.date() - first.user_at.date()).days)
        if shift.days % 7:
            print(f"note: the clock starts {shift.days:+d} days from the fixture; weekday names in the chat will not match")
        if clock.now > first.user_at + shift:
            print(f"refused: the fake clock ({clock.now.isoformat()}) is already past the first line "
                  f"({(first.user_at + shift).isoformat()}); start dev_panel.py --clock earlier that day", file=sys.stderr)
            return 2
    else:
        print("note: the panel runs on the real clock: every line lands on today, wake is skipped, "
              "and each fixture day is closed with 「现在写日报」")
    turns = turns_of(fx, shift)
    plan = build_plan(fx, bool(clock.files), shift)
    stand_in = StandIn(turns, replies=args.replies, upstream=args.upstream, key=key, model=args.model,
                       clock=clock, listen=args.listen, concurrency=max(1, args.concurrency)).start()
    print(f"stand-in upstream on http://127.0.0.1:{stand_in.port}/v1 → {args.upstream} (model {args.model})")
    if handle.get("upstream", "").rstrip("/") != f"http://127.0.0.1:{stand_in.port}/v1":
        print(f"note: the gateway's upstream is {handle.get('upstream')}, not this stand-in: "
              "start dev_panel.py with --upstream pointing here", file=sys.stderr)
    gw = Gateway(handle)
    squeeze = {"keep_raw": args.keep_raw}
    if args.context_tokens:
        squeeze["context_tokens"] = args.context_tokens
    gw.patch({"compress": squeeze,
              "wake": {"on": False, "every_min": 60, "daily_cap": args.wake_cap},
              "report": {"flip": "daily"}})
    messages = [{"role": "system", "content": system_prompt(fx)}]
    try:
        for step in plan.steps:
            if step.kind == "night":
                _night(gw, clock, step, args.wait)
            elif step.kind == "away":
                _away(gw, clock, step, args.wait, args.wake_cap)
            elif step.kind == "compress":
                _compress(gw, clock, step, args.wait)
            elif step.kind == "report_now":
                _report_now(gw, step, args.wait)
            else:
                t = step.turn
                clock.set(t.user_at)
                messages.append({"role": "user", "content": t.user})
                said = gw.chat(args.model, messages, t)
                messages.append({"role": "assistant", "content": said})
                print(f"turn {t.index + 1:3d}/{len(turns)}  {t.user_at:%m-%d %H:%M}  {t.user[:18]} → {said[:24]}")
    finally:
        stand_in.stop()
    data = handle.get("gateway_data", "")
    print("\ndone. Where the samples are:")
    print(f"  day files and day reports   {os.path.join(data, 'host', 'days')} · {os.path.join(data, 'host', 'reports')}")
    print(f"  window summaries (private)  {os.path.join(data, 'threads')}  (each thread's window.carry)")
    print(f"  dreams                      {os.path.join(handle.get('buckets', ''), 'night_fall', 'dreams')} and the panel's 梦 page")
    print("  grown memories, slices      the panel (grow · recall pages)")
    print(f"  stand-in                    {stand_in.counts}")
    return 0


def print_plan(fx: dict, args) -> None:
    clock = not args.no_clock
    plan = build_plan(fx, clock)
    est = estimate(fx, plan, args.replies, args.wake_cap)
    turns = turns_of(fx)
    print(f"fixture: {args.fixture}")
    print(f"  {est['days']} days · {est['turns']} turns ({est['messages']} messages) · "
          f"{est['chars']} characters ({est['cjk']} CJK; {fx['owner']} {est['chars_owner']}, {fx['ai']} {est['chars_ai']})")
    for d, day in enumerate(fx["days"]):
        mine = [t for t in turns if t.day == d]
        chars = sum(len(t.user) + len(t.reply) for t in mine)
        late = " · late night" if any(t.user_at.date() != date.fromisoformat(day["date"]) for t in mine) else ""
        print(f"  {day['date']} {date.fromisoformat(day['date']):%a}  {len(mine):2d} turns  {chars:4d} chars  "
              f"{mine[0].user_at:%H:%M}–{mine[-1].reply_at:%H:%M}{late}")
    print(f"\nplan ({'fake clock' if clock else 'real clock'}, replies {args.replies}):")
    for s in plan.steps:
        if s.kind == "night":
            print(f"  night   {s.at:%m-%d %H:%M}  hand-off + day report for {s.day}, window flips")
        elif s.kind == "away":
            print(f"  away    {s.at:%m-%d %H:%M}–{s.until:%H:%M}  wake on, clock to each wake (≤ {args.wake_cap})  [{s.note}]")
        elif s.kind == "compress":
            print(f"  press   {s.at:%m-%d %H:%M}  「现在压」: a pack folds the window  [{s.note}]")
        elif s.kind == "report_now":
            print(f"  report  「现在写日报」 for {s.day}")
    print(f"  turns   {len(turns)} chat turns between them")
    print(f"\nmodel calls for a full replay (ranges; own turns take 1 + up to {DEFAULT_TOOL_ROUNDS} tool rounds):")
    for group, rows in (("main model (through the gateway)", est["main"]), ("side model (Loci)", est["side"])):
        print(f"  {group}")
        for name, (lo, hi) in rows.items():
            print(f"    {name:44s} {lo:3d} – {hi:3d}")
    print(f"  total                                        {est['total'][0]:3d} – {est['total'][1]:3d}")
    first = turns[0].user_at
    start = first.replace(hour=8, minute=0, second=0) if first.hour >= 8 else first - timedelta(minutes=30)
    print("\nto run it:")
    print(f"  python scripts/dev_panel.py --gateway --keep --upstream http://127.0.0.1:{args.listen}/v1"
          + (f" --clock {start.isoformat()}" if clock else ""))
    print("  REPLAY_UPSTREAM_URL=<provider …/v1> REPLAY_UPSTREAM_MODEL=<model> REPLAY_UPSTREAM_KEY=<key> \\")
    print(f"    python scripts/replay_week.py --panel-dir <folder dev_panel printed>{'' if args.replies == 'scripted' else ' --replies model'}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fixture", default=FIXTURE)
    ap.add_argument("--dry-run", action="store_true", help="check the fixture and print the plan; nothing is sent")
    ap.add_argument("--no-clock", action="store_true", help="(dry run) plan for a panel on the real clock")
    ap.add_argument("--panel-dir", help="the throwaway folder dev_panel.py --gateway printed (holds replay.json)")
    ap.add_argument("--replies", choices=("scripted", "model"), default="scripted",
                    help="小满's lines: the fixture's (default; chat turns cost nothing) or the model's")
    ap.add_argument("--upstream", default=os.environ.get("REPLAY_UPSTREAM_URL", ""),
                    help="the real OpenAI-compatible upstream (…/v1); env REPLAY_UPSTREAM_URL")
    ap.add_argument("--model", default=os.environ.get("REPLAY_UPSTREAM_MODEL", ""),
                    help="the real model; env REPLAY_UPSTREAM_MODEL")
    ap.add_argument("--key-env", default="REPLAY_UPSTREAM_KEY",
                    help="the environment variable holding the upstream key (the key itself is never an argument)")
    ap.add_argument("--listen", type=int, default=DEFAULT_LISTEN, help="the stand-in's port on 127.0.0.1")
    ap.add_argument("--context-tokens", type=int, default=0,
                    help="a window size to set on the gateway (default: leave it to the gateway); "
                         "small enough and a long day forces packs of its own, each a paid own turn")
    ap.add_argument("--keep-raw", type=int, default=10,
                    help="raw lines a flip or a pack keeps (small, so a pack folds most of a day)")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="requests in flight to the real upstream at once (1 for a tier that allows one)")
    ap.add_argument("--wake-cap", type=int, default=3, help="wakes per day at most (each is a paid own turn)")
    ap.add_argument("--wait", type=float, default=900, help="seconds to wait for one night's report or one wake")
    ap.add_argument("--days", type=int, default=0,
                    help="replay only the first N days (and the night after them); try 1 first")
    args = ap.parse_args()

    fx = load_fixture(args.fixture)
    bad = problems(fx)
    fx = first_days(fx, args.days)
    if bad:
        print("the fixture does not replay:", file=sys.stderr)
        for line in bad:
            print(f"  · {line}", file=sys.stderr)
        return 1
    if args.dry_run:
        print_plan(fx, args)
        return 0
    if not args.panel_dir:
        print("refused: --panel-dir is needed (or --dry-run)", file=sys.stderr)
        return 2
    try:
        handle = json.load(open(os.path.join(args.panel_dir, "replay.json"), encoding="utf-8"))
    except OSError as e:
        print(f"refused: no replay.json in {args.panel_dir} ({e}); start dev_panel.py --gateway", file=sys.stderr)
        return 2
    key = os.environ.get(args.key_env, "").strip()
    if not (key and args.upstream and args.model):
        print(f"refused: the replay needs the upstream URL, model and key ({args.key_env}); "
              "without them every own turn is refused at the stand-in. Use --dry-run to look first.",
              file=sys.stderr)
        return 2
    return replay(fx, handle, args, key)


if __name__ == "__main__":
    raise SystemExit(main())
