"""Dense 向量召回（SubTask 8.2）。

接入策略：
    - 真实接入：``settings.embedding_model``（默认 ``BAAI/bge-m3``），
      通过模型网关 HTTP API（``/v1/embeddings`` 批量）或本地
      sentence-transformers 计算向量。
    - 降级：真实不可用时回退到 hash 伪向量（bigram 哈希投影），保证
      查询与文档同处一个向量空间，绝不跨空间混算。
    - 文档向量按 ``chunk_id`` 缓存到 ``_DOC_VEC_CACHE``，避免每次请求
      对候选集重复 embed。

公开接口：
    dense_search(query, top_k=20, chunks=None) -> list[ScoredChunk]
    dense_search_bge_m3(query, top_k=20, chunks=None) -> list[ScoredChunk]
    embed_text(text) -> list[float]
"""

from __future__ import annotations

import hashlib
import math
import os
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
# - _DOC_VEC_CACHE: chunk_id → 真实文档向量，避免每次请求对候选集重复 embed
_REAL_EMBEDDING_PROBED: bool | None = None
_ST_MODEL_CACHE: Any = None
_DOC_VEC_CACHE: dict[str, list[float]] = {}
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
        except Exception:  # noqa: BLE001 - optional embedding provider boundary
            return None

    if _ST_MODEL_CACHE is not None:
        try:
            emb = _ST_MODEL_CACHE.encode(text, normalize_embeddings=True)
            return list(map(float, emb))
        except Exception:  # noqa: BLE001 - optional local model boundary
            return None
    return None


def _try_real_embedding_batch(texts: list[str]) -> list[list[float]] | None:
    """批量真实 embedding；任一环节失败返回 None 由调用方降级。

    优先调用模型网关（OpenAI 兼容 ``/v1/embeddings`` 支持 ``input`` 数组），
    其次本地 sentence-transformers（``encode`` 接受列表）。
    """
    if _REAL_EMBEDDING_PROBED is not True or not texts:
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
                json={"model": settings.embedding_model, "input": texts},
                headers=headers,
                timeout=max(10.0, 2.0 * len(texts)),
            )
            resp.raise_for_status()
            data = resp.json()
            return [list(map(float, d["embedding"])) for d in data["data"]]
        except Exception:  # noqa: BLE001 - optional embedding provider boundary
            return None

    if _ST_MODEL_CACHE is not None:
        try:
            embs = _ST_MODEL_CACHE.encode(texts, normalize_embeddings=True)
            return [list(map(float, e)) for e in embs]
        except Exception:  # noqa: BLE001 - optional local model boundary
            return None
    return None


def embed_text(text: str) -> list[float]:
    """对文本计算 embedding（真实或桩）。

    优先尝试真实接入（settings.embedding_model），不可用时降级到 hash 桩。
    首次调用探测真实可用性，结果缓存到模块级；后续调用直接走对应路径，
    避免对 85k chunks 重复探测导致的性能问题。
    """
    # 真实 embedding 已通过 settings.embedding_model 配置接入（Qwen3-Embedding / BGE-M3）
    if _probe_real_embedding():
        real = _try_real_embedding(text)
        if real is not None:
            return real
    allow_fallback = os.getenv("ALLOW_HASH_EMBEDDING_FALLBACK", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not allow_fallback:
        raise RuntimeError(
            "真实 Embedding 不可用，且 ALLOW_HASH_EMBEDDING_FALLBACK=false；"
            "拒绝用伪向量生成法律检索结果"
        )
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


def _chunk_id_of(chunk: Any) -> str:
    """统一从 dict / ArticleChunk 读取 chunk_id。"""
    if isinstance(chunk, dict):
        return chunk.get("chunk_id", "") or ""
    return getattr(chunk, "chunk_id", "") or ""


def _chunk_text_of(chunk: Any) -> str:
    """统一从 dict / ArticleChunk 拼接标题 + 正文，作为 embedding 输入。"""
    if isinstance(chunk, dict):
        title = chunk.get("title", "") or ""
        text = chunk.get("article_text", "") or ""
    else:
        title = getattr(chunk, "title", "") or ""
        text = getattr(chunk, "article_text", "") or ""
    return f"{title} {text}".strip() if title else text


def _rank_by_real_embedding(
    query_vec: list[float],
    candidate_chunks: list[Any],
    top_k: int,
) -> list[ScoredChunk] | None:
    """真实 embedding 路径：批量 embed 候选集（带缓存）并按余弦排序。

    返回 None 表示批量 embedding 失败，调用方应降级到 hash 路径。
    """
    cached_vecs: dict[int, list[float]] = {}
    to_embed_idx: list[int] = []
    to_embed_text: list[str] = []
    for i, chunk in enumerate(candidate_chunks):
        cid = _chunk_id_of(chunk)
        if cid and cid in _DOC_VEC_CACHE:
            cached_vecs[i] = _DOC_VEC_CACHE[cid]
        else:
            to_embed_idx.append(i)
            to_embed_text.append(_chunk_text_of(chunk))

    if to_embed_text:
        vecs = _try_real_embedding_batch(to_embed_text)
        if vecs is None or len(vecs) != len(to_embed_text):
            return None
        for j, vec in enumerate(vecs):
            i = to_embed_idx[j]
            cached_vecs[i] = vec
            cid = _chunk_id_of(candidate_chunks[i])
            if cid:
                _DOC_VEC_CACHE[cid] = vec

    scored: list[tuple[int, float]] = []
    for i, chunk in enumerate(candidate_chunks):
        vec = cached_vecs.get(i)
        if vec is None:
            continue
        sim = _cosine_similarity(query_vec, vec)
        if sim > 0:
            scored.append((i, sim))

    scored.sort(key=lambda x: x[1], reverse=True)
    results: list[ScoredChunk] = []
    for idx, sim in scored[:top_k]:
        chunk = candidate_chunks[idx]
        results.append(
            ScoredChunk(chunk_id=_chunk_id_of(chunk), score=round(sim, 4), chunk=chunk)
        )
    return results


def _rank_by_hash(
    query: str,
    candidate_chunks: list[Any],
    top_k: int,
) -> list[ScoredChunk]:
    """Hash 桩路径：查询与文档同空间，避免跨空间混算。"""
    query_vec = _hash_embed(query)
    if not any(query_vec):
        return []

    scored: list[tuple[int, float]] = []
    for idx, chunk in enumerate(candidate_chunks):
        full = _chunk_text_of(chunk)
        chunk_vec = _hash_embed(full)
        sim = _cosine_similarity(query_vec, chunk_vec)
        if sim > 0:
            scored.append((idx, sim))

    scored.sort(key=lambda x: x[1], reverse=True)
    results: list[ScoredChunk] = []
    for idx, sim in scored[:top_k]:
        chunk = candidate_chunks[idx]
        results.append(
            ScoredChunk(chunk_id=_chunk_id_of(chunk), score=round(sim, 4), chunk=chunk)
        )
    return results


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

    优先走真实 embedding（``settings.embedding_model``）。真实可用时查询与
    文档向量同处一个真实向量空间，文档向量按 ``chunk_id`` 缓存避免重复 embed。
    真实探测失败、查询向量获取失败或批量 embed 失败时，自动降级到 hash 桩，
    hash 桩内查询与文档同处 hash 空间，绝不跨空间混算。
    """
    if chunks is None:
        chunks = _load_article_chunks()
    if not chunks:
        return []

    candidate_chunks = _select_dense_candidates(query, chunks, top_k)

    if _probe_real_embedding():
        query_vec = _try_real_embedding(query)
        if query_vec is not None:
            real_results = _rank_by_real_embedding(query_vec, candidate_chunks, top_k)
            if real_results is not None:
                return real_results
            log("[Dense] 真实 embedding 批量失败，降级到 hash 桩")
        else:
            log("[Dense] 查询真实 embedding 不可用，降级到 hash 桩")

    return _rank_by_hash(query, candidate_chunks, top_k)


def dense_search_bge_m3(
    query: str,
    top_k: int = 20,
    chunks: list[Any] | None = None,
) -> list[ScoredChunk]:
    """BGE-M3 对照接入桩（与 :func:`dense_search` 同口径，仅切换模型）。

    通过 settings.embedding_model 配置切换到 BGE-M3；当前复用 dense_search 通道。
    """
    # 临时切换 embedding_model 到 BGE-M3
    original = settings.embedding_model
    try:
        settings.embedding_model = "BAAI/bge-m3"
        return dense_search(query=query, top_k=top_k, chunks=chunks)
    finally:
        settings.embedding_model = original


__all__ = [
    "dense_search",
    "dense_search_bge_m3",
    "embed_text",
]
