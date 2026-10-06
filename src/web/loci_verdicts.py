"""
========================================
web/loci_verdicts.py — the write routes the user drives by hand on the panel
========================================

    POST /api/loci/want/resolve       -> close something that was wanted (trace status)
    POST /api/loci/want/asked         -> record that it was asked about (trace)
    POST /api/loci/event/correct      -> regrow: writes a NEW VERSION of a memory
    POST /api/loci/subjects/action    -> edits the alias table in the data volume

Every one goes through `_write_body` (the same-origin check).
========================================
"""

import re

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh
from ._guards import _write_body

logger = sh.logger


# ---------------------------------------------------------
# Three write endpoints the user drives by hand:
# 1. The close button — the user is the one who knows whether something is finished, and
#    should not have to wait to be asked.
# 2. The "asked about this" ping — the stamp is only applied at the moment the question
#    is actually shown to them.
# 3. Event correction — only an event can be corrected this way; mind has no such route
#    by design. The original text is left untouched, and a correction is stored as a new
#    entry pointing back through `from`. Nothing is ever really deleted.
# All three go through `_write_body` (the same-origin check) plus a direct in-process
# call to `trace_core` / `bucket_mgr`, following the similar/action precedent rather than
# going out through the MCP layer.
# ---------------------------------------------------------
async def api_loci_want_resolve(request: Request) -> Response:
    """The close button: one click sets status to resolved or abandoned and records that
    the user closed it.

    Open only to something wanted that is still open (telic, not closed) — closing is not
    an action that applies to anything else. A repeat click on something already closed is caught
    by trace's "no fields need changing" path, so it neither errors nor overwrites
    closed_by a second time.
    """
    from starlette.responses import JSONResponse
    try:
        body = await _write_body(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    bucket_id = str(body.get("id") or "").strip()
    new_status = str(body.get("status") or "").strip().lower()
    if not bucket_id:
        return JSONResponse({"error": "缺 id"}, status_code=400)
    if new_status not in ("resolved", "abandoned"):
        return JSONResponse(
            {"error": f'status 只能是 "resolved"（放下了）或 "abandoned"（不做了），收到：{new_status}'},
            status_code=400)

    target = await sh.bucket_mgr.get(bucket_id)
    if not target:
        return JSONResponse({"error": f"查无此桶：{bucket_id}"}, status_code=404)
    tmeta = target.get("metadata", {}) or {}
    from utils import is_closed, is_telic
    if not is_telic(tmeta) or is_closed(tmeta):
        return JSONResponse(
            {"error": "这条不是还开着的想要（telic 且没关），没有结案这个动作"},
            status_code=409)

    try:
        from tools.trace.core import trace_core
        # `closed_by` records that a PERSON closed this, rather than that I noticed it
        # myself — see the note in tools/trace/core.py. Nothing compares this value; it
        # is free text that exists to be read. It names no specific person, or every
        # install would write that name into its own data.
        msg = str(await trace_core(bucket_id=bucket_id, status=new_status, closed_by="user"))
        return JSONResponse({"ok": True, "id": bucket_id, "status": new_status, "msg": msg})
    except Exception as e:
        logger.warning(f"[loci] 结案失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_loci_want_asked(request: Request) -> Response:
    """The "last asked" stamp, applied once at the moment the panel **actually renders**
    the question line where the user can see it.

    This does not decide whether the entry is the longest-standing one — that is already
    computed as `heavy_question_id` in `/api/loci/profile`. This endpoint only applies
    the stamp, and trusts whoever calls it.
    """
    from starlette.responses import JSONResponse
    try:
        body = await _write_body(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    bucket_id = str(body.get("id") or "").strip()
    if not bucket_id:
        return JSONResponse({"error": "缺 id"}, status_code=400)
    target = await sh.bucket_mgr.get(bucket_id)
    if not target:
        return JSONResponse({"error": f"查无此桶：{bucket_id}"}, status_code=404)

    try:
        from tools.trace.core import trace_core
        await trace_core(bucket_id=bucket_id, mark_asked=True)
        fresh = await sh.bucket_mgr.get(bucket_id)
        last_asked = str((fresh or {}).get("metadata", {}).get("last_asked") or "")
        return JSONResponse({"ok": True, "id": bucket_id, "last_asked": last_asked})
    except Exception as e:
        logger.warning(f"[loci] 记「问过了」失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_loci_event_correct(request: Request) -> Response:
    """The user corrects an event: **the original text is left untouched**, and the
    correction is stored as a separate entry whose `from` points back at it.

    A user's edit does not become truth directly. What lands is **a new event**, carrying
    the `core.profile._EDITED_BY_USER_TAG` tag and `from=[old id]`, with the old bucket
    untouched. Whether to accept the correction is decided by folding it, and the folding
    hand always belongs to the model. The notification is that new bucket itself
    (`core.profile.edited_by_user()` scans for that same tag on entries not yet folded;
    see the comments there).

    mind has no such entry point — and that is not enforced merely by the front-end not
    drawing a button; it is hard-checked here as well. A realization is the model's own
    judgement: the user may disagree with it, but changing it has to be the model's own
    act.
    """
    from starlette.responses import JSONResponse
    try:
        body = await _write_body(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    old_id = str(body.get("id") or "").strip()
    new_text = str(body.get("text") or "")
    if not old_id:
        return JSONResponse({"error": "缺 id"}, status_code=400)
    if not new_text.strip():
        return JSONResponse({"error": "text 不能为空——改完之后的完整正文"}, status_code=400)

    old = await sh.bucket_mgr.get(old_id)
    if not old:
        return JSONResponse({"error": f"查无此桶（也可能已归档）：{old_id}"}, status_code=404)
    old_meta = old.get("metadata", {}) or {}

    from core._rooms import is_event_room, is_mind_room
    old_room = str(old_meta.get("room") or "")
    if is_mind_room(old_room):
        return JSONResponse(
            {"error": "mind 不能从这儿改——mind 是我的判断，你可以不同意，"
                      "但得由我自己改（跟我说，我认同了自己 regrow）"},
            status_code=403)
    if not is_event_room(old_room):
        return JSONResponse(
            {"error": f"这不是一条 event（room={old_room or '未分房'}），"
                      "改错这个动作只对 event 开放"},
            status_code=409)

    try:
        from tools.grow.rooms_path import grow_event
        from core.profile import _EDITED_BY_USER_TAG
        # v/a are inherited from the old bucket: this is a factual correction, not a new
        # emotional experience, so nobody should be made to re-score the coordinates.
        old_v = old_meta.get("valence", 0.5)
        old_a = old_meta.get("arousal", 0.3)
        msg = await grow_event(
            items=[{"room": old_room, "text": new_text, "v": old_v, "a": old_a}],
            from_ids=[old_id],
        )
        m = re.search(r"📝([0-9a-f]{12})", msg)
        if not m:
            return JSONResponse({"error": f"新桶落盘失败：{msg}"}, status_code=500)
        new_id = m.group(1)
        # Apply _EDITED_BY_USER_TAG by merging, not replacing — the same reason as
        # rooms_path._backfill_one: the background-filled tags may not have landed yet,
        # and trace(tags=...) replaces the whole list, which would wipe them out. So this
        # reads the new bucket's current tags, merges into them, and goes through
        # bucket_mgr.update rather than trace.
        fresh = await sh.bucket_mgr.get(new_id)
        cur_tags = [str(t) for t in ((fresh or {}).get("metadata", {}).get("tags") or [])]
        merged_tags = list(dict.fromkeys(cur_tags + [_EDITED_BY_USER_TAG]))
        await sh.bucket_mgr.update(new_id, tags=merged_tags)
        return JSONResponse({"ok": True, "old_id": old_id, "new_id": new_id, "msg": msg})
    except Exception as e:
        logger.warning(f"[loci] event 改错失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_loci_subjects_action(request: Request) -> Response:
    """The three actions on the subjects screen. **This is a write endpoint** — it writes
    buckets/aliases.yaml.

    Same principle as muse and fold: **the system lays things out, and which one to
    change is a human click.** So there is no automatic trigger path here, and no bulk
    "tidy all of this up for me" request is accepted: one call changes one name.

    The three actions reduce to two operations, because merging and renaming are the same
    thing — folding one name's entry into another (`_subjects.merge_names`: its key
    and aliases become the other's aliases, its links move, its entry goes):
      not_person  this is not a person -> record it on the blocklist so it is never
                  extracted again.
                  **Not one byte of the historical entries is touched.** Rewriting
                     historical metadata means writing into someone's memories, and
                     "this is what the model extracted at the time" is itself a fact. The
                     blocklist is enough, and it can be undone at any moment.
      merge       these two are one person -> fold `name` into `target`
      rename      give them a proper name -> the same, with `target` as the new canonical
                  name (created when the table does not have it)

    WARNING: all of it applies **going forward** only. Older entries keep their old names
       on disk; there is no migration script for this table. The panel screen merges them
       for display according to the table, which is why a row disappears the moment the
       click lands.
    """
    from starlette.responses import JSONResponse
    from tools import _subjects as subj
    try:
        body = await _write_body(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    action = str(body.get("action") or "").strip()
    name = body.get("name")
    target = body.get("target")
    try:
        if action == "not_person":
            changed = subj.mark_not_person(name)
            note = ("记下了，以后不再抽它（历史那几条没动）" if changed
                    else "它已经在黑名单里了")
        elif action in ("merge", "rename"):
            changed = subj.merge_names(name, target)
            note = ("写进别名表了 —— 只管以后，老条目盘上还是老名字" if changed
                    else "这条已经在表里了")
        else:
            return JSONResponse(
                {"error": "action 只有三个：not_person / merge / rename"},
                status_code=400)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"[loci] subjects/action 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"ok": True, "changed": bool(changed), "note": note})
