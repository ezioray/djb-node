# SPDX-License-Identifier: Apache-2.0
"""三端平台抽象（C2）：识别当前平台，供 paths / service / credentials 三层共用。

设计边界
========

* 本层只回答「在哪跑、什么架构、是不是 WSL / 容器」，不决定目录（platform.paths）、
  不渲染服务（platform.service）、不碰凭据（platform.credentials）。
* **纯函数优先**：只读 ``sys.platform`` / ``platform.machine()`` / ``os.environ`` 与少量
  只读探针文件；不建目录、不写文件、不执行子进程、不提权。副作用只出现在
  :meth:`djb_node.platform.paths.PlatformPaths.prepare` 与凭据后端的显式写操作里。
* 平台判定可注入（``system=`` / ``machine=`` / ``environ=`` / ``probe_files=``），
  测试可覆盖三平台分支而不依赖宿主 OS；真实运行时用默认参数即可。
* **Linux 行为零变化**：Linux 归一化为 ``"linux"``，未知 POSIX 归为 ``"other"``，
  两者在 paths 层共用同一套 ``~/.djb`` 布局（与 S1 逐字一致）。

归一化口径
==========

``PlatformInfo.os`` 只取四个值：

    linux    sys.platform 以 linux 开头（含 linux2）
    darwin   sys.platform == darwin（macOS）
    windows  win32 / win64 / cygwin / msys
    other    其它（FreeBSD / AIX / SunOS …）；目录布局按 POSIX 处理

``PlatformInfo.arch`` 只做别名归一（AMD64→x86_64、arm64→aarch64），未知值原样小写返回；
是否「受支持架构」由 envcheck 结合 facts.SUPPORTED_ARCHES 判定，不在本层下结论。
"""
from __future__ import annotations

import os
import platform as _stdlib_platform
import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping

__all__ = [
    "PlatformInfo",
    "current_platform",
    "get_platform",
    "normalize_system",
    "normalize_arch",
    "SYSTEM_LINUX",
    "SYSTEM_DARWIN",
    "SYSTEM_WINDOWS",
    "SYSTEM_OTHER",
    "KNOWN_SYSTEMS",
]

SYSTEM_LINUX = "linux"
SYSTEM_DARWIN = "darwin"
SYSTEM_WINDOWS = "windows"
SYSTEM_OTHER = "other"
KNOWN_SYSTEMS: tuple[str, ...] = (SYSTEM_LINUX, SYSTEM_DARWIN, SYSTEM_WINDOWS, SYSTEM_OTHER)

_LINUX_ALIASES = frozenset({"linux", "linux2", "linux3"})
_DARWIN_ALIASES = frozenset({"darwin", "macos", "mac-os", "osx"})
_WINDOWS_ALIASES = frozenset({"win32", "win64", "windows", "cygwin", "msys", "msys2"})

_ARCH_ALIASES: dict[str, str] = {
    "amd64": "x86_64",
    "x86-64": "x86_64",
    "x64": "x86_64",
    "x86_64": "x86_64",
    "arm64": "aarch64",
    "arm64e": "aarch64",
    "aarch64": "aarch64",
    "i386": "x86",
    "i686": "x86",
}

#: WSL 的环境标记（任一非空即认为在 WSL 里）
_WSL_ENV_KEYS: tuple[str, ...] = ("WSL_DISTRO_NAME", "WSL_INTEROP", "WSLENV")
#: WSL 的只读内核文本标记（environ 无标记时才读 /proc，仅受 probe_files 控制）
_WSL_TEXT_MARKERS: tuple[str, ...] = ("microsoft", "wsl")

#: 容器的环境标记
_CONTAINER_ENV_KEYS: tuple[str, ...] = (
    "KUBERNETES_SERVICE_HOST", "DOCKER_CONTAINER", "container")
#: 容器的只读文件标记（docker / podman 会在根目录留空文件）
_CONTAINER_MARKER_FILES: tuple[str, ...] = ("/.dockerenv", "/run/.containerenv")
#: cgroup 文本里的容器运行时关键字
_CONTAINER_CGROUP_MARKERS: tuple[str, ...] = (
    "docker", "containerd", "kubepods", "lxc", "podman", "libpod", "buildkit")


def normalize_system(raw: str | None) -> str:
    """把 ``sys.platform`` / 外部输入归一化为 linux / darwin / windows / other。"""
    text = (raw or "").strip().lower()
    if not text:
        return SYSTEM_OTHER
    if text in _LINUX_ALIASES or text.startswith("linux"):
        return SYSTEM_LINUX
    if text in _DARWIN_ALIASES or text.startswith("darwin"):
        return SYSTEM_DARWIN
    if text in _WINDOWS_ALIASES or text.startswith("win"):
        return SYSTEM_WINDOWS
    return SYSTEM_OTHER


def normalize_arch(raw: str | None) -> str:
    """架构别名归一（只做等价替换；未知值原样小写返回，不猜）。"""
    text = (raw or "").strip().lower()
    if not text:
        return "unknown"
    return _ARCH_ALIASES.get(text, text)


@dataclass(frozen=True)
class PlatformInfo:
    """一次平台判定的不可变结果（os / arch / is_wsl / is_container）。

    构造时字段会被归一化，因此 ``PlatformInfo("win32", "AMD64")`` 等价于
    ``PlatformInfo("windows", "x86_64")``；判定逻辑本身在 :meth:`detect`。
    """

    os: str
    arch: str
    is_wsl: bool = False
    is_container: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "os", normalize_system(self.os))
        object.__setattr__(self, "arch", normalize_arch(self.arch))
        object.__setattr__(self, "is_wsl", bool(self.is_wsl))
        object.__setattr__(self, "is_container", bool(self.is_container))

    @property
    def is_linux(self) -> bool:
        return self.os == SYSTEM_LINUX

    @property
    def is_darwin(self) -> bool:
        return self.os == SYSTEM_DARWIN

    @property
    def is_windows(self) -> bool:
        return self.os == SYSTEM_WINDOWS

    def to_dict(self) -> dict[str, Any]:
        """给 status / diagnose / 日志用的投影（无凭据、无路径）。"""
        return {"os": self.os, "arch": self.arch, "is_wsl": self.is_wsl,
                "is_container": self.is_container}

    @classmethod
    def detect(cls, *, system: str | None = None, machine: str | None = None,
               environ: Mapping[str, str] | None = None,
               probe_files: bool = True) -> "PlatformInfo":
        """按给定（或当前）环境做一次判定；``probe_files=False`` 时完全不读文件。"""
        env = os.environ if environ is None else environ
        normalized = normalize_system(sys.platform if system is None else system)
        arch = normalize_arch(machine if machine is not None else _machine())
        return cls(
            os=normalized,
            arch=arch,
            is_wsl=_detect_wsl(normalized, env, probe_files),
            is_container=_detect_container(env, probe_files),
        )


def current_platform(*, system: str | None = None, machine: str | None = None,
                     environ: Mapping[str, str] | None = None,
                     probe_files: bool = True) -> PlatformInfo:
    """重新判定一次当前平台（不缓存；纯读取，无副作用）。"""
    return PlatformInfo.detect(system=system, machine=machine, environ=environ,
                               probe_files=probe_files)


@lru_cache(maxsize=1)
def get_platform() -> PlatformInfo:
    """进程级缓存的当前平台（平台在进程生命周期内不变）。

    测试可 ``get_platform.cache_clear()``；需要反映 monkeypatch 的调用方请直接用
    :func:`current_platform`。
    """
    return current_platform()


def _machine() -> str:
    try:
        return _stdlib_platform.machine() or ""
    except Exception:                                          # pragma: no cover - 极少数平台
        return ""


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _exists(path: str) -> bool:
    """只读探针（单独成函数，测试可替换而不影响进程里其它 os.path 调用）。"""
    return os.path.exists(path)


def _detect_wsl(system: str, environ: Mapping[str, str], probe_files: bool) -> bool:
    if system != SYSTEM_LINUX:
        return False
    for key in _WSL_ENV_KEYS:
        if str(environ.get(key) or "").strip():
            return True
    if not probe_files:
        return False
    text = (_read_text("/proc/sys/kernel/osrelease") + _read_text("/proc/version")).lower()
    return any(marker in text for marker in _WSL_TEXT_MARKERS)


def _detect_container(environ: Mapping[str, str], probe_files: bool) -> bool:
    for key in _CONTAINER_ENV_KEYS:
        if str(environ.get(key) or "").strip():
            return True
    if not probe_files:
        return False
    if any(_exists(marker) for marker in _CONTAINER_MARKER_FILES):
        return True
    cgroup = _read_text("/proc/self/cgroup").lower()
    return any(marker in cgroup for marker in _CONTAINER_CGROUP_MARKERS)
