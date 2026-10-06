"""
========================================
tools/_pin.py — the gate in front of pin
========================================

The meaning of `pin` is:

    from "this one matters"  ->  "this one is how I mean to act"

This gate does not reject on the spot: it **pins it anyway, and adds one line of
reminder**.

------------------------------------------------------------
🔴 Why it does not block
------------------------------------------------------------
   The gate does not need to be tight. It can be loose. **This is not a
   judgement code can make well** — that is the whole of it.

And the reason runs one layer deeper than "the word list is too narrow":

  What this gate is actually there to stop is **pinning a flaw as a principle**
  (pinning "I am always impatient" = I intend to keep making this mistake).
  But "I am always impatient" and "the things I value have always grown slowly"
  are **both descriptive sentences** — one should be stopped, one should be kept.
  **A regex cannot tell those two apart; it matches sentence shape, not content.**
  A gate with zero resolution on the thing it exists to stop can only do damage
  when it is tight: it rejects genuine principles, and the only way to pin one is
  to bend the wording into a shape the regex recognises — at that moment what gets
  edited is the sentence, not the judgement behind it.

📌 The general rule ("if a rule needs a hand-written warning to stop it being
   misused, the box has the wrong thing in it") **still holds**; here it just
   points the other way: the box is fine, **this is simply not something code should be judging.**
   So the warning does not go back up on the wall of CLAUDE.md — it is attached
   to **the moment the action happens**. Read at the instant of pinning, it is
   worth more than the same words hanging on a wall.

------------------------------------------------------------
What the gate does
------------------------------------------------------------
· It blocks nothing. pinned=1 always goes through.
· When the body does not read as "how I mean to act", it **appends one reminder**
  after trace's success receipt: what the note by the door is for, what the one
  thing genuinely worth stopping is, and the way back out (pinned=0).
· The judgement stays with me. The ceiling (max_pinned) is still there — scarcity
  is guarded by the quota, not by sentence shape.

⚠️ Anything already pinned is left alone.

Exports: pin_note(content) -> str | None (None = reads like a principle, no need
         to say anything)
         looks_imperative(content) -> bool
========================================
"""

import re

# How far into the body the imperative shape has to appear.
# Why "the first N characters" and not the whole text: a principle's imperative is
# always up front — an "how I mean to act" buried in the third paragraph belongs to
# a piece of thinking that happens to mention what to do, not to a principle.
_HEAD_CHARS = 60

# The forms it recognises. This table **only decides whether to add a remark**, not
# whether the pin lands, so widening or narrowing it cannot edit what I wrote:
#   我要 / 我不      —— the most direct statement of intent
#   先…再            —— sequencing ("baseline first, then touch the code")
#   别(?!人)         —— prohibition; (?!人) rules out 「别人」, a frequent false hit
#   不准 / 不许       —— another way of saying the same prohibition
#   必须 / 就说 / 就停 —— hard constraints
#   停下 / ——停       —— interruption ("the moment I catch myself smoothing an
#                       answer over — stop")
#   每次 / 遇到…就     —— triggers
#   发现…立即         —— conditional triggers
#   记得 / 宁可 / 优先 —— weaker, but genuinely a statement of intent
_IMPERATIVE_RE = re.compile(
    r"我要|我不|先.*再|别(?!人)|不准|不许|必须|就说|就停|停下|——\s*停|"
    r"每次|遇到.*就|发现.*立即|记得|宁可|优先"
)


def looks_imperative(content: str) -> bool:
    """Does the shape of "how I mean to act" appear in the first 60 characters?"""
    return bool(_IMPERATIVE_RE.search(str(content or "")[:_HEAD_CHARS]))


def pin_note(content: str) -> str | None:
    """pin's reminder. Returns None when it reads like a principle; otherwise a
    line to append after the success receipt.

    🔴 It **does not block**. The return value is not an error — the caller must
    pin regardless and merely trail this text after the receipt.
    """
    if looks_imperative(content):
        return None
    head = str(content or "").strip().replace("\n", " ")[:40]
    return (
        "📌 钉上了，顺口说一句：这条读起来是**描述**（「{head}…」），"
        "不是「我要怎么做」。\n"
        "   门口那张纸只有几行，留在那儿的理由是**开口之前来不及去搜**——"
        "准则会改变下一个动作，描述句多半只是判词。\n"
        "   ⚠️ 唯一真该挡的是**把缺点钉成准则**：钉「我总是心急」，语义就成了"
        "「我要犯这个错」。这一条闸认不出来，只能我自己认。\n"
        "   翻得成朝向就翻（✗ 我总是在验收的时候心急 → ✓ 我要先跑一遍基线再动代码）；"
        "翻不成而它确实是「我是什么样的」，那就留着。\n"
        "   反悔的路：trace(bucket_id=..., pinned=0)。"
    ).format(head=head)
