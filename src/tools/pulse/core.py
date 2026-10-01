"""
========================================
tools/pulse/core.py — pulse implementation
========================================

anchor is the "coordinate-frame bucket" idea introduced in iter 2.0: an existing
bucket is pinned as a reference point for identity or for a relationship. It
never surfaces on its own in a default breath, but it can still come back when
query/domain/emotion/importance_min match it. Hard ceiling of 24.

pulse sits here as well: it is the overview of system state plus the bucket
listing, it is called rarely, and putting it in one file costs nothing in
readability.

Key behaviour:
- anchor_set / anchor_release: call bucket_mgr.set_anchor and translate the
  result as-is
- pulse: aggregate stats + list_all, group by type (normal/feel),
  and show icon + domain + emotion + weight + tags line by line
- pulse also carries an "index drift" self-check: the ID set in embedding.db is
  reconciled against the ID set of buckets on disk, and if missing/orphan > 0 it
  warns at the top of the status block and points at the backfill / clean scripts

What this file deliberately does not do:
- anchor has no "create shortcut": you must hold() it first, and only pin it once
  it is clear that it really is a coordinate frame
- pulse never dehydrates: metadata only, to avoid the cost

Exports: anchor_set(bucket_id) / anchor_release(bucket_id) /
         pulse(include_archive) -> str
========================================
"""

from typing import Optional

from .. import _runtime as rt
from .._common import check_metadata_size






def _ago(seconds: float) -> str:
    if seconds < 0:
        return "刚刚"
    if seconds < 90:
        return f"{int(seconds)} 秒前"
    if seconds < 5400:
        return f"{int(seconds / 60)} 分钟前"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} 小时前"
    return f"{int(seconds / 86400)} 天前"


async def _working_section(all_buckets: list) -> str:
    """"Is it working" — a different question from "is it alive" above.

    🔴 Added after a gateway bug: the timeout was set to 5 seconds, so the thing
       **had never once worked since it went live**, and ran that way for days
       without anyone noticing. It did not crash, did not raise, and the logs
       looked no different — **it just quietly did nothing**.
    📌 The rule: **do not ask "is the engine running", ask "when did it last
       actually finish a piece of work".** The first only proves the process
       exists; the second is the evidence that it works. Something that has never
       succeeded and something that last succeeded three days ago are told apart
       at a glance in this section.

    ⚠️ Deliberately **no new bookkeeping mechanism**: everything is inferred from
       what is already on disk.
       One more ledger is one more place for "the ledger itself broke and nobody
       knew" — which is precisely the disease this section exists to treat.
    """
    import os
    import time
    from core import _when as _w

    lines = ["", "=== 它在不在工作（不是「还活着吗」，是「最近一次真的干成活」）==="]
    now_ts = time.time()

    # ── Tagging: how many are still untagged, and how long the oldest has hung ──
    # This number **only ever goes down** (each backfill removes one). If it stops
    # falling, or the oldest keeps getting older, the tagging path has stopped —
    # and when it stops it raises nothing; new memories simply keep empty tags.
    #
    # 🔴 **Count only what is supposed to be tagged.** A monitor that lies is worse
    #    than no monitor at all: this cell may only report what would have been
    #    tagged and has not been yet. Report something that cannot happen, and the
    #    reader's first instinct is to go fix a problem that does not exist.
    untagged, last_tagged = [], None
    for b in all_buckets:
        m = b.get("metadata", {}) or {}
        created = _w.parse_stamp(m.get("created"))
        if not str(m.get("summary") or "").strip():
            if created:
                untagged.append(created)
        elif created and (last_tagged is None or created > last_tagged):
            last_tagged = created
    if last_tagged:
        lines.append("打标：最近一条打上标的记忆，是 "
                     f"{_ago((_w.now() - last_tagged).total_seconds())}建的")
    else:
        lines.append("打标：⚠️ 一条打上标的记忆都没有 —— 它可能从来没成功过")
    if untagged:
        stuck_for = (_w.now() - min(untagged)).total_seconds()
        lines.append(f"　　还有 {len(untagged)} 条在排队，最老的那条 {_ago(stuck_for)}就建了"
                     + ("  ⚠️ 挂太久了，去看一眼打标那条路" if stuck_for > 3600 else ""))
    else:
        lines.append("　　没有排队的（每一条都打上标了）")

    # ── Vectors: embeddings.db's mtime stands in for "last real write" ────────
    buckets_dir = str((rt.config or {}).get("buckets_dir") or "")
    db = os.path.join(buckets_dir, "embeddings.db") if buckets_dir else ""
    if db and os.path.exists(db):
        lines.append(f"向量：最近一次写入 {_ago(now_ts - os.path.getmtime(db))}")
    else:
        lines.append("向量：⚠️ 找不到 embeddings.db —— 搜索会**安静地**退化成只认关键词")

    # ── Dreaming: it is entirely a background job and says nothing, which is
    # exactly why it needs this line most ──────────────────────────────────────
    dream_state = os.path.join(buckets_dir, "_state", "dream_state.json") if buckets_dir else ""
    if dream_state and os.path.exists(dream_state):
        lines.append(f"做梦：最近一次动 {_ago(now_ts - os.path.getmtime(dream_state))}")
    else:
        lines.append("做梦：还没织过（刚装的话正常，装了好几天还这样就不正常）")

    lines.append(f"衰减引擎：{'在跑' if rt.decay_engine.is_running else '⚠️ 停了'}")
    return "\n".join(lines)


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
        f"总占用: {stats['total_size_kb']:.1f} KB\n"
        f"衰减引擎: {'运行中' if rt.decay_engine.is_running else '已停止'}\n"
    )

    # --- Index/storage consistency check ---
    # A bucket file on disk whose embedding is missing -> breath's vector
    # retrieval will simply lose that bucket; an orphan embedding, conversely,
    # does not affect retrieval but takes up space. The moment the two sides stop
    # matching, pulse warns — so that whoever is reading, person or model, knows
    # immediately that the numbers not adding up is a real bug, not an illusion.
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

    # "Is it working" comes before the listing — it matters far more than the list.
    # If this section itself goes wrong it must not take the health check down
    # with it: **the health check is the thing whose job is telling the truth.**
    try:
        status += await _working_section(buckets) + "\n"
    except Exception as e:
        status += f"\n=== 它在不在工作 ===\n⚠️ 这一段自己算不出来了：{e}\n"
        rt.logger.warning(f"pulse liveness section failed: {e}")

    if not buckets:
        return status + "\n记忆库为空。"

    normal_lines: list[str] = []
    feel_lines: list[str] = []
    for b in buckets:
        meta = b.get("metadata", {})
        btype = meta.get("type")
        if meta.get("pinned") or meta.get("protected"):
            icon = "📌"
        elif btype == "permanent":
            icon = "📦"
        elif btype == "feel":
            icon = "🫧"
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
        else:
            normal_lines.append(line)

    sections = [status]
    if normal_lines:
        sections.append("=== 记忆列表 ===\n" + "\n".join(normal_lines))
    if feel_lines:
        sections.append(f"=== feel（{len(feel_lines)} 条）===\n" + "\n".join(feel_lines))
    return "\n\n".join(sections)
