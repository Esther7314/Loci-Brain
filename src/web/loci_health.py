"""
========================================
web/loci_health.py — the health check, the settings page's top block, pulse and the log tail
========================================

    GET  /api/loci/health             -> this project's own health check (not the upstream diagnostics endpoint)
    GET  /api/loci/setup              -> the settings page's top block: five status rows, each saying what breaks if it is left unset
    GET  /api/logs                    -> the tail of server.log
    GET  /api/loci/pulse              -> health check: how many entries, how much space, are the engines alive
========================================
"""

import os
import sqlite3

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh
from core import _when as _w      # "today" in the user's local timezone — never call datetime.now() directly
from utils import read_from_ids
from .loci_reads import _BIGEVENT_TAG, _PROFILE_TAG
from .loci_similar import _emb_db_path

logger = sh.logger


_BARE_QUOTES_NAMED = 10   # the health row names this many entries with a bare quoted line


async def build_setup() -> dict:
    """The screen at the top of the settings page: **make the silent things visible.**

    The rule comes from a single day on which every failure encountered was silent:
        no tagging key       -> memories save, but with no tags and no summary   no error
        no embedding         -> search loses one of its two legs                 no error
        no names configured  -> both names bleed into tags and drown search      no error
        no alias table       -> two spellings of one person stay two people      no error
        TZ set in container  -> new memories become "the future" and vanish      no error
    All five were hit, and every one was found either by a smoke test or by a human noticing.
    So the most important thing this screen does is not collect settings, it is **tell the
    reader what happens if they do not set them.**

    Every row carries three things: what it is, what state it is in now, and **what goes
       wrong if it is not right**. The third column is the point: a newcomer does not know
       what "AI_NAME unset" means. Write "not configured" and they skip it; write "both of
       your names will bleed into the tags" and they understand.
    This screen has two readers: a person, and **that person's AI** — they screenshot these
       rows into their own assistant, which then knows immediately what to change. So it has
       to be written in plain language, not as KEY_NAME unset.
    """
    import os as _os
    from tools import _subjects as subj

    rows: list[dict] = []

    def row(key, label, ok, now, why, fix="", note=False):
        """note=True means **a fact that has to be stated clearly**, not "you configured
        this wrong".

        Why they are separated: without the distinction, "the panel has no lock" sits there
        forever reporting "1 item needs attention" — even where leaving it unlocked was a
        deliberate decision. A decision that raises an alarm every day is not an alarm any
        more, it is noise, and then the real alarms get ignored along with it.
        """
        rows.append({"key": key, "label": label, "ok": bool(ok), "note": bool(note),
                     "now": now, "why": why, "fix": fix})

    cfg = sh.config or {}
    dehy = cfg.get("dehydration", {}) or {}
    emb = cfg.get("embedding", {}) or {}

    # 1. The tagging model
    d_key = str(dehy.get("api_key") or "")
    d_model = str(dehy.get("model") or "")
    d_base = str(dehy.get("base_url") or "")
    row("dehydration", "打标模型", bool(d_key and d_model),
        (d_model + "（" + (d_base or "默认地址") + "）") if d_key and d_model else "没配",
        "存得进去，但没有标签、没有摘要、也抽不出人名 —— 而且一声不响。"
        "搜索靠标签和摘要，所以等于存了一堆搜不到的东西。")

    # 2. Vectors — search's second leg
    e_on = str(emb.get("enabled", "")).strip().lower() in ("1", "true", "yes", "on")
    e_key = str(emb.get("api_key") or "")
    e_model = str(emb.get("model") or "")
    e_base = str(emb.get("base_url") or "")
    e_ok = e_on and bool(e_model) and (bool(e_key) or "localhost" in e_base or "ollama" in e_base)
    # 🔴 Being configured and being usable are two different things, and this row used to
    #    check only the first. On a fresh install the bundled compose file starts an Ollama
    #    container with **no model pulled**: everything comes up, every service reports
    #    healthy, THIS ROW SHOWS A GREEN TICK, and the first real write is what fails.
    #    So when the configuration looks complete, ask the backend for one actual vector.
    #    Configuration is the one moment someone is prepared to hear about configuration.
    e_detail = ""
    if e_ok:
        # Read it off `sh` every time rather than binding it once: hot reload replaces the
        # instance by assigning to `sh.embedding_engine`, and a captured reference would
        # keep probing the engine that is no longer in use.
        engine = getattr(sh, "embedding_engine", None)
        probe = getattr(engine, "probe", None)
        if callable(probe):
            try:
                works, why = await probe(timeout_seconds=3.0)
            except Exception as exc:                  # noqa: BLE001 - a screen must not die
                works, why = False, str(exc)
            if not works:
                e_ok = False
                e_detail = f"{e_model} 配好了，但用不了：{why}"
    row("embedding", "向量", e_ok,
        e_detail or ((e_model + "（" + (e_base or "默认地址") + "）") if e_ok else
                     ("开着但没配全" if e_on else "关着")),
        "搜索少一条腿：只剩字面匹配。换个说法搜同一件事就搜不到了 —— "
        "而它不会告诉你「这次没用上向量」。")

    # 3. Your name and the AI's name
    ai_name = _os.environ.get("AI_NAME", "").strip()
    owner = _os.environ.get("LOCI_OWNER_NAME", "").strip()
    row("names", "你和他的名字", bool(ai_name and owner),
        ((ai_name or "没配") + " / " + (owner or "没配")),
        "这两个名字要挡在标签外面。没配的话，你俩的名字会被当成普通词抽进标签 —— "
        "而几乎每条记忆都有你们，于是这两个词淹掉整个搜索。",
        "改的是容器的环境变量 AI_NAME / LOCI_OWNER_NAME")

    # 4. The alias table
    apath = subj._alias_path()
    a_exists = _os.path.isfile(apath)
    table = subj.load_alias_table()
    row("aliases", "别名表", a_exists,
        (apath + "（" + str(len(table)) + " 条写法）") if a_exists else ("还没有：" + apath),
        "同一个人的几种写法会被当成几个人。「老张」和「张三」各算一个，"
        "按人找记忆就永远只找到一半。",
        "面板「整理 → 人名表」上点一下就是往这张表里写")

    # 5. Which timezone "today" is cut in. (The container's own TZ no longer matters:
    #    stamps are written as UTC by name, see utils.now_iso.)
    tz = _w.tz_status()
    row("tz", "时区", not tz["problem"],
        ("LOCI_TZ=" + tz["name"]) if not tz["problem"] else (tz["problem"] + " ⚠️"),
        "「今天」「昨天」「这周」都按这个时区切。设错或者没设、又不在 +8 的话，"
        "每天有几个小时的记忆会算到隔壁那天去，「今天存的东西今天翻不到」。",
        "在启动环境里设 LOCI_TZ，例如 Asia/Shanghai、America/Los_Angeles")

    # 6. The panel lock — after the strip-down /api/* stopped authenticating, so that gate
    #    never appeared again
    locked = False
    try:
        from . import panel_auth as _pa
        locked = bool(_pa.gate_needed())
    except Exception:                                # noqa: BLE001
        locked = False
    has_pw = False
    try:
        has_pw = sh._load_password_hash() is not None
    except Exception:                                # noqa: BLE001
        has_pw = False
    sw_on = str(cfg.get("panel_auth", True)).strip().lower() not in (
        "0", "false", "no", "off", "none", "")
    if locked:
        row("panel_lock", "面板的锁", True, "锁着（要输密码）",
            "", note=True)
    elif sw_on and not has_pw:
        row("panel_lock", "面板的锁", False, "开关开着，但还没设密码 —— 所以现在没锁",
            "锁一个还没有钥匙的门只会把你自己关在外面，所以没密码的时候这道门不生效。"
            "去上面「账号」设一把密码，锁立刻就生效了。")
    else:
        # The switch was **explicitly turned off** -> that is a decision someone made, not a
        # problem. Still say what it means (anyone can read all of your memories), but do
        # not count it in "N items need attention" — a decision that raises an alarm every
        # day is not an alarm any more, it is noise, and the real alarms get ignored with it.
        row("panel_lock", "面板的锁", False, "没有锁（你关掉的）",
            "任何能访问到这个地址的人都能看你全部记忆 —— 这台机器监听 0.0.0.0，"
            "同一个网里的设备都算。要锁上：上面「账号」里那个开关。",
            note=True)

    # 🔴 The gap between two pieces of advice that are each correct on their own.
    #    Locking the panel is the recommended setup. Once it is locked, the four hook
    #    routes the bridge uses stop being exempt and need a key — and if that key was
    #    never set, the bridge starts getting 401s.
    #
    #    Nobody does anything wrong to reach that state: they follow the instruction to
    #    set a password, and something they were not told about breaks. The 401 does say
    #    what to configure, but only to whoever reads the bridge's log, and the symptom
    #    people actually notice is that dreams and nudges quietly stop arriving.
    #    So it is said HERE, on the screen that exists to answer "is this set up right".
    if locked:
        try:
            from web.panel_auth import hook_token
            key = hook_token()
        except Exception:                            # noqa: BLE001
            key = ""
        row("hook_token", "桥的钥匙", bool(key),
            "配好了" if key else "面板锁着，但没配钥匙 —— 用桥的话它会被挡在门外",
            "" if key else
            "面板一上锁，桥走的那四条口就不再免检了。没有钥匙的话，"
            "梦和「该发呆了」会安静地不再送达 —— 桥那边收到的是 401，"
            "而你这边只会觉得它们不来了。"
            "设一个环境变量 LOCI_HOOK_TOKEN（随便一串够长的字），桥那边设同一个。")

    # 7. A hosts: table holding a host with a ceiling promises that host only its own
    #    material; the promise needs the panel locked and MCP auth on (panel_auth.lock_problem).
    try:
        from . import panel_auth as _pa
        hs = _pa.hosts()
    except Exception:                                # noqa: BLE001
        hs = None
    if hs is not None and hs.ceilinged:
        row("hosts_lock", "带上限的宿主", not hs.unsafe,
            "面板锁着、MCP 鉴权开着" if not hs.unsafe else hs.unsafe,
            "hosts 表里有只许碰自己那份材料的宿主（max_grant）。面板没锁、或者 MCP 鉴权关着的时候，"
            "它——和任何能连上这个端口的人——绕过去就能读全库，所以这时候带上限的宿主一律被拒。",
            "" if not hs.unsafe else
            "在上面「账号」里设一把口令（panel_auth 别关），config 里 mcp_require_auth 别关")
    # Two hosts declaring one place at the same depth (core/scope.load_hosts): neither is
    # taken there, which refuses quietly unless it is said here.
    if hs is not None and getattr(hs, "collisions", ()):
        row("hosts_collision", "宿主表里撞车的地方", False,
            "；".join(hs.collisions),
            "hosts 表里两个宿主在同一处、同一深度都写了 authority / provides / registers。"
            "这一处谁都不算：那里的变化通知一律拒、原话取不到、行列表也登记不进来；"
            "两个宿主别处的东西照常。",
            "在 config 的 hosts 表里把这一处只留给一个宿主，或者让其中一个写得更深一层")

    # ---- Read-only facts: not "is this configured correctly", but "where things are" ----
    ver = ""
    try:
        vp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
        with open(vp, "r", encoding="utf-8") as f:
            ver = f.read().strip()
    except OSError:
        ver = ""
    # The count goes through _visible — the same ruler recall, rooms and the subjects screen
    # use. Without that filter this reads 990 while the subjects screen reads 941: one thing
    # with two numbers, and anyone who sees both will simply assume one of them is wrong.
    try:
        from tools.recall.core import _visible as _vis
        allb = await sh.bucket_mgr.list_all(include_archive=False)
        n_buckets = sum(1 for b in allb if _vis(b.get("metadata", {}) or {}))
    except Exception:                                # noqa: BLE001
        n_buckets = -1
    return {
        "rows": rows,
        # Note rows do not count as "needs attention", or a decision already made would
        # raise an alarm every day.
        "bad": sum(1 for r in rows if not r["ok"] and not r["note"]),
        "facts": {
            "buckets_dir": str(cfg.get("buckets_dir") or os.environ.get("LOCI_BUCKETS_DIR") or ""),
            "log_file": os.environ.get("LOCI_LOG_FILE", ""),
            "alias_table": apath,
            "version": ver,
            "buckets": n_buckets,
            "in_docker": bool(sh.in_docker()) if hasattr(sh, "in_docker") else None,
            "tz_display": os.environ.get("LOCI_TZ", "").strip() or "Asia/Shanghai",
        },
    }


async def build_health() -> dict:
    """**Our own health check.**

    The upstream `/api/system/diagnostics` checks whether this open-source project is
    released correctly — ADR documents, the public tool manifest, vNext preflight, hosting
    environment variables. Not one of those is relevant to a person whose memories live in
    here.

    What this checks is **whether the memory itself is doing well**: is everything still
    there, can it be found, are the two external dependencies reachable, and could anything
    lost be recovered.
    """
    import shutil
    checks: list[dict] = []

    def add(label, status, message, action=""):
        checks.append({"label": label, "status": status,
                       "message": message, "action": action})

    # WARNING: this whole function used to run as one straight line. A failed bucket read, a
    # malformed metadata shape, a config section that was not a dict — an exception anywhere
    # meant **the entire health check returned 500**. Meanwhile the disk and dream sections
    # were `except: pass`, so two checks quietly disappeared and the summary still read
    # healthy. **A health check must not be all-or-nothing about itself.**
    # Each check now runs independently; one that blows up records a red row in place and the
    # rest carry on.
    def guard(label, fn, action=""):
        try:
            fn()
        except Exception as e:
            add(label, "error", f"这一项自己出错了：{type(e).__name__}: {e}", action)

    def need_buckets(label, fn, action=""):
        if not buckets_ok:
            add(label, "error", "读不到记忆库，这一项没法查", "先解决上面「记忆库读取」那条")
            return
        guard(label, fn, action)

    # ---- The base ingredient: read the buckets. A failure here must not take down the whole
    # ---- check; the independent items (config, disk) still run. ----
    metas: list[dict] = []
    n_with_archive = -1
    buckets_ok = True
    try:
        all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
        metas = [(b.get("metadata", {}) or {}) for b in all_buckets]
        # The wording used to say "N entries including the archive", but N was the count
        # above, which **excludes** the archive — the label did not match the number. The
        # archive has to be counted separately.
        n_with_archive = len(await sh.bucket_mgr.list_all(include_archive=True))
    except Exception as e:
        buckets_ok = False
        add("记忆库读取", "error", f"读不出记忆桶：{type(e).__name__}: {e}",
            "看容器日志 + buckets 目录挂载对不对")

    now = _w.now()          # local timezone

    # ---- Is everything still there ----
    from tools.recall.core import _visible
    visible: list[dict] = []
    bad_meta = 0
    for m in metas:
        # try per entry: one bad metadata record (a domain that is an integer, say) must not
        # silence the whole health check
        try:
            if _visible(m):
                visible.append(m)
        except Exception:
            bad_meta += 1
    if bad_meta:
        add("元数据形状", "error", f"{bad_meta} 条记忆的元数据读不动（字段类型不对）",
            "在「日志」里搜这几条的 id，多半是早期写入留下的")

    def sec_total():
        homeless = [m for m in visible if not str(m.get("room") or "")]
        # "Alive" goes through _visible, the same ruler used by recall, the subjects screen
        # and the small print on the settings page — one thing with two numbers means
        # whoever sees both will assume one of them is wrong.
        add("记忆总量", "ok",
            f"{len(visible)} 条活着的"
            + (f"（盘上一共 {n_with_archive} 条，含归档和旧版）"
               if n_with_archive >= 0 else ""))
        if homeless:
            add("没房间的记忆", "warn",
                f"{len(homeless)} 条没有 room，recall 的房间门筛不到它们",
                "跑 scripts/backfill_rooms.py --buckets <库目录> 先看，再加 --apply 补房间")
        else:
            add("房间", "ok", "每条都有房间")
    need_buckets("记忆总量", sec_total)

    # ---- Can it be found? (vector coverage) ----
    have_vec = 0
    try:
        con = sqlite3.connect(f"file:{_emb_db_path()}?mode=ro", uri=True)
        try:
            ids = {r[0] for r in con.execute("select bucket_id from embeddings")}
        finally:
            con.close()
        live_ids = {str(m.get("id") or "") for m in visible}
        have_vec = len(live_ids & ids)
        miss = len(live_ids) - have_vec
        if miss > max(3, len(live_ids) * 0.02):
            add("语义搜索覆盖", "warn",
                f"{miss} 条没有向量，query 门搜不到它们（只能靠关键词撞）",
                "看「日志」里 embedding 回填有没有报错；ollama 断了会积压")
        else:
            add("语义搜索覆盖", "ok", f"{have_vec}/{len(live_ids)} 条有向量")
    except Exception as e:
        add("语义搜索覆盖", "error", f"读不到向量库：{e}", "检查 embeddings.db")

    # ---- The two external dependencies (the only two places the system reaches the network) ----
    # Any section of cfg may fail to be a dict — a hand-edited config.yaml can do that — so
    # each gets its own guard.
    cfg = sh.config if isinstance(sh.config, dict) else {}

    def sec_deepseek():
        dehy = cfg.get("dehydration") or {}
        if not isinstance(dehy, dict):
            raise TypeError("config.yaml 里的 dehydration 不是一个配置块")
        if str(dehy.get("api_key") or "").strip() or os.environ.get("LOCI_API_KEY", ""):
            add("摘要/标签", "ok", f"配着 {dehy.get('model') or '?'}")
        else:
            add("摘要/标签", "warn",
                "没配 key —— 存进去的东西不会自动生成摘要和标签",
                "在 config.yaml 里配 dehydration.api_key")
    guard("摘要/标签", sec_deepseek, "检查 config.yaml 的 dehydration 段")

    def sec_embedding():
        emb = cfg.get("embedding") or {}
        if not isinstance(emb, dict):
            raise TypeError("config.yaml 里的 embedding 不是一个配置块")
        if _parse_ok(emb.get("enabled")):
            add("向量", "ok", f"开着，模型 {emb.get('model') or '?'}")
        else:
            add("向量", "warn",
                "关着 —— query 门只能靠关键词，搜不到「意思相近」的",
                "在 config.yaml 里开 embedding.enabled")
    guard("向量", sec_embedding, "检查 config.yaml 的 embedding 段")

    def sec_reembed():
        # Only said while a change of embedding model is being recomputed, or stopped
        # half-way (core/embedding_switch.py).
        bd = str(cfg.get("buckets_dir") or "")
        if not bd:
            return
        from core import embedding_switch as _es
        st = _es.status(bd)
        target = (st.get("target") or {}).get("model") or st.get("target_model") or "?"
        if st["phase"] == "running":
            add("换向量模型", "warn",
                f"正在用 {target} 重算：{st.get('done', 0)}/{st.get('total', 0)}"
                f"（失败 {st.get('failed_count', 0)}）；算完之前旧模型照常用")
        elif st["phase"] in ("failed", "interrupted"):
            add("换向量模型", "error",
                f"换到 {target} 的重算没完成：{st.get('message') or st.get('error') or '中途停了'}"
                "；旧模型和旧向量照用",
                "在「设置 → 引擎」接着算，或者放弃这一次" if st.get("resumable") else "")
    guard("换向量模型", sec_reembed)

    def sec_literal():
        from core.bm25_index import dependency_status
        deps = dependency_status()
        missing = [name for name, ok in deps.items() if not ok]
        if not missing:
            add("字面搜索", "ok", "rank_bm25 和 jieba 都在")
        else:
            add("字面搜索", "error",
                f"缺 {' / '.join(missing)} —— 字面搜索退成了整句子串匹配，"
                "换个说法、中文拆词都搜不到",
                "pip install " + " ".join(
                    "rank-bm25" if n == "rank_bm25" else n for n in missing)
                + "，装完重启")
    guard("字面搜索", sec_literal)

    def sec_tz():
        st = _w.tz_status()
        if st["problem"]:
            add("时区", "error",
                f"{st['problem']} —— 「今天」「昨天」「这周」按这个时区切",
                "在启动环境里设 LOCI_TZ，例如 Asia/Shanghai，然后重启")
        else:
            add("时区", "ok", f"LOCI_TZ={st['name']}")
    guard("时区", sec_tz)

    # ---- Could anything lost be recovered ----
    bd = str(cfg.get("buckets_dir") or "")

    def sec_persist():
        pers = sh.data_dir_persistence(bd)
        if pers.get("persistent"):
            add("数据持久性", "ok", pers.get("note") or "记忆目录在持久位置")
        else:
            add("数据持久性", "error", "记忆目录没挂到持久卷 —— 容器重建会丢！",
                "在 docker-compose 里挂到命名卷或宿主机目录")
    guard("数据持久性", sec_persist)

    def sec_disk():
        # This used to be `except: pass` — the disk check going quiet at exactly the moment
        # it most needed to speak.
        free_gb = shutil.disk_usage(bd).free / (1024**3)
        add("磁盘", "ok" if free_gb > 2 else "warn", f"还剩 {free_gb:.1f} GB",
            "" if free_gb > 2 else "腾点地方，写不进去就存不了记忆")
    guard("磁盘", sec_disk, f"确认 buckets_dir 存在：{bd or '(没配)'}")

    def sec_schema():
        from core import schema as _schema
        st = _schema.status(bd)
        if st["error"]:
            add("库的版本", "error", f"读不出库的版本：{st['error']}",
                "看 buckets/_state/schema.json")
        elif st["behind"]:
            add("库的版本", "error",
                f"库是第 {st['version']} 版，代码要第 {st['current']} 版",
                "停掉服务，先跑 python scripts/migrate.py 看要改什么，"
                "再加 --apply（会先备份整个库）")
        else:
            add("库的版本", "ok", f"第 {st['version']} 版")
    guard("库的版本", sec_schema)

    # ---- Is it still growing lately ----
    def sec_fresh():
        # An empty store is not "writes are broken": a fresh install should see "nothing yet"
        # rather than a screen of yellow. Memories present but none in the last seven days —
        # *that* might mean writes are broken, and only then is a warning warranted.
        fresh = 0
        for m in visible:
            # This used to be fromisoformat(str(created)[:19]) — exactly the slice that is
            # banned elsewhere. It cuts off the timezone suffix, producing a naive datetime,
            # while `now = _w.now()` above is timezone-aware; subtracting the two raises
            # TypeError, which the surrounding except then swallowed -> fresh was always 0 ->
            # the panel reported "nothing stored at all" every single day. This was the last
            # surviving [:19] in the codebase, and it was spotted in a screenshot. Silently
            # wrong, and frightening in exactly the wrong direction.
            ts = _w.parse_stamp(m.get("created"))
            if ts is not None and (now - ts).days < 7:
                fresh += 1
        if fresh:
            add("最近七天", "ok", f"存了 {fresh} 条")
        elif not visible:
            add("最近七天", "note", "还没存过东西 —— 存第一条之后这儿就有数了")
        else:
            add("最近七天", "warn",
                "一条都没存 —— 要么最近没聊，要么写入坏了",
                "去「日志」看看 grow 有没有报错")
    need_buckets("最近七天", sec_fresh)

    # ---- Open wants nobody is bound by ----
    def sec_unbound_wants():
        # 惦记的事 (core/profile.prospective) shows a want by its date, its cue, or as an
        # undated promise someone is bound by. One with none of the three is never shown:
        # migrated wants whose `bound` was never filled are exactly that, and nothing else
        # says so.
        from core import _holds as _H
        from core.profile import _waits_on_cue, due_day
        from utils import is_closed, is_telic
        today = now.date()
        ids = [str(m.get("id") or "") for m in visible
               if is_telic(m) and not is_closed(m) and not m.get("bound")
               and not _H.is_hold(m) and not str(m.get("superseded_by") or "").strip()
               and due_day(m, today) is None and not _waits_on_cue(m)]
        ids = [i for i in ids if i]
        if ids:
            add("没人认领的想要", "error",
                f"{len(ids)} 条还开着的想要没有 bound（谁该做），也没有日子或条件，"
                f"「惦记的事」里永远看不到它们：{'、'.join(ids)}",
                "给每条补上 bound：面板上改，或 trace(bucket_id=…, bound=[\"谁\"])")
        else:
            add("没人认领的想要", "ok", "开着的想要都有人认领，或有日子、有条件")
    need_buckets("没人认领的想要", sec_unbound_wants)

    # ---- Quoted lines that name no source ----
    def sec_bare_quotes():
        # A wasQuotedFrom line holding only the host's bare id (m_0142) names no container:
        # its original cannot be fetched and a withdrawal cannot reach the entry through
        # it. A new write refuses such a line (tools/grow/rooms_path.check_sources); these
        # are the entries written before.
        from utils import WAS_QUOTED_FROM, read_prov
        ids = [str(m.get("id") or "") for m in visible
               if any(ln["rel"] == WAS_QUOTED_FROM and "#" not in ln["target"]
                      for ln in read_prov(m))]
        ids = [i for i in ids if i]
        if ids:
            shown = "、".join(ids[:_BARE_QUOTES_NAMED]) + (
                f" 等 {len(ids)} 条" if len(ids) > _BARE_QUOTES_NAMED else "")
            add("引原话追不到来源", "warn",
                f"{len(ids)} 条记忆有引原话的线只写了编号，没写系统和容器——"
                f"原话取不回，来源撤回也够不着它们：{shown}",
                "知道是哪段对话的，用 trace(bucket_id=…, sources_append=[{system, instance, "
                "container, id}]) 补上那条记录，编号对上的线会接过去")
        else:
            add("引原话追不到来源", "ok", "引原话的线都写全了来源")
    need_buckets("引原话追不到来源", sec_bare_quotes)

    # ---- Are the things that should be there still there ----
    def _tags_of(m) -> list[str]:
        raw = m.get("tags")
        return [str(t) for t in raw] if isinstance(raw, (list, tuple)) else []

    def sec_profile():
        # The same gate the door uses (core.profile.door_note): a covered page keeps its
        # tag but is no longer the page.
        from core import _fold as _F
        tagged = [m for m in metas if _PROFILE_TAG in _tags_of(m)]
        profile = [m for m in tagged if not _F.is_covered(m)]
        if len(profile) == 1:
            add("门口那张纸", "ok", "名字页在，且只有一张")
        elif not profile and tagged:
            gone = tagged[0]
            add("门口那张纸", "error",
                f"名字页 {gone.get('id')} 被 {'、'.join(_F.covers_of(gone))} 换掉了，"
                "新版没带 tag —— 睁眼时档案那格是空的",
                f"给新版补上 tag {_PROFILE_TAG}（trace 的 tags 是整份替换，原来的一起写上）")
        elif not profile:
            add("门口那张纸", "note" if not visible else "warn",
                "还没有名字页 —— 睁眼时档案那格是空的"
                if not visible else "没有名字页 —— 睁眼时档案那格是空的",
                f"存一条带 tag {_PROFILE_TAG} 的记忆")
        else:
            add("门口那张纸", "error", f"有 {len(profile)} 张名字页，只该有一张", "合并掉多的")
    need_buckets("门口那张纸", sec_profile)

    def sec_pinned():
        pinned = [m for m in visible if m.get("pinned")]
        # Nothing pinned means "nothing pinned yet", not "broken": principles grow one at a
        # time.
        add("钉着的准则", "ok" if pinned else "note",
            f"{len(pinned)} 条" if pinned else "一条都没钉 —— 睁眼时准则那格是空的")
    need_buckets("钉着的准则", sec_pinned)

    def sec_big():
        # Renamed and downgraded. Two things changed:
        #   - the "big event" entry point was withdrawn; it is called a **period** now, and
        #     goes through fold(when=...)
        #   - the "long range" cell was dropped from both the waking screen and the panel
        #     (the back end had stopped supplying it long before)
        # And a period is **optional by design** — use one if you have one. Having no periods
        # is not a fault, and reporting it as a warning tells a fresh install "you are
        # missing something".
        big = [m for m in metas if _BIGEVENT_TAG in _tags_of(m)]
        add("时期", "ok" if big else "note",
            f"{len(big)} 个" if big else "还没给哪段日子起过名（不强制，有就用）")
    need_buckets("时期", sec_big)

    # ---- Chains that point at nothing ----
    async def sec_orphan():
        """A `from` that no longer resolves — **there are two kinds, and they are not the
        same thing.**

        This check used to merge both into one sentence: "the source of N memories points at
        a bucket that does not exist, most likely because that source was hard-deleted."
        Asked whether the health check was actually correct, a look at the data showed that
        of 26 such entries, **24 had their source sitting safely in the archive** —
        `trace(delete=True)` is a soft delete and a direct id lookup always recovers it —
        and only 2 were genuinely missing.
        So the number was right and the sentence was wrong, in the worst possible direction:
        describing something perfectly normal as "hard-deleted" sends someone hunting for an
        incident that never happened.
        """
        live_ids = {str(m.get("id") or "") for m in metas}
        try:
            allb = await sh.bucket_mgr.list_all(include_archive=True)
            all_ids = {str((b.get("metadata") or {}).get("id") or "") for b in allb}
        except Exception:                            # noqa: BLE001
            all_ids = live_ids                       # if the archive cannot be read, fall back to the old measure
        sunk = gone = 0
        for m in metas:
            for src in read_from_ids(m):
                if src in live_ids:
                    continue
                if src in all_ids:
                    sunk += 1
                else:
                    gone += 1
        if gone:
            add("断掉的 from 链", "warn",
                f"{gone} 条记忆的来源哪儿都找不到了",
                "这才是真断了：多半那条源被物理删过。星空里它们少一根线")
        elif not sunk:
            add("from 链", "ok", "每条 from 都指得到")
        if sunk:
            add("来源沉进归档区", "note",
                f"{sunk} 条记忆的来源已经归档 —— 没断，拿 id 直查捞得回",
                "" if gone else "")
    if not buckets_ok:
        add("from 链", "error", "读不到记忆库，这一项没法查", "先解决上面「记忆库读取」那条")
    else:
        try:
            await sec_orphan()
        except Exception as e:                       # noqa: BLE001
            add("from 链", "error", f"这一项自己出错了：{type(e).__name__}: {e}", "")

    # ---- Dreams on disk ----
    # Since night_fall was retired, this counts the new engine's dream files. **Empty is
    # normal**: dreams are time-driven and disappear if left alone, and a backlog that never
    # reaches the threshold simply means a night without dreams.
    def sec_dreams():
        # Same story: this used to be `except OSError: pass`, so a missing directory made
        # the whole check vanish.
        from core import _dream as _D
        n = len(_D.load_dreams())
        add("盘上的梦", "ok",
            f"{n} 个还在（时间到了自己会没）" if n else "空的（攒不到线就一夜无梦，正常）")
    guard("盘上的梦", sec_dreams, "确认 buckets/night_fall/dreams 目录在")

    # This used to hardcode three states (ok/warn/error), while the health check **has a
    #    fourth, `note`** — "you have not started yet", "this one is optional": neutral
    #    statements, not problems.
    #    The consequence: the page said "14 items" while the three summary numbers added up
    #    to 13. **The health check was under-reporting itself**, and the health check is
    #    precisely the thing whose job is to tell the truth.
    #    (The smoke test that asserts the summary matches the item count catches this.)
    # The rule: **count whatever states actually appear**, never a hardcoded list — a
    #    hardcoded list would miss the same way again when a fifth state is added.
    summary = {"ok": 0, "warn": 0, "error": 0}
    for c in checks:
        st = str(c.get("status") or "").lower() or "unknown"
        summary[st] = summary.get(st, 0) + 1
    return {"ok": summary["error"] == 0, "summary": summary, "checks": checks}


def _parse_ok(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


async def api_loci_health(request: Request) -> Response:
    """Our own health check. The upstream /api/system/diagnostics checks release
    compliance, not whether this memory is doing well."""
    from starlette.responses import JSONResponse
    try:
        return JSONResponse(await build_health())
    except Exception as e:
        logger.warning(f"[loci] health 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_loci_setup(request: Request) -> Response:
    """The screen at the top of the settings page: five status rows plus read-only facts.
    **Read-only.**

    Why it exists: all five failures encountered in one day were silent, and not one of
    them raised an error. This endpoint's job is not to collect settings, it is to turn
    the silent things into visible ones.
    """
    from starlette.responses import JSONResponse
    try:
        return JSONResponse(await build_setup())
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"[loci] setup 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_logs(request: Request) -> Response:
    """Logs: read the tail of server.log. **Read-only.**

    Restored. This route used to live in `web/system.py` and went with it when the twenty
    upstream modules were cut — while the panel's entire log section kept calling it,
    getting HTML back from the 404, and blowing up in the front-end's `.json()`. What the
    user saw was "the response was not JSON". **The writing side was alive the whole
    time** (utils.setup_logging writes to <buckets>/.logs/server.log); nobody could read
    it.

    `level` filters upward by severity: choosing WARNING also returns ERROR and CRITICAL.
    Someone selecting "warnings" wants to know whether anything is wrong, not "warnings
    but please hide the errors".
    """
    from starlette.responses import JSONResponse
    q = request.query_params
    level = (q.get("level") or "WARNING").strip().upper()
    try:
        limit = max(1, min(2000, int(q.get("limit") or 200)))
    except (TypeError, ValueError):
        limit = 200
    path = os.environ.get("LOCI_LOG_FILE", "").strip()
    if not path or not os.path.exists(path):
        return JSONResponse({
            "lines": [], "log_file": path,
            "note": "还没有日志文件（LOCI_LOG_FILE 没设，或者这次启动没开文件日志）。",
        })
    rank = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    floor = 0 if level == "ALL" else rank.get(level, 30)
    try:
        # Read only the tail: the log rotates at 5MB, and reading all of it is pure waste
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 1024 * 1024))
            raw = f.read().decode("utf-8", "replace")
        lines = raw.splitlines()
        if size > 1024 * 1024 and lines:
            lines = lines[1:]                    # drop the first line, which was cut in half
        keep = []
        for ln in lines:
            if floor:
                hit = next((lv for lv in rank if f" {lv}:" in ln or f" {lv} " in ln), "")
                if not hit or rank[hit] < floor:
                    continue
            keep.append(ln)
        return JSONResponse({
            "lines": keep[-limit:],
            "log_file": path,
            "level": level,
            "note": "" if keep else f"{level} 这一档下没有东西。",
        })
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"[loci] logs 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_loci_pulse(request: Request) -> Response:
    """Health: how many entries, how much space, are the engines alive. **Read-only;
    nothing is written to disk.**

    `pulse` was withdrawn from the MCP tool surface — the other nine tools are all "what
    am I doing to a memory", and this one alone is "is this machine healthy", which is
    not a memory action.
    The implementation is unchanged (`tools/pulse/`); only its entry point moved from the
    tool surface to this read-only route, which the panel uses to draw the health card.

    The `include_archive=1` query parameter includes the archive in the report.
    """
    from starlette.responses import PlainTextResponse
    from tools import pulse as _pulse
    inc = str(request.query_params.get("include_archive") or "").strip() in ("1", "true", "yes")
    try:
        return PlainTextResponse(await _pulse.pulse(include_archive=inc))
    except Exception as e:                       # noqa: BLE001 - the health endpoint must not take the panel down with it
        logger.warning(f"[loci] pulse 失败: {e}")
        return PlainTextResponse(f"pulse 失败：{e}", status_code=500)
