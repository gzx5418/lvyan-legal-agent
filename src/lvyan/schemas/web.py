"""联网权威来源的数据模型与域名边界。"""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

__all__ = ["OnlineSource", "is_official_source_url"]


# P1 收紧：原 `host.endswith(".gov.cn")` 放行任意 gov.cn 子域（含区县政务网）。
# 权威法律来源收敛到核心域名后缀集合；需要扩充时在此显式登记。
_OFFICIAL_SOURCE_DOMAINS: tuple[str, ...] = (
    "flk.npc.gov.cn",  # 国家法律法规数据库
    "npc.gov.cn",  # 全国人大
    "www.gov.cn",  # 国务院
    "court.gov.cn",  # 最高人民法院
    "spp.gov.cn",  # 最高人民检察院
    "justice.gov.cn",  # 司法部
)


def is_official_source_url(value: str) -> bool:
    """仅接受核心官方域名的 HTTPS 页面。"""
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in _OFFICIAL_SOURCE_DOMAINS)


class OnlineSource(BaseModel):
    """一条经过官方域名验证的联网参考资料。"""

    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=2_048)
    snippet: str = Field(default="", max_length=800)
    source_name: str = Field(default="官方来源", min_length=1, max_length=120)

    @field_validator("url")
    @classmethod
    def url_must_be_official_https(cls, value: str) -> str:
        if not is_official_source_url(value):
            raise ValueError("联网来源必须为允许的官方 HTTPS 域名")
        return value
