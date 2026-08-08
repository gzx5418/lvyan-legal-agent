"""精确法条号召回（SubTask 8.3 第一部分）。

从用户查询中识别 ``《XX法》第Y条`` 模式，精确匹配 chunk 的 title +
article_number。匹配到的 chunk score=1.0，否则不返回。

公开接口：
    article_no_search(query, chunks=None) -> list[ScoredChunk]
"""

from __future__ import annotations

import re
from typing import Any

from lvyan.retrieval.lexical import ScoredChunk, _load_article_chunks

# 《XX法》第Y条 — Y 支持中文数字与阿拉伯数字
_ARTICLE_NO_RE = re.compile(r"《([^》]+)》第([一二三四五六七八九十百千零0-9]+)条")

# 中文数字 → 阿拉伯数字 用于归一化比对
_CN_DIGIT = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _cn_to_int(text: str) -> int | None:
    """中文数字转 int（与 ingest_laws._parse_chinese_number 等价实现，避免循环依赖）。"""
    if not text:
        return None
    text = text.strip()
    if text.isdigit():
        try:
            return int(text)
        except ValueError:
            return None
    result = 0
    current = 0
    for ch in text:
        if ch in _CN_DIGIT:
            current = _CN_DIGIT[ch]
        elif ch == "十":
            if current == 0:
                current = 1
            result += current * 10
            current = 0
        elif ch == "百":
            if current == 0:
                current = 1
            result += current * 100
            current = 0
        elif ch == "千":
            if current == 0:
                current = 1
            result += current * 1000
            current = 0
    result += current
    return result if result > 0 or text == "零" else None


def _normalize_article_number(raw: str) -> str:
    """归一化法条号：把中文数字转阿拉伯数字，保留「第N条」外壳。

    例如 ``第一千零三十二条`` → ``第1032条``，
    ``第二十三条`` → ``第23条``，``第5条`` → ``第5条``。
    """
    num = _cn_to_int(raw)
    if num is None:
        return f"第{raw}条"
    return f"第{num}条"


def extract_article_refs(query: str) -> list[tuple[str, str, str]]:
    """从查询中抽取 (法律名, 法条号原文, 法条号归一化) 三元组列表。

    >>> extract_article_refs("根据《民法典》第一千零三十二条的规定")
    [('民法典', '一千零三十二', '第1032条')]
    """
    refs: list[tuple[str, str, str]] = []
    for m in _ARTICLE_NO_RE.finditer(query):
        law_name = m.group(1).strip()
        raw_num = m.group(2).strip()
        norm = _normalize_article_number(raw_num)
        refs.append((law_name, raw_num, norm))
    return refs


def _match_law_title(query_name: str, chunk_title: str) -> bool:
    """法律名匹配：query_name（如「民法典」）是否命中 chunk 的 title。

    chunk title 通常是「中华人民共和国民法典」这种带「中华人民共和国」前缀，
    也可能简写为「民法典」，因此做宽松的子串包含判定。
    """
    if not query_name or not chunk_title:
        return False
    qn = query_name.strip()
    ct = chunk_title.strip()
    if qn in ct:
        return True
    # 兼容「民法典」匹配「中华人民共和国民法典」
    # 反向也兼容（chunk_title 简写时）
    if ct in qn:
        return True
    # 兼容「民法典」匹配「中华人民共和国民法典」（去掉国名前缀）
    for prefix in ("中华人民共和国", "全国人民代表大会", "全国人大常委会"):
        if ct.startswith(prefix) and qn in ct[len(prefix) :]:
            return True
    return False


def article_no_search(
    query: str,
    chunks: list[Any] | None = None,
) -> list[ScoredChunk]:
    """精确法条号匹配召回。

    Args:
        query: 用户查询字符串
        chunks: 候选 ArticleChunk；None 时从全库加载

    Returns:
        list[ScoredChunk]：每个匹配的 chunk score=1.0。无匹配时返回空列表。
    """
    refs = extract_article_refs(query)
    if not refs:
        return []

    if chunks is None:
        chunks = _load_article_chunks()
    if not chunks:
        return []

    results: list[ScoredChunk] = []
    seen_ids: set[str] = set()
    for chunk in chunks:
        if isinstance(chunk, dict):
            chunk_id = chunk.get("chunk_id", "")
            title = chunk.get("title", "") or ""
            article_number = chunk.get("article_number", "") or ""
        else:
            chunk_id = getattr(chunk, "chunk_id", "")
            title = getattr(chunk, "title", "") or ""
            article_number = getattr(chunk, "article_number", "") or ""

        if not article_number or not title:
            continue
        if chunk_id in seen_ids:
            continue

        # 归一化 chunk 的法条号
        # article_number 形如「第一千零三十二条」，去掉首尾的「第」「条」取中间
        inner = article_number
        if inner.startswith("第"):
            inner = inner[1:]
        if inner.endswith("条"):
            inner = inner[:-1]
        chunk_norm = _normalize_article_number(inner)

        for law_name, _raw, ref_norm in refs:
            if _match_law_title(law_name, title) and chunk_norm == ref_norm:
                results.append(ScoredChunk(chunk_id=chunk_id, score=1.0, chunk=chunk))
                seen_ids.add(chunk_id)
                break

    return results


__all__ = [
    "article_no_search",
    "extract_article_refs",
]
