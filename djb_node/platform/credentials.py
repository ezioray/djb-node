# SPDX-License-Identifier: Apache-2.0
"""凭据后端（C2）：``CredentialBackend`` 协议 + 文件后端 + 三端 OS 存储探测。

设计边界
========

* **默认后端永远是文件后端**（:class:`FileBackend` → 现有 ``secure.CredentialStore``）：
  0600 文件 + 0700 目录，行为与 S1 完全一致。本模块不改变任何现有读取路径。
* macOS Keychain / Windows Credential Manager / libsecret 在 S2 之前**只做可用性探测**
  （:meth:`OsCredentialStore.available`），不读写、不弹钥匙串授权框、不调用
  ``security`` / ``cmdkey`` / ``secret-tool`` 子进程，因此不会被误当成可用后端选中。
  探测结果只用于 diagnose / 安装器提示「这台机器以后可以迁到 OS 安全存储」。
* 不做提权、不写注册表、不写系统目录；凭据值永不进日志 / 诊断 / 事件 / 收据
  （INV-02 仍由 ``secure.CredentialStore`` 的脱敏视图保证）。
* 错误沿用 ``djb_node.errors``：凭据缺失 → ``AuthError``（fail-closed，不猜）。

为什么不是「后端自动切换」
==========================

自动切到 OS 存储会改变凭据的读取位置（可能是破坏性迁移），且需要人审批与回滚规则；
本层只提供「协议 + 探测」，切换属于 S2 的显式迁移任务（带 owner / expire_at /
rollback_rule 的开关），不在 C2 范围内。
"""
from __future__ import annotations

import os
import shutil
from typing import Any, Mapping, Protocol, runtime_checkable

from ..config import Paths
from ..errors import AuthError
from ..secure import STORAGE_BACKEND as FILE_BACKEND
from ..secure import CredentialStore
from . import SYSTEM_DARWIN, SYSTEM_LINUX, SYSTEM_WINDOWS, PlatformInfo, current_platform
from .paths import PlatformPaths

__all__ = [
    "CredentialBackend",
    "FileBackend",
    "OsCredentialStore",
    "MacKeychainStore",
    "WindowsCredentialManagerStore",
    "LibsecretStore",
    "default_backend",
    "probe_backends",
    "FILE_BACKEND",
]

PathsLike = Paths | PlatformPaths


@runtime_checkable
class CredentialBackend(Protocol):
    """凭据后端协议：现有文件后端与（S2 的）OS 后端都实现同一组能力。"""

    name: str
    kind: str

    def available(self) -> bool: ...

    def describe(self) -> dict[str, Any]: ...

    def exists(self) -> bool: ...

    def node_id(self) -> str | None: ...

    def set_node(self, node_id: str, client_key: str) -> None: ...

    def client_key(self) -> str | None: ...

    def require_client_key(self) -> str: ...

    def upstream_secret(self, upstream_id: str) -> str | None: ...

    def set_upstream_secret(self, upstream_id: str, secret: str) -> None: ...

    def delete_upstream_secret(self, upstream_id: str) -> bool: ...

    def ensure_permissions(self) -> list[str]: ...

    def clear(self) -> list[str]: ...


class FileBackend:
    """0600 文件后端：完全复用 ``secure.CredentialStore``（S1 行为不变）。

    目录 0700 / 文件 0600 / 原子写 / 脱敏描述都由现有实现负责；本类只做委托与命名，
    不新增存储格式、不迁移、不删除任何既有凭据。
    """

    name = FILE_BACKEND
    kind = "file"

    def __init__(self, paths: PathsLike) -> None:
        self.paths = paths
        self.store = CredentialStore(paths)          # 同一份 0600 存储，无新格式

    def available(self) -> bool:
        # 文件后端始终可用（能否写由调用方在 prepare() 时自然暴露 OSError）
        return True

    def describe(self) -> dict[str, Any]:
        payload = dict(self.store.describe())
        payload["backend"] = self.name
        payload["backend_kind"] = self.kind
        return payload

    def exists(self) -> bool:
        return self.store.exists()

    def node_id(self) -> str | None:
        return self.store.node_id()

    def set_node(self, node_id: str, client_key: str) -> None:
        self.store.set_node(node_id, client_key)

    def client_key(self) -> str | None:
        return self.store.client_key()

    def require_client_key(self) -> str:
        return self.store.require_client_key()

    def upstream_secret(self, upstream_id: str) -> str | None:
        return self.store.upstream_secret(upstream_id)

    def set_upstream_secret(self, upstream_id: str, secret: str) -> None:
        self.store.set_upstream_secret(upstream_id, secret)

    def delete_upstream_secret(self, upstream_id: str) -> bool:
        return self.store.delete_upstream_secret(upstream_id)

    def ensure_permissions(self) -> list[str]:
        return self.store.ensure_permissions()

    def clear(self) -> list[str]:
        return self.store.clear()


class OsCredentialStore:
    """OS 安全存储的可用性探测器（只探测，不读写、不起子进程、不弹授权框）。"""

    name = ""
    kind = "os"
    probe = ""
    status = "probe-only"

    def __init__(self, *, platform: PlatformInfo | None = None,
                 environ: Mapping[str, str] | None = None,
                 which: Any = None) -> None:
        self._platform = platform
        self._environ = environ
        self._which = which or shutil.which

    @property
    def platform(self) -> PlatformInfo:
        return self._platform or current_platform(environ=self._environ)

    def _env(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def _has_binary(self, name: str) -> bool:
        return bool(self._which(name))

    def available(self) -> bool:                          # pragma: no cover - 子类实现
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "available": self.available(),
                "probe": self.probe, "status": self.status}


class MacKeychainStore(OsCredentialStore):
    """macOS Keychain：探测 ``security``（系统自带）是否在 PATH 上。"""

    name = "macos-keychain"
    probe = "security"

    def available(self) -> bool:
        return self.platform.os == SYSTEM_DARWIN and self._has_binary("security")


class WindowsCredentialManagerStore(OsCredentialStore):
    """Windows Credential Manager：探测 advapi32 是否可加载（ctypes.windll 存在）。"""

    name = "windows-credential-manager"
    probe = "advapi32!CredReadW"

    def available(self) -> bool:
        return self.platform.os == SYSTEM_WINDOWS and _windll_available()


class LibsecretStore(OsCredentialStore):
    """Linux libsecret：需要 ``secret-tool`` 且当前会话有 DBus（两者缺一不可）。"""

    name = "linux-libsecret"
    probe = "secret-tool + DBUS_SESSION_BUS_ADDRESS"

    def available(self) -> bool:
        return (self.platform.os == SYSTEM_LINUX
                and self._has_binary("secret-tool")
                and bool(str(self._env().get("DBUS_SESSION_BUS_ADDRESS") or "").strip()))


def _windll_available() -> bool:
    try:
        import ctypes
    except Exception:                                     # pragma: no cover - 极端裁剪环境
        return False
    return hasattr(ctypes, "windll")


def _os_stores(**kwargs: Any) -> tuple[OsCredentialStore, ...]:
    return (MacKeychainStore(**kwargs), WindowsCredentialManagerStore(**kwargs),
            LibsecretStore(**kwargs))


def probe_backends(*, platform: PlatformInfo | None = None,
                   environ: Mapping[str, str] | None = None,
                   which: Any = None) -> dict[str, bool]:
    """探测各后端可用性；文件后端恒为 True（不需要任何系统组件）。"""
    result: dict[str, bool] = {FILE_BACKEND: True}
    for store in _os_stores(platform=platform, environ=environ, which=which):
        result[store.name] = store.available()
    return result


def default_backend(paths: PathsLike) -> FileBackend:
    """默认后端：文件后端（S1 行为不变；OS 存储只探测、不自动切换）。"""
    backend = FileBackend(paths)
    if not backend.available():                          # pragma: no cover - 文件后端恒可用
        raise AuthError("文件凭据后端不可用")
    return backend
