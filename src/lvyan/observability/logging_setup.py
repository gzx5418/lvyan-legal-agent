"""结构化日志配置。

生产环境：JSON 格式 → 可被 Loki/ELK 解析。
开发环境：彩色文本格式 → 人类可读。

使用 structlog 统一 stdlib logging 和 structlog 输出。
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any


def setup_logging(log_format: str | None = None, level: str = "INFO") -> None:
    """配置全局日志。

    Args:
        log_format: "json"（生产）或 "text"（开发）。默认读取 LOG_FORMAT 环境变量。
        level: 日志级别，默认读取 LOG_LEVEL 环境变量或 INFO。
    """
    if log_format is None:
        log_format = os.getenv("LOG_FORMAT", "text").strip().lower()

    log_level = os.getenv("LOG_LEVEL", level).strip().upper()
    numeric_level = getattr(logging, log_level, logging.INFO)

    try:
        import structlog

        shared_processors: list[Any] = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
        ]

        if log_format == "json":
            renderer = structlog.processors.JSONRenderer(ensure_ascii=False)
        else:
            renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

        structlog.configure(
            processors=[
                *shared_processors,
                structlog.stdlib.ProcessorFormatter.wrap_events_in_msg_field,
                renderer,
            ],
            wrapper_class=structlog.stdlib.BoundLogger,
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )

        # 同时配置 stdlib logging 使用 structlog 格式化
        formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
            foreign_pre_chain=shared_processors,
        )

        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)

        root = logging.getLogger()
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(numeric_level)

        # 降低噪声库的日志级别
        for noisy in ("httpcore", "httpx", "urllib3", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    except ImportError:
        # structlog 未安装时回退到基础 logging
        logging.basicConfig(
            level=numeric_level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            stream=sys.stderr,
        )
        logging.getLogger("lvyan").info(
            "structlog 未安装，使用基础 logging 格式"
        )
