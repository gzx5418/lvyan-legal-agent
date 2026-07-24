"""案由规则召回（SubTask 8.3 第二部分）。

基于案由关键词到法规标题关键词的规则映射，对查询做案由识别后召回匹配
的 ArticleChunk。

案由→法规映射参考最高人民法院《民事案件案由规定》《最高人民法院关于案由
的若干规定》以及通用法律实务经验，覆盖常见民事 / 劳动 / 侵权 / 知产 /
婚姻家庭 / 合同等领域。

公开接口：
    case_rule_search(query, chunks=None) -> list[ScoredChunk]
"""

from __future__ import annotations

from typing import Any

from lvyan.retrieval.lexical import ScoredChunk, _load_article_chunks

# ---------------------------------------------------------------------------
# 案由 → 法规标题关键词映射
# ---------------------------------------------------------------------------
_CASE_RULE_MAP: dict[str, list[str]] = {
    # 劳动
    "劳动争议": ["劳动合同法", "劳动法", "劳动仲裁", "劳动争议"],
    "劳动仲裁": ["劳动合同法", "劳动法", "劳动仲裁"],
    "经济补偿": ["劳动合同法", "劳动法"],
    "经济赔偿": ["劳动合同法", "劳动法"],
    "辞退": ["劳动合同法", "劳动法"],
    "开除": ["劳动合同法", "劳动法"],
    "解雇": ["劳动合同法", "劳动法"],
    "工伤": ["工伤保险", "劳动法", "劳动合同法"],
    "拖欠工资": ["劳动合同法", "劳动法"],
    "欠薪": ["劳动合同法", "劳动法"],
    # 合同
    "合同纠纷": ["民法典", "合同法"],
    "买卖合同": ["民法典", "合同法"],
    "借款合同": ["民法典", "合同法"],
    "租赁合同": ["民法典", "合同法"],
    "房屋租赁": ["民法典", "合同法"],
    "违约": ["民法典", "合同法"],
    "违约金": ["民法典", "合同法"],
    # 侵权
    "侵权": ["民法典"],
    "侵权责任": ["民法典"],
    "损害赔偿": ["民法典"],
    "人格权": ["民法典"],
    "名誉权": ["民法典"],
    "肖像权": ["民法典"],
    # 隐私 / 个人信息
    "隐私": ["个人信息保护法", "民法典"],
    "个人信息": ["个人信息保护法", "民法典"],
    "数据保护": ["个人信息保护法", "数据安全法", "网络安全法"],
    "健康信息": ["个人信息保护法", "民法典"],
    # 婚姻家庭
    "离婚": ["民法典", "婚姻法"],
    "继承": ["民法典"],
    "遗嘱": ["民法典"],
    "抚养": ["民法典"],
    "赡养": ["民法典"],
    "分割": ["民法典"],
    # 消费者
    "消费者": ["消费者权益保护法"],
    "假货": ["消费者权益保护法", "民法典"],
    "欺诈": ["消费者权益保护法", "民法典"],
    "三倍赔偿": ["消费者权益保护法"],
    "惩罚性赔偿": ["消费者权益保护法", "民法典"],
    # 知识产权
    "知识产权": ["著作权法", "商标法", "专利法"],
    "专利": ["专利法"],
    "商标": ["商标法"],
    "著作权": ["著作权法"],
    # 不动产
    "房产": ["民法典", "不动产登记暂行条例"],
    "不动产": ["民法典", "不动产登记暂行条例"],
    "房屋": ["民法典"],
    # 交通事故
    "交通事故": ["道路交通安全法", "民法典"],
    "机动车": ["道路交通安全法"],
    "肇事": ["道路交通安全法", "刑法"],
    # 公司 / 商事
    "公司": ["公司法"],
    "股权": ["公司法"],
    "破产": ["企业破产法"],
    # 程序
    "诉讼": ["民事诉讼法", "刑事诉讼法", "行政诉讼法"],
    "起诉": ["民事诉讼法"],
    "管辖": ["民事诉讼法"],
    "举证": ["民事诉讼法"],
    "证据": ["民事诉讼法"],
    # 刑事
    "诈骗": ["刑法"],
    "盗窃": ["刑法"],
    "故意伤害": ["刑法"],
    # 电商 / 网络
    "网购": ["电子商务法", "消费者权益保护法"],
    "网络购物": ["电子商务法", "消费者权益保护法"],
    "电子商务": ["电子商务法"],
    "网络交易": ["电子商务法"],
}


def detect_case_keywords(query: str) -> list[str]:
    """从查询中识别案由关键词，返回命中的案由列表。"""
    if not query:
        return []
    matched: list[str] = []
    for case_kw in _CASE_RULE_MAP:
        if case_kw in query:
            matched.append(case_kw)
    return matched


def case_rule_search(
    query: str,
    chunks: list[Any] | None = None,
) -> list[ScoredChunk]:
    """案由规则召回。

    Args:
        query: 用户查询字符串
        chunks: 候选 ArticleChunk；None 时从全库加载

    Returns:
        list[ScoredChunk]：所有匹配的 chunk score=0.8。
        无案由命中或无 chunk 匹配时返回空列表。
    """
    cases = detect_case_keywords(query)
    if not cases:
        return []

    # 聚合所有案由对应的法规关键词
    law_keywords: set[str] = set()
    for case_kw in cases:
        for lk in _CASE_RULE_MAP.get(case_kw, []):
            law_keywords.add(lk)

    if not law_keywords:
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
        else:
            chunk_id = getattr(chunk, "chunk_id", "")
            title = getattr(chunk, "title", "") or ""

        if not title or chunk_id in seen_ids:
            continue

        if any(lk in title for lk in law_keywords):
            results.append(ScoredChunk(chunk_id=chunk_id, score=0.8, chunk=chunk))
            seen_ids.add(chunk_id)

    return results


__all__ = [
    "case_rule_search",
    "detect_case_keywords",
    "_CASE_RULE_MAP",
]
