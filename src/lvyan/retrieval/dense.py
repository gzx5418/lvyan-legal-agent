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
# - _DOC_VEC_CACHE: "model:chunk_id" → 真实文档向量（键含模型名避免跨模型污染），
#   避免每次请求对候选集重复 embed；容量有界，超限按插入序淘汰最旧
_REAL_EMBEDDING_PROBED: bool | None = None
_ST_MODEL_CACHE: Any = None
_DOC_VEC_CACHE: dict[str, list[float]] = {}
_DENSE_CANDIDATE_FLOOR = 100
_DENSE_CANDIDATE_MULTIPLIER = 10

# 真实 embedding 批量护栏：
# - 单批最多 64 条文本，防止一次请求构造数百 MB 的 HTTP payload
# - 每批超时固定上限 30 秒（旧公式 max(10, 2*len(texts)) 对大批量会算出
#   小时级超时，形同挂死）
_REAL_EMBED_BATCH_SIZE = 64
_REAL_EMBED_BATCH_TIMEOUT = 30.0
# 参与真实 embedding 精排的候选数上限：超过时先按 BM25 预筛取前 N 条，
# 防止对全库 8.5w chunks 逐条 embed
_REAL_RANK_CANDIDATE_LIMIT = 2000
# 文档向量缓存容量上限（条），超限按插入序淘汰最旧的（dict 保序即可）
_DOC_VEC_CACHE_MAX_ENTRIES = 50_000


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


def _try_real_embedding(text: str, model: str | None = None) -> list[float] | None:
    """尝试用已探测的真实模型计算向量；不可用时返回 None。

    ``model`` 为 None 时读 ``settings.embedding_model``（默认行为不变）。
    """
    if _REAL_EMBEDDING_PROBED is not True:
        return None

    model = model or settings.embedding_model
    gateway = settings.model_gateway_url
    if gateway:
        try:
            import httpx  # type: ignore[import-untyped]

            headers: dict[str, str] = {}
            if settings.model_gateway_api_key:
                headers["Authorization"] = f"Bearer {settings.model_gateway_api_key}"

            resp = httpx.post(
                f"{gateway.rstrip('/')}/v1/embeddings",
                json={"model": model, "input": text},
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


def _try_real_embedding_batch(
    texts: list[str],
    model: str | None = None,
) -> list[list[float]] | None:
    """批量真实 embedding；任一环节失败返回 None 由调用方降级。

    优先调用模型网关（OpenAI 兼容 ``/v1/embeddings`` 支持 ``input`` 数组），
    其次本地 sentence-transformers（``encode`` 接受列表）。

    护栏：按 ``_REAL_EMBED_BATCH_SIZE``（64 条/批）分批提交，每批超时固定
    ``_REAL_EMBED_BATCH_TIMEOUT``（30 秒）上限，防止对大批量构造数百 MB 的
    单次 HTTP payload、或按条数线性放大的小时级超时。
    """
    if _REAL_EMBEDDING_PROBED is not True or not texts:
        return None

    model = model or settings.embedding_model
    gateway = settings.model_gateway_url
    if gateway:
        try:
            import httpx  # type: ignore[import-untyped]

            headers: dict[str, str] = {}
            if settings.model_gateway_api_key:
                headers["Authorization"] = f"Bearer {settings.model_gateway_api_key}"

            embeddings: list[list[float]] = []
            for start in range(0, len(texts), _REAL_EMBED_BATCH_SIZE):
                batch = texts[start : start + _REAL_EMBED_BATCH_SIZE]
                resp = httpx.post(
                    f"{gateway.rstrip('/')}/v1/embeddings",
                    json={"model": model, "input": batch},
                    headers=headers,
                    timeout=_REAL_EMBED_BATCH_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                batch_vecs = [list(map(float, d["embedding"])) for d in data["data"]]
                if len(batch_vecs) != len(batch):
                    return None
                embeddings.extend(batch_vecs)
            return embeddings
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


def _doc_vec_cache_key(model: str, chunk_id: str) -> str:
    """文档向量缓存键：``model:chunk_id``。键含模型名，避免跨模型污染缓存。"""
    return f"{model}:{chunk_id}"


def _select_real_rank_candidates(query: str, chunks: list[Any]) -> list[Any]:
    """真实 embedding 精排候选上限护栏。

    候选数超过 ``_REAL_RANK_CANDIDATE_LIMIT``（2000）时，先按 BM25 预筛取
    前 2000 条再进入 embed，防止对全库 8.5w chunks 发起批量 embed；
    BM25 无命中时退化为取前 2000 条。小集合原样返回（不触发 BM25 预筛）。
    """
    if len(chunks) <= _REAL_RANK_CANDIDATE_LIMIT:
        return chunks

    log(
        f"[Dense] 真实精排候选 {len(chunks)} 条超上限 {_REAL_RANK_CANDIDATE_LIMIT}，"
        "按 BM25 预筛截断后再 embed"
    )
    lexical_candidates = bm25_search(query=query, chunks=chunks, top_k=_REAL_RANK_CANDIDATE_LIMIT)
    candidates: list[Any] = []
    seen_ids: set[str] = set()
    for item in lexical_candidates:
        chunk = item.chunk
        chunk_id = _chunk_id_of(chunk)
        if chunk_id and chunk_id not in seen_ids:
            candidates.append(chunk)
            seen_ids.add(chunk_id)
    if not candidates:
        # BM25 无命中：保留有界降级路径，不全库 embed
        return chunks[:_REAL_RANK_CANDIDATE_LIMIT]
    return candidates


def _rank_by_real_embedding(
    query_vec: list[float],
    candidate_chunks: list[Any],
    top_k: int,
    model: str | None = None,
) -> list[ScoredChunk] | None:
    """真实 embedding 路径：批量 embed 候选集（带缓存）并按余弦排序。

    ``model`` 为 None 时读 ``settings.embedding_model``；文档向量缓存键为
    ``model:chunk_id`` 且容量有界（超限按插入序淘汰最旧）。

    返回 None 表示批量 embedding 失败，调用方应降级到 hash 路径。
    """
    model = model or settings.embedding_model
    cached_vecs: dict[int, list[float]] = {}
    to_embed_idx: list[int] = []
    to_embed_text: list[str] = []
    for i, chunk in enumerate(candidate_chunks):
        cid = _chunk_id_of(chunk)
        cache_key = _doc_vec_cache_key(model, cid) if cid else ""
        if cache_key and cache_key in _DOC_VEC_CACHE:
            cached_vecs[i] = _DOC_VEC_CACHE[cache_key]
        else:
            to_embed_idx.append(i)
            to_embed_text.append(_chunk_text_of(chunk))

    if to_embed_text:
        vecs = _try_real_embedding_batch(to_embed_text, model=model)
        if vecs is None or len(vecs) != len(to_embed_text):
            return None
        for j, vec in enumerate(vecs):
            i = to_embed_idx[j]
            cached_vecs[i] = vec
            cid = _chunk_id_of(candidate_chunks[i])
            if cid:
                _DOC_VEC_CACHE[_doc_vec_cache_key(model, cid)] = vec
        # 容量上限：超限按插入序淘汰最旧的（dict 保序）
        while len(_DOC_VEC_CACHE) > _DOC_VEC_CACHE_MAX_ENTRIES:
            _DOC_VEC_CACHE.pop(next(iter(_DOC_VEC_CACHE)))

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
        results.append(ScoredChunk(chunk_id=_chunk_id_of(chunk), score=round(sim, 4), chunk=chunk))
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
        results.append(ScoredChunk(chunk_id=_chunk_id_of(chunk), score=round(sim, 4), chunk=chunk))
    return results


# ---------------------------------------------------------------------------
# Dense 检索主接口
# ---------------------------------------------------------------------------
def dense_search(
    query: str,
    top_k: int = 20,
    chunks: list[Any] | None = None,
    model: str | None = None,
) -> list[ScoredChunk]:
    """Dense 向量召回。

    Args:
        query: 用户查询字符串
        top_k: 返回前 K 条
        chunks: 候选 ArticleChunk；None 时从全库加载
        model: 指定 embedding 模型名；None 时读 ``settings.embedding_model``。
            显式传参代替临时改写全局 settings（多线程下会竞态）。

    Returns:
        list[ScoredChunk]：按余弦相似度降序。

    优先走真实 embedding（``model`` 或 ``settings.embedding_model``）。真实可用
    时查询与文档向量同处一个真实向量空间，文档向量按 ``model:chunk_id`` 缓存
    避免重复 embed，参与精排的候选数有上限（超限时 BM25 预筛截断）。真实探测
    失败、查询向量获取失败或批量 embed 失败时，自动降级到 hash 桩，
    hash 桩内查询与文档同处 hash 空间，绝不跨空间混算。
    """
    if chunks is None:
        chunks = _load_article_chunks()
    if not chunks:
        return []

    # 真实 embedding 路径：语义向量有独立信号空间，不需要 BM25 预筛
    # （BM25 已在 hybrid_search 作为独立路被调用一次）；但候选数超上限时
    # 仍需 BM25 预筛截断，防止对全库 8.5w chunks 发起批量 embed。
    if _probe_real_embedding():
        query_vec = _try_real_embedding(query, model=model)
        if query_vec is not None:
            real_candidates = _select_real_rank_candidates(query, chunks)
            real_results = _rank_by_real_embedding(
                query_vec, real_candidates, top_k, model=model
            )
            if real_results is not None:
                return real_results
            log("[Dense] 真实 embedding 批量失败，降级到 hash 桩")
        else:
            log("[Dense] 查询真实 embedding 不可用，降级到 hash 桩")

    # Hash 降级路径：hash 向量只反映词面重叠，需 BM25 预筛有界候选
    candidate_chunks = _select_dense_candidates(query, chunks, top_k)
    return _rank_by_hash(query, candidate_chunks, top_k)


def dense_search_bge_m3(
    query: str,
    top_k: int = 20,
    chunks: list[Any] | None = None,
) -> list[ScoredChunk]:
    """BGE-M3 对照接入桩（与 :func:`dense_search` 同口径，仅切换模型）。

    通过 ``model`` 参数显式传递模型名到 embed 调用路径；不再临时改写全局
    ``settings.embedding_model``（多线程下会竞态污染其他请求）。
    """
    return dense_search(query=query, top_k=top_k, chunks=chunks, model="BAAI/bge-m3")


__all__ = [
    "dense_search",
    "dense_search_bge_m3",
    "embed_text",
]
