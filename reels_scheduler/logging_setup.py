"""Structured logging.

Extra keyword fields passed as ``log.info("msg", extra={...})`` are rendered as
``key=value`` pairs in text mode and merged into the object in JSON mode, so the
same call sites work for humans and for log shipping.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict

_STANDARD = set(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"message", "asctime", "taskName"}


def _extras(record: logging.LogRecord) -> Dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _STANDARD}


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = "%s %-7s %-28s %s" % (
            self.formatTime(record, "%Y-%m-%d %H:%M:%S"),
            record.levelname,
            record.name,
            record.getMessage(),
        )
        extras = _extras(record)
        if extras:
            base += "  " + " ".join("%s=%s" % (k, v) for k, v in sorted(extras.items()))
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update({k: str(v) for k, v in _extras(record).items()})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure(level: str = "INFO", fmt: str = "text") -> None:
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
