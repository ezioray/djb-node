# SPDX-License-Identifier: Apache-2.0
import os
from datetime import datetime, timezone

from djb_node import util


def test_iso_and_parse_roundtrip():
    moment = datetime(2026, 10, 24, 1, 2, 3, tzinfo=timezone.utc)
    text = util.iso(moment)
    assert text == "2026-10-24T01:02:03Z"
    assert util.parse_iso(text) == moment
    assert util.parse_iso("not-a-date") is None
    assert util.parse_iso(None) is None


def test_canonical_json_is_stable():
    assert util.canonical_json({"b": 1, "a": [1, 2]}) == util.canonical_json(
        {"a": [1, 2], "b": 1})


def test_redaction_masks_secrets_and_bodies():
    text = ('client_key=ck_abcdefghijklmnop Bearer abcdefghijklmnop '
            + 'sk-' + 'abcdefghijklmnopqrstuvwxyz' + ' hmac-sha256:' + "a" * 64)
    masked = util.redact_text(text)
    assert "ck_abcdefghijklmnop" not in masked
    assert "sk-" + "abcdefghijklmnopqrstuvwxyz" not in masked
    assert "a" * 64 not in masked
    assert "[REDACTED]" in masked


def test_redact_obj_masks_secret_fields():
    payload = {"client_key": "secret-value", "messages": [{"content": "正文"}],
               "nested": {"token": "abc", "ok": "fine"}}
    masked = util.redact_obj(payload)
    assert masked["client_key"] == "[REDACTED]"
    assert masked["messages"] == "[REDACTED]"
    assert masked["nested"]["token"] == "[REDACTED]"
    assert masked["nested"]["ok"] == "fine"


def test_mask_secret_keeps_tail_only():
    assert util.mask_secret("abcdefgh") == "****efgh"
    assert util.mask_secret(None) == "(未设置)"


def test_atomic_write_mode_and_content(tmp_path):
    path = tmp_path / "secret.txt"
    util.atomic_write(str(path), "hello", mode=0o600)
    assert path.read_text() == "hello"
    assert util.file_mode(str(path)) == 0o600
    assert not [name for name in os.listdir(tmp_path) if name.startswith(".tmp-")]


def test_write_json_and_read_json(tmp_path):
    path = tmp_path / "x.json"
    util.write_json(str(path), {"b": 2, "a": 1})
    assert util.read_json(str(path)) == {"a": 1, "b": 2}
    assert util.read_json(str(tmp_path / "missing.json"), default={"x": 1}) == {"x": 1}


def test_human_duration_and_clamp_and_chunks():
    assert util.human_duration(5) == "5 秒"
    assert util.human_duration(65) == "1 分 5 秒"
    assert util.human_duration(3700).startswith("1 小时")
    assert util.clamp(5, 1, 3) == 3
    assert list(util.chunks([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]


def test_hmac_and_constant_time_equals():
    digest = util.hmac_sha256(b"k" * 32, "payload")
    assert len(digest) == 64
    assert util.constant_time_equals(digest, digest)
    assert not util.constant_time_equals(digest, "0" * 64)
    assert util.first_line("a\nb", 10) == "a"
