# SPDX-License-Identifier: Apache-2.0
"""通用工具：时间、JSON、ID、脱敏、原子写、文件权限。

刻意只用标准库；S1 的客户端要能装进最小服务器镜像。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

# ─────────────────────────────────────────────────────────── 时间

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None) -> str | None:
    """RFC 3339 UTC（Z 结尾），与 spec / 00-conventions §4 一致。"""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(raw: Any) -> datetime | None:
    """宽松解析（tolerant reader）：非法 / 缺失返回 None，不抛给调用方。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def epoch_s(moment: datetime | None = None) -> int:
    return int((moment or utcnow()).timestamp())


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return str(seconds) + " 秒"
    if seconds < 3600:
        return str(seconds // 60) + " 分 " + str(seconds % 60) + " 秒"
    hours, rem = divmod(seconds, 3600)
    if hours < 24:
        return str(hours) + " 小时 " + str(rem // 60) + " 分"
    days, hours = divmod(hours, 24)
    return str(days) + " 天 " + str(hours) + " 小时"


# ─────────────────────────────────────────────────────────── JSON / 哈希

def canonical_json(value: Any) -> str:
    """确定性 JSON（排序键、紧凑分隔符、UTF-8 原样）——签名与 digest 的唯一口径。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hmac_sha256(key: bytes, payload: str | bytes) -> str:
    import hmac as _hmac

    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return _hmac.new(key, payload, hashlib.sha256).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    import hmac as _hmac

    return _hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def new_request_id() -> str:
    return str(uuid.uuid4())


def new_install_id() -> str:
    return uuid.uuid4().hex


def new_key(nbytes: int = 32) -> bytes:
    return secrets.token_bytes(nbytes)


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


# ─────────────────────────────────────────────────────────── 脱敏（INV-02 / §8.2）

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"hmac-sha256:[0-9a-f]{32,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(client_key|provider_key|consumer_key|api[_-]?key|secret|token)"
               r"(\"?\s*[:=]\s*\"?)([^\s\"',}]{8,})"),
)

#: 明确禁止落日志 / 落诊断包的字段名（命中即整值替换）
SECRET_KEYS: frozenset[str] = frozenset({
    "client_key", "api_key", "apikey", "secret", "token", "local_token", "password",
    "passwd", "credential", "provider_key", "consumer_key", "signing_key",
    "authorization", "cookie", "set-cookie", "sig_provider", "sig_consumer",
    "signature", "messages", "prompt", "response", "content", "body",
})


def redact_text(text: str) -> str:
    """把疑似凭据 / 签名替换成 [REDACTED]（日志、诊断包共用）。"""
    if not text:
        return text
    out = text
    for pattern in _SECRET_PATTERNS:
        def _repl(match: re.Match[str]) -> str:
            if match.re.groups >= 3 and match.group(1):
                return match.group(1) + match.group(2) + "[REDACTED]"
            return "[REDACTED]"
        out = pattern.sub(_repl, out)
    return out


def redact_value(key: str, value: Any) -> Any:
    """按字段名脱敏（结构化日志 / 诊断包用）。"""
    if key.lower() in SECRET_KEYS:
        if value in (None, "", {}, []):
            return value
        if isinstance(value, (dict, list)):
            return "[REDACTED]"
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(key, item) for item in value]
    return value


def redact_obj(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def mask_secret(secret: str | None, visible: int = 4) -> str:
    """只留尾部 4 位用于确认「是哪一把」，不可用于任何鉴权。"""
    if not secret:
        return "(未设置)"
    if len(secret) <= visible:
        return "*" * len(secret)
    return "*" * (len(secret) - visible) + secret[-visible:]


# ─────────────────────────────────────────────────────────── 文件 / 权限

def ensure_dir(path: str, mode: int = 0o700) -> str:
    os.makedirs(path, mode=mode, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:                                            # pragma: no cover - 容器只读
        pass
    return path


def ensure_private_file(path: str, mode: int = 0o600) -> None:
    try:
        os.chmod(path, mode)
    except OSError:                                            # pragma: no cover
        pass


def atomic_write(path: str, data: bytes | str, mode: int = 0o600) -> None:
    """同目录临时文件 + fsync + rename；写完成即设置权限（不出现宽权限窗口）。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    directory = os.path.dirname(os.path.abspath(path)) or "."
    ensure_dir(directory, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        # Windows 没有 fchmod（NTFS ACL 也不吃 POSIX 位）；退化为路径级 chmod，不阻断写入
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def file_mode(path: str) -> int | None:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path: str, value: Any, mode: int = 0o600) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), mode)


def now_monotonic() -> float:
    return time.monotonic()


# ─────────────────────────────────────────────────────────── 其它

def chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def first_line(text: str | None, limit: int = 200) -> str:
    if not text:
        return ""
    stripped = text.strip()
    line = stripped.splitlines()[0] if stripped else ""
    return line[:limit]
