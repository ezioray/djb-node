# SPDX-License-Identifier: Apache-2.0
"""本机路径与 client.* 配置（M15 §8.1：本地默认值，仅展示，不覆盖服务端 policy）。

本机目录（默认 ~/.djb，可用 DJB_HOME 或 --home 覆盖）::

    config.json                 非敏感运行配置（0600）
    state.db                    安装 / Profile / 策略 / 公告 / 收据 / 用量
    credentials/credentials.json 凭据（0600，S1 允许的文件后端，显式标注）
    keys/                       收据缓冲密钥 / 供方签名密钥（0600）
    logs/                       结构化日志（7 天，脱敏）
    runtime/                    daemon.sock（0600）、daemon.pid
    versions/                   安装包版本目录（保留 2 版）
    diagnostics/                诊断包
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from . import facts
from .platform.paths import default_home as _platform_default_home
from .util import clamp, ensure_dir, file_mode, read_json, write_json


DEFAULT_HOME_DIRNAME = ".djb"
GATEWAY_PLACEHOLDER = "https://api.example.invalid"


def resolve_home(explicit: str | None = None) -> str:
    """优先级：--home > DJB_HOME > 平台默认（C2）。

    Linux 与 S1 逐字一致（~/.djb）；macOS / Windows 的默认目录由
    `platform.paths.default_home()` 决定（S2 托盘安装包使用）。
    """
    return _platform_default_home(explicit)


@dataclass
class Paths:
    home: str

    def __post_init__(self) -> None:
        self.home = os.path.abspath(os.path.expanduser(self.home))

    @property
    def config_file(self) -> str:
        return os.path.join(self.home, "config.json")

    @property
    def db_file(self) -> str:
        return os.path.join(self.home, "state.db")

    @property
    def credentials_file(self) -> str:
        return os.path.join(self.home, "credentials", "credentials.json")

    @property
    def keys_dir(self) -> str:
        return os.path.join(self.home, "keys")

    @property
    def buffer_key_file(self) -> str:
        return os.path.join(self.keys_dir, "buffer.key")

    @property
    def provider_key_file(self) -> str:
        return os.path.join(self.keys_dir, "receipt_provider.key")

    @property
    def logs_dir(self) -> str:
        return os.path.join(self.home, "logs")

    @property
    def log_file(self) -> str:
        return os.path.join(self.logs_dir, facts.LOG_FILE)

    @property
    def runtime_dir(self) -> str:
        return os.path.join(self.home, "runtime")

    @property
    def socket_file(self) -> str:
        return os.path.join(self.runtime_dir, "daemon.sock")

    @property
    def pid_file(self) -> str:
        return os.path.join(self.runtime_dir, "daemon.pid")

    @property
    def versions_dir(self) -> str:
        return os.path.join(self.home, "versions")

    @property
    def diagnostics_dir(self) -> str:
        return os.path.join(self.home, "diagnostics")

    @property
    def cache_dir(self) -> str:
        return os.path.join(self.home, "cache")

    def prepare(self) -> None:
        """创建 0700 目录树（幂等；不创建文件）。"""
        ensure_dir(self.home, 0o700)
        ensure_dir(os.path.dirname(self.credentials_file), 0o700)
        ensure_dir(self.keys_dir, 0o700)
        ensure_dir(self.logs_dir, 0o700)
        ensure_dir(self.runtime_dir, 0o700)
        ensure_dir(self.versions_dir, 0o700)
        ensure_dir(self.diagnostics_dir, 0o700)
        ensure_dir(self.cache_dir, 0o700)

    def existing(self) -> bool:
        return os.path.isdir(self.home) and os.path.exists(self.db_file)

    def snapshot(self) -> dict[str, Any]:
        """诊断 / 卸载自检用：路径与权限（不含内容）。"""
        return {
            "home": self.home,
            "db_file": self.db_file,
            "db_mode": oct(file_mode(self.db_file) or 0),
            "credentials_file": self.credentials_file,
            "credentials_mode": oct(file_mode(self.credentials_file) or 0),
            "config_mode": oct(file_mode(self.config_file) or 0),
        }


@dataclass
class ClientConfig:
    """config.json 的形状（非敏感；凭据永不写这里）。"""

    gateway_url: str = GATEWAY_PLACEHOLDER
    mirror_port: int = facts.MIRROR_PORT
    update_channel: str = facts.DEFAULT_UPDATE_CHANNEL
    update_source: str = ""              # 空 = 内置 mock 源（S1）；可填 URL / 本地目录
    log_level: str = facts.DEFAULT_LOG_LEVEL
    yield_enabled: bool = facts.DEFAULT_YIELD_ENABLED
    extra: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not (facts.PORT_MIN <= int(self.mirror_port) <= facts.PORT_MAX):
            raise ValueError("mirror_port 必须在 " + str(facts.PORT_MIN) + "-"
                             + str(facts.PORT_MAX))
        if self.update_channel not in facts.UPDATE_CHANNELS:
            raise ValueError("update_channel 必须是 " + "/".join(facts.UPDATE_CHANNELS))
        if self.log_level not in facts.LOG_LEVELS:
            raise ValueError("log_level 必须是 " + "/".join(facts.LOG_LEVELS))

    def to_dict(self) -> dict[str, Any]:
        base = {
            "gateway_url": self.gateway_url,
            "mirror_port": int(self.mirror_port),
            "update_channel": self.update_channel,
            "update_source": self.update_source,
            "log_level": self.log_level,
            "yield_enabled": bool(self.yield_enabled),
        }
        if self.extra:
            base["extra"] = dict(self.extra)
        return base

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None, *, tolerate: bool = True) -> "ClientConfig":
        """tolerant reader：未知字段进 extra，非法值回落默认（不因脏配置崩溃）。"""
        raw = dict(raw or {})
        config = cls()
        for name in ("gateway_url", "update_channel", "update_source", "log_level"):
            value = raw.get(name)
            if isinstance(value, str) and value.strip():
                setattr(config, name, value.strip())
        port = raw.get("mirror_port")
        if isinstance(port, int) and not isinstance(port, bool):
            config.mirror_port = clamp(port, facts.PORT_MIN, facts.PORT_MAX)
        enabled = raw.get("yield_enabled")
        if isinstance(enabled, bool):
            config.yield_enabled = enabled
        known = {"gateway_url", "mirror_port", "update_channel", "update_source",
                 "log_level", "yield_enabled"}
        extra = raw.get("extra")
        if isinstance(extra, dict):
            config.extra = dict(extra)
        for key, value in raw.items():
            if key not in known and key != "extra":
                config.extra[key] = value
        if not tolerate:
            config.validate()
        return config


def load_config(paths: Paths) -> ClientConfig:
    raw = read_json(paths.config_file, default=None)
    if not isinstance(raw, dict):
        return ClientConfig()
    return ClientConfig.from_dict(raw)


def load_config_strict(paths: Paths) -> ClientConfig:
    """热重载专用：**坏配置必须报错**，不许悄悄回落默认值。

    :func:`load_config` 是宽容读（脏配置不能把 daemon 弄崩，启动时也必须能用）；
    但 ``djb daemon reload`` 的语义是「让磁盘上的新配置生效」——新配置写错了却静默
    变成默认值，用户会以为改动生效了。所以这里：

    * 文件不存在 / 空文件 → 返回默认值（等于「没有自定义配置」，不算错误）；
    * JSON 坏 / 顶层不是对象 / 枚举或范围非法 / 类型不对 → 抛 ``ValueError``，
      调用方保留旧配置并如实报错；
    * 未知字段仍走 tolerant 进 ``extra``（向前兼容，不因新字段拒绝重载）。
    """
    try:
        with open(paths.config_file, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return ClientConfig()
    except OSError as exc:
        raise ValueError("读不到 config.json：" + (exc.strerror or type(exc).__name__)) from exc
    if not text.strip():
        return ClientConfig()
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ValueError("config.json 不是合法 JSON：" + str(exc)) from exc
    if not isinstance(raw, dict):
        raise ValueError("config.json 顶层必须是对象（当前 " + type(raw).__name__ + "）")
    port = raw.get("mirror_port")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int)
                             or not (facts.PORT_MIN <= port <= facts.PORT_MAX)):
        raise ValueError("mirror_port 必须是 " + str(facts.PORT_MIN) + "-"
                         + str(facts.PORT_MAX) + " 的整数（当前 " + repr(port) + "）")
    for name, allowed in (("update_channel", facts.UPDATE_CHANNELS),
                          ("log_level", facts.LOG_LEVELS)):
        value = raw.get(name)
        if value is not None and value not in allowed:
            raise ValueError(name + " 必须是 " + "/".join(allowed)
                             + "（当前 " + repr(value) + "）")
    for name in ("gateway_url", "update_source"):
        value = raw.get(name)
        if value is not None and not isinstance(value, str):
            raise ValueError(name + " 必须是字符串（当前 " + type(value).__name__ + "）")
    config = ClientConfig.from_dict(raw)
    config.validate()                    # 兜底：漏网的非法值也不许进内存
    return config


_TRUE_TEXT = ("1", "true", "on", "yes", "y", "enabled")

#: 开关的环境变量覆盖前缀（ROUND3 / C-01b）：`client.probe.periodic.enabled`
#: → `DJB_FLAG_CLIENT_PROBE_PERIODIC_ENABLED`。给运维一个**不改配置、不改代码**的
#: 一键闸门（回滚规则里写的就是它），也让「干净 home 上做真机取证」不必先写盘。
FLAG_ENV_PREFIX = "DJB_FLAG_"


def flag_env_name(name: str) -> str:
    """开关名 → 环境变量名（唯一换算处：CLI/文档/测试都引用这里，不各自拼字符串）。"""
    return FLAG_ENV_PREFIX + str(name).upper().replace(".", "_").replace("-", "_")


def _env_flag(name: str) -> bool | None:
    """环境变量里有没有显式取值；没设/空串返回 None（= 不用它，继续看配置）。"""
    raw = os.environ.get(flag_env_name(name))
    if raw is None or not str(raw).strip():
        return None
    return str(raw).strip().lower() in _TRUE_TEXT


def _extra_of(config: Any) -> dict[str, Any]:
    """把 ClientConfig / 形状等价的 mapping（`config.extra`）统一成 extra 字典。"""
    if isinstance(config, ClientConfig):
        return config.extra if isinstance(config.extra, dict) else {}
    if isinstance(config, Mapping):
        extra = config.get("extra")
        if isinstance(extra, Mapping):
            return dict(extra)          # 形状等价于 config.json 的 {"extra": {...}}
        return dict(config)             # 直接就是 extra 字典
    return {}


def _utc_today() -> date:
    """求值当天（UTC）：与 tray/actions、ipc_windows 的到期口径一致。"""
    return datetime.now(timezone.utc).date()


def _parse_expire_at(value: Any) -> date | None:
    """把 `expire_at` 解析成日期；缺失/非法返回 None（调用方 fail-closed 处理）。

    接受三类写法：ISO 日期字符串、ISO **日期时间**字符串（ROUND3：`2026-12-15T00:00:00+08:00`
    这类带时区的「批准窗口」写法，取**写明的那个时区里的日期**，不换算到 UTC 免得前后差一天）、
    以及 date/datetime 对象。数字等其它类型一律视为非法（避免 `20271231` 被 ISO basic
    格式静默解析成日期）。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:                                   # 带时间的 ISO 8601（含时区偏移）
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def flag_expired(name: str, *, today: date | None = None) -> bool:
    """开关是否已到期（`expire_at` 当日仍有效，次日起强制关）。

    - 已登记开关（`facts.FEATURE_FLAGS`）缺失/非法 `expire_at` → **返回 True（fail-closed）**，
      宁可不启用也不让开关永久开着；
    - 未登记开关没有到期元数据 → 返回 False，语义仍由 `feature_flag` 的 C7 口径处理，
      本函数不改变未登记开关的既有行为。

    `today` 只供测试注入；非日期时回落 UTC 今天（生产路径的默认行为）。
    """
    spec = facts.FEATURE_FLAGS.get(str(name))
    if not isinstance(spec, Mapping):
        return False
    expires = _parse_expire_at(spec.get("expire_at"))
    if expires is None:
        return True
    ref = today if isinstance(today, date) and not isinstance(today, datetime) else _utc_today()
    return ref > expires


def feature_flag(config: Any, name: str, *, today: date | None = None) -> bool:
    """特性开关求值（**默认关闭**；未知开关一律按定义默认值；到期强制关）。

    M17 §9：开关必须三字段齐全（owner / expire_at / rollback_rule）且默认关闭，
    定义在 `facts.FEATURE_FLAGS`。本机取值只读 config.json 的非敏感 `extra`
    （`feature_flags` 子字典优先，其次同名平铺键），不覆盖服务端 policy。

    取值优先级（ROUND3 追加，只加不改）：**环境变量 > 配置 > 登记表 default**。
    环境变量名由 :func:`flag_env_name` 换算（`DJB_FLAG_<NAME 大写点转下划线>`），
    只在该变量存在且非空时参与判定 —— 没设它时行为与改前逐字一致。它是回滚规则里
    写的那道闸门：`DJB_FLAG_CLIENT_PROBE_PERIODIC_ENABLED=false` 可压过配置里的 true。

    `expire_at` 由本入口统一强制（与 tray/actions、ipc_windows 同一口径：**UTC 日期，
    到期日当天仍有效，次日起为 False**）；已登记但 `expire_at` 缺失/非法按 fail-closed
    处理。未登记开关没有到期元数据，保持 C7 既有语义不额外收紧。

    这是全队唯一的开关求值入口：`config` 接受 `ClientConfig`，也接受形状等价的
    mapping（即 `config.extra` 本身，或含 `extra` 键的 dict），便于其他模块复用。
    `today` 是仅测试注入的只加参数（线上调用不传）。
    """
    if flag_expired(name, today=today):
        return False
    spec = facts.FEATURE_FLAGS.get(str(name)) or {}
    default = bool(spec.get("default", False))
    from_env = _env_flag(name)
    if from_env is not None:
        return from_env
    extra = _extra_of(config)
    nested = extra.get("feature_flags")
    value: Any = None
    if isinstance(nested, Mapping) and name in nested:
        value = nested[name]
    elif name in extra:
        value = extra[name]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_TEXT
    if isinstance(value, int) and not isinstance(value, bool):
        return bool(value)
    return default


def flag_spec(name: str) -> dict[str, Any]:
    """开关定义（三字段 + 默认值），用于 status / 诊断展示。"""
    spec = facts.FEATURE_FLAGS.get(str(name)) or {}
    return dict(spec)


def save_config(paths: Paths, config: ClientConfig) -> None:
    config.validate()
    paths.prepare()
    write_json(paths.config_file, config.to_dict(), mode=0o600)
