# SPDX-License-Identifier: Apache-2.0
"""结构化日志：脱敏、轮转、7 天清理、异常字段。"""
from __future__ import annotations

import json
import logging
import os
import time

from djb_node import facts
from djb_node.logging_setup import (JsonFormatter, RotatingJsonHandler, get_logger,
                                    log_retention_cutoff, prune_old_logs,
                                    setup_logging)


def _lines(path):
    with open(path, "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_event_is_json_and_redacted(tmp_path):
    log_file = str(tmp_path / "logs" / facts.LOG_FILE)
    logger = setup_logging(log_file, level="debug", also_stderr=True)
    logger.info("test.event", extra={"event": "test.event",
                                     "fields": {"client_key": "ck_" + "secret_0123456789",
                                                "model": "m1",
                                                "sig_provider": "hmac-sha256:" + "a" * 64}})
    logger.debug("debug.event", extra={"event": "debug.event", "fields": {}})
    logger.error("boom", exc_info=True, extra={"event": "boom", "fields": {}})
    entries = _lines(log_file)
    assert entries[0]["event"] == "test.event"
    assert entries[0]["module"] == facts.MODULE_ID
    assert entries[0]["client_key"] == "[REDACTED]"
    assert entries[0]["model"] == "m1"
    assert entries[1]["level"] == "debug"
    assert entries[2]["error"] == "NoneType" or "error" in entries[2]
    assert oct(os.stat(log_file).st_mode)[-3:] == "600"


def test_handler_rotates_and_prunes(tmp_path):
    path = str(tmp_path / "djb.log")
    handler = RotatingJsonHandler(path, max_bytes=50, retention_days=7)
    logger = logging.getLogger("djb-rotate")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    for index in range(6):
        logger.info("rotate.me", extra={"event": "rotate.me",
                                        "fields": {"index": index}})
    handler.close()
    names = os.listdir(tmp_path)
    assert any(name != "djb.log" for name in names)
    assert os.path.exists(path)


def test_prune_old_logs_and_cutoff(tmp_path):
    old = tmp_path / "djb.log.old"
    new = tmp_path / "djb.log.new"
    old.write_text("{}\n")
    new.write_text("{}\n")
    eight_days = time.time() - (facts.LOG_RETENTION_DAYS + 1) * 86400
    os.utime(old, (eight_days, eight_days))
    removed = prune_old_logs(str(tmp_path))
    assert str(old) in removed
    assert new.exists()
    assert log_retention_cutoff().endswith("Z")
    assert prune_old_logs(str(tmp_path / "missing")) == []


def test_formatter_handles_extra_and_exception():
    formatter = JsonFormatter()
    record = logging.LogRecord("djb", logging.WARNING, __file__, 1, "msg", None, None)
    record.event = "custom"
    record.fields = {"token": "secret-value-123", "nested": {"a": "sk-" + "abcdefghijklmnop"}}
    payload = json.loads(formatter.format(record))
    assert payload["event"] == "custom"
    assert payload["token"] == "[REDACTED]"
    assert "sk-" not in json.dumps(payload)
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record2 = logging.LogRecord("djb", logging.ERROR, __file__, 2, "msg", None,
                                    (RuntimeError, RuntimeError("x"), None))
    payload2 = json.loads(formatter.format(record2))
    assert payload2["error"] == "RuntimeError"
    assert payload2["error_message"] == "x"


def test_get_logger_returns_module_logger():
    assert get_logger().name == "djb"
