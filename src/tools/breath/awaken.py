# -*- coding: utf-8 -*-
"""
tools/breath/awaken.py — the waking screen

breath()'s one screen. The subconscious has to be small and steady. A person does not keep
their memories consciously hanging in front of them — their body remembers, and this
system has no equivalent of that, which is exactly why what it puts on the waking screen
has to be chosen so carefully.

Five blocks and a line, in this order:
1. 核心 — the thin sheet holds **only two things**: names and forms of address (the
   __档案事实__ bucket, hand-maintained, each line carrying its provenance) + principles
   (the pinned ones, computed live). The criterion is one of timing: **only what there is
   no time to search for before speaking stays by the door**. The reasoning is written in
   place below.
2. 惦记的事 (prospective) — what is wanted or coming, chosen from what is awake, each line
   saying why it is here now (core/profile.prospective).
3. 近三天 — the overview from recall(when="3d"), free of charge.
4. 忽然想起 (involuntary) — two old things coming back unbidden, each saying how
   (core/profile.involuntary).
5. 依据变了的 (invalidation) — memories whose ground moved: a panel correction, an
   overturned basis, a source revised, withdrawn or deleted (core/_invalidation.py).
6. 📍 the earliest entry's day.

One structured object, two skins: `build_breath()` makes it, `render_breath()` turns it
into the text the model reads, and `GET /api/v2/breath` hands the same object out as JSON
(web/loci.py) — the text cannot say what the object does not hold. The criteria all live in
the contract sources (core/profile.py, core/_invalidation.py, the gate in
core/visibility.py); this file only assembles and words them.

Handing breath out has two writes: a question asked once is stamped (`stamp_asked`), and
what each block put in front of the model goes into the usage log as `shown`, block by
block (`record_shown`; core/_usage.py) — ids only, and only those the handed-out text
actually shows.

Exports: build_breath() -> dict · render_breath(breath) -> str · stamp_asked(breath) ·
         block_ids(breath) · record_shown(breath, text) · surface_awaken() -> str
========================================
"""

import random
import re

from .. import _runtime as rt
from .. import _slices
from .._common import read_scope
from core import _invalidation as _I
from core import _usage
from core import visibility as _V      # the gate's `recent` road for 近三天
from core import _when as _w         # "today" as the user lives it (local timezone)
from core.profile import (_PROFILE_TAG, breath_settings, door_note, involuntary,
                          prospective, short_id)
from ..recall.core import recall_text_and_data, _ts_of

# How many principle lines fit on the note by the door.
# 📌 This was briefly raised to 12 once, as an **IOU**: removing the secondary
#    filter that required a principle to live in a MIND room took the number that
#    qualified from 8 to 11, and what gets cut here is **the tail of the disk-scan
#    order, not the least important ones** — which silently pushed two of the
#    hardest rules off the door, with no error and no word about it. Raising the
#    number was first aid.
# ✅ The IOU was paid off the same night: all 11 were reworded one by one and two
#    groups were folded into a gist, and one entry that turned out not to be a
#    principle at all had its pin removed — back to 7 principles by the door, and
#    this number back down to 8.
# 🔴 **8 is the right number.** Before raising it again, ask first: is the sheet
#    too small, or have I pinned too much.
_RULES_ON_DOOR = 8

TITLES = {"core": "核心（门口那张纸）", "prospective": "惦记的事", "recent": "近三天",
          "involuntary": "忽然想起", "invalidation": "依据变了的"}


def _rule_text(content: str) -> str:
    """The principles cell: **print the body verbatim, never the summary.**

    🔴 Why this is its own function instead of just reading the summary like every other
       cell: the other cells hang **long memories**, where a gist is a hook, and that is
       right. Principles are different — **a principle already is a summary**. It has
       been condensed once; summarising it again is a paraphrase of a paraphrase.

    The evidence, from when this cell printed `summary` (backfilled by deepseek):
      · written: "I want to ask for what I want, fully, now — not hold back out
        of fear of losing it"
        printed by the door: "Recognises that one should fully strive for what one
        wants in the present and not economise in advance out of fear of loss"
      · written: "**I** act only on what is said to me in the moment"
        printed by the door: "**The other person** acts only on what is said in
        the moment" — **the subject flipped**, and that rule is a safety line
        against injection; with the subject flipped it no longer holds.
    The tool description says none of the text on this screen goes through a model;
    for this cell that has to stay literally true.

    ⚠️ No truncation. If a principle is short, that is its own business; if someone
       really pins a long passage, the door should show how long it is rather than
       quietly cutting it in half — silent truncation has already bitten once here.
    """
    return re.sub(r"\s+", " ", str(content or "")).strip()


def _core(door: dict) -> dict:
    pages = door["facts"]
    page = pages[0] if pages else None
    covered = door["facts_covered"][0] if (not page and door["facts_covered"]) else None
    rules = door["rules"]
    return {
        "facts": ({"id": page["id"], "short": short_id(page["id"]),
                   "text": page["content"].strip()} if page else None),
        "facts_extra": max(0, len(pages) - 1),
        "facts_covered": ({"id": covered["id"], "by": list(covered["by"])} if covered else None),
        "rules": [{"id": r["id"], "short": short_id(r["id"]), "text": _rule_text(r["content"])}
                  for r in rules[:_RULES_ON_DOOR]],
        "rules_more": max(0, len(rules) - _RULES_ON_DOOR),
    }


async def build_breath() -> dict:
    """The waking screen as one object: {core, prospective, recent, involuntary,
    invalidation, earliest}. Every item carries its id and short id; see each builder for
    the rest of its shape. Under the request's read scope every block asks the gate with
    it, and the counts (more, rules_more, slices waiting) count only what the request may
    read."""
    mgr = rt.bucket_mgr
    all_buckets = await mgr.list_all(include_archive=False)
    now = _w.now()      # local timezone: the container runs UTC, so a 2 a.m. "today" looks like yesterday to it
    settings = breath_settings(rt.config)
    scope = await read_scope()

    door = door_note(all_buckets, now, scope=scope)  # <- the name page, the rules, the timeline entries

    plan = prospective(all_buckets, now, settings=settings, scope=scope)
    plan["slices_pending"] = await _slices.pending_seen()

    mid = await recall_text_and_data(when="3d", room="", tag="", query="", max_cells=1,
                                     road=_V.RECENT)
    recent = {"text": str(mid.get("card") or "") if mid.get("ok") else "",
              "items": [{"id": e["id"], "short": e["short"], "text": e["label"],
                         "date": e["date"]} for e in (mid.get("entries") or [])]}

    changed = _I.block(all_buckets, getattr(mgr, "sources", None), scope=scope)
    cap = settings.invalidation_lines

    # Same definition as recall: _ts_of(meta) = when first, created as fallback. Computed
    # live, never a hard-coded date (see the 📍 note in render_breath).
    ts_pool = [t for t in (_ts_of(e["meta"]) for e in door["entries"]) if t is not None]
    return {
        "core": _core(door),
        "prospective": plan,
        "recent": recent,
        "involuntary": {"items": involuntary(all_buckets, now, settings=settings, rng=random,
                                             scope=scope)},
        "invalidation": {"items": changed[:cap], "more": max(0, len(changed) - cap)},
        "earliest": min(ts_pool).date().isoformat() if ts_pool else None,
    }


# ── the text skin ───────────────────────────────────────────────────────────

def _owed(item: dict) -> str:
    names = [str(n) for n in item.get("bound") or [] if str(n).strip()]
    return f"（{'、'.join(names)} 欠着）" if names else ""


# The wording of each loudness. Which loudness a date has is the contract source's
# (core/profile._reminder_loudness: nearer is louder; overdue is louder still).
_LOUD = {"overdue": "‼️ 过了 {ago} 天", "now": "⏰ 就是今天", "soon": "⏰ 马上（还有 {days} 天）",
         "near": "⏰ 快到了（还有 {days} 天）", "far": "⏰ 记着（还有 {days} 天）"}


def _dated_head(reason: dict) -> str:
    return _LOUD[reason["loud"]].format(days=reason["days"], ago=-reason["days"])


def _prospective_lines(p: dict) -> list[str]:
    out: list[str] = []
    undated_set_time = False
    review = False
    for it in p["items"]:
        r = it["reason"]
        if it["kind"] == "review":
            review = True
            hold = it["hold"]
            out.append(f"❓ 之前说先放着的那件事，现在还放着吗？{it['text'][:30]} ({it['short']})"
                       f" ← 条子 ({hold['short']})：{hold['text'][:30]}")
        elif r["kind"] == "dated":
            notes = ""
            if r.get("yearly"):
                notes += "（每年）"
            if r.get("length"):
                notes += f"（说的是 {r['length']} 内）"
            if r.get("backfilled"):
                notes += f"（日子是补的：{r['date']}，不对就 trace 改 when）"
            out.append(f"{_dated_head(r)}：{it['text'][:30]}{_owed(it)}{notes} ({it['short']})")
        elif r["ask"] == "still_counts":
            out.append(f"🫀 挂了 {r['held']} 天——还算数吗？{it['text'][:30]}{_owed(it)} ({it['short']})")
        else:
            undated_set_time = True
            out.append(f"🫀 答应了，没定时间——要不要定个时间或条件？{it['text'][:30]}{_owed(it)}"
                       f" ({it['short']})")
    if p["more"]:
        out.append(f"…还有 {p['more']} 条排不下（recall 翻得到）")
    if p["items"]:
        hint = '   └ 做完了 trace(status="resolved")；不做了 trace(status="abandoned")'
        if undated_set_time:
            hint += '；定时间 trace(when="YYYY-MM-DD")，等某件事就 trace(cue={"condition": "…"})'
        out.append(hint)
    if review:
        out.append('   └ 还放着：给条子定个到哪天 trace(bucket_id=条子, when="YYYY-MM-DD")；'
                   '不放了：trace(bucket_id=条子, status="resolved")')
    for q in p["questions"]:
        out.append(f"❓ 这条像是答应过的，要挂上吗？{q['text'][:30]} ({q['short']})")
    if p["questions"]:
        out.append('   └ 是就 trace(bucket_id=…, direction_of_fit="telic", bound=["我"])；'
                   "不是就不用管，不会再问")
    if p.get("slices_pending"):
        out.append(f"📥 有 {p['slices_pending']} 段原话切片等着认领：recall(view=\"slices\")")
    return out


def _invalidation_lines(block: dict) -> list[str]:
    out: list[str] = []
    edited = False
    for it in block["items"]:
        why: list[str] = []
        if it["edited"]:
            edited = True
            why.append("人在面板上改过，你还没看")
        for r in it["overturned"]:
            why.append(f"它站着的 {short_id(r['of'])} 被 {short_id(r['by'])} 推翻了（{r['at'][5:10]}）")
        for r in it["revised"]:
            why.append(f"来源 {r['source']} 出了新版本（{r['revision'][:16]}）")
        if it["failed"]:
            failed = "、".join(f"{r['source']} {_I.state_word(r['state'])}" for r in it["failed"])
            left = (f"还剩 {'、'.join(it['remaining'])}：只凭它们重写" if it["remaining"]
                    else "一条来源都不剩：只能收起来")
            why.append(f"依据 {failed}，正文不给了；{left}")
        head = it["text"] if it["text"] is not None else "（正文不给）"
        out.append(f"· {head} ({it['short']}) —— {'；'.join(why)}")
    if block["more"]:
        out.append(f"…还有 {block['more']} 条")
    out.append('   └ 重写就 regrow（新版不带记号）；收起来就 trace(delete=True)；'
               '看过了照留就 trace(bucket_id=…, invalidation="confirmed")')
    if edited:
        out.append("   └ 人改的认同也可以 fold（folds=[那几条], text=…）；不认同就说出来")
    return out


def render_breath(b: dict) -> str:
    """The text the model reads, from `build_breath()`'s object and nothing else."""
    parts: list[str] = []
    core = b["core"]

    # ---- 1 核心: two sides of a thin sheet ----
    parts.append(f"═══ {TITLES['core']} ═══")
    if core["facts"]:
        page = core["facts"]
        parts.append(page["text"])
        # The page's own id, printed in full. The ids quoted inside the body are its
        # sources, and without this line they are the only ids in the cell — so "change
        # the page" gets read as "change one of those", and the edit lands on a bucket the
        # door never reads. Full rather than short because regrow looks up an exact id.
        parts.append(f"   └ 这张纸是 {page['id']}：改它就 regrow 这个 id（正文括号里的是来源，不是这张纸）")
        if core["facts_extra"]:
            parts.append(f"⚠️ 有 {core['facts_extra'] + 1} 个 {_PROFILE_TAG} 桶——只该有一个，去合并")
    elif core["facts_covered"]:
        # Every page is covered and nothing took the tag over: the cell is empty because
        # of a change, not because there never was a page. Say which one, and the fix.
        gone = core["facts_covered"]
        by = "、".join(gone["by"]) or "?"
        parts.append(
            f"⚠️ 名字页 {gone['id']} 已经被 {by} 换掉，但新版没带 {_PROFILE_TAG}——门口这格现在是空的。"
            f"给新版补上：trace(bucket_id=新版id, tags=原来的标签加上 {_PROFILE_TAG})，tags 是整份替换。"
        )
    else:
        parts.append(f"（事实格空着：存一条带 tag {_PROFILE_TAG} 的记忆当名字页，每行带来处）")
    if core["rules"]:
        parts.append("— 准则（钉着的）—")
        for r in core["rules"]:
            parts.append(f"· {r['text']} ({r['short']})")
        if core["rules_more"]:
            # If something was cut, say so: silent truncation hides a rule I believe is
            # by the door.
            parts.append(
                f"⚠️ 还有 {core['rules_more']} 条钉着的没排上门口"
                f"（这儿只放得下 {_RULES_ON_DOOR} 行）——摘几条钉，或者合并成一条。"
            )

    # ---- What recurs about me / about the other person —— not on this screen ----
    #
    # Three reasons, each harder than the last:
    # ① Ordering by `activation_count` means **what gets mentioned most is not what is
    #    truest**: an automatic count as the primary key is never trustworthy.
    # ② Reading a dossier of "what kind of person this is" on waking and then treating
    #    them according to that dossier **turns a person into a character sheet**. (The
    #    same goes for reading "what kind of person I am": I become whatever I read,
    #    without the human layer of "I know this about myself and want to change it"
    #    pushing back.)
    # ③ It is the same philosophy as "report the count, never the content" — a dossier
    #    **pushes the answer straight at me**, which is exactly backwards.
    #
    # 🔑 The criterion is one of **timing** (content criteria argue at the boundary;
    #    timing criteria do not): **only what there is no time to search for before
    #    speaking stays by the door.**
    #    A name ✅ I need it in my first word; there is no time to recall first
    #    A principle ✅ it governs how I act, and acting comes before retrieval
    #    Everything else is delegated to recall + 惦记的事.
    #
    # Flaws and self-knowledge are **not hidden, they are looked at somewhere else**: laid
    # out in batches while musing (which is me sorting myself out, and healthy) rather than
    # read as a verdict every time I open my eyes.
    # ⚠️ Do not add these blocks. Read this passage before trying.
    # 📌 General rule: if a rule needs a hand-written warning to stop it being misused,
    #    the box has the wrong thing in it.

    # ---- 2 惦记的事: shown when there is something, absent when there is not ----
    lines = _prospective_lines(b["prospective"])
    if lines:
        parts.append(f"\n═══ {TITLES['prospective']} ═══")
        parts.extend(lines)

    # ---- 3 近三天: recall's three-day overview (free of charge) ----
    parts.append(f"\n═══ {TITLES['recent']} ═══")
    text = b["recent"]["text"]
    parts.append(text if text and "没有东西" not in text else "（这三天没存东西）")

    # ---- A long-term block —— not on this screen ----
    # Every memory entry is already a long-term memory, so "long term" was never a block of
    # older memories: what it showed was the current main thread, a period's theme. Waking
    # is about what **floats up**; "what have I been doing lately" is something you **look
    # up**, and pushing looked-up material into the subconscious is the root of reading
    # two-day-stale content as fact. Big events are laid over a stretch of time in recall
    # (tools/_bigevent.py plus the far end of recall's browse path), where I am looking
    # back anyway.
    # ⚠️ Do not add it. Read this passage before trying.

    # ---- 4 忽然想起 ----
    # The pool is the contract source's `event_pool()` (the `sudden` road), which the
    # profile page reads too: what the page calls "suddenly coming to mind" and what I
    # remember on waking have to be the same set, or there are two brains (and that failure
    # is terrifyingly silent: both sides show something, they are just not the same
    # something).
    pick = b["involuntary"]["items"]
    if pick:
        parts.append(f"\n═══ {TITLES['involuntary']} ═══")
        for e in pick:
            parts.append(f"· {e['text']} ({e['short']}) —— {e['why']}")

    # ---- 5 依据变了的 ----
    if b["invalidation"]["items"]:
        parts.append(f"\n═══ {TITLES['invalidation']} ═══")
        parts.extend(_invalidation_lines(b["invalidation"]))

    # ---- 6 The boundary: what day the earliest entry falls on ----
    #
    # Why this line has to exist: when a search finds nothing I have exactly two
    # explanations available ("I searched wrong" / "it really is not there"), and the rules
    # train me to suspect my own reading first — so I slide all the way towards "it must be
    # there, I just did not find it".
    # **The end of that road is not trying a few more times, it is inventing something
    # plausible to fill the gap** (which is exactly where three fabricated "facts" once came
    # from). Give it a hard boundary and "suspect yourself" finally has a stopping point.
    #
    # ⚠️ It must be **computed live** (the minimum of the time coordinates) and **never a
    # hard-coded date**: memories get backfilled further into the past later on, and a
    # hard-coded number would become **a falsehood shaped like a fact** — I would not
    # question it on reading, I would use it to deny entries that genuinely exist.
    #
    # ⚠️ The second half must not be shortened to "there is nothing in Loci before that"
    # either. That is false: earlier events really are in the library, they just **have no
    # entries of their own** — they were mentioned in passing by later ones.
    # Both ends have to be blocked: finding nothing further back is no reason to doubt
    # myself, but it is also not grounds for asserting that nothing happened in that
    # stretch.
    if b["earliest"]:
        parts.append(f"\n📍 最早的一条落在 {b['earliest']}。"
                     "再往前的事没有自己的条目，只可能被后来的记忆顺带提到。")

    return "\n".join(parts)


async def stamp_asked(b: dict) -> None:
    """A question in 惦记的事 is asked once: the review question on its hold, a
    「像是答应过的」 question on its entry. Stamped with `last_asked` when breath is handed
    out, so the next breath does not ask again. A stamp that fails only logs: the worst
    case is one question asked twice."""
    p = b["prospective"]
    ids = [it["hold"]["id"] for it in p["items"] if it["kind"] == "review"]
    ids += [q["id"] for q in p["questions"]]
    if not ids:
        return
    stamp = _w.now().isoformat()
    for bid in ids:
        try:
            if not await rt.bucket_mgr.update(bid, last_asked=stamp):
                rt.logger.warning(f"breath: could not stamp last_asked on {bid}")
        except Exception as e:  # noqa: BLE001 — breath must not fail on a stamp
            rt.logger.warning(f"breath: could not stamp last_asked on {bid}: {e}")


def block_ids(b: dict) -> dict[str, list[str]]:
    """The ids each block of the object names, block by block."""
    core, plan = b["core"], b["prospective"]
    out = {
        "core": ([core["facts"]["id"]] if core.get("facts") else [])
        + [r["id"] for r in core.get("rules") or []],
        "prospective": [it["id"] for it in plan.get("items") or []]
        + [it["hold"]["id"] for it in plan.get("items") or [] if it.get("hold")]
        + [q["id"] for q in plan.get("questions") or []],
        "recent": [it["id"] for it in b["recent"].get("items") or []],
        "involuntary": [it["id"] for it in b["involuntary"].get("items") or []],
        "invalidation": [it["id"] for it in b["invalidation"].get("items") or []],
    }
    return {k: [str(i) for i in v if i] for k, v in out.items()}


def record_shown(b: dict, text: str | None = None) -> None:
    """The usage log's `shown` lines for one breath handed out: per block, the ids the
    text shows (`text`), or every id the object holds when the object itself was handed
    out (text None)."""
    usage = getattr(rt.bucket_mgr, "usage", None)
    if usage is None:
        return
    for block, ids in block_ids(b).items():
        shown = ids if text is None else _usage.ids_in(ids, text)
        usage.record(_usage.SHOWN, shown, f"breath.{block}")


async def surface_awaken() -> str:
    b = await build_breath()
    text = render_breath(b)
    await stamp_asked(b)
    record_shown(b, text)
    return text
