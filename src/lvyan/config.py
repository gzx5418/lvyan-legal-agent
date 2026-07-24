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
_PKG_DIR = Path(__file__).resolve().parent            # AGENT/src/lvyan
_SRC_DIR = _PKG_DIR.parent                            # AGENT/src
AGENT_DIR = _SRC_DIR.parent                           # AGENT/
REPO_ROOT = AGENT_DIR.parent                          # 法律/（仓库根）


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
    model_gateway_api_key: str = Field(default="", description="模型网关 API Key，用于 Authorization: Bearer 头")

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
    max_retrieval_iterations: int = Field(default=3, description="Citation Verifier 最大重检索次数")
    max_cost_budget_usd: float = Field(default=2.0, description="单次 run 最大成本预算（美元）")
    hitl_enabled: bool = Field(default=True, description="是否启用 Human-in-the-loop 不可逆操作审批")

    # --- Legal Reasoner 迭代守卫 ---
    max_legal_reasoner_iterations: int = Field(
        default=2,
        description="Critic 不通过时回退 legal_reasoner 的最大重试次数",
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
        chat_model=_get("CHAT_MODEL", "Qwen/Qwen2.5-7B-Instruct"),
        embedding_model=_get("EMBEDDING_MODEL", "BAAI/bge-m3"),
        reranker_model=_get("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3"),
        vision_model=_get("VISION_MODEL", "Qwen/Qwen3-VL-8B-Instruct"),
        langfuse_host=_get("LANGFUSE_HOST", ""),
        langfuse_public_key=_get("LANGFUSE_PUBLIC_KEY", ""),
        langfuse_secret_key=_get("LANGFUSE_SECRET_KEY", ""),
        knowledge_dir=KNOWLEDGE_DIR,
        lawtext_dir=LAWTEXT_DIR,
        max_retrieval_iterations=_get_int("MAX_RETRIEVAL_ITERATIONS", 3),
        max_cost_budget_usd=_get_float("MAX_COST_BUDGET_USD", 2.0),
        hitl_enabled=_get_bool("HITL_ENABLED", True),
        max_legal_reasoner_iterations=_get_int("MAX_LEGAL_REASONER_ITERATIONS", 2),
    )


# 全局单例：整个 Runtime 共享一份配置
settings: Settings = _build_settings()


def is_official_db_available() -> bool:
    """官方法律全文库是否可用（目录存在且非空）。

    检索脚本据此决定是否启用官方库，或降级为仅精编知识库 + AI 补充模式。
    """
    return LAWTEXT_DIR.is_dir() and any(LAWTEXT_DIR.iterdir())


__all__ = [
    "AGENT_DIR",
    "REPO_ROOT",
    "KNOWLEDGE_DIR",
    "LAWTEXT_DIR",
    "Settings",
    "settings",
    "is_official_db_available",
]
