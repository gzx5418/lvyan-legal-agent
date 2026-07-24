"""校验层：引用校验、法规效力状态校验、语义接地校验、隐私脱敏、输出结构校验。

子模块
------
- :mod:`lvyan.validators.citation`：法条引用存在性 / 内容匹配 / 状态校验
- :mod:`lvyan.validators.authority_status`：法规版本有效性校验
- :mod:`lvyan.validators.grounding`：引用语义接地校验
- :mod:`lvyan.validators.privacy`：隐私脱敏（身份证号/银行卡号/手机号/邮箱/病历/住址）
- :mod:`lvyan.validators.output`：输出结构 / 引用 / 风险声明 / 数字概率校验
- :mod:`lvyan.validators.prompt_injection`：提示注入检测（忽略指令/系统覆盖/角色切换/HTML 注释）
"""

from __future__ import annotations

from .authority_status import (
    AuthorityStatusIssue,
    AuthorityStatusReport,
    validate_authority_status,
)
from .citation import (
    CitationIssue,
    CitationValidationReport,
    validate_citations,
)
from .grounding import (
    GroundingIssue,
    GroundingReport,
    validate_grounding,
)
from .output import (
    OutputValidationResult,
    ValidationError,
    validate_output,
)
from .privacy import (
    PrivacyRedactionResult,
    SanitizedItem,
    SanitizedItemType,
    redact_privacy,
    sanitize_privacy,
)
from .prompt_injection import (
    INJECTION_PATTERN_NAMES,
    InjectionDetectionResult,
    SecurityEvalReport,
    detect_prompt_injection,
)

__all__ = [
    # citation.py
    "CitationIssue",
    "CitationValidationReport",
    "validate_citations",
    # authority_status.py
    "AuthorityStatusIssue",
    "AuthorityStatusReport",
    "validate_authority_status",
    # grounding.py
    "GroundingIssue",
    "GroundingReport",
    "validate_grounding",
    # privacy.py
    "PrivacyRedactionResult",
    "redact_privacy",
    "SanitizedItem",
    "SanitizedItemType",
    "sanitize_privacy",
    # output.py
    "ValidationError",
    "OutputValidationResult",
    "validate_output",
    # prompt_injection.py
    "INJECTION_PATTERN_NAMES",
    "InjectionDetectionResult",
    "SecurityEvalReport",
    "detect_prompt_injection",
]
