"""统一配置模块。

律言 Agent Runtime 的所有运行时配置集中在此，避免分散读取环境变量。
路径解析策略遵循「环境变量优先」约定：

1. 显式环境变量（``LAWTEXT_DIR`` / ``KNOWLEDGE_DIR`` 等）覆盖一切
2. AGENT 工程内默认路径（官方法律全文库走 git submodule）

本模块刻意不引入 ``pydantic-settings``，仅依赖 ``pydantic.BaseModel`` + ``os.getenv``，
以保持 ``pyproject.toml`` 依赖清单的最小化。
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# .env 文件加载（轻量实现，不引入 python-dotenv）
# ---------------------------------------------------------------------------
def _load_dotenv_from(env_path: Path) -> None:
    """从指定 .env 文件加载环境变量（不覆盖已有值）。

    支持格式::

        KEY=VALUE
        KEY="VALUE"         # 双引号包裹
        KEY='VALUE'         # 单引号包裹
        KEY=VALUE  # 行内注释（未加引号时）
        export KEY=VALUE    # 兼容 shell export 前缀

    忽略注释行（# 开头）和空行。值两侧的引号会被去除。
    引号内的 ``#`` 不视为注释。
    """
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        # 兼容 export 前缀
        if line.startswith("export "):
            line = line[7:].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # 去除值两侧引号
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            # 引号内内容原样保留（# 不视为注释）
            value = value[1:-1]
        else:
            # 未加引号时去除行内注释（# 后面的内容）
            comment_idx = value.find(" #")
            if comment_idx >= 0:
                value = value[:comment_idx].rstrip()
        # 不覆盖已有环境变量
        if key and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# 路径常量（模块级，便于直接 import）
# ---------------------------------------------------------------------------
# config.py 位于 AGENT/src/lvyan/config.py
_PKG_DIR = Path(__file__).resolve().parent  # AGENT/src/lvyan
_SRC_DIR = _PKG_DIR.parent  # AGENT/src
_DEFAULT_AGENT_DIR = _SRC_DIR.parent  # 本地开发默认 AGENT 根目录

# .env 引导加载：必须在**任何**模块级路径常量（AGENT_DIR/KNOWLEDGE_DIR/
# LAWTEXT_DIR 等）求值之前执行，否则写在 .env 里的路径配置会被静默忽略、
# 而同文件里的 DATABASE_URL 却在 _build_settings() 阶段生效——一半生效
# 一半不生效，容器部署时极难排查。
# AGENT_DIR 已由真实环境变量提供时，加载该目录下的 .env；
# 未提供时尝试加载 __file__ 推导的默认目录下的 .env（.env 中定义的
# AGENT_DIR 随之生效）。
_bootstrap_dir = (
    Path(os.getenv("AGENT_DIR", "")).resolve() if os.getenv("AGENT_DIR") else _DEFAULT_AGENT_DIR
)
_load_dotenv_from(_bootstrap_dir / ".env")

# 环境变量优先：容器部署时包安装到 site-packages，__file__ 推导的路径无效，
# 必须通过 AGENT_DIR 环境变量显式指定工作根目录（如 /app）。
# 本地开发不设此变量时走 __file__ 推导，行为不变。
_agent_dir_env = os.getenv("AGENT_DIR")
AGENT_DIR = Path(_agent_dir_env).resolve() if _agent_dir_env else _SRC_DIR.parent  # AGENT/


def _load_dotenv() -> None:
    """从 AGENT/.env 加载环境变量（幂等；不覆盖已有值）。

    模块导入时已完成引导加载（见上方 _bootstrap_dir 逻辑），此处保留
    供 _build_settings() 兜底：引导阶段 AGENT_DIR 尚未确定、或调用方在
    import 之后才设置 AGENT_DIR 的场景。
    """
    _load_dotenv_from(AGENT_DIR / ".env")


def _resolve_knowledge_dir() -> Path:
    """精编知识库目录：环境变量 KNOWLEDGE_DIR > AGENT/knowledge/curated。"""
    env = os.getenv("KNOWLEDGE_DIR")
    if env:
        return Path(env)
    return AGENT_DIR / "knowledge" / "curated"


def _resolve_lawtext_dir() -> Path:
    """官方法律全文库目录解析（优先级：环境变量 > submodule）。

    解析顺序：
      1. ``LAWTEXT_DIR`` 环境变量（显式覆盖一切）
      2. ``AGENT/external/lvyan-lawtext/content``（git submodule，默认）

    submodule 未检出时返回默认路径，由 ``is_official_db_available()`` 返回 False
    触发降级到精编知识库。
    """
    env = os.getenv("LAWTEXT_DIR")
    if env:
        return Path(env)
    return AGENT_DIR / "external" / "lvyan-lawtext" / "content"


KNOWLEDGE_DIR: Path = _resolve_knowledge_dir()
LAWTEXT_DIR: Path = _resolve_lawtext_dir()


class Settings(BaseModel):
    """律言 Agent Runtime 统一配置。

    所有字段均从环境变量读取并带有合理默认值，保证本地开发开箱即用。
    生产部署时通过环境变量或 .env 覆盖。
    """

    # --- 数据库（PostgreSQL，LangGraph checkpoint + 法规元数据） ---
    database_url: str = Field(
        default="postgresql+psycopg://lvyan:lvyan@localhost:5432/lvyan",
        description="SQLAlchemy 数据库连接串",
    )

    # --- OpenSearch（条文级检索索引） ---
    opensearch_url: str = Field(default="https://localhost:9200")
    opensearch_user: str = Field(default="admin")
    opensearch_password: str = Field(default="admin")

    # --- MinIO / 对象存储（案件加密空间、文书附件） ---
    object_storage_endpoint: str = Field(default="localhost:9000")

    # --- 模型网关（统一 LLM/Embedding/Reranker 入口） ---
    model_gateway_url: str = Field(default="", description="为空时由 ModelGateway 自行决定降级策略")
    model_gateway_api_key: str = Field(
        default="", description="模型网关 API Key，用于 Authorization: Bearer 头"
    )

    # --- M3：API 认证（X-User-ID 网关模式 vs 进程内 JWT 验签） ---
    auth_enabled: bool = Field(
        default=False,
        description="是否启用认证；false 时所有 user_id 都返回 anonymous（单租户本地开发）",
    )
    jwt_verify_in_process: bool = Field(
        default=False,
        description=(
            "是否在本进程内验证 Bearer JWT 的签名 / exp / nbf / iss / aud。"
            "false 时禁止信任 Bearer JWT（必须由可信网关注入 X-User-ID）。"
        ),
    )
    jwt_issuer: str = Field(
        default="",
        description="JWT 预期 iss；jwt_verify_in_process=true 时必填",
    )
    jwt_audience: str = Field(
        default="",
        description="JWT 预期 aud；jwt_verify_in_process=true 时必填",
    )
    jwt_jwks_url: str = Field(
        default="",
        description="JWKS endpoint，用于获取 RS256 等非对称签名公钥；jwt_verify_in_process=true 时必填",
    )
    jwt_algorithms: str = Field(
        default="RS256",
        description="允许的签名算法，逗号分隔；默认 RS256。禁止允许 none。",
    )

    # --- 模型选择 ---
    chat_model: str = Field(default="Qwen/Qwen2.5-7B-Instruct", description="对话/推理模型名称")
    embedding_model: str = Field(default="BAAI/bge-m3", description="Embedding 模型名称")
    reranker_model: str = Field(default="BAAI/bge-reranker-v2-m3", description="Reranker 模型名称")
    vision_model: str = Field(
        default="Qwen/Qwen3-VL-8B-Instruct",
        description="视觉模型名称（图片理解），走视觉网关 chat/completions",
    )
    vision_gateway_url: str = Field(
        default="",
        description="视觉模型网关根 URL；为空时回退 model_gateway_url",
    )
    vision_api_key: str = Field(
        default="",
        description="视觉模型 API Key；为空时回退 model_gateway_api_key",
    )
    vision_api_path: str = Field(
        default="/v1/chat/completions",
        description="视觉 chat completions 路径（智谱为 /chat/completions）",
    )

    # --- Langfuse（可观测性） ---
    langfuse_host: str = Field(default="")
    langfuse_public_key: str = Field(default="")
    langfuse_secret_key: str = Field(default="")

    # --- 路径解析（运行时也可读取模块级 KNOWLEDGE_DIR / LAWTEXT_DIR） ---
    knowledge_dir: Path = Field(default_factory=lambda: KNOWLEDGE_DIR)
    lawtext_dir: Path = Field(default_factory=lambda: LAWTEXT_DIR)

    # --- 运行时策略守卫 ---
    max_retrieval_iterations: int = Field(
        default=1, description="Citation Verifier 最大重检索次数（降低以减少 LLM 调用放大）"
    )
    max_cost_budget_usd: float = Field(default=2.0, description="单次 run 最大成本预算（美元）")
    hitl_enabled: bool = Field(
        default=True, description="是否启用 Human-in-the-loop 不可逆操作审批"
    )

    # --- P0-1：部署模式与持久化强制 ---
    # production：禁止任何静默降级；PostgresSaver / metadata store 初始化失败
    # 必须让服务启动失败。development（默认）：允许回退 MemorySaver / None。
    runtime_mode: str = Field(
        default="development",
        description="部署模式：development（允许内存降级）/ production（禁止降级）",
    )
    # 即使在 development 也允许显式强制持久化（CI / 预发环境用）
    persistence_required: bool = Field(
        default=False,
        description="为 true 时，PostgresSaver 与 metadata store 任一初始化失败都抛异常",
    )
    checkpointer_backend: str = Field(
        default="auto",
        description="期望的 checkpointer 后端：postgres / memory / auto",
    )

    # --- P1-2：跨实例取消 ---
    cancel_poll_interval_seconds: float = Field(
        default=5.0,
        description="运行中 worker 检查 PostgreSQL cancel_requested_at 的间隔",
    )

    # --- P1-4：上传与上下文资源限制 ---
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, description="单文件上传字节上限")
    max_extracted_chars_per_file: int = Field(
        default=200_000, description="单附件转换后 Markdown 字符上限"
    )
    max_total_attachment_chars: int = Field(
        default=400_000, description="单次 run 所有附件拼接后的总字符上限"
    )
    max_attachment_count: int = Field(default=10, description="单次 run 附件数量上限")
    max_concurrent_conversions: int = Field(default=2, description="文档转换并发数上限（信号量）")
    document_conversion_timeout_seconds: float = Field(
        default=60.0, description="单次文档转换超时秒数"
    )
    zip_uncompressed_bytes_limit: int = Field(
        default=100 * 1024 * 1024,
        description="Office(ZIP) 文件解压后总字节上限，防止 ZIP bomb",
    )

    # --- Legal Reasoner 迭代守卫 ---
    max_legal_reasoner_iterations: int = Field(
        default=1,
        description="Critic 不通过时回退 legal_reasoner 的最大重试次数（降低以减少 LLM 调用放大）",
    )

    # --- P1: 案件加密空间安全配置 ---
    case_vault_allow_insecure: bool = Field(
        default=False,
        description="开发环境是否允许 base64 降级（CASE_VAULT_KEY 未配置时）",
    )

    # --- P2: 多租户与 Redis ---
    redis_url: str = Field(
        default="",
        description="Redis 连接地址（生产环境限流必需）",
    )
    rate_limit_backend: str = Field(
        default="memory",
        description="限流后端：memory（单实例）/ redis（多实例生产）",
    )
    rls_enforced: bool = Field(
        default=False,
        description="RLS 策略是否强制（生产必须为 true）",
    )

    # --- P3: 优雅停机与并发控制 ---
    shutdown_grace_seconds: int = Field(
        default=30,
        description="SIGTERM 收到后等待运行中任务完成的最大秒数",
    )
    max_llm_concurrency: int = Field(
        default=10,
        description="LLM 并发请求数上限（信号量）",
    )
    max_retrieval_concurrency: int = Field(
        default=20,
        description="检索并发请求数上限（信号量）",
    )

    # --- P4: 可观测性 ---
    log_format: str = Field(
        default="text",
        description="日志格式：text（开发彩色）/ json（生产结构化）",
    )
    metrics_enabled: bool = Field(
        default=False,
        description="是否启用 Prometheus /metrics 端点",
    )
    metrics_auth_token: str = Field(
        default="",
        description="指标端点 Bearer 令牌（生产环境保护）",
    )


def _build_settings() -> Settings:
    """从环境变量构造 Settings 单例。

    环境变量名与字段名大写对应，例如 ``DATABASE_URL`` / ``OPENSEARCH_URL``。
    布尔字段接受 ``true/false/1/0``（大小写不敏感）。
    """
    # 先加载 .env 文件（不覆盖已有环境变量）
    _load_dotenv()

    def _get(name: str, default: str) -> str:
        return os.getenv(name, default)

    def _get_bool(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    def _get_int(name: str, default: int) -> int:
        raw = os.getenv(name)
        if raw is None or raw.strip() == "":
            return default
        return int(raw)

    def _get_float(name: str, default: float) -> float:
        raw = os.getenv(name)
        if raw is None or raw.strip() == "":
            return default
        return float(raw)

    return Settings(
        database_url=_get("DATABASE_URL", "postgresql+psycopg://lvyan:lvyan@localhost:5432/lvyan"),
        opensearch_url=_get("OPENSEARCH_URL", "https://localhost:9200"),
        opensearch_user=_get("OPENSEARCH_USER", "admin"),
        opensearch_password=_get("OPENSEARCH_PASSWORD", "admin"),
        object_storage_endpoint=_get("OBJECT_STORAGE_ENDPOINT", "localhost:9000"),
        model_gateway_url=_get("MODEL_GATEWAY_URL", ""),
        model_gateway_api_key=_get("MODEL_GATEWAY_API_KEY", ""),
        auth_enabled=_get_bool("AUTH_ENABLED", False),
        jwt_verify_in_process=_get_bool("JWT_VERIFY_IN_PROCESS", False),
        jwt_issuer=_get("JWT_ISSUER", ""),
        jwt_audience=_get("JWT_AUDIENCE", ""),
        jwt_jwks_url=_get("JWT_JWKS_URL", ""),
        jwt_algorithms=_get("JWT_ALGORITHMS", "RS256"),
        chat_model=_get("CHAT_MODEL", "Qwen/Qwen2.5-7B-Instruct"),
        embedding_model=_get("EMBEDDING_MODEL", "BAAI/bge-m3"),
        reranker_model=_get("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3"),
        vision_model=_get("VISION_MODEL", "Qwen/Qwen3-VL-8B-Instruct"),
        vision_gateway_url=_get("VISION_GATEWAY_URL", ""),
        vision_api_key=_get("VISION_API_KEY", ""),
        vision_api_path=_get("VISION_API_PATH", "/v1/chat/completions"),
        langfuse_host=_get("LANGFUSE_HOST", ""),
        langfuse_public_key=_get("LANGFUSE_PUBLIC_KEY", ""),
        langfuse_secret_key=_get("LANGFUSE_SECRET_KEY", ""),
        knowledge_dir=KNOWLEDGE_DIR,
        lawtext_dir=LAWTEXT_DIR,
        max_retrieval_iterations=_get_int("MAX_RETRIEVAL_ITERATIONS", 1),
        max_cost_budget_usd=_get_float("MAX_COST_BUDGET_USD", 2.0),
        hitl_enabled=_get_bool("HITL_ENABLED", True),
        runtime_mode=_get("RUNTIME_MODE", "development").strip().lower(),
        persistence_required=_get_bool("PERSISTENCE_REQUIRED", False),
        checkpointer_backend=_get("CHECKPOINTER_BACKEND", "auto").strip().lower(),
        cancel_poll_interval_seconds=_get_float("CANCEL_POLL_INTERVAL_SECONDS", 5.0),
        max_upload_bytes=_get_int("MAX_UPLOAD_BYTES", 10 * 1024 * 1024),
        max_extracted_chars_per_file=_get_int("MAX_EXTRACTED_CHARS_PER_FILE", 200_000),
        max_total_attachment_chars=_get_int("MAX_TOTAL_ATTACHMENT_CHARS", 400_000),
        max_attachment_count=_get_int("MAX_ATTACHMENT_COUNT", 10),
        max_concurrent_conversions=_get_int("MAX_CONCURRENT_CONVERSIONS", 2),
        document_conversion_timeout_seconds=_get_float("DOCUMENT_CONVERSION_TIMEOUT_SECONDS", 60.0),
        zip_uncompressed_bytes_limit=_get_int("ZIP_UNCOMPRESSED_BYTES_LIMIT", 100 * 1024 * 1024),
        max_legal_reasoner_iterations=_get_int("MAX_LEGAL_REASONER_ITERATIONS", 1),
        case_vault_allow_insecure=_get_bool("CASE_VAULT_ALLOW_INSECURE", False),
        redis_url=_get("REDIS_URL", ""),
        rate_limit_backend=_get("RATE_LIMIT_BACKEND", "memory").strip().lower(),
        rls_enforced=_get_bool("RLS_ENFORCED", False),
        shutdown_grace_seconds=_get_int("SHUTDOWN_GRACE_SECONDS", 30),
        max_llm_concurrency=_get_int("MAX_LLM_CONCURRENCY", 10),
        max_retrieval_concurrency=_get_int("MAX_RETRIEVAL_CONCURRENCY", 20),
        log_format=_get("LOG_FORMAT", "text").strip().lower(),
        metrics_enabled=_get_bool("METRICS_ENABLED", False),
        metrics_auth_token=_get("METRICS_AUTH_TOKEN", ""),
    )


# 全局单例：整个 Runtime 共享一份配置
settings: Settings = _build_settings()


def is_rls_enforced() -> bool:
    """返回当前 RLS 开关；环境覆盖只在此配置边界统一解析。"""
    raw = os.getenv("RLS_ENFORCED")
    if raw is None:
        return settings.rls_enforced
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def is_official_db_available() -> bool:
    """官方法律全文库是否可用（目录存在且非空）。

    检索脚本据此决定是否启用官方库，或降级为仅精编知识库 + AI 补充模式。
    """
    return LAWTEXT_DIR.is_dir() and any(LAWTEXT_DIR.iterdir())


def is_official_law_db_required() -> bool:
    """P0-4：是否强制要求完整官方法律库可用。

    生产环境（``RUNTIME_MODE=production``）默认要求；可通过
    ``REQUIRE_OFFICIAL_LAW_DB=false`` 显式放宽（如开发 / 预发联调）。
    非生产环境默认不要求，除非显式设 ``REQUIRE_OFFICIAL_LAW_DB=true``。

    ``/readyz`` 据此决定法律库缺失时是否返回 not-ready。
    """
    raw = os.getenv("REQUIRE_OFFICIAL_LAW_DB")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    # 未显式设置时：生产模式默认要求，其余模式默认不要求
    return is_production()


def is_production() -> bool:
    """是否处于生产部署模式（``RUNTIME_MODE=production``）。

    P0-1：生产模式下禁止任何静默降级（MemorySaver / metadata store=None）。
    """
    import os

    raw = os.getenv("RUNTIME_MODE", settings.runtime_mode).strip().lower()
    return raw == "production"


def persistence_required() -> bool:
    """是否强制持久化（生产模式或显式 ``PERSISTENCE_REQUIRED=true``）。"""
    import os

    if is_production():
        return True
    raw = os.getenv("PERSISTENCE_REQUIRED")
    if raw is None:
        return settings.persistence_required
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def durable_runtime_required() -> bool:
    """P1-3：是否需要持久化运行时（checkpointer + metadata store 都必须可用）。

    当以下任一为 true 时返回 True：
    - ``persistence_required()``（生产模式或显式 PERSISTENCE_REQUIRED=true）
    - ``CHECKPOINTER_BACKEND=postgres``（显式要求 PG checkpointer）
    """
    import os

    if persistence_required():
        return True
    backend = os.getenv("CHECKPOINTER_BACKEND", settings.checkpointer_backend).strip().lower()
    return backend == "postgres"


def validate_runtime_config() -> None:
    """P1-2 / P1-4：启动期验证运行时配置，非法值直接启动失败。

    - ``CHECKPOINTER_BACKEND`` 只允许 ``memory`` / ``postgres`` / ``auto``；
    - ``PERSISTENCE_REQUIRED=true`` + ``CHECKPOINTER_BACKEND=memory`` 冲突；
    - 生产模式 + ``AUTH_ENABLED=true`` + ``AUTH_MODE=auto`` 冲突；
    - W13：``JWT_VERIFY_IN_PROCESS=true`` 时校验 JWKS_URL / ISSUER / AUDIENCE
      非空，且 ``JWT_ALGORITHMS`` 不含 ``none``。
    """
    import os

    backend = os.getenv("CHECKPOINTER_BACKEND", settings.checkpointer_backend).strip().lower()
    if backend not in {"memory", "postgres", "auto"}:
        raise RuntimeError(
            f"CHECKPOINTER_BACKEND='{backend}' 非法；允许值: memory / postgres / auto"
        )
    # 视觉网关 URL 不应带 /v1 等版本段：请求路径（VISION_API_PATH，默认
    # /v1/chat/completions）已含版本前缀，网关侧再带会拼出 /v1/v1/...。
    vision_gateway = os.getenv("VISION_GATEWAY_URL", settings.vision_gateway_url).strip()
    if vision_gateway.rstrip("/").endswith("/v1"):
        raise RuntimeError(
            "VISION_GATEWAY_URL 不应包含 /v1 后缀（请求路径 VISION_API_PATH 已带版本"
            "前缀，重复拼接会得到 /v1/v1/...）；请只填网关根地址，"
            "如需自定义完整路径请改 VISION_API_PATH"
        )
    if backend == "memory" and persistence_required():
        raise RuntimeError("PERSISTENCE_REQUIRED=true 时禁止 CHECKPOINTER_BACKEND=memory")
    # P1-4：生产认证配置校验
    if is_production() and is_auth_enabled_env():
        auth_mode = os.getenv("AUTH_MODE", "auto").strip().lower()
        if auth_mode == "auto":
            raise RuntimeError(
                "AUTH_MODE=auto 在生产模式下被禁止；请设置 AUTH_MODE=jwt 或 AUTH_MODE=trusted_proxy"
            )
    # P1: 加密配置校验（生产环境必须有合法密钥）
    from lvyan.memory.case_vault import CaseVault

    CaseVault.validate_encryption_config()

    # P2: 多租户配置校验（生产环境必须强制 RLS + Redis 限流）
    if is_production():
        if not is_rls_enforced():
            raise RuntimeError(
                "生产模式下 RLS_ENFORCED 必须为 true（确保 RLS 策略已部署并强制启用）"
            )
        rl_backend = os.getenv("RATE_LIMIT_BACKEND", "memory").strip().lower()
        if rl_backend != "redis":
            raise RuntimeError("生产模式下 RATE_LIMIT_BACKEND 必须为 redis（多实例限流必需）")
        if not os.getenv("REDIS_URL", "").strip():
            raise RuntimeError("生产模式下 REDIS_URL 必须配置（限流后端依赖）")
        gateway = os.getenv("MODEL_GATEWAY_URL", settings.model_gateway_url).strip()
        allow_local_models = os.getenv("ALLOW_LOCAL_MODELS", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if not gateway and not allow_local_models:
            raise RuntimeError(
                "生产模式必须配置 MODEL_GATEWAY_URL，或显式设置 ALLOW_LOCAL_MODELS=true"
            )
        if os.getenv("ALLOW_HASH_EMBEDDING_FALLBACK", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise RuntimeError("生产模式禁止 ALLOW_HASH_EMBEDDING_FALLBACK=true")
        if os.getenv("ALLOW_HEURISTIC_RERANKER_FALLBACK", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise RuntimeError("生产模式禁止 ALLOW_HEURISTIC_RERANKER_FALLBACK=true")

    # W13：JWT 进程内验签的配置组合校验（启动期暴露配置错误，避免首请求才发现）
    if os.getenv("JWT_VERIFY_IN_PROCESS", "").strip().lower() in {"1", "true", "yes", "on"}:
        jwks_url = os.getenv("JWT_JWKS_URL", "").strip()
        issuer = os.getenv("JWT_ISSUER", "").strip()
        audience = os.getenv("JWT_AUDIENCE", "").strip()
        if not jwks_url:
            raise RuntimeError(
                "JWT_VERIFY_IN_PROCESS=true 时必须配置 JWT_JWKS_URL（JWKS 公钥来源）"
            )
        if not issuer or not audience:
            raise RuntimeError(
                "JWT_VERIFY_IN_PROCESS=true 时必须同时配置 JWT_ISSUER 和 JWT_AUDIENCE"
            )
        algorithms = [
            a.strip().lower() for a in os.getenv("JWT_ALGORITHMS", "RS256").split(",") if a.strip()
        ]
        if not algorithms or any(a == "none" for a in algorithms):
            raise RuntimeError(
                "JWT_ALGORITHMS 配置非法：禁止使用 none，必须使用 RS256/ES256 等非对称算法"
            )


def is_auth_enabled_env() -> bool:
    """从环境变量读取 AUTH_ENABLED（供 validate_runtime_config 使用）。"""
    import os

    raw = os.getenv("AUTH_ENABLED")
    if raw is None:
        return settings.auth_enabled
    return raw.strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "AGENT_DIR",
    "KNOWLEDGE_DIR",
    "LAWTEXT_DIR",
    "Settings",
    "settings",
    "is_official_db_available",
    "is_official_law_db_required",
    "is_production",
    "persistence_required",
    "durable_runtime_required",
    "validate_runtime_config",
    "is_auth_enabled_env",
]
