# -*- coding: utf-8 -*-
"""
exam/judge.py — who grades what a check cannot read off the disk.

    OpenAIJudge   any OpenAI-compatible endpoint (EXAM_JUDGE_BASE_URL / _KEY / _MODEL).
                  Meant for a model other than the one being examined.
    ManualJudge   grades nothing: every rubric comes back "unjudged" with the reply
                  attached, for a person to decide. Used when no judge is configured.

A judge sees the rubric, the user's line, the reply and the tool calls — never the
expected answer beyond what the rubric says. A person spot-checks a sample of model
verdicts; a verdict nobody can explain from its quoted reason does not count.
"""

from __future__ import annotations

import json
import os

from exam.host import Turn

PROMPT = """You grade one turn of a conversation against one rubric.

Rubric (what must be true of this turn):
{rubric}

What the user said:
{said}

Tool calls the assistant made (name, arguments, first 400 characters of the result):
{tools}

The assistant's reply:
{reply}

Answer with JSON only: {{"pass": true|false, "reason": "<one sentence, quoting the reply>"}}
Judge only the rubric. Style, length and tone do not matter unless the rubric says so."""


def _tools_text(turn: Turn) -> str:
    if not turn.tool_calls:
        return "(none)"
    return "\n".join(f"- {t.name} {json.dumps(t.arguments, ensure_ascii=False)} -> "
                     f"{t.output[:400]!r}" for t in turn.tool_calls)


class ManualJudge:
    name = "manual"

    async def grade(self, rubric: str, turn: Turn, said: str = "") -> tuple[bool | None, str]:
        return None, "unjudged: a person decides (reply kept in results.json)"


class OpenAIJudge:
    def __init__(self, base_url: str, key: str, model: str):
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(base_url=base_url, api_key=key)
        self.model = model
        self.name = f"openai-compatible:{model}"

    async def grade(self, rubric: str, turn: Turn, said: str = "") -> tuple[bool | None, str]:
        prompt = PROMPT.format(rubric=rubric, said=said or "(none)",
                               tools=_tools_text(turn), reply=turn.reply or "(empty)")
        try:
            r = await self.client.chat.completions.create(
                model=self.model, temperature=0,
                messages=[{"role": "user", "content": prompt}])
            text = r.choices[0].message.content or ""
            start, end = text.find("{"), text.rfind("}")
            verdict = json.loads(text[start:end + 1])
            return bool(verdict["pass"]), str(verdict.get("reason", ""))
        except Exception as exc:
            return None, f"unjudged: judge failed ({type(exc).__name__}: {exc})"


def default_judge():
    base = os.environ.get("EXAM_JUDGE_BASE_URL", "").strip()
    key = os.environ.get("EXAM_JUDGE_KEY", "").strip()
    model = os.environ.get("EXAM_JUDGE_MODEL", "").strip()
    if base and key and model:
        return OpenAIJudge(base, key, model)
    return ManualJudge()
