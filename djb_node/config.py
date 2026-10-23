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

import os
from dataclasses import dataclass, field
from typing import Any

from . import facts
from .util import clamp, ensure_dir, file_mode, read_json, write_json


DEFAULT_HOME_DIRNAME = ".djb"
GATEWAY_PLACEHOLDER = "https://api.example.invalid"


def resolve_home(explicit: str | None = None) -> str:
    """优先级：--home > DJB_HOME > ~/.djb（绝对路径）。"""
    candidate = explicit or os.environ.get("DJB_HOME") or ""
    if candidate:
        return os.path.abspath(os.path.expanduser(candidate))
    return os.path.abspath(os.path.join(os.path.expanduser("~"), DEFAULT_HOME_DIRNAME))


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


def save_config(paths: Paths, config: ClientConfig) -> None:
    config.validate()
    paths.prepare()
    write_json(paths.config_file, config.to_dict(), mode=0o600)
