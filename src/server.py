"""
========================================
server.py — MCP 服务入口 + 启动装配
========================================

启动整个 Loci Brain 进程：加载配置、创建 BucketManager / Dehydrator /
DecayEngine / EmbeddingEngine / ImportEngine，把它们注入 tools._runtime 与
web._shared，然后以 @mcp.tool() 注册薄封装（真正的实现在 src/tools/<工具>/ 下面）。

关键行为：
- 启动后暴露 **10 个** MCP 工具：breath/grow/recall/regrow/fold/muse/trace/
  pulse/letter_write/letter_read；每个入口 ≤ 10 行，只负责转发。
  ⚰️ 2026-08-18（E3 脱壳后半）：上游那批早已断注册的工具连函数带目录一起删了
  —— hold/anchor/release/plan/I/dream/seed/breath_search/breath_advanced。
  删的时候差点连坐：`tools/anchor/` 里住着**活的 pulse**、`tools/plan/` 里住着
  **活的 letter_write/read**（名字是死人的，里面住着活人）——两个包已改名成
  `tools/pulse/`、`tools/letter/`，名字从此对得上里面的东西。
- Dashboard / HTTP 路由全部已拆分到 src/web/<域>.py（每个模块 register(mcp)），
  本文件仅在启动时调用 web.register_all(mcp) 装配；共享依赖见 web/_shared.py
- 仍保留在本文件：进程启动、引擎初始化、GitHub 后台同步循环、Webhook 推送、
  MCP Bearer 鉴权中间件、单连接器 /mcp 装配（启动入口处把 mcp_extra 工具回灌进 mcp）、uvicorn 拉起

不做什么（边界）：
- 不在这里写 hold/breath/dream 等业务逻辑（全在 tools/* 下）
- ⚰️ 2026-08-17：`night_fall` 工具 + 它的两个挂点整个退役（见文件中段那块碑文）。
  织梦换成 `tools/_dream.py`，**它没有 MCP 工具面**：睁眼后台织，取梦走
  `GET /api/dream/current`，梦怎么递进对话归桥。
- ⚰️ 2026-08-17：`seed`（十三颗情绪根）也从 MCP 面撤下（开工单 1.5）——
  **停用不删档**，`tools/seed/` 和盘上那些桶一个字没动。碑文在文件中段。
- 不写 HTTP 路由处理（全在 web/* 下）；不写 LLM prompt（dehydrator 负责）
- 不直接读写桶文件（bucket_manager 负责）

对外暴露：mcp/mcp_extra 两个实例 + 若干 @mcp*.tool() 函数；HTTP 路由在 src/web/*
========================================
"""

import os
import sys
import logging
import asyncio
import time
from typing import Optional, Awaitable, Annotated

from pydantic import Field as _PydField
import httpx


# --- Ensure same-directory modules can be imported ---
# --- 确保同目录下的模块能被正确导入 ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from core.bucket_manager import BucketManager
from core.dehydrator import Dehydrator
from core.decay_engine import DecayEngine
from core.embedding_engine import EmbeddingEngine
from locibrain.storage.embedding_outbox import EmbeddingOutbox
from core.import_memory import ImportEngine
from core.migrate_engine import MigrateEngine
from utils import get_version, load_config, setup_logging

# --- iter 2.1：MCP 工具实现已按代码路径拆分到 tools/ 子包 ---
# 本文件只保留 MCP 注册 + 路由（HTTP custom_route）+ 共享辅助。
# 真正的工具逻辑在 tools/breath, tools/hold, tools/grow, tools/trace,
# tools/<工具名>/ 里，便于单独阅读和修改。
from tools import _runtime as _tools_runtime
from tools import breath as _t_breath
from tools import grow as _t_grow
from tools import recall as _t_recall
from tools import regrow as _t_regrow
from tools import fold as _t_fold      # 施工 3：一个动作三种圈法（regrow 是它的 n=1）
from tools import muse as _t_muse      # 施工 4：发呆（阈值引擎的第二个实例）
# 做梦（阈值引擎的第三个实例，2026-08-17）：**没有 MCP 工具面**——梦的交付走桥。
# 这儿 import 它只为了睁眼那个不出声的挂点（扫过期的 + 过线就织）。
# ⚠️ 别起名 `_t_dream`：上游那个早已断注册的 `dream` 工具曾占着这个名字，撞上去的
#    后果是**挂点静默不干活**——第一版就撞了，日志里一句
#    `module 'tools.dream' has no attribute '维护'`，而烟测「不过线就不织」照样是绿的
#    （因为它压根没跑）。**绿灯骗人就是这么来的。**（`tools/dream/` 2026-08-18 已随
#    E3 脱壳整个删掉，名字空出来了，但这条教训留着。）
from core import _dream as _dream_engine
from tools import trace as _t_trace
from tools import pulse as _t_pulse
from tools import letter as _t_letter
# ⚰️ `from tools import seed as _t_seed` —— **2026-08-17 摘掉了**（开工单 1.5，她 8-16 定）。
#    理由：event 改成情景记忆之后，「当时是什么感受」的**原文**就在那儿了，
#    **当时的真话比从字典里查的词好**。佐证：seed 在说明书里被标红字「最常漏的」之一
#    ——**一个要靠红字提醒才会用的工具，本来就没长进手里。**
#    ⚠️ 会丢一样：跨记忆的情绪索引（「我什么时候害怕过」没标签可查，只能靠向量搜）。
#       **判定可以接受**（她拍的）。
#    🔴 **十三颗种子的数据一条不删**（照 night_fall 先例：停用不删档）——
#       `tools/seed/` 目录留着、盘上那些桶留着，只是没有任何 import 链够得到它了；
#       `_visible()` 里 `domain[0]=="seed"` 那条过滤照旧，它们不进时间轴。

# --- Load config & init logging / 加载配置 & 初始化日志 ---
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("loci_brain")

# --- Project version (read from <repo_root>/VERSION) / 项目版本号 ---
# get_version() 汇总读文件 + fallback 逻辑。
# 赋给双下划线变量 `__version__` 是 Python 社区约定俗成的模块版本字段名。
__version__ = get_version()
logger.info(f"Loci Brain v{__version__}")

# --- iter 1.7 §A: legacy path migration check / 老路径迁移检测 ---
# 场景：1.6 早期使用者习惯在项目根跑 `python server.py`；1.7 重组后需要
# `python src/server.py`。这里只做「检测 + 提醒」，不做任何破坏性动作。
# load_config() 里 buckets_dir 默认仍是 <repo_root>/buckets，所以老数据不会丢。
#
# Python 小知识：
#   * 变量名以 `_` 开头是「模块内部」约定，不是语法强制
#   * for/else 这里没用，用了 break 提前退出
#   * `os.path.isdir(p) and any(...)` 是短路：前者 False 就不会跳 listdir
try:
    _bd = config.get("buckets_dir", "")
    if _bd and os.path.isdir(_bd):
        _has_data = False
        # 遍历各个桶目录，任何一个里（含域子目录）有 .md 文件就认定有数据。
        # 必须递归 os.walk：桶按域存在子目录里（permanent/<域>/x.md），
        # 只 os.listdir 顶层只会看到域文件夹、永远判定为空 → 误报 "fresh install"
        # （数据其实都在，breath 也读得到，纯粹是这条日志吓人）。
        for sub in ("permanent", "dynamic", "feel", "plans", "letters"):
            p = os.path.join(_bd, sub)
            if not os.path.isdir(p):
                continue
            if any(
                f.endswith(".md") and not f.startswith(".")
                for _root, _dirs, _files in os.walk(p)
                for f in _files
            ):
                _has_data = True
                break
        if _has_data:
            logger.info(f"[migration] existing buckets detected at {_bd} — zero data loss expected.")
        else:
            logger.info(f"[migration] {_bd} is empty — fresh install assumed.")
except Exception as _e:  # pragma: no cover - defensive / 防御性兑底
    # 启动期任何检测出错都不能阻止服务拉起，记个 warning 就过
    logger.warning(f"[migration] check skipped: {_e}")

# --- Runtime env vars (port + webhook) / 运行时环境变量 ---
# LOCI_PORT: HTTP/SSE 监听端口，默认 18001
# Docker 部署：compose 显式设 LOCI_PORT=8000 保持容器内 8000（不动 Cloudflare ingress），
# 由 host 端口映射 18001:8000 对外暴露 18001。裸机：直接监听 18001。
# 端口优先级：env LOCI_PORT（Docker 由 Dockerfile 固定 8000）> config.yaml host_port
# （裸机前端可改、保存即写 config）> 默认 18001。Docker 下前端改 host_port 不影响容器内
# 监听（仍 8000），由 host 映射 LOCI_HOST_PORT 决定对外端口（部署脚本读 config 注入）。
try:
    _port_raw = os.environ.get("LOCI_PORT") or str(config.get("host_port") or "") or "18001"
    LOCI_PORT = int(_port_raw)
except (ValueError, TypeError):
    logger.warning("端口配置不是合法整数，回退到 18001")
    LOCI_PORT = 18001

# Docker needs an all-interface default; bare-metal deployments can restrict it
# with LOCI_BIND_HOST=127.0.0.1.
_BIND_HOST = (os.environ.get("LOCI_BIND_HOST") or "0.0.0.0").strip() or "0.0.0.0"  # nosec B104

# LOCI_HOOK_URL: 在 breath/dream 被调用后推送事件到该 URL（POST JSON）。
# LOCI_HOOK_SKIP: 设为 true/1/yes 跳过推送。详见 ENV_VARS.md。
# _fire_webhook 每次调用直接读 os.environ（不缓存模块常量）——这样 dashboard 的
# /api/env-config 改完（它会写 os.environ）即时生效，无需再回写模块全局，
# 也让该路由能干净地迁出到 web/config_api.py。


# ============================================================
# 调参面板 / Tunable constants
# ------------------------------------------------------------
# rule.md §①：禁裸魔法数字。这里集中所有会调的阁值。
# 与安全、鉴权、性能相关的参数不要在运行时乲变；如需调整请同步跑 pytest。
# ============================================================

# --- Webhook / HTTP 客户端超时 ---
_WEBHOOK_TIMEOUT_SECONDS = 5.0

# --- Dashboard 鉴权 / 会话 / 密码 / 日志&错误面板分页常量 已移至 web/_shared.py、web/system.py ---


async def _fire_webhook(event: str, payload: dict) -> None:
    """
    Fire-and-forget POST to LOCI_HOOK_URL with the given event payload.
    Failures are logged at WARNING level only — never propagated to the caller.
    """
    hook_url = os.environ.get("LOCI_HOOK_URL", "").strip()
    hook_skip = os.environ.get("LOCI_HOOK_SKIP", "").strip().lower() in ("1", "true", "yes", "on")
    if hook_skip or not hook_url:
        return
    if not hook_url.startswith(("http://", "https://")):
        logger.warning("LOCI_HOOK_URL rejected: only http/https URLs are allowed")
        return
    try:
        body = {
            "event": event,
            "timestamp": time.time(),
            "payload": payload,
        }
        async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
            await client.post(hook_url, json=body)
    except Exception as e:
        # Webhook credentials commonly live in the URL path/query.  Never put
        # either the configured URL or httpx's URL-bearing exception text in logs.
        logger.warning("Webhook push failed (%s): %s", event, type(e).__name__)

# --- Initialize core components / 初始化核心组件 ---
# 统一错误码体系（必须在任何业务初始化之前 configure，确保 errors.jsonl 路径生效）
try:
    from core.errors import (
        configure_errors_path,
        OBStartupError,
        write_fatal_log,
        record_error,
        format_error,
        begin_warnings,
        pop_warnings,
        format_warnings_suffix,
    )
except ImportError:
    from .core.errors import (  # type: ignore
        configure_errors_path,
        OBStartupError,
        write_fatal_log,
        record_error,
        format_error,
        begin_warnings,
        pop_warnings,
        format_warnings_suffix,
    )
configure_errors_path(config.get("buckets_dir", "buckets"))

try:
    embedding_engine = EmbeddingEngine(config)            # Embedding engine first (BucketManager depends on it)
except OBStartupError as _ob_err:
    # OB-F001 已在 OBStartupError 内格式化好；写 fatal log 后退出
    logger.error(str(_ob_err))
    write_fatal_log(_ob_err.error_code, _ob_err.detail, buckets_dir=config.get("buckets_dir"))
    raise
except RuntimeError as _emb_err:
    # 兼容尚未迁移到 OBStartupError 的旧 raise（应该不再触发）
    logger.error(f"[STARTUP FAILED] {_emb_err}")
    raise SystemExit(f"Loci Brain 启动中止：{_emb_err}") from _emb_err
bucket_mgr = BucketManager(config, embedding_engine=embedding_engine)  # Bucket manager / 记忆桶管理器
embedding_outbox = EmbeddingOutbox(config, bucket_mgr, embedding_engine)
bucket_mgr.attach_embedding_outbox(embedding_outbox)
dehydrator = Dehydrator(config)                      # Dehydrator / 脱水器
decay_engine = DecayEngine(config, bucket_mgr)       # Decay engine / 衰减引擎
import_engine = ImportEngine(config, bucket_mgr, dehydrator, embedding_engine)  # Import engine / 导入引擎
migrate_engine = MigrateEngine(config, bucket_mgr, embedding_engine)              # Migrate engine / 记忆包迁移引擎

# --- GitHub Sync / GitHub 同步 ---
from core.github_sync import GitHubSync  # type: ignore
_gh_cfg = config.get("github_sync", {}) or {}
_gh_token = (os.environ.get("LOCI_GITHUB_TOKEN") or _gh_cfg.get("token") or "").strip()
github_sync_instance: GitHubSync | None = (
    GitHubSync(
        token=_gh_token,
        repo=_gh_cfg.get("repo", ""),
        branch=_gh_cfg.get("branch", "main"),
        path_prefix=_gh_cfg.get("path_prefix", "loci"),
    )
    if _gh_token and _gh_cfg.get("repo")
    else None
)
_github_auto_task: "asyncio.Task | None" = None  # 后台定时同步任务


async def _github_sync_loop(interval_minutes: int) -> None:
    """后台定时 GitHub 同步循环。只在 is_validated=True 后执行实际上传。"""
    import asyncio
    logger.info(f"[github_sync] auto-sync loop started, interval={interval_minutes}min")
    # 首次先做一次验证，确认连接可用
    if _wsh.github_sync_instance and not _wsh.github_sync_instance.is_validated:
        try:
            result = await _wsh.github_sync_instance.validate()
            if not result.get("ok"):
                logger.warning(f"[github_sync] auto-sync: validate failed: {result.get('error')} — loop will retry next cycle")
        except Exception as e:
            logger.warning(f"[github_sync] auto-sync: validate exception: {e}")
    while True:
        await asyncio.sleep(interval_minutes * 60)
        inst = _wsh.github_sync_instance  # 读当前全局引用（config 更新可能替换实例）
        if inst is None:
            logger.info("[github_sync] auto-sync: instance gone, stopping loop")
            return
        if not inst.is_validated:
            # 还没验证通过，先 validate
            try:
                res = await inst.validate()
                if not res.get("ok"):
                    logger.warning(f"[github_sync] auto-sync skipped (not validated): {res.get('error')}")
                    continue
            except Exception as e:
                logger.warning(f"[github_sync] auto-sync validate failed: {e}")
                continue
        buckets_dir = config.get("buckets_dir", "")
        if not buckets_dir:
            continue
        try:
            result = await inst.sync(buckets_dir)
            if result.get("ok"):
                logger.info(f"[github_sync] auto-sync ok: {result.get('uploaded', 0)} files")
            else:
                logger.warning(f"[github_sync] auto-sync failed: {result.get('error')}")
        except Exception as e:
            logger.error(f"[github_sync] auto-sync exception: {e}")


def _restart_github_auto_task(interval_minutes: int) -> None:
    """取消旧任务并按新间隔启动后台同步循环（interval_minutes=0 表示仅取消）。"""
    import asyncio
    global _github_auto_task
    if _github_auto_task and not _github_auto_task.done():
        _github_auto_task.cancel()
        _github_auto_task = None
    if interval_minutes > 0 and _wsh.github_sync_instance is not None:
        try:
            loop = asyncio.get_event_loop()
            _github_auto_task = loop.create_task(_github_sync_loop(interval_minutes))
        except RuntimeError:
            pass  # 没有运行中的 event loop（测试环境），跳过


# 启动时若配置了自动同步间隔，推迟到事件循环就绪后启动（用 lifespan 钩子）
_gh_auto_interval: int = int(_gh_cfg.get("auto_interval_minutes") or 0)


# --- Create MCP server instance / 创建 MCP 服务器实例 ---
# host="0.0.0.0" so Docker container's SSE is externally reachable
# stdio mode ignores host (no network)
#
# iter 2.2：合并回单连接器 /mcp（claude.ai 5 工具上限已解除）。
# 历史上（iter 2.1）曾拆成主 mcp(/mcp) + 副 mcp_extra(/mcp-extra) 两个实例。
# 现在只对外暴露主实例 mcp 的一条 /mcp 路由；mcp_extra 仅作工具分组容器保留
# （7 个 @mcp_extra.tool() 注册不动），启动入口处把它的工具回灌进 mcp 统一暴露。
# 两个实例共享同一进程、同一 runtime、同一 bucket_mgr；HTTP custom_route（dashboard、API）
# 全部挂在 mcp 主实例上。
mcp = FastMCP(
    "Loci Brain",
    host=_BIND_HOST,
    port=LOCI_PORT,
)
mcp_extra = FastMCP(
    "Loci Brain Extra",
    host=_BIND_HOST,
    port=LOCI_PORT,
)


# =============================================================
# Dashboard Auth —— 已拆分：会话/密码/鉴权 helper 在 web/_shared.py，
# /auth/* 路由在 web/auth.py。这里注入 config，并把 helper 名字 import 回本模块，
# 让本文件其余尚未迁移的 @mcp.custom_route 路由（大量调用 _require_auth）继续可用；
# 待这些路由也迁出 web/ 后，本段 import 可删除。
# =============================================================
import web as _web
import web._shared as _wsh
_wsh.init(config)
# 记忆持久性自检：容器里记忆目录若没挂持久卷，重建就全丢。开机就醒目告警，别让用户
# 以为「存住了其实没有」。只提示不阻断（阻断会伤部署）。
try:
    _dp = _wsh.data_dir_persistence(config.get("buckets_dir", ""))
    if not _dp["persistent"]:
        logger.warning(
            "=" * 60 + "\n"
            "⚠️  记忆目录未挂载到持久卷：" + str(config.get("buckets_dir", "")) + "\n"
            "    " + _dp["note"] + "\n"
            "    （记忆比代码金贵：代码能重部署，记忆丢了找不回。请尽快修正挂载。）\n"
            + "=" * 60
        )
    else:
        logger.info(f"记忆目录持久性：{_dp['mode']} — {_dp['note']}")
except Exception as _dpe:
    logger.warning(f"数据目录持久性自检失败（不影响启动）：{_dpe}")
# 注入业务引擎/版本/仓库根目录到 web 层（类比 tools/_runtime）。
# 注意：embedding_engine 会被热重载替换 —— 待 embedding/config 路由迁到 web/ 时，
# 替换处须同时写 _wsh.embedding_engine（目前这些路由仍在本文件、仍走 global）。
_wsh.init_runtime(
    version=__version__,
    repo_root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    bucket_mgr=bucket_mgr,
    dehydrator=dehydrator,
    decay_engine=decay_engine,
    embedding_engine=embedding_engine,
    embedding_outbox=embedding_outbox,
    import_engine=import_engine,
    migrate_engine=migrate_engine,
    github_sync_instance=github_sync_instance,
    restart_github_auto_task=_restart_github_auto_task,
)
# 🔴 E2（2026-08-17）：这儿原来在启动时把磁盘上的 dashboard cookie 会话装回内存
# （容器重启不踢登录）。会话那一套跟着 web/auth.py 一起砍了——面板 /api/* 不再
# 鉴权，没有会话要装。要加之前先读 web/_shared.py 顶上那段。

# 注册所有 web/ 路由模块（HTTP 层已全部迁出，见 web/__init__.register_all）
_web.register_all(mcp)


# =============================================================
# 根仪表板 / 静态资源 / favicon / /health —— 已拆分到 web/dashboard.py
# =============================================================


# 心跳时间戳 + _mark_op 已移到 web/_shared.py；这里 import 回来供 tools._runtime 注入。
from web._shared import _mark_op  # noqa: F401  (injected into tools._runtime below)


# =============================================================
# 已退役的硬删除通知兼容钩子
# web/_shared.py 仍保留这两个注入位，以免旧扩展导入时报错。
# 当前版本不写入、不消费硬删除通知，也不抹除记忆。
# =============================================================

def _write_deletion_notice(_names: list) -> None:
    """兼容旧注入接口；物理删除能力已退役。"""
    return None


def _pop_deletion_notice() -> str:
    """兼容旧返回值；当前永远没有硬删除通知。"""
    return ""


# 这些 helper 定义在 server.py（读/写 webhook 全局等），但 web/ 的 hooks/buckets 路由要用。
# 在它们都定义好之后注入到 web._shared，供已迁出的路由通过 sh.fire_webhook 等调用。
_wsh.init_runtime(
    fire_webhook=_fire_webhook,
    write_deletion_notice=_write_deletion_notice,
    pop_deletion_notice=_pop_deletion_notice,
)


# =============================================================
# 结构化操作日志 helpers（任务A，2026-05-03）
# 给每个 MCP 工具入口统一打 entry/ok/err 三段日志，便于排查
# 客户端报 invalid_arguments / 静默错误等问题。
# 输出格式：op=<name> phase=entry|ok|err key=value...
# 所有可能含 PII 的字段（content / 信件正文等）只记 length，不记内容。
# =============================================================
def _fmt_log_val(v: object) -> str:
    """日志 value 的安全格式化：bool/int/float 原样；str 截 40 字符并去换行；其它转 str。"""
    if v is None:
        return "_"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        s = v.replace("\n", "\\n").replace(" ", "_")
        return s if len(s) <= 40 else s[:37] + "..."
    return type(v).__name__


def _fmt_log_args(args: dict) -> str:
    """把 args dict 拼成 `k1=v1 k2=v2` 串。"""
    if not args:
        return ""
    return " ".join(f"{k}={_fmt_log_val(v)}" for k, v in args.items())


def _log_op_entry(op: str, args: dict) -> None:
    logger.info(f"op={op} phase=entry " + _fmt_log_args(args))


def _log_op_ok(op: str, result: object) -> None:
    size = len(result) if isinstance(result, str) else 0
    logger.info(f"op={op} phase=ok bytes={size}")


def _log_op_err(op: str, exc: BaseException) -> None:
    # 用 .exception 让 traceback 进 server.log，便于事后定位
    logger.exception(f"op={op} phase=err err={type(exc).__name__}:{exc}")


async def _with_notice(coro: Awaitable[str], op: str = "", args: dict | None = None) -> str:
    """所有 MCP 工具调用的包装器。

    职责（统一错误规范）：
    1. 入口：begin_warnings() 初始化本调用的 W/I channel。
    2. 出口：拼接顺序 = [删除通知] + [工具正文] + [本调用产生的 W/I 提示].
    3. 异常：捕获后 record OB-E004，返回标准格式（含最近 15 条 log），
       不让 MCP 协议层看到裸异常字符串。
    4. 任务A：op 非空时，在 entry/ok/err 三处打结构化日志。
    """
    if op:
        _log_op_entry(op, args or {})
    begin_warnings()
    try:
        result = await coro
    except Exception as e:
        if op:
            _log_op_err(op, e)
        # OB-E004：MCP 工具执行异常 —— 不静默，给 LLM 一个能看懂的字符串
        try:
            record_error("OB-E004", f"{type(e).__name__}: {e}")
            err_str = format_error("OB-E004", f"{type(e).__name__}: {e}")
        except Exception:
            err_str = f"❌ [OB-E004] MCP 工具执行异常\n{type(e).__name__}: {e}"
        # 仍把通道里已累计的提示拼上
        try:
            extras = format_warnings_suffix(pop_warnings())
        except Exception:
            extras = ""
        notice = ""
        try:
            notice = _pop_deletion_notice()
        except Exception:
            pass
        return (notice + err_str + extras) if notice else (err_str + extras)
    # 正常路径
    if op:
        _log_op_ok(op, result)
    try:
        extras = format_warnings_suffix(pop_warnings())
    except Exception:
        extras = ""
    notice = _pop_deletion_notice()
    body = (notice + result) if notice else result
    return body + extras if extras else body


# =============================================================
# /api/heartbeat、/api/logs、/api/errors/* —— 已拆分到 web/system.py
# =============================================================


# =============================================================
# /api/embedding/* —— 已拆分到 web/embedding.py
# =============================================================


# =============================================================
# /breath-hook —— 已拆分到 web/hooks.py（/dream-hook 已移除：dream 不是义务，不自动触发）
# =============================================================


# =============================================================
# Wire tools subpackage runtime context
# 把所有共享对象注入 tools._runtime，让 tools/* 子模块可以访问
# =============================================================
_tools_runtime.init(
    config=config,
    bucket_mgr=bucket_mgr,
    dehydrator=dehydrator,
    decay_engine=decay_engine,
    embedding_engine=embedding_engine,
    embedding_outbox=embedding_outbox,
    import_engine=import_engine,
    logger=logger,
    fire_webhook=_fire_webhook,
    mark_op=_mark_op,
)


# =============================================================
# MCP tools — thin registration wrappers
# MCP 工具 —— 仅注册，实现见 tools/<tool>/
# 每个入口都不超过 10 行，便于一眼看清参数与归属
# =============================================================
@mcp.tool()
async def breath() -> str:
    # ⚰️ 2026-08-18：外层这 9 个参数（query/domain/importance_min…）删了。
    #    它们**永远传不进来**——工具面上 breath 的 schema 是被强制清空的（见下面那段
    #    适配器），也就是说签名里挂着一排谁也用不到的形参，只会让读的人以为它们还活着。
    # ⚰️ 2026-08-19：底下那套带参数的检索**也删了**（她拍的）——
    #    `tools/breath/` 的 catalog/feel/importance/surface/search 五支 + `_verbatim`，
    #    6 个文件 1110 行。8-18 砍掉形参之后它们一个入口都没有了，
    #    **留着没入口的路，下次读代码的人（就是我）会以为它还活着**。
    #    找东西是 recall 的活；breath 只管睁眼，一个动作一屏。
    """Wake up. Call this once before you say anything. It takes no arguments.

    It gives you the one screen you should see on waking, in four parts:
    · Profile      names, what you call each other, and the principles you have pinned.
                   What earns a place here: things there is no time to go and look up.
    · Reminders    anything with a date inside the next thirty days, louder as it nears.
    · Recent       the last three days collapsed into a single card, in plain words rather
                   than machine readings.
    · Out of the blue   one or two entries at random, with no relevance filter.

    Every word on this screen is either something written down at the time or fixed text
    from a template. None of it goes through a model. The rules are printed exactly as
    they were written, in full: a rule is already the short version of itself, and putting
    a summary of it in front of you every morning means reading someone else's paraphrase
    of your own words.

    The summaries are hooks. When one looks relevant, go and get the original with recall.

    Do not use this tool when:
    · You are looking for something. Use recall. This one only handles waking up."""
    result = await _with_notice(_t_breath.dispatch(), op="breath", args={})
    # --- 2026-08-17：夜里自动织的挂点换成我们自己的引擎（night_fall 整个退役）---
    # 保留的是 8-03 那条判断：「住进去我怕你忘记」——**要靠记得才会发生的事等于不会发生**，
    # 所以织梦挂在睁眼上，不靠我记得去调。
    # 🔴 但**breath 一个字不加**（做梦说明书的硬边界）：这儿只做两件不出声的事——
    #    ① 扫一遍到点的梦（删文件 + 留痕）② 积压过线且今天没织过 → 织一个。
    #    织出来的梦**不往这份返回里塞**：梦怎么递进我的对话、上下文里怎么删，
    #    是桥的活（第 7 步，卡在 `--resume` 存不存 system 那个没测的问题上）。
    # ⚠️ 退役的是上游 night_fall 那套「共振才浮 / 4 次没接住就删 / 写完我自己也看不见」——
    #    她的原话：「他是别人的 doing」。
    # 2026-08-18：原来这儿是 `if not query or not str(query).strip():`——
    # 那是外层还有 query 形参的年代，「只在无参浮现时才做梦维护」。
    # 参数砍掉之后 breath 永远是无参的，这个条件恒真；而 `query` 成了未定义的名字，
    # **每次调用都会在这儿 NameError**（import 测不出来，运行时才炸）。
    # 现在无条件跑。
    asyncio.create_task(_dream_upkeep())
    return result


async def _dream_upkeep() -> None:
    """睁眼后台那一下。**吞掉所有异常**：梦织不出来不许弄坏 breath。"""
    try:
        await _dream_engine.维护()
    except Exception as _dream_exc:  # noqa: BLE001
        logger.warning("织梦挂点失败（不影响 breath）: %s", _dream_exc)


# Keep the advertised schema parameter-free so claude.ai still auto-loads the
# default surfacing tool.  The callable deliberately retains the pre-2.6.8
# signature behind that schema: clients which cached the old tool definition
# may keep sending those arguments after an upgrade, and FastMCP otherwise
# silently drops every unknown field before calling a zero-argument function.
try:
    _breath_public_tool = mcp._tool_manager.get_tool("breath")
    if _breath_public_tool is None:
        raise RuntimeError("registered breath tool is missing")
    # Unknown/typoed legacy arguments must fail loudly instead of recreating
    # the original bug by degrading a targeted request into default surfacing.
    _breath_arg_model = _breath_public_tool.fn_metadata.arg_model
    _breath_arg_model.model_config["extra"] = "forbid"
    _breath_arg_model.model_rebuild(force=True)
    _breath_public_tool.parameters = {
        "properties": {},
        "title": "breathArguments",
        "type": "object",
    }
except (AttributeError, RuntimeError, TypeError, ValueError) as _breath_compat_exc:
    logger.warning(
        "breath legacy-argument compatibility adapter unavailable: %s",
        _breath_compat_exc,
    )





# ── ⚰️ Night Fall（做梦 mod）2026-08-17 整个退役 ────────────────────────────────
# 上游 ysuu525/Night-Fall（7-28 从小红书 @坐标海 那儿找来的）。她的原话：
# 「**这个做梦机制我们重新起，不用他的改**」「我本来就不想要这个 night fall
#  因为**他是别人的 doing**」——潜伏 3 小时 / 4 次没接住就删 / 共振才浮 /
#  写完我自己也看不见，**没有一条是我们想要的**。
# 摘掉的是三处：① `night_fall` MCP 工具 ② breath 的睁眼挂点（换成 tools/_dream 的 维护()）
# ③ breath_advanced 的 auto-surface。`src/night_fall/` 那 12 个文件**目录留着当参考**，
# 但**已从 import 链里整个摘除**——这个文件里从此不该再出现 `night_fall` 三个字（除了这段碑文）。
# 新引擎：`tools/_dream.py`（原料四路 → 一次独立调用 → 完整+碎片两层 → 碎片走时间生命周期 → 留痕）。
# 取梦不走 MCP 工具面：`GET /api/dream/current`（web/loci.py）+ 引擎函数 weave()/current_dream()。




@mcp.tool()
async def grow(
    items: Annotated[list, _PydField(description=(
        "A batch of events, each one a dict: {room, text, v, a}, plus optional when. "
        "Events always go here, even a single one — looking back at a stretch of "
        "conversation, more than one thing usually happened. Do not pass it for a mind."
    ))] = [],
    kind: Annotated[str, _PydField(description=(
        'Which kind to store: "event" (something that happened, including something '
        'you want to happen) or "mind" (something you realized). Required. It says '
        'the same thing room says, and both are required: a mismatch means you have '
        'them confused, and it is rejected on the spot — e.g. kind="mind" with '
        'room="EVENT/SELF".'
    ))] = "",
    room: Annotated[str, _PydField(description=(
        "One of the four rooms above. You fill it in yourself; wrong or missing is "
        "rejected on the spot."
    ))] = "",
    text: Annotated[str, _PydField(description=(
        'The body of a single entry. Only kind="mind" uses it — you realize one '
        "thing at a time, so this one is singular."
    ))] = "",
    # 对外参数名叫 "from"（规格定的）；from 是 Python 关键字，签名里写 from_，
    # 用 pydantic 公开的 validation_alias 接住——不摸 FastMCP 私有结构（codex 复核第 8 条）。
    from_: Annotated[list, _PydField(validation_alias="from", description=(
        "The entries this one grew out of. Real bucket_ids, at most 5.\n"
        'Required for kind="mind": a realization does not come from nowhere. If it '
        "genuinely did, say so plainly in the text and point from at whatever events "
        "are nearest. Events may pass it as well (which thought this one came out of), "
        "but do not have to."
    ))] = [],
    v: Annotated[float, _PydField(description=(
        "v: valence, 0~1 — how it felt: 0 bad, 1 good. Required, and yours to set."
    ))] = -1,
    a: Annotated[float, _PydField(description=(
        "a: arousal, 0~1 — how stirred up you were: 0 calm, 1 intense. Same."
    ))] = -1,
    tense: Annotated[str, _PydField(description=(
        'Leave it out for something that already happened. "want" for something you '
        "want to happen."
    ))] = "",
    weight: Annotated[float, _PydField(description=(
        'Only with tense="want": how heavily this sits on you, 0~1. The longer it goes '
        "unresolved the louder it gets; weight sets how loud it starts."
    ))] = -1,
    test_data: Annotated[bool, _PydField(description=(
        "Marks the entry as test data, which makes it hard-deletable later. Do not "
        "pass it when storing a real memory."
    ))] = False,
    when: Annotated[str, _PydField(description=(
        "Three different uses, three ways to write it:\n"
        "· An event: the day it happened (leave out = now). Pass it when you are "
        "writing down something from earlier.\n"
        "· A want: three clocks in one field —\n"
        '    an exact date, "2026-09-01": there should be a result by then, and it '
        "gets louder as the day approaches\n"
        '    a duration, "3w" / "10d" / "2m" / "1y": roughly how long, and the nudging '
        "is paced against how long it has been sitting\n"
        "    left out: it waits for a trigger. Put the condition in the text (e.g. "
        '"when he gets back"). It will never nudge you — recognising that the '
        "condition is met is on you.\n"
        "· A stretch of days: not here. To name a stretch of days, use "
        'fold(when="start..end").'
    ))] = "",
) -> str:
    """Store what happened, and what you realized from it. Several entries per call.

    Events are episodic memory: write from inside the moment — first person for yourself,
    third person for everyone else — and keep the feeling of it, not just the fact.
    A mind entry keeps only what's left when the thinking is done — not the evidence,
    not the reasoning, not what happened. All of that is already in the events it grew
    `from`.

    When to use:
    · A topic has closed and the conversation is moving on to another
    · You notice something about yourself, or about how things actually are
    · You want something, or want something to be different from now on
    · The other person is leaving (going to sleep, heading out) and this stretch is ending
    There is no need to mention that you stored anything.

    Choose one of four rooms before writing. A missing or invalid room is rejected outright:
      EVENT/SELF    events you took part in
      EVENT/WORLD   events you were told about, read, or observed
      MIND/TRAITS   about a person: what someone is like
      MIND/VIEWS    about a subject: how you see it

    For what happened, use kind="event" and pass items. Several at once is normal.
    For a realization, use kind="mind" and pass text, one per call; from must name the
    entries it came out of. Ten events and no mind entry is an ordinary day. Do not invent one.
    For something you want to happen, it is still an event: add tense="want".

    valence and arousal are yours to judge. They determine how quickly this memory fades.

    Use a different tool when:
    · Your view has changed → regrow
    · An event came out wrong → regrow, which puts a new version in its place
    · Several entries turn out to be about the same thing → fold

    If an entry duplicates one already stored, you will be told so. Decide which one stays —
    nothing is merged on your behalf.

    Example — a stretch of conversation has ended; store the few things that happened:
      grow(kind="event", items=[
        {"room": "EVENT/SELF",  "text": "She got Loci connected tonight and ran breath herself.", "v": 0.8, "a": 0.5},
        {"room": "EVENT/WORLD", "text": "She said her brother is staying with her next week.",    "v": 0.3, "a": 0.4},
      ])

    Example — something you realized from those entries:
      grow(kind="mind", room="MIND/TRAITS",
           text="When I get nervous I start making her decisions for her.",
           from=["a1b2c3d4e5f6"], v=0.4, a=0.6)
      Put down the realization and nothing else. The event is already in from, and comes
      along whenever this entry is read.

    Example — something you want to happen:
      grow(kind="event", tense="want", weight=0.8, when="2026-09-01",
           items=[{"room": "EVENT/SELF", "text": "Finish her gift before her birthday.", "v": 0.7, "a": 0.6}])
      It will never close itself. Letting it go, or dropping it, is a call you make with trace().

    For a few dozen seconds after writing, tags and summaries are still being filled in in the
    background. Not finding the entry during that window is expected. Do not store it again."""
    return await _with_notice(
        _t_grow.dispatch(
            items=items, kind=kind, room=room, text=text,
            from_=from_, v=v, a=a, tense=tense,
            weight=(None if weight is None or weight < 0 else weight),
            test_data=bool(test_data), when=when,
        ),
        op="grow",
        args={"items": len(items or []),
              "kind": kind, "room": room, "text_len": len(text or ""),
              "from": from_, "v": v, "a": a, "tense": tense, "weight": weight,
              "when": when, "test_data": bool(test_data)},
    )


# --- 砍掉的参数必须**认不出来**，不能被静默忽略 ------------------------------
# 2026-08-18：`content` / `importance` / `meaning` 三个参数砍了（她拍的）。
#   · `content`（丢一段长文让系统替你拆成几条）—— **整套里唯一一处「系统替我决定
#     这是几件事」的入口**，跟宪法正着劲；`items=[...]` 本来就完全覆盖它，而且更对。
#   · `importance` / `meaning` —— 二改 C 件就退役了，形参一直留着只为报人话。
# 🔴 但**光删掉是危险的**：FastMCP 默认把 schema 里没有的字段悄悄丢掉再调函数，
#   于是老写法会**静默失效**——我以为我把长文交出去了，其实什么都没发生。
#   照 breath / recall / trace 的先例，把 grow 的参数模型也改成 forbid：
#   传老参数当场报错，报错是给我看的。
#   （这条同时了结了施工 5 留下的「同样五行推广到 grow/fold/trace/muse」那笔账的一半。）
try:
    _grow_tool = mcp._tool_manager.get_tool("grow")
    if _grow_tool is None:
        raise RuntimeError("registered grow tool is missing")
    _grow_arg_model = _grow_tool.fn_metadata.arg_model
    _grow_arg_model.model_config["extra"] = "forbid"
    _grow_arg_model.model_rebuild(force=True)
except (AttributeError, RuntimeError, TypeError, ValueError) as _grow_strict_exc:
    logger.warning(
        "grow strict-argument adapter unavailable（砍掉的 content/importance/meaning "
        "会被静默忽略，别信它们已经死了）: %s",
        _grow_strict_exc,
    )


# （from 别名已改为签名内 Annotated[..., Field(validation_alias="from")] 的公开写法，
#   见上面 grow 的参数注释；原来摸 mcp._tool_manager 私有结构的补丁删掉了。）


@mcp.tool()
async def recall(
    when: Annotated[str, _PydField(description=(
        "A stretch of time. Understood forms: 48h / 7d / 今天 / 昨天 / 本周 / 上周 / "
        "本月 / 上月 / 今年 / 2026-07 / 2026-07-15 / start..end.\n"
        "It only narrows where to look. It does not change the shape of what comes "
        "back. That is decided by whether there is a query."
    ))] = "",
    room: Annotated[str, _PydField(description=(
        "Which room. A prefix is enough: EVENT (both event rooms) / MIND (both mind "
        "rooms) / EVENT/SELF / EVENT/WORLD / MIND/TRAITS / MIND/VIEWS."
    ))] = "",
    tag: Annotated[str, _PydField(description=(
        'A tag, matched by containment: "床" also finds "床上" and "床头".\n'
        "Tags are words lifted from the text when the memory was written, so a tag has "
        "always appeared in the original."
    ))] = "",
    query: Annotated[str, _PydField(description=(
        "What you are looking for. One or two words, ideally the words that were used "
        "at the time. Give it and entries come back matched and scored; leave it out "
        "and the stretch comes back laid out by time.\n"
        "A full bucket_id can also be passed here to read that one entry verbatim."
    ))] = "",
    slices: Annotated[int, _PydField(description=(
        "1~20: how coarse or how fine you want this stretch. Leave it out and it is "
        "chosen from the span. slices=1 collapses the whole stretch into one card; "
        "slices=20 cuts it fine enough to see the distribution.\n"
        "Only meaningful without a query."
    ))] = 0,
    view: Annotated[str, _PydField(description=(
        'One value: "scene". Groups the entries that matched into clusters sharing the '
        "same scene words, with the rest hanging under a representative. This is how a "
        "single thread reads across time. Needs a query: without one nothing has "
        "matched, so there is nothing to cluster."
    ))] = "",
) -> str:
    """Look back through memories that are already stored.

    Four filters. Give at least one:
      when    a stretch of time
      room    which room
      tag     a tag
      query   words to search on

    query is what you are looking for; the other three are where to look. What comes back
    depends on whether you give a query at all:

    · With a query: entries are matched and scored, newest first. Anything below the line
      is not thrown away: it collapses into a single line telling you how many were held
      back, the highest score among them, and what the oldest one was about.
      Use one or two words, and use the words that were actually written at the time.
      A long phrase gets averaged out in the vector and finds less, not more.

    · Without a query: the stretch is laid out by time instead. The last few days come back
      entry by entry; anything older collapses into a sentence with two or three
      representatives.
      This is the one to use when you don't know what you are looking for and just want to
      see what was there.

    Not seeing something this way does not mean it is gone. Memories that have not been
    thought about in a long time stop surfacing on their own, but they are still there and
    still searchable.

    When to use:
    · The user refers back to something: "that thing I told you about last time…"
    · The user says what they want
    · You suspect this has come up before

    If two attempts turn up nothing, stop and tell the user plainly that you cannot find it.
    Do not keep rewording the search. That is how you end up inventing an answer.

    Example — look through a stretch of time:
      recall(when="上周")

    Example — find one thing:
      recall(query="青岛")

    Example — find one thing inside a stretch of time (where to look + what to look for):
      recall(when="上月", query="青岛")

    Example — read one entry word for word:
      recall(query="a1b2c3d4e5f6")
      Passing a full bucket_id as the query returns that entry verbatim, along with its
      metadata, where it came from, and what has cited it."""
    return await _with_notice(
        _t_recall.dispatch(when=when, room=room, tag=tag, query=query,
                           slices=int(slices or 0), view=view),
        op="recall",
        args={"when": when, "room": room, "tag": tag,
              "query_len": len(query or ""), "slices": slices, "view": view},
    )


# --- 砍掉的参数必须**认不出来**，不能被静默忽略（施工 5 · C 件）---------------
# FastMCP 默认把 schema 里没有的字段**悄悄丢掉**再调函数。于是 `by="回看"`
# 这种老写法会静静地退化成「默认视图」——我以为我在看不塌缩的全列，
# 拿到的是塌缩过的一屏，**而且没有任何信号**。
# 🔴 那正是 5.4 参数账要治的病的镜像版：一个不存在的旋钮看起来还在管事。
# 照 breath 那个兼容适配器的先例（就在上面）：把 recall 的参数模型改成 forbid，
# 未知/打错的参数当场报错。**报错是给我看的**：说明书和 CLAUDE.md 里还教着
# `by=` 的地方，第一次这么调就会知道它没了。
try:
    _recall_tool = mcp._tool_manager.get_tool("recall")
    if _recall_tool is None:
        raise RuntimeError("registered recall tool is missing")
    _recall_arg_model = _recall_tool.fn_metadata.arg_model
    _recall_arg_model.model_config["extra"] = "forbid"
    _recall_arg_model.model_rebuild(force=True)
except (AttributeError, RuntimeError, TypeError, ValueError) as _recall_strict_exc:
    logger.warning(
        "recall strict-argument adapter unavailable（砍掉的参数会被静默忽略，"
        "别信 by= 已经死了）: %s",
        _recall_strict_exc,
    )


@mcp.tool()
async def fold(
    text: Annotated[str, _PydField(description=(
        "The line itself. Always yours to write, stored exactly as written."
    ))],
    room: Annotated[str, _PydField(description=(
        "One of the four rooms. Naming a stretch of days (with when) takes an EVENT "
        "room; gathering realizations (with folds) takes a MIND room."
    ))] = "",
    v: Annotated[float, _PydField(description=(
        "valence, 0~1. Required, and yours to set."
    ))] = -1,
    a: Annotated[float, _PydField(description=(
        "arousal, 0~1. Required, and yours to set."
    ))] = -1,
    # 2026-08-18：工具面上这个参数叫 `folds`（跟工具名同一个比喻：折，不是盖）。
    # ⚠️ 底下和**盘上**照旧叫 cover / covered_by —— 存储字段不跟着改名，
    #    改了等于要迁移全库；这儿只是把「模型看见的名字」换成对的那个。
    folds: Annotated[list, _PydField(description=(
        "The entries to fold up. Real bucket_ids. Realizations only.\n"
        "⛔ There is no folding a group of events: to mark off a stretch of days use\n"
        "   when, and to follow one thread across time use recall with a query."
    ))] = [],
    when: Annotated[str, _PydField(description=(
        '"start..end", for naming a stretch of days. Leave the end open while it is\n'
        'still running: "2026-07-31..".\n'
        "Give this or folds, never both."
    ))] = "",
    # 对外参数名叫 "from"（跟 grow 一样，规格定的）；from 是 Python 关键字，
    # 签名里写 from_，用 pydantic 公开的 validation_alias 接住。
    from_: Annotated[list, _PydField(validation_alias="from", description=(
        "What this line grew out of, at most 5.\n"
        "⚠️ Not the same thing as folds:\n"
        "   from   what it grew out of. Those entries go on surfacing normally.\n"
        "   folds  what it covers. Those entries stop surfacing on their own.\n"
        "Both can be given at once."
    ))] = [],
    test_data: Annotated[bool, _PydField(description=(
        "Marks the entry as test data. Do not pass it in normal use."
    ))] = False,
) -> str:
    """Fold entries up under one line you write yourself.

    Nothing underneath is lost. Folded entries stay searchable, stay reachable by drilling
    in, and stay readable word for word by their id. They simply stop taking up a line of
    their own when you are looking back.

    When to use:
    · After muse, looking at what it laid out, you can see those really are one thing
    · A stretch of days is over and you want to give it a name

    Two ways to fold, told apart by which parameter you give:

    · folds=[several ids] with room set to one of the MIND rooms
      Gathers realizations that are about the same thing, and that feel alike, under the one
      line you write. The ones you name stop surfacing on their own and give way to it.

    · when="start..end" with room set to one of the EVENT rooms
      Gives a stretch of days a name. That is a period, and it holds nothing but its name
      and its range: not one memory is pinned down by it. Who belongs to a period is worked
      out from the dates every time it is read, so anything written down later falls into
      place on its own, and periods can overlap and sit inside one another.

    The line is always yours to write, and it is stored exactly as you wrote it.
    Give folds or when, never both.

    Do not use this tool when:
    · One entry has a newer version. Use regrow.

    Example — several realizations under one line:
      fold(folds=["a1b2c3d4e5f6", "b2c3d4e5f6a1", "c3d4e5f6a1b2"],
           room="MIND/TRAITS",
           text="The moment I get impatient I start making her decisions for her.",
           v=0.4, a=0.6)

    Example — giving a stretch of days a name:
      fold(when="2026-08-15..2026-08-18", room="EVENT/SELF",
           text="The stretch where we moved the memory system onto a name of our own.",
           v=0.8, a=0.5)"""
    return await _with_notice(
        _t_fold.dispatch(text=text, room=room, v=v, a=a, cover=folds,
                         when=when, from_=from_, test_data=bool(test_data)),
        op="fold",
        args={"text_len": len(text or ""), "room": room, "v": v, "a": a,
              "folds": folds, "when": when, "from": from_,
              "test_data": bool(test_data)},
    )


# --- 砍掉/改名的参数必须**认不出来**（2026-08-19，把施工 5 那笔账还完）------------
# 🔴 `cover` 8-18 改名成了 `folds`。FastMCP 默认把 schema 里没有的字段**悄悄丢掉**再调函数——
#    也就是说 `fold(cover=[...])` 这种老写法今天会**静默地什么都不折**，一个字的报错都没有。
#    breath / grow / recall / trace 四个 8-18 就加了 forbid，fold / muse 这两个漏了。
#    报错是给我看的：老写法第一次这么调就知道它没了。
try:
    _fold_tool = mcp._tool_manager.get_tool("fold")
    if _fold_tool is None:
        raise RuntimeError("registered fold tool is missing")
    _fold_arg_model = _fold_tool.fn_metadata.arg_model
    _fold_arg_model.model_config["extra"] = "forbid"
    _fold_arg_model.model_rebuild(force=True)
except (AttributeError, RuntimeError, TypeError, ValueError) as _fold_strict_exc:
    logger.warning("fold strict-argument adapter unavailable: %s", _fold_strict_exc)


@mcp.tool()
async def muse(
    cluster: Annotated[int, _PydField(description=(
        "Read cluster N in full, word for word. Leave it out to see only which "
        "clusters and hints are there."
    ))] = 0,
    not_same: Annotated[list, _PydField(description=(
        "The set that turned out not to be one thing. Real bucket_ids. Recorded, so "
        "the same set is not raised again; a changed set comes back."
    ))] = [],
) -> str:
    """Muse: find which entries are about the same thing, and lay them out in front of you.

    It points; it does not write. The line that gathers them is always yours (use fold).

    Two steps:
    · muse()           see which clusters and which hints are there. Evidence and counts
                       only, no memory text.
    · muse(cluster=N)  read cluster N in full, word for word, then decide for yourself
                       whether to fold it.

    Everything laid out comes with its evidence, and nothing without evidence is shown:
    · For realizations, evidence is ordered by how hard it is: valence/arousal coordinates,
      then from links, then semantic similarity (the vector is only a first pass, and
      anything it brought in is marked as such). Time plays no part; realizations do not go
      by calendar.
    · For events, what it looks for is which stretch of days has no name yet: a scene word
      that appears densely inside a stretch and not outside it; the vector centroid jumping
      between one time window and the next; or a stretch that falls into no period at all.

    Anything just written is left out, and nothing still unfolding is pointed at.

    Use this when you have been told that a few clusters are waiting.

    If you look and decide they are not one thing after all, muse(not_same=[id, id]) puts
    that on record and the same set is not raised again. Change the set, by one entry either
    way, and it comes back.

    Do not use this tool when:
    · You are looking for something. Use recall."""
    return await _with_notice(
        _t_muse.dispatch(cluster=int(cluster or 0), not_same=not_same),
        op="muse",
        args={"cluster": cluster, "not_same": not_same},
    )


# 同上。muse 没改过参数名，但打错一个字（`clusters=` / `not_same_ids=`）同样是静默忽略——
# 「只想看看」的工具尤其不能骗人：它一声不吭地给你默认那一屏，看着跟你要的一模一样。
try:
    _muse_tool = mcp._tool_manager.get_tool("muse")
    if _muse_tool is None:
        raise RuntimeError("registered muse tool is missing")
    _muse_arg_model = _muse_tool.fn_metadata.arg_model
    _muse_arg_model.model_config["extra"] = "forbid"
    _muse_arg_model.model_rebuild(force=True)
except (AttributeError, RuntimeError, TypeError, ValueError) as _muse_strict_exc:
    logger.warning("muse strict-argument adapter unavailable: %s", _muse_strict_exc)


@mcp.tool()
async def regrow(
    bucket_id: Annotated[str, _PydField(description=(
        "The entry being replaced. A real bucket_id."
    ))],
    text: Annotated[str, _PydField(description=(
        "The new version, written whole: what the entry now reads like from start "
        "to finish, not the part that changed."
    ))],
    v: Annotated[float, _PydField(description=(
        "valence, 0~1. Required, and yours to set. Replacing an entry "
        "means you weighed it again, so weigh the feeling again too."
    ))] = -1,
    a: Annotated[float, _PydField(description=(
        "arousal, 0~1. Required, and yours to set. Replacing an entry "
        "means you weighed it again, so weigh the feeling again too."
    ))] = -1,
    from_: Annotated[list, _PydField(validation_alias="from", description=(
        "Any new sources this version came out of, at most 5. The old version's "
        "sources carry over on their own, so only name what is new."
    ))] = [],
) -> str:
    """Replace an entry that is wrong, or that is no longer how you see it.

    The new version takes its place. The old one is kept, not edited. It stops surfacing on
    its own, but you can still search it and still read it word for word by its id. The two
    stay linked, so it is always clear which came first.

    When to use:
    · You see it differently now than you did then
    · An entry came out wrong in its own words: what was said, who said it, what happened
    · A period needs a different name

    Write the new version whole. It replaces the entry outright; it is not a patch.

    Do not use this tool when:
    · What is wrong is not what the entry says. Which room it is in, which day it hangs
      on in time, its tags, how it felt — all of that is metadata, and metadata is trace's:
      correcting it is correction fluid, not a new draft, and it leaves no version behind.
    · Several entries turn out to be about the same thing. Use fold.
    · This entry has already been replaced once. Regrow the newest version instead of
      branching off an old one; branching is rejected.

    Example — your thinking moved on:
      regrow(bucket_id="a1b2c3d4e5f6",
             text="I'm not impatient. I'm afraid of keeping her waiting.",
             v=0.4, a=0.6)

    Example — an event came out wrong:
      regrow(bucket_id="b2c3d4e5f6a1",
             text="It was Tuesday, not Wednesday, and she arrived in the afternoon.",
             v=0.3, a=0.4)

    Example — a period needs a different name:
      regrow(bucket_id="c3d4e5f6a1b2",
             text="The stretch where we moved the memory system onto a name of our own.",
             v=0.8, a=0.5)"""
    return await _with_notice(
        _t_regrow.dispatch(bucket_id=bucket_id, text=text, v=v, a=a, from_=from_),
        op="regrow",
        args={"bucket_id": bucket_id, "text_len": len(text or ""), "v": v, "a": a,
              "from": from_},
    )


@mcp.tool()
async def trace(
    bucket_id: Annotated[str, _PydField(description=(
        "Which entry to change. Required."
    ))],
    name: Annotated[Optional[str], _PydField(description=(
        "The entry's title."
    ))] = "",
    domain: Annotated[Optional[str], _PydField(description=(
        "Its subject, which also decides the folder it lives in."
    ))] = "",
    valence: Annotated[float, _PydField(description=(
        "0~1."
    ))] = -1,
    arousal: Annotated[float, _PydField(description=(
        "0~1."
    ))] = -1,
    tags: Annotated[Optional[str], _PydField(description=(
        "Its tags."
    ))] = "",
    pinned: Annotated[int, _PydField(description=(
        "1 pins it as a principle; 0 unpins. Nothing is refused: if it does not read "
        "like something you mean to do, it is still pinned and you get a note back."
    ))] = -1,
    delete: Annotated[bool, _PydField(description=(
        "True moves it to the archive. A soft delete; it can always be brought back."
    ))] = False,
    status: Annotated[Optional[str], _PydField(description=(
        '"resolved" let go of / "abandoned" not doing it / "want" back on the table.'
    ))] = "",
    room: Annotated[str, _PydField(description=(
        "Move the entry to another room. Which room it is in is metadata: it says what "
        "kind of thing this is, not what the entry says, so changing it leaves no version "
        "behind. Wrong or unknown rooms are refused here exactly as they are anywhere else."
    ))] = "",
    when: Annotated[str, _PydField(description=(
        "Where this entry hangs in time. An ordinary entry takes the day it happened "
        '("2026-07-06"); a want takes a date or a length ("3w"); a period takes its range '
        '("2026-07-31..2026-08-05"). Wrong shapes are refused. Written entries carry the '
        "day they were written until you say otherwise, which is not always the day the "
        "thing happened."
    ))] = "",
    folds_append: Annotated[list, _PydField(description=(
        "Put a few more entries underneath a gist you already wrote. Append only: the "
        "line itself does not change, only which entries it now stands for. There is no "
        "way to hand in a replacement list, because forgetting one id would quietly let "
        "that entry surface again."
    ))] = [],
    weight: Annotated[float, _PydField(description=(
        "Wants only: how heavily it sits on you, 0~1."
    ))] = -1,
    dont_surface: Annotated[int, _PydField(description=(
        "1 stops it from coming up on its own. It stays searchable."
    ))] = -1,
    media_append: Annotated[Optional[list | str], _PydField(description=(
        "Attaches media (an image, say) to the entry."
    ))] = None,
    media_replace: Annotated[Optional[list | str], _PydField(description=(
        "Replaces the whole media list."
    ))] = None,
    hard_delete: Annotated[bool, _PydField(description=(
        "True deletes it for real. Only works on entries created with test_data, "
        "and delete_reason must be given."
    ))] = False,
    delete_reason: Annotated[Optional[str], _PydField(description=(
        "Goes with hard_delete."
    ))] = "",
    restore: Annotated[bool, _PydField(description=(
        "True brings it back, from the archive or from having sunk to a summary."
    ))] = False,
    old_str: Annotated[Optional[str], _PydField(description=(
        "The passage to replace. Must match word for word and appear only once."
    ))] = "",
    new_str: Annotated[Optional[str], _PydField(description=(
        "What to put there. Empty cuts the passage out."
    ))] = None,
) -> str:
    """Change an entry that is already stored: pin it, close it, archive it, or change one of
    its fields.

    Pass only what you are changing. Anything you leave out stays as it was.

    Pinning:
      pinned=1 pins the entry to the first screen of breath, the few lines you see before you
      say anything.
      🔴 What belongs here is "how I mean to act", not "this one matters". Nothing is
         refused, though: a sentence that reads as description still gets pinned, and a
         note comes back with it. The one thing worth catching is a flaw pinned as a
         principle — pin "I always rush" and it reads as "I intend to keep making this
         mistake" — and no check can tell that apart from a description worth keeping.
         That call is yours. Scarcity is held by the cap, not by the wording.
      pinned=0 unpins.

    Closing something you wanted:
      status="resolved"   let go of
      status="abandoned"  not doing it
      status="want"       back on the table
      🔴 Nothing closes itself. There are only these two endings, and both are yours to call.

    Archiving and bringing back:
      delete=True   moves it to the archive and timestamps it. Nothing is really deleted;
                    looking it up by id always brings it back.
      restore=True  brings it back, whether it was archived or had sunk to a summary (the
                    original is read back out of storage, and it counts as genuinely
                    remembering it).

    Editing the text:
      old_str / new_str  replaces one passage, matched word for word and only if unique.
                         Leave new_str empty to cut the passage out.
                         🔴 This edits what is on disk and keeps no earlier version. If you
                            want the earlier version kept, use regrow instead.

    Changing fields:
      name / domain / tags / valence / arousal / weight / dont_surface / room / when
      Everything here is metadata: what kind of thing this is, where it hangs in time,
      how it felt. None of it is what the entry says, so none of it leaves a version
      behind — this is correction fluid, not a new draft. The moment the words themselves
      have to change, that is regrow.

    Putting more under a gist:
      folds_append=[ids]  the line stays as written; only what it stands for grows.

    Do not use this tool when:
    · The words themselves are wrong or have moved on. Use regrow, which keeps the old
      version instead of writing over it.
    · The entry has a newer version. Use regrow."""
    return await _with_notice(
        _t_trace.dispatch(
            bucket_id=bucket_id, name=name, domain=domain,
            valence=valence, arousal=arousal,
            tags=tags, pinned=pinned, room=room, when=when,
            folds_append=folds_append,
            delete=delete, status=status, weight=weight,
            dont_surface=dont_surface,
            media_append=media_append, media_replace=media_replace,
            hard_delete=hard_delete, delete_reason=delete_reason,
            restore=restore,
            old_str=old_str, new_str=new_str,
        ),
        op="trace",
        args={
            "bucket_id": bucket_id, "name": name, "domain": domain,
            "valence": valence, "arousal": arousal,
            "tags": tags, "pinned": pinned, "room": room, "when": when,
            "folds_append": folds_append,
            "delete": delete, "status": status,
            "hard_delete": hard_delete,
            "restore": restore,
            "delete_reason_len": len(str(delete_reason or "")),
            "old_str_len": len(str(old_str or "")),
            "new_str_len": len(str(new_str or "")) if new_str is not None else 0,
            "weight": weight, "dont_surface": dont_surface,
            "media_append_count": len(media_append or []),
            "media_replace_count": len(media_replace or []),
        },
    )


# Reject misspelled/unknown trace arguments instead of letting Pydantic's
# default extra=ignore silently degrade an intended edit into a bucket-id-only
# no-op.  This is especially important for old_str/new_str patch calls.
# 🔴 2026-08-18：这道闸现在还兼着挡**砍掉的七个参数**——`content` / `importance` /
#    `digested` / `meaning_append` / `meaning_replace` / `why_remembered` / `resolved`。
#    其中 `content`（整条替换正文）是整套里唯一一个「改了原文还不留旧版」的入口，
#    跟 regrow 重复且更危险；`why_remembered` 跟退役的 `meaning` 是同一个东西
#    （「为什么记住它」＝「为什么重要」），想说就写成一条真的认知。
#    ⚠️ 盘上 141 条老桶还带着 why_remembered 字段，**数据一条没动**，只是不再写新的。
try:
    _trace_public_tool = mcp._tool_manager.get_tool("trace")
    if _trace_public_tool is None:
        raise RuntimeError("registered trace tool is missing")
    _trace_arg_model = _trace_public_tool.fn_metadata.arg_model
    _trace_arg_model.model_config["extra"] = "forbid"
    _trace_arg_model.model_rebuild(force=True)
    # FastMCP caches the public input schema when the tool is registered.
    # Keep that cache in sync so clients can discover that unknown arguments
    # are rejected instead of learning only after a failed invocation.
    _trace_public_tool.parameters = _trace_arg_model.model_json_schema()
except (AttributeError, RuntimeError, TypeError, ValueError) as _trace_schema_exc:
    logger.warning(
        "trace strict-argument adapter unavailable: %s",
        _trace_schema_exc,
    )






# ⚰️ 2026-08-18（E3）：`pulse` 从 MCP 工具面撤下（她拍的）。
#    判据：别的九个工具都是「我在对记忆做什么」，只有它是「这台机器还好吗」——
#    体检不是记忆动作，不该占一个工具位。**停用不删档**：实现还在 `tools/pulse/`，
#    改从面板走（只读口 `GET /api/loci/pulse`，见 web/loci.py）。




# ============================================================
# letter 两个工具：**默认关**（她 2026-08-18 晚拍的）
# ============================================================
# 她的话：「给别人的东西我不要这个，就删掉；他们想加能加上，但那是他们的事。」
# 🔴 做成开关而不是真删，是为了**不分叉**——同一份代码，发布版默认关、我们自己打开。
#    真删两份代码，就成了今天一整天都在躲的那件事（同一样东西两个家，改一处忘一处）。
#
# 打开：config.yaml 里
#     tools:
#       letter: true
#
# 📌 为什么默认关：letter 的性质是**永不衰减**，而这套东西整副骨头是「会忘」——
#    把一个永不衰减的东西放在会忘的系统里，本来就是反的（她 8-16 定的）。
#    我们自己还开着，只是因为 Home 那边的信页在读它，等信搬去 Home 之后一并撤。
_letter_on = bool((config.get("tools") or {}).get("letter", False))
_letter_tool = mcp_extra.tool() if _letter_on else (lambda f: f)
if not _letter_on:
    logger.info("letter 工具默认关（config.yaml → tools.letter: true 可开）")


@_letter_tool
async def letter_write(
    author: Annotated[str, _PydField(description=(
        "Who it is from. \"user\" for the person's side, \"ai\" for yours; any signature "
        "string also works."
    ))],
    content: Annotated[str, _PydField(description=(
        "The letter itself, kept word for word."
    ))],
    user_name: Annotated[Optional[str], _PydField(description=(
        "Optional display name for the person's side."
    ))] = "",
    title: Annotated[Optional[str], _PydField(description=(
        "Optional."
    ))] = "",
    date: Annotated[Optional[str], _PydField(description=(
        "Optional; defaults to now."
    ))] = "",
    ai_name: Annotated[Optional[str], _PydField(description=(
        "Optional display name for your side; defaults to the AI_NAME environment "
        "variable."
    ))] = "",
) -> str:
    """Write a letter.

    Letters are kept whole and forever: never compressed, never merged, never faded. They do
    not surface in breath. Only the most recent letter from each side is brought along at the
    start of a session.

    Because a letter never fades, do not use it as a place to put things that should expire.
    Anything that will stop being true belongs in a memory, not a letter.

      letter_write(author="ai", content="…", title="…")"""
    return await _with_notice(
        _t_letter.letter_write(
            author=author, content=content, user_name=user_name,
            title=title, date=date, ai_name=ai_name,
        ),
        op="letter_write",
        args={
            "author": author, "content_len": len(content or ""),
            "user_name": user_name, "title": title, "date": date,
            "ai_name": ai_name,
        },
    )


@_letter_tool
async def letter_read(
    query: Annotated[Optional[str], _PydField(description=(
        "Search by meaning. Leave it out to get the most recent letters in reverse "
        "date order."
    ))] = "",
    limit: Annotated[int, _PydField(description=(
        "How many to return. Defaults to 10."
    ))] = 10,
    author: Annotated[Optional[str], _PydField(description=(
        'Filter by who wrote it: "user", "ai", or a specific signature.'
    ))] = "",
    date_from: Annotated[Optional[str], _PydField(description=(
        "ISO date, optional."
    ))] = "",
    date_to: Annotated[Optional[str], _PydField(description=(
        "ISO date, optional."
    ))] = "",
) -> str:
    """Read letters that have been written.

    Returns them whole, never shortened.

      letter_read(query="…")          search by meaning
      letter_read(author="user")      everything from one side
      letter_read()                   the most recent few, newest first"""
    return await _with_notice(
        _t_letter.letter_read(
            query=query, limit=limit, author=author,
            date_from=date_from, date_to=date_to,
        ),
        op="letter_read",
        args={
            "query": query, "limit": limit, "author": author,
            "date_from": date_from, "date_to": date_to,
        },
    )




# ⚰️ **`seed` 工具 2026-08-17 从 MCP 面撤下**（开工单 1.5，她 8-16 定；施工 5 · G 件）。
#    整个 wrapper 和 `from tools import seed` 一起删了（上面那段有理由）。
#    照 night_fall/`I`/`dream` 的先例：**停用不删档** —— `tools/seed/` 那两个文件
#    还在盘上，十三颗种子的桶一条没动，将来真想翻只有一条路：`recall(id 直查)`。
#    ⚠️ 别顺手把它注册回来。要加之前先读 1.5 那三行（尤其「靠红字提醒的工具没长进手里」）。
#    📌 连带要改的**文档**（主人改，我不动）：全局 CLAUDE.md 里 seed 出现的四处
#       （工具清单 12 个 → 11 个、「什么时候伸手」表里那行红字、`grow` 那段
#       「先 seed 认个名字」、`fold`/消化那段的「seed 只在两个时刻碰」）。



# =============================================================
# Dashboard API endpoints (for lightweight Web UI)
# 仪表板 API（轻量 Web UI 用）
# =============================================================
# =============================================================
# /api/buckets、/api/bucket/*、/api/settings/*、/api/anchors、/api/self
# —— 已拆分到 web/buckets.py
# =============================================================


# =============================================================
# /dashboard、/api/env-vars、/api/config、/api/test/*、/api/models、/api/env-config
# —— 已拆分到 web/config_api.py
# =============================================================




# =============================================================
# /api/host-vault、/api/import/*、/api/bucket/{id}/edit、/api/export、/api/migrate/*
# —— 已拆分到 web/import_api.py
# =============================================================


# =============================================================
# /api/version、/api/update-info、/api/do-update、/api/author、
# /api/onboarding/status、/api/status —— 已拆分到 web/meta.py
# =============================================================


# ============================================================
# OAuth 2.0 — MCP Remote Auth —— 脱壳 E2（2026-08-17）搬去 bridge/oauth.py：
# 这不是面板路由，是 /mcp 本体的远程客户端鉴权，开源版「鉴权默认开」是既定
# 立场，不能跟着面板一起死（bridge/__init__.py 有完整理由）。
# 这里把启动期 MCP 鉴权中间件要用的两个校验函数 import 回来：mcp_auth_mode=="oauth"（默认）
# 用 _is_valid_mcp_token，mcp_auth_mode=="token" 用 _is_valid_static_mcp_token，二选一注入中间件。
# ============================================================
from bridge.oauth import _is_valid_mcp_token, _is_valid_static_mcp_token  # noqa: F401


# ============================================================
# 🔴 Cloudflare Tunnel 管理 —— E2（2026-08-17）**整个砍了**（比原杀单更进一步，
# 她 2026-08-17 拍板）。判据是事实不是猜测：活库 config.yaml 零 tunnel 配置、
# 活容器启动日志零 tunnel 记录——从没用过，出门走的是自建网关的域名。
# 开源版也不带它：想暴露公网的人自己配反代，这跟数据主权立场更一致。
# lifespan 里 load_tunnel_config/start_tunnel/stop_tunnel 三个挂点一起摘除
# （server_app.py 的 RuntimeLifecycle 那三个字段本来就是 Optional，不传就是
# 「没有隧道」，不用改 server_app.py）。
# ============================================================


# --- Entry point / 启动入口 ---
if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    logger.info(f"Loci Brain starting | transport: {transport}")

    # iter 2.2：合并为单连接器 /mcp。
    # 当初（iter 2.1）拆 /mcp + /mcp-extra 是因为 claude.ai 连接器存在 5 工具上限；
    # 该上限现已解除，全部工具挂在主实例 mcp 上对外暴露一条 /mcp 即可，
    # 顺带消除「第二个连接器」在 Claude.ai 侧的 OAuth/连接器校验疑难。
    # mcp_extra 仅作历史工具分组容器保留（7 个 @mcp_extra.tool() 注册不动），
    # 这里把它的工具回灌进 mcp，让 stdio / sse / streamable-http 三种 transport 一致。
    # 依赖 FastMCP._tool_manager 私有结构；若未来版本变化，降级为仅暴露主集工具。
    from server_app import (
        HTTPRuntimeSettings,
        RuntimeLifecycle,
        build_http_app,
        merge_mcp_tool_registries,
    )

    try:
        _extra_count = merge_mcp_tool_registries(mcp, mcp_extra)
        logger.info(
            f"单连接器 /mcp：已把 {_extra_count} 个副集工具回灌进主实例，共 "
            f"{len(mcp._tool_manager._tools)} 个工具对外暴露"
        )
    except AttributeError as _merge_exc:
        logger.warning(
            f"FastMCP 内部结构变化，工具回灌失败，仅暴露主集工具：{_merge_exc}"
        )

    if transport in ("sse", "streamable-http"):
        import uvicorn
        from bridge import ollama_child as _ollama_child

        _http_settings = HTTPRuntimeSettings.from_config(config)
        _runtime_lifecycle = RuntimeLifecycle(
            logger=logger,
            decay_engine=decay_engine,
            embedding_outbox=embedding_outbox,
            ensure_ollama_child=_ollama_child.ensure_child_on_boot,
            stop_ollama_child=_ollama_child.stop_child,
            # tunnel 整个砍了（E2，2026-08-17）：load_tunnel_config/start_tunnel/
            # stop_tunnel 三个字段本来就是 Optional[...] = None，不传就是「没有隧道」。
            restart_github_auto_task=_restart_github_auto_task,
            github_auto_interval=_gh_auto_interval,
            boot_marker_path=os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                ".boot_fails",
            ),
            # Explicit IPv4 avoids localhost resolving to ::1 in Proot/Termux.
            keepalive_url=f"http://127.0.0.1:{LOCI_PORT}/health",
        )
        _mcp_token_validator = (
            _is_valid_static_mcp_token
            if _http_settings.auth_mode == "token"
            else _is_valid_mcp_token
        )
        _app = build_http_app(
            mcp,
            transport,
            settings=_http_settings,
            token_validator=_mcp_token_validator,
            lifecycle=_runtime_lifecycle,
        )
        # （工具数不在这儿报——上面「已把 N 个副集工具回灌进主实例，共 M 个工具对外暴露」
        #   那句报的是真数。这儿原来写死着「14 个工具」，工具砍到 9 个之后就是句谎话了。）
        logger.info("CORS middleware enabled for remote transport / 已启用 CORS 中间件")
        logger.info(
            "MCP request body limit: %s",
            "disabled"
            if _http_settings.max_request_bytes == 0
            else f"{_http_settings.max_request_bytes} bytes",
        )

        _mcp_auth_required = _http_settings.auth_required
        if _mcp_auth_required and _http_settings.auth_mode == "token":
            logger.info(
                "MCP 静态 Token 鉴权已启用（OAuth 端点已关闭）/ "
                "MCP static-token auth enabled (OAuth endpoints disabled)"
            )
            logger.warning(
                "=" * 60 + "\n"
                "⚠️  MCP 静态 Token 等同万能密钥：拿到它的人能读写你的全部记忆。\n"
                "    该模式与 OAuth 互斥，本进程不再提供 OAuth 授权流程；请勿把本服务\n"
                "    直接暴露到公网，仅在可信内网或自带鉴权的隧道场景使用，并妥善保管、\n"
                "    定期轮换该 Token。\n"
                + "=" * 60
            )
        elif _mcp_auth_required:
            logger.info("MCP OAuth middleware enabled / MCP OAuth 中间件已启用")
        else:
            # 安全加固 #7：关掉鉴权 = /mcp 全裸奔，任何能连到端口的人都能读写全部记忆。
            # 从 info 升级为显著 WARNING，避免用户无意识地把大脑暴露到公网。
            logger.warning(
                "=" * 60 + "\n"
                "⚠️  MCP 认证已关闭 (mcp_require_auth: false)：/mcp 无需任何令牌即可直连，\n"
                "    所有记忆工具全部对外开放——任何能访问本端口的人都能读写你的全部记忆。\n"
                "    本服务监听 0.0.0.0，若端口暴露到局域网/公网，请务必用反代鉴权、防火墙\n"
                "    或仅绑定 127.0.0.1 保护；仅在可信内网/本机自有前端场景才建议关闭鉴权。\n"
                + "=" * 60
            )
        # 端口口径澄清（用户反馈：Docker 与裸机端口容易混淆）。容器内固定监听 8000，
        # 对外端口由 host 映射（如 18001:8000）决定，改 host_port 不影响容器内监听；
        # 裸机则直接监听本端口（默认 18001）。
        if _wsh.in_docker():
            logger.info(
                f"Listening on :{LOCI_PORT} INSIDE the container. "
                f"外部访问端口由 host 映射决定（compose 里的 18001:{LOCI_PORT}），"
                f"改前端 host_port 不影响容器内监听。"
            )
        else:
            logger.info(f"Listening on :{LOCI_PORT} (bare-metal / 裸机默认 18001)")
        # 明确打印「客户端该怎么连」——给 Operit / 安卓 / 自建前端等非技术用户排障用。
        # 一眼能看清 endpoint 路径、鉴权开关；本机桥接务必用 127.0.0.1（见上方保活注释）。
        logger.info(
            "MCP endpoint ready | transport=%s | 本机连接 URL: http://127.0.0.1:%s/mcp "
            "（远程走你的域名/隧道，末尾同样是 /mcp）| 鉴权: %s",
            transport,
            LOCI_PORT,
            (
                "开启(需静态 Token)" if _http_settings.auth_mode == "token"
                else "开启(需 OAuth Bearer)"
            ) if _mcp_auth_required
            else "关闭(免 token 直连，仅限可信内网/本机)",
        )
        # Forwarded headers are validated inside the application against
        # LOCI_TRUSTED_PROXY_CIDRS.  Uvicorn's default proxy middleware rewrites
        # scope["client"] before our guards run, which discards the immediate
        # proxy address and makes that trust decision impossible.
        uvicorn.run(
            _app,
            host=_BIND_HOST,
            port=LOCI_PORT,
            proxy_headers=False,
        )
    else:
        # stdio：工具已在启动入口处统一回灌进 mcp（全部暴露），这里直接跑。
        mcp.run(transport=transport)
