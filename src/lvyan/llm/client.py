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
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

_logger = logging.getLogger("lvyan.llm.client")

__all__ = ["LLMClient", "LLMResponse", "get_llm_client"]


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
                        attempt + 1, self._max_retries, duration_ms, delay, exc,
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

    def _record_metrics(
        self, model: str, response: LLMResponse | None, success: bool
    ) -> None:
        """记录 Prometheus 指标。"""
        try:
            from lvyan.observability.metrics import (
                LLM_CALL_DURATION,
                LLM_CALL_TOTAL,
                LLM_TOKEN_USAGE,
                _PROM_AVAILABLE,
            )

            if not _PROM_AVAILABLE:
                return

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
