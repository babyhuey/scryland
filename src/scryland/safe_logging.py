"""Logging setup with sensitive data redaction."""

import logging
import re

from rich.logging import RichHandler

from scryland.config import ScrylandConfig

# Patterns that look like sensitive data
_SENSITIVE_PATTERNS = [
    # Match key=value patterns for sensitive keys (not bare words like "Secret Lair")
    re.compile(
        r"(token|session_id|cookie|auth_ticket|password|secret_key|api_key)[=:]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
]

_REDACTED = "[REDACTED]"


class SensitiveDataFilter(logging.Filter):
    """Filter that redacts sensitive data from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._redact(str(v)) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._redact(str(a)) if isinstance(a, str) else a for a in record.args
                )
        return True

    def _redact(self, text: str) -> str:
        for pattern in _SENSITIVE_PATTERNS:
            text = pattern.sub(_REDACTED, text)
        return text


def setup_logging(config: ScrylandConfig) -> logging.Logger:
    """Configure and return the scryland logger."""
    logger = logging.getLogger("scryland")
    logger.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

    # Clear existing handlers
    logger.handlers.clear()

    # Console handler with rich
    console_handler = RichHandler(
        rich_tracebacks=True,
        show_time=True,
        show_path=False,
    )
    console_handler.addFilter(SensitiveDataFilter())
    logger.addHandler(console_handler)

    # Optional file handler
    if config.log_file:
        file_handler = logging.FileHandler(config.log_file)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        file_handler.addFilter(SensitiveDataFilter())
        logger.addHandler(file_handler)

    return logger
