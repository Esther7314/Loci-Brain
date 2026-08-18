#!/bin/sh
# entrypoint.sh —— Loci Brain 启动入口
#
# 只做三件事，做完就把台交给 python：
#   ① 老变量兼容：把还在用 OMBRE_* 的环境变量映射成 LOCI_*（从 Ombre-Brain 迁过来的人，
#      .env 一个字不用改就能起来）。同名 LOCI_* 已存在时以 LOCI_* 为准。
#   ② 配置就位：确保 $LOCI_CONFIG_PATH 是一个可用的**普通文件**，缺了就从内置模板初始化。
#      它若是目录（宿主文件不存在时 Docker 会替你建一个目录挂进来），当场 FATAL 退出 ——
#      绝不对这个路径 rm -rf，它可能就是整个记忆卷。
#   ③ 起服务。
#
# ⚠️ 这里没有「代码播种」那一套：代码烤在镜像里，/app/buckets 只放记忆。
#    升级 = docker pull + 重启，不会碰你的数据。

set -e

APP_ROOT="${LOCI_APP_ROOT:-/app}"

# --- ① OMBRE_* → LOCI_* 兼容 ---
# 只取变量名（名字里不会有空格），值带空格也不会被拆坏。
for _old_name in $(env | grep '^OMBRE_' | cut -d= -f1 || true); do
    _new_name="LOCI_${_old_name#OMBRE_}"
    eval "_cur=\${$_new_name-}"
    if [ -z "$_cur" ]; then
        eval "export $_new_name=\"\${$_old_name}\""
        echo "[entrypoint] 兼容旧变量: $_old_name → $_new_name"
    fi
done

CONFIG="${LOCI_CONFIG_PATH:-$APP_ROOT/buckets/config.yaml}"
DEFAULT="$APP_ROOT/config.default.yaml"

mkdir -p "$(dirname "$CONFIG")" 2>/dev/null || true

# --- ② 配置就位 ---
if [ -d "$CONFIG" ]; then
    echo "[entrypoint] FATAL: '$CONFIG' 是一个目录。"
    echo "[entrypoint] 多半是 compose 里单文件挂载了一个宿主上不存在的 config —— Docker 会替你建成目录。"
    echo "[entrypoint] 改成把配置放进数据卷里：LOCI_CONFIG_PATH=/app/buckets/config.yaml"
    echo "[entrypoint] （这个路径可能就是你的整个记忆卷，所以我不会去删它。）"
    exit 1
fi

if [ ! -e "$CONFIG" ]; then
    echo "[entrypoint] 首次启动，从内置模板生成配置: $CONFIG"
    cp "$DEFAULT" "$CONFIG"
fi

if [ ! -f "$CONFIG" ]; then
    echo "[entrypoint] FATAL: 无法在 '$CONFIG' 准备出一个可用的配置文件。"
    echo "[entrypoint] 常见原因：该路径所在的文件系统只读（很多 PaaS 只有挂载卷可写）。"
    exit 1
fi

echo "[entrypoint] 配置就位: $CONFIG"

# --- ③ 起服务 ---
cd "$APP_ROOT"
exec python src/server.py
