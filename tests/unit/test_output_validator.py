"""Output Validator 单元测试（SubTask 14.3）。

覆盖场景（基础校验）：
1. Light 模式：完整结构 → passed=True
2. Light 模式：缺章节 → passed=False, missing_section error
3. Deep 模式：完整结构 → passed=True
4. Deep 模式：缺章节 → passed=False
5. Deep 模式：缺类案参考 / 法规冲突 → warning
6. Document 模式：完整结构 → passed=True
7. 缺风险声明 → missing_risk_disclaimer error
8. 数字概率（百分比）→ numeric_probability error
9. 数字概率（胜诉率）→ numeric_probability error
10. 无效引用（statutes 中不存在）→ invalid_citation error
11. 缺失引用（statutes 为空但输出含引用）→ missing_citation error
12. 空文本 → passed=False
13. 有效引用（statutes 中存在）→ 无 invalid_citation error
14. 返回类型校验
15. 未知模式 → 按 light 校验

覆盖场景（spec 扩展计算字段）：
16. structural_issues：结构缺失问题文本列表
17. citation_issues：引用问题文本列表
18. risk_statement_missing：是否缺少风险声明
19. numeric_probability_detected：是否检测到数字概率
20. suggestions：由 warning 派生的改进建议
21. passed=True 时计算字段均为空 / False
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from lvyan.schemas import Authority
from lvyan.validators.output import (
    OutputValidationResult,
    ValidationError,
    validate_output,
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
    source_id: str | None = None,
) -> Authority:
    return Authority(
        source_id=source_id or f"src-{title}-{article_number}",
        title=title,
        article_number=article_number,
        article_text=article_text,
        authority_level="法律",
        effective_date=date(2021, 1, 1),
        status="effective",
        retrieved_at=datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc),
    )


# 完整的 Light 模式输出
_LIGHT_VALID = """# 日常咨询快答

## 用户目标
咨询合同违约赔偿问题。

## 核心法律结论
本案构成违约，可主张赔偿。

## 关键法条引用
- 《中华人民共和国民法典》第五百七十七条

## 行动建议
1. 收集合同与违约证据。

## 风险声明
以上内容仅供参考，不构成正式法律意见。
"""

# 完整的 Deep 模式输出
_DEEP_VALID = """# 案件深度分析报告

## 案件事实摘要
- 当事人：张三与李四

## 法律关系识别
- 合同纠纷

## 构成要件分析
- 合同关系成立
- 违约行为

## 争议焦点
- 是否构成违约

## 双方主张对比
### 原告主张
- 主张违约赔偿

### 被告主张
- 主张不可抗力

## 证据对应与缺口
- 合同文本对应违约事实

## 裁判倾向
- 裁判倾向：较有利

## 法条引用
- 《中华人民共和国民法典》第五百七十七条

## 类案参考
（暂无类案）

## 法规冲突提示
（暂无冲突）

## 行动建议
1. 及时主张权利。

## 风险声明
以上内容仅供参考，不构成正式法律意见。
"""

# 完整的 Document 模式输出
_DOCUMENT_VALID = """# 民事起诉状

## 当事人信息
- 原告：张三
- 被告：李四

## 事实与理由
- 原告与被告签订合同。

## 法律依据
- 《中华人民共和国民法典》第五百七十七条

## 诉讼请求
- 请求赔偿。

此致
XX人民法院

具状人：张三
日期：2026年7月23日

---
仅供参考，不构成正式法律意见。
"""


# ---------------------------------------------------------------------------
# 1. Light 模式：完整结构 → passed=True
# ---------------------------------------------------------------------------
def test_light_mode_valid():
    """Light 模式完整输出 + 风险声明 + 无数字概率 → passed=True。"""
    statutes = [_make_authority()]
    result = validate_output(_LIGHT_VALID, "light", statutes)
    # 允许有 warning，但不应有 error
    assert result.passed is True


# ---------------------------------------------------------------------------
# 2. Light 模式：缺章节 → passed=False
# ---------------------------------------------------------------------------
def test_light_mode_missing_section():
    """Light 模式缺「行动建议」章节 → missing_section error。"""
    text = """# 日常咨询快答

## 用户目标
咨询违约问题。

## 核心法律结论
构成违约。

## 关键法条引用
- 《中华人民共和国民法典》第五百七十七条

## 风险声明
以上内容仅供参考。
"""
    result = validate_output(text, "light", [_make_authority()])
    assert result.passed is False
    assert any(e.error_type == "missing_section" for e in result.errors)


# ---------------------------------------------------------------------------
# 3. Deep 模式：完整结构 → passed=True
# ---------------------------------------------------------------------------
def test_deep_mode_valid():
    """Deep 模式完整输出 → passed=True。"""
    statutes = [_make_authority()]
    result = validate_output(_DEEP_VALID, "deep", statutes)
    assert result.passed is True


# ---------------------------------------------------------------------------
# 4. Deep 模式：缺章节 → passed=False
# ---------------------------------------------------------------------------
def test_deep_mode_missing_section():
    """Deep 模式缺「构成要件分析」章节 → missing_section error。"""
    text = """# 案件深度分析报告

## 案件事实摘要
- 张三与李四

## 法律关系识别
- 合同纠纷

## 争议焦点
- 是否违约

## 双方主张对比
### 原告主张
- 违约

### 被告主张
- 不可抗力

## 证据对应与缺口
- 合同文本

## 裁判倾向
- 较有利

## 法条引用
- 《中华人民共和国民法典》第五百七十七条

## 行动建议
1. 起诉

## 风险声明
仅供参考。
"""
    result = validate_output(text, "deep", [_make_authority()])
    assert result.passed is False
    assert any(e.error_type == "missing_section" for e in result.errors)


# ---------------------------------------------------------------------------
# 5. Deep 模式：缺类案参考 / 法规冲突 → warning（非 error）
# ---------------------------------------------------------------------------
def test_deep_mode_optional_sections_warning():
    """Deep 模式缺类案参考 / 法规冲突 → warning，不影响 passed。"""
    text = """# 案件深度分析报告

## 案件事实摘要
- 张三

## 法律关系识别
- 合同纠纷

## 构成要件分析
- 违约

## 争议焦点
- 是否违约

## 双方主张对比
### 原告主张
- 违约

### 被告主张
- 不可抗力

## 证据对应与缺口
- 合同

## 裁判倾向
- 较有利

## 法条引用
- 《中华人民共和国民法典》第五百七十七条

## 行动建议
1. 起诉

## 风险声明
仅供参考。
"""
    result = validate_output(text, "deep", [_make_authority()])
    assert result.passed is True
    # 应有 warning 提示缺类案参考 / 法规冲突
    assert any("类案" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 6. Document 模式：完整结构 → passed=True
# ---------------------------------------------------------------------------
def test_document_mode_valid():
    """Document 模式完整输出 → passed=True。"""
    statutes = [_make_authority()]
    result = validate_output(_DOCUMENT_VALID, "document", statutes)
    assert result.passed is True


# ---------------------------------------------------------------------------
# 7. 缺风险声明 → missing_risk_disclaimer error
# ---------------------------------------------------------------------------
def test_missing_risk_disclaimer():
    """输出缺少风险声明 → missing_risk_disclaimer error。"""
    text = """# 日常咨询快答

## 用户目标
咨询违约。

## 核心法律结论
构成违约。

## 关键法条引用
- 《中华人民共和国民法典》第五百七十七条

## 行动建议
1. 起诉。
"""
    result = validate_output(text, "light", [_make_authority()])
    assert result.passed is False
    assert any(e.error_type == "missing_risk_disclaimer" for e in result.errors)


# ---------------------------------------------------------------------------
# 8. 数字概率（百分比）→ numeric_probability error
# ---------------------------------------------------------------------------
def test_numeric_probability_percentage():
    """输出含 85% → numeric_probability error。"""
    text = _LIGHT_VALID + "\n胜诉概率：85%\n"
    result = validate_output(text, "light", [_make_authority()])
    assert result.passed is False
    assert any(e.error_type == "numeric_probability" for e in result.errors)


# ---------------------------------------------------------------------------
# 9. 数字概率（胜诉率）→ numeric_probability error
# ---------------------------------------------------------------------------
def test_numeric_probability_win_rate():
    """输出含「胜诉率80」→ numeric_probability error。"""
    text = (
        _LIGHT_VALID.replace("以上内容仅供参考，不构成正式法律意见。", "")
        + "\n胜诉率80\n仅供参考。\n"
    )
    result = validate_output(text, "light", [_make_authority()])
    assert result.passed is False
    assert any(e.error_type == "numeric_probability" for e in result.errors)


# ---------------------------------------------------------------------------
# 10. 无效引用（statutes 中不存在）→ invalid_citation error
# ---------------------------------------------------------------------------
def test_invalid_citation():
    """输出引用《民法典》第9999条，但 statutes 中只有第五百七十七条 → invalid_citation。"""
    text = _LIGHT_VALID.replace(
        "《中华人民共和国民法典》第五百七十七条",
        "《中华人民共和国民法典》第九千九百九十九条",
    )
    statutes = [_make_authority(article_number="第五百七十七条")]
    result = validate_output(text, "light", statutes)
    assert result.passed is False
    assert any(e.error_type == "invalid_citation" for e in result.errors)


# ---------------------------------------------------------------------------
# 11. 缺失引用（statutes 为空但输出含引用）→ missing_citation error
# ---------------------------------------------------------------------------
def test_missing_citation_empty_statutes():
    """输出含法条引用但 statutes 为空 → missing_citation error。"""
    result = validate_output(_LIGHT_VALID, "light", [])
    assert result.passed is False
    assert any(e.error_type == "missing_citation" for e in result.errors)


# ---------------------------------------------------------------------------
# 12. 空文本 → passed=False
# ---------------------------------------------------------------------------
def test_empty_text():
    """空文本 → passed=False, missing_section error。"""
    result = validate_output("", "light", [])
    assert result.passed is False
    assert any(e.error_type == "missing_section" for e in result.errors)


# ---------------------------------------------------------------------------
# 13. 有效引用（statutes 中存在）→ 无 invalid_citation error
# ---------------------------------------------------------------------------
def test_valid_citation_no_error():
    """输出引用的法规在 statutes 中存在 → 无 invalid_citation / missing_citation error。"""
    statutes = [_make_authority(article_number="第五百七十七条")]
    result = validate_output(_LIGHT_VALID, "light", statutes)
    citation_errors = [
        e for e in result.errors if e.error_type in ("invalid_citation", "missing_citation")
    ]
    assert len(citation_errors) == 0


# ---------------------------------------------------------------------------
# 14. 返回类型校验
# ---------------------------------------------------------------------------
def test_return_type():
    """返回值应为 OutputValidationResult 实例。"""
    result = validate_output(_LIGHT_VALID, "light", [_make_authority()])
    assert isinstance(result, OutputValidationResult)
    assert isinstance(result.passed, bool)
    assert isinstance(result.errors, list)
    assert isinstance(result.warnings, list)
    for err in result.errors:
        assert isinstance(err, ValidationError)


# ---------------------------------------------------------------------------
# 15. 未知模式 → 按 light 校验
# ---------------------------------------------------------------------------
def test_unknown_complexity_falls_back_to_light():
    """未知 complexity 值 → 按 light 模式校验。"""
    result = validate_output(_LIGHT_VALID, "unknown_mode", [_make_authority()])
    # 应按 light 校验，完整 light 输出应通过
    assert result.passed is True


# ===========================================================================
# spec 扩展计算字段测试（structural_issues / citation_issues /
# risk_statement_missing / numeric_probability_detected / suggestions）
# ===========================================================================


# ---------------------------------------------------------------------------
# 16. structural_issues：结构缺失问题文本列表
# ---------------------------------------------------------------------------
def test_structural_issues_missing_section():
    """缺章节 → structural_issues 包含对应缺失章节的文本。"""
    text = """# 日常咨询快答

## 用户目标
咨询违约。

## 核心法律结论
构成违约。

## 风险声明
仅供参考。
"""
    result = validate_output(text, "light", [_make_authority()])
    assert result.passed is False
    # structural_issues 应为非空 list[str]
    assert isinstance(result.structural_issues, list)
    assert len(result.structural_issues) > 0
    # 缺「法律依据」「后续动作」章节
    combined = " ".join(result.structural_issues)
    assert "法律依据" in combined or "后续动作" in combined


def test_structural_issues_empty_when_no_missing():
    """结构完整 → structural_issues 为空列表。"""
    result = validate_output(_LIGHT_VALID, "light", [_make_authority()])
    assert result.passed is True
    assert result.structural_issues == []


# ---------------------------------------------------------------------------
# 17. citation_issues：引用问题文本列表
# ---------------------------------------------------------------------------
def test_citation_issues_invalid_citation():
    """无效引用 → citation_issues 包含 invalid_citation 的文本。"""
    text = _LIGHT_VALID.replace(
        "《中华人民共和国民法典》第五百七十七条",
        "《中华人民共和国民法典》第九千九百九十九条",
    )
    statutes = [_make_authority(article_number="第五百七十七条")]
    result = validate_output(text, "light", statutes)
    assert result.passed is False
    assert isinstance(result.citation_issues, list)
    assert len(result.citation_issues) > 0
    assert any("9999" in issue or "九千九百" in issue for issue in result.citation_issues)


def test_citation_issues_missing_citation():
    """statutes 为空但输出含引用 → citation_issues 包含 missing_citation 的文本。"""
    result = validate_output(_LIGHT_VALID, "light", [])
    assert result.passed is False
    assert len(result.citation_issues) > 0
    assert any("statutes" in issue or "无法核对" in issue for issue in result.citation_issues)


def test_citation_issues_empty_when_valid():
    """引用全部有效 → citation_issues 为空列表。"""
    result = validate_output(_LIGHT_VALID, "light", [_make_authority()])
    assert result.citation_issues == []


# ---------------------------------------------------------------------------
# 18. risk_statement_missing：是否缺少风险声明
# ---------------------------------------------------------------------------
def test_risk_statement_missing_true():
    """缺风险声明 → risk_statement_missing=True。"""
    text = """# 日常咨询快答

## 用户目标
咨询违约。

## 核心法律结论
构成违约。

## 关键法条引用
- 《中华人民共和国民法典》第五百七十七条

## 行动建议
1. 起诉。
"""
    result = validate_output(text, "light", [_make_authority()])
    assert result.risk_statement_missing is True


def test_risk_statement_missing_false():
    """含风险声明 → risk_statement_missing=False。"""
    result = validate_output(_LIGHT_VALID, "light", [_make_authority()])
    assert result.risk_statement_missing is False


# ---------------------------------------------------------------------------
# 19. numeric_probability_detected：是否检测到数字概率
# ---------------------------------------------------------------------------
def test_numeric_probability_detected_true():
    """输出含数字概率 → numeric_probability_detected=True。"""
    text = _LIGHT_VALID + "\n胜诉概率：85%\n"
    result = validate_output(text, "light", [_make_authority()])
    assert result.numeric_probability_detected is True


def test_numeric_probability_detected_false():
    """输出无数字概率 → numeric_probability_detected=False。"""
    result = validate_output(_LIGHT_VALID, "light", [_make_authority()])
    assert result.numeric_probability_detected is False


# ---------------------------------------------------------------------------
# 20. suggestions：由 warning 派生的改进建议
# ---------------------------------------------------------------------------
def test_suggestions_from_warnings():
    """Deep 模式缺类案参考 → suggestions 包含对应 warning。"""
    text = """# 案件深度分析报告

## 案件事实摘要
- 张三

## 法律关系识别
- 合同纠纷

## 构成要件分析
- 违约

## 争议焦点
- 是否违约

## 双方主张对比
### 原告主张
- 违约

### 被告主张
- 不可抗力

## 证据对应与缺口
- 合同

## 裁判倾向
- 较有利

## 法条引用
- 《中华人民共和国民法典》第五百七十七条

## 行动建议
1. 起诉

## 风险声明
仅供参考。
"""
    result = validate_output(text, "deep", [_make_authority()])
    assert result.passed is True
    # 应有 warning 提示缺类案参考 / 法规冲突
    assert len(result.warnings) > 0
    # suggestions 应与 warnings 一致
    assert isinstance(result.suggestions, list)
    assert result.suggestions == result.warnings
    assert any("类案" in s for s in result.suggestions)


def test_suggestions_empty_when_no_warnings():
    """无 warning → suggestions 为空列表。"""
    result = validate_output(_LIGHT_VALID, "light", [_make_authority()])
    assert result.suggestions == []


# ---------------------------------------------------------------------------
# 21. passed=True 时计算字段均为空 / False
# ---------------------------------------------------------------------------
def test_computed_fields_when_passed():
    """校验通过时：structural_issues=[], citation_issues=[], risk_statement_missing=False,
    numeric_probability_detected=False, suggestions=[]。"""
    result = validate_output(_LIGHT_VALID, "light", [_make_authority()])
    assert result.passed is True
    assert result.structural_issues == []
    assert result.citation_issues == []
    assert result.risk_statement_missing is False
    assert result.numeric_probability_detected is False
    assert result.suggestions == []
