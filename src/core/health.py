"""
========================================
core/health.py — the health check and the settings page's top block
========================================

Two of the panel's reads (GET /api/loci/health, GET /api/loci/setup) are checks over the
library, the configuration and the machine. Each check is a named function here; the
routes in web/loci_health.py hand in what only the web side holds (the config and the
engines it was given, the panel lock's state) and turn the dict into JSON.

health(): **whether the memory itself is doing well** — is everything still there, can
   it be found, are the two external dependencies reachable, could anything lost be
   recovered. Not release compliance (that is the upstream /api/system/diagnostics).
setup(): the settings page's top block — every silent failure made visible, each row
   saying what goes wrong if it is left as it is.

Exports: health(bucket_mgr, config, persistence) · setup(config, bucket_mgr,
         embedding_engine, panel, in_docker) · PanelLock
========================================
"""

import inspect
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from . import _when as _w
from .profile import _BIGEVENT_TAG, _PROFILE_TAG
from .similarity import stored_ids
from .visibility import on_timeline
from utils import read_from_ids

_BARE_QUOTES_NAMED = 10   # the health row names this many entries with a bare quoted line


# ============================================================
# The health check
# ============================================================

@dataclass
class _Ground:
    """What every check reads: the live entries' metadata, the visible ones among them,
    the configuration, the clock."""
    bucket_mgr: Any
    config: Any                      # as handed in, possibly not a dict
    cfg: dict                        # the config when it is a dict, else {}
    bd: str                          # buckets_dir from cfg, "" when unset
    now: datetime
    persistence: Callable[[str], dict]
    metas: list = field(default_factory=list)
    visible: list = field(default_factory=list)
    n_with_archive: int = -1
    buckets_ok: bool = True


class _Checks:
    """The rows, in the order they are added.

    WARNING: **a health check must not be all-or-nothing about itself.** Run as one
    straight line, a failed bucket read, a malformed metadata shape, a config section that
    is not a dict — an exception anywhere — would make **the entire health check return
    500**; swallowed with `except: pass`, a check quietly disappears and the summary still
    reads healthy. Each check runs independently; one that blows up records a red row in
    place and the rest carry on.
    """

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, label, status, message, action=""):
        self.rows.append({"label": label, "status": status,
                          "message": message, "action": action})

    async def guard(self, label, fn, g: _Ground, action=""):
        try:
            res = fn(self, g)
            if inspect.isawaitable(res):
                await res
        except Exception as e:
            self.add(label, "error", f"这一项自己出错了：{type(e).__name__}: {e}", action)

    async def need_buckets(self, label, fn, g: _Ground, action=""):
        if not g.buckets_ok:
            self.add(label, "error", "读不到记忆库，这一项没法查", "先解决上面「记忆库读取」那条")
            return
        await self.guard(label, fn, g, action)


def _parse_ok(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _tags_of(m) -> list[str]:
    raw = m.get("tags")
    return [str(t) for t in raw] if isinstance(raw, (list, tuple)) else []


# ---- Is everything still there ----
def check_total(c: _Checks, g: _Ground) -> None:
    homeless = [m for m in g.visible if not str(m.get("room") or "")]
    # "Alive" goes through the timeline gate, the same ruler used by recall, the subjects
    # screen and the small print on the settings page — one thing with two numbers means
    # whoever sees both will assume one of them is wrong.
    c.add("记忆总量", "ok",
          f"{len(g.visible)} 条活着的"
          + (f"（盘上一共 {g.n_with_archive} 条，含归档和旧版）"
             if g.n_with_archive >= 0 else ""))
    if homeless:
        c.add("没房间的记忆", "warn",
              f"{len(homeless)} 条没有 room，recall 的房间门筛不到它们",
              "跑 scripts/backfill_rooms.py --buckets <库目录> 先看，再加 --apply 补房间")
    else:
        c.add("房间", "ok", "每条都有房间")


# ---- Can it be found? (vector coverage) ----
def check_vectors(c: _Checks, g: _Ground) -> None:
    """Runs on its own (not under guard): a store that cannot be read is this check's own
    red row, with its own wording."""
    try:
        ids = stored_ids(g.config["buckets_dir"])
        live_ids = {str(m.get("id") or "") for m in g.visible}
        have_vec = len(live_ids & ids)
        miss = len(live_ids) - have_vec
        if miss > max(3, len(live_ids) * 0.02):
            c.add("语义搜索覆盖", "warn",
                  f"{miss} 条没有向量，query 门搜不到它们（只能靠关键词撞）",
                  "看「日志」里 embedding 回填有没有报错；ollama 断了会积压")
        else:
            c.add("语义搜索覆盖", "ok", f"{have_vec}/{len(live_ids)} 条有向量")
    except Exception as e:
        c.add("语义搜索覆盖", "error", f"读不到向量库：{e}", "检查 embeddings.db")


# ---- The two external dependencies (the only two places the system reaches the network) ----
# Any section of cfg may fail to be a dict — a hand-edited config.yaml can do that — so
# each gets its own guard.
def check_summariser(c: _Checks, g: _Ground) -> None:
    dehy = g.cfg.get("dehydration") or {}
    if not isinstance(dehy, dict):
        raise TypeError("config.yaml 里的 dehydration 不是一个配置块")
    if str(dehy.get("api_key") or "").strip() or os.environ.get("LOCI_API_KEY", ""):
        c.add("摘要/标签", "ok", f"配着 {dehy.get('model') or '?'}")
    else:
        c.add("摘要/标签", "warn",
              "没配 key —— 存进去的东西不会自动生成摘要和标签",
              "在 config.yaml 里配 dehydration.api_key")


def check_embedding(c: _Checks, g: _Ground) -> None:
    emb = g.cfg.get("embedding") or {}
    if not isinstance(emb, dict):
        raise TypeError("config.yaml 里的 embedding 不是一个配置块")
    if _parse_ok(emb.get("enabled")):
        c.add("向量", "ok", f"开着，模型 {emb.get('model') or '?'}")
    else:
        c.add("向量", "warn",
              "关着 —— query 门只能靠关键词，搜不到「意思相近」的",
              "在 config.yaml 里开 embedding.enabled")


def check_reembed(c: _Checks, g: _Ground) -> None:
    # Only said while a change of embedding model is being recomputed, or stopped
    # half-way (core/embedding_switch.py).
    bd = str(g.cfg.get("buckets_dir") or "")
    if not bd:
        return
    from . import embedding_switch as _es
    st = _es.status(bd)
    target = (st.get("target") or {}).get("model") or st.get("target_model") or "?"
    if st["phase"] == "running":
        c.add("换向量模型", "warn",
              f"正在用 {target} 重算：{st.get('done', 0)}/{st.get('total', 0)}"
              f"（失败 {st.get('failed_count', 0)}）；算完之前旧模型照常用")
    elif st["phase"] in ("failed", "interrupted"):
        c.add("换向量模型", "error",
              f"换到 {target} 的重算没完成：{st.get('message') or st.get('error') or '中途停了'}"
              "；旧模型和旧向量照用",
              "在「设置 → 引擎」接着算，或者放弃这一次" if st.get("resumable") else "")


def check_literal(c: _Checks, g: _Ground) -> None:
    from .bm25_index import dependency_status
    deps = dependency_status()
    missing = [name for name, ok in deps.items() if not ok]
    if not missing:
        c.add("字面搜索", "ok", "rank_bm25 和 jieba 都在")
    else:
        c.add("字面搜索", "error",
              f"缺 {' / '.join(missing)} —— 字面搜索退成了整句子串匹配，"
              "换个说法、中文拆词都搜不到",
              "pip install " + " ".join(
                  "rank-bm25" if n == "rank_bm25" else n for n in missing)
              + "，装完重启")


def check_tz(c: _Checks, g: _Ground) -> None:
    st = _w.tz_status()
    if st["problem"]:
        c.add("时区", "error",
              f"{st['problem']} —— 「今天」「昨天」「这周」按这个时区切",
              "在启动环境里设 LOCI_TZ，例如 Asia/Shanghai，然后重启")
    else:
        c.add("时区", "ok", f"LOCI_TZ={st['name']}")


# ---- Could anything lost be recovered ----
def check_persist(c: _Checks, g: _Ground) -> None:
    pers = g.persistence(g.bd)
    if pers.get("persistent"):
        c.add("数据持久性", "ok", pers.get("note") or "记忆目录在持久位置")
    else:
        c.add("数据持久性", "error", "记忆目录没挂到持久卷 —— 容器重建会丢！",
              "在 docker-compose 里挂到命名卷或宿主机目录")


def check_disk(c: _Checks, g: _Ground) -> None:
    # No `except: pass` here (guard() records the failure) — the disk check must not go
    # quiet at exactly the moment it most needs to speak.
    free_gb = shutil.disk_usage(g.bd).free / (1024**3)
    c.add("磁盘", "ok" if free_gb > 2 else "warn", f"还剩 {free_gb:.1f} GB",
          "" if free_gb > 2 else "腾点地方，写不进去就存不了记忆")


def check_schema(c: _Checks, g: _Ground) -> None:
    from . import schema as _schema
    st = _schema.status(g.bd)
    if st["error"]:
        c.add("库的版本", "error", f"读不出库的版本：{st['error']}",
              "看 buckets/_state/schema.json")
    elif st["behind"]:
        c.add("库的版本", "error",
              f"库是第 {st['version']} 版，代码要第 {st['current']} 版",
              "停掉服务，先跑 python scripts/migrate.py 看要改什么，"
              "再加 --apply（会先备份整个库）")
    else:
        c.add("库的版本", "ok", f"第 {st['version']} 版")


# ---- Is it still growing lately ----
def check_fresh(c: _Checks, g: _Ground) -> None:
    # An empty store is not "writes are broken": a fresh install should see "nothing yet"
    # rather than a screen of yellow. Memories present but none in the last seven days —
    # *that* might mean writes are broken, and only then is a warning warranted.
    fresh = 0
    for m in g.visible:
        # Never fromisoformat(str(created)[:19]): the slice cuts off the timezone suffix,
        # producing a naive datetime, while `now` is timezone-aware; subtracting the two
        # raises TypeError -> fresh is always 0 -> the panel reports "nothing stored at
        # all" every single day.
        ts = _w.parse_stamp(m.get("created"))
        if ts is not None and (g.now - ts).days < 7:
            fresh += 1
    if fresh:
        c.add("最近七天", "ok", f"存了 {fresh} 条")
    elif not g.visible:
        c.add("最近七天", "note", "还没存过东西 —— 存第一条之后这儿就有数了")
    else:
        c.add("最近七天", "warn",
              "一条都没存 —— 要么最近没聊，要么写入坏了",
              "去「日志」看看 grow 有没有报错")


# ---- Open wants nobody is bound by ----
def check_unbound_wants(c: _Checks, g: _Ground) -> None:
    # 惦记的事 (core/profile.prospective) shows a want by its date, its cue, or as an
    # undated promise someone is bound by. One with none of the three is never shown:
    # migrated wants whose `bound` was never filled are exactly that, and nothing else
    # says so.
    from . import _holds as _H
    from .profile import _waits_on_cue, due_day
    from utils import is_closed, is_telic
    today = g.now.date()
    ids = [str(m.get("id") or "") for m in g.visible
           if is_telic(m) and not is_closed(m) and not m.get("bound")
           and not _H.is_hold(m) and not str(m.get("superseded_by") or "").strip()
           and due_day(m, today) is None and not _waits_on_cue(m)]
    ids = [i for i in ids if i]
    if ids:
        c.add("没人认领的想要", "error",
              f"{len(ids)} 条还开着的想要没有 bound（谁该做），也没有日子或条件，"
              f"「惦记的事」里永远看不到它们：{'、'.join(ids)}",
              "给每条补上 bound：面板上改，或 trace(bucket_id=…, bound=[\"谁\"])")
    else:
        c.add("没人认领的想要", "ok", "开着的想要都有人认领，或有日子、有条件")


# ---- Quoted lines that name no source ----
def check_bare_quotes(c: _Checks, g: _Ground) -> None:
    # A wasQuotedFrom line holding only the host's bare id (m_0142) names no container:
    # its original cannot be fetched and a withdrawal cannot reach the entry through
    # it. A new write refuses such a line (tools/grow/_sources_check.check_sources); these
    # are the entries written before.
    from utils import WAS_QUOTED_FROM, read_prov
    ids = [str(m.get("id") or "") for m in g.visible
           if any(ln["rel"] == WAS_QUOTED_FROM and "#" not in ln["target"]
                  for ln in read_prov(m))]
    ids = [i for i in ids if i]
    if ids:
        shown = "、".join(ids[:_BARE_QUOTES_NAMED]) + (
            f" 等 {len(ids)} 条" if len(ids) > _BARE_QUOTES_NAMED else "")
        c.add("引原话追不到来源", "warn",
              f"{len(ids)} 条记忆有引原话的线只写了编号，没写系统和容器——"
              f"原话取不回，来源撤回也够不着它们：{shown}",
              "知道是哪段对话的，用 trace(bucket_id=…, sources_append=[{system, instance, "
              "container, id}]) 补上那条记录，编号对上的线会接过去")
    else:
        c.add("引原话追不到来源", "ok", "引原话的线都写全了来源")


# ---- Are the things that should be there still there ----
def check_profile(c: _Checks, g: _Ground) -> None:
    # The same gate the door uses (core.profile.door_note): a covered page keeps its
    # tag but is no longer the page.
    from . import _fold as _F
    tagged = [m for m in g.metas if _PROFILE_TAG in _tags_of(m)]
    profile = [m for m in tagged if not _F.is_covered(m)]
    if len(profile) == 1:
        c.add("门口那张纸", "ok", "名字页在，且只有一张")
    elif not profile and tagged:
        gone = tagged[0]
        c.add("门口那张纸", "error",
              f"名字页 {gone.get('id')} 被 {'、'.join(_F.covers_of(gone))} 换掉了，"
              "新版没带 tag —— 睁眼时档案那格是空的",
              f"给新版补上 tag {_PROFILE_TAG}（trace 的 tags 是整份替换，原来的一起写上）")
    elif not profile:
        c.add("门口那张纸", "note" if not g.visible else "warn",
              "还没有名字页 —— 睁眼时档案那格是空的"
              if not g.visible else "没有名字页 —— 睁眼时档案那格是空的",
              f"存一条带 tag {_PROFILE_TAG} 的记忆")
    else:
        c.add("门口那张纸", "error", f"有 {len(profile)} 张名字页，只该有一张", "合并掉多的")


def check_pinned(c: _Checks, g: _Ground) -> None:
    pinned = [m for m in g.visible if m.get("pinned")]
    # Nothing pinned means "nothing pinned yet", not "broken": principles grow one at a
    # time.
    c.add("钉着的准则", "ok" if pinned else "note",
          f"{len(pinned)} 条" if pinned else "一条都没钉 —— 睁眼时准则那格是空的")


def check_periods(c: _Checks, g: _Ground) -> None:
    # A period (a named stretch of days, fold(when=...)) is **optional by design** — use
    # one if you have one. Having none is not a fault, and reporting it as a warning tells
    # a fresh install "you are missing something".
    big = [m for m in g.metas if _BIGEVENT_TAG in _tags_of(m)]
    c.add("时期", "ok" if big else "note",
          f"{len(big)} 个" if big else "还没给哪段日子起过名（不强制，有就用）")


# ---- Chains that point at nothing ----
async def check_orphans(c: _Checks, g: _Ground) -> None:
    """A `from` that no longer resolves — **there are two kinds, and they are not the
    same thing.**

    Most such sources sit safely in the archive — `trace(delete=True)` is a soft delete
    and a direct id lookup always recovers it — and only a few are genuinely missing.
    One sentence for both ("most likely hard-deleted") would be wrong in the worst
    possible direction: describing something perfectly normal as "hard-deleted" sends
    someone hunting for an incident that never happened.
    """
    live_ids = {str(m.get("id") or "") for m in g.metas}
    try:
        allb = await g.bucket_mgr.list_all(include_archive=True)
        all_ids = {str((b.get("metadata") or {}).get("id") or "") for b in allb}
    except Exception:                            # noqa: BLE001
        all_ids = live_ids                       # if the archive cannot be read, fall back to the live ids
    sunk = gone = 0
    for m in g.metas:
        for src in read_from_ids(m):
            if src in live_ids:
                continue
            if src in all_ids:
                sunk += 1
            else:
                gone += 1
    if gone:
        c.add("断掉的 from 链", "warn",
              f"{gone} 条记忆的来源哪儿都找不到了",
              "这才是真断了：多半那条源被物理删过。星空里它们少一根线")
    elif not sunk:
        c.add("from 链", "ok", "每条 from 都指得到")
    if sunk:
        c.add("来源沉进归档区", "note",
              f"{sunk} 条记忆的来源已经归档 —— 没断，拿 id 直查捞得回",
              "" if gone else "")


# ---- Dreams on disk ----
def check_dreams(c: _Checks, g: _Ground) -> None:
    # **Empty is normal**: dreams are time-driven and disappear if left alone, and a backlog
    # that never reaches the threshold simply means a night without dreams. No
    # `except OSError: pass`, or a missing directory would make the whole check vanish.
    from . import _dream as _D
    n = len(_D.load_dreams())
    c.add("盘上的梦", "ok",
          f"{n} 个还在（时间到了自己会没）" if n else "空的（攒不到线就一夜无梦，正常）")


async def _read_buckets(c: _Checks, g: _Ground) -> None:
    """The base ingredient. A failure here must not take down the whole check; the
    independent items (config, disk) still run."""
    try:
        all_buckets = await g.bucket_mgr.list_all(include_archive=False)
        g.metas = [(b.get("metadata", {}) or {}) for b in all_buckets]
        # The count above **excludes** the archive, so "N entries including the archive"
        # needs the archive counted separately — or the label would not match the number.
        g.n_with_archive = len(await g.bucket_mgr.list_all(include_archive=True))
    except Exception as e:
        g.buckets_ok = False
        c.add("记忆库读取", "error", f"读不出记忆桶：{type(e).__name__}: {e}",
              "看容器日志 + buckets 目录挂载对不对")


def _sort_visible(c: _Checks, g: _Ground) -> None:
    bad_meta = 0
    for m in g.metas:
        # try per entry: one bad metadata record (a domain that is an integer, say) must not
        # silence the whole health check
        try:
            if on_timeline(m):
                g.visible.append(m)
        except Exception:
            bad_meta += 1
    if bad_meta:
        c.add("元数据形状", "error", f"{bad_meta} 条记忆的元数据读不动（字段类型不对）",
              "在「日志」里搜这几条的 id，多半是早期写入留下的")


async def health(bucket_mgr, config, persistence: Callable[[str], dict]) -> dict:
    """**Our own health check**: {ok, summary: {status: count}, checks: [rows]}.

    `config` is the configuration as handed in (possibly not a dict: a check reads around
    it); `persistence(buckets_dir)` says whether the data directory is on persistent
    storage ({persistent, note}).
    """
    c = _Checks()
    cfg = config if isinstance(config, dict) else {}
    g = _Ground(bucket_mgr=bucket_mgr, config=config, cfg=cfg,
                bd=str(cfg.get("buckets_dir") or ""), now=_w.now(),
                persistence=persistence)
    await _read_buckets(c, g)
    _sort_visible(c, g)

    await c.need_buckets("记忆总量", check_total, g)
    check_vectors(c, g)
    await c.guard("摘要/标签", check_summariser, g, "检查 config.yaml 的 dehydration 段")
    await c.guard("向量", check_embedding, g, "检查 config.yaml 的 embedding 段")
    await c.guard("换向量模型", check_reembed, g)
    await c.guard("字面搜索", check_literal, g)
    await c.guard("时区", check_tz, g)
    await c.guard("数据持久性", check_persist, g)
    await c.guard("磁盘", check_disk, g, f"确认 buckets_dir 存在：{g.bd or '(没配)'}")
    await c.guard("库的版本", check_schema, g)
    await c.need_buckets("最近七天", check_fresh, g)
    await c.need_buckets("没人认领的想要", check_unbound_wants, g)
    await c.need_buckets("引原话追不到来源", check_bare_quotes, g)
    await c.need_buckets("门口那张纸", check_profile, g)
    await c.need_buckets("钉着的准则", check_pinned, g)
    await c.need_buckets("时期", check_periods, g)
    await c.need_buckets("from 链", check_orphans, g)
    await c.guard("盘上的梦", check_dreams, g, "确认 buckets/night_fall/dreams 目录在")

    # Besides ok/warn/error the health check **has a fourth state, `note`** — "you have not
    #    started yet", "this one is optional": neutral statements, not problems.
    # The rule: **count whatever states actually appear**, never a hardcoded list — a
    #    hardcoded list would miss the same way again when a fifth state is added.
    summary = {"ok": 0, "warn": 0, "error": 0}
    for row in c.rows:
        st = str(row.get("status") or "").lower() or "unknown"
        summary[st] = summary.get(st, 0) + 1
    return {"ok": summary["error"] == 0, "summary": summary, "checks": c.rows}


# ============================================================
# The settings page's top block
# ============================================================

@dataclass
class PanelLock:
    """The panel lock as the web side reads it (web/panel_auth.py): whether the gate is
    locked, whether a password is set, the bridge's key when locked ("" otherwise), and
    the deployment's hosts table (None when it cannot be read)."""
    locked: bool = False
    has_password: bool = False
    hook_key: str = ""
    hosts: Any = None


class _Rows:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def row(self, key, label, ok, now, why, fix="", note=False):
        """note=True means **a fact that has to be stated clearly**, not "you configured
        this wrong".

        Why they are separated: without the distinction, "the panel has no lock" sits there
        forever reporting "1 item needs attention" — even where leaving it unlocked was a
        deliberate decision. A decision that raises an alarm every day is not an alarm any
        more, it is noise, and then the real alarms get ignored along with it.
        """
        self.rows.append({"key": key, "label": label, "ok": bool(ok), "note": bool(note),
                          "now": now, "why": why, "fix": fix})


def setup_summariser(r: _Rows, cfg: dict) -> None:
    dehy = cfg.get("dehydration", {}) or {}
    d_key = str(dehy.get("api_key") or "")
    d_model = str(dehy.get("model") or "")
    d_base = str(dehy.get("base_url") or "")
    r.row("dehydration", "打标模型", bool(d_key and d_model),
          (d_model + "（" + (d_base or "默认地址") + "）") if d_key and d_model else "没配",
          "存得进去，但没有标签、没有摘要、也抽不出人名 —— 而且一声不响。"
          "搜索靠标签和摘要，所以等于存了一堆搜不到的东西。")


async def setup_embedding(r: _Rows, cfg: dict, embedding_engine) -> None:
    """Search's second leg.

    🔴 Being configured and being usable are two different things, and this row checks
       both. On a fresh install the bundled compose file starts an Ollama container with
       **no model pulled**: everything comes up, every service reports healthy, a
       config-only check SHOWS A GREEN TICK, and the first real write is what fails.
       So when the configuration looks complete, ask the backend for one actual vector.
       Configuration is the one moment someone is prepared to hear about configuration.
    """
    emb = cfg.get("embedding", {}) or {}
    e_on = str(emb.get("enabled", "")).strip().lower() in ("1", "true", "yes", "on")
    e_key = str(emb.get("api_key") or "")
    e_model = str(emb.get("model") or "")
    e_base = str(emb.get("base_url") or "")
    e_ok = e_on and bool(e_model) and (bool(e_key) or "localhost" in e_base or "ollama" in e_base)
    e_detail = ""
    if e_ok:
        probe = getattr(embedding_engine, "probe", None)
        if callable(probe):
            try:
                works, why = await probe(timeout_seconds=3.0)
            except Exception as exc:                  # noqa: BLE001 - a screen must not die
                works, why = False, str(exc)
            if not works:
                e_ok = False
                e_detail = f"{e_model} 配好了，但用不了：{why}"
    r.row("embedding", "向量", e_ok,
          e_detail or ((e_model + "（" + (e_base or "默认地址") + "）") if e_ok else
                       ("开着但没配全" if e_on else "关着")),
          "搜索少一条腿：只剩字面匹配。换个说法搜同一件事就搜不到了 —— "
          "而它不会告诉你「这次没用上向量」。")


def setup_names(r: _Rows) -> None:
    ai_name = os.environ.get("AI_NAME", "").strip()
    owner = os.environ.get("LOCI_OWNER_NAME", "").strip()
    r.row("names", "你和他的名字", bool(ai_name and owner),
          ((ai_name or "没配") + " / " + (owner or "没配")),
          "这两个名字要挡在标签外面。没配的话，你俩的名字会被当成普通词抽进标签 —— "
          "而几乎每条记忆都有你们，于是这两个词淹掉整个搜索。",
          "改的是容器的环境变量 AI_NAME / LOCI_OWNER_NAME")


def setup_aliases(r: _Rows) -> str:
    """The alias table's row; returns its path for the facts below."""
    from . import names as subj
    apath = subj._alias_path()
    a_exists = os.path.isfile(apath)
    table = subj.load_alias_table()
    r.row("aliases", "别名表", a_exists,
          (apath + "（" + str(len(table)) + " 条写法）") if a_exists else ("还没有：" + apath),
          "同一个人的几种写法会被当成几个人。「老张」和「张三」各算一个，"
          "按人找记忆就永远只找到一半。",
          "面板「整理 → 人名表」上点一下就是往这张表里写")
    return apath


def setup_tz(r: _Rows) -> None:
    # Which timezone "today" is cut in. (The container's own TZ does not matter: stamps
    # are written as UTC by name, see utils.now_iso.)
    tz = _w.tz_status()
    r.row("tz", "时区", not tz["problem"],
          ("LOCI_TZ=" + tz["name"]) if not tz["problem"] else (tz["problem"] + " ⚠️"),
          "「今天」「昨天」「这周」都按这个时区切。设错或者没设、又不在 +8 的话，"
          "每天有几个小时的记忆会算到隔壁那天去，「今天存的东西今天翻不到」。",
          "在启动环境里设 LOCI_TZ，例如 Asia/Shanghai、America/Los_Angeles")


def setup_panel_lock(r: _Rows, cfg: dict, panel: PanelLock) -> None:
    """The panel lock (web/panel_auth.py) — /api/* has no other gate."""
    sw_on = str(cfg.get("panel_auth", True)).strip().lower() not in (
        "0", "false", "no", "off", "none", "")
    if panel.locked:
        r.row("panel_lock", "面板的锁", True, "锁着（要输密码）",
              "", note=True)
    elif sw_on and not panel.has_password:
        r.row("panel_lock", "面板的锁", False, "开关开着，但还没设密码 —— 所以现在没锁",
              "锁一个还没有钥匙的门只会把你自己关在外面，所以没密码的时候这道门不生效。"
              "去上面「账号」设一把密码，锁立刻就生效了。")
    else:
        # The switch was **explicitly turned off** -> that is a decision someone made, not a
        # problem. Still say what it means (anyone can read all of your memories), but do
        # not count it in "N items need attention".
        r.row("panel_lock", "面板的锁", False, "没有锁（你关掉的）",
              "任何能访问到这个地址的人都能看你全部记忆 —— 这台机器监听 0.0.0.0，"
              "同一个网里的设备都算。要锁上：上面「账号」里那个开关。",
              note=True)


def setup_hook_token(r: _Rows, panel: PanelLock) -> None:
    """🔴 The gap between two pieces of advice that are each correct on their own.

    Locking the panel is the recommended setup. Once it is locked, the four hook routes
    the bridge uses stop being exempt and need a key — and if that key was never set, the
    bridge starts getting 401s. Nobody does anything wrong to reach that state, and the
    symptom people actually notice is that dreams and nudges quietly stop arriving. So it
    is said on the screen that exists to answer "is this set up right".
    """
    if not panel.locked:
        return
    key = panel.hook_key
    r.row("hook_token", "桥的钥匙", bool(key),
          "配好了" if key else "面板锁着，但没配钥匙 —— 用桥的话它会被挡在门外",
          "" if key else
          "面板一上锁，桥走的那四条口就不再免检了。没有钥匙的话，"
          "梦和「该发呆了」会安静地不再送达 —— 桥那边收到的是 401，"
          "而你这边只会觉得它们不来了。"
          "设一个环境变量 LOCI_HOOK_TOKEN（随便一串够长的字），桥那边设同一个。")


def setup_hosts(r: _Rows, panel: PanelLock) -> None:
    """A hosts: table holding a host with a ceiling promises that host only its own
    material; the promise needs the panel locked and MCP auth on (panel_auth.lock_problem).
    Two hosts declaring one place at the same depth (core/scope.load_hosts): neither is
    taken there, which refuses quietly unless it is said here."""
    hs = panel.hosts
    if hs is not None and hs.ceilinged:
        r.row("hosts_lock", "带上限的宿主", not hs.unsafe,
              "面板锁着、MCP 鉴权开着" if not hs.unsafe else hs.unsafe,
              "hosts 表里有只许碰自己那份材料的宿主（max_grant）。面板没锁、或者 MCP 鉴权关着的时候，"
              "它——和任何能连上这个端口的人——绕过去就能读全库，所以这时候带上限的宿主一律被拒。",
              "" if not hs.unsafe else
              "在上面「账号」里设一把口令（panel_auth 别关），config 里 mcp_require_auth 别关")
    if hs is not None and getattr(hs, "collisions", ()):
        r.row("hosts_collision", "宿主表里撞车的地方", False,
              "；".join(hs.collisions),
              "hosts 表里两个宿主在同一处、同一深度都写了 authority / provides / registers。"
              "这一处谁都不算：那里的变化通知一律拒、原话取不到、行列表也登记不进来；"
              "两个宿主别处的东西照常。",
              "在 config 的 hosts 表里把这一处只留给一个宿主，或者让其中一个写得更深一层")


async def setup(config, bucket_mgr, embedding_engine, panel: PanelLock, in_docker) -> dict:
    """The screen at the top of the settings page: **make the silent things visible.**

    The rule comes from a single day on which every failure encountered was silent:
        no tagging key       -> memories save, but with no tags and no summary   no error
        no embedding         -> search loses one of its two legs                 no error
        no names configured  -> both names bleed into tags and drown search      no error
        no alias table       -> two spellings of one person stay two people      no error
        TZ set in container  -> new memories become "the future" and vanish      no error
    So the most important thing this screen does is not collect settings, it is **tell the
    reader what happens if they do not set them.**

    Every row carries three things: what it is, what state it is in now, and **what goes
       wrong if it is not right**. A newcomer does not know what "AI_NAME unset" means;
       "both of your names will bleed into the tags" they understand.
    This screen has two readers: a person, and **that person's AI** — they screenshot these
       rows into their own assistant. So it is written in plain language.

    -> {rows, bad (rows needing attention), facts (where things are)}. `in_docker` is
    passed through to the facts as given.
    """
    r = _Rows()
    cfg = config or {}
    setup_summariser(r, cfg)
    await setup_embedding(r, cfg, embedding_engine)
    setup_names(r)
    apath = setup_aliases(r)
    setup_tz(r)
    setup_panel_lock(r, cfg, panel)
    setup_hook_token(r, panel)
    setup_hosts(r, panel)

    # ---- Read-only facts: not "is this configured correctly", but "where things are" ----
    ver = ""
    try:
        vp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
        with open(vp, "r", encoding="utf-8") as f:
            ver = f.read().strip()
    except OSError:
        ver = ""
    # The count goes through the timeline gate — the same ruler recall, rooms and the
    # subjects screen use, so one thing does not carry two numbers.
    try:
        allb = await bucket_mgr.list_all(include_archive=False)
        n_buckets = sum(1 for b in allb if on_timeline(b.get("metadata", {}) or {}))
    except Exception:                                # noqa: BLE001
        n_buckets = -1
    return {
        "rows": r.rows,
        # Note rows do not count as "needs attention", or a decision already made would
        # raise an alarm every day.
        "bad": sum(1 for row in r.rows if not row["ok"] and not row["note"]),
        "facts": {
            "buckets_dir": str(cfg.get("buckets_dir") or os.environ.get("LOCI_BUCKETS_DIR") or ""),
            "log_file": os.environ.get("LOCI_LOG_FILE", ""),
            "alias_table": apath,
            "version": ver,
            "buckets": n_buckets,
            "in_docker": in_docker,
            "tz_display": os.environ.get("LOCI_TZ", "").strip() or "Asia/Shanghai",
        },
    }
