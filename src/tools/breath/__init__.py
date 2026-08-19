"""
========================================
tools/breath/__init__.py — breath 工具的入口
========================================

breath 是「我睁眼看看自己记得什么」。**没有参数，没有分支**：
一个动作，一屏，永远是同一屏。

- awaken.py：睁眼那一屏（门口那张纸 · 提醒 · 中期 · 忽然想起）

关键行为：
- dispatch() 只做一件事：记一笔 op、确保遗忘引擎起来了、把睁眼那屏渲出来
- 不在这里做实际取桶/调 LLM 的工作

不做什么（边界）：
- 不做权限校验，MCP 调用方默认是模型自身

对外暴露：dispatch() → str

⚰️ **2026-08-19：底下那套带参数的检索整个删了**（她拍的「改吧」）。
   原来这儿按参数路由到五支：`catalog`（目录模式）· `feel`（feel 通道）·
   `importance`（按 importance 拉）· `surface`（老浮现）· `search`（关键词+向量检索），
   加上它们共用的 `_verbatim.py`，一共 6 个文件 1110 行。
   🔴 **8-18 把 breath 工具面的 9 个形参砍掉之后，这五支就一个入口都没有了**
   —— `server.py` 里 `_t_breath.dispatch()` 永远不带参数调，
   而工具面的 schema 是强制清空 + `extra="forbid"` 的，外面也塞不进来。
   **留着没入口的路，下次读代码的人（就是我）会以为它还活着。**
   要找东西是 `recall` 的活，那才是检索器；breath 只管睁眼。
   （删之前逐个查过引用：五支只被这个文件调，`_verbatim` 只被那四支用，
     `awaken` 一个都不依赖，烟测里 breath 全是 `breath({})`。）
========================================
"""

from .. import _runtime as rt
from .awaken import surface_awaken


async def dispatch() -> str:
    if rt.mark_op:
        rt.mark_op("breath")
    rt.record_v3_tool_event("breath", {})
    await rt.decay_engine.ensure_started()
    return await surface_awaken()
