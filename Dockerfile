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
# - knowledge/curated 作为只读资源打入镜像；manifests/ 运行时重建或挂卷。
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

# 复制应用源码、精编知识库、迁移脚本（migrations 仅作归档，compose 另挂到 postgres）
COPY src ./src
COPY knowledge/curated ./knowledge/curated
COPY migrations ./migrations
COPY README.md ./

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
CMD ["uvicorn", "lvyan.api.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
