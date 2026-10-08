# -*- coding: utf-8 -*-
"""
tools/recall/_search.py — the searching path (with a query)

Searching is looking for one thing: ordered by relevance, the score is the point, hits
sharing their roots are one line, what falls under the relevance floor is reported in a
final line, and what top-k cut is said out loud. view="scene" clusters the hits by the
scene words they share instead.

tools/recall/core.py re-exports relevance_floor, search_rows_json, _how_mark and
_PROMISE_MARK.
"""

from core import profile as _P        # what counts as an open promise; days since written
from core import thresholds as _T
from core import visibility as _V     # the one gate: what may be put in front of the model
from core._slicer import _short_id    # the handle a read tool prints for an id
from core import _when as _w          # "today" as the user lives it (local timezone) — never call datetime.now() directly

from ._cells import _label_of
from ._collect import _SEARCH_TOPK
from ._words import is_human_tag, kind_badge


# The relevance floor: below it, most results are merely adjacent.
# It came out of a search for a term meaning "taking care of one's health" (in the
# sense of massage and herbal medicine) that dredged up a pile of entries about
# the body and intimacy: if there is no direct connection, better not to show
# anything and say there is no relevant memory.
# After scoring was cut to two dimensions (semantic 2.5 + bm25 1.5) the absolute
# values all shrank, so the line was measured again (six real queries):
#   queries with keyword/literal support: genuinely relevant 50-80, clean.
#   purely semantic short queries: genuinely relevant around 36, adjacent 31-33,
#   and the noise from the health query peaked at 34.2
#   —— a line at 35 separates the two exactly, but with a margin of only 1-2
#   points (cosine has a narrow dynamic range to begin with).
#   Better too few than too many: what falls below the line is reported in one
#   final line (how many, the highest score, the earliest entry) — visible, and
#   drillable.
# ⚠️ This is the **combined score** (0-100), not a cosine; `recall_meaning` (0.65)
# is the **cosine line for the 意思 mark**, a different thing. Entries with a literal
# hit are floored at max(score, line) and are never blocked by this.
# The line is `recall_floor` in core/thresholds, read at every call: the setting page
# edits it live; the LOCI_RELEVANCE_FLOOR environment variable is its default when
# config does not set it; the slider on the panel's recall page passes a floor for one
# request only.


def relevance_floor() -> float:
    """The relevance floor as it runs now (core/thresholds `recall_floor`)."""
    return _T.value(_T.RECALL_FLOOR)


def _topk_line(ledger: dict | None) -> str:
    """How many entries top-k cut — **whatever was blocked stays visible**. This
    line is the counterpart to tightening top-k.

    The rule behind it: more words in a query = an averaged vector = a poorer aim.
    So this line does not just report a number, it spells out the way forward:
    **use one or two core words, in the wording actually used at the time**.
    """
    n = int((ledger or {}).get("topk砍掉") or 0)
    if n <= 0:
        return ""
    k = int((ledger or {}).get("topk") or _SEARCH_TOPK)
    return (f"── 还有 {n} 条命中被 top-{k} 挡在外面（按相关度截的）——"
            "词多了向量就取平均，换一两个核心词、用当时的原话再搜一次")


def _eff_score(e: dict, floor: float) -> float:
    """The effective score: a literal hit becomes max(score, floor). This is a
    floor, not a bonus — it lifts the low ones and leaves the high ones alone.

    Its whole purpose is **not missing things**, not putting them first.
    """
    s = e.get("score") or 0.0
    return max(s, floor) if e.get("literal") else s


def _how_mark(e: dict) -> str:
    """How a search hit matched, said right after its score — the two sides the score is
    made of, never a third number:
      字面   the whole query appears in it as written
      意思   its vector is close to the query's (cosine at or above the vector line)
    Both when both hold. A hit with neither got over the line on some of the query's words
    (部分字面) or on a weaker closeness in meaning alone (意思)."""
    marks = [word for word, on in (("字面", e.get("literal")), ("意思", e.get("meaning"))) if on]
    if marks:
        return "+".join(marks)
    return "部分字面" if e.get("words") else "意思"


def _written(e: dict) -> str:
    """「N天前写的」 from `created` on the local calendar: an old line was true the day it
    was written, not necessarily now."""
    days = _P.written_days_ago(e["meta"], _w.now().date())
    if days is None:
        return ""
    return "今天写的" if days <= 0 else f"{days}天前写的"


_PROMISE_MARK = "⏳答应了还没关 · "


def _lead_of(key: frozenset, members: list[dict], floor: float) -> dict:
    """Which hit of one root's group the line shows: an open promise (the best-scoring, if
    several), else the root itself when it matched, else the best-scoring hit."""
    def best(es: list[dict]) -> dict:
        return max(es, key=lambda m: (_eff_score(m, floor), m["ts"]))
    promised = [m for m in members if _P.is_open_promise(m["meta"])]
    if promised:
        return best(promised)
    root = next(iter(key)) if len(key) == 1 else None
    return next((m for m in members if m["id"] == root), None) or best(members)


def _root_tail(lead: dict, key: frozenset, members: list[dict], listed: set[str],
               below: set[str], roots: dict) -> str:
    """What the line says about the rest of its root's group. Every other hit is named by
    its id (collapsing is for reading, not hiding); a root the search did not list says why
    (under the line, not in this listing, or in the archive)."""
    others = sorted((m for m in members if m is not lead and m["id"] not in key),
                    key=lambda m: m["ts"], reverse=True)
    ids = " · ".join(("⏳" if _P.is_open_promise(m["meta"]) else "") + _short_id(m["id"])
                     for m in others)
    if key == {lead["id"]}:
        return f"＋{len(others)} 条派生：{ids}" if others else ""
    refs = []
    for r in sorted(key):
        if r in listed:
            note = "（也搜中了）"
        elif r in below:
            note = "（在线下）"
        else:
            mark = _V.visible_for((roots.get(r) or {}).get("metadata") or {}, road=_V.READ).mark
            note = f" {mark}" if mark else "（这次没列）"
        refs.append(_short_id(r) + note)
    head = f"派生自 {refs[0]}" if len(refs) == 1 else f"派生自 {len(refs)} 个根：{'、'.join(refs)}"
    return head + (f"，同根另有 {len(others)} 条：{ids}" if others else "")


def _search_rows(hit: list[dict], floor: float) -> list[tuple[dict, frozenset, list[dict]]]:
    """The search view's lines: one per root (`_find_roots`), as (lead, roots, hits).

    Order: lines holding an open promise first, then the rest; newest first within each
    (by the date the line shows). Only what this search matched is ordered — a promise that
    did not match is not brought in."""
    groups: dict[frozenset, list[dict]] = {}
    for e in hit:
        groups.setdefault(e.get("roots") or frozenset({e["id"]}), []).append(e)
    rows = [(_lead_of(key, members, floor), key, members) for key, members in groups.items()]
    rows.sort(key=lambda r: (any(_P.is_open_promise(m["meta"]) for m in r[2]), r[0]["ts"]),
              reverse=True)
    return rows


def search_rows_json(entries: list[dict], floor: float) -> list[dict]:
    """The panel's lines of a recall, the same lines the text skin lists.

    With a query (the entries carry scores): `_search_rows` over the hits at or above
    `floor` — one line per root, open promises first, then newest — each line its lead
    with how it matched (`_how_mark`), its effective score, and the group's other hits
    (`others`: what 「+ N 条派生」 counts). What is under the line is not a line. Without a
    query: every entry is a line of its own, newest first."""
    def line(e: dict, others: list[dict]) -> dict:
        out = {"id": e["id"], "short": _short_id(e["id"]), "text": _label_of(e),
               "date": e["ts"].strftime("%Y-%m-%d"), "written_words": _written(e),
               "open_promise": _P.is_open_promise(e["meta"])}
        if e.get("score") is not None:
            out.update(score=round(_eff_score(e, floor), 2), how=_how_mark(e),
                       roots=sorted(e.get("roots") or ()))
        out["others"] = [{"id": m["id"], "short": _short_id(m["id"]),
                          "open_promise": _P.is_open_promise(m["meta"])} for m in others]
        return out

    if not any(e.get("score") is not None for e in entries):
        return [line(e, []) for e in reversed(entries)]
    hit = [e for e in entries if e.get("score") is not None and _eff_score(e, floor) >= floor]
    return [line(lead, sorted((m for m in members if m is not lead),
                              key=lambda m: m["ts"], reverse=True))
            for lead, _key, members in _search_rows(hit, floor)]


def _render_search(entries, gates, floor: float = None, ledger: dict | None = None) -> str:
    """Searching: I am looking for something and I know what. **This is the
    default view whenever there is a query.**

    Whatever clears the line is ordered by time (newest first) with its score
    attached, how it matched (`_how_mark`) and how long ago it was written. Hits that
    grew from the same root are one line (`_search_rows`), and lines holding an open
    promise go first.
    🔴 **This is the default view**, so "find that one thing" never takes a detour.
    The rule: **order by time plus score by default, and ask for scene clusters
    explicitly** (`view="scene"`). Supplying `when` never changes the shape of the
    view — that would be one parameter doing two jobs: `when` governs range only,
    and shape is decided by `view` alone.

    The score governs filtering only, never ordering: the results of "find one
    thing" only show how it got here when they are laid out
    along the timeline; sorting by relevance stirs July and August together.
    Time is a discount, not a gate: older entries stay out of the way by default
    through the decay discount, and when you really are looking for one and hit it
    accurately, it survives the discount and comes up anyway.

    Anything below the line is not listed — if there is no direct connection,
    better not to show it at all.
    But those entries **have not disappeared**: a final line carries how many
    there are, the highest score, and what the earliest one says — which
    incidentally answers "when did this start".
    """
    floor = relevance_floor() if floor is None else float(floor)
    hit = [e for e in entries if _eff_score(e, floor) >= floor]
    below = [e for e in entries if _eff_score(e, floor) < floor]
    top_below = max(((e.get("score") or 0.0) for e in below), default=0.0)

    if not hit:
        return (f"〔{gates}〕**没有相关的记忆。**\n"
                f"够到 {len(below)} 条，但最高才 {top_below:.1f} 分（线在 {floor:.0f}）——"
                "都只是沾边，不弹出来。\n"
                "真觉得该有：换当时说过的原话当 query（别造词），或者用 when/room 直接翻。")

    rows = _search_rows(hit, floor)
    listed = {e["id"] for e in hit}
    below_ids = {e["id"] for e in below}
    roots = (ledger or {}).get("roots") or {}
    all_roots = set().union(*(key for _lead, key, _m in rows))
    head = f"〔{gates}〕{len(hit)} 条"
    if all_roots != listed:
        # The same origin found several times is still one origin: say how many there are.
        head += f"，出自 {len(all_roots)} 个根（同一个根收成一行）"
    promised_first = any(_P.is_open_promise(m["meta"]) for _l, _k, ms in rows for m in ms)
    head += (" · " + ("答应了还没关的在最前，其余" if promised_first else "")
             + f"按时间 新→旧（线 {floor:.0f}，分数只管过滤）")
    lines = [head]
    for lead, key, members in rows:
        # The search path **never cuts the gist**: cut it and you cannot tell
        # whether this is the entry you were after, and telling is the entire
        # point of searching. The browse path still cuts — there the goal is an
        # impression, not content.
        # 🧠 = thinking; wearing no badge means it is something that happened (the
        # room code is gone).
        written = _written(lead)
        line = (f"{_eff_score(lead, floor):5.1f} {_how_mark(lead)}  "
                f"{_PROMISE_MARK if _P.is_open_promise(lead['meta']) else ''}"
                f"{kind_badge(lead['meta'])}{_label_of(lead)}"
                f"  ({_short_id(lead['id'])})  {lead['ts'].strftime('%m-%d')}"
                + (f" · {written}" if written else ""))
        tail = _root_tail(lead, key, members, listed, below_ids, roots)
        lines.append(line + (f" ▏{tail}" if tail else ""))
    if below:
        earliest = min(below, key=lambda x: x["ts"])
        lines.append(f"── 另有 {len(below)} 条在线下（最高 {top_below:.1f}，"
                     f"最早 {earliest['ts'].strftime('%m-%d')}：「{_label_of(earliest)[:40]}」）——"
                     "多半只是沾边，没列")
    lines.append(_topk_line(ledger))
    lines.append("（看原文、看收起来的派生：拿 id 搜；换个说法再搜：用当时的原话，别造词）")
    return chr(10).join(x for x in lines if x)


def _render_scene_clusters(entries, gates, floor: float = None, ledger: dict | None = None) -> str:
    """Recall as scenes: how this thing got from there to here.

    🔴 **It has to be asked for explicitly**: `recall(query=…, view="scene")`.
    As a default it would make the same query change shape depending on whether
    `when` was supplied — **one parameter doing two jobs**. The default is "time
    plus score" (find that one thing), and scene clusters are reserved for "how
    this thing got here".

    The structure a mind actually holds is not a flat list but **clusters with a
    main scene and subordinate ones** — a representative can carry sub-scenes
    hanging off it (for instance, learning to code -> ① the day of making a
    spreadsheet, talking about code ② talking with a chatbot at the airport about
    how to learn it systematically (sub-scene: asking it for a document from an
    aeroplane seat) ③ reading the notes on a laptop at a desk).
    What clusters grip is the scene anchors (which is all tags now hold): one
    cluster = the hits that share scene words.
    Clusters are ordered by their earliest entry ("how it got here" is told from
    the beginning); the representative inside a cluster is the highest-scoring one.
    """
    floor = relevance_floor() if floor is None else float(floor)
    hit = [e for e in entries if _eff_score(e, floor) >= floor]
    below = [e for e in entries if _eff_score(e, floor) < floor]
    top_below = max(((e.get("score") or 0.0) for e in below), default=0.0)
    if not hit:
        return (f"〔{gates}〕**没有相关的记忆。**\n"
                f"够到 {len(below)} 条，但最高才 {top_below:.1f} 分（线在 {floor:.0f}）——"
                "都只是沾边，不弹出来。\n"
                "真觉得该有：换当时说过的原话当 query（别造词），或者用 when/room 直接翻。")

    def _vis_tags(e) -> set[str]:
        # Clustering also grips plain-language scene words only: machine-voiced
        # tags (the `aspect:patterns` kind) will string completely unrelated
        # memories into a single false "scene"
        return {str(t) for t in (e["meta"].get("tags") or []) if is_human_tag(t)}

    # Greedy clustering: take the main scene in descending score order, and gather
    # whatever shares its scene words as subordinate scenes
    ranked = sorted(hit, key=lambda e: _eff_score(e, floor), reverse=True)
    unassigned = list(ranked)
    clusters: list[list[dict]] = []
    while unassigned:
        head = unassigned.pop(0)
        ht = _vis_tags(head)
        members = [head]
        if ht:
            rest = []
            for e in unassigned:
                if ht & _vis_tags(e):
                    members.append(e)
                else:
                    rest.append(e)
            unassigned = rest
        clusters.append(members)

    clusters.sort(key=lambda c: min(e["ts"] for e in c))  # how it got here: tell it from the beginning
    lines = [f"〔{gates}〕{len(hit)} 条 · {len(clusters)} 个画面 · 一路过来（线 {floor:.0f}）"]
    for c in clusters[:8]:
        rep = max(c, key=lambda e: _eff_score(e, floor))
        kids = sorted((e for e in c if e is not rep), key=lambda e: e["ts"])
        shared = set.intersection(*(_vis_tags(e) for e in c)) if len(c) > 1 else set()
        label = ("·".join(sorted(shared)[:2]) + " ") if shared else ""
        written = _written(rep)
        lines.append(f"■ {rep['ts'].strftime('%m-%d')} {label}"
                     f"{_eff_score(rep, floor):5.1f} {_how_mark(rep)}  "
                     f"{kind_badge(rep['meta'])}{_label_of(rep)}"
                     f"  ({_short_id(rep['id'])})" + (f" · {written}" if written else ""))
        for e in kids[:3]:
            written = _written(e)
            lines.append(f"   └ {e['ts'].strftime('%m-%d')}  {kind_badge(e['meta'])}"
                         f"{_label_of(e)[:56]}  ({_short_id(e['id'])})"
                         + (f" · {written}" if written else ""))
        if len(kids) > 3:
            lines.append(f"   └ …还有 {len(kids) - 3} 条同画面的")
    if len(clusters) > 8:
        n_rest = sum(len(c) for c in clusters[8:])
        lines.append(f"…还有 {len(clusters) - 8} 个画面（{n_rest} 条）——加 when 缩小段落再看")
    if below:
        earliest = min(below, key=lambda x: x["ts"])
        lines.append(f"── 另有 {len(below)} 条在线下（最高 {top_below:.1f}，"
                     f"最早 {earliest['ts'].strftime('%m-%d')}：「{_label_of(earliest)[:40]}」）")
    lines.append(_topk_line(ledger))
    lines.append("（要平铺的时间轴：去掉 view；看原文：拿 id 搜）")
    return chr(10).join(x for x in lines if x)
