"""附件分块排序：小语料 BM25。

与 ``retrieval.lexical`` 的全库索引解耦 —— 附件分块每次只有几十块，
现场计算 IDF 即可，避免加载全局索引。
"""
from __future__ import annotations

import math
import re

from lvyan.schemas.attachment import AttachmentChunk

_TOKEN_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_TOKEN_ASCII_RE = re.compile(r"[a-zA-Z0-9]+")


def _tokenize(text: str) -> list[str]:
    """混合分词：CJK 每字一个 token，ASCII 连续字母数字串一个 token。

    中文无空格，``\\w+`` 会把整句当成一个 token，无法匹配「押金」之类的子串。
    单字切分对小语料 BM25 足够鲁棒。
    """
    tokens: list[str] = list(_TOKEN_CJK_RE.findall(text))
    tokens.extend(w.lower() for w in _TOKEN_ASCII_RE.findall(text))
    return tokens


def rank_chunks(
    query: str,
    chunks: list[AttachmentChunk],
    top_k: int = 6,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[AttachmentChunk]:
    """对 ``chunks`` 用 BM25 打分，返回前 ``top_k`` 条。

    并列分数（含全 0 分）保持原序（Python sort 稳定）。
    """
    if not chunks:
        return []

    tokenized = [_tokenize(c.content) for c in chunks]
    n_docs = len(tokenized)
    avgdl = sum(len(d) for d in tokenized) / n_docs if n_docs else 0.0

    df: dict[str, int] = {}
    for toks in tokenized:
        for term in set(toks):
            df[term] = df.get(term, 0) + 1

    q_terms = _tokenize(query)
    scores = [0.0] * n_docs
    for i, toks in enumerate(tokenized):
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        dl = len(toks) or 1
        norm = 1 - b + b * (dl / (avgdl or 1))
        score = 0.0
        for term in q_terms:
            if term not in tf:
                continue
            idf = math.log(1 + (n_docs - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            f = tf[term]
            score += idf * (f * (k1 + 1)) / (f + k1 * norm)
        scores[i] = score

    order = sorted(range(n_docs), key=lambda i: scores[i], reverse=True)
    return [chunks[i] for i in order[:top_k]]
