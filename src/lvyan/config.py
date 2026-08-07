"""统一配置模块。

律言 Agent Runtime 的所有运行时配置集中在此，避免分散读取环境变量。
路径解析策略沿用原 ``律言skill/scripts/project_config.py`` 的「环境变量优先」约定：

1. 显式环境变量（``LAWTEXT_DIR`` / ``KNOWLEDGE_DIR`` 等）覆盖一切
2. AGENT 工程内默认路径
3. ``../律言skill/lawtext_extracted/laws-main/content`` 作为官方法律全文库默认位置

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
def _load_dotenv() -> None:
    """从 AGENT/.env 加载环境变量（不覆盖已有值）。

    支持 ``KEY=VALUE`` 格式，忽略注释行（#开头）和空行。
    值两侧的引号会被去除。
    """
    env_path = AGENT_DIR / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # 去除值两侧引号
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        # 不覆盖已有环境变量
        if key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# 路径常量（模块级，便于直接 import）
# ---------------------------------------------------------------------------
# config.py 位于 AGENT/src/lvyan/config.py
_PKG_DIR = Path(__file__).resolve().parent  # AGENT/src/lvyan
_SRC_DIR = _PKG_DIR.parent  # AGENT/src
# 环境变量优先：容器部署时包安装到 site-packages，__file__ 推导的路径无效，
# 必须通过 AGENT_DIR 环境变量显式指定工作根目录（如 /app）。
# 本地开发不设此变量时走 __file__ 推导，行为不变。
_agent_dir_env = os.getenv("AGENT_DIR")
AGENT_DIR = Path(_agent_dir_env).resolve() if _agent_dir_env else _SRC_DIR.parent  # AGENT/
REPO_ROOT = AGENT_DIR.parent  # 法律/（仓库根）


def _resolve_knowledge_dir() -> Path:
    """精编知识库目录：环境变量 KNOWLEDGE_DIR > AGENT/knowledge/curated。"""
    env = os.getenv("KNOWLEDGE_DIR")
    if env:
        return Path(env)
    return AGENT_DIR / "knowledge" / "curated"


def _resolve_lawtext_dir() -> Path:
    """官方法律全文库目录：环境变量 LAWTEXT_DIR > ../律言skill/lawtext_extracted/laws-main/content。

    沿用原 project_config 的环境变量优先策略，三处都不存在时仍返回默认值，
    由 ``is_official_db_available()`` 返回 False 触发降级。
    """
    env = os.getenv("LAWTEXT_DIR")
    if env:
        return Path(env)
    return REPO_ROOT / "律言skill" / "lawtext_extracted" / "laws-main" / "content"


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
        description="视觉模型名称（图片理解），走模型网关 /v1/chat/completions",
    )

    # --- Langfuse（可观测性） ---
    langfuse_host: str = Field(default="")
    langfuse_public_key: str = Field(default="")
    langfuse_secret_key: str = Field(default="")

    # --- 路径解析（运行时也可读取模块级 KNOWLEDGE_DIR / LAWTEXT_DIR） ---
    knowledge_dir: Path = Field(default_factory=lambda: KNOWLEDGE_DIR)
    lawtext_dir: Path = Field(default_factory=lambda: LAWTEXT_DIR)

    # --- 运行时策略守卫 ---
    max_retrieval_iterations: int = Field(default=1, description="Citation Verifier 最大重检索次数（降低以减少 LLM 调用放大）")
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
    max_concurrent_conversions: int = Field(
        default=2, description="文档转换并发数上限（信号量）"
    )
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
        document_conversion_timeout_seconds=_get_float(
            "DOCUMENT_CONVERSION_TIMEOUT_SECONDS", 60.0
        ),
        zip_uncompressed_bytes_limit=_get_int("ZIP_UNCOMPRESSED_BYTES_LIMIT", 100 * 1024 * 1024),
        max_legal_reasoner_iterations=_get_int("MAX_LEGAL_REASONER_ITERATIONS", 1),
    )


# 全局单例：整个 Runtime 共享一份配置
settings: Settings = _build_settings()


def is_official_db_available() -> bool:
    """官方法律全文库是否可用（目录存在且非空）。

    检索脚本据此决定是否启用官方库，或降级为仅精编知识库 + AI 补充模式。
    """
    return LAWTEXT_DIR.is_dir() and any(LAWTEXT_DIR.iterdir())


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
    - 生产模式 + ``AUTH_ENABLED=true`` + ``AUTH_MODE=auto`` 冲突。
    """
    import os

    backend = os.getenv("CHECKPOINTER_BACKEND", settings.checkpointer_backend).strip().lower()
    if backend not in {"memory", "postgres", "auto"}:
        raise RuntimeError(
            f"CHECKPOINTER_BACKEND='{backend}' 非法；"
            f"允许值: memory / postgres / auto"
        )
    if backend == "memory" and persistence_required():
        raise RuntimeError(
            "PERSISTENCE_REQUIRED=true 时禁止 CHECKPOINTER_BACKEND=memory"
        )
    # P1-4：生产认证配置校验
    if is_production() and is_auth_enabled_env():
        auth_mode = os.getenv("AUTH_MODE", "auto").strip().lower()
        if auth_mode == "auto":
            raise RuntimeError(
                "AUTH_MODE=auto 在生产模式下被禁止；"
                "请设置 AUTH_MODE=jwt 或 AUTH_MODE=trusted_proxy"
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
    "REPO_ROOT",
    "KNOWLEDGE_DIR",
    "LAWTEXT_DIR",
    "Settings",
    "settings",
    "is_official_db_available",
    "is_production",
    "persistence_required",
    "durable_runtime_required",
    "validate_runtime_config",
    "is_auth_enabled_env",
]
