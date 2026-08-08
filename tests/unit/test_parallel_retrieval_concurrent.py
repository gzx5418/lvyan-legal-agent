"""验证 statutes 与 cases 检索并发执行（总耗时 ≈ max 而非 sum）。"""

from __future__ import annotations

import time
from unittest.mock import patch

from lvyan.nodes.retrieve_statutes import parallel_retrieval


def _state(queries=None):
    return {
        "retrieval_queries": queries or [{"query_text": "押金返还"}],
        "user_goal": "房东不退押金",
        "law_as_of_date": None,
        "plan": [],
    }


def test_statute_and_case_search_run_concurrently():
    """两个检索各睡 0.3s；并发总耗时 < 0.55s（串行会 ≥ 0.6s）。"""

    def slow_statutes(query, **kw):
        time.sleep(0.3)
        return []

    def slow_cases(query, **kw):
        time.sleep(0.3)
        return None

    with (
        patch("lvyan.nodes.retrieve_statutes.search_statutes", side_effect=slow_statutes),
        patch("lvyan.nodes.retrieve_statutes.search_cases", side_effect=slow_cases),
    ):
        t0 = time.monotonic()
        parallel_retrieval(_state())
        elapsed = time.monotonic() - t0

    assert elapsed < 0.55, f"statutes/cases 未并发，耗时 {elapsed:.2f}s"
