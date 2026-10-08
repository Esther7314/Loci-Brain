# -*- coding: utf-8 -*-
"""
dev_panel.py — the panel on a throwaway library, for looking at it

    python scripts/dev_panel.py              # http://127.0.0.1:18761/loci
    python scripts/dev_panel.py --port 0     # any free port
    python scripts/dev_panel.py --locked     # with a panel password, to see the login page
    python scripts/dev_panel.py --keep       # leave the library folder behind afterwards
    python scripts/dev_panel.py --gateway    # also run the gateway (gateway/server.js), so
                                             # the present page is connected (implies --locked)

Starts the real server (src/server.py, streamable-http) on 127.0.0.1 over a library made
fresh in the system temp folder and seeded with a small believable sample through the
store's own APIs: a name page and two pinned rules, events today (a promise with a day,
another with a hold on it, one waiting on a cue, a yearly day, something heard about a
friend), an
entry formed from an imported conversation (its 看原话 answers with who and when) and one
from a run of three imported lines (its 来源 row says from when to when), a
judgement derived from two events carrying a 人说不对 mark, two names in the alias
table, a slices batch from a host. `seed_grow_and_recall` adds what grow's 等着写的 and
recall's timeline show: an older host batch with handled slices, an imported
conversation's drafts waiting to be checked, and three days of searches and cards. It then
asks for one breath the way a host's window-opening hook does, so breath's page has a last
breath to show.

🔴 It never touches a real library. Every LOCI_* variable of the calling shell is dropped
   (a LOCI_BUCKETS_DIR, LOCI_VAULT_DIR, LOCI_ALIAS_TABLE or LOCI_DASHBOARD_PASSWORD left
   in it would point the server at real data or a real password) and set again to the
   temp folder. `--dir` must lie inside the system temp folder and be empty, new, or a
   folder this script made before (it carries a marker file); anything else is refused.
   The seeding runs in a child process started with that environment, so no module that
   reads its paths at import time (core/names reads the alias table's) ever sees another.

Without a side model or an embedding service, what degrades: memories are written with no
model-filled tags or summaries, recall finds by words only (no 「意思相近」), nothing has a
vector (embedding is off in the throwaway config), so the similarity page has no pairs and
muse finds only what needs no vector (thoughts grown from one evening, a word burst);
dreams and slicing a host's lines do not run. The panel's reads all answer.

With --gateway the script also starts the gateway on another free port of 127.0.0.1, its
data folder inside the throwaway folder, wired to this server the way the config's
`hosts: gateway` example wires a real one (fresh keys each run). Its upstream is
https://api.deepseek.com/v1 with no key, so nothing it does costs anything: no chat goes
through it, wake and the night's report wait for a key that never comes. A hosts table
means the panel must be locked, so --gateway implies --locked. The gateway's own address,
its key for /present and the clock files (below) are written to `replay.json` in the
throwaway folder, for scripts/replay_week.py.

Two more switches, for replaying a chat through the gateway (scripts/replay_week.py):

    --upstream URL   the gateway's upstream, and Loci's side model too (slicing, tags,
                     dreams), with the placeholder key `replay-placeholder-key` and model `stand-in`.
                     The sample's entries arrive with stand-in names and summaries quoted
                     from their bodies (stamped `fallback`, fill_blanks), so Loci's startup
                     sweep does not send the side model one backfill per sample entry.
                     Meant for replay_week.py's stand-in, which swaps in the real key and
                     model; this script never holds a real key. Pointed straight at a
                     provider, the placeholder key is refused and nothing costs anything.
                     Loci's config also names the sample's people (阿青, 小满).
    --clock ISO      a fake clock for the whole run, starting at ISO (with an offset): the
                     sample is seeded at that time, the server runs under exam/clock.py and
                     the gateway under LOCI_GATEWAY_TEST_CLOCK. Two files in the throwaway
                     folder hold the time, `clock.iso` (Loci) and `clock.ms` (the gateway);
                     whoever moves the clock rewrites both. What exam/clock.py does not
                     patch reads the real clock: the usage log behind recall's timeline,
                     the seeded searches included.

Both processes count the day in Asia/Shanghai (LOCI_TZ), where the sample's people live.

Public surface: run as a script. `seed(store, base_dir)` fills a store,
`seed_regrow_muse_trace(store, base_dir)` adds the regrow/fold, muse and trace pages'
sample and, under --upstream, `fill_blanks()` stamps the sample's blanks (the child's work).
"""

import argparse
import asyncio
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
MARKER = ".loci-panel-dev"
DEFAULT_PORT = 18761
DEFAULT_UPSTREAM = "https://api.deepseek.com/v1"
ZONE = "Asia/Shanghai"
# What Loci's side model is configured with under --upstream: placeholders the replay's
# stand-in replaces, never a real key.
STAND_IN_KEY = "replay-placeholder-key"
STAND_IN_MODEL = "stand-in"
HANDLE = "replay.json"
# Set for the seeding child under --upstream: the sample arrives with its blanks filled
# (fill_blanks).
FILL_BLANKS_ENV = "DEV_PANEL_FILL_BLANKS"


# ---------------------------------------------------------------------------
# Where the throwaway library lives
# ---------------------------------------------------------------------------

def _temp_root() -> str:
    return os.path.realpath(tempfile.gettempdir())


def _inside_temp(path: str) -> bool:
    real, root = os.path.realpath(path), _temp_root()
    try:
        return os.path.commonpath([real, root]) == root and real != root
    except ValueError:          # another drive
        return False


def _usable_dir(path: str) -> str:
    """The folder to use, or the reason it may not be used."""
    if not _inside_temp(path):
        return f"refused: {path} is not inside the system temp folder ({_temp_root()})"
    if os.path.exists(path):
        if not os.path.isdir(path):
            return f"refused: {path} is a file"
        if os.listdir(path) and not os.path.isfile(os.path.join(path, MARKER)):
            return f"refused: {path} is not empty and was not made by this script"
    return ""


def _layout(base: str) -> dict:
    return {"base": base, "buckets": os.path.join(base, "buckets"),
            "config": os.path.join(base, "config.yaml"), "logs": os.path.join(base, "logs")}


def _child_env(paths: dict, port: int, hook_token: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("LOCI_") and k.upper() != "PASSWORD"}
    env.update({
        "LOCI_BUCKETS_DIR": paths["buckets"],
        "LOCI_CONFIG_PATH": paths["config"],
        "LOCI_ALIAS_TABLE": os.path.join(paths["buckets"], "aliases.yaml"),
        "LOCI_LOG_DIR": paths["logs"],
        "LOCI_PORT": str(port),
        "LOCI_BIND_HOST": "127.0.0.1",
        "LOCI_TRANSPORT": "streamable-http",
        "LOCI_HOOK_TOKEN": hook_token,
        "LOCI_TZ": ZONE,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    return env


def _write_config(paths: dict, gateway_url: str = "", upstream: str = "") -> None:
    # JSON is YAML: written without a YAML library, read back by the server's.
    cfg = {
        "transport": "streamable-http",
        "buckets_dir": paths["buckets"],
        "log_level": "INFO",
        "embedding": {"enabled": False},
        "dehydration": {"api_key": ""},
    }
    if upstream:
        cfg["dehydration"] = {"base_url": upstream, "api_key": STAND_IN_KEY,
                              "model": STAND_IN_MODEL}
        cfg.update({"human": "阿青", "owner_name": "阿青", "ai_name": "小满"})
    if gateway_url:
        # legacy stays listed: the breath handed out below comes with the hook token
        gw = {"system": "gateway", "instance": "gateway"}
        cfg["hosts"] = {
            "legacy": {"token_env": "LOCI_HOOK_TOKEN", "scope_mode": "open"},
            "gateway": {
                "token_env": "LOCI_HOST_TOKEN_GATEWAY", "scope_mode": "open",
                "authority": [gw], "provides": [gw],
                "fetch_url": f"{gateway_url}/loci/source",
                "fetch_token_env": "LOCI_FETCH_TOKEN_GATEWAY",
                "present_url": gateway_url,
            },
        }
    with open(paths["config"], "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)


def _free_port(wanted: int) -> int:
    for port in ([wanted] if wanted else []) + [0]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return s.getsockname()[1]
    raise SystemExit("no free port on 127.0.0.1")


# ---------------------------------------------------------------------------
# The sample library (runs in the child, with the temp environment)
# ---------------------------------------------------------------------------

def store_import(store, base_dir: str, meta: dict, lines: dict) -> None:
    """An imported conversation stored the way an upload stores one (core/import_memory.
    ImportEngine.take): its lines in the import's store and each conversation's order
    registered with the source registry, so a run over its lines is readable."""
    from core.import_memory import ImportStore
    from core.scope import IMPORT_SYSTEM
    ImportStore(base_dir).create(meta, lines)
    for container, rows in lines.items():
        store.sources.record_order(
            {"system": IMPORT_SYSTEM, "instance": meta["batch"], "container": container},
            [r["id"] for r in rows], batch_id=meta["batch"])


async def host_batch(store, *, batch_id: str, source: dict, lines: list, **kw):
    """A host's slices batch as a handed-over batch is kept (core/_slicer.take_batch): the
    batch and its slices in the pending store, the lines' order in the source registry."""
    out = await store.slices.record_batch(batch_id=batch_id, source=source, lines=lines, **kw)
    store.sources.record_order(source, [i for i, _fp in lines], batch_id=batch_id)
    return out


async def seed(store, base_dir: str) -> dict:
    """Fill `store` with the sample. Returns the ids worth knowing."""
    from datetime import timedelta
    from core import _when as W
    from core import _invalidation as I
    from core import names as N
    from core.profile import _PROFILE_TAG

    today = W.now()
    day = lambda n: (today + timedelta(days=n)).strftime("%Y-%m-%d")  # noqa: E731

    ids = {}
    ids["page"] = await store.create(
        "我叫小满。对方叫阿青，我叫阿青「青青」，阿青叫我「满满」。\n"
        "阿青的朋友小林在找工作，别主动问面试结果。",
        room="EVENT/SELF", tags=[_PROFILE_TAG], name="名字页", pinned=True)
    ids["rule1"] = await store.create("阿青累的时候先别讲道理，先陪着。", room="MIND/VIEWS",
                                      pinned=True, name="累的时候先陪着")
    ids["rule2"] = await store.create("说不准的事就说不知道，不编一个听起来对的答案。",
                                      room="MIND/VIEWS", pinned=True, name="不知道就说不知道")

    ids["teeth"] = await store.create(
        "阿青说牙又疼了，我说周六陪阿青去看牙。", room="EVENT/SELF", name="周六陪阿青去看牙",
        summary="答应周六陪阿青去看牙", when=day(3), direction_of_fit="telic", bound=["我"],
        subjects=["阿青"], tags=["看牙"], valence=0.45, arousal=0.4)
    ids["bike"] = await store.create(
        "答应帮阿青把自行车的链条修好。", room="EVENT/SELF", name="帮阿青修自行车",
        direction_of_fit="telic", bound=["我"], subjects=["阿青"])
    ids["hold"] = await store.create(
        "修车这事先别催，阿青说等零件到了再说。", room="EVENT/SELF", name="修车先别催",
        direction_of_fit="telic", bound=["我"], exception_of=ids["bike"], hold="defer",
        when=day(4), subjects=["阿青"])
    ids["exam"] = await store.create(
        "阿青说考完了，想去吃那家甜品。", room="EVENT/SELF", name="阿青考完了",
        subjects=["阿青"], tags=["考试", "甜品"], valence=0.8, arousal=0.6)
    ids["sea"] = await store.create(
        "等阿青考完，带阿青去海边。", room="EVENT/SELF", name="考完去海边",
        cue={"condition": "阿青说考完了", "phrasings": ["考完了", "终于考完"]},
        direction_of_fit="telic", bound=["我"], subjects=["阿青"])
    ids["birthday"] = await store.create(
        "阿青的生日。", room="EVENT/SELF", name="阿青的生日", when="2026-11-03",
        recurrence="FREQ=YEARLY", subjects=["阿青"])
    ids["lin"] = await store.create(
        "小林第二轮面试过了，下周还有一轮。", room="EVENT/WORLD", name="小林面完第二轮",
        subjects=["小林"], tags=["找工作"])

    batch = "imp_" + secrets.token_hex(6)
    said = ["周六去海边吧，好久没看海了", "好，周六去。", "要不要带相机？", "带上，上次的照片都糊了。",
            "那早点出发，七点？", "七点起不来，八点吧。", "行，八点，我来叫你。", "说好了啊。",
            "最近睡得不好，半夜老醒。", "几点醒的？", "三四点，醒了就睡不着。", "睡前别看手机了。",
            "试试吧。", "今晚早点睡。"]
    start = today - timedelta(days=2)
    store_import(store, base_dir,
        {"batch": batch, "same_self": True, "human": "阿青", "title": "chat-export.json",
         "conversations": [{"container": "c0001"}]},
        {"c0001": [{"id": f"l{i + 1:04d}", "role": "user" if i % 2 == 0 else "assistant",
                    "at": (start + timedelta(minutes=i)).isoformat(), "text": t}
                   for i, t in enumerate(said)]})
    ids["beach"] = await store.create(
        "阿青说周六想去海边，好久没看海了。", room="EVENT/SELF", name="阿青想去看海",
        subjects=["阿青"], tags=["海边"],
        sources=[{"system": "import", "instance": batch, "container": "c0001", "id": "l0001"},
                 {"system": "import", "instance": batch, "container": "c0001", "id": "l0002"}])

    ids["mind"] = await store.create(
        "阿青紧张的时候想有人陪着。", room="MIND/VIEWS", name="紧张的时候想有人陪",
        evidential="inference", subjects=["阿青"],
        prov=[{"rel": "wasDerivedFrom", "target": ids["exam"]},
              {"rel": "wasDerivedFrom", "target": ids["beach"]}])
    stamp = today.isoformat(timespec="seconds")
    await store.add_invalidation_record(
        ids["mind"], I.dispute_record(ids["mind"], "不是紧张，是累了", stamp))

    N.set_kind("阿青", "人")
    N.add_alias("阿青", "青青")
    N.set_kind("小林", "人")

    src = {"system": "lento", "instance": "home", "container": "p"}
    lines = [(f"m_{i:04d}", f"sha256:{i:064x}") for i in range(1, 7)]
    await host_batch(
        store, batch_id="b_" + secrets.token_hex(6), source=src, day=day(-1), revision=None,
        lines=lines, host="lento", report_at=(today - timedelta(hours=6)).isoformat(timespec="seconds"),
        slices=[
            {"first": "m_0001", "last": "m_0003", "gist": "阿青说牙又疼了，约了周六",
             "guesses": [{"id": ids["teeth"], "score": 0.81}]},
            {"first": "m_0004", "last": "m_0006", "gist": "聊到考完想去吃甜品", "guesses": []},
        ])
    ids.update(await seed_said_span(store, base_dir))
    await seed_grow_and_recall(store, ids, batch)
    await seed_dreams_and_names(store, base_dir, ids)
    return ids


async def seed_said_span(store, base_dir: str) -> dict:
    """An entry formed from a run of an imported conversation (its first line through its
    last), so the 来源 layer has a 「日期 几点 – 几点」 to show."""
    from datetime import timedelta
    from core import _when as W

    start = W.now() - timedelta(days=1, hours=3)
    batch = "imp_" + secrets.token_hex(6)
    store_import(store, base_dir,
        {"batch": batch, "same_self": True, "human": "阿青", "title": "chat-export.json",
         "conversations": [{"container": "c0002"}]},
        {"c0002": [
            {"id": "l0001", "role": "user", "at": start.isoformat(),
             "text": "下周想去爬山"},
            {"id": "l0002", "role": "assistant", "at": (start + timedelta(minutes=7)).isoformat(),
             "text": "好，挑个不下雨的日子。"},
            {"id": "l0003", "role": "user", "at": (start + timedelta(minutes=18)).isoformat(),
             "text": "那就周三吧"},
        ]})
    hike = await store.create(
        "阿青说下周三想去爬山，挑个不下雨的日子。", room="EVENT/SELF", name="阿青想去爬山",
        subjects=["阿青"], tags=["爬山"],
        sources=[{"system": "import", "instance": batch, "container": "c0002", "id": "l0001",
                  "through": "l0003"}])
    return {"hike": hike}


async def seed_grow_and_recall(store, ids: dict, import_batch: str) -> None:
    """What grow's 等着写的 and recall's timeline show, through the store's own APIs: a
    host batch from the day before yesterday whose slices were handled (one written up,
    one skipped, one still waiting with a guess), an imported conversation's drafts
    waiting to be checked, and searches and cards over the last three days. Searches and
    cards are stamped with the clock, so the older ones are written with the clock set
    back."""
    from contextlib import contextmanager
    from datetime import datetime, timedelta
    from core import _usage as U
    from core import _when as W

    today = W.now()

    @contextmanager
    def clock_back(days: int, hours: int = 0):
        shift = timedelta(days=days, hours=hours)
        real_now, real_dt = W.now, U.datetime

        class _Shifted(datetime):
            @classmethod
            def now(cls, tz=None):
                return real_dt.now(tz) - shift
        W.now = lambda: real_now() - shift
        U.datetime = _Shifted
        try:
            yield
        finally:
            W.now, U.datetime = real_now, real_dt

    # A host batch of 90 lines from the day before yesterday, its slices handled.
    src = {"system": "lento", "instance": "home", "container": "p"}
    lines = [(f"n_{i:04d}", f"sha256:{i + 1000:064x}") for i in range(1, 91)]
    older, _r = await host_batch(
        store, batch_id="b_" + secrets.token_hex(6), source=src,
        day=(today - timedelta(days=2)).strftime("%Y-%m-%d"), revision=None, lines=lines,
        host="lento", slices=[
            {"first": "n_0012", "last": "n_0040", "gist": "阿青说起小林的面试，有点替他紧张",
             "guesses": []},
            {"first": "n_0041", "last": "n_0058", "gist": "又说到看牙的事，想改到周日",
             "guesses": [{"id": ids["teeth"], "score": 0.77}]},
            {"first": "n_0059", "last": "n_0080", "gist": "阿青考完了，说想去吃那家甜品",
             "guesses": []},
            {"first": "n_0081", "last": "n_0090", "gist": "互道晚安", "guesses": []},
        ])
    _waiting, _guessed, written, skipped = (x["slice_id"] for x in older["slices"])
    await store.slices.close(written, "grow", [ids["exam"]])
    await store.slices.close(skipped, "drop", [])

    # The imported conversation's drafts, waiting to be checked.
    from core import _slicer as SL
    from core.import_memory import ImportStore
    conv = ImportStore(store.base_dir).lines(import_batch, "c0001")
    imp_lines = [(r["id"], SL.fingerprint_of(r.get("text") or "")) for r in conv]
    await store.slices.record_batch(
        batch_id="b_" + secrets.token_hex(6),
        source={"system": "import", "instance": import_batch, "container": "c0001"},
        day=(today - timedelta(days=2)).strftime("%Y-%m-%d"), revision=None,
        lines=imp_lines, origin={"batch": import_batch, "same_self": True,
                                 "title": "chat-export.json"},
        slices=[
            {"first": "l0001", "last": "l0008", "gist": "约周六去海边",
             "draft": "阿青说周六想去海边，好久没看海了。", "guesses": []},
            {"first": "l0009", "last": "l0014", "gist": "说到最近睡得不好",
             "draft": "阿青这阵子睡得不好，半夜总醒。", "guesses": []},
        ])

    # recall's timeline: what the model searched for, and the cards the owner's words
    # brought, over the last three natural days.
    def found(query: str, found_ids: list) -> None:
        store.usage.record(U.FOUND, found_ids, "recall.search", query=query,
                           gates={"when": "", "room": "", "tag": ""})

    def cards(window: str, turn: str, bid: str, why: str, delivered: bool) -> None:
        card = f"{bid}@seed{turn[-2:]}"
        store.cues.open_window("lento", window, [])
        store.cues.offer("lento", window, turn,
                         [{"card": card, "id": bid, "kind": "memory", "why": why}])
        store.cues.deliver("lento", window, cards=[card])
        if not delivered:
            store.cues.drop("lento", window, cards=[card])

    with clock_back(2, 3):
        found("海边", [ids["beach"], ids["sea"]])
    with clock_back(1, 2):
        found("面试", [])
        cards("w-seed-1", "t-01", ids["teeth"], "牙疼", delivered=False)
    with clock_back(0, 1):
        found("甜品", [ids["exam"]])
        found("生日", [ids["birthday"]])
        found("看牙", [ids["teeth"], ids["hold"]])
    cards("w-seed-2", "t-02", ids["sea"], "考完了", delivered=True)
async def seed_regrow_muse_trace(store, base_dir: str) -> dict:
    """More sample for the regrow/fold, muse and trace pages, after `seed`:

      regrow/fold  a new version, three walks folded into one, a period ringed, a rule
                   pinned, and three entries whose basis moved: rewritten, put away, kept
      muse         two groups of three thoughts that grew from one evening (written two
                   weeks ago, past muse's cooldown), the second poked and already told to
                   the model; five days of camping tagged 露营, a word burst with no name
      trace        two more things hanging now (a page and a bit), and three written three
                   weeks ago that sleep: a cue, a promise far off, another cue

    Entries "written N days ago" are written with the store's clock set back for that one
    write. Returns the ids worth knowing."""
    from datetime import timedelta
    from core import _fold as F
    from core import _invalidation as I
    from core import _muse as M
    from core import _nudge
    from core import _when as W
    from core import muse_view as MV
    from tools.regrow import dispatch as regrow
    from tools.trace import dispatch as trace
    from utils import utc_now

    async def written_ago(days: int, *args, **kw) -> str:
        store._now_iso = lambda: (utc_now() - timedelta(days=days)).isoformat(timespec="seconds")
        try:
            return await store.create(*args, **kw)
        finally:
            del store._now_iso

    today = W.now()
    day = lambda n: (today + timedelta(days=n)).strftime("%Y-%m-%d")  # noqa: E731
    ids = {}

    # regrow/fold
    walk = [await store.create(text, room="EVENT/SELF", name=name, subjects=["阿青"])
            for text, name in (("周一晚上跟阿青沿河走了一圈。", "周一沿河走"),
                               ("周三又去河边走了走，阿青说风很舒服。", "周三河边"),
                               ("周五走到了桥那头才回来。", "周五走到桥那头"))]
    ids["walks_gist"], _ = await F.save_gist("这周跟阿青三个晚上都去河边散步。", "EVENT/SELF",
                                             0.7, 0.3, walk)
    ids["period"] = await store.create("阿青的考试周。", room="EVENT/SELF", name="阿青的考试周",
                                       tags=["__大event__"], when=f"{day(-12)}..{day(-6)}")
    ids["pin"] = await store.create("阿青说「随便」的时候，给两个选项让阿青挑。",
                                    room="MIND/VIEWS", name="给两个选项")
    await store.update(ids["pin"], pinned=True)
    ids["hotpot"] = await store.create("阿青不太能吃辣。", room="EVENT/WORLD", name="阿青不太能吃辣")
    await regrow(bucket_id=ids["hotpot"], mode="supplement", v=0.5, a=0.3,
                 text="阿青不太能吃辣，微辣可以。")
    stamp = today.isoformat(timespec="seconds")
    moved = {}
    for key, text in (("rewrite", "阿青周末喜欢睡懒觉。"), ("away", "阿青不喜欢下雨天。"),
                      ("kept", "阿青喜欢靠窗的位置。")):
        moved[key] = await store.create(text, room="MIND/VIEWS", name=text.rstrip("。"))
        await store.add_invalidation_record(moved[key], I.dispute_record(moved[key], "不对", stamp))
    await regrow(bucket_id=moved["rewrite"], mode="supplement", v=0.5, a=0.3,
                 text="阿青周末喜欢睡懒觉，考试周除外。")
    await trace(bucket_id=moved["away"], delete=True)
    await trace(bucket_id=moved["kept"], invalidation="confirmed")
    ids.update({f"moved_{k}": v for k, v in moved.items()})

    # muse
    groups = []
    for night, v, a, thoughts in (
            ("那晚阿青说起小时候搬过三次家。", 0.3, 0.6,
             ("阿青对换地方住有点怕。", "阿青很看重有一个固定的角落。", "阿青不太愿意扔旧东西。")),
            ("那晚阿青说想学游泳。", 0.8, 0.7,
             ("阿青想试新东西的时候会先找个伴。", "阿青说开始前最难。", "阿青喜欢有人在旁边看着。"))):
        root = await written_ago(16, night, room="EVENT/SELF", subjects=["阿青"])
        groups.append([await written_ago(15, t, room="MIND/TRAITS", name=t.rstrip("。"),
                                         valence=v, arousal=a, subjects=["阿青"],
                                         prov=[{"rel": "wasDerivedFrom", "target": root}])
                       for t in thoughts])
    for n in range(5):
        await written_ago(16 - n, f"露营第 {n + 1} 天，阿青搭帐篷越来越快。", room="EVENT/SELF",
                          tags=["露营"], when=day(-16 + n), subjects=["阿青"])
    clusters, *_ = await M.both_sides(force=True)
    poked = next((c for c in clusters if set(c.ids) == set(groups[1])), None)
    if poked is not None:
        cid = MV.cluster_id(poked.ids)
        _nudge.poke(base_dir, cid, list(poked.ids), W.now())

        async def open_view():
            return None
        await _nudge.take_lines(base_dir, store, open_view, W.now())
        ids["muse_seen"] = cid

    # trace
    ids["umbrella"] = await store.create("答应下次出门帮阿青带伞。", room="EVENT/SELF",
                                         name="出门帮阿青带伞", direction_of_fit="telic", bound=["我"],
                                         subjects=["阿青"])
    ids["photo"] = await store.create("阿青说照片洗出来了就告诉阿青。", room="EVENT/SELF",
                                      name="照片洗出来告诉阿青",
                                      cue={"condition": "阿青说照片洗好了", "phrasings": ["照片洗好了"]})
    ids["deep_cue"] = await written_ago(21, "等阿青的新书到了，一起去书店。", room="EVENT/SELF",
                                        name="新书到了去书店",
                                        cue={"condition": "阿青说书到了", "phrasings": ["书到了"]})
    ids["deep_promise"] = await written_ago(21, "答应明年春天带阿青去看樱花。", room="EVENT/SELF",
                                            name="明年春天去看樱花", direction_of_fit="telic",
                                            bound=["我"], when=day(170), subjects=["阿青"])
    ids["deep_cue2"] = await written_ago(22, "阿青的猫要是学会开门了，记下来。", room="EVENT/SELF",
                                         name="猫学会开门",
                                         cue={"condition": "阿青说猫会开门了", "phrasings": ["会开门了"]})
    return ids


async def seed_dreams_and_names(store, base_dir: str, ids: dict) -> None:
    """The dream and name pages' sample: three nights' dreams in the panel's copy (one
    waiting, one fading, one gone; two of the last night's thread candidates, one of them
    kept by the 考完去海边 cue written after it), and names enough to page — known people,
    a game and a character in it, a card filed for 小林, and six names waiting to be
    recognised (two of them looking like a person)."""
    from datetime import timedelta
    from core import _dream_archive as DA
    from core import _when as W
    from core import names as N

    now = W.now()
    today = min(now.replace(hour=3, minute=10, second=0, microsecond=0), now - timedelta(minutes=5))
    nights = [
        ("d_seed_0001", today, DA.WAITING,
         "梦里是一间没有屋顶的厨房。锅里炖着的东西一直冒白气，白气往上飘，飘成了海边的云。\n"
         "阿青站在灶台边改一张纸，纸上的字自己排起队，排着排着变成一串脚印，往海那边走。\n"
         "远处有个钟在敲。敲到第十二下，阿青回过头说：「该睡了。」",
         [{"碰到": "考完了", "想起": "答应过考完带阿青去海边"},
          {"碰到": "钟声", "想起": "那串往海边走的脚印"}]),
        ("d_seed_0002", today - timedelta(days=1), DA.FADING,
         "一辆没有链条的自行车。", [{"碰到": "零件到了", "想起": "修车先别催"}]),
        ("d_seed_0003", today - timedelta(days=2), DA.GONE, "", []),
    ]
    for did, woven, state, text, candidates in nights:
        stamp = woven.isoformat(timespec="seconds")
        dream = {"id": did, "织于": stamp, "完整": text, "线索候选": candidates,
                 "素材": {"压在心头": [ids["sea"]]}, "来源": []}
        DA.keep(dream, state=DA.WAITING, at=woven, base_dir=base_dir)
        if state != DA.WAITING:
            DA.keep(dream, state=state, at=woven + timedelta(hours=5), base_dir=base_dir)

    people = ["老周", "小雨", "妈妈", "康纳"]
    for name in people:
        N.set_kind(name, N.KIND_PERSON)
    N.set_kind("底特律：变人", "游戏")
    N.link_name("康纳", "present_in", "底特律：变人")
    N.add_alias("康纳", "RK800")
    N.add_alias("小林", "林林")
    N.link_name("小林", "member_of", "读书会")
    # Two waiting names that look like something: 小鹿 hangs in a group with no kind of its
    # own, and the side model said 汉克 is a person when the table could not take it.
    N.link_name("小鹿", "member_of", "读书会")
    from core import name_guesses as G
    G.record(base_dir, "汉克", N.KIND_PERSON, now)
    await store.create("老周说周末去钓鱼，问要不要一起。", room="EVENT/WORLD", name="老周约钓鱼",
                       subjects=["老周", "小雨"])
    await store.create("小雨寄来一盒桂花糕。", room="EVENT/WORLD", name="小雨寄桂花糕",
                       subjects=["小雨", "妈妈"])
    await store.create("阿青在玩《底特律：变人》，最喜欢康纳。", room="EVENT/SELF",
                       name="阿青在玩底特律", subjects=["阿青", "底特律：变人", "康纳"])
    await store.create("小林在找工作，别主动问面试结果，等小林自己说。", room="MIND/VIEWS",
                       name="小林的名字卡", card_of="小林", subjects=["小林"])
    for who, what in [("阿哲", "阿哲今天又加班"), ("团子", "团子把花盆打翻了"),
                      ("汉克", "汉克在剧情里一直喝酒"), ("小鹿", "小鹿说下个月结婚"),
                      ("阿宁", "阿宁换了新工作"), ("一号线", "一号线早高峰停了二十分钟")]:
        await store.create(f"阿青说{what}。", room="EVENT/WORLD", name=what,
                           subjects=["阿青", who])


async def fill_blanks() -> int:
    """Under --upstream: give every sample entry still waiting for its backfill the
    stand-ins a backfill writes when the side model has nothing usable to say — a name and
    a summary quoted from the body, stamped `fallback` (tools/grow/_backfill.py). It runs
    the startup sweep itself with a side model that answers nothing, so the server's own
    sweep, started by the first grow, finds nothing left: a replay pays for the week's
    memories, not for tagging the sample (one call per entry, some fifty)."""
    from core import runtime as rt
    from tools.grow import rooms_path

    class Silent:
        api_available = True

        async def _chat(self, *args, **kwargs) -> str:
            return ""

    before = rt.dehydrator, rt.logger
    rt.dehydrator, rt.logger = Silent(), logging.getLogger("dev_panel.seed")
    try:
        return await rooms_path.backfill_sweep()
    finally:
        rt.dehydrator, rt.logger = before


def _seed_child(base: str) -> int:
    """`--seed-into`: fill the library at `base` (this process's environment already points
    every path there)."""
    paths = _layout(base)
    if not os.path.isfile(os.path.join(base, MARKER)) or not _inside_temp(base):
        print(f"refused: {base} is not a folder this script made in the temp folder", file=sys.stderr)
        return 2
    if os.path.realpath(os.environ.get("LOCI_BUCKETS_DIR", "")) != os.path.realpath(paths["buckets"]):
        print("refused: LOCI_BUCKETS_DIR does not point at the throwaway library", file=sys.stderr)
        return 2
    sys.path.insert(0, SRC)
    clock_file = os.environ.get("EXAM_CLOCK_FILE", "")
    if clock_file:          # --clock: the sample is seeded at the fake time
        sys.path.insert(0, ROOT)
        from exam import clock
        clock.install(clock_file)
    from core import runtime as rt
    from core import schema
    from core.bucket_manager import BucketManager
    from tools.grow import rooms_path

    # Stamped before the first memory, as server start stamps a new library: memories on
    # disk with no version file read as a library from before versions existed (version
    # 1), and its export package would be refused by this version's importer.
    schema.stamp_new_library(paths["buckets"])
    cfg = {"buckets_dir": paths["buckets"]}
    store = BucketManager(cfg)
    rt.bucket_mgr, rt.config = store, cfg

    async def no_backfill(pairs):     # no side model while seeding
        return None
    real_backfill = rooms_path._backfill_batch
    rooms_path._backfill_batch = no_backfill

    if os.environ.get("DEV_PANEL_PASSWORD"):
        from web import _shared as sh
        sh.init(cfg)
        sh._save_password_hash(os.environ["DEV_PANEL_PASSWORD"])
        sh._save_security_qa("第一只猫叫什么", "团子")

    async def both():
        ids = await seed(store, paths["buckets"])
        ids.update(await seed_regrow_muse_trace(store, paths["buckets"]))
        if os.environ.get(FILL_BLANKS_ENV):
            rooms_path._backfill_batch = real_backfill
            await fill_blanks()
        return ids
    ids = asyncio.run(both())
    print(json.dumps(ids, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def _get(url: str, headers: dict | None = None, timeout: float = 5.0):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:     # noqa: S310 — loopback only
        return r.status, r.read()


def _wait_up(port: int, proc, seconds: float = 90.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            if _get(f"http://127.0.0.1:{port}/auth/recovery-question", timeout=2)[0] == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"port on 127.0.0.1 (default {DEFAULT_PORT}; taken or 0 -> any free one)")
    ap.add_argument("--dir", help="the throwaway folder (inside the system temp folder)")
    ap.add_argument("--locked", action="store_true", help="set a panel password (printed)")
    ap.add_argument("--keep", action="store_true", help="leave the folder behind on exit")
    ap.add_argument("--gateway", action="store_true",
                    help="also run the gateway, so the present page is connected (implies --locked)")
    ap.add_argument("--upstream", default="",
                    help="the gateway's upstream and Loci's side model, with a placeholder key "
                         "(for scripts/replay_week.py's stand-in)")
    ap.add_argument("--clock", default="",
                    help="run on a fake clock starting at this ISO time with an offset, "
                         "e.g. 2026-10-14T08:00:00+08:00")
    ap.add_argument("--seed-into", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.seed_into:
        return _seed_child(args.seed_into)
    start_ms = None
    if args.clock:
        try:
            start = datetime.fromisoformat(args.clock)
        except ValueError:
            start = None
        if start is None or start.tzinfo is None:
            print(f"refused: --clock needs an ISO time with an offset, got {args.clock!r}", file=sys.stderr)
            return 2
        start_ms = int(start.timestamp() * 1000)

    if args.dir:
        base = os.path.realpath(args.dir)
        why = _usable_dir(base)
        if why:
            print(why, file=sys.stderr)
            return 2
        made = False
    else:
        base = os.path.realpath(tempfile.mkdtemp(prefix="loci-panel-dev-"))
        made = True
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, MARKER), "w", encoding="utf-8") as f:
        f.write("made by loci-brain scripts/dev_panel.py; safe to delete\n")
    paths = _layout(base)
    fresh = not os.path.isdir(paths["buckets"])
    for d in (paths["buckets"], paths["logs"]):
        os.makedirs(d, exist_ok=True)
    port = _free_port(args.port)
    gw_port = _free_port(0) if args.gateway else 0
    gw_url = f"http://127.0.0.1:{gw_port}" if args.gateway else ""
    _write_config(paths, gw_url, args.upstream)

    hook = secrets.token_hex(16)
    env = _child_env(paths, port, hook)
    clock = None
    if start_ms is not None:
        clock = {"iso": os.path.join(base, "clock.iso"), "ms": os.path.join(base, "clock.ms")}
        with open(clock["iso"], "w", encoding="utf-8") as f:
            f.write(start.isoformat())
        with open(clock["ms"], "w", encoding="utf-8") as f:
            f.write(str(start_ms))
        env["EXAM_CLOCK_FILE"] = clock["iso"]
    password = secrets.token_urlsafe(9) if args.locked or args.gateway else ""
    gw_env = None
    if args.gateway:
        host_token, fetch_token = secrets.token_hex(16), secrets.token_hex(16)
        env.update({"LOCI_HOST_TOKEN_GATEWAY": host_token, "LOCI_FETCH_TOKEN_GATEWAY": fetch_token})
        gw_env = {k: v for k, v in os.environ.items() if not k.upper().startswith("LOCI_")}
        gw_env.update({
            "PORT": str(gw_port),
            "LOCI_UPSTREAM": args.upstream or DEFAULT_UPSTREAM,
            "LOCI_MCP": f"http://127.0.0.1:{port}/mcp",
            "LOCI_GATEWAY_DATA": os.path.join(base, "gateway"),
            "LOCI_GATEWAY_TOKEN": fetch_token,
            "LOCI_HOOK_TOKEN": host_token,
            "LOCI_TZ": ZONE,
        })
        if args.upstream:
            gw_env.update({"LOCI_OWNER_NAME": "阿青", "LOCI_AI_NAME": "小满"})
        if clock:
            gw_env["LOCI_GATEWAY_TEST_CLOCK"] = clock["ms"]
        # For scripts/replay_week.py: where the gateway is, its key for /present (a fresh
        # random one, gone with this folder) and the clock files. No model key is in it.
        with open(os.path.join(base, HANDLE), "w", encoding="utf-8") as f:
            json.dump({"loci": f"http://127.0.0.1:{port}", "gateway": gw_url,
                       "gateway_token": fetch_token, "upstream": gw_env["LOCI_UPSTREAM"],
                       "zone": ZONE, "clock": clock,
                       "gateway_data": gw_env["LOCI_GATEWAY_DATA"],
                       "buckets": paths["buckets"]}, f, indent=1)

    try:
        if fresh:
            seed_env = dict(env, DEV_PANEL_PASSWORD=password) if password else dict(env)
            if args.upstream:
                seed_env[FILL_BLANKS_ENV] = "1"
            done = subprocess.run([sys.executable, os.path.abspath(__file__), "--seed-into", base],
                                  env=seed_env, cwd=base, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            if done.returncode != 0:
                print(done.stdout + done.stderr, file=sys.stderr)
                print("seeding failed", file=sys.stderr)
                return 1

        log = open(os.path.join(paths["logs"], "server.out.txt"), "w", encoding="utf-8")
        # Under --clock the server starts through exam/serve.py, which installs the fake
        # clock first (and builds BM25 before the first search; its other seams stay off).
        entry = os.path.join(ROOT, "exam", "serve.py") if clock else os.path.join(SRC, "server.py")
        server = subprocess.Popen([sys.executable, entry], env=env,
                                  cwd=base, stdout=log, stderr=subprocess.STDOUT)
        gateway = None
        if gw_env:
            gw_log = open(os.path.join(paths["logs"], "gateway.out.txt"), "w", encoding="utf-8")
            gateway = subprocess.Popen(["node", os.path.join(ROOT, "gateway", "server.js")], env=gw_env,
                                       cwd=base, stdout=gw_log, stderr=subprocess.STDOUT)
        try:
            if not _wait_up(port, server):
                print(f"the server did not come up; its output: {log.name}", file=sys.stderr)
                return 1
            # One breath handed out the way a host's window-opening hook asks for it (not a
            # peek), so breath's page has a last breath to show.
            try:
                _get(f"http://127.0.0.1:{port}/api/v2/breath?format=json",
                     headers={"x-loci-hook-token": hook}, timeout=60)
            except (urllib.error.URLError, OSError) as e:
                print(f"note: handing out a breath failed ({e}); breath's page will say none was", file=sys.stderr)

            print(f"Loci panel (throwaway library): http://127.0.0.1:{port}/loci", flush=True)
            print(f"library: {paths['buckets']}", flush=True)
            if gateway:
                print(f"gateway: {gw_url} (upstream {gw_env['LOCI_UPSTREAM']}; "
                      "it holds no key of its own)", flush=True)
                print(f"replay handle: {os.path.join(base, HANDLE)}", flush=True)
            if clock:
                print(f"fake clock from {start.isoformat()}: {clock['iso']} and {clock['ms']}", flush=True)
            if password:
                print(f"panel password: {password}  (security question 第一只猫叫什么 -> 团子)", flush=True)
            print("Ctrl+C stops it.", flush=True)
            server.wait()
        except KeyboardInterrupt:
            pass
        finally:
            for proc in (server, gateway):
                if proc is None or proc.poll() is not None:
                    continue
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            log.close()
    finally:
        if made and not args.keep:
            shutil.rmtree(base, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
