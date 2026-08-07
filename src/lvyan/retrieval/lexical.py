#!/usr/bin/env python3
"""本地词汇检索模块（迁移自 ``律言skill/scripts/query_local.py`` v3）。

适配 AGENT 工程的关键改动：
  - 路径解析改用 ``lvyan.config.settings``（``LAWTEXT_DIR`` / ``KNOWLEDGE_DIR``），
    不再依赖原 ``project_config.py``。
  - 保留核心检索逻辑：同义词扩展、行级 OR 评分（BM25 风格）、跨文件排序、
    文档级 ``max_line_score`` 归一化。
  - **Task 8**：取消官方库「标题预筛」，新增 ``bm25_search`` 全库条文级 BM25
    召回（基于 ArticleChunk），并保留 ``search`` 作为兼容入口。

公开接口：
  ``search(query, search_type="all", top_k=10) -> list[dict]``
  ``bm25_search(query, chunks=None, top_k=20) -> list[ScoredChunk]``

命令行用法:
  python -m lvyan.retrieval.lexical -q "被公司辞退怎么赔偿"
  python -m lvyan.retrieval.lexical -q "买到假货怎么索赔" --type official
  python -m lvyan.retrieval.lexical -q "离婚财产分割" --top 20
  python -m lvyan.retrieval.lexical -q "民间借贷利息" -o result.json --quiet
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 路径解析统一走 lvyan.config（环境变量优先 > AGENT 内默认路径）
from lvyan.config import (  # noqa: E402
    AGENT_DIR,
    settings,
)

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
# 同义词 JSON 文件位置（与本模块同目录）
_SYNONYM_FILE = Path(__file__).resolve().parent / "synonyms.json"


def _resolve_law_index_file() -> Path:
    """官方法律索引文件路径：环境变量 > AGENT/knowledge/manifests/law_index.json。

    索引文件由 ``ingest_laws.py`` CLI 预生成到 ``AGENT/knowledge/manifests/``；
    不存在时返回默认路径，由 ``_load_law_index()`` 容错返回空列表。
    """
    env = os.getenv("LAW_INDEX_FILE")
    if env:
        return Path(env)
    return AGENT_DIR / "knowledge" / "manifests" / "law_index.json"


_LAW_INDEX_FILE: Path = _resolve_law_index_file()

# 结果相对路径展示基准（AGENT 工程根）
_PROJECT_DIR: Path = AGENT_DIR

SEARCH_TYPES = ["law", "case", "evidence", "official", "all"]

# 检索类型 → 知识库目录/文件名提示（沿用原 project_config.SEARCH_TYPE_DIR_MAP）
SEARCH_TYPE_DIR_MAP: dict[str, list[str]] = {
    "law": [
        "law",
        "法条",
        "法规",
        "法律",
        "civil_code",
        "labor_law",
        "consumer_and_tort",
        "procedure_law",
    ],
    "case": ["case", "裁判", "案例", "判例", "case_patterns"],
    "evidence": ["evidence", "证据", "evidence_guide"],
    "official": [],
    "all": [],
}

# 法律领域高频术语词典（用于轻量分词，正向最长匹配）。
# 仅收录法律术语子词（实体/行为/领域），不收录口语长短语。
# 口语长短语（如"买到假货"）应被切成"买到"+"假货"，让子词去匹配。
_DOMAIN_TERMS: list[str] = [
    # 行为/状态（原子词，不再细分）
    "买到",
    "假货",
    "假冒",
    "伪劣",
    "欺诈",
    "索赔",
    "退一赔三",
    "退还",
    "返还",
    "退款",
    "退货",
    "赔偿",
    "补偿",
    "辞退",
    "开除",
    "解雇",
    "解除",
    "终止",
    "违约",
    "欠薪",
    "拖欠工资",
    "不发工资",
    "工伤",
    "受伤",
    "离婚",
    "结婚",
    "继承",
    "遗嘱",
    "借款",
    "欠款",
    "还款",
    "利息",
    "利率",
    # 实体/领域
    "消费者",
    "消费者权益",
    "经营者",
    "用人单位",
    "劳动者",
    "劳动合同",
    "劳动",
    "房东",
    "租客",
    "租赁",
    "租房",
    "房屋租赁",
    "承租",
    "押金",
    "保证金",
    "定金",
    "房产",
    "房屋",
    "不动产",
    "房地产",
    "遗产",
    "交通事故",
    "机动车",
    "肇事",
    "食品安全",
    "食品",
    "知识产权",
    "专利",
    "商标",
    "著作权",
    "网购",
    "网络购物",
    "电子商务",
    "网络交易",
    "隐私",
    "个人信息",
    "数据保护",
    "三倍赔偿",
    "惩罚性赔偿",
    "违约金",
    "损害赔偿",
    # 法律概念
    "诉讼时效",
    "管辖",
    "举证责任",
    "证据",
    "胜诉率",
    "裁判",
    "公司",
    "合同",
]

# 同义词表默认值（synonyms.json 缺失或加载失败时的兜底，保证永不因配置缺失而崩溃）
_DEFAULT_SYNONYM_MAP: dict[str, list[str]] = {
    "假货": ["假货", "假冒", "伪劣", "欺诈", "消费者", "消费者权益"],
    "三倍赔偿": ["三倍", "惩罚性赔偿", "增加赔偿"],
    "索赔": ["索赔", "赔偿", "补偿", "请求赔偿", "主张赔偿"],
    "赔偿": ["赔偿", "补偿", "索赔"],
    "辞退": ["辞退", "解除", "终止", "开除", "解雇"],
    "离婚": ["离婚", "婚姻"],
    "借款": ["借款", "借贷", "贷款", "欠款"],
    "利息": ["利息", "利率", "利率上限"],
    "交通事故": ["交通事故", "机动车", "肇事", "道路交通事故"],
    "工伤": ["工伤", "职业伤害", "工伤保险"],
    "租房": ["租赁", "租房", "房屋租赁", "承租"],
    "押金": ["押金", "保证金", "定金"],
    "食品安全": ["食品安全", "食品", "食品卫生"],
    "违约金": ["违约金", "违约", "损害赔偿"],
    "劳动合同": ["劳动合同", "劳动"],
    "知识产权": ["知识产权", "专利", "商标", "著作权"],
    "房产": ["房产", "房屋", "不动产", "房地产"],
    "遗产": ["遗产", "继承", "遗嘱"],
    "网购": ["网络购物", "电子商务", "网购", "网络交易"],
    "隐私": ["隐私", "个人信息", "数据保护"],
    "欠薪": ["欠薪", "拖欠工资", "工资"],
}


def log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg, file=sys.stderr)


def _load_synonym_map() -> dict[str, list[str]]:
    """加载同义词表，优先从 synonyms.json 读取，失败则用默认值。

    JSON 中以 _ 开头的 key（如 _meta）视为元信息，自动忽略。
    """
    try:
        if _SYNONYM_FILE.is_file():
            with open(_SYNONYM_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            loaded = {k: v for k, v in raw.items() if not k.startswith("_")}
            if loaded:
                return loaded
    except (OSError, json.JSONDecodeError) as e:
        log(f"[Synonym] 加载 {_SYNONYM_FILE.name} 失败 ({e})，使用内置默认表")
    return dict(_DEFAULT_SYNONYM_MAP)


# 模块级加载（仅一次）
SYNONYM_MAP: dict[str, list[str]] = _load_synonym_map()


# ---------------------------------------------------------------------------
# ScoredChunk：四路检索通用结果载体
# ---------------------------------------------------------------------------
@dataclass
class ScoredChunk:
    """检索结果条目：携带 chunk 引用与分数。

    用于 BM25 / Dense / 精确法条号 / 案由规则 / RRF 融合 / Reranker 各路。
    ``chunk`` 字段允许传入 :class:`ArticleChunk` 实例或其 ``model_dump()``
    字典，避免本模块对 ingest_laws 形成硬依赖（懒导入）。
    """

    chunk_id: str
    score: float
    chunk: Any  # ArticleChunk | dict[str, Any]


# ---------------------------------------------------------------------------
# ArticleChunk 懒加载与缓存（避免 ingest_laws 循环导入）
# ---------------------------------------------------------------------------
_ARTICLE_INDEX_FILE: Path = AGENT_DIR / "knowledge" / "manifests" / "article_index_v2.json"
_ARTICLE_INDEX_PKL: Path = AGENT_DIR / "knowledge" / "manifests" / "article_index_v2.pkl"
_BM25_INDEX_FILE: Path = AGENT_DIR / "knowledge" / "manifests" / "bm25_index.json"
_BM25_INDEX_PKL: Path = AGENT_DIR / "knowledge" / "manifests" / "bm25_index.pkl"
ARTICLE_INDEX_SCHEMA_VERSION = 3

# 模块级缓存（仅全局 chunks 路径使用，显式传 chunks 时不污染缓存）
_GLOBAL_CHUNKS_CACHE: list[Any] | None = None
_GLOBAL_BM25_INDEX: dict[str, Any] | None = None


def _load_article_chunks() -> list[Any]:
    """加载全库 ArticleChunk 列表。

    优先读取 ``AGENT/knowledge/manifests/article_index_v2.pkl``（pickle 加速，
    比 JSON 快 3-5x）；其次读取 ``article_index_v2.json``（由
    ``ingest_laws.py`` CLI 预生成）；不存在时调用 ``build_article_index``
    现场构建并落盘，便于后续运行复用。

    返回 ArticleChunk 实例列表（懒导入 ingest_laws 以避免循环依赖）。
    """
    global _GLOBAL_CHUNKS_CACHE
    if _GLOBAL_CHUNKS_CACHE is not None:
        return _GLOBAL_CHUNKS_CACHE

    from lvyan.scripts.ingest_laws import (  # noqa: WPS433 lazy import
        ArticleChunk,
        build_article_index,
        save_index_json,
    )

    # 1) 优先尝试 pickle 缓存（最快）
    if _ARTICLE_INDEX_PKL.is_file():
        try:
            import pickle

            with open(_ARTICLE_INDEX_PKL, "rb") as f:
                cached = pickle.load(f)
            if (
                isinstance(cached, dict)
                and cached.get("schema_version") == ARTICLE_INDEX_SCHEMA_VERSION
                and isinstance(cached.get("chunks"), list)
            ):
                chunks = cached["chunks"]
                _GLOBAL_CHUNKS_CACHE = chunks
                log(f"[BM25] 命中 pickle chunks：{_ARTICLE_INDEX_PKL} (n={len(chunks)})")
                return chunks
        except (OSError, pickle.PickleError, Exception):
            pass

    # 2) 回退到 JSON 缓存
    if _ARTICLE_INDEX_FILE.is_file():
        try:
            with open(_ARTICLE_INDEX_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if (
                not isinstance(raw, dict)
                or raw.get("schema_version") != ARTICLE_INDEX_SCHEMA_VERSION
                or not isinstance(raw.get("chunks"), list)
            ):
                raise ValueError("article index schema version mismatch")
            chunks = [ArticleChunk.model_validate(item) for item in raw["chunks"]]
            if chunks:
                _GLOBAL_CHUNKS_CACHE = chunks
                # 顺手写一份 pickle 加速后续
                try:
                    import pickle

                    with open(_ARTICLE_INDEX_PKL, "wb") as f:
                        pickle.dump(
                            {
                                "schema_version": ARTICLE_INDEX_SCHEMA_VERSION,
                                "chunks": chunks,
                            },
                            f,
                        )
                    log(f"[BM25] 已写入 pickle chunks -> {_ARTICLE_INDEX_PKL}")
                except Exception:
                    pass
                return chunks
        except (OSError, json.JSONDecodeError, Exception):
            # 校验失败则现场重建
            _GLOBAL_CHUNKS_CACHE = None

    # 3) 现场构建（较慢，仅首次运行）
    log("[BM25] article_index_v2 缓存不存在或损坏，现场构建全库 chunks ...")
    chunks = build_article_index()
    try:
        save_index_json(chunks, _ARTICLE_INDEX_FILE)
        log(f"[BM25] 已写入 {len(chunks)} chunks -> {_ARTICLE_INDEX_FILE}")
        # 同时写 pickle
        try:
            import pickle

            with open(_ARTICLE_INDEX_PKL, "wb") as f:
                pickle.dump(
                    {
                        "schema_version": ARTICLE_INDEX_SCHEMA_VERSION,
                        "chunks": chunks,
                    },
                    f,
                )
            log(f"[BM25] 已写入 pickle chunks -> {_ARTICLE_INDEX_PKL}")
        except Exception:
            pass
    except OSError as exc:
        log(f"[BM25] 落盘失败（忽略，仅内存）：{exc}")
    _GLOBAL_CHUNKS_CACHE = chunks
    return chunks


# ---------------------------------------------------------------------------
# BM25 倒排索引构建与缓存
# ---------------------------------------------------------------------------
_CJK_RANGE = ("\u4e00", "\u9fff")


def _is_cjk(ch: str) -> bool:
    return _CJK_RANGE[0] <= ch <= _CJK_RANGE[1]


def _bm25_tokenize(text: str) -> list[str]:
    """BM25 专用分词：字符二元组（bigram）+ 领域词典术语。

    全中文段落抽取 2-gram，保证「个人信息 / 健康信息 / 公开」等查询都能命中
    至少一个 bigram（如「信息」「公开」），解决标题预筛被取消后的召回漏洞。
    同时叠加 ``_tokenize`` 的领域术语，提升法律实体词的权重精度。
    """
    if not text:
        return []

    # 1) 领域词典术语（长词加权）
    domain_tokens = _tokenize(text)

    # 2) 字符二元组：从所有连续中文段落切出 2-gram
    bigrams: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        if not _is_cjk(text[i]):
            i += 1
            continue
        j = i
        while j < n and _is_cjk(text[j]):
            j += 1
        # text[i:j] 是连续中文段
        seg = text[i:j]
        if len(seg) >= 2:
            for k in range(len(seg) - 1):
                bigrams.append(seg[k : k + 2])
        elif len(seg) == 1:
            # 单字也作为一个 token 兜底（避免极短条文零 token）
            bigrams.append(seg)
        i = j if j > i else i + 1

    return domain_tokens + bigrams


def _chunk_field(chunk: Any, name: str, default: str = "") -> str:
    """统一从 dict / 对象读取字段为字符串。"""
    if isinstance(chunk, dict):
        v = chunk.get(name, default)
    else:
        v = getattr(chunk, name, default)
    if v is None:
        return ""
    # 日期等非字符串统一 stringify
    return str(v)


def _compute_chunk_signature(chunks_or_ids: list[Any]) -> str:
    """根据 chunk 列表计算签名，用于 BM25 索引缓存失效判定。

    P1-7：签名必须覆盖影响检索正确性的字段，而不仅是 chunk_id。
    否则法条正文 / 标题 / 施行日期 / 效力状态更新后 chunk_id 不变，旧索引仍
    会被错误复用。现在签名包含：

      chunk_id, title, article_number, article_text, effective_date,
      expiry_date, authority_status（status）, source_revision

    兼容旧调用：``chunks_or_ids`` 为字符串列表时退化为按 id 哈希（旧签名），
    但 :func:`_build_bm25_index` / ``_get_or_build_bm25_index`` 已改为传 chunk 列表。
    """
    h = hashlib.sha256()
    for item in chunks_or_ids:
        if isinstance(item, str):
            # 兼容路径：仅 chunk_id（旧签名，不足以发现正文变更）
            h.update(item.encode("utf-8"))
            h.update(b"\n")
            continue
        chunk = item
        for field in (
            "chunk_id",
            "title",
            "article_number",
            "article_text",
            "effective_date",
            "expiry_date",
            "source_revision",
        ):
            h.update(_chunk_field(chunk, field).encode("utf-8"))
            h.update(b"\x1f")  # 字段分隔符
        # authority_status 字段名在 schema 中可能是 status / authority_status
        status_val = _chunk_field(chunk, "authority_status") or _chunk_field(chunk, "status")
        h.update(status_val.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:16]


def _build_bm25_index(chunks: list[Any]) -> dict[str, Any]:
    """从 chunks 现场构建 BM25 倒排索引。

    返回字典结构：
        {
            "doc_lengths": [int, ...],
            "avgdl": float,
            "inverted": {token: [[doc_idx, tf], ...]},
            "idf": {token: float},
            "n_docs": int,
            "signature": str,
        }
    """
    n_docs = len(chunks)
    if n_docs == 0:
        return {
            "doc_lengths": [],
            "avgdl": 0.0,
            "inverted": {},
            "idf": {},
            "n_docs": 0,
            "signature": "",
        }

    doc_lengths: list[int] = []
    doc_freq: dict[str, int] = {}  # token -> 含该 token 的文档数
    inverted: dict[str, list[tuple[int, int]]] = {}

    for idx, chunk in enumerate(chunks):
        text = getattr(chunk, "article_text", "") or (
            chunk.get("article_text", "") if isinstance(chunk, dict) else ""
        )
        title = getattr(chunk, "title", "") or (
            chunk.get("title", "") if isinstance(chunk, dict) else ""
        )
        # 标题也并入分词（标题命中可显著提升 BM25 命中率）
        tokens = _bm25_tokenize(text) + _bm25_tokenize(title)
        doc_lengths.append(len(tokens))

        tf_map: dict[str, int] = {}
        for tok in tokens:
            tf_map[tok] = tf_map.get(tok, 0) + 1

        for tok, tf in tf_map.items():
            inverted.setdefault(tok, []).append((idx, tf))
            doc_freq[tok] = doc_freq.get(tok, 0) + 1

    avgdl = (sum(doc_lengths) / n_docs) if n_docs > 0 else 0.0

    # IDF（BM25+ 形式，避免负值）
    idf: dict[str, float] = {}
    for tok, df in doc_freq.items():
        idf[tok] = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))

    signature = _compute_chunk_signature(chunks)

    return {
        "doc_lengths": doc_lengths,
        "avgdl": avgdl,
        "inverted": inverted,
        "idf": idf,
        "n_docs": n_docs,
        "signature": signature,
    }


def _serialize_bm25_index(index: dict[str, Any]) -> dict[str, Any]:
    """把内存中的 BM25 索引转为可 JSON 序列化的结构。"""
    return {
        "version": 1,
        "schema_version": ARTICLE_INDEX_SCHEMA_VERSION,
        "signature": index["signature"],
        "n_docs": index["n_docs"],
        "avgdl": index["avgdl"],
        "doc_lengths": index["doc_lengths"],
        # inverted: {token: [[doc_idx, tf], ...]}
        "inverted": {
            tok: [[di, tf] for di, tf in postings] for tok, postings in index["inverted"].items()
        },
        "idf": index["idf"],
    }


def _deserialize_bm25_index(raw: dict[str, Any]) -> dict[str, Any]:
    """把 JSON 反序列化的字典还原成内存结构。"""
    inverted: dict[str, list[tuple[int, int]]] = {}
    for tok, postings in (raw.get("inverted") or {}).items():
        inverted[tok] = [(int(p[0]), int(p[1])) for p in postings]
    return {
        "doc_lengths": list(raw.get("doc_lengths") or []),
        "avgdl": float(raw.get("avgdl") or 0.0),
        "inverted": inverted,
        "idf": {k: float(v) for k, v in (raw.get("idf") or {}).items()},
        "n_docs": int(raw.get("n_docs") or 0),
        "signature": str(raw.get("signature") or ""),
    }


def _load_or_build_global_bm25_index(chunks: list[Any]) -> dict[str, Any]:
    """加载或构建全局 BM25 索引（带磁盘缓存）。

    签名不匹配或缓存缺失时现场重建并落盘到 ``bm25_index.pkl``（pickle 优先，
    比 JSON 快 3-5x）与 ``bm25_index.json``（兼容旧缓存）。
    """
    global _GLOBAL_BM25_INDEX
    if _GLOBAL_BM25_INDEX is not None:
        return _GLOBAL_BM25_INDEX

    expected_sig = _compute_chunk_signature(chunks)

    # 1) 优先尝试 pickle 缓存（更快）
    if _BM25_INDEX_PKL.is_file():
        try:
            import pickle

            with open(_BM25_INDEX_PKL, "rb") as f:
                raw = pickle.load(f)
            if (
                raw.get("schema_version") == ARTICLE_INDEX_SCHEMA_VERSION
                and raw.get("signature") == expected_sig
                and int(raw.get("n_docs", 0)) == len(chunks)
            ):
                _GLOBAL_BM25_INDEX = _deserialize_bm25_index(raw)
                log(
                    f"[BM25] 命中 pickle 缓存：{_BM25_INDEX_PKL} (n_docs={_GLOBAL_BM25_INDEX['n_docs']})"
                )
                return _GLOBAL_BM25_INDEX
            else:
                log("[BM25] pickle 缓存签名不匹配，重建索引 ...")
        except (OSError, pickle.PickleError, Exception) as exc:
            log(f"[BM25] pickle 缓存读取失败 ({exc})，尝试 JSON ...")

    # 2) 回退到 JSON 缓存（兼容旧版本）
    if _BM25_INDEX_FILE.is_file():
        try:
            with open(_BM25_INDEX_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if (
                raw.get("schema_version") == ARTICLE_INDEX_SCHEMA_VERSION
                and raw.get("signature") == expected_sig
                and int(raw.get("n_docs", 0)) == len(chunks)
            ):
                _GLOBAL_BM25_INDEX = _deserialize_bm25_index(raw)
                log(
                    f"[BM25] 命中 JSON 缓存：{_BM25_INDEX_FILE} (n_docs={_GLOBAL_BM25_INDEX['n_docs']})"
                )
                # 顺手写一份 pickle 加速后续
                try:
                    import pickle

                    with open(_BM25_INDEX_PKL, "wb") as f:
                        pickle.dump(_serialize_bm25_index(_GLOBAL_BM25_INDEX), f)
                except Exception:
                    pass
                return _GLOBAL_BM25_INDEX
            else:
                log("[BM25] JSON 缓存签名不匹配，重建索引 ...")
        except (OSError, json.JSONDecodeError, Exception) as exc:
            log(f"[BM25] JSON 缓存读取失败 ({exc})，重建索引 ...")

    # 3) 现场构建并落盘（同时写 pickle + JSON）
    log(f"[BM25] 构建 BM25 倒排索引（{len(chunks)} chunks）...")
    index = _build_bm25_index(chunks)
    try:
        _BM25_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        serialized = _serialize_bm25_index(index)
        # pickle（首选）
        try:
            import pickle

            with open(_BM25_INDEX_PKL, "wb") as f:
                pickle.dump(serialized, f)
            log(f"[BM25] 已写入 pickle 索引 -> {_BM25_INDEX_PKL}")
        except Exception as exc:
            log(f"[BM25] pickle 落盘失败（忽略）：{exc}")
        # JSON（兼容备份）
        with open(_BM25_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(serialized, f, ensure_ascii=False)
        log(f"[BM25] 已写入 JSON 索引 -> {_BM25_INDEX_FILE}")
    except OSError as exc:
        log(f"[BM25] 索引落盘失败（仅内存）：{exc}")

    _GLOBAL_BM25_INDEX = index
    return index


# ---------------------------------------------------------------------------
# BM25 评分参数
# ---------------------------------------------------------------------------
_BM25_K1 = 1.5
_BM25_B = 0.75
_TITLE_MATCH_BOOST = 1.2  # 标题命中查询词时加分（非硬过滤）


def _bm25_score(
    query_tokens: list[str],
    index: dict[str, Any],
    doc_idx: int,
) -> float:
    """对单个文档计算 BM25 分数（query_tokens 已分词）。"""
    score = 0.0
    doc_len = index["doc_lengths"][doc_idx]
    avgdl = index["avgdl"] or 1.0
    idf_table = index["idf"]
    inverted = index["inverted"]

    # 去重 query tokens 避免重复计分
    seen_tokens = set()
    for tok in query_tokens:
        if tok in seen_tokens:
            continue
        seen_tokens.add(tok)
        idf = idf_table.get(tok)
        if idf is None:
            continue
        postings = inverted.get(tok)
        if not postings:
            continue
        # postings 是 list of (doc_idx, tf)；用二分或线性查 doc_idx
        # 由于构建时按 doc_idx 递增写入，可用线性扫描或简单查表
        tf = 0
        for di, t in postings:
            if di == doc_idx:
                tf = t
                break
            elif di > doc_idx:
                break  # 已超过目标，提前结束
        if tf <= 0:
            continue
        # BM25 公式
        denom = tf + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * (doc_len / avgdl))
        score += idf * (tf * (_BM25_K1 + 1.0)) / denom
    return score


def _bm25_score_bulk(
    query_tokens: list[str],
    index: dict[str, Any],
) -> dict[int, float]:
    """批量计算所有文档的 BM25 分数，只返回 > 0 的文档。

    利用倒排索引：对每个 query token 取 postings list，把分数累加到对应 doc。
    比逐文档评分快 O(N) 倍（N=文档数）。
    """
    scores: dict[int, float] = {}
    avgdl = index["avgdl"] or 1.0
    doc_lengths = index["doc_lengths"]
    idf_table = index["idf"]
    inverted = index["inverted"]

    seen_tokens = set()
    for tok in query_tokens:
        if tok in seen_tokens:
            continue
        seen_tokens.add(tok)
        idf = idf_table.get(tok)
        if idf is None:
            continue
        postings = inverted.get(tok)
        if not postings:
            continue
        for doc_idx, tf in postings:
            doc_len = doc_lengths[doc_idx]
            denom = tf + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * (doc_len / avgdl))
            scores[doc_idx] = scores.get(doc_idx, 0.0) + idf * (tf * (_BM25_K1 + 1.0)) / denom

    return scores


def bm25_search(
    query: str,
    chunks: list[Any] | None = None,
    top_k: int = 20,
) -> list[ScoredChunk]:
    """全库条文级 BM25 召回（取消标题预筛）。

    Args:
        query: 用户查询字符串
        chunks: 候选 ArticleChunk 列表；为 None 时从全库加载（带磁盘缓存）
        top_k: 返回前 K 条结果

    Returns:
        list[ScoredChunk]：按 score 降序，最多 top_k 条。每条含 chunk_id /
        score / chunk 引用。

    性能：
        - 全库 85639 chunks 首次构建倒排索引 ~10-30s（仅一次，落盘复用）
        - 命中缓存后单次查询 < 1s
        - 显式传 chunks 时按 chunks 规模现场构建（小规模 <100ms）
    """
    if chunks is None:
        chunks = _load_article_chunks()
    if not chunks:
        return []

    # 显式传 chunks（小规模）→ 现场构建；为 None（全局）→ 用磁盘缓存
    if chunks is _GLOBAL_CHUNKS_CACHE and _GLOBAL_BM25_INDEX is not None:
        index = _GLOBAL_BM25_INDEX
    elif chunks is _GLOBAL_CHUNKS_CACHE:
        index = _load_or_build_global_bm25_index(chunks)
    else:
        # 显式传入的小集合：直接现场构建（不污染全局缓存）
        index = _build_bm25_index(chunks)

    # 查询分词：领域词典 + bigram，并叠加同义词扩展
    raw_tokens = [t for t in query.replace(",", " ").replace("，", " ").split() if t.strip()]
    query_tokens: list[str] = []
    seen: set[str] = set()
    for tok in raw_tokens:
        sub_tokens = _tokenize(tok)
        if not sub_tokens:
            sub_tokens = [tok]
        for st in sub_tokens:
            if st and st not in seen:
                seen.add(st)
                query_tokens.append(st)
    # 同义词扩展
    expanded = _expand_keywords(query_tokens)
    # 加上查询原文的 bigram 兜底（确保「公开」「信息」等被切出来）
    query_tokens_full = list(expanded) + _bm25_tokenize(query)

    if not query_tokens_full:
        return []

    # 批量评分
    scores = _bm25_score_bulk(query_tokens_full, index)
    if not scores:
        return []

    # 标题命中加分（仅作 boost，非硬过滤）
    query_terms = [t for t in (expanded or query_tokens) if len(t) >= 2]
    boosted: list[tuple[int, float]] = []
    for doc_idx, sc in scores.items():
        chunk = chunks[doc_idx]
        title = getattr(chunk, "title", "") or (
            chunk.get("title", "") if isinstance(chunk, dict) else ""
        )
        if title and any(term in title for term in query_terms):
            sc *= _TITLE_MATCH_BOOST
        boosted.append((doc_idx, sc))

    boosted.sort(key=lambda x: x[1], reverse=True)
    top = boosted[:top_k]

    results: list[ScoredChunk] = []
    for doc_idx, sc in top:
        chunk = chunks[doc_idx]
        chunk_id = getattr(chunk, "chunk_id", "") or (
            chunk.get("chunk_id", "") if isinstance(chunk, dict) else ""
        )
        results.append(ScoredChunk(chunk_id=chunk_id, score=round(sc, 4), chunk=chunk))
    return results


def _expand_keywords(keywords: list[str]) -> list[str]:
    expanded = set(keywords)
    for kw in keywords:
        if kw in SYNONYM_MAP:
            for syn in SYNONYM_MAP[kw]:
                expanded.add(syn)
    return list(expanded)


def _build_keyword_groups(keywords: list[str]) -> list[list[str]]:
    """每个原始关键词及其同义词构成一组（组内 OR）。"""
    groups: list[list[str]] = []
    for kw in keywords:
        group = [kw]
        if kw in SYNONYM_MAP:
            group.extend(SYNONYM_MAP[kw])
        groups.append(group)
    return groups


def _build_domain_trie() -> dict:
    """从领域词典构建前缀树（dict 套 dict），用于正向最长匹配分词。"""
    trie: dict = {}
    for term in _DOMAIN_TERMS:
        node = trie
        for ch in term:
            node = node.setdefault(ch, {})
        node["$"] = True  # 词结束标记
    # 同时把同义词表里的所有词也纳入词典，避免遗漏
    for variants in SYNONYM_MAP.values():
        for term in variants:
            node = trie
            for ch in term:
                node = node.setdefault(ch, {})
            node["$"] = True
    return trie


_DOMAIN_TRIE: dict = _build_domain_trie()


def _tokenize(text: str) -> list[str]:
    """轻量中文分词：基于领域词典的正向最长匹配。

    将口语化长短语切成可匹配的法律术语子串。例如：
      "买到假货"    -> ["买到", "假货"]
      "消费者欺诈"  -> ["消费者", "欺诈"]
      "房东不退押金"-> ["房东", "不", "退", "押金"]  （"退"非领域词，保留原文）

    设计目标：宁可多切不可漏切，让同义词扩展和正文匹配能接管。
    纯标准库实现，无 jieba 依赖。
    """
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # 非汉字字符（空格/标点/数字/英文）直接跳过，交给上层 split 预处理
        if not ("\u4e00" <= ch <= "\u9fff"):
            i += 1
            continue
        # 正向最长匹配：从位置 i 开始，找词典里最长的词
        node = _DOMAIN_TRIE
        j = i
        last_match_end = -1
        while j < n and ("\u4e00" <= text[j] <= "\u9fff"):
            ch = text[j]
            if ch not in node:
                break
            node = node[ch]
            j += 1
            if "$" in node:
                last_match_end = j
        if last_match_end > i:
            tokens.append(text[i:last_match_end])
            i = last_match_end
        else:
            # 未在词典命中：对连续汉字段扫描，仅保留已知术语子串，丢弃噪声
            start = i
            while i < n and ("\u4e00" <= text[i] <= "\u9fff"):
                i += 1
            segment = text[start:i]
            if segment:
                _extend_known_subtokens(segment, tokens)
    return tokens


def _extend_known_subtokens(segment: str, out: list[str]) -> None:
    """从片段中提取所有已知术语子串（领域词典 + 同义词表），追加到 out。

    使用滑动方式在未匹配片段中捞回已知词。优先匹配更长（4→3→2 字）的已知词，
    避免把"劳动者"切成"劳动"。
    """
    known: set[str] = set(_DOMAIN_TERMS)
    for variants in SYNONYM_MAP.values():
        known.update(variants)
    k = 0
    L_seg = len(segment)
    while k < L_seg:
        matched_len = 0
        for L in (4, 3, 2):
            if k + L <= L_seg and segment[k : k + L] in known:
                matched_len = L
                break
        if matched_len:
            out.append(segment[k : k + matched_len])
            k += matched_len
        else:
            k += 1


def _load_law_index() -> list[dict]:
    if _LAW_INDEX_FILE.is_file():
        try:
            with open(_LAW_INDEX_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return []
    return []


def _read_text_safely(md_file: Path) -> str:
    """容错读取：优先 utf-8，失败回退 gbk，再失败返回空串。"""
    try:
        return md_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        try:
            return md_file.read_text(encoding="gbk")
        except (OSError, UnicodeDecodeError):
            return ""


def _collect_knowledge_files(search_type: str) -> list[Path]:
    # 路径来自 lvyan.config.settings（运行时可被环境变量覆盖）
    knowledge_dir = settings.knowledge_dir
    if not knowledge_dir.is_dir():
        return []

    if search_type == "all":
        return sorted(knowledge_dir.rglob("*.md"))

    if search_type == "official":
        return []

    dir_hints = SEARCH_TYPE_DIR_MAP.get(search_type, [])
    files: list[Path] = []
    for md_file in sorted(knowledge_dir.rglob("*.md")):
        rel_parts = md_file.relative_to(knowledge_dir).parts
        matched = False
        for hint in dir_hints:
            if any(hint in part for part in rel_parts):
                matched = True
                break
        if not matched:
            filename_lower = md_file.stem.lower()
            for hint in dir_hints:
                if hint in filename_lower:
                    matched = True
                    break
        if matched:
            files.append(md_file)

    return files


def _collect_official_files(keywords: list[str], expanded_keywords: list[str]) -> list[Path]:
    """[已弃用] 官方库候选文件收集：标题预筛（快）。

    Task 8 已用 :func:`bm25_search` 取代此路径：BM25 在 ArticleChunk 条文级
    倒排索引上做全库召回，不再需要先按标题预筛。

    本函数仅保留给 CLI 的旧入口 ``_search_full`` 使用（已不参与公开 ``search``
    路径），后续可逐步下线。
    """
    lawtext_dir = settings.lawtext_dir
    if not lawtext_dir.is_dir():
        return []

    index = _load_law_index()
    if not index:
        return []

    matched_files: list[Path] = []
    seen: set[Path] = set()
    for entry in index:
        title = entry.get("title", "")
        # 标题命中任一关键词（含同义词扩展）即纳入候选
        if any(kw in title for kw in expanded_keywords):
            fpath = lawtext_dir / entry["category"] / entry["file"]
            if fpath.is_file() and fpath not in seen:
                seen.add(fpath)
                matched_files.append(fpath)

    return matched_files


def _score_line(line: str, keywords: list[str]) -> tuple[float, list[str]]:
    matched: list[str] = []
    score = 0.0
    positions: list[tuple[int, int, str]] = []

    for kw in keywords:
        count = line.count(kw)
        if count > 0:
            matched.append(kw)
            score += 1.0 + (count - 1) * 0.5
            start = 0
            for _ in range(count):
                idx = line.find(kw, start)
                positions.append((idx, idx + len(kw), kw))
                start = idx + 1

    # 共现位置紧凑加权（同一行内多个关键词相邻则加分）
    if len(positions) >= 2:
        positions.sort(key=lambda p: p[0])
        for i in range(1, len(positions)):
            gap = positions[i][0] - positions[i - 1][1]
            if 0 <= gap <= 10:
                score += 0.5

    return score, matched


def _text_matches_groups(text: str, groups: list[list[str]]) -> bool:
    """整文件是否同时命中所有关键词组（AND，组内 OR）。用于精编知识库。"""
    for group in groups:
        if not any(variant in text for variant in group):
            return False
    return True


def _search_in_files(
    files: list[Path],
    keywords: list[str],
    source: str,
    require_all_groups: bool,
) -> list[dict]:
    """在文件列表中逐行评分检索。

    Args:
        require_all_groups: True 时要求整文件正文同时命中所有关键词组
            （用于精编知识库的精确匹配）；False 时放宽为逐行 OR 评分
            （用于官方全文库，解决口语化查询零召回问题）。
    """
    all_matches: list[dict] = []
    groups = _build_keyword_groups(keywords)
    all_variants = list(set(v for g in groups for v in g))

    for md_file in files:
        text = _read_text_safely(md_file)
        if not text:
            continue

        if require_all_groups and not _text_matches_groups(text, groups):
            continue

        lines = text.splitlines()
        file_matches: list[dict] = []

        for line_num, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped:
                continue

            has_any = any(variant in stripped for variant in all_variants)
            if not has_any:
                continue

            score, matched_kws = _score_line(stripped, all_variants)
            groups_hit = sum(1 for g in groups if any(v in stripped for v in g))
            all_keyword_bonus = groups_hit * 0.5 if groups_hit == len(groups) else 0.0

            file_matches.append(
                {
                    "line_number": line_num,
                    "content": stripped,
                    "score": round(score + all_keyword_bonus, 1),
                    "matched_keywords": matched_kws,
                    "all_keywords_in_line": groups_hit == len(groups),
                }
            )

        if file_matches:
            # 文档级得分用 max_line_score 归一化，避免「弱命中多行」压过「强命中少行」
            max_line_score = max(m["score"] for m in file_matches)
            try:
                rel_path = str(md_file.relative_to(_PROJECT_DIR)).replace("\\", "/")
            except ValueError:
                rel_path = md_file.name
            all_matches.append(
                {
                    "file": md_file.name,
                    "path": rel_path,
                    "source": source,
                    "matches": file_matches,
                    "_file_score": round(max_line_score, 1),
                    "_total_score": round(sum(m["score"] for m in file_matches), 1),
                }
            )

    return all_matches


def _search_full(
    query: str,
    search_type: str = "all",
    top_n: int = 10,
    quiet: bool = False,
) -> dict:
    """完整检索（含元信息），供 CLI / 上层编排使用。

    返回字典结构与原 ``query_local.search`` 保持一致。
    """
    # 关键词提取：先按空格/标点粗分，再对每个片段做轻量分词，最后去重保序
    raw_tokens = [t for t in query.replace(",", " ").replace("，", " ").split() if t.strip()]
    keywords: list[str] = []
    seen: set[str] = set()
    for tok in raw_tokens:
        sub_tokens = _tokenize(tok)
        if not sub_tokens:
            sub_tokens = [tok]
        for st in sub_tokens:
            if st and st not in seen:
                seen.add(st)
                keywords.append(st)

    if not keywords:
        return {
            "query": query,
            "type": search_type,
            "results": [],
            "total_matches": 0,
            "files_searched": 0,
            "error": "查询关键词为空",
        }

    expanded_keywords = _expand_keywords(keywords)

    log(f'[Local Query] 查询: "{query}"', quiet)
    log(f"[Keywords] 原始关键词: {keywords}", quiet)
    if len(expanded_keywords) > len(keywords):
        log(f"[Keywords] 扩展关键词: {expanded_keywords}", quiet)
    log(f"[Type] 检索类型: {search_type}", quiet)

    all_matches: list[dict] = []
    knowledge_count = 0
    official_count = 0

    if search_type in ("law", "case", "evidence", "all"):
        knowledge_files = _collect_knowledge_files(search_type)
        knowledge_count = len(knowledge_files)
        log(f"[Knowledge] 知识库文件: {knowledge_count} 个", quiet)
        # 精编知识库：保持 AND 精确匹配（文件小、要求精确）
        knowledge_matches = _search_in_files(
            knowledge_files, keywords, "knowledge", require_all_groups=True
        )
        all_matches.extend(knowledge_matches)

    if search_type in ("official", "all"):
        official_files = _collect_official_files(keywords, expanded_keywords)
        official_count = len(official_files)
        log(f"[Official] 官方法律文件候选: {official_count} 个", quiet)
        # 官方全文库：逐行 OR 评分，不要求整文件 AND（修复召回）
        official_matches = _search_in_files(
            official_files, keywords, "official", require_all_groups=False
        )
        all_matches.extend(official_matches)

    # 排序：max_line_score 为主，total_score 为辅（同 max 分时偏好覆盖更广的文件）
    all_matches.sort(key=lambda f: (f["_file_score"], f["_total_score"]), reverse=True)

    total_lines = sum(len(f["matches"]) for f in all_matches)

    # 截断：top_n 表示返回的「文件数」（top N 个最相关文件），而非匹配行数。
    # 每个文件内部最多展示 max_lines_per_file 条最佳匹配，保证文件多样性
    # （避免官方库某大文件命中上千行、把精编库结果挤出）。
    MAX_LINES_PER_FILE = 5
    trimmed: list[dict] = []
    for f in all_matches[:top_n]:
        sorted_matches = sorted(f["matches"], key=lambda m: m["score"], reverse=True)
        take = min(len(sorted_matches), MAX_LINES_PER_FILE)
        trimmed.append(
            {
                "file": f["file"],
                "path": f["path"],
                "source": f.get("source", ""),
                "match_count": len(f["matches"]),
                "max_score": f["_file_score"],
                "top_matches": sorted_matches[:take],
            }
        )

    files_searched = knowledge_count + official_count

    log(f"[Done] 命中: {len(trimmed)} 个文件, {total_lines} 行匹配", quiet)

    return {
        "query": query,
        "type": search_type,
        "results": trimmed,
        "total_matches": total_lines,
        "files_searched": files_searched,
        "keywords": keywords,
        "expanded_keywords": expanded_keywords if len(expanded_keywords) > len(keywords) else None,
        "database_info": {
            "knowledge_files": knowledge_count,
            "official_indexed": len(_load_law_index()),
            "official_matched": official_count,
        },
    }


def search(query: str, search_type: str = "all", top_k: int = 10) -> list[dict]:
    """公开检索接口：返回 top_k 个最相关文件的命中结果列表。

    Task 8 起统一路由到 :func:`bm25_search`（全库条文级 BM25，**取消标题预筛**）。
    保留 ``search_type`` 参数仅为兼容旧调用者，实际 ``law`` / ``official`` /
    ``all`` 三类都走 BM25；``case`` / ``evidence`` 走精编知识库
    ``_search_full``。

    Args:
        query: 用户查询字符串（空格/标点分隔多关键词）
        search_type: 检索类型
            ``law``=法条 / ``case``=裁判规则 / ``evidence``=证据
            / ``official``=官方法律 / ``all``=全部
        top_k: 返回的最相关结果数（默认 10）

    Returns:
        list[dict]: 每个元素包含 ``chunk_id`` / ``score`` / ``file`` / ``path``
        / ``title`` / ``article_number`` / ``article_text`` / ``source`` 字段。
    """
    # case / evidence 仍走精编知识库（无 ArticleChunk 索引，BM25 不适用）
    if search_type in ("case", "evidence"):
        result = _search_full(query=query, search_type=search_type, top_n=top_k, quiet=True)
        return result["results"]

    # law / official / all → 全库条文级 BM25（取消标题预筛）
    scored = bm25_search(query=query, chunks=None, top_k=top_k)
    return [_scored_chunk_to_dict(sc) for sc in scored]


def _scored_chunk_to_dict(sc: ScoredChunk) -> dict:
    """把 ScoredChunk 转成兼容旧 ``search()`` 返回结构的 dict。"""
    chunk = sc.chunk
    if isinstance(chunk, dict):
        chunk_id = chunk.get("chunk_id", sc.chunk_id)
        title = chunk.get("title", "")
        article_number = chunk.get("article_number", "")
        article_text = chunk.get("article_text", "")
        source_id = chunk.get("source_id", "")
    else:
        chunk_id = getattr(chunk, "chunk_id", sc.chunk_id)
        title = getattr(chunk, "title", "")
        article_number = getattr(chunk, "article_number", "")
        article_text = getattr(chunk, "article_text", "")
        source_id = getattr(chunk, "source_id", "")

    return {
        "chunk_id": chunk_id,
        "source_id": source_id,
        "title": title,
        "article_number": article_number,
        "article_text": article_text,
        "score": sc.score,
        "source": "official",
        "file": f"{title}#{article_number}" if article_number else title,
        "path": f"chunks/{chunk_id}",
        "max_score": sc.score,
        "match_count": 1,
        "top_matches": [
            {
                "line_number": 0,
                "content": article_text[:500],
                "score": sc.score,
                "matched_keywords": [],
                "all_keywords_in_line": False,
            }
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="本地法律知识库检索 — 精编知识库 + 官方法律法规全文搜索",
        epilog=(
            "示例:\n"
            '  python -m lvyan.retrieval.lexical -q "被公司辞退怎么赔偿"\n'
            '  python -m lvyan.retrieval.lexical -q "买到假货怎么索赔" --type official\n'
            '  python -m lvyan.retrieval.lexical -q "离婚财产分割" --top 20\n'
            '  python -m lvyan.retrieval.lexical -q "民间借贷利息" -o result.json --quiet'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-q",
        "--query",
        required=True,
        help="搜索查询，空格分隔多个关键词",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="输出 JSON 文件路径（不指定则输出到 stdout）",
    )
    parser.add_argument(
        "--type",
        default="all",
        choices=SEARCH_TYPES,
        help="检索类型: law=法条, case=裁判规则, evidence=证据, official=官方法律, all=全部",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="静默模式",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="返回前 N 个最相关的结果（默认 10）",
    )

    args = parser.parse_args()

    result = _search_full(
        query=args.query,
        search_type=args.type,
        top_n=args.top,
        quiet=args.quiet,
    )

    output_text = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output:
        out_path = Path(args.output)
        if out_path.parent and not out_path.parent.exists():
            out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_text, encoding="utf-8")
        log(f"[Output] 结果已写入: {args.output}", args.quiet)
    else:
        print(output_text)


if __name__ == "__main__":
    main()
