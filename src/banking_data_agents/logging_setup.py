"""Structured logging.

JSON in non-local environments (so CloudWatch Logs Insights can query it and the
audit trail is machine-readable), coloured key-value on a laptop.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog

from banking_data_agents.config import get_settings


def configure_logging(level: str | None = None) -> None:
    """Install the structlog -> stdlib pipeline exactly once per process."""
    settings = get_settings()
    # INFO by default everywhere; per-module DEBUG is opt-in via BDA_LOG_LEVEL so
    # that pipeline output stays readable without silencing diagnostics.
    configured = level or os.environ.get("BDA_LOG_LEVEL")
    log_level = (configured or "INFO").upper()

    # Logs go to stderr, never stdout. stdout belongs to the program's payload:
    # `bda ask --json | jq` has to receive JSON and nothing else.
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=log_level)

    # Botocore/httpx trace every TCP attempt at INFO. That is noise, not signal,
    # and it buries the pipeline output; surface it only when explicitly debugging.
    # `strands` is here because the SDK announces its own telemetry client at INFO.
    for noisy in ("httpx", "httpcore", "botocore", "urllib3", "s3transfer", "asyncio", "strands"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if settings.is_local:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(log_level)),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for ``name``."""
    return structlog.get_logger(name)


def bind_request_context(**kwargs: Any) -> None:
    """Attach request-scoped fields (trace_id, session_id, user_id) to all logs."""
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()
