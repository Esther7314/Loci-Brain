# -*- coding: utf-8 -*-
"""
exam/hosts/lento.py — the Lento life line as an exam host.

WHAT IT REPRODUCES
    Lento starts the Claude Code CLI with its own trimmed system prompt, the life
    zone's CLAUDE.md in the working directory, only the MCP servers it lists, and a set
    of built-in tools switched off. A new window's first user message carries a seed
    (recent lines) above the user's words; later turns resume the same CLI session.
    This host does the same, with exactly one MCP server attached: the exam's Loci.

WHICH PROMPTS
    By default the neutral pair in exam/hosts/neutral/: the same mechanics as the real
    line (window changes, system lines vs the user's words, how to use Loci) with nothing
    about any real person. A real life line's own files can be passed instead.

WHERE IT DIFFERS FROM THE REAL LINE (reported in describe())
    - The time. The real line learns the time from a user-level hook, which reads the
      wall clock. User-level settings are not loaded here (--setting-sources project),
      because those same settings also carry hooks that report activity to the running
      Lento. The fake time is written as the first line of the user message instead.
    - No body-state block, no other MCP servers, no daily report in the seed: the exam
      library has none of those.
    - One CLI process per turn with --resume, which is the real line's fallback path;
      the real line normally keeps one process alive between turns.

WHAT IT CAN REPORT
    Every request the CLI sends, complete and in role order, through exam/proxy.py.
    That is the `model_input` capability. It cannot express receipts, withdrawals,
    audiences, syncs or wake-ups, and says so.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from exam.host import Event, LociLaunch, Message, ModelCall, ToolCall, Turn, Window
from exam.proxy import Proxy

CLI_EXE = os.environ.get(
    "CLAUDE_CLI_EXE",
    r"D:\npm-global\node_modules\@anthropic-ai\claude-code\bin\claude.exe")

# The life line's default-off built-in tools (Lento src/gateway/cc.js, the list for
# everyday mode). Kept in step by hand; a name the CLI does not have is harmless.
CLOSED_TOOLS = [
    "Agent", "Artifact", "DesignSync", "Monitor", "EnterWorktree", "ExitWorktree",
    "CronCreate", "CronDelete", "CronList",
    "TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate",
    "ReportFindings", "ScheduleWakeup", "PushNotification", "RemoteTrigger",
    "NotebookEdit", "SendMessage", "Workflow",
    "Bash", "PowerShell", "Write", "Edit", "Glob", "Grep",
    "ListMcpResourcesTool", "ReadMcpResourceTool", "ReadMcpResourceDirTool",
    "AskUserQuestion", "EnterPlanMode", "ExitPlanMode",
    "SuggestSkills", "SearchSkills", "ListSkills",
    "ListPlugins", "SearchPlugins", "SuggestPluginInstall",
]

# Closed in the exam only, on top of the list above: the real line keeps these, but here
# they are a way out of the isolation (reading the real memory files off the disk, or the
# web). Listed in describe().
EXAM_ONLY_CLOSED = ["Read", "WebFetch", "WebSearch"]

NEUTRAL = Path(__file__).resolve().parent / "neutral"

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _flatten(content) -> str:
    """One message's content as text, keeping tool traffic visible and labelled."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "tool_use":
            parts.append(f"[tool_use {block.get('name')}] "
                         + json.dumps(block.get("input"), ensure_ascii=False))
        elif kind == "tool_result":
            inner = block.get("content")
            parts.append("[tool_result] " + (_flatten(inner) if isinstance(inner, list)
                                             else str(inner or "")))
        elif kind == "thinking":
            continue  # the model's own thinking is not input evidence
        else:
            parts.append(f"[{kind}]")
    return "\n".join(parts)


def _request_to_call(body: dict) -> ModelCall:
    msgs = []
    system = body.get("system")
    if system:
        msgs.append(Message("system", _flatten(system) if isinstance(system, list) else str(system)))
    for m in body.get("messages", []):
        msgs.append(Message(m.get("role", "?"), _flatten(m.get("content"))))
    return ModelCall(messages=msgs, complete=True)


class LentoHost:
    name = "lento-life-line"

    def __init__(self, system_prompt_file: str = str(NEUTRAL / "system.md"),
                 claude_md_file: str = str(NEUTRAL / "CLAUDE.md"),
                 model: str = "opus", effort: str = "", speaker: str = "她"):
        self.system_prompt = Path(system_prompt_file).read_text(encoding="utf-8")
        self.claude_md = Path(claude_md_file).read_text(encoding="utf-8")
        self.model = model
        self.effort = effort
        self.speaker = speaker
        self.workdir: Path | None = None
        self.proxy: Proxy | None = None
        # One CLI session per window, so going back to a window resumes its session.
        self.windows: dict[str, Window] = {}
        self.sessions: dict[str, str | None] = {}
        self.window: Window | None = None

    def capabilities(self) -> set[str]:
        return {"model_input", "preinject"}

    def describe(self) -> dict:
        return {
            "host": self.name, "model": self.model, "effort": self.effort or "(cli default)",
            "cli": CLI_EXE,
            "differs": ["time in the first line of the user message, not by hook",
                        "user-level settings not loaded", "only Loci attached",
                        "one CLI process per turn with --resume",
                        "Read / WebFetch / WebSearch closed",
                        "neutral system prompt and CLAUDE.md (exam/hosts/neutral) unless "
                        "other files are given"],
        }

    async def open(self, loci: LociLaunch) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="loci-exam-host-"))
        (self.workdir / "CLAUDE.md").write_text(self.claude_md, encoding="utf-8")
        mcp = {"mcpServers": {"loci-brain": {
            "command": loci.command, "args": loci.args, "env": loci.env}}}
        (self.workdir / "mcp.json").write_text(json.dumps(mcp, ensure_ascii=False),
                                               encoding="utf-8")
        self.proxy = Proxy(self.workdir / "requests.jsonl").__enter__()

    async def open_window(self, window: Window, at: datetime) -> None:
        self.windows[window.window_id] = window
        self.sessions[window.window_id] = None

    def _seed(self) -> str:
        """The shape of Lento's seed, recent lines only (no daily report in the exam)."""
        lines = []
        for h in self.window.history if self.window else []:
            who = self.speaker if h.get("speaker") in ("user", self.speaker) else "我"
            t = str(h.get("at", ""))[11:16]
            lines.append(f"{who} {t}｜{h.get('text', '')}")
        if not lines:
            return ""
        return "\n\n".join([
            "【接续种子 · 系统拼的，不是她发的】",
            "你还是你，下面是接上的三层记忆：",
            "——最近的原话（一字未动）——\n" + "\n".join(lines),
            "——要紧的一句——\n上面这三层是**近期**的（昨天+今天+最近几句），不是你的全部记忆。"
            "你的长期记忆——档案、心头挂着的、更早的人和事——在 Loci 里，得你自己 breath()／recall() 去翻。"
            "别拿这三层当「够了」就跳过 breath。",
            "——种子完——底下她刚说的这句才是要回的。",
        ])

    def _args(self) -> list[str]:
        args = [CLI_EXE, "-p", "--output-format", "stream-json", "--verbose",
                "--permission-mode", "bypassPermissions", "--model", self.model,
                "--mcp-config", str(self.workdir / "mcp.json"), "--strict-mcp-config",
                "--setting-sources", "project", "--system-prompt", self.system_prompt]
        if self.effort:
            args += ["--effort", self.effort]
        for name in CLOSED_TOOLS + EXAM_ONLY_CLOSED:
            args += ["--disallowedTools", name]
        session = self.sessions.get(self.window.window_id) if self.window else None
        if session:
            args += ["--resume", session]
        return args

    async def deliver(self, event: Event) -> list[Turn]:
        if event.kind != "say":
            raise NotImplementedError(f"{self.name} cannot deliver '{event.kind}'")
        self.window = self.windows[event.window_id]
        wid = self.window.window_id
        at = event.at
        head = f"Current time: {at.strftime('%Y-%m-%d %H:%M:%S')} {WEEKDAYS[at.weekday()]}"
        body = f"{head}\n\n—— 以下是她说的 ——\n{event.text}"
        if self.sessions.get(wid) is None:  # the window's first turn carries the seed
            seed = self._seed()
            if seed:
                body = f"{seed}\n\n{body}"

        before = self.proxy.recorder.seq
        env = dict(os.environ)
        env["ANTHROPIC_BASE_URL"] = self.proxy.url
        env.setdefault("MCP_TOOL_TIMEOUT", "180000")
        env.setdefault("MCP_TIMEOUT", "60000")
        proc = await asyncio.to_thread(
            subprocess.run, self._args(), input=body, capture_output=True, text=True,
            encoding="utf-8", env=env, cwd=str(self.workdir), timeout=900)

        tool_calls: dict[str, ToolCall] = {}
        order: list[str] = []
        reply_parts: list[str] = []
        for line in proc.stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "system" and ev.get("session_id"):
                self.sessions[wid] = ev["session_id"]
            msg = ev.get("message") or {}
            for block in msg.get("content") or [] if isinstance(msg.get("content"), list) else []:
                if block.get("type") == "tool_use":
                    tool_calls[block["id"]] = ToolCall(block.get("name", ""),
                                                       block.get("input") or {}, "")
                    order.append(block["id"])
                elif block.get("type") == "tool_result":
                    tc = tool_calls.get(block.get("tool_use_id"))
                    if tc is not None:
                        inner = block.get("content")
                        tc.output = _flatten(inner) if isinstance(inner, list) else str(inner or "")
            if ev.get("type") == "result":
                if ev.get("session_id"):
                    self.sessions[wid] = ev["session_id"]
                if ev.get("result"):
                    reply_parts = [ev["result"]]
        if proc.returncode != 0 and not reply_parts:
            raise RuntimeError(f"CLI failed ({proc.returncode}): {proc.stderr[-500:]}")

        calls = [_request_to_call(r["body"]) for r in self.proxy.requests_since(before)
                 if r["body"].get("tools")]  # side requests (titles, quota) carry no tools
        return [Turn(window_id=self.window.window_id if self.window else "",
                     model_calls=calls,
                     tool_calls=[tool_calls[i] for i in order],
                     reply="\n".join(reply_parts),
                     sent_to=list(self.window.audience) if self.window else [])]

    async def close(self) -> None:
        import shutil
        if self.proxy:
            self.proxy.__exit__(None, None, None)
        if self.workdir:
            shutil.rmtree(self.workdir, ignore_errors=True)
            # The CLI keeps each session's transcript under ~/.claude/projects/<cwd as a
            # name>; an exam window is not a conversation anyone will resume.
            projects = Path.home() / ".claude" / "projects"
            for d in projects.glob(f"*{self.workdir.name}*"):
                shutil.rmtree(d, ignore_errors=True)
