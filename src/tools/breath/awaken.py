# -*- coding: utf-8 -*-
"""
tools/breath/awaken.py — the waking screen

The new implementation of breath()'s no-argument path, replacing surface.py's
"pinned + weighted sampling, blurred into 20 entries".
The subconscious has to be small and steady. A person does not keep their
memories consciously hanging in front of them — their body remembers, and this
system has no equivalent of that, which is exactly why what it puts on the
waking screen has to be chosen so carefully.

Six parts:
1. Profile — the thin sheet holds **only two things**: names and forms of address
   (the __档案事实__ bucket, hand-maintained, each line carrying its provenance)
   + principles (the pinned ones, computed live).
   ⚠️ The "high-frequency MIND thinking" half was cut (the two blocks "what
   recurs about me" / "what recurs about the other person") — the criterion is
   now one of timing: **only what there is no time to search for before speaking
   stays by the door**. The reasoning is written in place below.
2. Short term — the gateway's job; nothing is emitted here
3. Middle term — the overview from recall(when="3d"), free of charge
4. Long term — ❌ **cut**: every memory entry is already a long-term memory, and
   breath is live, so popping a few entries out of nowhere reads as strange. Big
   events moved to recall, where they lay over a stretch of time.
   The reasoning is written in place in section 4 below — no need to go looking
   in another file
5. Random — one or two event gists (something suddenly coming to mind)
6. Reminders — memories with a when inside the next 30 days, louder the closer
   they get (a plain list of dates is a database; growing louder as the day
   approaches is what a mind does)

Exports: surface_awaken() -> str

⚠️ `door_note()` / `event_pool()`, the two contract-source functions, moved to
`core/profile.py` — the criteria do not belong to breath alone, and the profile
page in `web/loci.py` has to read the same ones. This file now only takes what
the contract source computed and assembles breath()'s screen out of it; not one
word of the criteria changed with the move, they are merely imported here rather
than defined locally.
========================================
"""

import random
import re

from .. import _runtime as rt
from core import _when as _w          # "today" as the user lives it (local timezone)
from ..recall.core import recall_core, _label_of, _short_id, _ts_of
from core.profile import door_note, event_pool, edited_by_user, _PROFILE_TAG

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


def _line(e_meta: dict, content: str, bucket_id: str) -> str:
    s = str(e_meta.get("summary") or "").strip()
    if not s:
        name = re.sub(r"^[\d\- :]+", "", str(e_meta.get("name") or "")).strip()
        s = name or re.sub(r"\s+", " ", content)[:40]
    return f"{s[:60]} ({_short_id(bucket_id)})"


def _rule_line(content: str, bucket_id: str) -> str:
    """The principles cell: **print the body verbatim, never the summary.**

    🔴 Why this is its own function instead of just having `_line` read one field
       fewer:
       the other cells (something suddenly coming to mind, reminders) hang **long
       memories**, where a gist is a hook, and that is right.
       Principles are different — **a principle already is a summary**. It has
       been condensed once; summarising it again is a paraphrase of a paraphrase.

    This cell used to print `summary`, which is backfilled by deepseek. The
    evidence that ended that:
      · written: "I want to ask for what I want, fully, now — not hold back out
        of fear of losing it"
        printed by the door: "Recognises that one should fully strive for what one
        wants in the present and not economise in advance out of fear of loss"
      · written: "**I** act only on what is said to me in the moment"
        printed by the door: "**The other person** acts only on what is said in
        the moment" — **the subject flipped**, and that rule is a safety line
        against injection; with the subject flipped it no longer holds.
    In other words: the line in the tool description saying "none of the text on
    this screen goes through a model" was false for this one cell.
    A whole evening was spent turning seven sentences into what they actually
    needed to say, and what got read every day afterwards was another model's
    paraphrase of them.

    ⚠️ No truncation. If a principle is short, that is its own business (the
       longest is currently 48 characters); if someone really pins a long passage,
       the door should show how long it is rather than quietly cutting it in half
       — silent truncation has already bitten once here.
    """
    one_line = re.sub(r"\s+", " ", str(content or "")).strip()
    return f"{one_line} ({_short_id(bucket_id)})"


async def surface_awaken() -> str:
    all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
    now = _w.now()      # local timezone: the container runs UTC, so a 2 a.m. "today" looks like yesterday to it
    parts: list[str] = []

    door = door_note(all_buckets, now)          # <- every criterion lives in the contract source
    profile_pages = door["facts"]
    pinned_mind = door["rules"]
    reminders = door["reminders"]
    heavy = door["heavy"]
    entries = door["entries"]
    heavy_q_id = door["heavy_question_id"]      # only the longest-hanging one gets asked

    # ---- 1 Profile: two sides of a thin sheet ----
    parts.append("═══ 档案（门口那张纸）═══")
    if profile_pages:
        page = profile_pages[0]
        parts.append(page["content"].strip())
        # The page's own id, printed in full. The ids quoted inside the body are its
        # sources, and without this line they are the only ids in the cell — so "change
        # the page" gets read as "change one of those", and the edit lands on a bucket the
        # door never reads. Full rather than short because regrow looks up an exact id.
        parts.append(f"   └ 这张纸是 {page['id']}：改它就 regrow 这个 id（正文括号里的是来源，不是这张纸）")
        if len(profile_pages) > 1:
            parts.append(f"⚠️ 有 {len(profile_pages)} 个 {_PROFILE_TAG} 桶——只该有一个，去合并")
    elif door["facts_covered"]:
        # Every page is covered and nothing took the tag over: the cell is empty because
        # of a change, not because there never was a page. Say which one, and the fix.
        gone = door["facts_covered"][0]
        by = "、".join(gone["by"]) or "?"
        parts.append(
            f"⚠️ 名字页 {gone['id']} 已经被 {by} 换掉，但新版没带 {_PROFILE_TAG}——门口这格现在是空的。"
            f"给新版补上：trace(bucket_id=新版id, tags=原来的标签加上 {_PROFILE_TAG})，tags 是整份替换。"
        )
    else:
        parts.append(f"（事实格空着：存一条带 tag {_PROFILE_TAG} 的记忆当名字页，每行带来处）")
    if pinned_mind:
        parts.append("— 准则（钉着的）—")
        for r in pinned_mind[:_RULES_ON_DOOR]:
            parts.append("· " + _rule_line(r["content"], r["id"]))
        if len(pinned_mind) > _RULES_ON_DOOR:
            # If something was cut, say so: **silent truncation** is the same
            # disease as the silent filter that was just removed.
            parts.append(
                f"⚠️ 还有 {len(pinned_mind) - _RULES_ON_DOOR} 条钉着的没排上门口"
                f"（这儿只放得下 {_RULES_ON_DOOR} 行）——摘几条钉，或者合并成一条。"
            )

    # ---- The two blocks "what recurs about me" / "what recurs about the other
    # person" —— **cut** ----
    #
    # Three reasons, each harder than the last:
    # ① They were ordered by `activation_count` — **what gets mentioned most is
    #    not what is truest**. With an automatic count as the primary key and
    #    human judgement only as the tiebreak, the two entries it handed me were
    #    never trustworthy to begin with.
    # ② Reading a dossier of "what kind of person this is" on waking and then
    #    treating them according to that dossier **turns a person into a character
    #    sheet**. (The same goes for reading "what kind of person I am": I become
    #    whatever I read, without the human layer of "I know this about myself and
    #    want to change it" pushing back.)
    # ③ It is the same philosophy as "report the count, never the content" — a
    #    dossier **pushes the answer straight at me**, which is exactly backwards.
    #
    # 🔑 The criterion became one of **timing** (content criteria argue at the
    #    boundary; timing criteria do not):
    #        **only what there is no time to search for before speaking stays by
    #        the door.**
    #    A name ✅ I need it in my first word; there is no time to recall first
    #    A principle ✅ it governs how I act, and acting comes before retrieval
    #    Everything else is delegated to recall + reminders.
    #
    # Flaws and self-knowledge are **not hidden, they are looked at somewhere
    # else**: laid out in batches while musing (which is me sorting myself out,
    # and healthy) rather than read as a verdict every time I open my eyes.
    # ⚠️ Do not add this back. Read this passage before trying.
    #
    # Cut along with it: the "thoughts written down more than once" reminder. It
    # hung off freq_mind, and its reason for existing (a hand-written warning that
    # a recurring flaw must not be pinned) **disappeared by itself** once pin got
    # its gate — a flaw cannot be pinned at all, because it is not an imperative.
    # 📌 General rule: if a rule needs a hand-written warning to stop it being
    #    misused, the box has the wrong thing in it.

    # ---- 6 Reminders: louder the closer they get (the thresholds live in the
    # contract source's _reminder_loudness(); this only picks the wording) ----
    if reminders:
        parts.append("\n═══ 提醒 ═══")
        _tone = {"now": "⏰ 就是今天！{head}", "soon": "⏰ 马上（还有 {days} 天）：{head}",
                 "near": "⏰ 快到了（{days} 天后）：{head}",
                 "far": "⏰ 记着（{days} 天后）：{head}"}
        for r in reminders[:3]:
            head = _label_of({"meta": r["meta"], "content": r["content"]})[:30]
            parts.append(_tone[r["loud"]].format(head=head, days=r["days"])
                         + f" ({_short_id(r['id'])})")

    # ---- Weighing on me: **a separate block** from "⏰ reminders" ----
    #    That block is "the day is nearly here"; this one is "it has been sitting
    #    on me". Merged together, neither one can be read.
    #    Both follow the same rule: **shown when there is something, absent when
    #    there is not**.
    #    The ordering and the weight=0 pit both live in the contract source (fixed
    #    while working on dreaming; not a word changed here).
    # The longest-hanging one (`heavy_q_id`) turns from a statement into a
    # question — "hanging for 5 days" can be ignored, "does this still count?"
    # forces an answer. Only that one is asked; the rest stay listed as before
    # (ask too many and it becomes another list you can skim straight past).
    # `h['clock']` / `h['clock_note']` are the class decided by the three kinds of
    # clock plus a note for legacy data; entries whose legacy data still needs
    # review expose that note, as a nudge to refill them via trace/regrow in the
    # current form.
    if heavy:
        parts.append("\n═══ 压在心头 ═══")
        for h in heavy[:2]:
            head = _label_of({"meta": h["meta"], "content": h["content"]})[:30]
            note = f"（{h['clock_note']}）" if h["clock_note"] else ""
            if h["id"] == heavy_q_id:
                asked = (f"，上次问过是 {h['last_asked'][:10]}" if h["last_asked"]
                         else "，从来没问过")
                parts.append(f"🫀❓ 挂了 {h['held']} 天（重 {h['weight']:g}{asked}）："
                             f"{head} ({_short_id(h['id'])}) —— 这条还算数吗？{note}")
            else:
                parts.append(f"🫀 挂了 {h['held']} 天（重 {h['weight']:g}）："
                             f"{head} ({_short_id(h['id'])}){note}")
        parts.append('   └ 放下了 trace(status="resolved")；不做了 trace(status="abandoned")')

    # ---- Edited by the user: which events the user changed that I have not
    # looked at or folded yet ----
    # The notification mechanism is this pool itself — the tag is both the mark
    # and the notification; see core/profile.py.
    edited = edited_by_user(all_buckets)
    if edited:
        parts.append("\n═══ 人改过的 ═══")
        for e in edited[:3]:
            parts.append("· " + _line(e["meta"], e["content"], e["id"]))
        parts.append("   └ 认同就 fold（folds=[那几条], text=…）；不认同就说出来")

    # ---- 3 Middle term: recall's three-day overview (free of charge) ----
    parts.append("\n═══ 中期（这三天）═══")
    mid = await recall_core(when="3d", room="", tag="", query="", max_cells=1)
    parts.append(mid if "没有东西" not in mid else "（这三天没存东西）")

    # ---- 4 Long term —— **cut** ----
    #
    # Two observations, one killing the name and one killing the placement:
    # ① Every memory entry is already a long-term memory — "long term" was the
    #   wrong name from the start. That column was never holding "older
    #   memories"; it held **the current main thread**. Because the name was
    #   wrong, every attempted fix went in the wrong direction (adding an expiry
    #   mechanism, adding fields, adding reminders).
    # ② breath is live, so popping a few entries out reads as strange — what it
    #   would actually be showing is a period's overall theme.
    #   Waking is about what **floats up**; "what have I been doing lately" is
    #   something you **look up**. Pushing looked-up material into the
    #   subconscious is the root of reading two-day-stale content as fact.
    #
    # Big events did not disappear; they went back where they belong: **laid over
    # a stretch of time in recall** (tools/_bigevent.py plus the far end of
    # recall's browse path). At that moment I am looking back anyway.
    # ⚠️ Do not add this back. Read this passage before trying.

    # ---- 5 Random: something suddenly coming to mind ----
    # All three gates live in the contract source `event_pool()`: both old and new
    # room names are recognised · tooling entries do not count · covered ones stay
    # out.
    # The profile page imports the same function — what the page calls "suddenly
    # coming to mind" and what I remember on waking have to be the same set, or
    # there are two brains (and that failure is terrifyingly silent: both sides
    # show something, they are just not the same something).
    ev_pool = event_pool(all_buckets)
    if ev_pool:
        parts.append("\n═══ 忽然想起 ═══")
        for e in random.sample(ev_pool, min(2, len(ev_pool))):
            parts.append("· " + _line(e["meta"], e["content"], e["id"]))

    # ---- 7 The boundary: what day the earliest entry falls on ----
    #
    # Why this line has to exist: when a search finds nothing I have exactly two
    # explanations available ("I searched wrong" / "it really is not there"), and
    # the rules train me to suspect my own reading first — so I slide all the way
    # towards "it must be there, I just did not find it".
    # **The end of that road is not trying a few more times, it is inventing
    # something plausible to fill the gap** (which is exactly where three
    # fabricated "facts" once came from).
    # Give it a hard boundary and "suspect yourself" finally has a stopping point.
    #
    # ⚠️ It must be **computed live** (the minimum of the time coordinates) and
    # **never a hard-coded date**: memories get backfilled further into the past
    # later on, and a hard-coded number would become **a falsehood shaped like a
    # fact** — I would not question it on reading, I would use it to deny entries
    # that genuinely exist.
    #
    # ⚠️ The second half must not be shortened to "there is nothing in Loci before
    # that" either. That is false: earlier events really are in the library, they
    # just **have no entries of their own** — they were mentioned in passing by
    # later ones.
    # Both ends have to be blocked: finding nothing further back is no reason to
    # doubt myself, but it is also not grounds for asserting that nothing happened
    # in that stretch.
    #
    # Same definition as recall: _ts_of(meta) = when first, created as fallback.
    # Do not write a second one of these.
    # (The `by` parameter was cut — `_ts_of` now has exactly one definition, and
    #  there is no second.)
    ts_pool = [t for t in (_ts_of(e["meta"]) for e in entries) if t is not None]
    if ts_pool:
        parts.append(f"\n📍 最早的一条落在 {min(ts_pool).date().isoformat()}。"
                     "再往前的事没有自己的条目，只可能被后来的记忆顺带提到。")

    return "\n".join(parts)
