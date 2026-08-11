"""统一 LLM 客户端：支持多模型、自动重试、指标收集、并发控制。

设计目标
--------
1. 单一入口点：所有 LLM 调用走 `LLMClient.invoke()` / `LLMClient.ainvoke()`
2. 自动重试：429/5xx 指数退避重试（最多 3 次）
3. 并发控制：通过 `InstrumentedSemaphore` 限制同时请求数
4. 可观测性：自动记录延迟、token 用量、错误率到 Prometheus
5. 模型路由：根据任务类型选择合适模型（chat/embedding/reranker/vision）

用法
----
    from lvyan.llm.client import get_llm_client

    client = get_llm_client()
    response = await client.ainvoke(
        messages=[{"role": "user", "content": "..."}],
        model="chat",  # 或具体模型名
        temperature=0.1,
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence, TypeVar

from pydantic import BaseModel, ValidationError

_logger = logging.getLogger("lvyan.llm.client")

__all__ = [
    "LLMClient",
    "LLMResponse",
    "get_llm_client",
    "chat",
    "chat_json",
    "chat_structured",
    "llm_available",
]

T = TypeVar("T", bound=BaseModel)


@dataclass
class LLMResponse:
    """LLM 调用响应。"""

    content: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: float = 0.0
    raw: Any = field(default=None, repr=False)


class LLMClient:
    """统一 LLM 客户端。

    内部使用 LangChain ChatModel 或 httpx 直接调用网关。
    """

    def __init__(
        self,
        gateway_url: str | None = None,
        api_key: str | None = None,
        default_chat_model: str | None = None,
        max_retries: int = 3,
        timeout: float = 120.0,
    ) -> None:
        self._gateway_url = gateway_url or os.getenv("MODEL_GATEWAY_URL", "")
        self._api_key = api_key or os.getenv("MODEL_GATEWAY_API_KEY", "")
        self._default_chat_model = default_chat_model or os.getenv(
            "CHAT_MODEL", "Qwen/Qwen2.5-7B-Instruct"
        )
        self._max_retries = max_retries
        self._timeout = timeout

    def _resolve_model(self, model: str | None) -> str:
        """解析模型别名到实际模型名。"""
        aliases = {
            "chat": os.getenv("CHAT_MODEL", self._default_chat_model),
            "embedding": os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"),
            "reranker": os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3"),
            "vision": os.getenv("VISION_MODEL", "Qwen/Qwen3-VL-8B-Instruct"),
        }
        if model and model.lower() in aliases:
            return aliases[model.lower()]
        return model or self._default_chat_model

    async def ainvoke(
        self,
        messages: Sequence[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """异步调用 LLM（带重试、并发控制、指标）。"""
        resolved_model = self._resolve_model(model)

        # 并发控制
        from lvyan.infra.concurrency import get_llm_semaphore

        sem = get_llm_semaphore()

        async with sem:
            return await self._invoke_with_retry(
                messages=messages,
                model=resolved_model,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )

    async def _invoke_with_retry(
        self,
        messages: Sequence[dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int | None,
        **kwargs: Any,
    ) -> LLMResponse:
        """带指数退避重试的实际调用。"""
        last_exc: Exception | None = None

        for attempt in range(self._max_retries):
            start = time.perf_counter()
            try:
                response = await self._call_gateway(
                    messages=messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs,
                )
                duration_ms = (time.perf_counter() - start) * 1000

                result = LLMResponse(
                    content=response.get("content", ""),
                    model=model,
                    input_tokens=response.get("input_tokens", 0),
                    output_tokens=response.get("output_tokens", 0),
                    duration_ms=duration_ms,
                    raw=response,
                )

                # 记录指标
                self._record_metrics(model, result, success=True)
                return result

            except Exception as exc:  # noqa: BLE001 boundary-exception: 重试逻辑
                last_exc = exc
                duration_ms = (time.perf_counter() - start) * 1000
                self._record_metrics(model, None, success=False)

                if not self._is_retryable(exc):
                    raise

                if attempt < self._max_retries - 1:
                    delay = (2**attempt) * 1.0  # 1s, 2s, 4s
                    _logger.warning(
                        "LLM 调用失败 (attempt %d/%d), %.0fms, 重试 in %.1fs: %s",
                        attempt + 1,
                        self._max_retries,
                        duration_ms,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)

        raise RuntimeError(
            f"LLM 调用在 {self._max_retries} 次重试后仍失败: {last_exc}"
        ) from last_exc

    async def _call_gateway(
        self,
        messages: Sequence[dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int | None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """调用模型网关 (OpenAI 兼容接口)。"""
        if not self._gateway_url:
            raise RuntimeError("MODEL_GATEWAY_URL 未配置，无法调用 LLM")

        import httpx

        url = f"{self._gateway_url.rstrip('/')}/v1/chat/completions"
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        payload.update(kwargs)

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)

            if resp.status_code == 429:
                raise _RetryableError(f"Rate limited (429): {resp.text[:200]}")
            if resp.status_code >= 500:
                raise _RetryableError(f"Server error ({resp.status_code}): {resp.text[:200]}")
            if resp.status_code != 200:
                raise RuntimeError(f"LLM API error ({resp.status_code}): {resp.text[:500]}")

            data = resp.json()
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            usage = data.get("usage", {})

            return {
                "content": message.get("content", ""),
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "finish_reason": choice.get("finish_reason"),
                "raw": data,
            }

    def _is_retryable(self, exc: Exception) -> bool:
        """判断异常是否可重试。"""
        if isinstance(exc, _RetryableError):
            return True
        # httpx 超时/连接错误
        try:
            import httpx

            if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
                return True
        except ImportError:
            pass
        return False

    def _record_metrics(self, model: str, response: LLMResponse | None, success: bool) -> None:
        """记录 Prometheus 指标并把成功调用计入 CostTracker。"""
        try:
            from lvyan.observability.metrics import (
                LLM_CALL_DURATION,
                LLM_CALL_TOTAL,
                LLM_TOKEN_USAGE,
                _PROM_AVAILABLE,
            )

            if _PROM_AVAILABLE:
                status = "success" if success else "error"
                LLM_CALL_TOTAL.labels(model=model, operation="chat", status=status).inc()

                if response:
                    LLM_CALL_DURATION.labels(model=model, operation="chat").observe(
                        response.duration_ms / 1000.0
                    )
                    if response.input_tokens:
                        LLM_TOKEN_USAGE.labels(model=model, direction="input").inc(
                            response.input_tokens
                        )
                    if response.output_tokens:
                        LLM_TOKEN_USAGE.labels(model=model, direction="output").inc(
                            response.output_tokens
                        )
        except ImportError:
            pass

        # P0-10：成功调用计入 CostTracker（按 model 单价表估算 USD）。
        # record_llm_call 内部会读取当前 cost thread contextvar 并累加；
        # 失败或无 cost thread 时不会累加（归属安全）。
        if success and response:
            try:
                from lvyan.observability.tracing import record_llm_call

                cost = _estimate_cost_usd(
                    model,
                    response.input_tokens or 0,
                    response.output_tokens or 0,
                )
                record_llm_call(
                    model=model,
                    prompt="",  # content 已在 LLMResponse 中，cost 路径不消费
                    response="",
                    tokens_in=response.input_tokens or 0,
                    tokens_out=response.output_tokens or 0,
                    cost=cost,
                )
            except Exception:  # noqa: BLE001 - 指标/成本上报不影响主链
                _logger.debug("CostTracker 上报失败（已忽略）", exc_info=True)


# ---------------------------------------------------------------------------
# 成本估算：按 model 名匹配单价表（每 1M tokens 的 USD 价格）
# ---------------------------------------------------------------------------
# 单价来源于各模型官方定价（2026-08）；未列出的模型按 0 计入，避免高估。
# 环境变量 LLM_PRICE_TABLE 可覆盖：格式 "model:in_price,out_price;..."
_MODEL_PRICES: dict[str, tuple[float, float]] = {
    "deepseek-ai/deepseek-v4-flash": (0.14, 0.28),
    "deepseek-ai/deepseek-v3": (0.27, 1.10),
    "qwen/qwen2.5-7b-instruct": (0.05, 0.10),
    "qwen/qwen3-vl-8b-instruct": (0.07, 0.14),
    "baai/bge-m3": (0.01, 0.0),
    "baai/bge-reranker-v2-m3": (0.01, 0.0),
}


def _estimate_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """按 model 单价表估算单次调用的 USD 成本。

    匹配策略：精确 -> 大小写不敏感 -> 前缀子串。未匹配返回 0.0。
    环境变量 ``LLM_PRICE_TABLE`` 可在运行时覆盖（格式 ``model:in,out;...``）。
    """
    prices = _MODEL_PRICES

    # 解析环境变量覆盖
    import os

    env_table = os.getenv("LLM_PRICE_TABLE", "").strip()
    if env_table:
        parsed: dict[str, tuple[float, float]] = {}
        for entry in env_table.split(";"):
            entry = entry.strip()
            if not entry or ":" not in entry:
                continue
            name, rest = entry.split(":", 1)
            parts = rest.split(",")
            if len(parts) == 2:
                try:
                    parsed[name.strip().lower()] = (float(parts[0]), float(parts[1]))
                except ValueError:
                    pass
        if parsed:
            prices = {**_MODEL_PRICES, **parsed}

    model_lower = model.lower()

    # 精确匹配
    if model_lower in prices:
        in_price, out_price = prices[model_lower]
        return (tokens_in / 1_000_000) * in_price + (tokens_out / 1_000_000) * out_price

    # 前缀子串匹配（处理版本后缀差异）
    for key, (in_price, out_price) in prices.items():
        if key in model_lower:
            return (tokens_in / 1_000_000) * in_price + (tokens_out / 1_000_000) * out_price

    return 0.0


class _RetryableError(Exception):
    """标记可重试的错误。"""

    pass


# 全局单例
_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """获取全局 LLM 客户端单例。"""
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


# ---------------------------------------------------------------------------
# 同步兼容接口
# ---------------------------------------------------------------------------
# 节点仍有同步实现，且公共 API 过去已暴露 chat/chat_json/chat_structured。保留这些
# 小型适配器以维持兼容；新异步节点应直接使用 LLMClient.ainvoke()。


def llm_available() -> bool:
    """返回模型网关是否已配置，供同步节点决定是否走 LLM 路径。"""
    from lvyan.config import settings

    return bool(os.getenv("MODEL_GATEWAY_URL", settings.model_gateway_url).strip())


def _legacy_request(
    messages: list[dict[str, str]],
    *,
    model: str | None,
    temperature: float,
    max_tokens: int,
    timeout: float,
    response_format: dict[str, str] | None = None,
) -> str | None:
    """兼容同步节点的 OpenAI 网关请求；失败由调用方降级到规则路径。"""
    from lvyan.config import settings

    gateway = os.getenv("MODEL_GATEWAY_URL", settings.model_gateway_url).strip()
    if not gateway:
        return None

    used_model = model or os.getenv("CHAT_MODEL", settings.chat_model)
    api_key = os.getenv("MODEL_GATEWAY_API_KEY", settings.model_gateway_api_key)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload: dict[str, Any] = {
        "model": used_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format is not None:
        payload["response_format"] = response_format

    try:
        import httpx
    except ImportError:
        return None

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{gateway.rstrip('/')}/v1/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
        content = data["choices"][0]["message"]["content"]
        return content.strip() if isinstance(content, str) and content.strip() else None
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        _logger.debug("兼容 LLM 调用失败 (model=%s): %s", used_model, exc)
        return None


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 1000,
    timeout: float = 60.0,
) -> str | None:
    """同步文本补全兼容接口。"""
    return _legacy_request(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )


def _extract_json_object(text: str) -> str | None:
    """从可能带 Markdown 围栏或解释文字的响应中截取第一个 JSON 对象。"""
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*\n?(.*?)```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    start = cleaned.find("{")
    if start < 0:
        return None
    depth = 0
    for index, char in enumerate(cleaned[start:], start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start : index + 1]
    return None


def chat_json(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 1500,
    timeout: float = 60.0,
) -> dict[str, Any] | None:
    """同步 JSON 补全兼容接口。"""
    content = _legacy_request(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        response_format={"type": "json_object"},
    )
    if content is None:
        return None
    json_text = _extract_json_object(content)
    if json_text is None:
        return None
    try:
        result = json.loads(json_text)
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


def chat_structured(
    messages: list[dict[str, str]],
    response_model: type[T],
    *,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 1500,
    timeout: float = 60.0,
) -> T | None:
    """Pydantic 校验兼容接口；无效输出带校验错误重试一次。"""
    result = chat_json(
        messages,
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
        repair_messages = [
            *messages,
            {
                "role": "user",
                "content": f"输出未通过 schema 校验：{exc}. 请只返回合法 JSON。",
            },
        ]
        repaired = chat_json(
            repair_messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        if repaired is None:
            return None
        try:
            return response_model.model_validate(repaired)
        except ValidationError:
            return None
