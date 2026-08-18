# Loci Brain —— 一个把记忆当记忆、不当数据库的记忆系统
#
# 代码烤在镜像里，/app/buckets 只放你的记忆。
# 升级 = docker pull + 重启，不碰数据。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOCI_APP_ROOT=/app \
    LOCI_CONFIG_PATH=/app/buckets/config.yaml \
    LOCI_TRANSPORT=streamable-http \
    LOCI_PORT=8000

# 🔴 **容器本身必须留在 UTC，别在这儿设 TZ。**
#    盘上的 `created` 存的是**容器系统本地时间**，读的时候按 UTC 解、再折算到
#    `LOCI_TZ` 显示。容器一旦不是 UTC，新记忆会被戳上一个「未来」的时间戳，
#    然后从 recall 的时间视图里整个消失（搜得到、翻不到）。
#    2026-08-18 我给镜像设过 TZ=Asia/Shanghai，当场把新条目全弄没了，是烟测抓住的。
#    要改显示时区用 LOCI_TZ（默认 Asia/Shanghai），不是这儿。

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# 依赖单独一层：只改代码时这一层命中缓存，build 是秒级的
COPY requirements.txt requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

COPY src/ ./src/
COPY frontend/ ./frontend/
COPY config.default.yaml VERSION entrypoint.sh ./
RUN chmod +x entrypoint.sh

VOLUME ["/app/buckets"]
EXPOSE 8000

# 健康检查：只问「有没有人在听」。
# ⚠️ 别用 `curl -f /health` —— 这个服务没有 /health 路由（上游的应用自保活一直在 ping
#    一个 404，2026-08-18 才发现）。而且 MCP 的 /mcp 对裸 GET 本来就回 400：
#    **4xx 是「有人在听」，不是「死了」**，所以这里用 http.client，它不对状态码抛异常。
HEALTHCHECK --interval=60s --timeout=15s --start-period=60s --retries=5 \
    CMD python -c "import http.client,sys; c=http.client.HTTPConnection('127.0.0.1',8000,timeout=10); c.request('GET','/mcp'); sys.exit(0 if c.getresponse().status else 1)" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
