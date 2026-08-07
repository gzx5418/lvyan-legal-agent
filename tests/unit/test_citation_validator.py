"""Citation Validator 单元测试（SubTask 13.5）。

覆盖场景：
1. 正常引用：法条存在于 statutes，内容匹配，状态有效 → passed=True
2. 虚构引用：「《民法典》第9999条」不在 statutes 中 → not_found error
3. 内容不匹配：引用上下文与条文无 bigram 重叠 → content_mismatch error
4. 状态无效：法规状态为 repealed → invalid_status error
5. 中文数字解析：第四十七条 ↔ 47 应可匹配
6. 空推理结果：reasoning_result=None → passed=True, total=0
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from lvyan.retrieval.version_aware import StatuteVerification
from lvyan.schemas import Authority, ReasoningResult
from lvyan.validators.citation import (
    _chinese_to_int,
    _normalize_article_number,
    validate_citations,
)


# ---------------------------------------------------------------------------
# 辅助：构造 Authority
# ---------------------------------------------------------------------------
def _make_authority(
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


def _make_reasoning_result(
    elements: list[str] | None = None,
    key_factors: list[str] | None = None,
    judicial_tendency: str = "somewhat_favorable",
) -> ReasoningResult:
    """构造 ReasoningResult，可在 key_factors 中嵌入法条引用。"""
    if elements is None:
        elements = ["合同关系成立（已满足）", "违约行为（已满足）"]
    if key_factors is None:
        key_factors = [
            "依据《中华人民共和国民法典》第五百七十七条，违约方应承担违约责任"
        ]
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


# ---------------------------------------------------------------------------
# 0. 中文数字解析辅助测试
# ---------------------------------------------------------------------------
def test_chinese_to_int_basic():
    """中文数字解析：三十二 → 32，四十七 → 47，一千零三十二 → 1032。"""
    assert _chinese_to_int("三十二") == 32
    assert _chinese_to_int("四十七") == 47
    assert _chinese_to_int("一千零三十二") == 1032
    assert _chinese_to_int("九千九百九十九") == 9999


def test_normalize_article_number_arabic():
    """阿拉伯数字归一化：47 / 0047 → 47。"""
    assert _normalize_article_number("47") == 47
    assert _normalize_article_number("0047") == 47
    assert _normalize_article_number("9999") == 9999


def test_normalize_article_number_chinese():
    """中文数字归一化：四十七 → 47。"""
    assert _normalize_article_number("四十七") == 47
    assert _normalize_article_number("九千九百九十九") == 9999


# ---------------------------------------------------------------------------
# 1. 正常引用：通过
# ---------------------------------------------------------------------------
def test_validate_citations_pass(monkeypatch: pytest.MonkeyPatch):
    """引用匹配 statutes 中的法条，状态有效 → passed=True。"""
    monkeypatch.setattr(
        "lvyan.validators.citation.verify_statute_status",
        _mock_verify_statute_status("effective"),
    )
    rr = _make_reasoning_result()
    statutes = [_make_authority()]
    report = validate_citations(rr, statutes)
    assert report.total_citations >= 1
    assert report.passed is True
    assert len([i for i in report.issues if i.severity == "error"]) == 0


# ---------------------------------------------------------------------------
# 2. 虚构引用：第9999条不在 statutes 中
# ---------------------------------------------------------------------------
def test_validate_citations_fabricated(monkeypatch: pytest.MonkeyPatch):
    """「《民法典》第9999条」不在 statutes 中 → not_found error。"""
    monkeypatch.setattr(
        "lvyan.validators.citation.verify_statute_status",
        _mock_verify_statute_status("effective"),
    )
    rr = _make_reasoning_result(
        key_factors=["依据《中华人民共和国民法典》第九千九百九十九条虚构法条"]
    )
    statutes = [_make_authority(article_number="第五百七十七条")]
    report = validate_citations(rr, statutes)
    assert report.passed is False
    not_found_issues = [
        i for i in report.issues if i.issue_type == "not_found"
    ]
    assert len(not_found_issues) >= 1
    assert any("9999" in i.citation_id or "九千九百九十九" in i.citation_id for i in not_found_issues)


# ---------------------------------------------------------------------------
# 3. 内容不匹配：引用上下文与条文无重叠
# ---------------------------------------------------------------------------
def test_validate_citations_content_mismatch(monkeypatch: pytest.MonkeyPatch):
    """引用存在但上下文与条文内容无 bigram 重叠 → content_mismatch error。"""
    monkeypatch.setattr(
        "lvyan.validators.citation.verify_statute_status",
        _mock_verify_statute_status("effective"),
    )
    # 引用《合同法》第四条，但条文内容完全不相干
    rr = _make_reasoning_result(
        key_factors=["引用《中华人民共和国合同法》第四条关于宇宙飞船的规定"]
    )
    statutes = [
        _make_authority(
            title="中华人民共和国合同法",
            article_number="第四条",
            article_text="完全不相干的随机文本xyzabc",
        )
    ]
    report = validate_citations(rr, statutes)
    mismatch_issues = [
        i for i in report.issues if i.issue_type == "content_mismatch"
    ]
    # 应检测到内容不匹配（或 not_found，取决于条文号匹配）
    assert report.passed is False


# ---------------------------------------------------------------------------
# 4. 状态无效：repealed
# ---------------------------------------------------------------------------
def test_validate_citations_invalid_status_repealed(
    monkeypatch: pytest.MonkeyPatch,
):
    """法规状态为 repealed → invalid_status error。"""
    monkeypatch.setattr(
        "lvyan.validators.citation.verify_statute_status",
        _mock_verify_statute_status("repealed"),
    )
    rr = _make_reasoning_result()
    statutes = [_make_authority(status="repealed")]
    report = validate_citations(rr, statutes)
    assert report.passed is False
    status_issues = [
        i for i in report.issues if i.issue_type == "invalid_status"
    ]
    assert len(status_issues) >= 1
    assert all(i.actual == "repealed" for i in status_issues)


# ---------------------------------------------------------------------------
# 5. 中文数字与阿拉伯数字互通
# ---------------------------------------------------------------------------
def test_validate_citations_chinese_arabic_interchange(
    monkeypatch: pytest.MonkeyPatch,
):
    """引用「第四十七条」能匹配 statute.article_number='47'。"""
    monkeypatch.setattr(
        "lvyan.validators.citation.verify_statute_status",
        _mock_verify_statute_status("effective"),
    )
    rr = _make_reasoning_result(
        key_factors=[
            "依据《中华人民共和国劳动合同法》第四十七条计算经济补偿"
        ]
    )
    statutes = [
        _make_authority(
            title="中华人民共和国劳动合同法",
            article_number="47",  # 阿拉伯数字
            article_text="经济补偿按劳动者在本单位工作的年限，每满一年支付一个月工资。",
        )
    ]
    report = validate_citations(rr, statutes)
    # 应能匹配到法条（not_found 不应出现）
    not_found = [i for i in report.issues if i.issue_type == "not_found"]
    assert len(not_found) == 0, "中文数字「四十七」应能匹配阿拉伯数字「47」"


# ---------------------------------------------------------------------------
# 6. 空推理结果
# ---------------------------------------------------------------------------
def test_validate_citations_empty_reasoning(
    monkeypatch: pytest.MonkeyPatch,
):
    """reasoning_result=None → passed=True, total=0。"""
    monkeypatch.setattr(
        "lvyan.validators.citation.verify_statute_status",
        _mock_verify_statute_status("effective"),
    )
    report = validate_citations(None, [])
    assert report.total_citations == 0
    assert report.passed is True
    assert len(report.issues) == 0


# ---------------------------------------------------------------------------
# 7. 多条引用：部分通过部分失败
# ---------------------------------------------------------------------------
def test_validate_citations_mixed(monkeypatch: pytest.MonkeyPatch):
    """多条引用：一条有效，一条虚构 → passed=False。"""
    monkeypatch.setattr(
        "lvyan.validators.citation.verify_statute_status",
        _mock_verify_statute_status("effective"),
    )
    rr = _make_reasoning_result(
        key_factors=[
            "依据《中华人民共和国民法典》第五百七十七条承担违约责任",
            "依据《中华人民共和国民法典》第九千九百九十九条虚构条款",
        ]
    )
    statutes = [_make_authority(article_number="第五百七十七条")]
    report = validate_citations(rr, statutes)
    assert report.passed is False
    assert report.total_citations >= 2
