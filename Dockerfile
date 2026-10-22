# Loci Brain —— 一个把记忆当记忆、不当数据库的记忆系统
#
# 一个容器里跑两样：Loci 和它的网关，由 package/launcher.py 一起起、一起守
# （跟 Windows 包是同一个启动器）。代码烤在镜像里，/app/buckets 放记忆，
# /app/data 放钥匙和网关自己的账。升级 = 重新 build + 重启，不碰数据。

# The gateway needs only node (no packages); take the binary from the official image.
FROM node:22.23.3-bookworm-slim AS node

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOCI_APP_ROOT=/app \
    LOCI_PACKAGE_ROOT=/app \
    LOCI_DATA_DIR=/app/data \
    LOCI_VAULT_DIR=/app/buckets \
    LOCI_CONFIG_PATH=/app/buckets/config.yaml \
    LOCI_TRANSPORT=streamable-http \
    LOCI_LAUNCHER_BIND=0.0.0.0 \
    LOCI_PORT=18002 \
    LOCI_GATEWAY_PORT=3100

WORKDIR /app

# libstdc++6: node's own runtime library, not in the slim image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node

COPY requirements.txt requirements.lock.txt ./
# LOCI_PIP_INDEX: a PyPI mirror for servers where pypi.org is slow (set in .env, passed
# by docker-compose.yml). The lock file pins hashes, so a mirror serving altered files fails.
ARG LOCI_PIP_INDEX=""
RUN if [ -n "$LOCI_PIP_INDEX" ]; then \
        pip install --no-cache-dir -i "$LOCI_PIP_INDEX" -r requirements.lock.txt; \
    else \
        pip install --no-cache-dir -r requirements.lock.txt; \
    fi

COPY src/ ./src/
COPY frontend/ ./frontend/
COPY gateway/ ./gateway/
COPY package/launcher.py ./package/launcher.py
COPY config.default.yaml VERSION entrypoint.sh ./
COPY scripts/ ./scripts/
RUN chmod +x entrypoint.sh && rm -rf gateway/tests

VOLUME ["/app/buckets", "/app/data"]
EXPOSE 18002 3100

HEALTHCHECK --interval=60s --timeout=15s --start-period=60s --retries=5 \
    CMD python -c "import http.client,sys; c=http.client.HTTPConnection('127.0.0.1',18002,timeout=10); c.request('GET','/mcp'); sys.exit(0 if c.getresponse().status else 1)" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
