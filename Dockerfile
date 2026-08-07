# syntax=docker/dockerfile:1.7
# 律言法律智能体 Agent Runtime 镜像
#
# 设计要点：
# - 多阶段构建：builder 阶段把依赖装进 venv，runtime 阶段只复制 venv + 源码，
#   最终镜像不含 .git / tests / 构建工具，体积可控。
# - 基于 python:3.11-slim：psycopg[binary] 自带预编译 wheel，markitdown 全家桶
#   均为纯 Python，无需额外系统库。
# - 非 root 用户运行（lvyan:1000），限制容器内提权。
# - 健康检查走 /livez（不查依赖，仅探测进程存活），与 K8s livenessProbe 语义一致。
# - knowledge/curated 作为只读资源打入镜像；manifests/ 在 builder 阶段预热
#   （article_index_v2.{json,pkl} + bm25_index.{json,pkl}），首个请求无需冷启动。
# - migrations/*.sql 由 docker-compose 挂载到 postgres 的
#   /docker-entrypoint-initdb.d/ 首次启动自动执行；应用层 _ensure_schema 也可兜底。

# ---------------------------------------------------------------------------
# Stage 1: builder
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

# 禁用字节码缓存（减小构建产物体积），pip 缓存走 BuildKit cache mount
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# 先仅复制依赖描述，最大化利用层缓存
COPY pyproject.toml README.md ./
COPY src ./src

# 创建 venv 并安装项目（含 documents extras，支持 Office/PDF 转 Markdown）
# --no-cache-dir 减小 venv 体积；cache mount 仅加速构建，不进最终镜像
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip && \
    /opt/venv/bin/pip install ".[documents]"

# P0-2：构建期预热法律检索索引，避免首个用户请求触发 10-30s 冷启动。
# 复制官方法律库 submodule 内容（构建前需 git submodule update --init --recursive）。
# submodule 未检出时目录为空，prewarm 会生成空索引——不影响构建，运行时降级到精编库。
COPY external/lvyan-lawtext/content ./external/lvyan-lawtext/content

# AGENT_DIR 指向 /build，使 config.py 正确解析 lawtext 与 manifests 路径
# （包已装进 venv，__file__ 推导出的路径是 site-packages，必须显式覆盖）
ENV AGENT_DIR=/build

# 预热：扫描法条 → 切分 85639 chunks → 生成 article_index_v2.{json,pkl} +
# bm25_index.{json,pkl}。产物在 /build/knowledge/manifests/，runtime 阶段 COPY。
# 若法律库为空（submodule 未检出），命令仍成功退出（生成空索引），不阻断构建。
RUN /opt/venv/bin/python -m lvyan.scripts.ingest_laws --prewarm \
        --output /build/knowledge/manifests/article_index_v2.json \
    && ls -lh /build/knowledge/manifests/

# ---------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    # 默认生产模式：禁止任何静默降级（validate_runtime_config 会校验）
    RUNTIME_MODE=production \
    PERSISTENCE_REQUIRED=true \
    CHECKPOINTER_BACKEND=postgres \
    # 应用容器内数据目录（与 compose 卷挂载点一致）
    AGENT_DIR=/app

WORKDIR /app

# 从 builder 复制 venv
COPY --from=builder /opt/venv /opt/venv

# 复制应用源码、精编知识库、文书模板、迁移脚本（migrations 仅作归档，compose 另挂到 postgres）
COPY src ./src
COPY knowledge/curated ./knowledge/curated
COPY templates ./templates
COPY migrations ./migrations
COPY README.md ./

# 官方法律全文库（git submodule，采集自 flk.npc.gov.cn）
# 前提：构建前本地执行 `git submodule update --init --recursive` 检出数据。
# 若 submodule 未检出，此 COPY 不产生内容，应用降级到精编知识库（不影响启动）。
# 也可通过运行时挂卷 + LAWTEXT_DIR 环境变量覆盖（见 .env.example）。
COPY external/lvyan-lawtext/content ./external/lvyan-lawtext/content

# P0-2：从 builder 复制构建期预热的检索索引（article_index_v2.{json,pkl} +
# bm25_index.{json,pkl}），运行时直接载入，首个请求无需冷启动构建。
# Docker 命名卷行为：compose 用 lvyan-app-manifests 卷挂载到该路径时，首次启动
# 会把镜像内此目录的内容 **复制进** 空卷（而非覆盖为空），因此预热索引可被保留。
COPY --from=builder /build/knowledge/manifests ./knowledge/manifests

# 创建运行时目录并切换非 root 用户
RUN groupadd -r -g 1000 lvyan && \
    useradd -r -u 1000 -g 1000 -d /app -s /sbin/nologin lvyan && \
    mkdir -p /app/data/uploads /app/outputs /app/knowledge/manifests && \
    chown -R lvyan:lvyan /app

USER lvyan

EXPOSE 8000

# 健康检查：/livez 仅探测进程存活，不查依赖（与 readyz 区分）
# start-period 40s 给首次启动的 PostgresSaver.setup() / 异步图初始化留时间
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=3).status==200 else 1)" || exit 1

# 直接 uvicorn factory 模式启动；host 0.0.0.0 才能被容器外访问
# factory 模式确保 create_app() 在事件循环内执行，AsyncPostgresSaver 绑定到正确循环
# W6 修复：--forwarded-allow-ips 默认只信任容器内部网段（172.16/12 + 127.0.0.1），
# 避免直接对外暴露时被伪造 X-Forwarded-For。部署在可信反向代理后方时，
# 可通过 FORWARDED_ALLOW_IPS 环境变量覆盖（如 "*" 或具体代理 IP）。
# 使用 shell 形式 CMD 以读取环境变量。
CMD sh -c 'uvicorn lvyan.api.server:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1,172.16.0.0/12,10.0.0.0/8,192.168.0.0/16}"'
