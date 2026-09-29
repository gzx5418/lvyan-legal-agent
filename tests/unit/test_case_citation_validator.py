"""U-05:类案引用校验器测试。

覆盖:
1. 案号存在性:库内案号通过;检索结果外的案号 → fabricated error;
2. 全/半角括号归一化匹配;
3. 案由一致性:偏离 → warning(不阻断);
4. 无案号文本 / 空 cases 列表的行为;
5. 报告统计与 passed 语义(0 error 即通过)。
"""

from __future__ import annotations

from lvyan.schemas import CaseAuthority
from lvyan.validators.case_citation import validate_case_citations


def _case(case_number: str, case_type: str = "劳动争议") -> CaseAuthority:
    return CaseAuthority(
        case_id=f"id-{case_number}",
        case_number=case_number,
        court="北京市第一中级人民法院",
        case_type=case_type,
        brief_facts="劳动者主张违法解除。",
        ruling_summary="用人单位举证不能。",
        similarity_score=0.9,
    )


def test_known_case_number_passes():
    cases = [_case("(2022)京01民终123号")]
    text = "根据(2022)京01民终123号判决,用人单位应支付赔偿金。"
    report = validate_case_citations(text, cases, case_type="劳动争议")
    assert report.total_case_numbers == 1
    assert report.not_found == 0
    assert report.passed is True


def test_fullwidth_bracket_normalized():
    """库内半角、输出全角(或相反)应归一化匹配。"""
    cases = [_case("(2022)京01民终123号")]
    text = "根据（2022）京01民终123号判决。"
    report = validate_case_citations(text, cases)
    assert report.passed is True
    assert report.not_found == 0


def test_fabricated_case_number_is_error():
    cases = [_case("(2022)京01民终123号")]
    text = "参见(2021)沪0105民初8888号民事判决,同类案件均支持劳动者。"
    report = validate_case_citations(text, cases, case_type="劳动争议")
    assert report.passed is False
    assert report.not_found == 1
    assert report.issues[0].issue_type == "case_not_found"
    assert report.issues[0].severity == "error"
    assert "虚构案例" in report.issues[0].detail


def test_case_type_mismatch_is_warning_not_blocking():
    """库内类案案由与本案案由偏离 → warning,passed 仍为 True。"""
    cases = [_case("(2020)沪01民终99号", case_type="买卖合同纠纷")]
    text = "类案(2020)沪01民终99号认定卖方违约。"
    report = validate_case_citations(text, cases, case_type="劳动争议")
    assert report.passed is True
    assert report.mismatches == 1
    assert report.issues[0].issue_type == "case_type_mismatch"
    assert report.issues[0].severity == "warning"


def test_case_type_prefix_compatible():
    """案由前缀兼容:劳动争议 vs 劳动争议纠纷 视为一致。"""
    cases = [_case("(2022)京01民终123号", case_type="劳动争议纠纷")]
    text = "类案(2022)京01民终123号。"
    report = validate_case_citations(text, cases, case_type="劳动争议")
    assert report.passed is True
    assert report.mismatches == 0


def test_no_case_numbers_in_text():
    """输出无案号 → 全零报告,通过(法条引用审计另行负责)。"""
    report = validate_case_citations("仅引用《劳动合同法》第八十七条。", [], case_type="劳动争议")
    assert report.total_case_numbers == 0
    assert report.passed is True


def test_empty_cases_with_number_is_error():
    """cases 为空但输出含案号 → fabricated(error)。"""
    report = validate_case_citations("参见2021京01民终77号。", [], case_type="劳动争议")
    assert report.passed is False
    assert report.not_found == 1


def test_dict_cases_supported():
    """cases 为 dict 列表(checkpoint 反序列化形态)同样可校验。"""
    cases = [
        {
            "case_id": "id-1",
            "case_number": "(2022)京01民终123号",
            "court": "北京一中院",
            "case_type": "劳动争议",
            "brief_facts": "f",
            "ruling_summary": "r",
            "similarity_score": 0.9,
        }
    ]
    report = validate_case_citations("类案(2022)京01民终123号支持诉求。", cases)
    assert report.passed is True
