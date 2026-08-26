# scripts/verify — 验收工具，不是测试

这四个不进 CI，也不该进。**它们是动完刀之后用来自证的**，跑给自己看，
判据是「我凭什么敢说这批改动是安全的」。

跑之前 `cd` 到仓根，路径都是仓根相对的。
（`mutate.py` 例外：它从自身位置推仓根，哪儿跑都行；要打别的 checkout 用 `--root`。
它每次开头会把用的仓根打出来 —— **看一眼，别让它去改另一个 checkout 然后回来跟你报喜。**）

| 用它 | 什么时候 | 怎么跑 |
|---|---|---|
| `only_comments.py` | 一批改动号称**只动了注释和 docstring** | `python scripts/verify/only_comments.py <git-ref> <文件...>` |
| `tool_docs_unchanged.py` | 同上，但要守住 `src/server.py` 里那七个 `@mcp.tool()` 的 docstring —— **上面那把恰好放行 docstring**，而那些 docstring 是产品本身 | `python scripts/verify/tool_docs_unchanged.py <git-ref>` |
| `mutate.py` | 写完断言，要回答「**它凭什么会红**」 | `python scripts/verify/mutate.py` |
| `strdiff.py` | 想知道两版之间所有非 docstring 的字符串字面量动过哪些（报错文案、提示语） | `python scripts/verify/strdiff.py <git-ref> <文件>` |

## 🔴 这批工具存在的理由，就一句

**验工具的工具坏了，它的失败长得跟「你的东西不行」一模一样。**

翻英文那一单里靠它们逮到四次自己的错，其中两次是**工具本身有洞**：
`only_comments.py` 曾经把括号内隐式拼接的第二段字符串当 docstring 放行
（而这个仓的报错文案几乎全是那个写法）；`mutate.py` 在目标不唯一时会
**悄悄改到别的地方**，报出来的「测试没抓住」其实是刀砍偏了。

所以用它们的时候记着：**绿灯先别信，先问这把尺子自己验过没有。**

## ⚠️ 退出码

`only_comments` / `tool_docs_unchanged` 有问题时返回非 0。
别用管道接 `head`/`tail` 之后再读 `$?` —— 那读到的是管道末端那个命令的。
