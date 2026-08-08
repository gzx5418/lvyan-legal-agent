"""OpenTelemetry 追踪 + Langfuse 集成 + 成本追踪。

降级策略
--------
- ``OpenTelemetry``：始终通过 ``opentelemetry.trace.get_tracer`` 取得 tracer。
  即便未配置 SDK（生产环境未调用 ``trace.set_tracer_provider``），OTel API 会
  返回无操作 tracer，装饰器照常运行、仅不产出 span —— 不报错。
- ``Langfuse``：仅当环境变量 ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY``
  同时存在且 ``langfuse`` 包可导入时启用；否则 ``record_llm_call`` /
  ``record_evaluation`` 降级为 no-op，仅写 debug 日志。
- 成本追踪（``CostTracker``）为纯内存实现，不依赖任何外部服务。

P2-16 隐私脱敏
--------------
- 默认 ``TRACE_CONTENT=false``：``record_llm_call`` 仅记录 token / latency /
  model / success / error_type / content_hash，**不**上传 prompt / response 原文。
- 显式设置 ``TRACE_CONTENT=true`` 时上传脱敏后的内容（先调
  :func:`lvyan.validators.privacy.redact_privacy`，再截断）。
- 法律案件内容极易含 PII（姓名 / 身份证 / 医疗 / 公司内部信息），内容遥测
  必须显式 opt-in。
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import logging
import os
import threading
from contextvars import ContextVar
from typing import Any, Callable, TypeVar

from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, Field

__all__ = [
    "get_tracer",
    "trace_node",
    "trace_tool",
    "trace_retrieval",
    "record_llm_call",
    "record_evaluation",
    "set_cost_thread",
    "CostSummary",
    "CostTracker",
    "get_cost_summary",
    "is_trace_content_enabled",
    "redact_for_telemetry",
    "content_hash",
]

_logger = logging.getLogger("lvyan.observability.tracing")

# 装饰器返回的函数类型变量
F = TypeVar("F", bound=Callable[..., Any])

# 摘要截断上限
_SUMMARY_MAX_LEN = 200


# ---------------------------------------------------------------------------
# 内容遥测开关与脱敏
# ---------------------------------------------------------------------------
def is_trace_content_enabled() -> bool:
    """是否启用内容遥测（默认 false，需显式 opt-in）。

    通过环境变量 ``TRACE_CONTENT`` 控制：``true`` / ``1`` / ``yes`` / ``on``
    视为启用。生产环境建议保持默认 false，仅记录 token / latency / hash。
    """
    raw = os.getenv("TRACE_CONTENT", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _redact_privacy_safe(text: str) -> str:
    """安全调用隐私脱敏（ validators 未就绪时返回原文）。"""
    if not text:
        return ""
    try:
        from lvyan.validators.privacy import redact_privacy

        return redact_privacy(text).redacted_text
    except Exception:  # noqa: BLE001
        return text


def redact_for_telemetry(text: str) -> str:
    """对要进入遥测的内容做脱敏 + 截断。

    即使 ``TRACE_CONTENT=true``，上传到 Langfuse / OTel 的内容也必须先脱敏。
    """
    if not text:
        return ""
    return _redact_privacy_safe(text)[:_SUMMARY_MAX_LEN]


def content_hash(text: str) -> str:
    """计算内容的短哈希（sha256 前 12 位），用于在不存原文时关联 trace。"""
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Tracer 入口
# ---------------------------------------------------------------------------
def get_tracer(name: str) -> trace.Tracer:
    """返回指定名称的 tracer。

    未配置 OTel SDK 时返回无操作 tracer，调用方无需关心是否已初始化。
    """
    return trace.get_tracer(name)


# ---------------------------------------------------------------------------
# 辅助：输入/输出摘要
# ---------------------------------------------------------------------------
def _summarize(value: Any, max_len: int = _SUMMARY_MAX_LEN) -> str:
    """将任意值转为截断字符串，用于 span 属性，避免泄露过大载荷。"""
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 repr 失败不应影响业务
        return "<unrepresentable>"
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def _input_summary(args: tuple, kwargs: dict) -> str:
    """取首个位置参数或首个关键字参数值作为输入摘要。"""
    if args:
        return _summarize(args[0])
    if kwargs:
        return _summarize(next(iter(kwargs.values())))
    return "<no-input>"


def _make_span(tracer: trace.Tracer, name: str, kind: str, extra: dict[str, Any]):
    """创建并进入一个 span，返回上下文管理器。"""
    span_cm = tracer.start_as_current_span(name)
    span = span_cm.__enter__()
    try:
        span.set_attribute(f"{kind}.name", name)
        for k, v in extra.items():
            try:
                span.set_attribute(k, v)
            except Exception:  # noqa: BLE001 属性设置失败不阻断追踪
                pass
    except Exception:  # noqa: BLE001
        pass
    return span_cm, span


def _finish_span(
    span_cm: Any, span: Any, kind: str, duration_ms: float, exc: BaseException | None
) -> None:
    """收尾 span：记录耗时与异常，退出上下文。"""
    try:
        span.set_attribute(f"{kind}.duration_ms", duration_ms)
    except Exception:  # noqa: BLE001
        pass
    if exc is not None:
        try:
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
        except Exception:  # noqa: BLE001
            pass
    try:
        span_cm.__exit__(type(exc) if exc else None, exc, None)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 装饰器
# ---------------------------------------------------------------------------
def _build_traced(
    func: F,
    kind: str,
    span_name: str,
    tracer_name: str,
    name_attr: tuple[str, str],
    extra_output_attrs: Callable[[Any], dict[str, Any]] | None = None,
) -> F:
    """构造一个被追踪的包装函数（自动适配同步/异步）。

    Args:
        kind: 类别前缀，``node`` / ``tool`` / ``retrieval``，用于 span 属性命名。
        span_name: span 名称。
        tracer_name: 取得 tracer 时使用的名称。
        name_attr: ``(属性名, 属性值)``，如 ``("node.name", "triage")``。
        extra_output_attrs: 可选，根据返回值追加额外属性（如检索结果数）。
    """
    import time as _time

    base_attrs: dict[str, Any] = {name_attr[0]: name_attr[1]}

    def _attrs(args: tuple, kwargs: dict) -> dict[str, Any]:
        attrs = dict(base_attrs)
        attrs[f"{kind}.input_summary"] = _input_summary(args, kwargs)
        return attrs

    def _record_output(span: Any, result: Any) -> None:
        try:
            span.set_attribute(f"{kind}.output_summary", _summarize(result))
            if extra_output_attrs is not None:
                for k, v in extra_output_attrs(result).items():
                    span.set_attribute(k, v)
        except Exception:  # noqa: BLE001
            pass

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def aio_wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = get_tracer(tracer_name)
            span_cm, span = _make_span(tracer, span_name, kind, _attrs(args, kwargs))
            start = _time.perf_counter()
            exc: BaseException | None = None
            try:
                result = await func(*args, **kwargs)
                _record_output(span, result)
                return result
            except BaseException as e:  # noqa: BLE001 需捕获 BaseException 以记录
                exc = e
                raise
            finally:
                _finish_span(span_cm, span, kind, (_time.perf_counter() - start) * 1000.0, exc)

        return aio_wrapper  # type: ignore[return-value]

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        tracer = get_tracer(tracer_name)
        span_cm, span = _make_span(tracer, span_name, kind, _attrs(args, kwargs))
        start = _time.perf_counter()
        exc: BaseException | None = None
        try:
            result = func(*args, **kwargs)
            _record_output(span, result)
            return result
        except BaseException as e:  # noqa: BLE001
            exc = e
            raise
        finally:
            _finish_span(span_cm, span, kind, (_time.perf_counter() - start) * 1000.0, exc)

    return wrapper  # type: ignore[return-value]


def _retrieval_extra(result: Any) -> dict[str, Any]:
    """检索装饰器额外属性：结果命中数。"""
    return {"retrieval.result_count": len(result) if isinstance(result, (list, tuple)) else 0}


def trace_node(node_name: str) -> Callable[[F], F]:
    """节点函数追踪装饰器：记录节点名、输入摘要、输出摘要、耗时、异常。"""

    def decorator(func: F) -> F:
        return _build_traced(func, "node", node_name, "lvyan.node", ("node.name", node_name))

    return decorator


def trace_tool(tool_name: str) -> Callable[[F], F]:
    """工具函数追踪装饰器：记录工具名、输入摘要、输出摘要、耗时、异常。"""

    def decorator(func: F) -> F:
        return _build_traced(func, "tool", tool_name, "lvyan.tool", ("tool.name", tool_name))

    return decorator


def trace_retrieval(strategy: str) -> Callable[[F], F]:
    """检索函数追踪装饰器：记录检索策略、输入摘要、输出摘要、耗时、异常。"""

    def decorator(func: F) -> F:
        return _build_traced(
            func,
            "retrieval",
            strategy,
            "lvyan.retrieval",
            ("retrieval.strategy", strategy),
            extra_output_attrs=_retrieval_extra,
        )

    return decorator


# ---------------------------------------------------------------------------
# 成本追踪
# ---------------------------------------------------------------------------
class CostSummary(BaseModel):
    """单个会话线程的累计成本摘要。"""

    thread_id: str
    total_tokens_in: int = Field(default=0, ge=0)
    total_tokens_out: int = Field(default=0, ge=0)
    total_cost: float = Field(default=0.0, ge=0.0)


class CostTracker:
    """按 ``thread_id`` 累计 token 数与成本的内存追踪器（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, float]] = {}

    def add(self, thread_id: str, tokens_in: int, tokens_out: int, cost: float) -> None:
        """累加一次模型调用的 token 与成本。"""
        with self._lock:
            entry = self._data.setdefault(thread_id, {"in": 0, "out": 0, "cost": 0.0})
            entry["in"] += tokens_in
            entry["out"] += tokens_out
            entry["cost"] += cost

    def get(self, thread_id: str) -> CostSummary:
        """返回指定线程的累计摘要；未知线程返回零值。"""
        with self._lock:
            entry = self._data.get(thread_id)
            if entry is None:
                return CostSummary(thread_id=thread_id)
            return CostSummary(
                thread_id=thread_id,
                total_tokens_in=int(entry["in"]),
                total_tokens_out=int(entry["out"]),
                total_cost=float(entry["cost"]),
            )

    def reset(self, thread_id: str | None = None) -> None:
        """清除指定线程或全部线程的累计记录。"""
        with self._lock:
            if thread_id is None:
                self._data.clear()
            else:
                self._data.pop(thread_id, None)


# 全局成本追踪器单例
_global_cost_tracker = CostTracker()

# 关联「当前正在运行的线程」与成本累计的 contextvar
_cost_thread_var: ContextVar[str | None] = ContextVar("lvyan_cost_thread", default=None)


def set_cost_thread(thread_id: str | None) -> None:
    """设置当前异步上下文关联的 ``thread_id``，供 :func:`record_llm_call` 计入成本。

    传入 ``None`` 清除关联。
    """
    _cost_thread_var.set(thread_id)


def get_cost_summary(thread_id: str) -> CostSummary:
    """返回全局成本追踪器中指定线程的累计摘要。"""
    return _global_cost_tracker.get(thread_id)


# ---------------------------------------------------------------------------
# Langfuse 集成（可选）
# ---------------------------------------------------------------------------
# 模块级缓存的 Langfuse 客户端；None 表示未启用。外部可 monkeypatch 以禁用。
_langfuse_client: Any = None
_langfuse_init_attempted = False


def _ensure_langfuse() -> Any:
    """惰性初始化 Langfuse 客户端；未配置时返回 ``None``。"""
    global _langfuse_client, _langfuse_init_attempted
    if _langfuse_init_attempted:
        return _langfuse_client
    _langfuse_init_attempted = True

    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    if not public_key or not secret_key:
        _logger.debug("Langfuse 未配置（缺少 LANGFUSE_PUBLIC_KEY/SECRET_KEY），降级为 no-op")
        return None

    try:
        from langfuse import Langfuse  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 langfuse 未安装或导入失败需降级
        _logger.debug("langfuse 包不可用（%s），降级为 no-op", exc)
        return None

    try:
        _langfuse_client = Langfuse(public_key=public_key, secret_key=secret_key)
    except Exception as exc:  # noqa: BLE001 初始化失败降级
        _logger.debug("Langfuse 初始化失败（%s），降级为 no-op", exc)
        _langfuse_client = None
    return _langfuse_client


def record_llm_call(
    model: str,
    prompt: str,
    response: str,
    tokens_in: int,
    tokens_out: int,
    cost: float,
) -> None:
    """记录一次模型调用到 Langfuse，并计入成本追踪。

    P2-16：默认 ``TRACE_CONTENT=false`` 时，Langfuse 仅记录 token / model /
    content_hash，**不**上传 prompt / response 原文；显式 opt-in 时上传
    脱敏后的内容。成本追踪始终执行（不涉及内容）。
    """
    # 1) 成本追踪（始终执行，不涉及内容）
    thread_id = _cost_thread_var.get()
    if thread_id:
        _global_cost_tracker.add(thread_id, tokens_in, tokens_out, cost)

    # 2) Langfuse 上报（可选）
    client = _ensure_langfuse()
    if client is None:
        return
    try:
        trace_obj = client.trace(id=thread_id) if thread_id else client.trace()

        # P2-16：根据 TRACE_CONTENT 开关决定是否上传内容
        if is_trace_content_enabled():
            # opt-in：上传脱敏后的内容
            safe_prompt = redact_for_telemetry(prompt)
            safe_response = redact_for_telemetry(response)
            trace_obj.generation(
                name=model,
                model=model,
                input=safe_prompt,
                output=safe_response,
                usage={"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
                metadata={
                    "cost": cost,
                    "prompt_hash": content_hash(prompt),
                    "response_hash": content_hash(response),
                },
            )
        else:
            # 默认：只记录 hash + token + model，不传原文
            trace_obj.generation(
                name=model,
                model=model,
                input=None,
                output=None,
                usage={"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
                metadata={
                    "cost": cost,
                    "prompt_hash": content_hash(prompt),
                    "response_hash": content_hash(response),
                },
            )
    except Exception as exc:  # noqa: BLE001 上报失败不阻断业务
        _logger.debug("Langfuse record_llm_call 失败：%s", exc)


def record_evaluation(score_name: str, score_value: float, comment: str = "") -> None:
    """记录一次评测分数到 Langfuse；未启用时降级为 no-op。"""
    client = _ensure_langfuse()
    if client is None:
        return
    try:
        trace_obj = client.trace()
        trace_obj.score(name=score_name, value=score_value, comment=comment)
    except Exception as exc:  # noqa: BLE001 上报失败不阻断业务
        _logger.debug("Langfuse record_evaluation 失败：%s", exc)
