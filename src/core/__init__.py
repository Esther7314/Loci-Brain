"""
========================================
core/ —— 存取 / 遗忘 / fold / muse / dream 引擎层（脱壳 C，2026-08-17）
========================================

按层重码（她 8-17 拍的「脱壳 + 重排」）：这里装的是记忆系统的地基——
不管来的是哪个工具、哪屏面板，落盘/索引/衰减/去重/摘要/织梦这些机制只有一份。

装的东西：
- `bucket_manager.py` / `decay_engine.py` / `dehydrator.py` / `bm25_index.py` /
  `embedding_engine.py`：真正读写磁盘、算衰减、调 LLM 摘要、算 BM25、管向量的引擎。
- `errors.py`：贯穿这些引擎的错误类型（谁都要认得同一套错误形状）。
- `github_sync.py` / `import_memory.py` / `migrate_engine.py` / `migration_engine.py`：
  跟 bucket_manager 同级的后台引擎（GitHub 备份同步、外部笔记导入、库迁移、
  embedding 后端迁移）——都只由 server.py 在启动期构造一次，性质上跟上面五个
  是一类东西，原来散在顶层，这次一起收进来。
- `_fold.py` / `_muse.py` / `_dream.py` / `_bigevent.py` / `_when.py` / `_rooms.py`：
  从 `tools/` 搬来的引擎件——fold/gist 的骨头、发呆的判据、织梦的判据、
  时期的时间圈法、日历/时区口径、四间房的白名单。这些原来住在 `tools/`
  只是因为 MCP 工具最先用到它们，但判据本身不属于任何一个工具
  （8-08 房间改名两边各写一遍判据、8-17 又抓到 weight=0 falsy 兜底一边修好
  一边没修，都是"该同源的东西分了家"吃的亏）。
- `profile.py`：施工 5 的合同源（门口那张纸() / 事件池()），原来住在
  `tools/breath/awaken.py`——判据不属于 breath 这一个工具，`web/loci.py`
  的档案页和 `tools/breath/awaken.py` 的睁眼都要读同一份判据，所以搬到这儿，
  两边各自 import，不再各写一份。

⚠️ **只挪家不改逻辑**：这一层的文件内容跟搬家前逐字一样，改的只有 import 路径。
`bucket_manager.py`/`recall/core.py` 这类巨石文件的内部结构这单不拆
（纪律：重构跟着删迁走，不单独开大重构战线）。

依赖方向：`core` 不依赖 `tools`/`web`/`bridge`（**尽量**——`dehydrator.py` 用
`tools/_subjects.py` 的 `normalize_subjects`、`import_memory.py` 用
`tools/_common.py` 的几个校验函数，这两条是历史就有的反向依赖，这次没有
跟着挪：`_subjects.py`/`_common.py` 本身更贴"工具怎么校验数据"，硬挪反而
把无关的东西搅在一起。交活报告里点名了这两条，不是漏改。

对外暴露：各文件自己的 docstring 写了，这里不重复。`server.py` 是唯一
在启动期直接 import 这些引擎类去构造实例的地方；`tools/_runtime.py` /
`web/_shared.py` 拿到构造好的实例引用后，其余代码一律通过它们访问，
不再直接 `import core.bucket_manager` 之类。
========================================
"""
