# -*- coding: utf-8 -*-
"""
dev_panel.py — the panel on a throwaway library, for looking at it

    python scripts/dev_panel.py              # http://127.0.0.1:18761/loci
    python scripts/dev_panel.py --port 0     # any free port
    python scripts/dev_panel.py --locked     # with a panel password, to see the login page
    python scripts/dev_panel.py --keep       # leave the library folder behind afterwards

Starts the real server (src/server.py, streamable-http) on 127.0.0.1 over a library made
fresh in the system temp folder and seeded with a small believable sample through the
store's own APIs: a name page and two pinned rules, events today (a promise with a day,
another with a hold on it, one waiting on a cue, a yearly day, something heard about a
friend), an
entry formed from an imported conversation (its 看原话 answers with who and when), a
judgement derived from two events carrying a 人说不对 mark, two names in the alias
table, a slices batch from a host. It then asks for one breath the way a host's
window-opening hook does, so breath's page has a last breath to show.

🔴 It never touches a real library. Every LOCI_* variable of the calling shell is dropped
   (a LOCI_BUCKETS_DIR, LOCI_VAULT_DIR, LOCI_ALIAS_TABLE or LOCI_DASHBOARD_PASSWORD left
   in it would point the server at real data or a real password) and set again to the
   temp folder. `--dir` must lie inside the system temp folder and be empty, new, or a
   folder this script made before (it carries a marker file); anything else is refused.
   The seeding runs in a child process started with that environment, so no module that
   reads its paths at import time (core/names reads the alias table's) ever sees another.

Without a side model or an embedding service, what degrades: memories are written with no
model-filled tags or summaries, recall finds by words only (no 「意思相近」), nothing has a
vector (embedding is off in the throwaway config), and dreams, musing and slicing a
host's lines do not run. The panel's reads all answer.

Public surface: run as a script. `seed(store, base_dir)` fills a store (the child's work).
"""

import argparse
import asyncio
import json
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
MARKER = ".loci-panel-dev"
DEFAULT_PORT = 18761


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
        "LOCI_HOOK_SKIP": "1",
        "LOCI_HOOK_TOKEN": hook_token,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    return env


def _write_config(paths: dict) -> None:
    # JSON is YAML: written without a YAML library, read back by the server's.
    cfg = {
        "transport": "streamable-http",
        "buckets_dir": paths["buckets"],
        "log_level": "INFO",
        "embedding": {"enabled": False},
        "dehydration": {"api_key": ""},
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

async def seed(store, base_dir: str) -> dict:
    """Fill `store` with the sample. Returns the ids worth knowing."""
    from datetime import timedelta
    from core import _when as W
    from core import _invalidation as I
    from core import names as N
    from core.import_memory import ImportStore
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
    ImportStore(base_dir).create(
        {"batch": batch, "same_self": True, "human": "阿青", "title": "chat-export.json",
         "conversations": [{"container": "c0001"}]},
        {"c0001": [
            {"id": "l0001", "role": "user", "at": (today - timedelta(days=2)).isoformat(),
             "text": "周六去海边吧，好久没看海了"},
            {"id": "l0002", "role": "assistant", "at": (today - timedelta(days=2, minutes=-1)).isoformat(),
             "text": "好，周六去。"},
        ]})
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
    await store.slices.record_batch(
        batch_id="b_" + secrets.token_hex(6), source=src, day=day(-1), revision=None,
        lines=lines, host="lento", report_at=(today - timedelta(hours=6)).isoformat(timespec="seconds"),
        slices=[
            {"first": "m_0001", "last": "m_0003", "gist": "阿青说牙又疼了，约了周六",
             "guesses": [{"id": ids["teeth"], "score": 0.81}]},
            {"first": "m_0004", "last": "m_0006", "gist": "聊到考完想去吃甜品", "guesses": []},
        ])
    return ids


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
    from core import runtime as rt
    from core.bucket_manager import BucketManager
    from tools.grow import rooms_path

    cfg = {"buckets_dir": paths["buckets"]}
    store = BucketManager(cfg)
    rt.bucket_mgr, rt.config = store, cfg

    async def no_backfill(pairs):     # no side model here: nothing fills tags afterwards
        return None
    rooms_path._backfill_batch = no_backfill

    if os.environ.get("DEV_PANEL_PASSWORD"):
        from web import _shared as sh
        sh.init(cfg)
        sh._save_password_hash(os.environ["DEV_PANEL_PASSWORD"])
        sh._save_security_qa("第一只猫叫什么", "团子")

    ids = asyncio.run(seed(store, paths["buckets"]))
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
    ap.add_argument("--seed-into", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.seed_into:
        return _seed_child(args.seed_into)

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
    _write_config(paths)

    port = _free_port(args.port)
    hook = secrets.token_hex(16)
    env = _child_env(paths, port, hook)
    password = secrets.token_urlsafe(9) if args.locked else ""

    try:
        if fresh:
            seed_env = dict(env, DEV_PANEL_PASSWORD=password) if password else env
            done = subprocess.run([sys.executable, os.path.abspath(__file__), "--seed-into", base],
                                  env=seed_env, cwd=base, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            if done.returncode != 0:
                print(done.stdout + done.stderr, file=sys.stderr)
                print("seeding failed", file=sys.stderr)
                return 1

        log = open(os.path.join(paths["logs"], "server.out.txt"), "w", encoding="utf-8")
        server = subprocess.Popen([sys.executable, os.path.join(SRC, "server.py")], env=env,
                                  cwd=base, stdout=log, stderr=subprocess.STDOUT)
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
            if password:
                print(f"panel password: {password}  (security question 第一只猫叫什么 -> 团子)", flush=True)
            print("Ctrl+C stops it.", flush=True)
            server.wait()
        except KeyboardInterrupt:
            pass
        finally:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
            log.close()
    finally:
        if made and not args.keep:
            shutil.rmtree(base, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
