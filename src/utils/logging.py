from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import structlog
from structlog.stdlib import LoggerFactory

from ..utils.config import get_config


def setup_logging() -> None:
    config = get_config()
    log_level = getattr(logging, config.logging.level.upper(), logging.INFO)
    log_file = Path(config.logging.file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Standard library logging
    logging.basicConfig(
        level=log_level,
        format="%(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )

    # Structlog configuration
    processors = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if config.logging.json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        context_class=dict,
        logger_factory=LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


class LogContext:
    def __init__(self, **kwargs: Any):
        self.kwargs = kwargs
        self.token = None

    def __enter__(self):
        self.token = structlog.contextvars.bind_contextvars(**self.kwargs)

    def __exit__(self, *args):
        if self.token:
            structlog.contextvars.reset_contextvars(self.token)