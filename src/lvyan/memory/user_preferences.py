"""长期记忆：用户偏好（跨会话）。

**关键约束**：长期记忆**只保存用户偏好**，绝不保存身份证号 / 合同原文 / 病历 /
银行流水 / 证据 / 完整聊天记录。``sanitize_preference`` 用于在落盘前过滤任何
误入偏好字典的敏感信息字段。

持久化到 ``AGENT/knowledge/manifests/user_prefs/{user_id}.json``。
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..config import AGENT_DIR

# ---------------------------------------------------------------------------
# 持久化根目录：AGENT/knowledge/manifests/user_prefs/
# ---------------------------------------------------------------------------
_DEFAULT_BASE = AGENT_DIR / "knowledge" / "manifests"
_USER_PREFS_DIR = Path(os.getenv("MANIFESTS_DIR", str(_DEFAULT_BASE))) / "user_prefs"

_LOCK = threading.RLock()

# ---------------------------------------------------------------------------
# 敏感信息检测正则
# ---------------------------------------------------------------------------
# 18 位身份证号（最后一位为数字或 X）：6 位地区码 + 8 位生日 + 3 位顺序 + 1 位校验
_ID_CARD_RE = re.compile(r"\b\d{17}[\dXx]\b")
# 银行卡号：16-19 位连续数字
_BANK_CARD_RE = re.compile(r"\b\d{16,19}\b")
# 手机号（中国大陆 11 位）
_PHONE_RE = re.compile(r"\b1[3-9]\d{9}\b")

# 偏好字段名黑名单：任何含这些关键字的字段直接丢弃
_SENSITIVE_KEY_KEYWORDS = (
    "id_card",
    "idcard",
    "id_number",
    "identity",
    "身份证",
    "bank_card",
    "bankcard",
    "bank_account",
    "card_number",
    "银行卡",
    "phone",
    "mobile",
    "电话",
    "手机",
    "password",
    "passwd",
    "token",
    "secret",
    "密码",
    "medical",
    "病历",
    "病史",
    "contract_text",
    "合同原文",
    "合同内容",
    "evidence",
    "证据",
    "chat_history",
    "聊天记录",
)

# 已知允许保留的字段白名单（与 UserPreference 字段对应）
_ALLOWED_FIELDS = {
    "user_id",
    "response_style",
    "prefer_depth",
    "preferred_doc_format",
    "language",
    "created_at",
    "updated_at",
}


class UserPreference(BaseModel):
    """单个用户的偏好模型。"""

    user_id: str
    response_style: str = Field(default="brief")  # "brief" | "detailed"
    prefer_depth: str = Field(default="light")  # "light" | "deep"
    preferred_doc_format: str = Field(default="md")  # "docx" | "md"
    language: str = Field(default="zh")  # "zh" | "en"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def sanitize_preference(pref: dict) -> dict:
    """过滤偏好字典中的敏感信息。

    策略（双重保险）：
    1. 白名单：仅保留 ``_ALLOWED_FIELDS`` 中的字段，丢弃任何未知字段
       （防止调用方误塞入合同原文、病历等大块敏感数据）。
    2. 黑名单：若白名单字段值中仍匹配到身份证号 / 银行卡号 / 手机号正则，
       则将该字段值置空（不抛错，便于在审计日志中观察异常写入尝试）。

    返回过滤后的新字典（不修改入参）。
    """
    cleaned: dict[str, Any] = {}
    for key, value in pref.items():
        # 白名单过滤
        if key not in _ALLOWED_FIELDS:
            continue
        # 字段名黑名单二次保险（理论上白名单已排除，此处冗余防御）
        key_lower = str(key).lower()
        if any(kw in key_lower for kw in _SENSITIVE_KEY_KEYWORDS):
            continue
        # 值层面的正则扫描：仅对字符串值做敏感信息检测
        if isinstance(value, str):
            if _ID_CARD_RE.search(value) or _BANK_CARD_RE.search(value) or _PHONE_RE.search(value):
                cleaned[key] = ""
                continue
        cleaned[key] = value
    return cleaned


class UserPreferences:
    """用户偏好的读写入口。"""

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = Path(base_dir) if base_dir is not None else _USER_PREFS_DIR
        self._base_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    def get(self, user_id: str) -> UserPreference:
        """获取用户偏好；不存在时返回默认偏好（已落盘）。"""
        path = self._path_of(user_id)
        with _LOCK:
            if path.exists():
                with open(path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                payload = sanitize_preference(payload)
                return UserPreference.model_validate(payload)
        # 不存在 → 返回默认并持久化，保证后续 get/set 行为一致
        pref = UserPreference(user_id=user_id)
        self._write(user_id, pref)
        return pref

    def set(self, user_id: str, pref: UserPreference) -> None:
        """整体覆盖设置用户偏好。

        会强制 ``pref.user_id = user_id``，并对落盘内容做 sanitize。
        """
        # 确保 user_id 一致
        pref = pref.model_copy(
            update={"user_id": user_id, "updated_at": datetime.now(timezone.utc)}
        )
        self._write(user_id, pref)

    def update(self, user_id: str, **kwargs: Any) -> None:
        """部分更新用户偏好。

        先读取现有偏好，合并 kwargs 后写回；kwargs 中不允许的字段会被
        ``sanitize_preference`` 过滤掉。
        """
        existing = self.get(user_id)
        merged = existing.model_dump(mode="json")
        merged.update(kwargs)
        merged["updated_at"] = datetime.now(timezone.utc).isoformat()
        merged = sanitize_preference(merged)
        pref = UserPreference.model_validate(merged)
        self._write(user_id, pref)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _write(self, user_id: str, pref: UserPreference) -> None:
        path = self._path_of(user_id)
        # 落盘前再过一次 sanitize，作为最终保险
        payload = sanitize_preference(pref.model_dump(mode="json"))
        data = json.dumps(payload, ensure_ascii=False, indent=2)
        with _LOCK:
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)

    def _path_of(self, user_id: str) -> Path:
        safe = user_id.replace(os.sep, "_").replace("/", "_").replace("\\", "_")
        if safe in ("", ".", ".."):
            safe = "_invalid_"
        return self._base_dir / f"{safe}.json"


__all__ = ["UserPreferences", "UserPreference", "sanitize_preference"]
