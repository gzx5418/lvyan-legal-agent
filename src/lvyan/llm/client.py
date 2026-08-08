"""统一 LLM client：封装模型网关调用，支持 JSON 模式与 Pydantic 校验。

设计要点
--------
- 模块级 ``httpx.Client`` 缓存，避免每次调用重建连接。
- ``chat``：普通对话补全，返回 ``str | None``（失败返回 None）。
- ``chat_json``：启用 ``response_format=json_object``，解析后返回 dict。
- ``chat_structured``：在 ``chat_json`` 之上加 Pydantic schema 校验 +
  一次修复重试，返回 ``BaseModel`` 实例（P3-21）。
- 自动调用 :func:`lvyan.observability.tracing.record_llm_call` 上报成本与 Langfuse。
- ``llm_available``：快速判断网关是否配置，供调用方决定是否走 LLM 路径。

降级策略
--------
所有调用在网关未配置、网络异常、JSON 解析失败时返回 ``None``，
调用方应回退到规则/模板实现。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

_logger = logging.getLogger("lvyan.llm.client")

# 装饰器返回的函数类型变量
T = TypeVar("T", bound=BaseModel)

# 模块级 httpx client 缓存
_HTTP_CLIENT: Any = None


def _get_http_client() -> Any:
    """获取或创建模块级 httpx.Client。"""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None:
        return _HTTP_CLIENT
    try:
        import httpx  # type: ignore[import-untyped]

        _HTTP_CLIENT = httpx.Client(timeout=60.0)
    except ImportError:
        _logger.debug("httpx 未安装，LLM 调用不可用")
    return _HTTP_CLIENT


def llm_available() -> bool:
    """快速判断 LLM 网关是否已配置且 httpx 可用。"""
    from lvyan.config import settings

    if not settings.model_gateway_url.strip():
        return False
    return _get_http_client() is not None


def _build_headers() -> dict[str, str]:
    """构造请求头。"""
    from lvyan.config import settings

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if settings.model_gateway_api_key:
        headers["Authorization"] = f"Bearer {settings.model_gateway_api_key}"
    return headers


def _record_call(
    model: str,
    prompt: str,
    response: str,
    latency_ms: float,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> None:
    """上报 LLM 调用到 tracing（失败静默忽略）。"""
    try:
        from lvyan.observability.tracing import record_llm_call

        record_llm_call(
            model=model,
            prompt=prompt,
            response=response,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=0.0,  # 实际成本由 tracing 内部按模型估算
        )
    except Exception:  # noqa: BLE001
        pass


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 1000,
    timeout: float = 60.0,
) -> str | None:
    """普通对话补全。

    Args:
        messages: OpenAI 格式消息列表 ``[{"role": "system", "content": "..."}, ...]``。
        model: 模型名；None 时用 ``settings.chat_model``。
        temperature: 采样温度。
        max_tokens: 最大输出 token 数。
        timeout: 请求超时秒数。

    Returns:
        模型输出文本；网关未配置或调用失败返回 ``None``。
    """
    from lvyan.config import settings

    gateway = settings.model_gateway_url.strip()
    if not gateway:
        return None

    client = _get_http_client()
    if client is None:
        return None

    used_model = model or settings.chat_model
    prompt_preview = (messages[-1].get("content", "") if messages else "")[:200]
    t0 = time.monotonic()

    try:
        resp = client.post(
            f"{gateway.rstrip('/')}/v1/chat/completions",
            json={
                "model": used_model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            headers=_build_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        latency_ms = (time.monotonic() - t0) * 1000

        # 提取 token 用量
        usage = data.get("usage", {})
        tokens_in = usage.get("prompt_tokens", 0)
        tokens_out = usage.get("completion_tokens", 0)

        _record_call(used_model, prompt_preview, content[:200], latency_ms, tokens_in, tokens_out)
        return content if content else None
    except Exception as exc:  # noqa: BLE001
        _logger.debug("LLM chat 调用失败 (model=%s): %s", used_model, exc)
        return None


def _strip_json_fences(text: str) -> str:
    """去除 markdown JSON 代码围栏（```json ... ```）。"""
    # 去除 ```json ... ``` 或 ``` ... ```
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


def _extract_json_object(text: str) -> str | None:
    """从可能包含多余文本的响应中提取第一个 JSON 对象。"""
    text = text.strip()
    # 先尝试直接解析
    if text.startswith("{"):
        return text
    # 去 markdown 围栏
    cleaned = _strip_json_fences(text)
    if cleaned.startswith("{"):
        return cleaned
    # 从文本中搜索第一个 { ... } 块
    start = cleaned.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(cleaned)):
        if cleaned[i] == "{":
            depth += 1
        elif cleaned[i] == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start : i + 1]
    return None


def chat_json(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 1500,
    timeout: float = 60.0,
) -> dict[str, Any] | None:
    """JSON 模式对话补全，返回解析后的字典。

    启用 ``response_format={"type": "json_object"}``，并对输出做：
    1. 去除 markdown 围栏
    2. 提取 JSON 对象
    3. ``json.loads`` 解析
    4. 失败时返回 ``None``（调用方应回退到规则实现）

    Args:
        messages: 消息列表（system prompt 应指示输出 JSON）。
        model: 模型名；None 时用 ``settings.chat_model``。
        temperature: 采样温度（JSON 模式建议低温）。
        max_tokens: 最大输出 token 数。
        timeout: 请求超时秒数。

    Returns:
        解析后的字典；失败返回 ``None``。
    """
    from lvyan.config import settings

    gateway = settings.model_gateway_url.strip()
    if not gateway:
        return None

    client = _get_http_client()
    if client is None:
        return None

    used_model = model or settings.chat_model
    prompt_preview = (messages[-1].get("content", "") if messages else "")[:200]
    t0 = time.monotonic()

    try:
        payload: dict[str, Any] = {
            "model": used_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        resp = client.post(
            f"{gateway.rstrip('/')}/v1/chat/completions",
            json=payload,
            headers=_build_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        latency_ms = (time.monotonic() - t0) * 1000

        usage = data.get("usage", {})
        tokens_in = usage.get("prompt_tokens", 0)
        tokens_out = usage.get("completion_tokens", 0)
        _record_call(used_model, prompt_preview, content[:200], latency_ms, tokens_in, tokens_out)

        # 解析 JSON
        json_str = _extract_json_object(content)
        if json_str is None:
            _logger.warning("LLM JSON 输出无法提取对象: %s", content[:200])
            return None
        result = json.loads(json_str)
        if not isinstance(result, dict):
            _logger.warning("LLM JSON 输出不是 dict: %s", type(result))
            return None
        return result
    except Exception as exc:  # noqa: BLE001
        _logger.debug("LLM chat_json 调用失败 (model=%s): %s", used_model, exc)
        return None


def chat_structured(
    messages: list[dict[str, str]],
    response_model: type[T],
    *,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 1500,
    timeout: float = 60.0,
) -> T | None:
    """Pydantic schema 校验的 JSON 模式调用，返回结构化模型实例（P3-21）。

    流程：
      1. 在 system prompt 末尾追加 JSON schema 指引（基于 ``response_model``
         的字段名与类型注释）。
      2. 调 :func:`chat_json` 拿到 dict。
      3. 用 ``response_model.model_validate`` 校验。
      4. 校验失败时构造修复 prompt（附 ValidationError 错误）重试一次。
      5. 仍失败则返回 None，调用方降级到规则路径。

    Args:
        messages: 消息列表（system prompt 应指示输出 JSON）。
        response_model: 期望的 Pydantic 模型类。
        model/temperature/max_tokens/timeout: 见 :func:`chat_json`。

    Returns:
        ``response_model`` 的实例；任何环节失败返回 ``None``。
    """
    schema_hint = _build_schema_hint(response_model)

    # 把 schema 指引追加到 system prompt
    enriched = list(messages)
    if enriched and enriched[0].get("role") == "system":
        enriched[0] = {
            "role": "system",
            "content": enriched[0]["content"] + "\n\n" + schema_hint,
        }
    else:
        enriched.insert(0, {"role": "system", "content": schema_hint})

    result = chat_json(
        enriched,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    if result is None:
        return None

    try:
        return response_model.model_validate(result)
    except ValidationError as exc:
        validation_error = str(exc)
        _logger.warning("Pydantic 校验失败（一次修复重试）：%s", validation_error[:300])

    # 修复重试：把错误反馈给模型
    repair_msg = {
        "role": "user",
        "content": (
            f"上一轮输出未通过 schema 校验：\n{validation_error[:500]}\n"
            "请仅输出符合 schema 的合法 JSON，不要解释。"
        ),
    }
    enriched.append(repair_msg)
    result = chat_json(
        enriched,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    if result is None:
        return None
    try:
        return response_model.model_validate(result)
    except ValidationError as exc:
        _logger.warning("修复重试后仍校验失败：%s", str(exc)[:300])
        return None


def _build_schema_hint(model_cls: type[BaseModel]) -> str:
    """根据 Pydantic 模型类构造 JSON schema 文本指引。"""
    try:
        schema = model_cls.model_json_schema()
        # 仅保留 properties 的 key + type，避免 prompt 过长
        props = schema.get("properties", {})
        lines = ["请输出符合以下 JSON schema 的对象（仅 JSON，不要解释）：", "{"]
        for name, prop in props.items():
            ptype = prop.get("type", "any")
            desc = prop.get("description", "")
            lines.append(f'  "{name}" ({ptype}): {desc}')
        lines.append("}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return "请输出 JSON 对象。"


__all__ = ["chat", "chat_json", "chat_structured", "llm_available"]
