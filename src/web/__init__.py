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
from . import panel_auth


_WEB_MODULES = (
    # 门要第一个注册：它那四条路由自己在白名单里，不会被自己锁住。
    ("web.panel_auth", panel_auth.register),
    ("web.config_api", config_api.register),
    ("web.loci", loci.register),
    # 2026-08-19 补回来的：导入那四条路由。引擎 core/import_memory.py 一直活着，
    # 是 E2 砍上游模块时把它的门一起砍了，面板上三个按钮点下去打的是 404。
    ("web.import_api", import_api.register),
)


class _Gated:
    """把 `mcp` 包一层，让**每一条 web 路由**自动带上面板那道门。

    为什么在注册这一层包，而不是去每个路由里加一句：
    二十多个路由，靠人记得加，早晚漏掉一个 —— 而漏掉的那一个不会报错，
    它只是**悄悄不设防**。这种错今晚已经见过太多次了。
    在这儿包一次，新加的路由自动就在门里面，除非显式写进白名单。

    白名单只有两类（见 panel_auth.PUBLIC_PATHS）：门本身要用的、
    以及调用方不是浏览器的（桥是另一个进程，它没有 cookie）。
    """

    def __init__(self, mcp):
        self._mcp = mcp

    def __getattr__(self, name):
        return getattr(self._mcp, name)

    def custom_route(self, path, methods=None, **kw):
        inner = self._mcp.custom_route(path, methods=methods, **kw)
        if panel_auth.is_public(path):
            return inner

        # 给桥用的那四条：不要 cookie（桥没有），但门锁着的时候要钥匙。
        # 判据和碑文都在 panel_auth.HOOK_PATHS 上面。
        if panel_auth.is_hook(path):
            def hook_deco(fn):
                import functools
                from starlette.responses import JSONResponse

                @functools.wraps(fn)
                async def hook_guarded(request):
                    ok, 为什么 = panel_auth.hook_ok(request)
                    if not ok:
                        return JSONResponse({"error": 为什么}, status_code=401)
                    return await fn(request)

                return inner(hook_guarded)

            return hook_deco

        def deco(fn):
            import functools
            from starlette.responses import JSONResponse

            @functools.wraps(fn)
            async def guarded(request):
                if panel_auth.gate_needed() and not panel_auth.has_session(request):
                    # 401 是**约定好的信号**：前端拿到它就弹门（页面里那句
                    # `if (r.status === 401){ openGate(); }` 一直都在，
                    # 只是 E2 之后再没有东西会回 401 了）。
                    return JSONResponse({"error": "请先登录"}, status_code=401)
                return await fn(request)

            return inner(guarded)

        return deco


def register_all(mcp) -> None:
    """注册所有已迁移到 web/ 的路由模块。后续每迁一个模块加一行。"""
    gated = _Gated(mcp)

    def _register():
        for _name, register in _WEB_MODULES:
            register(gated)

    return _shared.run_v3_web_operation(
        "register_all",
        {"modules": [name for name, _register_fn in _WEB_MODULES]},
        _register,
        module="web.*",
    )
