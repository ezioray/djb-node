# SPDX-License-Identifier: Apache-2.0
"""凭据存储（INV-02：只存本机 OS 安全存储；S1 允许 0600 文件并显式标注）。

S1 后端：0600 文件 credentials/credentials.json + keys/*.key（目录 0700）。
文件首行字段 storage 明确写 file-0600，表示「这是 S1 的降级后端，S2 换 OS 密钥链 /
KMS 时迁移」；任何时候都不把凭据写进数据库、日志、诊断包、事件、收据。

对外只暴露需要的能力：客户端密钥、节点 id、供方收据签名密钥、缓冲密钥、上游密钥。
所有读取函数在凭据缺失时返回 None / 抛 AuthError（fail-closed，不猜）。
"""
from __future__ import annotations

import json
import os
from typing import Any

from . import crypto, facts
from .config import Paths
from .errors import AuthError
from .util import (atomic_write, ensure_dir, ensure_private_file, file_mode, iso,
                   mask_secret, read_json, utcnow)

STORAGE_BACKEND = "file-0600"
STORAGE_NOTE = ("S1 降级后端：0600 文件（OS 安全存储 S2 落地后迁移）；"
                "凭据永不进数据库/日志/诊断包/事件/收据")


class CredentialStore:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    # ─────────────────────────────────────────────────────── 容器
    def _load_all(self) -> dict[str, Any]:
        raw = read_json(self.paths.credentials_file, default=None)
        if not isinstance(raw, dict):
            return {"storage": STORAGE_BACKEND, "upstream_keys": {}}
        raw.setdefault("storage", STORAGE_BACKEND)
        if not isinstance(raw.get("upstream_keys"), dict):
            raw["upstream_keys"] = {}
        return raw

    def _save_all(self, data: dict[str, Any]) -> None:
        self.paths.prepare()
        data = dict(data)
        data["storage"] = STORAGE_BACKEND
        data["note"] = STORAGE_NOTE
        atomic_write(self.paths.credentials_file,
                     json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
                     mode=0o600)
        ensure_private_file(self.paths.credentials_file, 0o600)

    def exists(self) -> bool:
        return os.path.exists(self.paths.credentials_file)

    def ensure_permissions(self) -> list[str]:
        """把凭据文件/目录纠正到 0600/0700；返回被纠正的路径（可审计）。"""
        fixed: list[str] = []
        for path, mode in ((os.path.dirname(self.paths.credentials_file), 0o700),
                           (self.paths.credentials_file, 0o600),
                           (self.paths.buffer_key_file, 0o600),
                           (self.paths.provider_key_file, 0o600)):
            if os.path.exists(path) and file_mode(path) != mode:
                ensure_private_file(path, mode)
                fixed.append(path)
        return fixed

    # ─────────────────────────────────────────────────────── 节点 / 客户端密钥
    def node_id(self) -> str | None:
        value = self._load_all().get("node_id")
        return str(value) if isinstance(value, str) and value else None

    def set_node(self, node_id: str, client_key: str) -> None:
        if not node_id or not client_key:
            raise AuthError("node_id / client_key 不能为空")
        data = self._load_all()
        data["node_id"] = node_id
        data["client_key"] = client_key
        data["key_issued_at"] = iso(utcnow())
        self._save_all(data)

    def set_client_key(self, client_key: str) -> None:
        if not client_key:
            raise AuthError("client_key 不能为空")
        data = self._load_all()
        data["client_key"] = client_key
        data["key_issued_at"] = iso(utcnow())
        self._save_all(data)

    def client_key(self) -> str | None:
        value = self._load_all().get("client_key")
        return str(value) if isinstance(value, str) and value else None

    def require_client_key(self) -> str:
        key = self.client_key()
        if not key:
            raise AuthError("缺少节点客户端密钥：先执行 djb auth --pair（或注册后保存密钥）")
        return key

    def clear_node(self) -> None:
        data = self._load_all()
        data.pop("node_id", None)
        data.pop("client_key", None)
        data.pop("key_issued_at", None)
        self._save_all(data)

    # ─────────────────────────────────────────────────────── 上游凭据
    def upstream_secret(self, upstream_id: str) -> str | None:
        value = self._load_all()["upstream_keys"].get(upstream_id)
        return str(value) if isinstance(value, str) and value else None

    def set_upstream_secret(self, upstream_id: str, secret: str) -> None:
        if not secret:
            raise AuthError("上游密钥不能为空")
        data = self._load_all()
        data["upstream_keys"][upstream_id] = secret
        self._save_all(data)

    def delete_upstream_secret(self, upstream_id: str) -> bool:
        data = self._load_all()
        existed = upstream_id in data["upstream_keys"]
        data["upstream_keys"].pop(upstream_id, None)
        self._save_all(data)
        return existed

    # ─────────────────────────────────────────────────────── 本地密钥文件
    def _load_key_file(self, path: str) -> bytes | None:
        try:
            with open(path, "r", encoding="ascii") as handle:
                text = handle.read().strip()
        except OSError:
            return None
        try:
            key = bytes.fromhex(text)
        except ValueError:
            return None
        return key or None

    def _ensure_key_file(self, path: str) -> bytes:
        existing = self._load_key_file(path)
        if existing is not None and len(existing) >= facts.RECEIPT_KEY_MIN_BYTES:
            ensure_private_file(path, 0o600)
            return existing
        ensure_dir(os.path.dirname(path), 0o700)
        key = crypto.new_key()
        atomic_write(path, key.hex(), mode=0o600)
        return key

    def buffer_key(self) -> bytes:
        return self._ensure_key_file(self.paths.buffer_key_file)

    def provider_signing_key(self) -> bytes:
        return self._ensure_key_file(self.paths.provider_key_file)

    # ─────────────────────────────────────────────────────── 展示 / 卸载
    def describe(self) -> dict[str, Any]:
        """给 status / diagnose 的脱敏视图（绝不返回密钥本体）。"""
        data = self._load_all()
        client_key = data.get("client_key")
        return {
            "storage": STORAGE_BACKEND,
            "note": STORAGE_NOTE,
            "file": self.paths.credentials_file,
            "file_mode": oct(file_mode(self.paths.credentials_file) or 0),
            "node_id": data.get("node_id"),
            "client_key_set": bool(client_key),
            "client_key_masked": mask_secret(client_key) if client_key else None,
            "upstream_credentials": sorted(data["upstream_keys"].keys()),
            "provider_signing_key": os.path.exists(self.paths.provider_key_file),
            "buffer_key": os.path.exists(self.paths.buffer_key_file),
        }

    def clear(self) -> list[str]:
        """卸载：删除凭据与本地密钥；返回被删除的路径。"""
        removed: list[str] = []
        for path in (self.paths.credentials_file, self.paths.buffer_key_file,
                     self.paths.provider_key_file):
            if os.path.exists(path):
                try:
                    os.unlink(path)
                    removed.append(path)
                except OSError:                            # pragma: no cover
                    pass
        return removed
