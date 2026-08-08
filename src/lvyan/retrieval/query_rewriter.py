"""查询改写（SubTask 8.6）。

用于 citation_verifier 重检索时改写查询。

策略：
    - rewrite_query：简单地在原查询后追加「法律条文 规定」，或用同义词扩展
    - rewrite_for_reretrieval：根据迭代次数调整改写策略
        iteration=1：扩展同义词
        iteration=2：改写为法条号查询（如「《民法典》第X条」）

公开接口：
    rewrite_query(query, failed_citations=None) -> str
    rewrite_for_reretrieval(query, iteration) -> str
"""

from __future__ import annotations

import re

from lvyan.retrieval.lexical import _expand_keywords, _tokenize


# ---------------------------------------------------------------------------
# 模型网关 / LLM 改写骨架
# ---------------------------------------------------------------------------
def _try_llm_rewrite(query: str, hint: str = "") -> str | None:
    """尝试调用 LLM 改写查询，不可用时返回 None。

    通过 settings.model_gateway_url 的 ``/v1/chat/completions`` 接口。
    """
    from lvyan.config import settings

    gateway = settings.model_gateway_url
    if not gateway:
        return None

    try:
        import httpx  # type: ignore[import-untyped]

        system_prompt = (
            "你是法律检索查询改写助手。把用户查询改写为更适合中国法律库检索的形式，"
            "保留核心事实并补充可能的法律术语。只输出改写后的查询，不要解释。"
        )
        user_prompt = f"原查询：{query}\n改写提示：{hint}" if hint else f"原查询：{query}"

        headers: dict[str, str] = {}
        if settings.model_gateway_api_key:
            headers["Authorization"] = f"Bearer {settings.model_gateway_api_key}"

        resp = httpx.post(
            f"{gateway.rstrip('/')}/v1/chat/completions",
            json={
                "model": settings.chat_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
                "max_tokens": 200,
            },
            headers=headers,
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        return content if content else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 同义词扩展改写
# ---------------------------------------------------------------------------
def _expand_query_with_synonyms(query: str) -> str:
    """对查询做同义词扩展，返回拼接了所有同义词的新查询。"""
    tokens = _tokenize(query)
    if not tokens:
        return query
    expanded = _expand_keywords(tokens)
    # 把原查询 + 扩展词拼起来
    base = query
    extra = [w for w in expanded if w not in query]
    if extra:
        return f"{base} {' '.join(extra)}"
    return base


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------
def rewrite_query(
    query: str,
    failed_citations: list[str] | None = None,
) -> str:
    """改写查询用于重检索（桩实现）。

    Args:
        query: 原始查询
        failed_citations: 上一轮失败的引用列表（如 ``["《民法典》第9999条"]``），
            桩实现不深度利用，但可用于在改写中避开

    Returns:
        改写后的查询字符串。桩策略：在原查询后追加「法律条文 规定」并做同义词扩展。

    TODO: 接入 LLM 真实改写
    """
    # 先尝试真实 LLM 改写
    hint = ""
    if failed_citations:
        hint = f"避免以下失败引用：{', '.join(failed_citations)}"
    real = _try_llm_rewrite(query, hint=hint)
    if real and real.strip():
        return real.strip()

    # 桩实现：追加 + 同义词扩展
    base = query.strip()
    # 同义词扩展
    expanded = _expand_query_with_synonyms(base)
    # 追加通用法律检索词
    if "法律条文" not in expanded:
        expanded = f"{expanded} 法律条文"
    if "规定" not in expanded:
        expanded = f"{expanded} 规定"
    return expanded


def rewrite_for_reretrieval(query: str, iteration: int) -> str:
    """根据重检索迭代次数调整改写策略。

    Args:
        query: 原始查询
        iteration: 重检索次数（1-based）

    Returns:
        改写后的查询。策略：
            - iteration <= 1：扩展同义词
            - iteration == 2：改写为「法律条文」+ 法条号查询（如果原查询暗示某法律）
            - iteration >= 3：兜底 — 通用扩展 + 法律术语
    """
    if iteration <= 0:
        return query

    # 先尝试 LLM 改写
    hint_map = {
        1: "第1次重检索：扩展同义词，提升召回率",
        2: "第2次重检索：聚焦法条号，识别「《XX法》第Y条」模式",
    }
    hint = hint_map.get(iteration, f"第{iteration}次重检索：兜底改写")
    real = _try_llm_rewrite(query, hint=hint)
    if real and real.strip():
        return real.strip()

    if iteration == 1:
        # 同义词扩展
        return _expand_query_with_synonyms(query)

    if iteration == 2:
        # 改写为法条号查询：从原查询中抽取可能的法条引用模式
        # 模式 1：用户直接说「民法典 第1032条」
        m = re.search(
            r"(《?[\u4e00-\u9fff]{2,15}法》?)\s*第\s*([一二三四五六七八九十百千零0-9]+)\s*条", query
        )
        if m:
            law = m.group(1)
            num = m.group(2)
            return f"《{law.strip('《》')}》第{num}条 法律条文 规定"

        # 模式 2：用户提到某法律名（如「民法典 隐私」），改写为「《民法典》隐私权 法律条文」
        law_match = re.search(
            r"(民法典|刑法|劳动合同法|劳动法|个人信息保护法|消费者权益保护法|公司法|合同法|著作权法|商标法|专利法|道路交通安全法|电子商务法|网络安全法|数据安全法|行政诉讼法|民事诉讼法|刑事诉讼法|监察法|宪法)",
            query,
        )
        if law_match:
            law = law_match.group(1)
            rest = query.replace(law, "").strip()
            return f"《中华人民共和国{law}》 {rest} 法律条文 规定".strip()

        # 模式 3：兜底
        return f"{query} 法律条文 第X条 规定"

    # iteration >= 3：兜底
    return rewrite_query(query)


__all__ = [
    "rewrite_query",
    "rewrite_for_reretrieval",
]
