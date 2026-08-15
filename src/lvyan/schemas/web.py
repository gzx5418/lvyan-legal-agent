"""联网权威来源的数据模型与域名边界。"""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

__all__ = ["OnlineSource", "is_official_source_url"]


def is_official_source_url(value: str) -> bool:
    """仅接受中国大陆政府、人大、法院和检察院的 HTTPS 页面。"""
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host:
        return False
    return (
        host == "flk.npc.gov.cn"
        or host == "npc.gov.cn"
        or host.endswith(".gov.cn")
        or host == "court.gov.cn"
        or host.endswith(".court.gov.cn")
        or host == "spp.gov.cn"
        or host.endswith(".spp.gov.cn")
    )


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
