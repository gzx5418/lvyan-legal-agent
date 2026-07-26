"""统一 LLM 调用层。

提供对模型网关（SiliconFlow / OpenAI 兼容）的统一封装：
- :func:`chat`：普通对话补全，返回文本。
- :func:`chat_json`：JSON 模式，返回结构化字典。
- :func:`chat_structured`：Pydantic schema 校验 + 一次修复重试（P3-21）。

所有调用自动集成 :mod:`lvyan.observability.tracing` 的成本追踪与 Langfuse 上报。
"""

from __future__ import annotations

from .client import chat, chat_json, chat_structured, llm_available

__all__ = ["chat", "chat_json", "chat_structured", "llm_available"]
