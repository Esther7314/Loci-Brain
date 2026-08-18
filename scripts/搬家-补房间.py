# -*- coding: utf-8 -*-
"""
搬家-补房间.py — 从旧版（Ombre-Brain 那一代）搬过来之后，给没有房间的记忆补一间。

========================================
先说清楚这个脚本**不做**什么
========================================
· 不调模型、不花钱、不联网
· 不动正文、不动 tags、不动 domain、不动任何已有字段
· 已经有 room 的桶**跳过**（幂等，跑几遍都一样）
· 默认是**干跑**：只数给你看，一个字节都不写。要真写得加 --apply

为什么只补 room 这一件事：
    读的路径（recall / 睁眼 / 面板）全都走 core/_rooms.normalize_room()，
    它认得旧十间的名字、会自动翻成新四间 —— **所以老库拿过来本来就读得了**。
    唯一读不了的是**压根没有 room 字段**的桶：它们不属于任何一间，
    在面板和 recall 的房间门里翻不出来。这个脚本就补这一样。

为什么不按内容猜是哪一间：
    「这条是事件还是认知」是语义判断，规则猜不准。而猜错的代价是
    **把一条认知记成一件事**，读的时候不会报错，只会一直不对劲。
    所以这儿不猜：全部落到一间（默认 EVENT/SELF —— 从自己的对话历史里
    搬过来的东西，绝大多数就是「我在场的事」），要改用 --room 指定。
    分得更细是后面拿 regrow 一条条改的事，不该由一个脚本替人决定。

用法：
    python scripts/搬家-补房间.py --buckets /path/to/buckets              # 干跑
    python scripts/搬家-补房间.py --buckets /path/to/buckets --apply      # 真写
    python scripts/搬家-补房间.py --buckets ... --room MIND/VIEWS --apply
========================================
"""
import argparse
import io
import os
import re
import sys
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOMS = ("EVENT/SELF", "EVENT/WORLD", "MIND/TRAITS", "MIND/VIEWS")
# 旧十间 → 新四间。跟 core/_rooms.LEGACY_ROOMS 同一套映射；
# 这个脚本要能脱离整套代码单跑（别人可能只想跑一下看看），所以在这儿抄一份。
LEGACY = {
    "I/EVENT/SELF": "EVENT/SELF", "I/EVENT/WHO": "EVENT/SELF",
    "I/EVENT/WHAT": "EVENT/SELF", "YOU/EVENT/WHO": "EVENT/WORLD",
    "YOU/EVENT/WHAT": "EVENT/WORLD", "I/MIND/WHO": "MIND/TRAITS",
    "I/MIND/WHAT": "MIND/VIEWS", "YOU/MIND/WHO": "MIND/TRAITS",
    "YOU/MIND/WHAT": "MIND/VIEWS", "I/MIND/SELF": "MIND/TRAITS",
}
SCAN = ("dynamic", "permanent", "feel", "plans", "archive")
_ROOM_LINE = re.compile(r"^room:\s*(.*)$", re.M)


def 扫(buckets: str):
    for sub in SCAN:
        d = os.path.join(buckets, sub)
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if fn.endswith(".md"):
                    yield os.path.join(root, fn)


def 补一条(path: str, room: str, apply: bool) -> str:
    """返回这条的处理结果：have / legacy / fill / skip。"""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    if not text.startswith("---"):
        return "skip"                     # 不是标准 frontmatter，不碰
    end = text.find("\n---", 3)
    if end < 0:
        return "skip"
    head = text[:end]
    m = _ROOM_LINE.search(head)
    if m:
        cur = m.group(1).strip().strip('"').strip("'")
        if cur in ROOMS:
            return "have"                 # 已经是新名字
        if cur in LEGACY:
            return "legacy"               # 旧名字：读的时候自动翻，不用改盘
        # 认不出来的名字：也不动它，只报出来（宁可让人看见，也别悄悄改）
        return "skip"
    if not apply:
        return "fill"
    # 在 frontmatter 关闭的那行 --- 之前插一行。**只插这一行**，
    # 不重新序列化整个 yaml —— 那会把人家自己写过的东西重排。
    new = text[:end] + "\nroom: " + room + text[end:]
    data = new.encode("utf-8")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)                 # 先编码再落盘，原子换
    return "fill"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--buckets", required=True, help="记忆目录（里面有 dynamic/ permanent/ 那几个）")
    ap.add_argument("--room", default="EVENT/SELF", choices=ROOMS, help="没有房间的补哪一间")
    ap.add_argument("--apply", action="store_true", help="真写。不加就是干跑")
    a = ap.parse_args()
    if not os.path.isdir(a.buckets):
        print("这个目录不存在：" + a.buckets)
        sys.exit(1)

    c = Counter()
    for p in 扫(a.buckets):
        c[补一条(p, a.room, a.apply)] += 1

    print("=" * 46)
    print("干跑（一个字节都没写）" if not a.apply else "已经写进去了")
    print("=" * 46)
    print("  已经是新房间的      %d 条" % c["have"])
    print("  旧十间的名字        %d 条   ← 读的时候自动翻成新四间，不用改盘" % c["legacy"])
    print("  没有房间的          %d 条   ← %s" % (
        c["fill"], ("会补成 " + a.room) if not a.apply else ("补成了 " + a.room)))
    print("  没动的              %d 条   ← 不是标准格式，或者房间名认不出来" % c["skip"])
    if not a.apply and c["fill"]:
        print()
        print("要真写：同样的命令加 --apply。**建议先把 buckets 目录整个拷一份**。")


if __name__ == "__main__":
    main()
