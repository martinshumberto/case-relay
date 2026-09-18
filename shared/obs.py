"""Structured logging shared by api and worker.

Uses logging.StreamHandler rather than print() because FastAPI routes are `def`
and run on the threadpool: print is not atomic across threads and lines
interleave under concurrency.

request_id lives in a ContextVar, set per request in the API and from the
claimed job in the worker. It is the field that ties a submission to its
processing.
"""

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

_request_id: ContextVar[str] = ContextVar("request_id", default="")

_SERVICE = "relay"

# Standard LogRecord attributes; anything else came from extra= and becomes a field.
_STANDARD = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "taskName", "thread", "threadName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "service": _SERVICE,
            "event": record.getMessage(),
        }

        rid = _request_id.get()
        if rid:
            payload["request_id"] = rid

        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def setup(service: str) -> logging.Logger:
    """Configure process logging. Call once, at startup."""
    global _SERVICE
    _SERVICE = service

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)

    # uvicorn installs its own text handlers; redirecting them keeps the whole
    # stream in JSON.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False

    return logging.getLogger(service)


def new_request_id() -> str:
    return uuid.uuid4().hex


def set_request_id(value: str) -> None:
    _request_id.set(value)


def get_request_id() -> str:
    return _request_id.get()
