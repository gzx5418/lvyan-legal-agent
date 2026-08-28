"""OpenTelemetry 追踪 + Langfuse 集成 + 成本追踪。

降级策略
--------
- ``OpenTelemetry``：始终通过 ``opentelemetry.trace.get_tracer`` 取得 tracer。
  即便未配置 SDK（生产环境未调用 ``trace.set_tracer_provider``），OTel API 会
  返回无操作 tracer，装饰器照常运行、仅不产出 span —— 不报错。
- ``Langfuse``：仅当环境变量 ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY``
  同时存在且 ``langfuse`` 包可导入时启用；否则 ``record_llm_call`` /
  ``record_evaluation`` 降级为 no-op，写 debug 日志。**上报失败**（如 SDK
  版本不兼容、服务不可达）会以 warning 级记录——同类错误仅告警一次，避免
  刷屏，同时保证遥测静默丢失时可在日志中发现。
- ``Langfuse`` SDK 必须为 v2（``langfuse>=2.0,<3.0``）：本模块使用 v2 的
  ``client.trace(id=...)`` / ``trace_obj.generation()`` / ``trace_obj.score()``
  API，与 docker-compose 的 ``langfuse/langfuse:2`` 服务对齐；v3+ 已移除这些
  方法。自建实例通过 ``LANGFUSE_HOST`` 指定（compose 默认映射到 3000 端口）。
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
import reprlib
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

# 受限 repr 实例：限制递归层级与容器/字符串规模，避免先把整个 CaseState
# 序列化成超大字符串再截断的开销（每个被追踪节点都会调用一次）。
# reprlib.Repr 的配置属性在 repr 过程中只读，实例可全局共享（线程安全）。
_REPR_LIMITS = reprlib.Repr()
_REPR_LIMITS.maxlevel = 2  # 递归深度（CaseState → 列表 → 元素即止）
_REPR_LIMITS.maxlist = 3
_REPR_LIMITS.maxtuple = 3
_REPR_LIMITS.maxdict = 3
_REPR_LIMITS.maxset = 3
_REPR_LIMITS.maxfrozenset = 3
_REPR_LIMITS.maxstring = 60
_REPR_LIMITS.maxother = 60


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
    """将任意值转为受限 repr 字符串，用于 span 属性，避免泄露过大载荷。

    使用 :mod:`reprlib` 受限实例（见 ``_REPR_LIMITS``）在 repr **过程中**就
    限制递归层级与容器/字符串规模，而非先生成完整大字符串再截断——节点首参
    往往是整个 CaseState，后者的每节点序列化开销不可接受。
    """
    try:
        text = _REPR_LIMITS.repr(value)
    except Exception:  # noqa: BLE001 repr 失败不应影响业务
        return "<unrepresentable>"
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def _input_summary(args: tuple, kwargs: dict) -> str:
    """取首个位置参数或首个关键字参数值作为输入摘要（受限 repr，见 _summarize）。"""
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
    """按 ``thread_id`` 累计 token 数与成本的内存追踪器（线程安全）。

    内存防护：最多保留 ``max_threads`` 个 thread（默认 1000）的累计条目；
    长驻进程中 thread 只增不减会导致内存无限增长，超限时淘汰最旧插入的
    thread（dict 保持插入序，弹出首个键即可）。
    """

    # 最多保留的 thread 条目数；超出时淘汰最旧的（issue #16）
    DEFAULT_MAX_THREADS = 1000

    def __init__(self, max_threads: int = DEFAULT_MAX_THREADS) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, float]] = {}
        self._max_threads = max(1, int(max_threads))

    def add(self, thread_id: str, tokens_in: int, tokens_out: int, cost: float) -> None:
        """累加一次模型调用的 token 与成本；超出容量上限时淘汰最旧 thread。"""
        with self._lock:
            if thread_id not in self._data and len(self._data) >= self._max_threads:
                # dict 按插入序保序：弹出首个键即最旧插入的 thread。
                # 已存在的 thread 更新时保持原位，不会被此次调用淘汰。
                oldest = next(iter(self._data))
                del self._data[oldest]
                _logger.debug(
                    "CostTracker 达到容量上限（%d），淘汰最旧 thread：%s", self._max_threads, oldest
                )
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

# 上报失败告警去重集合：同类错误（调用点 + 异常类型）只 warning 一次，
# 避免每次 LLM 调用失败都刷屏；后续同类失败仍静默吞掉（不阻断业务）。
_langfuse_warned_keys: set[str] = set()
_langfuse_warn_lock = threading.Lock()


def _warn_langfuse_once(key: str, message: str, *args: Any) -> None:
    """warning 级告警，但同一 ``key``（同类错误）只告警一次。"""
    with _langfuse_warn_lock:
        if key in _langfuse_warned_keys:
            return
        _langfuse_warned_keys.add(key)
    _logger.warning(message, *args)


def _ensure_langfuse() -> Any:
    """惰性初始化 Langfuse 客户端；未配置时返回 ``None``。

    SDK v2 构造参数：``public_key`` / ``secret_key`` / ``host``。自建实例
    （docker-compose 的 ``langfuse/langfuse:2`` 服务，默认映射 ``${LANGFUSE_PORT:-3000}``
    端口）需设置 ``LANGFUSE_HOST``（如 ``http://localhost:3000``）；未设置时
    SDK 默认连 Langfuse Cloud（``https://cloud.langfuse.com``）。
    """
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

    # 自建 Langfuse v2 实例通过 LANGFUSE_HOST 指定（compose 默认 3000 端口）
    host = os.getenv("LANGFUSE_HOST", "").strip()
    try:
        if host:
            _langfuse_client = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
        else:
            _langfuse_client = Langfuse(public_key=public_key, secret_key=secret_key)
    except Exception as exc:  # noqa: BLE001 初始化失败降级
        # 初始化失败意味着后续所有遥测都会丢失，不能只留 debug 级静默
        _warn_langfuse_once(
            f"init:{type(exc).__name__}",
            "Langfuse 初始化失败（%s），遥测将静默丢弃（同类错误仅告警一次）；"
            "请检查 LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY 配置",
            exc,
        )
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
        # 上报失败 = 遥测丢失（常见原因：langfuse v3+ 移除了 client.trace() 等
        # v2 API、服务不可达），必须 warning 级暴露；同类错误只告警一次防刷屏。
        _warn_langfuse_once(
            f"record_llm_call:{type(exc).__name__}",
            "Langfuse record_llm_call 上报失败（同类错误仅告警一次）：%s。"
            "请确认安装的是 langfuse>=2.0,<3.0（本模块使用 v2 API）",
            exc,
        )


def record_evaluation(
    score_name: str,
    score_value: float,
    comment: str = "",
    trace_id: str | None = None,
) -> None:
    """记录一次评测分数到 Langfuse；未启用时降级为 no-op。

    Args:
        score_name: 分数名称（如 ``citation_accuracy``）。
        score_value: 分数值。
        comment: 可选备注。
        trace_id: 要挂载 score 的业务 trace / thread ID。传入时通过
            ``client.trace(id=trace_id)`` 复用业务 trace，保证 score 与该次
            运行的 generation 落在同一 trace 下；未传时回退到当前上下文的
            cost thread（``set_cost_thread`` 设置的 thread_id，CLI/API 入口
            均会设置）。两者都无时才新建匿名 trace（并 warning 提示 score
            无法关联到业务 trace）。
    """
    client = _ensure_langfuse()
    if client is None:
        return
    effective_id = trace_id or _cost_thread_var.get()
    try:
        if effective_id:
            # 复用业务 trace：score 挂在真实运行轨迹上，而非每次新建空 trace
            trace_obj = client.trace(id=effective_id)
        else:
            _warn_langfuse_once(
                "record_evaluation:no_trace_id",
                "record_evaluation 未提供 trace_id 且当前上下文未设置 cost thread，"
                "score 将挂在新建的匿名 trace 上（无法关联业务 trace，仅告警一次）",
            )
            trace_obj = client.trace()
        trace_obj.score(name=score_name, value=score_value, comment=comment)
    except Exception as exc:  # noqa: BLE001 上报失败不阻断业务
        _warn_langfuse_once(
            f"record_evaluation:{type(exc).__name__}",
            "Langfuse record_evaluation 上报失败（同类错误仅告警一次）：%s。"
            "请确认安装的是 langfuse>=2.0,<3.0（本模块使用 v2 API）",
            exc,
        )
