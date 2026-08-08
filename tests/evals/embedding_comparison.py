"""Embedding 模型对比脚本（Task 16.3）。

对金标集分别用当前 Dense 桩（hash 向量，对应 ``dense_search``）与 BGE-M3 桩
（``dense_search_bge_m3``）跑检索，记录两者的 Recall@10 对比。

由于真实模型（Qwen3-Embedding-0.6B/4B 与 BGE-M3）当前环境不可用，两个接口
均会降级到 hash 桩，因此对比结果反映的是桩实现下的基线。真实接入后应替换为
模型网关调用并重新评测。

公开接口：
    compare_embeddings(golden_path=None, top_k=10, limit=None) -> dict

CLI 用法：
    python tests/evals/embedding_comparison.py --limit 5
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

# 路径引导：脚本独立运行时把 AGENT/src 加入 sys.path
_THIS_FILE = Path(__file__).resolve()
_AGENT_DIR = _THIS_FILE.parents[2]
_SRC_DIR = _AGENT_DIR / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from lvyan.retrieval.dense import dense_search, dense_search_bge_m3  # noqa: E402
from lvyan.retrieval.lexical import _load_article_chunks  # noqa: E402

# 复用 retrieval_eval 的金标加载与指标计算
sys.path.insert(0, str(_THIS_FILE.parent))
from retrieval_eval import (  # noqa: E402
    DEFAULT_GOLDEN_PATH,
    _compute_query_metrics,
    _scored_chunk_to_authority,
    load_golden_set,
)
from lvyan.schemas.authority import Authority  # noqa: E402


def _dense_to_authorities(scored_chunks: list[Any]) -> list[Authority]:
    """把 dense_search 返回的 ScoredChunk 列表转成 Authority（用于复用指标计算）。"""
    return [_scored_chunk_to_authority(sc) for sc in scored_chunks]


def _run_dense_variant(
    search_fn: Any,
    golden: list[dict[str, Any]],
    top_k: int,
) -> tuple[float, float, float]:
    """对金标集跑某个 dense 变体，返回 (avg_recall, avg_mrr, avg_ndcg)。"""
    chunks = _load_article_chunks()
    if not chunks:
        return 0.0, 0.0, 0.0

    recalls: list[float] = []
    rrs: list[float] = []
    ndcgs: list[float] = []

    for item in golden:
        query = item.get("query", "")
        expected = item.get("expected_statutes", []) or []
        try:
            scored = search_fn(query=query, top_k=top_k, chunks=chunks)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[EmbedCompare] 检索失败 query={query!r} err={exc}",
                file=sys.stderr,
            )
            scored = []

        authorities = _dense_to_authorities(scored)
        recall, rr, ndcg, _hit_rank, _matched, _repealed = _compute_query_metrics(
            authorities, expected, top_k
        )
        recalls.append(recall)
        rrs.append(rr)
        ndcgs.append(ndcg)

    n = len(golden) or 1
    return sum(recalls) / n, sum(rrs) / n, sum(ndcgs) / n


def compare_embeddings(
    golden_path: str | Path | None = None,
    top_k: int = 10,
    limit: int | None = None,
) -> dict[str, Any]:
    """对比当前 Dense 桩与 BGE-M3 桩在金标集上的检索指标。

    两个接口在真实模型不可用时均降级到 hash 桩，因此对比结果反映桩基线。
    接入真实模型后应重新评测。

    Args:
        golden_path: 金标集路径；None 用默认。
        top_k: 检索 top_k（默认 10）。
        limit: 仅评测前 N 条 query；None 评测全量。

    Returns:
        dict：含 ``dense_hash`` / ``bge_m3`` / ``note`` 三个键，
        每个变体下含 recall_at_k / mrr / ndcg_at_k。
    """
    # TODO: 接入真实 Qwen3-Embedding 0.6B/4B 与 BGE-M3 对比
    golden = load_golden_set(golden_path)
    if limit is not None and limit > 0:
        golden = golden[:limit]

    print(f"[EmbedCompare] 评测 {len(golden)} 条 query (top_k={top_k})", file=sys.stderr)

    # 当前 Dense 桩（hash 向量，对应 settings.embedding_model=Qwen3-Embedding-0.6B）
    print(
        "[EmbedCompare] 运行 dense_search（hash 桩 / Qwen3-Embedding-0.6B 占位）...",
        file=sys.stderr,
    )
    recall_hash, mrr_hash, ndcg_hash = _run_dense_variant(dense_search, golden, top_k)

    # BGE-M3 桩（当前复用 dense_search 流程，仅切换模型名占位）
    print("[EmbedCompare] 运行 dense_search_bge_m3（桩占位）...", file=sys.stderr)
    recall_bge, mrr_bge, ndcg_bge = _run_dense_variant(dense_search_bge_m3, golden, top_k)

    return {
        "dense_hash": {
            "model": "Qwen/Qwen3-Embedding-0.6B",
            "recall_at_k": round(recall_hash, 4),
            "mrr": round(mrr_hash, 4),
            "ndcg_at_k": round(ndcg_hash, 4),
            "note": "桩实现：hash 向量降级，非真实语义检索",
        },
        "bge_m3": {
            "model": "BAAI/bge-m3",
            "recall_at_k": round(recall_bge, 4),
            "mrr": round(mrr_bge, 4),
            "ndcg_at_k": round(ndcg_bge, 4),
            "note": "桩实现：复用 dense_search 流程，待接入真实 BGE-M3",
        },
        "note": (
            "当前环境真实 Embedding 模型不可用，两路均降级到 hash 桩，"
            "对比结果为桩基线。接入真实 Qwen3-Embedding 0.6B/4B 与 BGE-M3 后应重新评测。"
        ),
        "top_k": top_k,
        "num_queries": len(golden),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Embedding 模型对比脚本（Task 16.3）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python tests/evals/embedding_comparison.py --limit 5\n"
            "  python tests/evals/embedding_comparison.py --top-k 10\n"
        ),
    )
    parser.add_argument(
        "--golden",
        default=str(DEFAULT_GOLDEN_PATH),
        help=f"金标集路径（默认 {DEFAULT_GOLDEN_PATH}）",
    )
    parser.add_argument("--top-k", type=int, default=10, help="检索 top_k 值（默认 10）")
    parser.add_argument("--limit", type=int, default=None, help="仅评测前 N 条 query")

    args = parser.parse_args()

    result = compare_embeddings(golden_path=args.golden, top_k=args.top_k, limit=args.limit)

    # 格式化打印
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
