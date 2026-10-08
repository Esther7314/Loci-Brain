"""
Loci Brain — Unified Error Code System
==========================================================

Design principles (from rule.md §1.5):
    "When it comes to producing and finding errors, anything that can be said out loud
     is never swallowed."
    "An error has to be visible to the person on the dashboard AND to the model at the
     MCP return value."

Four severity levels:
    F (Fatal)   — refuse to start + terminal output + write error.log
    E (Error)   — dashboard dialog + appended to the MCP return + last 15 log lines
    W (Warning) — appended to the MCP return + the dashboard log panel
    I (Info)    — appended to the MCP return (a light note, e.g. an automatic downgrade)

What this module owns:
    1. ERROR_CODES: the error-code registry (level, description, suggested action)
    2. format_error(): rendering to the standard string form
    3. record_error(): persist to errors.jsonl + the in-memory buffer
    4. recent_errors(): what the /api/errors/recent endpoint reads
    5. log_buffer: a ring buffer holding the last N log lines (everything that went
       through stderr included)
    6. attach_log_buffer_handler(): installs BufferHandler on the root logger
    7. warnings_channel (contextvars): W/I notices accumulated during one MCP tool call,
       popped by _with_notice() and appended to the return value before the tool returns

No extra dependencies: standard library only.
"""
from __future__ import annotations

import collections
import contextvars
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Iterable

logger = logging.getLogger(__name__)


# ============================================================
# 1. Error Code Registry
# ============================================================

@dataclass(frozen=True)
class ErrorSpec:
    code: str            # e.g. "OB-E001"
    level: str           # "F" | "E" | "W" | "I"
    title_zh: str
    title_en: str
    suggestion_zh: str
    suggestion_en: str = ""


# The registry — when changing or adding an entry, keep rule.md §11 in sync
ERROR_CODES: dict[str, ErrorSpec] = {
    # ---- Fatal: refuse to start ----
    "OB-F001": ErrorSpec(
        code="OB-F001",
        level="F",
        title_zh="向量化 API Key 缺失",
        title_en="Embedding API key missing",
        suggestion_zh=(
            "设置环境变量 LOCI_EMBED_API_KEY（或在 config.yaml 中填写 embedding.api_key）。\n"
            "若暂时不需要语义检索，可在 config.yaml 中设置 embedding.enabled=false 跳过。"
        ),
    ),
    "OB-F002": ErrorSpec(
        code="OB-F002",
        level="F",
        title_zh="config.yaml 损坏或缺失",
        title_en="config.yaml missing or malformed",
        suggestion_zh=(
            "检查项目根目录是否存在 config.yaml；如缺失，从 config.example.yaml 复制一份。"
            "如已存在，运行 `python -c \"import yaml; yaml.safe_load(open('config.yaml'))\"` 看是否能解析。"
        ),
    ),
    "OB-F003": ErrorSpec(
        code="OB-F003",
        level="F",
        title_zh="vault 目录不可写",
        title_en="vault (buckets) directory not writable",
        suggestion_zh=(
            "检查 LOCI_BUCKETS_DIR 指向的目录是否存在且当前用户拥有写权限。"
            "Docker 部署请检查 volume 挂载与 uid/gid 映射。"
        ),
    ),
    "OB-F004": ErrorSpec(
        code="OB-F004",
        level="F",
        title_zh="embedding 后端初始化失败",
        title_en="Embedding backend initialization failed",
        suggestion_zh=(
            "检查 LOCI_EMBED_API_KEY 是否有效，以及 LOCI_EMBED_BASE_URL 是否可达。"
        ),
    ),

    # ---- Error: dashboard dialog + appended to the MCP return ----
    "OB-E001": ErrorSpec(
        code="OB-E001",
        level="E",
        title_zh="embedding API 调用失败",
        title_en="Embedding API call failed",
        suggestion_zh=(
            "检查网络可达性、LOCI_EMBED_API_KEY 是否有效、配额是否耗尽。"
            "本次写入仍会保存到 buckets，向量由后台自动重试；也可调用 "
            "/api/embedding/backfill 手动触发全库对账。"
        ),
    ),
    "OB-E002": ErrorSpec(
        code="OB-E002",
        level="E",
        title_zh="写盘失败",
        title_en="Disk write failed",
        suggestion_zh=(
            "检查磁盘剩余空间、目录权限；确认未被备份/同步软件锁定（iCloud/Dropbox 等）。"
        ),
    ),
    "OB-E003": ErrorSpec(
        code="OB-E003",
        level="E",
        title_zh="并发冲突超时",
        title_en="Concurrency lock timeout",
        suggestion_zh=(
            "同一 content 的 merge_or_create 长时间未释放锁；通常是上一个调用卡死。"
            "稍后重试；若反复出现，重启服务或检查 LLM 提供方是否慢响应。"
        ),
    ),
    "OB-E004": ErrorSpec(
        code="OB-E004",
        level="E",
        title_zh="MCP 工具执行异常",
        title_en="MCP tool execution exception",
        suggestion_zh=(
            "查看下方异常详情与最近 15 条日志定位根因。"
            "若是参数问题，按提示修正；若是后端故障，请重试或反馈。"
        ),
    ),

    # ---- Warning: appended to the MCP return + the dashboard log panel ----
    "OB-W001": ErrorSpec(
        code="OB-W001",
        level="W",
        title_zh="importance 越界已修正",
        title_en="importance out of range, clamped",
        suggestion_zh="importance 必须在 [1,10]；本次已被修正到边界值。",
    ),
    "OB-W002": ErrorSpec(
        code="OB-W002",
        level="W",
        title_zh="valence/arousal 越界已回退",
        title_en="valence/arousal out of range, clamped",
        suggestion_zh="valence/arousal 必须在 [0.0, 1.0]；本次已被修正到边界值。",
    ),
    "OB-W003": ErrorSpec(
        code="OB-W003",
        level="W",
        title_zh="importance≥9 配额接近上限",
        title_en="importance≥9 quota near cap",
        suggestion_zh=(
            "标为 importance≥9 的桶接近上限（硬上限 24）。\n"
            "⚠️ 2026-08-19 起这条**不是给模型的待办**：importance 已经不在工具面上"
            "（trace / grow 的 importance 形参 8-18 撤了），"
            "**没有任何入口能降低任何一条的 importance**。\n"
            "撑满这个池子的只会是历史条目。要处理去 Dashboard 手动改，"
            "或者不管——满了之后新的会自动降级（OB-I001），不会拒绝写入。"
        ),
    ),
    "OB-W004": ErrorSpec(
        code="OB-W004",
        level="W",
        title_zh="pinned 配额接近上限",
        title_en="pinned quota near cap",
        suggestion_zh=(
            "pinned 桶接近上限（默认 18/20，硬上限 20，可在 config.limits.max_pinned 调整）。"
            "建议先用 trace(bucket_id, pinned=0) 取消不再核心的钉选，再钉新桶。"
        ),
    ),
    "OB-W005": ErrorSpec(
        code="OB-W005",
        level="W",
        title_zh="embeddings.db 中的模型/维度与当前后端不一致",
        title_en="embeddings.db model/dim mismatch with current backend",
        suggestion_zh=(
            "过往写入的向量与当前模型不同维，搜索会退化为 0 分。"
            "请在 Dashboard 设置页点击「切换模型」，或调用 POST /api/embedding/migrate 重建索引。"
            "迁移期间搜索降级为关键词模式，不会丢文件。"
        ),
    ),

    # ---- Info: automatic downgrades and other light notes ----
    "OB-I001": ErrorSpec(
        code="OB-I001",
        level="I",
        title_zh="importance 已自动降级（importance≥9 配额超标）",
        title_en="importance auto-downgraded (≥9 quota exceeded)",
        suggestion_zh=(
            "★ 这是系统自作主张帮你做的事 ★\n"
            "importance≥9 的桶已达硬上限 24，这一条被自动降级为 importance=8。\n"
            "⚠️ 2026-08-19 起**不必也无法手动善后**：importance 的入口 8-18 整个撤了"
            "（连带 breath_advanced 也没了），这条只是告诉你「盘上高分条目满了」。\n"
            "真要重排，去 Dashboard。"
        ),
    ),
    "OB-I002": ErrorSpec(
        code="OB-I002",
        level="I",
        title_zh="pinned 已自动退出（pinned 配额超标）",
        title_en="pinned auto-unset (pinned quota exceeded)",
        suggestion_zh=(
            "★ 这是 OB 自作主张帮你做的事 ★\n"
            "pinned 桶已达硬上限（默认 20，可在 config.limits.max_pinned 调整），本次未钉成功（保留为普通桶）。\n"
            "建议：用 breath 看一遍当前 pinned 列表，把不再属于「永久核心准则」的"
            "用 trace(bucket_id, pinned=0) 取消，再来钉这条。"
        ),
    ),
}

# ============================================================
# 2. In-memory Log Ring Buffer
# ============================================================

_LOG_BUFFER_MAX = 500     # the whole ring buffer; the dashboard's "recent logs" reads it
_LOG_TAIL_FOR_ERROR = 15  # how many recent log lines ride along with an E-level error (per spec)

_log_buffer: collections.deque[str] = collections.deque(maxlen=_LOG_BUFFER_MAX)
_log_buffer_lock = threading.Lock()


class _BufferHandler(logging.Handler):
    """Keep a copy of every logging line in an in-memory deque, so an E-level error can
    carry a tail of them."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            with _log_buffer_lock:
                _log_buffer.append(line)
        except Exception:
            # a logging handler must never raise on its own account
            pass


def attach_log_buffer_handler(level: int = logging.INFO) -> None:
    """Attach BufferHandler to the root logger. Idempotent: calling it again is harmless."""
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, _BufferHandler):
            return
    h = _BufferHandler()
    h.setLevel(level)
    h.setFormatter(logging.Formatter(
        "[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(h)


def get_recent_logs(n: int = _LOG_TAIL_FOR_ERROR) -> list[str]:
    """Read the last n log lines (newest last)."""
    with _log_buffer_lock:
        if n >= len(_log_buffer):
            return list(_log_buffer)
        return list(_log_buffer)[-n:]


# ============================================================
# 3. Persistent Error Log
# ============================================================

_errors_path: str | None = None
_errors_path_lock = threading.Lock()
_MAX_ERROR_TAIL_SCAN_BYTES = 8 * 1024 * 1024
_TAIL_CHUNK_BYTES = 64 * 1024


def _iter_tail_lines(path: str, *, max_bytes: int):
    """Yield UTF-8 text lines newest-first from a bounded file tail."""

    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        remaining = max(0, int(max_bytes))
        carry = b""
        while position > 0 and remaining > 0:
            chunk_size = min(_TAIL_CHUNK_BYTES, position, remaining)
            position -= chunk_size
            remaining -= chunk_size
            handle.seek(position)
            block = handle.read(chunk_size) + carry
            parts = block.split(b"\n")
            carry = parts.pop(0)
            for raw_line in reversed(parts):
                yield raw_line.decode("utf-8", errors="replace")
        # Only yield carry when it starts at byte zero.  If the scan hit its
        # byte cap, carry is an intentionally incomplete giant/old line.
        if position == 0 and carry:
            yield carry.decode("utf-8", errors="replace")


def configure_errors_path(buckets_dir: str) -> None:
    """Called by the server at startup: errors.jsonl goes to buckets_dir/.logs/errors.jsonl."""
    global _errors_path
    log_dir = os.path.join(buckets_dir, ".logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        _errors_path = os.path.join(log_dir, "errors.jsonl")
    except Exception as e:
        logger.warning(f"[errors] cannot create log dir {log_dir}: {e}")
        _errors_path = None


def _persist_error_record(record: dict) -> None:
    if not _errors_path:
        return
    try:
        with _errors_path_lock:
            with open(_errors_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[errors] persist failed: {e}")


def recent_errors(limit: int = 50, min_level: str = "W") -> list[dict]:
    """Read the last `limit` recorded errors, walking backwards from the end of errors.jsonl."""
    if not _errors_path or not os.path.exists(_errors_path):
        return []
    order = ["I", "W", "E", "F"]
    if min_level not in order:
        min_level = "W"
    min_idx = order.index(min_level)
    out: list[dict] = []
    try:
        with _errors_path_lock:
            for ln in _iter_tail_lines(
                _errors_path,
                max_bytes=_MAX_ERROR_TAIL_SCAN_BYTES,
            ):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                lvl = obj.get("level", "W")
                if lvl in order and order.index(lvl) >= min_idx:
                    out.append(obj)
                if len(out) >= limit:
                    break
    except Exception as e:
        logger.warning(f"[errors] read failed: {e}")
        return []
    return out


def clear_errors_log() -> int:
    """Truncate errors.jsonl and return how many lines it held (drives the dashboard's
    "mark as read" button)."""
    if not _errors_path or not os.path.exists(_errors_path):
        return 0
    try:
        with _errors_path_lock:
            with open(_errors_path, "r", encoding="utf-8") as f:
                n = sum(1 for _ in f)
            open(_errors_path, "w", encoding="utf-8").close()
        return n
    except Exception as e:
        logger.warning(f"[errors] clear failed: {e}")
        return 0


# ============================================================
# 4. Standard Formatter
# ============================================================

_LEVEL_PREFIX = {
    "F": "🛑",   # Fatal
    "E": "❌",   # Error
    "W": "⚠️",   # Warning
    "I": "ℹ️",   # Info
}


def format_error(
    code: str,
    detail: str = "",
    *,
    include_logs: bool | None = None,
    extra: dict | None = None,
) -> str:
    """Render the standard string form.

    With include_logs=None the level decides: F/E carry a log tail by default, W/I do not.
    """
    spec = ERROR_CODES.get(code)
    if not spec:
        # Unknown code: still render, so that a mistyped code is visible at a glance
        return (
            f"❌ [{code}] 未注册错误码\n"
            f"详情：{detail}\n"
            f"建议：在 src/errors.py ERROR_CODES 注册该码或修正调用处。"
        )
    prefix = _LEVEL_PREFIX.get(spec.level, "•")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    parts = [
        f"{prefix} [{spec.code}] {spec.title_zh}",
    ]
    if detail:
        parts.append(f"描述：{detail}")
    parts.append(f"建议：{spec.suggestion_zh}")
    parts.append(f"时间：{ts}")
    if extra:
        for k, v in extra.items():
            parts.append(f"{k}：{v}")

    if include_logs is None:
        include_logs = spec.level in ("F", "E")
    if include_logs:
        tail = get_recent_logs(_LOG_TAIL_FOR_ERROR)
        parts.append("")
        parts.append(f"--- 最近 {len(tail)} 条日志 ---")
        parts.extend(tail if tail else ["(暂无日志)"])
    return "\n".join(parts)


def record_error(
    code: str,
    detail: str = "",
    *,
    extra: dict | None = None,
    log: bool = True,
) -> dict:
    """Record one error: write errors.jsonl, mirror it to the logger at the matching
    level, and return it as a structured dict.

    To attach it to an MCP return value, callers use format_error or push_warning.
    """
    spec = ERROR_CODES.get(code)
    level = spec.level if spec else "E"
    record = {
        "code": code,
        "level": level,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "title": spec.title_zh if spec else "未注册错误码",
        "detail": detail,
        "extra": extra or {},
    }
    _persist_error_record(record)
    if log:
        msg = f"[{code}] {record['title']} | {detail}"
        if level == "F":
            logger.critical(msg)
        elif level == "E":
            logger.error(msg)
        elif level == "W":
            logger.warning(msg)
        else:
            logger.info(msg)
    return record


# ============================================================
# 5. MCP Return Suffix Channel
# ============================================================
#
# The design: during one MCP tool call, business code (bucket_manager, tools/_common
# and friends) may raise a W/I notice at any depth. Those notices have to reach the end
# of the MCP return value so the model can see them. A per-task list is kept in
# contextvars; server_call._with_notice pops it when the tool returns and
# appends it. Note that contextvars are isolated per task under asyncio, so notices
# never bleed from one call into another.

_warnings_var: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "ob_warnings", default=None
)


def begin_warnings() -> None:
    """Called once at the entry of every MCP tool call, to open this call's channel."""
    _warnings_var.set([])


def push_warning(code: str, detail: str = "", *, extra: dict | None = None) -> None:
    """Called by business code to register one W/I-level notice.

    It also goes through record_error (persisted to disk and to the logger).
    """
    record_error(code, detail, extra=extra)
    cur = _warnings_var.get()
    if cur is None:
        # The caller is outside an MCP tool context (a background task, say): persisting is enough
        return
    cur.append(format_error(code, detail, extra=extra))


def pop_warnings() -> list[str]:
    """Called by server_call._with_notice before a tool returns, to take this call's notices."""
    cur = _warnings_var.get()
    if cur is None:
        return []
    _warnings_var.set([])
    return cur


def format_warnings_suffix(warnings: Iterable[str]) -> str:
    items = list(warnings)
    if not items:
        return ""
    return "\n\n" + "\n\n".join(items)


# ============================================================
# 6. Startup-time Exception
# ============================================================

class OBStartupError(SystemExit):
    """Fatal: refuse to start. Carries the error code; server.py catches it at top level,
    prints the standard form and writes error.log.

    Note that SystemExit has a built-in ``.code`` attribute of its own (the process exit
    code), so this class exposes the OB error code as ``.error_code``, with ``.code``
    kept as a compatibility alias.
    """

    def __init__(self, code: str, detail: str = "", *, extra: dict | None = None):
        self.error_code = code
        self.detail = detail
        self.extra = extra or {}
        # SystemExit's message is what finally reaches the terminal
        msg = format_error(code, detail, extra=extra, include_logs=True)
        super().__init__(msg)


def write_fatal_log(code: str, detail: str, *, buckets_dir: str | None = None) -> None:
    """Fatal level only: write error.log directly, not errors.jsonl, since the path for
    that may not have been configured yet."""
    target_dir = buckets_dir or os.environ.get("LOCI_BUCKETS_DIR", "").strip() or "."
    try:
        log_dir = os.path.join(target_dir, ".logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "error.log"), "a", encoding="utf-8") as f:
            f.write(format_error(code, detail, include_logs=True) + "\n\n")
    except Exception:
        pass


__all__ = [
    "ERROR_CODES",
    "ErrorSpec",
    "format_error",
    "record_error",
    "recent_errors",
    "clear_errors_log",
    "configure_errors_path",
    "get_recent_logs",
    "attach_log_buffer_handler",
    "begin_warnings",
    "push_warning",
    "pop_warnings",
    "format_warnings_suffix",
    "OBStartupError",
    "write_fatal_log",
]
