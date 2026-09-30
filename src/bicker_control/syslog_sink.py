"""Optional external syslog forwarding for structured device events."""

from __future__ import annotations

import json
import logging
import logging.handlers
from typing import Any


class SyslogSink:
    def __init__(self, host: str, port: int = 514, *, tag: str = "bicker-control") -> None:
        if not host.strip():
            raise ValueError("syslog host must not be empty")
        if not 1 <= port <= 65535:
            raise ValueError("syslog port must be between 1 and 65535")
        self._logger = logging.getLogger(f"bicker-control.syslog.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._handler = logging.handlers.SysLogHandler(address=(host, port))
        self._handler.ident = f"{tag}: "
        self._logger.addHandler(self._handler)

    def __call__(self, event_type: str, severity: str, source: str, details: dict[str, Any]) -> None:
        level = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
            "critical": logging.CRITICAL,
        }[severity]
        self._logger.log(
            level,
            "%s source=%s details=%s",
            event_type,
            source,
            json.dumps(details, sort_keys=True, ensure_ascii=True),
        )

    def close(self) -> None:
        self._logger.removeHandler(self._handler)
        self._handler.close()