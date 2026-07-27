"""Dense 向量召回（SubTask 8.2）。

接入策略：
    - 真实接入：``Qwen/Qwen3-Embedding-0.6B``（settings.embedding_model），
      通过 sentence-transformers 或模型网关 HTTP API。
    - 桩实现：当前环境无法访问 HuggingFace / 模型 API，使用 hash-based
      伪向量（hashlib 把文本映射到 256 维向量），用余弦相似度计算。
      非真实语义检索，但保证接口与流程可用。

公开接口：
    dense_search(query, top_k=20, chunks=None) -> list[ScoredChunk]
    dense_search_bge_m3(query, top_k=20, chunks=None) -> list[ScoredChunk]  # 对照桩
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from lvyan.config import settings
from lvyan.retrieval.case_rule import case_rule_search
from lvyan.retrieval.lexical import (
    ScoredChunk,
    _bm25_tokenize,
    _load_article_chunks,
    bm25_search,
    log,
)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
_DENSE_DIM = 256  # 桩向量维度

# 模块级缓存：记录真实 embedding 的可用性，避免反复网络探测。
# - _REAL_EMBEDDING_PROBED: 是否已尝试真实接入（None=未尝试 / True=可用 / False=不可用）
# - _ST_MODEL_CACHE: sentence-transformers 模型实例（真实接入可用时填充）
_REAL_EMBEDDING_PROBED: bool | None = None
_ST_MODEL_CACHE: Any = None
_DENSE_CANDIDATE_FLOOR = 100
_DENSE_CANDIDATE_MULTIPLIER = 10


# ---------------------------------------------------------------------------
# 桩向量实现：hash → 定长向量
# ---------------------------------------------------------------------------
def _hash_embed(text: str, dim: int = _DENSE_DIM) -> list[float]:
    """把文本哈希成固定维度的向量（桩实现）。

    策略：对文本的 token 列表，每个 token 计算 sha256 → int →
    投影到 dim 维（取模），把对应的维度加 1（计数）+ L2 归一化。
    相同文本得到相同向量；语义相近文本在共享 token 时向量部分重合，
    保证最基本的「同义召回」。
    """
    if not text:
        return [0.0] * dim

    vec = [0.0] * dim
    # 用 _bm25_tokenize 提取 bigrams + 领域术语，保证与 BM25 同口径
    tokens = _bm25_tokenize(text)
    if not tokens:
        # 至少把单字 hash 进去
        for ch in text:
            h = int(hashlib.sha256(ch.encode("utf-8")).hexdigest(), 16)
            vec[h % dim] += 1.0
    else:
        for tok in tokens:
            h = int(hashlib.sha256(tok.encode("utf-8")).hexdigest(), 16)
            vec[h % dim] += 1.0

    # L2 归一化
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度（向量已 L2 归一化时退化为点积）。"""
    if not a or not b:
        return 0.0
    # 兼容未归一化情况，仍计算真实余弦
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# 真实接入骨架（不可用时降级到桩）
# ---------------------------------------------------------------------------
def _probe_real_embedding() -> bool:
    """探测真实 embedding 是否可用，结果缓存到模块级。

    返回 True 表示可用（_ST_MODEL_CACHE 已就绪）；False 表示不可用，
    后续 embed_text 直接走 hash 桩，不再重复探测。
    """
    global _REAL_EMBEDDING_PROBED, _ST_MODEL_CACHE
    if _REAL_EMBEDDING_PROBED is not None:
        return _REAL_EMBEDDING_PROBED

    gateway = settings.model_gateway_url
    # 1) 模型网关 HTTP API（仅当 URL 配置时尝试一次，避免反复网络超时）
    if gateway:
        try:
            import httpx  # type: ignore[import-untyped]

            headers: dict[str, str] = {}
            if settings.model_gateway_api_key:
                headers["Authorization"] = f"Bearer {settings.model_gateway_api_key}"

            # 用一个空字符串探测，验证网关可达性
            resp = httpx.post(
                f"{gateway.rstrip('/')}/v1/embeddings",
                json={"model": settings.embedding_model, "input": "ping"},
                headers=headers,
                timeout=5.0,
            )
            resp.raise_for_status()
            data = resp.json()
            # 校验返回结构
            if data.get("data") and isinstance(data["data"][0].get("embedding"), list):
                _REAL_EMBEDDING_PROBED = True
                log(f"[Dense] 模型网关可用：{gateway}")
                return True
        except Exception as exc:  # noqa: BLE001
            log(f"[Dense] 模型网关不可用 ({exc})，降级到 hash 桩")
            _REAL_EMBEDDING_PROBED = False
            return False

    # 2) 尝试本地 sentence-transformers
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]

        _ST_MODEL_CACHE = SentenceTransformer(settings.embedding_model)
        _REAL_EMBEDDING_PROBED = True
        log(f"[Dense] sentence-transformers 可用：{settings.embedding_model}")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"[Dense] sentence-transformers 不可用 ({exc})，降级到 hash 桩")
        _REAL_EMBEDDING_PROBED = False
        return False


def _try_real_embedding(text: str) -> list[float] | None:
    """尝试用已探测的真实模型计算向量；不可用时返回 None。"""
    if _REAL_EMBEDDING_PROBED is not True:
        return None

    gateway = settings.model_gateway_url
    if gateway:
        try:
            import httpx  # type: ignore[import-untyped]

            headers: dict[str, str] = {}
            if settings.model_gateway_api_key:
                headers["Authorization"] = f"Bearer {settings.model_gateway_api_key}"

            resp = httpx.post(
                f"{gateway.rstrip('/')}/v1/embeddings",
                json={"model": settings.embedding_model, "input": text},
                headers=headers,
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return list(map(float, data["data"][0]["embedding"]))
        except Exception:
            return None

    if _ST_MODEL_CACHE is not None:
        try:
            emb = _ST_MODEL_CACHE.encode(text, normalize_embeddings=True)
            return list(map(float, emb))
        except Exception:
            return None
    return None


def embed_text(text: str) -> list[float]:
    """对文本计算 embedding（真实或桩）。

    优先尝试真实接入（settings.embedding_model），不可用时降级到 hash 桩。
    首次调用探测真实可用性，结果缓存到模块级；后续调用直接走对应路径，
    避免对 85k chunks 重复探测导致的性能问题。
    """
    # TODO: 接入真实 Qwen3-Embedding / BGE-M3
    if _probe_real_embedding():
        real = _try_real_embedding(text)
        if real is not None:
            return real
    return _hash_embed(text)


def _select_dense_candidates(
    query: str,
    chunks: list[Any],
    top_k: int,
) -> list[Any]:
    """为 hash-dense 召回限制候选集，避免首次请求扫描整个法规库。

    Hash 向量只反映词面重叠，不具备独立于 BM25 的语义空间。因此在全库检索时
    先由 BM25 选出候选，再以 hash 相似度做轻量排序；小集合与测试传入集合仍
    全量处理，保持原有接口行为。
    """
    global_chunks = _load_article_chunks.__globals__.get("_GLOBAL_CHUNKS_CACHE")
    if chunks is not global_chunks:
        return chunks

    candidate_limit = min(
        len(chunks), max(_DENSE_CANDIDATE_FLOOR, top_k * _DENSE_CANDIDATE_MULTIPLIER)
    )
    lexical_candidates = bm25_search(query=query, chunks=chunks, top_k=candidate_limit)
    rule_candidates = case_rule_search(query=query, chunks=chunks)
    if lexical_candidates or rule_candidates:
        candidates: list[Any] = []
        seen_ids: set[str] = set()
        for item in [*lexical_candidates, *rule_candidates[:candidate_limit]]:
            chunk = item.chunk
            chunk_id = (
                chunk.get("chunk_id", "")
                if isinstance(chunk, dict)
                else getattr(chunk, "chunk_id", "")
            )
            if chunk_id and chunk_id not in seen_ids:
                candidates.append(chunk)
                seen_ids.add(chunk_id)
        if candidates:
            return candidates
    # 查询没有词面命中时，保留有界降级路径，不在请求线程扫描全库。
    return chunks[:candidate_limit]


# ---------------------------------------------------------------------------
# Dense 检索主接口
# ---------------------------------------------------------------------------
def dense_search(
    query: str,
    top_k: int = 20,
    chunks: list[Any] | None = None,
) -> list[ScoredChunk]:
    """Dense 向量召回。

    Args:
        query: 用户查询字符串
        top_k: 返回前 K 条
        chunks: 候选 ArticleChunk；None 时从全库加载

    Returns:
        list[ScoredChunk]：按余弦相似度降序。

    Hash 降级路径始终在同一向量空间计算查询与文档向量。全库调用会先用
    BM25 预筛选有界候选集，避免在用户请求线程预计算全部法规向量。
    """
    if chunks is None:
        chunks = _load_article_chunks()
    if not chunks:
        return []

    # 文档向量始终是 hash 向量；查询也必须使用同一向量空间，不能混入真实
    # embedding，否则不同维度/分布的向量相似度没有语义意义。
    query_vec = _hash_embed(query)
    if not any(query_vec):
        return []

    candidate_chunks = _select_dense_candidates(query, chunks, top_k)

    scored: list[tuple[int, float]] = []
    for idx, chunk in enumerate(candidate_chunks):
        text = getattr(chunk, "article_text", "") or (
            chunk.get("article_text", "") if isinstance(chunk, dict) else ""
        )
        title = getattr(chunk, "title", "") or (
            chunk.get("title", "") if isinstance(chunk, dict) else ""
        )
        full = f"{title} {text}" if title else text
        chunk_vec = _hash_embed(full)

        sim = _cosine_similarity(query_vec, chunk_vec)
        if sim > 0:
            scored.append((idx, sim))

    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[:top_k]

    results: list[ScoredChunk] = []
    for idx, sim in top:
        chunk = candidate_chunks[idx]
        chunk_id = (chunk.get("chunk_id", "") if isinstance(chunk, dict)
                    else getattr(chunk, "chunk_id", ""))
        results.append(ScoredChunk(chunk_id=chunk_id, score=round(sim, 4), chunk=chunk))
    return results


def dense_search_bge_m3(
    query: str,
    top_k: int = 20,
    chunks: list[Any] | None = None,
) -> list[ScoredChunk]:
    """BGE-M3 对照接入桩（与 :func:`dense_search` 同口径，仅切换模型）。

    TODO: 接入真实 BGE-M3（settings 中预留切换）。
    """
    # 临时切换 embedding_model
    original = settings.embedding_model
    try:
        # 直接复用 dense_search 流程；真实接入后这里改为加载 BGE-M3
        return dense_search(query=query, top_k=top_k, chunks=chunks)
    finally:
        # settings 是 BaseModel 实例，复原不影响
        settings.embedding_model = original


__all__ = [
    "dense_search",
    "dense_search_bge_m3",
    "embed_text",
]
