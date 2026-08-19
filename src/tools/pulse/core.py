"""
========================================
tools/pulse/core.py — pulse 实现
========================================

anchor 是 iter 2.0 引入的「坐标系桶」概念：把某条已经存在的桶钉为
我们关系/身份的基准点。它不会主动浮现在默认 breath，但 query/domain/
emotion/importance_min 命中时仍能返回。硬上限 24 个。

pulse 顺带放在这里：它是系统状态 + 桶清单的总览，调用频次低，把它
塞进一个文件不影响阅读。

关键行为：
- anchor_set / anchor_release：调 bucket_mgr.set_anchor，原样转译结果
- pulse：聚合 stats + list_all，按 type 分组（normal/feel/plan/letter）
  逐行展示 icon + 主题 + 情感 + 权重 + 标签
- pulse 同时附带「索引漂移」自检：embedding.db 的 ID 集合与磁盘桶 ID 集合
  对账，缺失/孤儿 > 0 时在状态块顶部告警，提示运行 backfill / clean 脚本

不做什么（边界）：
- anchor 没有「创建快捷键」：必须先 hold() 写下，确认是坐标系再钉
- pulse 不做 dehydrate：只读元数据，避免大开销

对外暴露：anchor_set(bucket_id) / anchor_release(bucket_id) /
         pulse(include_archive) → str
========================================
"""

from typing import Optional

from .. import _runtime as rt
from .._common import check_metadata_size






def _多久以前(秒: float) -> str:
    if 秒 < 0:
        return "刚刚"
    if 秒 < 90:
        return f"{int(秒)} 秒前"
    if 秒 < 5400:
        return f"{int(秒 / 60)} 分钟前"
    if 秒 < 172800:
        return f"{秒 / 3600:.1f} 小时前"
    return f"{int(秒 / 86400)} 天前"


async def _在不在工作(all_buckets: list) -> str:
    """「它在不在工作」—— 跟上面那段「它还活着吗」不是同一个问题。

    🔴 2026-08-20 加的，起因是网关那个 bug：超时设成 5 秒，于是它**从上线起一次
       都没工作过**，活了好几天没人发现。它没崩、没报错、日志里也看不出区别 ——
       **它只是安静地什么都不做**。
    📌 判据：**别问「引擎在不在跑」，问「它最近一次真的干成活是什么时候」。**
       前者只证明进程还在，后者才是它在工作的证据。一个从来没成功过的东西，
       和一个上次成功在三天前的东西，在这一段里一眼就分得出来。

    ⚠️ 故意**不新建一套记账机制**：全部从盘上已有的东西推出来。
       多一套账就多一个「账本自己坏了而没人知道」的地方 —— 那正是这一段要治的病。
    """
    import os
    import time
    from core import _when as _w

    行 = ["", "=== 它在不在工作（不是「还活着吗」，是「最近一次真的干成活」）==="]
    现在 = time.time()

    # ── 打标：没打上标的还剩几条、最老那条挂了多久 ──────────────────────
    # 这个数**只会往下走**（回填一条少一条）。它一直不降、或者最老那条越挂越久，
    # 就是打标那条路停了 —— 而它停了不报错，只是新记忆的标签一直空着。
    没标, 最近一次打标 = [], None
    for b in all_buckets:
        m = b.get("metadata", {}) or {}
        建 = _w.parse_stamp(m.get("created"))
        if not str(m.get("summary") or "").strip():
            if 建:
                没标.append(建)
        elif 建 and (最近一次打标 is None or 建 > 最近一次打标):
            最近一次打标 = 建
    if 最近一次打标:
        行.append("打标：最近一条打上标的记忆，是 "
                  f"{_多久以前((_w.now() - 最近一次打标).total_seconds())}建的")
    else:
        行.append("打标：⚠️ 一条打上标的记忆都没有 —— 它可能从来没成功过")
    if 没标:
        挂了 = (_w.now() - min(没标)).total_seconds()
        行.append(f"　　还有 {len(没标)} 条在排队，最老的那条 {_多久以前(挂了)}就建了"
                  + ("  ⚠️ 挂太久了，去看一眼打标那条路" if 挂了 > 3600 else ""))
    else:
        行.append("　　没有排队的（每一条都打上标了）")

    # ── 向量：拿 embeddings.db 的 mtime 当「最近一次真的写进去」──────────
    库 = str((rt.config or {}).get("buckets_dir") or "")
    db = os.path.join(库, "embeddings.db") if 库 else ""
    if db and os.path.exists(db):
        行.append(f"向量：最近一次写入 {_多久以前(现在 - os.path.getmtime(db))}")
    else:
        行.append("向量：⚠️ 找不到 embeddings.db —— 搜索会**安静地**退化成只认关键词")

    # ── 做梦：它整个是后台活、一声不吭，所以最需要这一行 ────────────────
    梦 = os.path.join(库, "_state", "dream_state.json") if 库 else ""
    if 梦 and os.path.exists(梦):
        行.append(f"做梦：最近一次动 {_多久以前(现在 - os.path.getmtime(梦))}")
    else:
        行.append("做梦：还没织过（刚装的话正常，装了好几天还这样就不正常）")

    行.append(f"衰减引擎：{'在跑' if rt.decay_engine.is_running else '⚠️ 停了'}")
    return "\n".join(行)


async def pulse(include_archive: Optional[bool] = False) -> str:
    if include_archive is None:
        include_archive = False
    await rt.decay_engine.ensure_started()
    try:
        stats = await rt.bucket_mgr.get_stats()
    except Exception as e:
        return f"获取系统状态失败: {e}"

    status = (
        f"=== 我现在的记忆 ===\n"
        f"固化桶: {stats['permanent_count']} 个\n"
        f"动态桶: {stats['dynamic_count']} 个\n"
        f"归档桶: {stats['archive_count']} 个\n"
        f"feel 桶: {stats.get('feel_count', 0)} 条\n"
        f"plan 桶: {stats.get('plan_count', 0)} 条\n"
        f"letter 桶: {stats.get('letter_count', 0)} 封\n"
        f"总占用: {stats['total_size_kb']:.1f} KB\n"
        f"衰减引擎: {'运行中' if rt.decay_engine.is_running else '已停止'}\n"
    )

    # --- 索引/存储一致性检查（iter 2.1+）---
    # 桶文件落在磁盘但 embedding 缺失 → breath 走向量检索时会丢这些桶；
    # 反之孤儿 embedding 不影响检索，但占空间。两边一旦对不上就在 pulse 里告警，
    # 让她/他/模型立刻知道「数对不上是真 bug」而不是错觉。
    try:
        ee = getattr(rt, "embedding_engine", None)
        outbox = getattr(rt.bucket_mgr, "embedding_outbox", None)
        pending_ids = outbox.pending_ids() if outbox is not None else set()
        if outbox is not None:
            queue_state = outbox.status()
            circuit = queue_state.get("circuit") or {}
            status += (
                f"向量索引队列: 待处理 {queue_state['pending']} 个"
                f"（重试中 {queue_state['retrying']} 个）"
                + (
                    f"，供应商熔断中（连续失败 "
                    f"{circuit.get('consecutive_failures', 0)} 次）"
                    if circuit.get("state") == "open" else ""
                )
                + "\n"
            )
        if ee and getattr(ee, "enabled", False):
            disk_buckets = await rt.bucket_mgr.list_all(include_archive=True)
            disk_ids = {
                b["id"] for b in disk_buckets
                if not (b.get("metadata") or {}).get("deleted_at")
                and str(b.get("content") or "").strip()
            }
            index_ids = set(ee.list_all_ids())
            missing = disk_ids - index_ids - pending_ids
            orphan = index_ids - disk_ids
            if missing or orphan:
                status += (
                    f"⚠️ 索引漂移：缺失 embedding {len(missing)} 个 / "
                    f"孤儿 embedding {len(orphan)} 个 "
                    f"（缺失项可在 Dashboard 触发补齐；孤儿项可运行 "
                    f"tools/clean_orphan_embeddings.py 清理）\n"
                )
    except Exception as e:
        rt.logger.warning(f"pulse index/storage drift check failed: {e}")

    try:
        buckets = await rt.bucket_mgr.list_all(include_archive=include_archive)
    except Exception as e:
        return status + f"\n列出记忆桶失败: {e}"

    # 「它在不在工作」挂在清单之前 —— 它比清单要紧得多。
    # 这一段自己出岔子也不许把体检带崩：**体检正是那个负责说实话的东西。**
    try:
        status += await _在不在工作(buckets) + "\n"
    except Exception as e:
        status += f"\n=== 它在不在工作 ===\n⚠️ 这一段自己算不出来了：{e}\n"
        rt.logger.warning(f"pulse liveness section failed: {e}")

    if not buckets:
        return status + "\n记忆库为空。"

    normal_lines: list[str] = []
    feel_lines: list[str] = []
    plan_lines: list[str] = []
    letter_lines: list[str] = []
    for b in buckets:
        meta = b.get("metadata", {})
        btype = meta.get("type")
        if meta.get("pinned") or meta.get("protected"):
            icon = "📌"
        elif btype == "permanent":
            icon = "📦"
        elif btype == "feel":
            icon = "🫧"
        elif btype == "plan":
            icon = "📋"
        elif btype == "letter":
            icon = "💌"
        elif btype == "archived":
            icon = "🗄️"
        elif meta.get("resolved", False):
            icon = "✅"
        else:
            icon = "💭"
        try:
            score = rt.decay_engine.calculate_score(meta)
        except Exception:
            score = 0.0
        domains = ",".join(meta.get("domain", []))
        val = float(meta.get("valence") or 0.5)
        aro = float(meta.get("arousal") or 0.3)
        resolved_tag = " [已解决]" if meta.get("resolved", False) else ""
        name = meta.get("name", "") or ""
        name_tag = f" 《{name}》" if name and name != b["id"] else ""
        line = (
            f"{icon} [{b['id']}]{name_tag}{resolved_tag} "
            f"主题:{domains or '未分类'} "
            f"情感:V{val:.1f}/A{aro:.1f} "
            f"重要:{meta.get('importance', '?')} "
            f"权重:{score:.2f}"
        )
        tags = [t for t in (meta.get("tags", []) or []) if not (t.startswith("__") and t.endswith("__"))]
        if tags:
            line += f" 标签:{','.join(tags)}"
        if btype == "feel":
            feel_lines.append(line)
        elif btype == "plan":
            plan_status = meta.get("status", "active")
            plan_lines.append(line + f" [{plan_status}]")
        elif btype == "letter":
            author = meta.get("author", "?")
            letter_lines.append(line + f" [{author}]")
        else:
            normal_lines.append(line)

    sections = [status]
    if normal_lines:
        sections.append("=== 记忆列表 ===\n" + "\n".join(normal_lines))
    if plan_lines:
        sections.append(f"=== 计划（{len(plan_lines)} 条）===\n" + "\n".join(plan_lines))
    if feel_lines:
        sections.append(f"=== feel（{len(feel_lines)} 条）===\n" + "\n".join(feel_lines))
    if letter_lines:
        sections.append(f"=== 信件（{len(letter_lines)} 封）===\n" + "\n".join(letter_lines))
    return "\n\n".join(sections)
