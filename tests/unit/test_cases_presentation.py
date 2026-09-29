"""U-06:类案效力分级呈现测试。

覆盖:
1. 指导性案例头部标注 + 尾部"应当参照"提示;
2. 参考案例标注;
3. 真实类案尾部披露(来源切换语义);
4. curated 兜底维持"非真实案例检索"标注;
5. 混合来源(真实 + curated)双披露。
"""

from __future__ import annotations

from lvyan.schemas import CaseAuthority
from lvyan.nodes.composer_common import _format_cases


def _real_case(case_number: str, level: str = "normal") -> CaseAuthority:
    return CaseAuthority(
        case_id=f"id-{case_number}",
        case_number=case_number,
        court="北京市第一中级人民法院",
        case_type="劳动争议",
        brief_facts="劳动者主张违法解除。",
        ruling_summary="用人单位举证不能,支付赔偿金。",
        similarity_score=0.9,
        effective_level=level,  # type: ignore[arg-type]
    )


def _curated_case() -> dict:
    return {
        "case_id": "curated-1",
        "case_number": None,
        "court": None,
        "case_type": "劳动争议",
        "brief_facts": "欠薪",
        "ruling_summary": "支持劳动者",
        "similarity_score": 0.5,
        "source": "curated_knowledge",
    }


def test_guiding_case_labeled_with_binding_note():
    text = _format_cases([_real_case("(2020)京01民终1号", level="guiding")])
    assert "【指导性案例·参照效力最强】" in text
    assert "各级法院审判类似案件时应当参照" in text
    assert "检索到的真实案例" in text


def test_reference_case_labeled():
    text = _format_cases([_real_case("(2022)京01民终123号", level="reference")])
    assert "【参考案例】" in text
    assert "应当参照" not in text  # 参考案例无"应当参照"效力
    assert "检索到的真实案例" in text


def test_normal_real_case_tail_disclosure():
    text = _format_cases([_real_case("(2022)京01民终123号")])
    assert "### 类案1（(2022)京01民终123号）" in text
    assert "人民法院案例库" in text
    assert "非真实案例检索" not in text


def test_curated_fallback_keeps_non_real_label():
    text = _format_cases([_curated_case()])
    assert "类案参考规则1（精编裁判规则，非真实案例检索）" in text
    assert "非个案检索结果" in text
    assert "检索到的真实案例" not in text


def test_mixed_sources_disclose_both():
    text = _format_cases([_real_case("(2022)京01民终123号"), _curated_case()])
    assert "### 类案1（(2022)京01民终123号）" in text
    assert "类案参考规则2（精编裁判规则，非真实案例检索）" in text
    # 真实来源在场 → 尾部走"真实案例"披露
    assert "检索到的真实案例" in text
