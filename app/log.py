"""Logs estructurados en JSON (RNF-02)."""

import json
import logging
import sys
from datetime import datetime, timezone

LOGGER_NAME = "runnia"
# RNF-02: no se registran contraseñas ni tokens.
SENSITIVE_KEYS = ("password", "token", "secret", "api_key")
REDACTED = "[redactado]"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)


class StdoutHandler(logging.StreamHandler):
    """Escribe en el sys.stdout vigente al emitir, no en el del momento de creación."""

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, value):
        pass


def _redact(fields: dict) -> dict:
    return {
        key: REDACTED if any(s in key.lower() for s in SENSITIVE_KEYS) else value
        for key, value in fields.items()
    }


def get_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = StdoutHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def log_event(event: str, level: int = logging.INFO, **fields) -> None:
    """Registra un evento con campos estructurados, p. ej. log_event("guardrail", antes=12, despues=11)."""
    get_logger().log(level, event, extra={"fields": _redact(fields)})
