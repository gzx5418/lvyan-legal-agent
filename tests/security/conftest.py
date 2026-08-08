"""安全评测测试公共 fixture（Task 18）。

提供跨测试文件复用的构造器与 mock：

- :func:`make_authority` / :func:`make_reasoning_result`：构造 ``Authority`` /
  ``ReasoningResult``，便于在引用校验相关测试中复用。
- :func:`mock_statute_status_effective`：将 ``verify_statute_status`` mock 为
  始终返回 ``effective``，避免真实外部查询。
- :func:`tmp_vault` / :func:`tmp_user_prefs`：基于 ``tmp_path`` 的隔离
  ``CaseVault`` / ``UserPreferences``，测试间互不污染。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from lvyan.retrieval.version_aware import StatuteVerification
from lvyan.schemas import Authority, ReasoningResult


# ---------------------------------------------------------------------------
# mock verify_statute_status
# ---------------------------------------------------------------------------
def _mock_verify_statute_status(status: str = "effective"):
    """构造一个 mock 函数，返回指定 status 的 StatuteVerification。"""

    def _mock(source_id: str, as_of: Any = None) -> StatuteVerification:
        return StatuteVerification(
            source_id=source_id,
            title="mock-title",
            current_status=status,  # type: ignore[arg-type]
            checked_at=datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc),
        )

    return _mock


@pytest.fixture
def mock_statute_status_effective(monkeypatch: pytest.MonkeyPatch) -> None:
    """将 citation / authority_status 中的 verify_statute_status mock 为 effective。

    同时 patch 两个模块的引用，避免真实外部查询。
    """
    mock = _mock_verify_statute_status("effective")
    monkeypatch.setattr(
        "lvyan.validators.citation.verify_statute_status",
        mock,
    )
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        mock,
    )


# ---------------------------------------------------------------------------
# 构造器 fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def make_authority():
    """返回构造 Authority 的工厂函数。"""

    def _make(
        title: str = "中华人民共和国民法典",
        article_number: str = "第五百七十七条",
        article_text: str = (
            "当事人一方不履行合同义务或者履行合同义务不符合约定的，"
            "应当承担继续履行、采取补救措施或者赔偿损失等违约责任。"
        ),
        effective_date: date | None = date(2021, 1, 1),
        status: str = "effective",
        source_id: str | None = None,
    ) -> Authority:
        return Authority(
            source_id=source_id or f"src-{title}-{article_number}",
            title=title,
            article_number=article_number,
            article_text=article_text,
            authority_level="法律",
            effective_date=effective_date,
            status=status,  # type: ignore[arg-type]
            retrieved_at=datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc),
        )

    return _make


@pytest.fixture
def make_reasoning_result():
    """返回构造 ReasoningResult 的工厂函数。"""

    def _make(
        elements: list[str] | None = None,
        key_factors: list[str] | None = None,
        judicial_tendency: str = "somewhat_favorable",
    ) -> ReasoningResult:
        if elements is None:
            elements = ["合同关系成立（已满足）", "违约行为（已满足）"]
        if key_factors is None:
            key_factors = ["依据《中华人民共和国民法典》第五百七十七条，违约方应承担违约责任"]
        return ReasoningResult(
            legal_relationship="合同纠纷",
            elements=elements,
            disputed_focus=["是否构成违约"],
            plaintiff_arguments=["原告主张对方违约应赔偿"],
            defendant_arguments=["被告主张不可抗力免责"],
            evidence_mapping=["争议焦点1 → 合同文本"],
            judicial_tendency=judicial_tendency,  # type: ignore[arg-type]
            evidence_confidence="medium",
            key_factors=key_factors,
        )

    return _make


# ---------------------------------------------------------------------------
# 隔离存储 fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def tmp_vault(tmp_path: Path):
    """基于 tmp_path 的隔离 CaseVault，测试间互不污染。"""
    from lvyan.memory.case_vault import CaseVault

    return CaseVault(base_dir=tmp_path / "vault")


@pytest.fixture
def tmp_user_prefs(tmp_path: Path):
    """基于 tmp_path 的隔离 UserPreferences。"""
    from lvyan.memory.user_preferences import UserPreferences

    return UserPreferences(base_dir=tmp_path / "user_prefs")
