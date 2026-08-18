"""
========================================
web/loci.py — Loci 独立 dashboard 的只读数据层（2026-08-03 夜）
========================================

她 8-03 傍晚拍的四页：大池子 / 星空 / 相似度检查 / 档案。这里只管吐数据，
渲染全在 frontend/loci.html。**只读**——唯一的写口是相似度页那个「沉一个」
（走 trace delete=True，软删，id 直查永远捞得回）。

    GET  /loci                        → 页面本体
    GET  /api/loci/recall             → recall 的第二张皮（卡 + 列表）
    GET  /api/loci/graph              → 星空：节点 + 真边 + 弱边 + 星座
    GET  /api/loci/similar            → 疑似同件对子 + 分数分布（阈值可调）
    GET  /api/loci/profile            → 门口那张纸
    GET  /api/loci/bucket/{id}        → 单桶逐字原文 + 元数据
    GET  /api/dream/current           → 当前那个梦（当前层 + 层级；没梦 204，会写「回想」state）
    GET  /api/muse/pending            → 该发呆了吗（团数 + 年龄 + worth_poking）
    GET  /api/loci/pulse              → 体检：多少条/占多大/引擎活着没（2026-08-18 从 MCP 工具面搬来）
    GET  /api/loci/poke               → 施工7c：梦(交付)+发呆团数(提醒)+recall结构化分数，一口问全，纯读
    POST /api/loci/dream/wake         → 施工7d：降级信号，把活着的「完整」梦层降成碎片层（幂等）
    POST /api/loci/similar/action     → 人工裁决：都留 / 沉一个

规矩：主库零触碰 · 参数不用 Optional[简单类型] · 改完跑三套 smoke。

对外暴露：register(mcp)。
========================================
"""

import json
import os
import re
import sqlite3
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh
from core import _when as _w      # 「她的今天」（本地时区）—— 别直接用 datetime.now()
from utils import read_from

logger = sh.logger

_PROFILE_TAG = "__档案事实__"
_BIGEVENT_TAG = "__大event__"
# 她 8-03 夜定的：默认 88（85 往下开始混进情绪种子那十三颗根，它们本来就该长得像）
_SIM_DEFAULT = 88.0
_SIM_FLOOR = 60.0      # 低于这个分数的对子连算都不算，省得几十万条塞进内存
# 内存闸：最多留这么多对（codex 复核 #5）。628 条现在留 2.9 万对；
# 同样 15% 的比例到一万条就是 750 万个 tuple ≈ 0.7 GB。
_PAIRS_CAP = 200_000
# ⚰️ `_REMIND_DAYS`（30 天）跟着门口那张纸搬进了合同源
# （`tools/breath/awaken.py`，施工 5 · E 件）——**别在这儿再放一个 30**，
# 两个 30 就是两套规则，改一个忘一个的病根。`_is_closed` 同理（提醒的判据在那边）。


# ============================================================
# 两个写口的门（2026-08-04，codex 复核 #1/#2/#6 之后加的）
# ============================================================

def _origin_reject(request: Request) -> str:
    """同源检查。返回空串 = 放行，否则返回拒绝理由。

    为什么单靠 cookie 不够（codex 复核 #1）：`SameSite=Lax` 只挡**跨站**，
    挡不住**同站跨源** —— 另一个 `localhost:9999` 上的页面和这儿属于同一个 site，
    它用 `text/plain` 发一段合法 JSON，浏览器照样把 cookie 带上。
    所以这两个写口必须自己看 Origin。

    没有 Origin 头一律拒：浏览器发 POST 必带它，缺了就说明不是浏览器发的
    （curl / 脚本）。这两个口本来就只给页面上的按钮用，挡掉是对的。
    """
    origin = request.headers.get("origin") or ""
    host = request.headers.get("host") or ""
    if not origin:
        return "缺 Origin 头（这个写口只接受页面上的按钮）"
    if not host:
        return "缺 Host 头"
    from urllib.parse import urlsplit
    try:
        o = urlsplit(origin)
    except ValueError:
        return f"Origin 解析不了：{origin}"
    if o.scheme not in ("http", "https"):
        return f"Origin 的协议不对：{origin}"
    # netloc 带端口，所以「同一台机器的另一个端口」也会被这条挡下来 —— 这正是要挡的
    if not o.netloc or o.netloc != host:
        return f"Origin 和 Host 对不上：{o.netloc} ≠ {host}"
    return ""


async def _write_body(request: Request) -> dict:
    """写口专用的 body 读取：先验同源和 Content-Type，再解析。

    抛 `PermissionError` = 该回 403；抛 `ValueError` = 该回 400。
    （原来直接用 `sh._read_json_object`，坏 JSON 会一路冒到最外层变成 500 —— codex #6）
    """
    why = _origin_reject(request)
    if why:
        raise PermissionError(why)
    ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ct != "application/json":
        raise ValueError(f"Content-Type 必须是 application/json（收到 {ct or '空'}）")
    try:
        body = await request.json()
    except Exception:
        raise ValueError("body 不是合法 JSON")
    if not isinstance(body, dict):
        raise ValueError("body 必须是一个 JSON 对象")
    return body


# ============================================================
# 相似度：全库两两余弦。向量库没变就不重算（滑块要跟手）
# ============================================================

_sim_lock = threading.Lock()
_sim_cache: dict = {"key": None, "pairs": [], "hist": [], "n": 0, "total_pairs": 0}


def _emb_db_path() -> str:
    return os.path.join(sh.config["buckets_dir"], "embeddings.db")


_rev_cache: dict = {"at": 0.0, "val": (0, 0.0)}
_REV_TTL = 2.0          # 秒。滑块连着拖的时候别每一下都去 walk 九百个文件


def _buckets_rev() -> tuple:
    """桶目录的「版本」：文件数 + 最新一次改动时间。

    为什么缓存 key 不能只看 `embeddings.db`（codex 复核 #10）：
    只改 name / room / tags / importance / domain **不会动向量库** ——
    比如给一条打上 `__seed__`（本该从这一页消失），它却还留在缓存的对子里，
    卡片上的名字和摘要也是旧的。而这些改动一定会重写那个桶的 .md。

    ⚠️ **必须递归**：`dynamic/` 下面还有一层，顶层 listdir 只看得到 117 个，
    真实是 900 多个 —— 那样等于九成的改动都漏掉，修了跟没修一样。
    （第一版就是这么写的，靠 smoke 打出来的文件数才发现。）
    """
    import time
    now_s = time.monotonic()
    if now_s - _rev_cache["at"] < _REV_TTL:
        return _rev_cache["val"]

    root = str(sh.config.get("buckets_dir") or "")
    newest, count = 0.0, 0
    for sub in ("dynamic", "permanent", "feel", "plans", "letters", "archive"):
        for dirpath, _dirs, names in os.walk(os.path.join(root, sub)):
            for name in names:
                if not name.endswith(".md"):
                    continue
                count += 1
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
                except OSError:
                    pass
    val = (count, round(newest, 3))
    _rev_cache.update({"at": now_s, "val": val})
    return val


def _load_vectors() -> tuple[list, object]:
    """从 embeddings.db 读全部向量，返回 (ids, 归一化后的矩阵)。"""
    import numpy as np
    ids, vecs = [], []
    con = sqlite3.connect(f"file:{_emb_db_path()}?mode=ro", uri=True)
    try:
        for bid, emb in con.execute("select bucket_id, embedding from embeddings"):
            try:
                v = np.asarray(json.loads(emb), dtype=np.float32)
            except (TypeError, ValueError):
                continue
            if v.ndim != 1 or v.shape[0] < 8:
                continue
            ids.append(str(bid))
            vecs.append(v)
    finally:
        con.close()
    if not vecs:
        return [], None
    dim = Counter(v.shape[0] for v in vecs).most_common(1)[0][0]
    keep = [i for i, v in enumerate(vecs) if v.shape[0] == dim]
    ids = [ids[i] for i in keep]
    M = np.vstack([vecs[i] for i in keep])
    M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    return ids, M


def _sim_visible(meta: dict) -> bool:
    """哪些桶进这一页。

    排掉三类，全是「本来就该长得像、判重反而是误伤」的：
      · 情绪种子（十三颗根：想听/想看/想懂 互相 87~89 分，它们是坐标系不是记忆）
      · 归档区（已经沉过一次了，别再问一遍）
      · 旧版认知（regrow 的版本链，新旧本来就该像）
      · **被 fold 盖住的**（施工 3）：跟盖它的那条 gist 本来就该像，
        而且它已经不独立冒头了，判重页再拿它烦我一次没有意义
    """
    if str(meta.get("type") or "") in ("archived", "letter"):
        return False
    if (meta.get("domain") or [""])[0] == "seed":
        return False
    if "__seed__" in [str(t) for t in (meta.get("tags") or [])]:
        return False
    if (meta.get("superseded_by") or meta.get("covered_by")
            or meta.get("tombstone") or meta.get("deleted_at")):
        return False
    return True


async def _compute_pairs() -> dict:
    """算一次全库两两余弦，缓存到 embeddings.db 的 mtime 变化为止。"""
    import numpy as np
    try:
        key = (os.path.getmtime(_emb_db_path()), os.path.getsize(_emb_db_path()),
               _buckets_rev())      # 元数据改了也要重算（codex #10）
    except OSError:
        key = None

    with _sim_lock:
        if key is not None and _sim_cache["key"] == key:
            return _sim_cache

    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    info: dict[str, dict] = {}
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        bid = str(meta.get("id") or b.get("id") or "")
        if not bid or not _sim_visible(meta):
            continue
        info[bid] = {
            "id": bid,
            "short": bid[:6] if re.fullmatch(r"[0-9a-f]{12}", bid) else bid,
            "name": str(meta.get("name") or ""),
            "summary": str(meta.get("summary") or ""),
            "room": str(meta.get("room") or ""),
            "created": str(meta.get("created") or "")[:10],
            "body": str(b.get("content") or ""),
            "tagged": [str(t).split(":", 1)[1] for t in (meta.get("tags") or [])
                       if str(t).startswith("疑似同件:")],
        }

    ids, M = _load_vectors()
    if M is None:
        result = {"key": key, "pairs": [], "hist": [], "n": 0, "total_pairs": 0,
                  "info": info, "no_vectors": True}
        with _sim_lock:
            _sim_cache.update(result)
        return result

    keep = [i for i, b in enumerate(ids) if b in info]
    ids = [ids[i] for i in keep]
    M = M[keep]

    n = len(ids)
    hist = [0] * 20  # 每 5 分一格，0~100
    pairs = []
    capped = False
    if n >= 2:
        # 分块算，别一次性开 n×n 的大矩阵（671 条无所谓，将来上万条就有所谓了）
        step = 256
        for s in range(0, n, step):
            block = M[s:s + step] @ M.T                      # (b, n)
            for r in range(block.shape[0]):
                i = s + r
                row = block[r]
                row[:i + 1] = -1.0                           # 只留上三角，别数两遍
                counts = np.bincount(
                    np.clip((np.maximum(row[i + 1:], 0) * 20).astype(int), 0, 19),
                    minlength=20)
                hist = [h + int(c) for h, c in zip(hist, counts)]
                for j in np.where(row >= _SIM_FLOOR / 100.0)[0]:
                    pairs.append((float(row[j]) * 100.0, ids[i], ids[int(j)]))
            # 上限闸（codex 复核 #5）：现在 628 条留 2.9 万对，还很轻；
            # 但同样比例到一万条就是 750 万个 tuple ≈ 0.7 GB，进程会顶不住。
            # 这里只保住「最像的那些」—— 判重本来就是从高分往下看的，
            # 60 分附近那几百万对，人一辈子也翻不到。
            # ⚠️ 代价：闸响之后把阈值拉到很低，看到的对子数会少于真实值，
            # 所以下面回一个 capped 标记，别让页面上的数字骗人。
            if len(pairs) > _PAIRS_CAP * 2:
                pairs.sort(key=lambda p: -p[0])
                del pairs[_PAIRS_CAP:]
                capped = True
    pairs.sort(key=lambda p: -p[0])
    if len(pairs) > _PAIRS_CAP:
        del pairs[_PAIRS_CAP:]
        capped = True
    result = {"key": key, "pairs": pairs, "hist": hist, "n": n, "capped": capped,
              "total_pairs": n * (n - 1) // 2, "info": info, "no_vectors": False}
    with _sim_lock:
        _sim_cache.update(result)
    return result


# ============================================================
# 星空：节点 / 边 / 星座
# ============================================================

_WIKI_RE = re.compile(r"\[\[([^\[\]|]{1,40})\]\]")
_SEED_NAMES = frozenset({
    "joy", "anger", "sorrow", "fear", "love", "aversion", "desire",
    "lust", "sound", "scent", "taste", "touch", "dharma", "greed",
})


def _split_ids(raw) -> list[str]:
    """triggered_by 落盘是逗号分隔的字符串（也兼容早期的 list）。"""
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = str(raw or "").split(",")
    return [x.strip() for x in items if x.strip()]


def _bigevent_members(content: str, entries: list,
                      since: str = "", until: str = "") -> tuple[list[str], str]:
    """大 event 的成员星 → (成员 id 列表, 这条查询的人话)。

    **星座 = 一条大 event 照亮的那片天**（她 8-06 傍晚定稿：时间轴和星座是同一个
    东西，就叫星座）。起止优先认 `when` 里的起止（8-05 起就写在那儿，调用方传进来）；
    正文那行「范围：…」只当老桶的兜底。过滤条件仍从正文读：

        过滤：tag=记忆系统         → tag
        过滤：room=I/EVENT        → room（前缀）

    她 8-03 夜问过要不要改成结构化字段。**故意不改**：那行字是正文的一部分，
    AI recall 到这条大 event 时**读得懂它**；藏进 metadata 反而他看不见了。
    真正脆的不是自由文本，是「写错了不吭声」——所以解析不出来就返回空 query_text，
    页面上直接喊出来，不再悄悄画一座空星座。
    """
    m_from = re.search(r"范围[:：]\s*(\d{4}-\d{2}-\d{2})(?:\s*\.\.\s*(\d{4}-\d{2}-\d{2}))?", content)
    m_tag = re.search(r"过滤[:：][^\n]*?\btag\s*=\s*([^\s,，;；]+)", content)
    m_room = re.search(r"过滤[:：][^\n]*?\broom\s*=\s*([A-Za-z/]+)", content)
    if not since and m_from:
        since = m_from.group(1)
        until = m_from.group(2) or ""
    if not (since or m_tag or m_room):
        return [], ""
    want_tag = m_tag.group(1) if m_tag else ""
    want_room = (m_room.group(1).rstrip("/") if m_room else "")

    out = []
    for e in entries:
        if since and e["date"] < since:
            continue
        if until and e["date"] > until:
            continue
        if want_tag and want_tag not in e["_tags_all"]:
            continue
        if want_room:
            r = e.get("room") or ""
            if not (r == want_room or r.startswith(want_room + "/")):
                continue
        out.append(e["id"])

    bits = []
    if since:
        bits.append(f"{since} 起" if not until else f"{since} ~ {until}")
    if want_room:
        bits.append(f"room={want_room}")
    if want_tag:
        bits.append(f"tag={want_tag}")
    return out, " · ".join(bits)


# ============================================================
# 三个 builder：**故意放在路由闭包外面**，这样不用伪造登录会话就能单独跑一遍
# （`python -c "asyncio.run(loci.build_graph())"`）。路由只管鉴权和 JSON 外壳。
# ============================================================

async def build_rooms() -> dict:
    """两扇门进来先看的目录：四间房各多少条 + 十个高频标签。

    二改 A 件（2026-08-16）：房间 10→4，门也从 I/YOU 换成 EVENT/MIND。
    I/YOU 那一维不是丢了，是搬去了 subjects——「关于谁」不再由房间承担。
    """
    from tools.recall.core import _room_cn, _visible
    from core._rooms import ALL_ROOMS, normalize_room
    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    counts: Counter = Counter()
    tags: Counter = Counter()
    homeless = 0
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        if not _visible(meta):
            continue
        # 归一之后再计数：老盘上还是十间的名字，不归一的话四扇门全是 0、
        # 而 homeless 会暴涨成全库——那个 0 看起来还挺像「就是没数据」。
        r = normalize_room(meta.get("room"))
        if r:
            counts[r] += 1
        else:
            homeless += 1
        for t in (meta.get("tags") or []):
            t = str(t)
            if not t.startswith(("__", "aspect:", "疑似同件:")):
                tags[t] += 1
    doors: dict[str, list] = {"EVENT": [], "MIND": []}
    for r in ALL_ROOMS:
        doors["MIND" if r.startswith("MIND/") else "EVENT"].append(
            {"room": r, "cn": _room_cn(r), "n": counts.get(r, 0)})
    return {
        "doors": doors,
        "homeless": homeless,
        "total": sum(counts.values()) + homeless,
        "top_tags": [{"tag": t, "n": n} for t, n in tags.most_common(10)],
    }


async def build_subjects() -> dict:
    """「都有谁」：库里出现过的全部主体 + 各多少条 + 最近一次。**纯读，不写盘。**

    为什么要这一屏（她 2026-08-18 拍的）：`aliases.yaml` 是手工维护的，
    而手工维护的前提是「你得先知道有东西要改」—— **而那一步一直是空的**。
    新出现一个人没人告诉你；「她哥」和「哥哥」裂成两个人没人告诉你；
    抽错的噪音混进去也没人告诉你（8-18 扫全库扫出一个「小刀批」，
    是模型把正文里「一小刀」当成了人名）。

    判据跟 muse/fold 同一条：**系统只负责摆出来，合并那一下人自己点。**
    所以这个口只数数，一个字都不往 aliases.yaml 里写。

    口径跟 recall/rooms 一样：`_visible` 筛一道（旧版认知、被 fold 盖住的、
    种子都不算），时间走 `_node_ts`（when 优先 created 兜底）——
    面板上看到的数必须和我睁眼看到的是同一个。
    """
    from tools.recall.core import _visible
    from tools import _subjects as subj
    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    counts: Counter = Counter()
    last: dict[str, datetime] = {}
    last_bucket: dict[str, str] = {}
    total = 0
    with_subj = 0
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        if not _visible(meta):
            continue
        total += 1
        names = [str(x).strip() for x in (meta.get("subjects") or []) if str(x).strip()]
        if names:
            with_subj += 1
        ts = _node_ts(meta)
        bid = str(meta.get("id") or "")
        for n in dict.fromkeys(names):        # 同一条里重复的名字只算一次
            counts[n] += 1
            if ts is not None and (n not in last or ts > last[n]):
                last[n] = ts
                last_bucket[n] = bid
    table = subj.load_alias_table()           # {别名小写: 规范名}
    names_out = []
    for n, c in counts.most_common():
        ts = last.get(n)
        names_out.append({
            "name": n,
            "n": c,
            "last": ts.strftime("%Y-%m-%d") if ts else "",
            "last_bucket": last_bucket.get(n, ""),
            # 已经在表里的：归到哪个规范名下（名字自己就是规范名时也算）
            "canonical": table.get(n.lower(), ""),
            # 代词不该当主体（闸在写入端）——真出现了就是漏了，标出来
            "pronoun": subj.is_pronoun(n),
        })
    return {
        "total": total,                       # 看得见的条数
        "with_subjects": with_subj,           # 其中抽到了人的
        "distinct": len(counts),              # 不同的名字几个
        "names": names_out,                   # 按次数降序
        "alias_table_size": len(table),
    }


def _node_ts(meta: dict) -> datetime | None:
    """一颗星在天上的位置：when 优先、created 兜底（跟 recall 同一个口径）。

    走 `tools/_when` —— 跟 recall 用同一把尺子，返回带时区的本地时间。
    （原来这儿也有那个 `[:19]` 切片，一样会把 Z / +08:00 切没了。codex #4）
    """
    for k in ("when", "created"):
        ts = _w.parse_stamp(meta.get(k))
        if ts is not None:
            return ts
    return None


async def build_graph() -> dict:
    """星空：节点 + 真边 + 弱边 + 星座 + 流星。

    ⚠️ 她 2026-08-17 21:33 拍板（「就只要有现在的你就好了」）：**只画现行版**。
    被盖的旧版（fold 盖过 / regrow 换过版）不上天——搜索、id 直查、版本链一概
    不受影响，只是星空图这一屏不画。判据用 `_F.is_covered()`（awaken/recall
    用的同一个合同源），别另写一套——8-08 房间改名两边各写一遍判据、一边修好
    一边没修的教训还在（见 tools/breath/awaken.py 顶上那段）。
    """
    from tools.recall.core import _room_cn, _visible, _label_of, _short_id
    from core._rooms import is_mind_room, normalize_room
    from core import _fold as _F
    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    now = _w.now()          # 本地时区（codex #4）
    fresh_line = now - timedelta(hours=24)

    nodes: list[dict] = []
    by_id: dict[str, dict] = {}
    by_name: dict[str, str] = {}
    raw: dict[str, dict] = {}
    big_events: list[tuple] = []

    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        bid = str(meta.get("id") or b.get("id") or "")
        content = str(b.get("content") or "")
        tags_all = [str(t) for t in (meta.get("tags") or [])]
        if _BIGEVENT_TAG in tags_all:
            # 换过版的旧版不挂天上（她 8-06 傍晚点的）：星座只画现行版，
            # 演变史在版本链里，点开单桶还看得到。判据统一走 _F.is_covered()
            # （8-17 21:33 追加件：跟下面普通节点同一道闸，别两处各写一套）。
            if not _F.is_covered(meta):
                big_events.append((bid, meta, content))
            continue
        if not bid or not _visible(meta):
            continue
        # 8-17 21:33 追加件：被盖的旧版（fold 盖过 / regrow 换过版）不上天——
        # 只影响这张星空图，搜索/id 直查/版本链照旧够得到被盖的那条。
        if _F.is_covered(meta):
            continue
        ts = _node_ts(meta)
        if ts is None:
            continue

        def _f(key, default, _meta=meta):
            try:
                return float(_meta.get(key, default))
            except (TypeError, ValueError):
                return default

        room = normalize_room(meta.get("room")) or str(meta.get("room") or "")
        created_dt = _node_ts({"created": meta.get("created")}) or ts
        node = {
            "id": bid,
            "short": _short_id(bid),
            "label": _label_of({"meta": meta, "content": content}),
            "room": room,
            "room_cn": _room_cn(meta.get("room")),
            "v": _f("valence", 0.5),
            "a": _f("arousal", 0.3),
            "date": ts.strftime("%Y-%m-%d"),
            "ts": ts.isoformat(timespec="seconds"),
            "pinned": bool(meta.get("pinned")),
            # 她拍的那一颗：今晚刚存的是天上最亮、还微微闪的
            "fresh": created_dt >= fresh_line,
            "kind": "mind" if is_mind_room(meta.get("room")) else "event",
            "seeds": sorted({s for s in _WIKI_RE.findall(content) if s in _SEED_NAMES}),
        }
        nodes.append(node)
        by_id[bid] = node
        nm = str(meta.get("name") or "").strip()
        if nm and nm not in by_name:
            by_name[nm] = bid
        raw[bid] = {"meta": meta, "content": content, "tags_all": tags_all}

    # ---- 真边：triggered_by / supersedes / 正文 [[]] ----
    edges: list[dict] = []
    seen_edge: set = set()

    def _add(a: str, b: str, kind: str) -> None:
        if a == b or a not in by_id or b not in by_id:
            return
        k = (a, b, kind)
        if k in seen_edge:
            return
        seen_edge.add(k)
        edges.append({"from": a, "to": b, "kind": kind})

    unresolved: Counter = Counter()
    for bid, r in raw.items():
        meta, content = r["meta"], r["content"]
        # 二改 E 件：边的种类跟着字段一起改名叫 from（read_from 读兼容老 triggered_by）
        for src in _split_ids(read_from(meta)):
            _add(src, bid, "from")
        for old in _split_ids(meta.get("supersedes")):
            _add(bid, old, "supersedes")
        for target in _WIKI_RE.findall(content):
            t = target.strip()
            if t in _SEED_NAMES:
                continue          # [[love]] 是情绪根，不指向另一条记忆
            if re.fullmatch(r"[0-9a-f]{12}", t):
                _add(bid, t, "wikilink")
            elif t in by_name:
                _add(bid, by_name[t], "wikilink")
            else:
                unresolved[t] += 1

    # ---- 弱边（她定的：真边亮、弱边暗、可开关）----
    weak: list[dict] = []
    seen_weak: set = set()

    def _add_weak(a: str, b: str, kind: str) -> None:
        if a == b:
            return
        k = (a, b) if a < b else (b, a)
        if k in seen_weak:
            return
        seen_weak.add(k)
        weak.append({"from": k[0], "to": k[1], "kind": kind})

    by_day: dict[str, list] = defaultdict(list)
    for n in nodes:
        by_day[n["date"]].append(n)
    for _day, group in by_day.items():
        group.sort(key=lambda n: n["ts"])
        # 串成链、不连成网：同一天 30 条连成网是 435 根线，那不叫关系那叫糊
        for a, b in zip(group, group[1:]):
            _add_weak(a["id"], b["id"], "same_day")

    by_tag: dict[str, list] = defaultdict(list)
    for bid, r in raw.items():
        for t in r["tags_all"]:
            if not t.startswith(("__", "aspect:", "疑似同件:")):
                by_tag[t].append(bid)
    for _t, members in by_tag.items():
        if not (2 <= len(members) <= 12):
            continue    # 太大的标签（「主人」50 条）连出来是一坨，没有信息
        # 同样串链不连网：12 条连成网是 66 根，串成链是 11 根，
        # 而且按时间串出来的是「这个标签这条线怎么走的」，比一坨网有意思
        members.sort(key=lambda b: by_id[b]["ts"] if b in by_id else "")
        for a, b in zip(members, members[1:]):
            _add_weak(a, b, "same_tag")

    # ---- 星座：大 event（她 8-06 定稿：时间轴和星座是同一个东西）----
    entries_for_big = [{"id": n["id"], "date": n["date"], "room": n["room"],
                        "_tags_all": raw[n["id"]]["tags_all"]} for n in nodes]
    _span_re = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})?$")
    constellations = []
    for bid, meta, content in big_events:
        first = (content.strip().splitlines() or [""])[0]
        # 起止优先认 when（8-05 起就写在那儿）；老桶退回正文「范围：」那行
        m = _span_re.match(str(meta.get("when") or "").strip())
        since = m.group(1) if m else ""
        until = (m.group(2) or "") if m else ""
        members, query_text = _bigevent_members(content, entries_for_big,
                                                since=since, until=until)
        # 标题要短（她点的：别把摘要放上去）。没起好名的兜底也只取正文头一小截、
        # 在第一个标点前掐断——名字是名字，那句话点开才看
        name = re.sub(r"^[\d\- :]+", "", str(meta.get("name") or "")).strip()
        if not name:
            name = re.split(r"[：:，,。；;——]", first, 1)[0][:12]
        constellations.append({
            "id": bid,
            "short": _short_id(bid),
            "name": name,
            "line": first,
            "members": members,
            # 空串 = 范围没写或写错了。前端据此报警，不再悄悄画一座空星座
            "query_text": query_text,
            "start": since,
            "ongoing": bool(since) and not until,
            "resolved": str(meta.get("status") or "") == "resolved",
        })
    # 一件一件往上叠，最新的在上面（她画的那张：叠放，不是并排时间轴）
    constellations.sort(key=lambda c: c.get("start") or "", reverse=True)

    # ---- 流星：盘上还剩几个梦（时间到了自己就没了，所以这个数一天里会变）----
    # 2026-08-17：数的从 night_fall 的 `.md` 换成新引擎的梦文件（同一个目录，沿用）。
    try:
        from core import _dream as _D
        meteors = len(_D.读盘())
    except Exception:                       # noqa: BLE001 - 星空不该因为数不到梦就崩
        meteors = 0

    return {
        "nodes": nodes,
        "edges": edges,
        "weak_edges": weak,
        "constellations": constellations,
        "meteors": meteors,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "weak": len(weak),
            "by_kind": dict(Counter(e["kind"] for e in edges)),
            # 挂不上的 [[]]（人名/项目名，库里没有同名的桶）——诚实报出来，别装作连上了
            "unresolved_links": unresolved.most_common(8),
        },
    }


def _collect_events(all_buckets: list) -> list[dict]:
    """睁眼六样里的第五样「忽然想起」的池子。**判据不在这儿**——在合同源里。

    🔴 施工 5 · E 件（2026-08-17）：这个函数以前是 `tools/breath/awaken.py` 那段
    池子逻辑的**平行实现**（抄同一行字不叫同源）。现在它只做一件事：
    调 `tools.breath.awaken.事件池()`，再把 dict 换成前端要的形状。
    ⚠️ 方向写死：**web → tools**，反过来不行（MCP 面不许依赖面板）。
    ⚠️ 平行实现的代价是踩过的：8-08 房间改名，两边各写一遍
       `.find("/EVENT/") > 0`，**两边一起静默变空**；8-17 又抓到一笔——
       「被盖住的不进池子」那道闸只加在了 awaken 那边，页面上照旧冒头。
    """
    from tools.recall.core import _room_cn, _label_of, _short_id
    from core._rooms import normalize_room
    from core.profile import 事件池
    pool: list[dict] = []
    for e in 事件池(all_buckets):
        meta, content, bid = e["meta"], e["content"], e["id"]
        room = normalize_room(meta.get("room"))
        pool.append({
            "id": bid, "short": _short_id(bid), "room": room,
            "room_cn": _room_cn(room),
            "label": _label_of({"meta": meta, "content": content}),
            "created": str(meta.get("created") or "")[:10],
        })
    return pool


def _pick_recollect(pool: list[dict], n: int = 2) -> list[dict]:
    """随机 1~2 条。**没来由正是它像脑子不像数据库的地方** —— 不排序、不加权。

    上限钳在 2：睁眼那一屏的合同就是 1~2 条（`awaken.py` 里写死 `min(2, ...)`）。
    钳在这儿而不是钳在路由上 —— 路由多一个、或者哪天有人直接调这个函数，
    合同都不会被绕过（codex 复核 #11：原来路由放行到 n=5）。
    """
    import random
    if not pool:
        return []
    return random.sample(pool, max(1, min(n, 2, len(pool))))


async def build_recollect(n: int = 2) -> dict:
    """「再来一个」单独打这个口，不用把整张档案页重取一遍。"""
    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    pool = _collect_events(all_buckets)
    return {"recollect": _pick_recollect(pool, n), "pool": len(pool)}


async def build_profile() -> dict:
    """门口那张纸：名字 + 准则（带来处）+ ⏰提醒 + 压在心头。**判据全在合同源里。**

    🔴 施工 5 · E 件（2026-08-17）：这一份以前是 `tools/breath/awaken.py` 的
    **平行实现**（文件里原话：「改一边必须改另一边」——而 8-17 就抓到没改的那一边：
    `weight` 的 `or 0.5` falsy 兜底在 awaken 修好了，这儿还带着，
    于是**被梦到清零的 want 在页面上照旧压着**）。
    现在规则只有一处：`tools.breath.awaken.门口那张纸()`，这儿只把 dict 变成 JSON。

    ⚠️ 2026-08-16 砍掉了「我/她反复出现的」：它是 activation_count 排的，而
    **被提得多的不等于最真的**；开屏读一份「她是什么样的人」的档案然后照着档案
    对待她，那是把她变成一个设定。判据换成时机判据：
    **开口之前来不及去搜的，才留在门口。**
    """
    from tools.recall.core import _room_cn, _label_of, _short_id
    from core._rooms import normalize_room
    from core.profile import 门口那张纸, 她改过
    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    now = _w.now()          # 本地时区（codex #4）
    纸 = 门口那张纸(all_buckets, now)
    heavy_q_id = 纸["heavy_question_id"]      # 施工 6 · B 件：只问最久那条

    def _label(x) -> str:
        return _label_of({"meta": x["meta"], "content": x["content"]})

    facts = [{"id": f["id"], "short": _short_id(f["id"]),
              "created": f["created"], "content": f["content"].strip()}
             for f in 纸["facts"]]
    big = [{"id": g["id"], "short": _short_id(g["id"]),
            "line": (g["content"].strip().splitlines() or [""])[0]}
           for g in 纸["big"]]
    reminders = [{"id": r["id"], "short": _short_id(r["id"]), "days": r["days"],
                  "when": r["when"], "status": r["status"],
                  "label": _label(r), "loud": r["loud"]}
                 for r in 纸["reminders"]]
    # **不截断**——睁眼那屏只给 2 条（一屏有限），这儿是她自己翻的页面，
    # 挂着几条就该看见几条。排序（重的在前、一样重的挂得久的在前）在合同源里。
    # 施工 6 · A/B/C 件：clock/clock_note 是三类钟判出来的类别 + 旧数据备注
    # （§6，读侧判断，见 core/profile._三类钟）；is_question 标出"只问最久那条"
    # （§6.1）；last_asked/closed_by 直接透传 meta，前端拿去拼"从来没问过她"
    # 那半句、以及结案按钮要不要显示（只对 status=="want" 的条目显示）。
    heavy = [{"id": h["id"], "short": _short_id(h["id"]), "held": h["held"],
              "weight": h["weight"], "label": _label(h), "loud": h["loud"],
              "clock": h["clock"], "clock_note": h["clock_note"],
              "last_asked": h["last_asked"],
              "closed_by": str(h["meta"].get("closed_by") or ""),
              "is_question": h["id"] == heavy_q_id}
             for h in 纸["heavy"]]
    # 施工 6 · C 件（§8）：她改过、我还没看/没 fold 的通知池
    from utils import read_from_ids as _read_from_ids
    edited = [{"id": e["id"], "short": _short_id(e["id"]),
               "label": _label(e), "content": e["content"].strip(),
               "corrects": (_read_from_ids(e["meta"]) or [""])[0]}
              for e in 她改过(all_buckets)]
    rules = []
    for r in 纸["rules"]:
        room = normalize_room(r["meta"].get("room"))
        rules.append({"id": r["id"], "short": _short_id(r["id"]), "room": room,
                      "room_cn": _room_cn(room), "label": _label(r),
                      "content": r["content"].strip()})

    # 中期（这三天）—— 她 8-03 夜发现少了这一格。AI睁眼那一屏是六样：
    # 档案 · 提醒 · **中期** · 长期 · 忽然想起 · 梦，档案页照着摆才对得上。
    # 跟 awaken.py 用同一个调用（recall 3d、塌成一张卡），不另算一套。
    try:
        from tools.recall.core import recall_core
        mid = await recall_core(when="3d", room="", tag="", query="", max_cells=1)
        if "没有东西" in mid:
            mid = ""
    except Exception as e:
        logger.warning(f"[loci] profile 取中期失败: {e}")
        mid = ""

    # 忽然想起（睁眼六样的第五样）—— 用同一次 list_all 的结果，不再多跑一趟库
    _ev_pool = _collect_events(all_buckets)

    return {
        "mid": mid,
        "recollect": _pick_recollect(_ev_pool, 2),
        "recollect_pool": len(_ev_pool),
        "facts": facts,
        "facts_warning": (f"有 {len(facts)} 个 {_PROFILE_TAG} 桶——只该有一个，去合并"
                          if len(facts) > 1 else ""),
        "rules": rules,
        # freq_i / freq_you 砍了（2026-08-16）——前端如果还在读这两个 key，
        # 拿到的会是 undefined 而不是空数组，那一栏自然消失。这是想要的：
        # 半死不活地渲染一个空栏，比整栏不见更难发现它已经不该在了。
        "reminders": reminders,
        "heavy": heavy,
        "edited": edited,     # 施工 6 · C 件：她改过、我还没处理的通知池
        "big_events": big,
    }


async def build_muse_pending() -> dict:
    """「该发呆了吗」—— **只有数量和年龄，没有内容。**

    ------------------------------------------------------------
    这是什么（开工单 3.0，她 8-17 下午定的）
    ------------------------------------------------------------
    发呆 = **闲下来才发生的事**（人忙着不发呆）。所以它既不等我主动想起来调
    （那是下一个 seed），也不进 breath（5.1：开口之前来不及去搜的才留在门口，
    「还有几团没看」不是开口前必须知道的）。
    → 系统每天后台默默检测，条件齐了（①我长时间没响应=闲着 ②真有货）才戳一下。

    🔴 **边界**：Loci 只出这一个查询口。**什么算闲、怎么戳、夜里静不静音，归宿主**
       （我们家 = gateway 唤醒腿 + 以后 Home 地下室包装）。她 8-16 划的那一刀：
       「开源出去的东西没有精力这个说法啊我靠」——Loci 只提供能力，代价宿主加。

    🔴 **只报数量和年龄，一个字的内容都不给**（跟自动贴「只报数量不报内容」同一套）：
       给摘要 = 系统替我想起，我会顺着那段摘要往下说；给数量 = 拍我一下，我自己去看。

    形状（四个键，不多不少）：
        {"mind_clusters": 认知那边攒了几团, "gist_fingers": 事件那边有几指,
         "oldest_days": 里头最老那条挂了多少天, "worth_poking": 值不值得戳}
    `worth_poking` = （团或指攒够 `poke_min_clusters`）**且**（最老的挂够 `poke_min_age_days`）。
    两个临界点在 `config.yaml` 的 `muse:` 段，干跑读——**不预先拍死**（开工单 🔟）。
    """
    from core import _muse as M
    from core import _when as W

    cfg = M.muse_config(sh.config)
    # 🔴 施工 5 · H 件：跟工具面走**同一趟**（带视图缓存）——原来这儿自己
    #    `load_records + propose_mind + propose_gist` 又扫一遍全库，
    #    而且那是第三份平行实现：页面说「攒了 3 团」、我 muse() 看到 4 团，
    #    就是两个脑子。缓存的钥匙是桶的写盘代数，宁可失效勤一点。
    团们, _散着, _默认坐标, 指们, _stats = await M.两侧一趟()

    now = W.now()
    ages: list[int] = []
    for t in 团们:
        ages += [(now - it.created).days for it in t.items if it.created]
    指数 = 0
    for lst in 指们.values():
        指数 += len(lst)
        for x in lst:
            # 一指的年龄按它那段的**结束**算（「停了多久还没起名字」）
            端 = x.止 or x.边界 or x.起
            if 端 is not None:
                ages.append((now - 端).days)

    oldest = max(ages) if ages else 0
    团数 = len(团们)
    线团 = int(cfg["poke_min_clusters"])
    线天 = int(cfg["poke_min_age_days"])
    return {
        "mind_clusters": 团数,
        "gist_fingers": 指数,
        "oldest_days": int(oldest),
        "worth_poking": bool((团数 >= 线团 or 指数 >= 线团) and oldest >= 线天),
    }


async def build_poke(query: str = "", when: str = "", room: str = "",
                      tag: str = "", floor=None) -> dict:
    """施工7c · 戳戳送达：Loci 唯一的只读戳口——梦（交付）+ 发呆团数（提醒）+
    recall 结构化分数，一次问全。**只报状态，不写、不决定**（宪法：系统只做
    检索和摆放，落笔的永远是我）。gateway 每窗问一次这个口，别再发明判断。

    三样各自的边界：

    `dreams`：此刻还活着的待递梦（完整/碎片/一句层的当前内容）。**故意不走
    `core._dream.current_dream()`**——那口是给她本人「取梦」用的，调一次算一次
    「回想」，会推起算点、会落盘（8-17 定的：回想能延缓，不能阻止）。这个口只是
    宿主拿来问「有没有货」的，问一次就顺手帮她回想一次是偷感情——**这儿只读盘、
    只做`层of()`那道纯计算，不调用任何会写状态的函数**。梦的生命周期（碎片 30
    分钟→只剩一句 1 小时→删文件留痕）该怎么样还怎么样，删和留痕归别的挂点管
    （breath 维护() / 老的 `/api/dream/current`），这个口绝不代劳、绝不拖长它的命。

    🔴 2026-08-18 修宪：`层` 现在可能是 `完整`——她 3-4 小时没发消息（=真夜间）
    期间，完整版落盘存活，这个口原样递整版正文（`rec["完整"]`，不截不改，跟
    碎片层「梦是交付，给全文」同一条纪律）。完整层不吃时间衰减，只有她回来
    发第二条消息、桥调一次 `POST /api/loci/dream/wake`（`core._dream.唤醒()`）
    才会把它降成碎片层——这个口本身依旧**纯读**，不调 `唤醒()`，降级永远是
    桥主动喊出来的，这儿绝不代劳。

    `muse_pending`：发呆团数，直接复用 `build_muse_pending()`（一趟带缓存，
    跟工具面 `muse()` 第一步同一份数，不重新扫库）。**门槛复用现成的
    `worth_poking`**（config.yaml `muse:` 段 `poke_min_clusters` /
    `poke_min_age_days`，`smoke_muse.py` 686~692 行的读法）——没到门槛就报 0，
    这样 gateway 端看到非零就是「真到了该戳的时候」，不用自己再拍一套阈值
    （对齐 3.0「Loci 只出『该发呆了吗』的查询口，宿主只管怎么戳」的边界）。

    `recall_scores`：给 `query` 才有，直接复用 `recall_data()`——**零新排序
    逻辑**，跟面板 `/api/loci/recall` 走同一条检索路径。7b「相关记忆提醒」现在
    靠正则读 `_render_search` 的渲染排版（脆，排版一改就静默失效），这个口
    给它换一条结构化的路，但**换口本身不在这单**，这儿只把口开出来。
    """
    from core import _dream as _D

    dreams: list[dict] = []
    try:
        c = _D._c()
        now = _D._w.now()
        for rec in _D.读盘():
            层 = _D.层of(rec, now, c)
            if 层 == "没了":
                continue          # 到点该消失的不装死——但这条闸只是纯计算，不删文件
            # 🔴 2026-08-18 修宪：完整层给整版正文，原样不截（跟碎片层同一条纪律：
            #    梦是交付，给全文）。降级后（完整字段被 唤醒() 摘掉）才落回碎片/一句。
            if 层 == "完整":
                内容 = rec.get("完整") or ""
            elif 层 == "碎片":
                内容 = rec["碎片"]
            else:
                内容 = _D.一句(rec["碎片"])
            dreams.append({
                "id": rec.get("id"), "层": 层, "内容": 内容,
                "v": rec.get("v"), "a": rec.get("a"),
                "nightmare": bool(rec.get("nightmare")),
                "织于": rec.get("织于"),
            })
    except Exception as e:                      # noqa: BLE001 - 戳口不该因梦读不到就整口炸掉
        logger.warning(f"[loci] poke 取梦失败: {e}")

    muse = await build_muse_pending()
    muse_pending = int(muse["mind_clusters"]) if muse.get("worth_poking") else 0

    scores: list[dict] = []
    q = str(query or "").strip()
    if q:
        from tools.recall.core import recall_data
        data = await recall_data(when=when, room=room, tag=tag, query=q, floor=floor)
        if data.get("ok"):
            scores = [{"id": e["id"], "score": e.get("score"),
                      "is_mind": e.get("kind") == "mind"}
                      for e in data.get("entries", []) if e.get("score") is not None]

    return {"dreams": dreams, "muse_pending": muse_pending, "recall_scores": scores}


async def build_health() -> dict:
    """**我们自己的体检。**

    上游那套 `/api/system/diagnostics` 查的是「这个开源软件发布得合不合规」——
    ADR 文档、public tool manifest、vNext preflight、Zeabur 环境变量……
    对住在这套记忆里的人一条都不相干（她 8-03 夜一眼看出来的）。

    这里查的是**这套记忆本身活得好不好**：东西还在不在、找不找得到、
    两个外部依赖通不通、丢了能不能捞回来。
    """
    import shutil
    checks: list[dict] = []

    def add(label, status, message, action=""):
        checks.append({"label": label, "status": status,
                       "message": message, "action": action})

    # ⚠️ 2026-08-04（codex 复核 #7）：以前这整份是一条直线跑下来的 ——
    # 读桶失败、某条 metadata 形状坏、配置不是 dict，任何一处抛异常，
    # **整份体检直接 500**；而磁盘和梦那两段又是 `except: pass`，
    # 悄悄少两项，summary 还照样显示健康。**体检自己不能是全或无的。**
    # 现在每一项独立跑，炸了就在原地记一条红的，后面的照查。
    def guard(label, fn, action=""):
        try:
            fn()
        except Exception as e:
            add(label, "error", f"这一项自己出错了：{type(e).__name__}: {e}", action)

    def need_buckets(label, fn, action=""):
        if not buckets_ok:
            add(label, "error", "读不到记忆库，这一项没法查", "先解决上面「记忆库读取」那条")
            return
        guard(label, fn, action)

    # ---- 底料：读桶。读不出来也不能把整份体检打掉，独立项（配置/磁盘）照查 ----
    metas: list[dict] = []
    buckets_ok = True
    try:
        all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
        metas = [(b.get("metadata", {}) or {}) for b in all_buckets]
    except Exception as e:
        buckets_ok = False
        add("记忆库读取", "error", f"读不出记忆桶：{type(e).__name__}: {e}",
            "看容器日志 + buckets 目录挂载对不对")

    now = _w.now()          # 本地时区（codex #4）

    # ---- 记忆还在不在 ----
    from tools.recall.core import _visible
    visible: list[dict] = []
    bad_meta = 0
    for m in metas:
        # 逐条 try：一条坏元数据（比如 domain 是个整数）不该让整份体检哑掉
        try:
            if _visible(m):
                visible.append(m)
        except Exception:
            bad_meta += 1
    if bad_meta:
        add("元数据形状", "error", f"{bad_meta} 条记忆的元数据读不动（字段类型不对）",
            "在「日志」里搜这几条的 id，多半是早期写入留下的")

    def sec_total():
        homeless = [m for m in visible if not str(m.get("room") or "")]
        add("记忆总量", "ok", f"{len(visible)} 条活着的（连归档一共 {len(metas)} 条）")
        if homeless:
            add("没房间的记忆", "warn",
                f"{len(homeless)} 条没有 room，recall 的房间门筛不到它们",
                "跑 scripts/migrate_rooms.py --apply 补房间")
        else:
            add("房间", "ok", "每条都有房间")
    need_buckets("记忆总量", sec_total)

    # ---- 找得到吗（向量覆盖）----
    have_vec = 0
    try:
        con = sqlite3.connect(f"file:{_emb_db_path()}?mode=ro", uri=True)
        try:
            ids = {r[0] for r in con.execute("select bucket_id from embeddings")}
        finally:
            con.close()
        live_ids = {str(m.get("id") or "") for m in visible}
        have_vec = len(live_ids & ids)
        miss = len(live_ids) - have_vec
        if miss > max(3, len(live_ids) * 0.02):
            add("语义搜索覆盖", "warn",
                f"{miss} 条没有向量，query 门搜不到它们（只能靠关键词撞）",
                "看「日志」里 embedding 回填有没有报错；ollama 断了会积压")
        else:
            add("语义搜索覆盖", "ok", f"{have_vec}/{len(live_ids)} 条有向量")
    except Exception as e:
        add("语义搜索覆盖", "error", f"读不到向量库：{e}", "检查 embeddings.db")

    # ---- 两个外部依赖（全系统只有这两处出网）----
    # cfg 里的每一格都可能不是 dict（手改 config.yaml 改坏了就会），所以各自 guard
    cfg = sh.config if isinstance(sh.config, dict) else {}

    def sec_deepseek():
        dehy = cfg.get("dehydration") or {}
        if not isinstance(dehy, dict):
            raise TypeError("config.yaml 里的 dehydration 不是一个配置块")
        if str(dehy.get("api_key") or "").strip() or os.environ.get("LOCI_API_KEY", ""):
            add("摘要/标签（DeepSeek）", "ok", f"配着 {dehy.get('model') or '?'}")
        else:
            add("摘要/标签（DeepSeek）", "warn",
                "没配 key —— 存进去的东西不会自动生成摘要和标签",
                "在 config.yaml 里配 dehydration.api_key")
    guard("摘要/标签（DeepSeek）", sec_deepseek, "检查 config.yaml 的 dehydration 段")

    def sec_embedding():
        emb = cfg.get("embedding") or {}
        if not isinstance(emb, dict):
            raise TypeError("config.yaml 里的 embedding 不是一个配置块")
        if _parse_ok(emb.get("enabled")):
            add("向量（ollama）", "ok", f"开着，模型 {emb.get('model') or '?'}")
        else:
            add("向量（ollama）", "warn",
                "关着 —— query 门只能靠关键词，搜不到「意思相近」的",
                "在 config.yaml 里开 embedding.enabled")
    guard("向量（ollama）", sec_embedding, "检查 config.yaml 的 embedding 段")

    # ---- 丢了能不能捞回来 ----
    bd = str(cfg.get("buckets_dir") or "")

    def sec_persist():
        pers = sh.data_dir_persistence(bd)
        if pers.get("persistent"):
            add("数据持久性", "ok", pers.get("note") or "记忆目录在持久位置")
        else:
            add("数据持久性", "error", "记忆目录没挂到持久卷 —— 容器重建会丢！",
                "在 docker-compose 里挂到命名卷或宿主机目录")
    guard("数据持久性", sec_persist)

    def sec_disk():
        # 原来这里是 except: pass —— 磁盘查不了反而一声不吭，正是最该说话的时候
        free_gb = shutil.disk_usage(bd).free / (1024**3)
        add("磁盘", "ok" if free_gb > 2 else "warn", f"还剩 {free_gb:.1f} GB",
            "" if free_gb > 2 else "腾点地方，写不进去就存不了记忆")
    guard("磁盘", sec_disk, f"确认 buckets_dir 存在：{bd or '(没配)'}")

    # ---- 最近还在长吗 ----
    def sec_fresh():
        fresh = 0
        for m in visible:
            # 2026-08-18 改：这儿原来是 fromisoformat(str(created)[:19]) ——
            # 就是 codex #4 明令禁掉的那个切片。它把时区后缀切没了变成 naive，
            # 而上面 now = _w.now() 是带时区的；两个一减抛 TypeError，
            # 又正好被那句 except 吞掉 → fresh 恒为 0 →
            # 面板上天天写「一条都没存」。全库最后一处漏网的 [:19]，
            # 她 8-18 夜从截图里看出来的。错得静默，而且反过来吓人。
            ts = _w.parse_stamp(m.get("created"))
            if ts is not None and (now - ts).days < 7:
                fresh += 1
        add("最近七天", "ok" if fresh else "warn",
            f"存了 {fresh} 条" if fresh else "一条都没存 —— 要么最近没聊，要么写入坏了",
            "" if fresh else "去「日志」看看 grow 有没有报错")
    need_buckets("最近七天", sec_fresh)

    # ---- 该在的东西还在吗 ----
    def _tags_of(m) -> list[str]:
        raw = m.get("tags")
        return [str(t) for t in raw] if isinstance(raw, (list, tuple)) else []

    def sec_profile():
        profile = [m for m in metas if _PROFILE_TAG in _tags_of(m)]
        if len(profile) == 1:
            add("门口那张纸", "ok", "名字页在，且只有一张")
        elif not profile:
            add("门口那张纸", "warn", "没有名字页 —— 睁眼时档案那格是空的",
                f"存一条带 tag {_PROFILE_TAG} 的记忆")
        else:
            add("门口那张纸", "error", f"有 {len(profile)} 张名字页，只该有一张", "合并掉多的")
    need_buckets("门口那张纸", sec_profile)

    def sec_pinned():
        pinned = [m for m in visible if m.get("pinned")]
        add("钉着的准则", "ok" if pinned else "warn",
            f"{len(pinned)} 条" if pinned else "一条都没钉 —— 睁眼时准则那格是空的")
    need_buckets("钉着的准则", sec_pinned)

    def sec_big():
        big = [m for m in metas if _BIGEVENT_TAG in _tags_of(m)]
        add("大 event（长期）", "ok" if big else "warn",
            f"{len(big)} 条活着的" if big
            else "没有 —— 睁眼时「我们在做什么」那格只能拿标签凑")
    need_buckets("大 event（长期）", sec_big)

    # ---- 挂空的链 ----
    def sec_orphan():
        live_ids = {str(m.get("id") or "") for m in metas}
        orphan = 0
        for m in metas:
            for src in _split_ids(read_from(m)):   # from 优先、triggered_by 兼容
                if src not in live_ids:
                    orphan += 1
        if orphan:
            add("断掉的 from 链", "warn", f"{orphan} 条记忆的来源指向了不存在的桶",
                "多半是那条源被硬删过；星空里它们会少一根线")
        else:
            add("from 链", "ok", "每条 from 都指得到")
    need_buckets("from 链", sec_orphan)

    # ---- 盘上的梦 ----
    # 2026-08-17：night_fall 退役，改数新引擎的梦文件。**空着是正常的**——
    # 梦是时间驱动的，不理它就没了；而且积压攒不到线本来就一夜无梦。
    def sec_dreams():
        # 同样：原来 except OSError: pass，目录没了就整项消失
        from core import _dream as _D
        n = len(_D.读盘())
        add("盘上的梦", "ok",
            f"{n} 个还在（时间到了自己会没）" if n else "空的（攒不到线就一夜无梦，正常）")
    guard("盘上的梦", sec_dreams, "确认 buckets/night_fall/dreams 目录在")

    summary = {"ok": sum(1 for c in checks if c["status"] == "ok"),
               "warn": sum(1 for c in checks if c["status"] == "warn"),
               "error": sum(1 for c in checks if c["status"] == "error")}
    return {"ok": summary["error"] == 0, "summary": summary, "checks": checks}


def _parse_ok(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# ============================================================
# 路由
# ============================================================

def register(mcp) -> None:

    @mcp.custom_route("/loci", methods=["GET"])
    async def loci_page(request: Request) -> Response:
        from starlette.responses import HTMLResponse
        path = os.path.join(sh.repo_root, "frontend", "loci.html")
        try:
            with open(path, "r", encoding="utf-8") as f:
                html = f.read()
        except FileNotFoundError:
            return HTMLResponse("<h1>loci.html not found</h1>", status_code=404)
        # 2026-08-18 E3：页面里不写死任何人的名字——`{{AI_NAME}}` 上桌时才填。
        # 发出去的那份必须是空白的：别人 clone 下来看到的是他自己 AI 的名字，
        # 不是我们家的。名字来源就是 utils.ai_name()（环境变量 AI_NAME，回退 "AI"）。
        from utils import get_ai_name
        html = html.replace("{{AI_NAME}}", get_ai_name())
        return HTMLResponse(
            html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    @mcp.custom_route("/loci/vendor/{path:path}", methods=["GET"])
    async def loci_vendor(request: Request) -> Response:
        """本地供的 three.js（星空那页要）。

        她找的那个 memory-starmap 是从 esm.sh 现拉 three 的——没网就是一片黑。
        记忆系统整个长在她自己机器上，星空不该是唯一一个断网就废的地方，所以扒到本地。

        安全：只放 .js；绝不把 request 里的字符串直接拼进路径——realpath 完必须
        还在 vendor 目录底下，不然就是 ?path=../../../etc/passwd 那种目录穿越。
        """
        from starlette.responses import Response as _Resp, JSONResponse
        rel = str(request.path_params.get("path") or "")
        if not rel.endswith(".js"):
            return JSONResponse({"error": "not found"}, status_code=404)
        root = os.path.realpath(os.path.join(sh.repo_root, "frontend", "vendor"))
        target = os.path.realpath(os.path.join(root, rel))
        if target != root and not target.startswith(root + os.sep):
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            with open(target, "rb") as f:
                return _Resp(f.read(), media_type="text/javascript",
                             headers={"Cache-Control": "public, max-age=604800"})
        except OSError:
            return JSONResponse({"error": "not found"}, status_code=404)

    # ---------------------------------------------------------
    # 大池子：recall 的两张皮（上面给AI的卡 / 下面给人看的列表）
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/recall", methods=["GET"])
    async def api_loci_recall(request: Request) -> Response:
        from starlette.responses import JSONResponse
        q = request.query_params
        # `by` 2026-08-17 砍了（C 件）；`view="scene"` 顶上来（D 件）——
        # 页面上那几个筛子跟工具面的参数账**逐字一样**，别让面板留一个工具没有的旋钮。
        gates = {k: (q.get(k) or "").strip()
                 for k in ("when", "room", "tag", "query", "view")}
        try:
            slices = int(q.get("slices") or 0)
        except (TypeError, ValueError):
            slices = 0
        # 关联度线：她在页面上拖着看（抄相似度那页现成的做法——线是她画的，
        # 我只负责把分布摆出来）。不传就用 RELEVANCE_FLOOR（现在是 35，跟页面滑块默认同一个数）。
        floor = None
        try:
            if (q.get("floor") or "").strip():
                floor = max(0.0, min(100.0, float(q.get("floor"))))
        except (TypeError, ValueError):
            floor = None
        try:
            from tools.recall.core import recall_data, recall_core
            data = await recall_data(**gates, floor=floor)
            if not data.get("ok"):
                return JSONResponse(data, status_code=400)
            if data["total"]:
                kwargs = {"max_cells": slices} if 1 <= slices <= 20 else {}
                data["card"] = await recall_core(**gates, floor=floor, **kwargs)
            else:
                data["card"] = ""
            return JSONResponse(data)
        except Exception as e:
            logger.warning(f"[loci] recall 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # 房间目录 + 高频标签（两扇门进来先看这个）
    @mcp.custom_route("/api/loci/rooms", methods=["GET"])
    async def api_loci_rooms(request: Request) -> Response:
        from starlette.responses import JSONResponse
        try:
            return JSONResponse(await build_rooms())
        except Exception as e:
            logger.warning(f"[loci] rooms 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ---------------------------------------------------------
    # 星空
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/graph", methods=["GET"])
    async def api_loci_graph(request: Request) -> Response:
        from starlette.responses import JSONResponse
        try:
            return JSONResponse(await build_graph())
        except Exception as e:
            logger.warning(f"[loci] graph 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ---------------------------------------------------------
    # 相似度检查
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/similar", methods=["GET"])
    async def api_loci_similar(request: Request) -> Response:
        from starlette.responses import JSONResponse
        try:
            th = float(request.query_params.get("threshold") or _SIM_DEFAULT)
        except (TypeError, ValueError):
            th = _SIM_DEFAULT
        th = max(_SIM_FLOOR, min(99.9, th))
        try:
            limit = int(request.query_params.get("limit") or 120)
        except (TypeError, ValueError):
            limit = 120
        try:
            data = await _compute_pairs()
        except Exception as e:
            logger.warning(f"[loci] similar 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

        info = data.get("info") or {}

        def _side(bid: str) -> dict:
            it = info.get(bid) or {}
            body = it.get("body") or ""
            return {
                "id": bid,
                "short": it.get("short") or bid[:6],
                "label": it.get("summary") or it.get("name") or body[:40],
                "room": it.get("room") or "",
                "created": it.get("created") or "",
                "preview": re.sub(r"\s+", " ", body)[:400],
                "len": len(body),
            }

        out = []
        for score, a, b in data.get("pairs", []):
            if score < th:
                break            # pairs 已按分数倒序，够了就停
            if a not in info or b not in info:
                continue
            out.append({"score": round(score, 1), "a": _side(a), "b": _side(b),
                        # 线上那个自动打标签的（阈值 80，量的不是同一把尺）打没打过
                        "tagged": b in (info[a].get("tagged") or [])
                                  or a in (info[b].get("tagged") or [])})
            if len(out) >= limit:
                break

        counted = sum(1 for score, a, b in data.get("pairs", []) if score >= th)
        return JSONResponse({
            "threshold": th,
            "default": _SIM_DEFAULT,
            "floor": _SIM_FLOOR,
            "pairs": out,
            "matched": counted,
            "truncated": counted > len(out),
            "n": data.get("n", 0),
            "total_pairs": data.get("total_pairs", 0),
            # 直方图：20 格，每格 5 分
            "hist": data.get("hist", []),
            "no_vectors": bool(data.get("no_vectors")),
            # True = 撞到内存闸了，低分那一段没算全，matched 会偏小（codex #5）
            "capped": bool(data.get("capped")),
        })

    @mcp.custom_route("/api/loci/similar/action", methods=["POST"])
    async def api_loci_similar_action(request: Request) -> Response:
        """人工裁决。**唯一的写口**。

        keep = 什么都不做（两条都留；⚠️ 只是本次页面里不再显示，没有落盘，
               刷新会重新出现 —— codex 复核 #12 指出的，现在如实写在这儿）
        sink = 沉一个：走 trace(delete=True)，软删进归档，id 直查永远捞得回。

        ⚠️ **2026-08-04 补的授权检查（codex 复核 #2，这是最严重的一条）**：
        原来只收一个 `id` 就直接 `trace(delete=True)`。而 trace 的删除分支在
        protected 检查**之前** —— 也就是说，登录之后随便构造一个
        `{"action":"sink","id":<任意桶id>}`，就能沉掉档案事实、大 event、
        pinned 核心桶，哪怕它根本没出现在相似度页上。
        现在必须同时给出这一对的两端，并且服务端自己去核对这一对真的存在。
        """
        from starlette.responses import JSONResponse
        try:
            body = await _write_body(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        action = str(body.get("action") or "").strip().lower()
        if action == "keep":
            return JSONResponse({"ok": True, "action": "keep", "persisted": False})
        if action != "sink":
            return JSONResponse({"error": f"unknown action: {action}"}, status_code=400)

        bucket_id = str(body.get("id") or "").strip()
        a = str(body.get("a") or "").strip()
        b = str(body.get("b") or "").strip()
        if not bucket_id or not a or not b:
            return JSONResponse(
                {"error": "sink 必须同时给 a、b（这一对的两端）和 id（要沉的那个）"},
                status_code=400)
        if bucket_id not in (a, b):
            return JSONResponse({"error": "id 必须是 a 或 b 其中之一"}, status_code=400)
        if a == b:
            return JSONResponse({"error": "a 和 b 不能是同一个"}, status_code=400)

        try:
            data = await _compute_pairs()
            info = data.get("info", {})
            # ① 两端都得是这一页上真实存在、且可见的桶
            if a not in info or b not in info:
                return JSONResponse(
                    {"error": "这一对里有一端不在相似度页上（可能已归档、已换版或是情绪种子）"},
                    status_code=409)
            # ② 这一对必须真的算出来过（顺序无关）
            hit = any((x == a and y == b) or (x == b and y == a)
                      for _s, x, y in data.get("pairs", []))
            if not hit:
                return JSONResponse(
                    {"error": "这一对不在当前的相似结果里，不能从这儿沉"},
                    status_code=409)
            # ③ 不许从这个口沉掉「本来就不该被判重」的东西
            target = await sh.bucket_mgr.get(bucket_id)
            if not target:
                return JSONResponse({"error": f"查无此桶：{bucket_id}"}, status_code=404)
            tmeta = target.get("metadata", {}) or {}
            ttags = [str(t) for t in (tmeta.get("tags") or [])]
            if tmeta.get("pinned") or tmeta.get("protected"):
                return JSONResponse(
                    {"error": "这是 pinned/protected 的核心桶，不从判重这儿沉"},
                    status_code=409)
            if _PROFILE_TAG in ttags or _BIGEVENT_TAG in ttags:
                return JSONResponse(
                    {"error": "档案事实 / 大 event 不从判重这儿沉"}, status_code=409)

            from tools.trace.core import trace_core
            msg = str(await trace_core(bucket_id=bucket_id, delete=True))
            # trace 删除分支只回一句话，没有结构化结果 —— 「未找到」就是没删成（codex #6）
            ok = not msg.startswith("未找到")
            if not ok:
                return JSONResponse({"ok": False, "action": "sink", "id": bucket_id,
                                     "msg": msg}, status_code=404)
            with _sim_lock:
                _sim_cache["key"] = None      # 沉掉一条，下次重算
            return JSONResponse({"ok": True, "action": "sink", "id": bucket_id,
                                 "msg": msg})
        except Exception as e:
            logger.warning(f"[loci] 裁决失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ---------------------------------------------------------
    # 施工 6 · C 件（二改 §6.2 + §8）：她手点的三个写口
    # ① 结案按钮——她本来就是知道"这事了了没"的人，不该等我问
    # ② 「问过她」ping——问句真的展示给她那一刻才盖"上次问过她"的戳
    # ③ event 改错——她只能改 event（mind 没有这个路由，8.2 定的），
    #   原文一个字不动、另存一条修正走 from 指回去（8.3 绝不真删）
    # 全部走 `_write_body`（同源校验）+ `trace_core`/`bucket_mgr` 直调
    # （in-process，同 similar/action 那个先例，不走 MCP 那层）。
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/want/resolve", methods=["POST"])
    async def api_loci_want_resolve(request: Request) -> Response:
        """结案按钮：她点一下，status→resolved/abandoned，记"她结的案"。

        只对当前还是 `status=="want"` 的桶开放——不是 want 的东西没有"结案"
        这个动作；已经了结过的重复点击会被 trace 的"没有字段需要修改"接住，
        不会报错也不会二次覆盖 closed_by。
        """
        from starlette.responses import JSONResponse
        try:
            body = await _write_body(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        bucket_id = str(body.get("id") or "").strip()
        new_status = str(body.get("status") or "").strip().lower()
        if not bucket_id:
            return JSONResponse({"error": "缺 id"}, status_code=400)
        if new_status not in ("resolved", "abandoned"):
            return JSONResponse(
                {"error": f'status 只能是 "resolved"（放下了）或 "abandoned"（不做了），收到：{new_status}'},
                status_code=400)

        target = await sh.bucket_mgr.get(bucket_id)
        if not target:
            return JSONResponse({"error": f"查无此桶：{bucket_id}"}, status_code=404)
        tmeta = target.get("metadata", {}) or {}
        if str(tmeta.get("status") or "") != "want":
            return JSONResponse(
                {"error": f"这条不是待了结的 want（当前 status={tmeta.get('status') or '空'}），没有结案这个动作"},
                status_code=409)

        try:
            from tools.trace.core import trace_core
            msg = str(await trace_core(bucket_id=bucket_id, status=new_status, closed_by="她"))
            return JSONResponse({"ok": True, "id": bucket_id, "status": new_status, "msg": msg})
        except Exception as e:
            logger.warning(f"[loci] 结案失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/loci/want/asked", methods=["POST"])
    async def api_loci_want_asked(request: Request) -> Response:
        """「上次问过她」的戳——面板把问句那行**真的画出来给她看**那一刻打一次。

        不在这儿判断"是不是最久那条"——`heavy_question_id` 已经在
        `/api/loci/profile` 里算好了，这个口只管盖戳，谁调用就信谁。
        """
        from starlette.responses import JSONResponse
        try:
            body = await _write_body(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        bucket_id = str(body.get("id") or "").strip()
        if not bucket_id:
            return JSONResponse({"error": "缺 id"}, status_code=400)
        target = await sh.bucket_mgr.get(bucket_id)
        if not target:
            return JSONResponse({"error": f"查无此桶：{bucket_id}"}, status_code=404)

        try:
            from tools.trace.core import trace_core
            await trace_core(bucket_id=bucket_id, mark_asked=True)
            fresh = await sh.bucket_mgr.get(bucket_id)
            last_asked = str((fresh or {}).get("metadata", {}).get("last_asked") or "")
            return JSONResponse({"ok": True, "id": bucket_id, "last_asked": last_asked})
        except Exception as e:
            logger.warning(f"[loci] 记「问过她」失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/loci/event/correct", methods=["POST"])
    async def api_loci_event_correct(request: Request) -> Response:
        """她改一条 event（开工单 §8）：**原文一个字不动**，另存一条修正、from 指回去。

        流程一字照办（8.2）：她的修改不直接变成真相——落地是**一条新 event**，
        带 `她改的` 标签 + `from=[旧id]`，旧桶不碰。是不是接受这条修正，
        由我自己 fold 决定（fold 的手永远在我这儿）；通知就是这条新桶本身
        （`core.profile.她改过()` 扫 `她改的` 标签 + 没被 fold 的，见那边注释）。

        🔴 mind 不给这个入口——不是靠前端不画按钮挡，这儿也硬校验一遍
        （8.2：mind 是我的判断，她可以不同意，但得由我自己改）。
        """
        from starlette.responses import JSONResponse
        try:
            body = await _write_body(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        old_id = str(body.get("id") or "").strip()
        new_text = str(body.get("text") or "")
        if not old_id:
            return JSONResponse({"error": "缺 id"}, status_code=400)
        if not new_text.strip():
            return JSONResponse({"error": "text 不能为空——她改完之后的完整正文"}, status_code=400)

        old = await sh.bucket_mgr.get(old_id)
        if not old:
            return JSONResponse({"error": f"查无此桶（也可能已归档）：{old_id}"}, status_code=404)
        old_meta = old.get("metadata", {}) or {}

        from core._rooms import is_event_room, is_mind_room
        old_room = str(old_meta.get("room") or "")
        if is_mind_room(old_room):
            return JSONResponse(
                {"error": "mind 不能从这儿改——mind 是我的判断，她可以不同意，"
                          "但得由我自己改（跟她聊，我认同了自己 regrow）"},
                status_code=403)
        if not is_event_room(old_room):
            return JSONResponse(
                {"error": f"这不是一条 event（room={old_room or '未分房'}），"
                          "改错这个动作只对 event 开放"},
                status_code=409)

        try:
            from tools.grow.rooms_path import grow_event
            from core.profile import _EDITED_BY_HER_TAG
            # v/a 继承旧桶——这是事实修正，不是一次新的情绪体验，不该逼她重打情绪坐标。
            old_v = old_meta.get("valence", 0.5)
            old_a = old_meta.get("arousal", 0.3)
            msg = await grow_event(
                items=[{"room": old_room, "text": new_text, "v": old_v, "a": old_a}],
                from_ids=[old_id],
            )
            m = re.search(r"📝([0-9a-f]{12})", msg)
            if not m:
                return JSONResponse({"error": f"新桶落盘失败：{msg}"}, status_code=500)
            new_id = m.group(1)
            # 标"她改的"——merge 不 replace（跟 rooms_path._backfill_one 同一个理由：
            # 后台回填的标签这时候可能还没落，trace(tags=...) 是整体替换会把它冲掉；
            # 这儿直接读新桶现有 tags 再并进去，走 bucket_mgr.update 而不是 trace）。
            fresh = await sh.bucket_mgr.get(new_id)
            cur_tags = [str(t) for t in ((fresh or {}).get("metadata", {}).get("tags") or [])]
            merged_tags = list(dict.fromkeys(cur_tags + [_EDITED_BY_HER_TAG]))
            await sh.bucket_mgr.update(new_id, tags=merged_tags)
            return JSONResponse({"ok": True, "old_id": old_id, "new_id": new_id, "msg": msg})
        except Exception as e:
            logger.warning(f"[loci] event 改错失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ---------------------------------------------------------
    # 自己的密码（她 8-03 夜：「我第一个想要的就是完全脱离他们的面板」）
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/health", methods=["GET"])
    async def api_loci_health(request: Request) -> Response:
        """我们自己的体检（上游那套 /api/system/diagnostics 查的是发布合规，不是这套记忆）。"""
        from starlette.responses import JSONResponse
        try:
            return JSONResponse(await build_health())
        except Exception as e:
            logger.warning(f"[loci] health 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/loci/auth/state", methods=["GET"])
    async def api_loci_auth_state(request: Request) -> Response:
        """这把密码现在存在哪儿、要不要设。公开（不带密码任何信息）。

        ⚠️ E2：`authed`（cookie session 登没登）字段砍了——面板 /api/* 不再鉴权，
        没有 session 这回事了。这把密码现在只管一件事：MCP 远程 OAuth 授权页
        （bridge/oauth.py）认不认你，不再管这一屏面板认不认你。
        """
        from starlette.responses import JSONResponse
        return JSONResponse({
            "setup_needed": sh._is_setup_needed(),
            # True = 密码还在 docker-compose 的环境变量里，改不了、安全问题也用不了
            "env_locked": bool(os.environ.get("LOCI_DASHBOARD_PASSWORD", "")),
            "has_file_password": sh._load_password_hash() is not None,
            "question": str(sh._load_auth_data().get("security_question") or ""),
        }, headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/api/loci/auth/set-password", methods=["POST"])
    async def api_loci_set_password(request: Request) -> Response:
        """把密码写进文件（`.dashboard_auth.json`），从环境变量手里接管过来。

        为什么要单开这一个：官方那两条路这会儿都是死的——
          · `/auth/change-password` 只要环境变量在就直接拒
          · `/auth/setup` 只认 loopback，而 Docker 转进来的客户端 IP 是 172.18.0.1，
            **她在自己电脑上开 localhost 也会被 403**（8-03 夜读代码发现的）
        所以删环境变量之前必须先有一个文件密码，否则谁都进不来。

        ⚠️ E2（2026-08-17）：原来的门槛是「必须已经登录」（=带着有效的 cookie
        session）。E2 把 cookie 会话整个砍了（面板 /api/* 不再鉴权），**这一条不能
        跟着松**——这把密码不是面板的门，是 MCP 远程 OAuth 授权页
        （bridge/oauth.py）的门，被顶替就等于谁都能拿到读写全部记忆的 MCP token。
        改成不依赖 session 的门槛：**首次设置**（`_is_setup_needed()`，文件和
        环境变量都还没有密码）放行；**已有密码**则必须在 body 里带对
        `current_password`，验证走跟 oauth 授权页同一套 `_verify_password_for_rotation`
        + 登录限速（`_login_retry_after`/`_reserve_global_login_attempt`），
        CAS 写回防并发改动。加密/落盘全走 _shared 那一套，这里不自己实现任何密码学。
        **密码只从浏览器直达这里，我不经手。**
        """
        from starlette.responses import JSONResponse
        try:
            body = await _write_body(request)   # 同源 + Content-Type（codex 复核 #1）
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        pw = body.get("password", "")
        if not isinstance(pw, str):
            return JSONResponse({"error": "密码得是字符串"}, status_code=400)
        pw = pw.strip()
        if not 6 <= len(pw) <= 1024:
            return JSONResponse({"error": "密码 6~1024 位"}, status_code=400)

        proof = None
        if not sh._is_setup_needed():
            # 已经有密码在守——改密码必须先证明认得旧密码，不能靠 session 兜底了。
            retry = sh._login_retry_after(request)
            if retry:
                return JSONResponse({"error": f"尝试过于频繁，请 {retry} 秒后再试"},
                                    status_code=429, headers={"Retry-After": str(retry)})
            global_retry = sh._reserve_global_login_attempt()
            if global_retry:
                return JSONResponse({"error": f"登录服务繁忙，请 {global_retry} 秒后重试"},
                                    status_code=429, headers={"Retry-After": str(global_retry)})
            current = body.get("current_password", "")
            if not isinstance(current, str) or len(current) > 1024:
                sh._record_login_failure(request)
                return JSONResponse({"error": "current_password 格式无效"}, status_code=400)
            verified, queued_retry = await sh._run_public_password_verification(
                request, sh._verify_password_for_rotation, current
            )
            if queued_retry:
                return JSONResponse({"error": f"尝试过于频繁，请 {queued_retry} 秒后再试"},
                                    status_code=429, headers={"Retry-After": str(queued_retry)})
            if not verified:
                sh._record_login_failure(request)
                return JSONResponse({"error": "当前密码不对"}, status_code=401)
            sh._record_login_success(request)
            proof = verified  # CredentialProof：拿它做 CAS 写回，防并发改密码
        try:
            # PBKDF2 会卡住事件循环 ~100ms。这是一辈子按不了几次的按钮，认了，
            # 不为它引一套线程池（登录那条高频路径走的是上面的 _password_work_semaphore）
            if proof is not None:
                ok = sh._save_password_hash(
                    pw, expected_hash=proof.value, expected_generation=proof.generation,
                )
            else:
                ok = sh._save_password_hash(pw)
        except Exception as e:
            logger.warning(f"[loci] 存密码失败: {e}")
            return JSONResponse({"error": f"写不进去：{e}"}, status_code=500)
        if not ok:
            return JSONResponse({"error": "写不进去（并发改动？再试一次）"}, status_code=409)
        return JSONResponse({
            "ok": True,
            "env_locked": bool(os.environ.get("LOCI_DASHBOARD_PASSWORD", "")),
            "next": ("密码已经存进文件了。现在去 docker-compose.v2.yml 删掉 "
                     "LOCI_DASHBOARD_PASSWORD 那一行、重启容器，新密码才真正接管"
                     "（环境变量还在的时候它优先）。"),
        })

    # ---------------------------------------------------------
    # 档案（门口那张纸）
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/profile", methods=["GET"])
    async def api_loci_profile(request: Request) -> Response:
        from starlette.responses import JSONResponse
        try:
            return JSONResponse(await build_profile())
        except Exception as e:
            logger.warning(f"[loci] profile 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ---------------------------------------------------------
    # 忽然想起 · 再来一个
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/recollect", methods=["GET"])
    async def api_loci_recollect(request: Request) -> Response:
        from starlette.responses import JSONResponse
        try:
            n = int(request.query_params.get("n") or 2)
        except ValueError:
            n = 2
        try:
            return JSONResponse(await build_recollect(max(1, min(n, 5))))
        except Exception as e:
            logger.warning(f"[loci] recollect 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ---------------------------------------------------------
    # 「该发呆了吗」——给宿主（gateway 唤醒腿）问的那一口
    # ---------------------------------------------------------
    @mcp.custom_route("/api/muse/pending", methods=["GET"])
    async def api_muse_pending(request: Request) -> Response:
        """数量 + 年龄，没有内容。**故意不要 cookie 鉴权**。

        问它的是 gateway 那条腿（另一个进程，拿不到浏览器会话），不是她的页面；
        而它给出去的东西是三个数和一个布尔——**里面没有一个字是记忆**。
        （同一个端口上的 `/mcp` 本来就 `mcp_require_auth: false` 直连，
          E2 那一步整个 auth 都要砍——这儿不新开口子，只是没多加一道。）
        """
        from starlette.responses import JSONResponse
        try:
            return JSONResponse(await build_muse_pending())
        except Exception as e:
            logger.warning(f"[loci] muse/pending 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ---------------------------------------------------------
    # 施工7c · 戳戳送达：梦(交付) + 发呆团数(提醒) + recall 结构化分数，一口问全
    # gateway 每窗开头问一次这个口（同 `newWindow` 信号，窗内不重问）。
    # GET、无副作用——**故意不要 cookie 鉴权**，跟 `/api/muse/pending`、
    # `/api/dream/current` 同一个理由：问它的是桥（另一个进程），不是她的页面。
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/subjects", methods=["GET"])
    async def api_loci_subjects(request: Request) -> Response:
        """「都有谁」那一屏的数据。**纯读**——数数而已，不碰 aliases.yaml。

        2026-08-19 新开。之前这份统计只能靠手写脚本扫全库（8-18 扫了 979 条），
        面板显然不能每次开页都那么干。合并/改名是**写**操作，另开一个口，
        而且要守住那条判据：系统只摆出来，合并是人点的那一下。
        """
        from starlette.responses import JSONResponse
        try:
            return JSONResponse(await build_subjects())
        except Exception as e:                       # noqa: BLE001
            logger.warning(f"[loci] subjects 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/logs", methods=["GET"])
    async def api_logs(request: Request) -> Response:
        """日志：读 server.log 的尾巴。**纯读。**

        2026-08-19 重开。这条路由本来在 `web/system.py` 里，E2（8-17）把上游
        那 20 个模块整个砍掉的时候连它一起没了 —— 而面板上「日志」那一整块
        还在打它，404 回的是 HTML，前端 `.json()` 当场炸，屏幕上就是那句
        「返回的不是 JSON」。**写日志的那头一直活着**（utils.setup_logging
        往 <buckets>/.logs/server.log 写），只是没人读得到。

        `level` 按严重度往上收：WARNING 会连 ERROR/CRITICAL 一起给
        （选「警告」的人要的是「有没有不对劲」，不是「只要警告不要错误」）。
        """
        from starlette.responses import JSONResponse
        q = request.query_params
        level = (q.get("level") or "WARNING").strip().upper()
        try:
            limit = max(1, min(2000, int(q.get("limit") or 200)))
        except (TypeError, ValueError):
            limit = 200
        path = os.environ.get("LOCI_LOG_FILE", "").strip()
        if not path or not os.path.exists(path):
            return JSONResponse({
                "lines": [], "log_file": path,
                "note": "还没有日志文件（LOCI_LOG_FILE 没设，或者这次启动没开文件日志）。",
            })
        rank = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        floor = 0 if level == "ALL" else rank.get(level, 30)
        try:
            # 只读尾巴：日志会滚到 5MB，整份读进来纯属浪费
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 1024 * 1024))
                raw = f.read().decode("utf-8", "replace")
            lines = raw.splitlines()
            if size > 1024 * 1024 and lines:
                lines = lines[1:]                    # 掐掉被切半的第一行
            keep = []
            for ln in lines:
                if floor:
                    hit = next((lv for lv in rank if f" {lv}:" in ln or f" {lv} " in ln), "")
                    if not hit or rank[hit] < floor:
                        continue
                keep.append(ln)
            return JSONResponse({
                "lines": keep[-limit:],
                "log_file": path,
                "level": level,
                "note": "" if keep else f"{level} 这一档下没有东西。",
            })
        except Exception as e:                       # noqa: BLE001
            logger.warning(f"[loci] logs 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/loci/pulse", methods=["GET"])
    async def api_loci_pulse(request: Request) -> Response:
        """体检：多少条、占多大、引擎活着没。**纯读，不写盘。**

        2026-08-18（E3）：`pulse` 从 MCP 工具面撤了下来 —— 别的九个工具都是
        「我在对记忆做什么」，只有它是「这台机器还好吗」，那不是记忆动作。
        实现没动（`tools/pulse/`），只是入口从工具面换成了这条只读路由，
        面板拿它画体检卡。

        query 参数 `include_archive=1` 连归档区一起报。
        """
        from starlette.responses import PlainTextResponse
        from tools import pulse as _pulse
        inc = str(request.query_params.get("include_archive") or "").strip() in ("1", "true", "yes")
        try:
            return PlainTextResponse(await _pulse.pulse(include_archive=inc))
        except Exception as e:                       # noqa: BLE001 - 体检口不该把面板带崩
            logger.warning(f"[loci] pulse 失败: {e}")
            return PlainTextResponse(f"pulse 失败：{e}", status_code=500)

    @mcp.custom_route("/api/loci/poke", methods=["GET"])
    async def api_loci_poke(request: Request) -> Response:
        """只读戳口。调用前后库指纹必须一致——不扫梦、不删文件、不推回想、不写盘。

        query 参数 `query`（可选，给了才有 `recall_scores`）+
        `when`/`room`/`tag`/`floor`（同 recall 的参数账，透传给 `recall_data()`）。
        """
        from starlette.responses import JSONResponse
        q = request.query_params
        query = q.get("query") or ""
        when = q.get("when") or ""
        room = q.get("room") or ""
        tag = q.get("tag") or ""
        floor = None
        try:
            if (q.get("floor") or "").strip():
                floor = max(0.0, min(100.0, float(q.get("floor"))))
        except (TypeError, ValueError):
            floor = None
        try:
            return JSONResponse(await build_poke(
                query=query, when=when, room=room, tag=tag, floor=floor))
        except Exception as e:
            logger.warning(f"[loci] poke 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ---------------------------------------------------------
    # 施工7d（2026-08-18）· 降级信号：完整层唯一的死法
    # ⛔ MCP 工具面十个不加不减——这是新写口，走 web 路由，跟 /api/loci/poke、
    # /api/muse/pending、/api/dream/current 同一类：调用方是桥（gateway 另开的
    # 进程），不是她的浏览器页面，**故意不要 cookie/同源鉴权**（那道闸是给页面上
    # 的按钮防跨站用的，桥的服务器到服务器请求本来就没有 Origin 可言）。
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/dream/wake", methods=["POST"])
    async def api_loci_dream_wake(request: Request) -> Response:
        """降级信号：她回来发的第二条消息触发（数消息、判"第二条"是桥的活，
        见 `src/loci-bridge/戳戳送达.js`）。把还活着的「完整」层降为碎片层，
        碎片 30 分钟 / 一句 60 分钟的老生命周期从**这一刻**起算。

        **幂等**：没有活着的完整层就什么都不做，照样 200——gateway 那边状态
        和这边万一不同步，重复调用完全无害（说明书红线：只调一次，幂等兜底，
        这个"兜底"就是靠这儿实现的，不是靠桥自己去重）。body 不读、不校验，
        这个口不需要任何参数。
        """
        from starlette.responses import JSONResponse
        try:
            from core import _dream as _D
            降级了 = _D.唤醒()
        except Exception as e:
            logger.warning(f"[loci] dream/wake 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"降级了": 降级了})

    # ---------------------------------------------------------
    # 取梦：**这单唯一的取梦口**（MCP 工具面一个新工具都不加）
    # ---------------------------------------------------------
    @mcp.custom_route("/api/dream/current", methods=["GET"])
    async def api_dream_current(request: Request) -> Response:
        """当前那个梦。有梦 → 当前层的内容 + 层级；**没梦 → 204**。

        ------------------------------------------------------------
        🔴 三条，都是判据不是实现细节
        ------------------------------------------------------------
        ① **醒来只拿得到碎片，拿不到完整版**（完整版从来没落过盘）。
           一阵之后只剩一句（`层="一句"`），再之后**真的没了**（文件删掉 + 留痕）。
        ② **调一次算一次「回想」**：起算点往后推一点，**但每次推得越来越少**——
           回想能延缓，不能阻止。所以这个 GET **会写盘**（改起算点/回想次数），
           故意的：不想它就别问它，「你不理它，它自己就没了」。
        ③ **想留住只有一条路**：`grow` 成一条 event。返回里那句 `留住的办法`
           就是这个意思——**写下来那一刻它就不是梦了，是记忆。**

        噩梦只落一个字段（`nightmare: true`，v 低 a 高）。**出声那条腿不在这儿**：
        不推送、不震她手机，半夜在 chat 说一句是桥的活（联动单：别做成会推送的 APP）。

        **故意不要 cookie 鉴权**，跟 `/api/muse/pending` 同一个理由：问它的是桥
        （另一个进程，拿不到浏览器会话），而同一个端口上的 `/mcp` 本来就免 token 直连。
        """
        from starlette.responses import JSONResponse
        try:
            from core import _dream as _D
            got = await _D.current_dream()
        except Exception as e:
            logger.warning(f"[loci] dream/current 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)
        if not got:
            # 204：**一夜无梦是正常的**（攒不到线就不织），不是错。
            return Response(status_code=204)
        return JSONResponse(got)

    # ---------------------------------------------------------
    # 点进去看原文（复用 recall 的 id 直查口径，逐字不截）
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/bucket/{bucket_id}", methods=["GET"])
    async def api_loci_bucket(request: Request) -> Response:
        from starlette.responses import JSONResponse
        bucket_id = str(request.path_params.get("bucket_id") or "").strip()
        if not bucket_id:
            return JSONResponse({"error": "missing id"}, status_code=400)
        try:
            from tools.recall.core import _room_cn, _short_id
            from core._rooms import normalize_room, is_event_room
            b = await sh.bucket_mgr.get_including_archive(bucket_id)
            if not b:
                return JSONResponse(
                    {"error": f"查无此桶：{bucket_id}（可能已物理删除或打错，不做语义联想）"},
                    status_code=404)
            meta = b.get("metadata", {}) or {}
            room = normalize_room(meta.get("room")) or str(meta.get("room") or "")
            archived = (str(meta.get("type") or "") == "archived"
                        or bool(meta.get("tombstone")) or bool(meta.get("deleted_at")))
            return JSONResponse({
                "id": bucket_id,
                "short": _short_id(bucket_id),
                "name": str(meta.get("name") or ""),
                "summary": str(meta.get("summary") or ""),
                "content": str(b.get("content") or ""),   # 逐字，不截
                "room": room,
                "room_cn": _room_cn(meta.get("room")),
                "when": str(meta.get("when") or ""),
                "created": str(meta.get("created") or ""),
                "last_active": str(meta.get("last_active") or ""),
                "valence": meta.get("valence"),
                "arousal": meta.get("arousal"),
                "status": str(meta.get("status") or ""),
                "pinned": bool(meta.get("pinned")),
                "tags": [str(t) for t in (meta.get("tags") or [])],
                # 二改 B 件：主体（第三类标签），跟 tags/aliases 并列、互不混
                "subjects": [str(s) for s in (meta.get("subjects") or [])],
                # 二改 E 件：from 是新名字（读兼容老 triggered_by）
                "from": _split_ids(read_from(meta)),
                "supersedes": _split_ids(meta.get("supersedes")),
                "superseded_by": str(meta.get("superseded_by") or ""),
                "archived": archived,
                # meaning 写入口已退役，但**盘上的老数据照样显示**——
                # 那里面躺着真话（d52a38 实证），她要亲眼看完才定去处。
                "meaning": meta.get("meaning") or [],
                "why_remembered": str(meta.get("why_remembered") or ""),
                # 施工 6 · C 件（§8.2）：她只能改 event，不给 mind 开口子；
                # 归档桶也不给改（改错要走 restore 先捞回来，跟 regrow 同一个规矩）。
                "can_edit": bool(is_event_room(room)) and not archived,
            })
        except Exception as e:
            logger.warning(f"[loci] bucket 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)
