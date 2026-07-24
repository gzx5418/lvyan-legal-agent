"""法律问答检索评测脚本（Task 16）。

对金标法律问答集运行 ``search_statutes``，计算 Recall@k / MRR / nDCG@k /
正确法条命中率 / 正确版本命中率 / 废止法规误召回率，并支持与 reranker 对比。

公开接口：
    evaluate_retrieval(golden_path=None, top_k=10, limit=None) -> EvalReport
    evaluate_with_reranker(golden_path=None, top_k=10, limit=None) -> EvalReport

CLI 用法：
    python tests/evals/retrieval_eval.py --limit 5
    python tests/evals/retrieval_eval.py --golden tests/evals/golden_set.json --top-k 10
    python tests/evals/retrieval_eval.py --with-reranker --limit 5
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 路径引导：确保以脚本形式运行时（python tests/evals/retrieval_eval.py）
# 也能从 AGENT/src 导入 lvyan 包。pyproject.toml 已配 pythonpath=["src"]，
# 故 pytest 运行不需要这段。
# ---------------------------------------------------------------------------
_THIS_FILE = Path(__file__).resolve()
_AGENT_DIR = _THIS_FILE.parents[2]  # AGENT/
_SRC_DIR = _AGENT_DIR / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from lvyan.retrieval.hybrid import hybrid_search  # noqa: E402
from lvyan.retrieval.reranker import rerank  # noqa: E402
from lvyan.retrieval.version_aware import search_statutes  # noqa: E402
from lvyan.schemas.authority import Authority  # noqa: E402

# 默认金标集路径（相对 AGENT 工程根）
DEFAULT_GOLDEN_PATH = _AGENT_DIR / "tests" / "evals" / "golden_set.json"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class QueryResult:
    """单条金标 query 的评测结果。"""

    query_id: str
    query: str
    category: str
    hit: bool  # 是否至少命中一条 expected_statute
    hit_rank: int | None  # 第一条命中结果的排名（1-based）；未命中为 None
    recalled_titles: list[str] = field(default_factory=list)  # 返回结果的 title 列表（按排名）
    recalled_statuses: list[str] = field(default_factory=list)  # 与 titles 对应的 status
    expected_titles: list[str] = field(default_factory=list)
    matched_expected: list[str] = field(default_factory=list)  # 命中的 expected title 列表
    repealed_in_results: list[str] = field(default_factory=list)  # 结果中 status=repealed 的 title
    recall_at_k: float = 0.0
    reciprocal_rank: float = 0.0  # 1/rank，未命中为 0
    ndcg_at_k: float = 0.0


@dataclass
class EvalReport:
    """整批金标集的评测聚合报告。"""

    total_queries: int = 0
    avg_recall_at_k: float = 0.0
    avg_mrr: float = 0.0
    avg_ndcg_at_k: float = 0.0
    statute_hit_rate: float = 0.0  # 正确法条命中率
    version_hit_rate: float = 0.0  # 正确版本命中率
    repealed_recall_rate: float = 0.0  # 废止法规误召回率
    per_query: list[QueryResult] = field(default_factory=list)
    top_k: int = 10
    label: str = "baseline"  # baseline / reranker

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # per_query 转纯 dict 便于 JSON 序列化
        return d


# ---------------------------------------------------------------------------
# 金标集加载
# ---------------------------------------------------------------------------
def load_golden_set(golden_path: str | Path | None = None) -> list[dict[str, Any]]:
    """加载金标法律问答集 JSON。

    Args:
        golden_path: 金标集路径；None 时用默认路径 ``tests/evals/golden_set.json``

    Returns:
        list[dict]：每条含 id / category / query / expected_statutes /
        expected_effective_only / notes 字段。
    """
    path = Path(golden_path) if golden_path else DEFAULT_GOLDEN_PATH
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"金标集应为 JSON 数组，实际类型 {type(data)}: {path}")
    return data


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------
def _match_expected(
    authority: Authority,
    expected_statutes: list[dict[str, Any]],
) -> str | None:
    """判断某条 Authority 是否命中 expected_statutes 中的某一条。

    命中条件：title 完全匹配且 article_text 含至少一个 article_keywords。

    Returns:
        命中的 expected statute 的 title；未命中返回 None。
    """
    title = authority.title or ""
    text = authority.article_text or ""
    for exp in expected_statutes:
        exp_title = exp.get("title", "")
        if exp_title != title:
            continue
        keywords = exp.get("article_keywords", []) or []
        if not keywords:
            # 无关键词约束时仅凭 title 匹配即视为命中
            return exp_title
        if any(kw in text for kw in keywords):
            return exp_title
    return None


def _compute_query_metrics(
    results: list[Authority],
    expected_statutes: list[dict[str, Any]],
    top_k: int,
) -> tuple[float, float, float, int | None, list[str], list[str]]:
    """计算单条 query 的指标。

    Returns:
        (recall_at_k, reciprocal_rank, ndcg_at_k, hit_rank, matched_titles,
         repealed_titles)
    """
    total_expected = len(expected_statutes) or 1
    matched: set[str] = set()
    gained_expected: set[str] = set()  # 已计入 nDCG gain 的 expected title
    hit_rank: int | None = None
    repealed_titles: list[str] = []
    gains: list[int] = []  # 每个 rank 的 relevance（0/1）

    for rank, authority in enumerate(results[:top_k], start=1):
        rel = 0
        matched_title = _match_expected(authority, expected_statutes)
        if matched_title is not None:
            matched.add(matched_title)
            if hit_rank is None:
                hit_rank = rank
            # nDCG gain 仅在每个 expected statute 首次命中时计 1，避免同一
            # expected 被多条结果命中导致 DCG 超过 IDCG（nDCG > 1）。
            if matched_title not in gained_expected:
                rel = 1
                gained_expected.add(matched_title)
        gains.append(rel)

        if authority.status == "repealed":
            repealed_titles.append(authority.title)

    recall_at_k = len(matched) / total_expected
    reciprocal_rank = (1.0 / hit_rank) if hit_rank is not None else 0.0

    # nDCG@k
    dcg = 0.0
    for i, rel in enumerate(gains, start=1):
        if rel > 0:
            dcg += rel / math.log2(i + 1)
    ideal_hits = min(total_expected, top_k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    ndcg = dcg / idcg if idcg > 0 else 0.0

    return recall_at_k, reciprocal_rank, ndcg, hit_rank, sorted(matched), repealed_titles


def _aggregate(report: EvalReport) -> None:
    """聚合 per_query 到全局指标。"""
    n = report.total_queries
    if n == 0:
        return
    report.avg_recall_at_k = sum(q.recall_at_k for q in report.per_query) / n
    report.avg_mrr = sum(q.reciprocal_rank for q in report.per_query) / n
    report.avg_ndcg_at_k = sum(q.ndcg_at_k for q in report.per_query) / n

    hit_queries = sum(1 for q in report.per_query if q.hit)
    report.statute_hit_rate = hit_queries / n

    # 正确版本命中率：命中且首个命中结果 status=effective 的 query 占比
    version_hit_queries = 0
    for q in report.per_query:
        if not q.hit or q.hit_rank is None:
            continue
        idx = q.hit_rank - 1
        if idx < len(q.recalled_statuses) and q.recalled_statuses[idx] == "effective":
            version_hit_queries += 1
    report.version_hit_rate = version_hit_queries / n

    # 废止法规误召回率：所有返回结果中 status=repealed 的占比
    total_results = sum(len(q.recalled_statuses) for q in report.per_query)
    total_repealed = sum(len(q.repealed_in_results) for q in report.per_query)
    report.repealed_recall_rate = (total_repealed / total_results) if total_results else 0.0


# ---------------------------------------------------------------------------
# 评测主入口（SubTask 16.2）
# ---------------------------------------------------------------------------
def evaluate_retrieval(
    golden_path: str | Path | None = None,
    top_k: int = 10,
    limit: int | None = None,
) -> EvalReport:
    """对金标集运行 ``search_statutes``，计算检索指标。

    Args:
        golden_path: 金标集路径；None 用默认。
        top_k: 检索 top_k（默认 10）。
        limit: 仅评测前 N 条 query（性能考量）；None 评测全量。

    Returns:
        EvalReport：含全局指标与 per_query 详情。
    """
    golden = load_golden_set(golden_path)
    if limit is not None and limit > 0:
        golden = golden[:limit]

    report = EvalReport(top_k=top_k, label="baseline")
    report.total_queries = len(golden)

    for item in golden:
        qid = item.get("id", "")
        query = item.get("query", "")
        category = item.get("category", "")
        expected = item.get("expected_statutes", []) or []
        expected_titles = [e.get("title", "") for e in expected]

        try:
            results = search_statutes(query=query, top_k=top_k)
        except Exception as exc:  # noqa: BLE001
            # 检索失败不应中断整批评测；记录空结果
            print(f"[Eval] 检索失败 qid={qid} query={query!r} err={exc}", file=sys.stderr)
            results = []

        recalled_titles = [r.title or "" for r in results]
        recalled_statuses = [r.status or "" for r in results]

        recall, rr, ndcg, hit_rank, matched, repealed = _compute_query_metrics(
            results, expected, top_k
        )

        qr = QueryResult(
            query_id=qid,
            query=query,
            category=category,
            hit=hit_rank is not None,
            hit_rank=hit_rank,
            recalled_titles=recalled_titles,
            recalled_statuses=recalled_statuses,
            expected_titles=expected_titles,
            matched_expected=matched,
            repealed_in_results=repealed,
            recall_at_k=round(recall, 4),
            reciprocal_rank=round(rr, 4),
            ndcg_at_k=round(ndcg, 4),
        )
        report.per_query.append(qr)

    _aggregate(report)
    return report


# ---------------------------------------------------------------------------
# ScoredChunk → Authority 转换（reranker 路径复用）
# ---------------------------------------------------------------------------
def _scored_chunk_to_authority(sc: Any, jurisdiction: str = "中国大陆") -> Authority:
    """把 hybrid_search 返回的 ScoredChunk 转成 Authority（与 search_statutes 同口径）。"""
    chunk = sc.chunk

    def _get(name: str, default: Any = "") -> Any:
        if isinstance(chunk, dict):
            return chunk.get(name, default)
        return getattr(chunk, name, default)

    article_text = _get("article_text", "") or ""
    article_number = _get("article_number", "") or ""
    return Authority(
        source_id=_get("source_id", "") or "",
        title=_get("title", "") or "",
        article_number=article_number if article_number else None,
        article_text=article_text,
        authority_level=_get("authority_level", "") or "其他",
        publication_date=_get("publication_date", None),
        effective_date=_get("effective_date", None),
        status=_get("status", "unknown") or "unknown",
        jurisdiction=_get("jurisdiction", jurisdiction) or jurisdiction,
        official_source=_get("official_source", None),
        content_hash=_get("content_hash", None),
        retrieved_at=datetime.now(),
        lexical_score=float(sc.score) if sc.score is not None else 0.0,
        dense_score=0.0,
        rerank_score=0.0,
    )


# ---------------------------------------------------------------------------
# Reranker 改善评估（SubTask 16.4）
# ---------------------------------------------------------------------------
def evaluate_with_reranker(
    golden_path: str | Path | None = None,
    top_k: int = 10,
    limit: int | None = None,
) -> EvalReport:
    """对每条 query 先 hybrid_search 再 rerank，对比 rerank 前后的检索指标。

    Args:
        golden_path: 金标集路径；None 用默认。
        top_k: 检索 top_k（默认 10）。
        limit: 仅评测前 N 条 query；None 评测全量。

    Returns:
        EvalReport：reranker 路径的评测报告（label="reranker"）。
    """
    golden = load_golden_set(golden_path)
    if limit is not None and limit > 0:
        golden = golden[:limit]

    report = EvalReport(top_k=top_k, label="reranker")
    report.total_queries = len(golden)

    # reranker 需要更大候选池：取 top_k * 4 路召回后 rerank 到 top_k
    candidate_k = max(top_k * 4, 40)

    for item in golden:
        qid = item.get("id", "")
        query = item.get("query", "")
        category = item.get("category", "")
        expected = item.get("expected_statutes", []) or []
        expected_titles = [e.get("title", "") for e in expected]

        results: list[Authority] = []
        try:
            scored = hybrid_search(query=query, top_k=candidate_k, only_effective=True)
            if scored:
                reranked = rerank(query=query, candidates=scored, top_k=top_k)
                results = [_scored_chunk_to_authority(sc) for sc in reranked]
        except Exception as exc:  # noqa: BLE001
            print(
                f"[Eval-Rerank] 失败 qid={qid} query={query!r} err={exc}",
                file=sys.stderr,
            )
            results = []

        recalled_titles = [r.title or "" for r in results]
        recalled_statuses = [r.status or "" for r in results]

        recall, rr, ndcg, hit_rank, matched, repealed = _compute_query_metrics(
            results, expected, top_k
        )

        qr = QueryResult(
            query_id=qid,
            query=query,
            category=category,
            hit=hit_rank is not None,
            hit_rank=hit_rank,
            recalled_titles=recalled_titles,
            recalled_statuses=recalled_statuses,
            expected_titles=expected_titles,
            matched_expected=matched,
            repealed_in_results=repealed,
            recall_at_k=round(recall, 4),
            reciprocal_rank=round(rr, 4),
            ndcg_at_k=round(ndcg, 4),
        )
        report.per_query.append(qr)

    _aggregate(report)
    return report


# ---------------------------------------------------------------------------
# 报告格式化打印
# ---------------------------------------------------------------------------
def _format_report(report: EvalReport) -> str:
    """把 EvalReport 格式化成可读的多行字符串。"""
    lines: list[str] = []
    sep = "=" * 70
    lines.append(sep)
    lines.append(f"  检索评测报告 [{report.label}]  (top_k={report.top_k}, queries={report.total_queries})")
    lines.append(sep)
    lines.append(f"  Recall@k          : {report.avg_recall_at_k:.4f}")
    lines.append(f"  MRR               : {report.avg_mrr:.4f}")
    lines.append(f"  nDCG@k            : {report.avg_ndcg_at_k:.4f}")
    lines.append(f"  正确法条命中率     : {report.statute_hit_rate:.4f}")
    lines.append(f"  正确版本命中率     : {report.version_hit_rate:.4f}")
    lines.append(f"  废止法规误召回率   : {report.repealed_recall_rate:.4f}")
    lines.append("-" * 70)
    lines.append(f"  {'qid':<14} {'category':<10} {'hit':<4} {'rank':<5} {'recall':<7} {'rr':<6} {'ndcg':<6}")
    lines.append("-" * 70)
    for q in report.per_query:
        rank_str = str(q.hit_rank) if q.hit_rank is not None else "-"
        lines.append(
            f"  {q.query_id:<14} {q.category:<10} "
            f"{'Y' if q.hit else 'N':<4} {rank_str:<5} "
            f"{q.recall_at_k:<7.2f} {q.reciprocal_rank:<6.2f} {q.ndcg_at_k:<6.2f}"
        )
    lines.append(sep)
    return "\n".join(lines)


def _format_comparison(baseline: EvalReport, reranker: EvalReport) -> str:
    """对比 baseline 与 reranker 的指标改善。"""
    lines: list[str] = []
    sep = "=" * 70
    lines.append(sep)
    lines.append("  Reranker 改善对比 (baseline -> reranker)")
    lines.append(sep)
    rows = [
        ("Recall@k", baseline.avg_recall_at_k, reranker.avg_recall_at_k),
        ("MRR", baseline.avg_mrr, reranker.avg_mrr),
        ("nDCG@k", baseline.avg_ndcg_at_k, reranker.avg_ndcg_at_k),
        ("正确法条命中率", baseline.statute_hit_rate, reranker.statute_hit_rate),
        ("正确版本命中率", baseline.version_hit_rate, reranker.version_hit_rate),
        ("废止误召回率", baseline.repealed_recall_rate, reranker.repealed_recall_rate),
    ]
    lines.append(f"  {'metric':<18} {'baseline':<10} {'reranker':<10} {'delta':<10}")
    lines.append("-" * 70)
    for name, b, r in rows:
        delta = r - b
        sign = "+" if delta >= 0 else ""
        lines.append(f"  {name:<18} {b:<10.4f} {r:<10.4f} {sign}{delta:<9.4f}")
    lines.append(sep)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="法律问答检索评测脚本（Task 16）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python tests/evals/retrieval_eval.py --limit 5\n"
            "  python tests/evals/retrieval_eval.py --golden tests/evals/golden_set.json --top-k 10\n"
            "  python tests/evals/retrieval_eval.py --with-reranker --limit 5\n"
        ),
    )
    parser.add_argument(
        "--golden",
        default=str(DEFAULT_GOLDEN_PATH),
        help=f"金标集路径（默认 {DEFAULT_GOLDEN_PATH}）",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="检索 top_k 值（默认 10）",
    )
    parser.add_argument(
        "--with-reranker",
        action="store_true",
        help="同时评估 reranker 并对比改善",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="仅评测前 N 条 query（性能考量，全量 20 条约 80s）",
    )
    parser.add_argument(
        "--json",
        default=None,
        help="把评测报告以 JSON 写入指定文件（可选）",
    )

    args = parser.parse_args()

    print(f"[Eval] 加载金标集: {args.golden}", file=sys.stderr)
    print(f"[Eval] top_k={args.top_k} limit={args.limit}", file=sys.stderr)

    baseline = evaluate_retrieval(golden_path=args.golden, top_k=args.top_k, limit=args.limit)
    print(_format_report(baseline))

    reranker: EvalReport | None = None
    if args.with_reranker:
        print("\n[Eval] 运行 reranker 评测 ...", file=sys.stderr)
        reranker = evaluate_with_reranker(
            golden_path=args.golden, top_k=args.top_k, limit=args.limit
        )
        print(_format_report(reranker))
        print()
        print(_format_comparison(baseline, reranker))

    if args.json:
        out: dict[str, Any] = {"baseline": baseline.to_dict()}
        if reranker is not None:
            out["reranker"] = reranker.to_dict()
        Path(args.json).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[Eval] JSON 报告已写入: {args.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
