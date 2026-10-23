# SPDX-License-Identifier: Apache-2.0
"""结构化 JSON 日志 + 轮转 + 脱敏（00-conventions §8 / M15 §8.2）。

字段：ts / level / module / event / trace_id / request_id / error_code / duration_ms +
任意业务字段；**任何字段都先过脱敏**（凭据、签名、Bearer、prompt 正文一律不落）。

保留 7 天、按大小轮转（5 MiB）；日志文件 0600，目录 0700。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any

from . import facts
from .util import iso, redact_text, redact_value, utcnow

LOGGER_NAME = "djb"
_DATE_FMT = "%Y%m%dT%H%M%S"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": iso(utcnow()),
            "level": record.levelname.lower(),
            "module": facts.MODULE_ID,
            "event": getattr(record, "event", None) or record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for key, value in fields.items():
                if key in ("ts", "level", "module", "event"):
                    continue
                payload[key] = redact_value(str(key), value)
        if record.exc_info:
            exc_type = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            payload["error"] = redact_text(str(exc_type))
            payload["error_message"] = redact_text(str(record.exc_info[1] or ""))[:300]
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class RotatingJsonHandler(logging.Handler):
    """按大小 / 日期轮转，保留 N 天（默认 7）。"""

    def __init__(self, path: str, *, max_bytes: int = facts.LOG_MAX_BYTES,
                 retention_days: int = facts.LOG_RETENTION_DAYS) -> None:
        super().__init__()
        self.path = path
        self.max_bytes = int(max_bytes)
        self.retention_days = int(retention_days)
        self._stream: Any = None
        self._size = 0
        self._day = ""
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        self._open()
        self.setFormatter(JsonFormatter())

    def _open(self) -> None:
        self._stream = open(self.path, "a", encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:                                        # pragma: no cover
            pass
        try:
            self._size = os.path.getsize(self.path)
        except OSError:
            self._size = 0
        self._day = datetime.now().strftime("%Y%m%d")

    def _rotate(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:                                    # pragma: no cover
                pass
            self._stream = None
        suffix = datetime.now().strftime(_DATE_FMT)
        target = self.path + "." + suffix
        counter = 0
        while os.path.exists(target):
            counter += 1
            target = self.path + "." + suffix + "." + str(counter)
        try:
            os.replace(self.path, target)
        except OSError:                                        # pragma: no cover
            pass
        self._open()
        self._prune()

    def _prune(self) -> None:
        directory = os.path.dirname(self.path) or "."
        base = os.path.basename(self.path)
        cutoff = time.time() - self.retention_days * 86400
        try:
            names = os.listdir(directory)
        except OSError:                                        # pragma: no cover
            return
        for name in names:
            if not name.startswith(base + "."):
                continue
            candidate = os.path.join(directory, name)
            try:
                if os.path.getmtime(candidate) < cutoff:
                    os.unlink(candidate)
            except OSError:                                    # pragma: no cover
                pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            if self._stream is None:
                self._open()
            today = datetime.now().strftime("%Y%m%d")
            if self._size > self.max_bytes or today != self._day:
                self._rotate()
            self._stream.write(message + "\n")
            self._stream.flush()
            self._size += len(message.encode("utf-8")) + 1
        except Exception:                                      # pragma: no cover - 日志不阻断
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
        finally:
            self._stream = None
            super().close()


def setup_logging(log_file: str, *, level: str = facts.DEFAULT_LOG_LEVEL,
                  also_stderr: bool = False) -> logging.Logger:
    """幂等：重复调用只替换本模块自己的 handler。"""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if level == "debug" else logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:                                      # pragma: no cover
            pass
    file_handler = RotatingJsonHandler(log_file)
    logger.addHandler(file_handler)
    if also_stderr:
        stderr_handler = logging.StreamHandler()
        stderr_handler.setFormatter(JsonFormatter())
        logger.addHandler(stderr_handler)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def event(name: str, *, level: str = "info", **fields: Any) -> None:
    """记录一条结构化事件（字段自动脱敏）。"""
    logger = get_logger()
    numeric = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
               "error": logging.ERROR}.get(level, logging.INFO)
    logger.log(numeric, name, extra={"event": name, "fields": fields})


def prune_old_logs(directory: str, *, retention_days: int = facts.LOG_RETENTION_DAYS) -> list[str]:
    """登录前 / 卸载前跑一次：返回被删除的文件（7 天保留）。"""
    removed: list[str] = []
    cutoff = time.time() - retention_days * 86400
    try:
        names = os.listdir(directory)
    except OSError:
        return removed
    for name in names:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
                removed.append(path)
        except OSError:                                        # pragma: no cover
            pass
    return removed


def log_retention_cutoff(*, retention_days: int = facts.LOG_RETENTION_DAYS) -> str:
    return iso(utcnow() - timedelta(days=retention_days))
