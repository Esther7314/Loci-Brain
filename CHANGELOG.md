# CHANGELOG

## 1.1.0 · 2026-08-19

### 🔴 修掉一个已经推出去的崩溃（如果你在 8-19 白天 clone 过，请更新）

`tools/trace/core.py` 里有两个未定义名（`why_remembered` / `requested_importance`），
**任何走到那段的 `trace` 调用都会当场 NameError**。
原因是一次误推：砍死参数砍到一半的工作树被 `git add -A` 卷进了另一条 commit。
（教训写在这儿供人一笑：**手上有活就先落一条 commit，别把半成品留在暂存区。**）

### 变快

- **`recall` 浏览（不带 `query`）13 秒 → 0.014 秒**。两个原因，都在 `bucket_manager`：
  - 「上次检查文件是什么时候」记的是**开扫之前**的时刻，而扫一遍 1000 个文件在
    Windows 的 bind mount 上要 2.6 秒 > 间隔 1 秒 —— 于是**每次调用都判定过期、
    每次都重扫，缓存一次都没命中过**
  - 文件指纹改走 `os.scandir`（目录列举本来就带 mtime/size，不必再 `os.stat` 一遍）：
    1000 个文件 2.45 秒 → 1.05 秒
- **面板 `/api/loci/recall` 不再把同一次搜索算两遍**（原来 `recall_data()` 和
  `recall_core()` 各走一遍 `_collect`）。带 `query` 时 8.6 秒 → 约 3 秒。

### 变了（升级前看一眼）

- **`recall` 浏览面的长相**：
  - 「时期」不再用 `◈` 符号，直接写「时期」两个字；**一格只留一条**（原来最多 3 条）
  - **认知快照（gist）的标题行不在浏览面出现了**。判据：翻记忆的时候要看的是 event；
    认知在「突出的点」那儿露面。被折起的那些照旧算进统计、搜得到、id 直查得到
  - **`regrow` 换版的旧版不再算进条数**，也不再以「▣… 盖着这里 1 条」在原地出头。
    一条换了版还是一条。旧版没消失：id 直查逐字 + 版本链两头照旧
- **`trace` 删掉七个早已不可用的参数**（`importance` / `resolved` / `digested` /
  `content` / `why_remembered` / `meaning_append` / `meaning_replace`）。
  它们 8-18 就从工具面撤了（`extra="forbid"` 挡着），这次是把底下的死签名一起清掉。
  ⚠️ `importance` 作为**内部字段**没动：`pin` 一条准则仍然把它锁成 10。
- **`breath` 底下五支没有入口的检索整个删了**（`catalog` / `feel` / `importance` /
  `surface` / `search` + `_verbatim`，共 6 个文件）。工具面的 `breath` 8-18 起就是零参数的，
  这些分支永远走不到。找东西是 `recall` 的活。

### 撤掉

- **`gateway/近期记忆视图.js`**（连同 `LOCI_RECENT_VIEW`）。
  它做的是「隔天开窗，把 `recall(when="昨天")` 的原文摘录贴进去」，
  而「昨天做了什么」本来就该是**收窗时压出来的那几句**，第二天带着它开窗即可 ——
  同一件事的第二个做法，而且要多问 Loci 一次。思路留在 `gateway/README.md` 第四节。

### 加了

- **`RELEVANCE_STRONG_WORDS`**：强档词表可以整表换（逗号分隔，或 `gateway/强档词.json`），
  匹配不分大小写。
  🔴 **为什么这条重要**：仓库带的是中文词表，而弱档默认关 ——
  **不说中文的人装上之后，相关记忆提醒一次都不会触发，而且不会收到任何提示。**
  跟「超时 5 秒」是同一种失败：安静地什么都不做。
- **对外函数的英文别名**：`paste` / `computeReminder` / `appendToTail`
  （＝`贴一次` / `算相关记忆提醒` / `贴到真尾巴`，同一个函数）。
- `gateway/README.md` 重写。

---

## 1.0.0 · 2026-08-19

首个公开版本。基于 [Ombre-Brain](https://github.com/P0luz/Ombre-Brain) 二次开发（MIT）。
