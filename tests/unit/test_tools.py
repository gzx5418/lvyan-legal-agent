"""标准工具集单元测试（Task 15）。

覆盖：
  1. search_statutes 返回 StatuteSearchResult 且可 JSON 序列化
  2. get_statute_article 对已知法规返回 found=True
  3. verify_statute_status 返回 is_effective_as_of
  4. calculate_legal_deadline 劳动仲裁 1 年（365 天）
  5. calculate_claim_amount 经济补偿计算
  6. generate_evidence_checklist 劳动争议返回劳动合同等证据
  7. build_case_timeline 排序正确
  8. extract_document 对 .md 文件提取成功
  9. analyze_contract_clause 检测到「定金不退」风险
  10. render_docx 导出（至少 .md 降级成功）
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from lvyan.config import LAWTEXT_DIR
from lvyan.retrieval.version_resolver import find_law_files_by_title, parse_law_metadata
from lvyan.tools import (
    CaseDetailResult,
    CaseSearchResult,
    ClaimAmountResult,
    ContractAnalysisResult,
    DeadlineResult,
    DocumentExtractResult,
    EvidenceChecklistResult,
    ExportResult,
    StatuteArticleResult,
    StatuteSearchResult,
    StatuteStatusResult,
    TimelineResult,
    ToolResult,
    analyze_contract_clause,
    build_case_timeline,
    calculate_claim_amount,
    calculate_legal_deadline,
    extract_document,
    generate_evidence_checklist,
    get_case_detail,
    get_statute_article,
    render_docx,
    search_cases,
    search_statutes,
    verify_statute_status,
)


# ---------------------------------------------------------------------------
# 1. search_statutes
# ---------------------------------------------------------------------------
def test_search_statutes_returns_pydantic_and_serializable():
    """search_statutes 返回 StatuteSearchResult 且可序列化为 JSON。"""
    result = search_statutes("劳动", top_k=3)

    assert isinstance(result, StatuteSearchResult)
    assert isinstance(result, ToolResult)
    assert result.tool_name == "search_statutes"
    assert result.success is True
    assert result.query == "劳动"
    # Pydantic v2 model_dump_json 必须产出合法 JSON
    json_str = result.model_dump_json()
    parsed = json.loads(json_str)
    assert parsed["tool_name"] == "search_statutes"
    assert "results" in parsed
    assert "filtered_out_count" in parsed
    assert "executed_at" in parsed


def test_search_statutes_empty_query_returns_error():
    """空查询应返回 success=False。"""
    result = search_statutes("")
    assert isinstance(result, StatuteSearchResult)
    assert result.success is False
    assert result.error
    assert result.total == 0


# ---------------------------------------------------------------------------
# 2. get_statute_article
# ---------------------------------------------------------------------------
def test_get_statute_article_known_law():
    """对民法典第一条查询，应返回 found=True。"""
    if not LAWTEXT_DIR.is_dir():
        pytest.skip(f"官方法律库目录不存在：{LAWTEXT_DIR}")

    files = find_law_files_by_title("中华人民共和国民法典", LAWTEXT_DIR)
    if not files:
        pytest.skip("未找到民法典文件")
    meta = parse_law_metadata(files[0])

    result = get_statute_article(meta.source_id, "第一条")

    assert isinstance(result, StatuteArticleResult)
    assert result.tool_name == "get_statute_article"
    assert result.success is True
    assert result.found is True
    assert result.source_id == meta.source_id
    assert result.article_number == "第一条"
    assert result.article_text  # 非空
    assert result.title  # 民法典标题
    # JSON 可序列化
    parsed = json.loads(result.model_dump_json())
    assert parsed["found"] is True


def test_get_statute_article_not_found_returns_found_false():
    """不存在的条文应返回 found=False 但 success=True。"""
    result = get_statute_article("nonexistent-source-id", "第一条")

    assert isinstance(result, StatuteArticleResult)
    assert result.success is True  # 调用本身成功
    assert result.found is False


# ---------------------------------------------------------------------------
# 3. verify_statute_status
# ---------------------------------------------------------------------------
def test_verify_statute_status_returns_is_effective_as_of():
    """verify_statute_status 返回 is_effective_as_of 字段。"""
    if not LAWTEXT_DIR.is_dir():
        pytest.skip(f"官方法律库目录不存在：{LAWTEXT_DIR}")

    files = find_law_files_by_title("中华人民共和国民法典", LAWTEXT_DIR)
    if not files:
        pytest.skip("未找到民法典文件")
    meta = parse_law_metadata(files[0])

    # 不指定 as_of
    result = verify_statute_status(meta.source_id)
    assert isinstance(result, StatuteStatusResult)
    assert result.tool_name == "verify_statute_status"
    assert result.success is True
    assert result.source_id == meta.source_id
    assert isinstance(result.is_effective_as_of, bool)
    # 民法典当前应为有效
    assert result.current_status == "effective"
    assert result.is_effective_as_of is True

    # 指定早于生效日期的 as_of，应返回 False
    if meta.effective_date is not None:
        early = (meta.effective_date.replace(year=meta.effective_date.year - 1)).isoformat()
        result_early = verify_statute_status(meta.source_id, as_of=early)
        assert isinstance(result_early, StatuteStatusResult)
        assert result_early.is_effective_as_of is False

    # JSON 可序列化
    parsed = json.loads(result.model_dump_json())
    assert "is_effective_as_of" in parsed


def test_verify_statute_status_unknown_source():
    """未知 source_id 返回 current_status=unknown。"""
    result = verify_statute_status("definitely-not-exist-12345")
    assert isinstance(result, StatuteStatusResult)
    assert result.success is True
    assert result.current_status == "unknown"
    assert result.is_effective_as_of is False


# ---------------------------------------------------------------------------
# 4. calculate_legal_deadline
# ---------------------------------------------------------------------------
def test_calculate_legal_deadline_labor_arbitration_one_year():
    """劳动仲裁时效 1 年（365 天）。"""
    result = calculate_legal_deadline("2024-01-01", "labor_arbitration")

    assert isinstance(result, DeadlineResult)
    assert result.tool_name == "calculate_legal_deadline"
    assert result.success is True
    assert result.deadline_days == 365
    # 验算到期日（用 datetime 计算，避免闰年误判）
    from datetime import timedelta

    expected_date = (date.fromisoformat("2024-01-01") + timedelta(days=365)).isoformat()
    assert result.deadline_date == expected_date
    assert isinstance(result.warning, str)
    assert "1 年" in result.warning


def test_calculate_legal_deadline_civil_litigation_three_years():
    """民事诉讼时效 3 年（1095 天）。"""
    result = calculate_legal_deadline("2024-01-01", "civil_litigation")
    assert result.deadline_days == 1095
    assert result.success is True


def test_calculate_legal_deadline_invalid_date():
    """非法日期返回 success=False。"""
    result = calculate_legal_deadline("not-a-date", "labor_arbitration")
    assert result.success is False
    assert result.error


def test_calculate_legal_deadline_invalid_type():
    """不支持的期限类型返回 success=False。"""
    result = calculate_legal_deadline("2024-01-01", "totally_made_up")
    assert result.success is False
    assert "totally_made_up" in result.error


# ---------------------------------------------------------------------------
# 5. calculate_claim_amount
# ---------------------------------------------------------------------------
def test_calculate_claim_amount_economic_compensation():
    """经济补偿计算：N 月工资。"""
    result = calculate_claim_amount(
        claim_type="economic_compensation",
        principal=0.0,
        months=3,
        wage=10000.0,
    )

    assert isinstance(result, ClaimAmountResult)
    assert result.tool_name == "calculate_claim_amount"
    assert result.success is True
    assert result.calculated_amount == 30000.0
    assert "3" in result.formula
    assert "10000" in result.formula
    assert result.breakdown["months"] == 3.0
    assert result.breakdown["wage"] == 10000.0
    # JSON 可序列化
    parsed = json.loads(result.model_dump_json())
    assert parsed["calculated_amount"] == 30000.0


def test_calculate_claim_amount_double_compensation():
    """违法解除赔偿金 = 2N。"""
    result = calculate_claim_amount(
        claim_type="double_compensation",
        principal=0.0,
        months=2,
        wage=5000.0,
    )
    assert result.success is True
    assert result.calculated_amount == 20000.0  # 2 * 2 * 5000


def test_calculate_claim_amount_consumer_triple():
    """消费者三倍赔偿。"""
    result = calculate_claim_amount(
        claim_type="consumer_triple",
        principal=1000.0,
    )
    assert result.success is True
    assert result.calculated_amount == 3000.0


def test_calculate_claim_amount_invalid_type():
    """不支持的 claim_type 返回 success=False。"""
    result = calculate_claim_amount("made_up", principal=100.0)
    assert result.success is False
    assert result.error


# ---------------------------------------------------------------------------
# 6. generate_evidence_checklist
# ---------------------------------------------------------------------------
def test_generate_evidence_checklist_labor_dispute():
    """劳动争议应返回劳动合同等证据。"""
    result = generate_evidence_checklist("劳动争议")

    assert isinstance(result, EvidenceChecklistResult)
    assert result.tool_name == "generate_evidence_checklist"
    assert result.success is True
    assert result.case_type == "劳动争议"
    assert len(result.required_evidence) > 0

    names = [item.name for item in result.required_evidence]
    assert "劳动合同" in names
    assert "工资流水/工资条" in names

    # 默认未持有：missing_evidence 应包含 required 项
    assert len(result.missing_evidence) > 0
    missing_names = [item.name for item in result.missing_evidence]
    assert "劳动合同" in missing_names


def test_generate_evidence_checklist_with_obtained_facts():
    """传入已持有证据应标记 obtained=True。"""
    result = generate_evidence_checklist(
        "劳动争议",
        facts=[{"obtained_evidence": ["劳动合同", "工资流水"]}],  # "工资流水" 模糊匹配 "工资流水/工资条"
    )
    assert result.success is True
    by_name = {item.name: item for item in result.required_evidence}
    assert by_name["劳动合同"].obtained is True
    assert by_name["工资流水/工资条"].obtained is True
    # 未持有的仍为 False
    assert by_name["辞退/解除通知"].obtained is False
    # missing_evidence 不再包含已持有的
    missing_names = [item.name for item in result.missing_evidence]
    assert "劳动合同" not in missing_names
    assert "工资流水/工资条" not in missing_names


def test_generate_evidence_checklist_invalid_case_type():
    """不支持的案类型返回 success=False。"""
    result = generate_evidence_checklist("不存在的案由")
    assert result.success is False
    assert result.error


# ---------------------------------------------------------------------------
# 7. build_case_timeline
# ---------------------------------------------------------------------------
def test_build_case_timeline_sorts_by_date():
    """时间线按日期升序排序。"""
    events = [
        {"date": "2024-03-15", "description": "签订合同", "involved_parties": ["甲", "乙"]},
        {"date": "2024-01-10", "description": "初次接触", "involved_parties": ["甲"]},
        {"date": "2024-06-01", "description": "发生争议", "involved_parties": ["甲", "乙"], "is_key_date": True},
        {"date": "2024-04-20", "description": "交付货物"},
    ]

    result = build_case_timeline(events)

    assert isinstance(result, TimelineResult)
    assert result.tool_name == "build_case_timeline"
    assert result.success is True
    assert len(result.events) == 4

    # 验证排序：应按日期升序
    dates = [item.date for item in result.events]
    assert dates == ["2024-01-10", "2024-03-15", "2024-04-20", "2024-06-01"]
    assert result.earliest_event == "2024-01-10"
    assert result.latest_event == "2024-06-01"

    # 关键日期标记保留
    key_event = next(item for item in result.events if item.date == "2024-06-01")
    assert key_event.is_key_date is True


def test_build_case_timeline_empty_events():
    """空事件列表返回空时间线。"""
    result = build_case_timeline([])
    assert result.success is True
    assert result.events == []
    assert result.earliest_event is None


# ---------------------------------------------------------------------------
# 8. extract_document
# ---------------------------------------------------------------------------
def test_extract_document_markdown(tmp_path: Path):
    """对 .md 文件提取文本应成功。"""
    md = tmp_path / "test.md"
    md.write_text("# 标题\n\n正文内容\n", encoding="utf-8")

    result = extract_document(str(md))

    assert isinstance(result, DocumentExtractResult)
    assert result.tool_name == "extract_document"
    assert result.success is True
    assert result.doc_type == "md"
    assert "标题" in result.text
    assert "正文内容" in result.text
    assert result.full_text_length > 0
    # JSON 可序列化
    parsed = json.loads(result.model_dump_json())
    assert parsed["doc_type"] == "md"


def test_extract_document_txt(tmp_path: Path):
    """对 .txt 文件提取文本应成功。"""
    txt = tmp_path / "test.txt"
    txt.write_text("纯文本内容", encoding="utf-8")

    result = extract_document(str(txt))
    assert result.success is True
    assert result.doc_type == "txt"
    assert "纯文本内容" in result.text


def test_extract_document_nonexistent():
    """不存在的文件应返回 success=False。"""
    result = extract_document("/definitely/not/exist.md")
    assert result.success is False
    assert result.error


# ---------------------------------------------------------------------------
# 9. analyze_contract_clause
# ---------------------------------------------------------------------------
def test_analyze_contract_clause_detects_deposit_nonrefund():
    """检测到「定金不退」风险条款。"""
    text = "乙方支付的定金不退，且甲方保留最终解释权。"
    result = analyze_contract_clause(text, clause_type="违约责任")

    assert isinstance(result, ContractAnalysisResult)
    assert result.tool_name == "analyze_contract_clause"
    assert result.success is True
    assert result.clause_type == "违约责任"
    assert "定金不退" in result.risk_factors
    assert "最终解释权" in result.risk_factors
    assert result.risk_level in ("medium", "high")  # 至少 medium
    assert len(result.suggestions) > 0
    assert result.analyzed_text_length == len(text)


def test_analyze_contract_clause_no_risk():
    """无风险关键词的文本应返回 risk_level=low。"""
    text = "本合同自双方签字盖章之日起生效。"
    result = analyze_contract_clause(text)
    assert result.success is True
    assert result.risk_level == "low"
    assert result.risk_factors == []


def test_analyze_contract_clause_empty_text():
    """空文本应返回 success=False。"""
    result = analyze_contract_clause("")
    assert result.success is False
    assert result.error


# ---------------------------------------------------------------------------
# 10. render_docx
# ---------------------------------------------------------------------------
def test_render_docx_markdown_fallback(tmp_path: Path):
    """render_docx 至少 .md 降级成功（即使 python-docx 不可用）。"""
    md_text = "# 测试标题\n\n正文段落\n\n- 列表项 1\n- 列表项 2\n"
    out = tmp_path / "output.docx"

    result = render_docx(md_text, str(out))

    assert isinstance(result, ExportResult)
    assert result.tool_name == "render_docx"
    assert result.success is True
    assert result.format in ("docx", "md")
    # 文件应实际存在
    assert Path(result.output_path).is_file()
    assert result.file_size > 0


def test_render_docx_empty_text(tmp_path: Path):
    """空 Markdown 文本应返回 success=False。"""
    out = tmp_path / "out.docx"
    result = render_docx("", str(out))
    assert result.success is False
    assert result.error


def test_render_docx_with_template(tmp_path: Path):
    """使用模板时应成功生成（或降级 .md）。"""
    md_text = "# 测试\n\n内容\n"
    out = tmp_path / "out.docx"
    template_path = tmp_path / "tpl.docx"
    # 创建一个空的 docx 作为模板
    try:
        from docx import Document  # type: ignore[import-untyped]

        Document().save(str(template_path))
    except ImportError:
        pytest.skip("python-docx 不可用")

    result = render_docx(md_text, str(out), template=str(template_path))
    assert result.success is True
    assert Path(result.output_path).is_file()


# ---------------------------------------------------------------------------
# 案例工具（顺手覆盖）
# ---------------------------------------------------------------------------
def test_search_cases_returns_pydantic():
    """search_cases 返回 CaseSearchResult。"""
    result = search_cases("劳动")
    assert isinstance(result, CaseSearchResult)
    assert result.tool_name == "search_cases"
    assert result.success is True
    # JSON 可序列化
    json.loads(result.model_dump_json())


def test_get_case_detail_returns_pydantic():
    """get_case_detail 返回 CaseDetailResult。"""
    # 先搜索拿到 case_id
    search = search_cases("劳动")
    if search.results:
        case_id = search.results[0].case_id
        result = get_case_detail(case_id)
        assert isinstance(result, CaseDetailResult)
        assert result.found is True
        assert result.case_type
    else:
        # 知识库不存在时跳过
        pytest.skip("精编知识库 case_patterns.md 不可用")
