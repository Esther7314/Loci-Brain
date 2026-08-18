"""
========================================
web/ — 面板 HTTP 路由层（脱壳 E2 之后：只剩这一屏该留的）
========================================

历史上 server.py 把 93 个 @mcp.custom_route 全平铺在一个 5000 行文件里，后来按域
拆成独立模块。E2（2026-08-17）把上游那 20 个模块砍了——认证/OAuth 登录页/
GitHub 同步/一键装 ollama/隧道管理/搜索/桶浏览/导入导出/webhook/plans/onboarding/
v3 debug/旧 dashboard，一个字都不剩；四组还活着但不是「面板」的东西
（MCP 远程 OAuth 校验、/mcp 请求体护栏、本地 ollama 子进程常驻）挪去了 `bridge/`。

**现在有三个模块**：
- `config_api`：E1 留 4 删 7 之后的引擎设置（`/api/config` GET+POST ·
  `/api/test/dehydration` · `/api/test/embedding` · `/api/models`）。
- `import_api`：导入的四条路由（preflight/upload/status/pause），2026-08-19 补回来。
- `loci`：我们自己的新面板本体（房间四间、breath/recall 预览、档案、
  发呆、密码设置……），`web/` 下唯一全新写的模块。

共享依赖（config、密码/登录限速工具）放在 web/_shared.py（类比 tools/_runtime.py）。
⚠️ `_shared.py` 里已经**没有 cookie 会话/鉴权**了——面板 `/api/*` 不再鉴权
（她拍板，跟 8-05「家里内网不鉴权」一致）。_shared 留下的密码原语是给
`bridge/oauth.py` 的 MCP 远程 OAuth 授权页用的，两回事。

对外暴露：register_all(mcp) —— 注册当前已迁移的所有 web 路由模块。
========================================
"""

from . import _shared
from . import config_api
from . import import_api
from . import loci


_WEB_MODULES = (
    ("web.config_api", config_api.register),
    ("web.loci", loci.register),
    # 2026-08-19 补回来的：导入那四条路由。引擎 core/import_memory.py 一直活着，
    # 是 E2 砍上游模块时把它的门一起砍了，面板上三个按钮点下去打的是 404。
    ("web.import_api", import_api.register),
)


def register_all(mcp) -> None:
    """注册所有已迁移到 web/ 的路由模块。后续每迁一个模块加一行。"""
    def _register():
        for _name, register in _WEB_MODULES:
            register(mcp)

    return _shared.run_v3_web_operation(
        "register_all",
        {"modules": [name for name, _register_fn in _WEB_MODULES]},
        _register,
        module="web.*",
    )
