# SPDX-License-Identifier: Apache-2.0
"""平台目录布局（C2）：三端壳唯一的「东西放哪」来源，Linux 与 S1 逐字一致。

覆盖规则（优先级从高到低）
==========================

1. ``--home`` / ``PlatformPaths.from_env(explicit=...)`` 显式参数；
2. ``DJB_HOME`` 环境变量；
3. 平台默认（见下表）。

1 / 2 都是「单根模式」：所有目录都落在同一个根下——与 S1 的 ``config.Paths(home)``
完全一致，三端也一样。这样测试、便携数据目录、用户自选位置都不需要额外分支。

平台默认布局
============

* Linux / 其它 POSIX：config / state = ``~/.djb``，logs = ``~/.djb/logs``，
  cache = ``~/.djb/cache``（与 S1 逐字一致）；
* macOS：config / state = ``~/Library/Application Support/DJB``，
  logs = ``~/Library/Logs/DJB``，cache = ``~/Library/Caches/DJB``；
* Windows：config = ``%APPDATA%\\DJB``，state / logs / cache 都在 ``%LOCALAPPDATA%\\DJB``
  下（logs / cache 是其子目录）。

Windows 把 machine-local 的 state / logs / cache 放 ``%LOCALAPPDATA%``（roaming 配置不跟随
漫游目录复制库与日志）；macOS 把日志与缓存拆到 ``Logs`` / ``Caches`` 两个系统约定目录。

文件归属
========

* ``config_dir``：``config.json``、``credentials/credentials.json``、``keys/``
* ``state_dir``：``state.db``、``runtime/``、``versions/``、``diagnostics/``
* ``logs_dir``：``djb.log``（文件名取自 facts.LOG_FILE）
* ``cache_dir``：可随时丢弃的缓存

设计边界
========

* 本模块只在内存里拼字符串：除 :meth:`PlatformPaths.prepare` 显式创建 0700 目录外
  不落盘、不执行命令、不改系统目录；测试用 ``DJB_HOME`` 指向 tmp，绝不碰真实目录。
* ``config.Paths`` 仍保持单根（S1 契约）；三端壳请直接用 ``PlatformPaths``。
* 路径分隔符跟随平台：Windows 分支用 ``ntpath``（即使当前宿主不是 Windows），
  因此渲染 Windows 服务参数时路径也是反斜杠，不依赖生成它的机器。
"""
from __future__ import annotations

import ntpath
import os
import posixpath
from dataclasses import dataclass
from typing import Any, Mapping

from .. import facts
from ..errors import UsageError
from ..util import ensure_dir, file_mode
from . import SYSTEM_DARWIN, SYSTEM_WINDOWS, PlatformInfo, current_platform

__all__ = [
    "PlatformPaths",
    "default_home",
    "default_paths",
    "APP_DIRNAME",
    "DJB_HOME_ENV",
    "LINUX_HOME_DIRNAME",
]

#: 三端共用的应用目录名（macOS / Windows 用；Linux 仍是 ~/.djb）
APP_DIRNAME = "DJB"
#: 与 config.DEFAULT_HOME_DIRNAME 一致；本层不 import config，避免循环依赖
LINUX_HOME_DIRNAME = ".djb"
#: 覆盖根目录的环境变量（与 config.resolve_home 同一口径）
DJB_HOME_ENV = "DJB_HOME"


def _pathmod(system: str) -> Any:
    """按平台选路径模块：Windows 用 ntpath，其余用 posixpath（Linux 上即 os.path）。"""
    return ntpath if system == SYSTEM_WINDOWS else posixpath


def _abspath(pathmod: Any, path: str) -> str:
    return pathmod.abspath(pathmod.expanduser(path))


@dataclass(frozen=True)
class PlatformPaths:
    """一次平台布局的不可变快照；所有属性都是纯字符串计算。"""

    platform: PlatformInfo
    source: str                 # arg / env / platform（谁决定了布局，诊断用）
    config_dir: str
    state_dir: str
    logs_dir: str
    cache_dir: str

    def __post_init__(self) -> None:
        for name in ("config_dir", "state_dir", "logs_dir", "cache_dir"):
            if not str(getattr(self, name) or "").strip():
                raise UsageError("PlatformPaths." + name + " 不能为空")

    # ─────────────────────────────────────────────────── 构造
    @classmethod
    def from_env(cls, explicit: str | None = None, *,
                 platform: PlatformInfo | None = None,
                 environ: Mapping[str, str] | None = None) -> "PlatformPaths":
        """按 --home > DJB_HOME > 平台默认 的顺序构造布局。

        ``environ`` 只用于 ``DJB_HOME`` / ``APPDATA`` / ``LOCALAPPDATA`` 这类平台变量；
        ``~`` 的展开与 S1 同源（``os.path.expanduser``，跟随宿主 ``os.environ``）。
        """
        env = os.environ if environ is None else environ
        info = platform or current_platform(environ=env)
        candidate = explicit or env.get(DJB_HOME_ENV) or ""
        if candidate:
            return cls._from_override(info, str(candidate),
                                      source="arg" if explicit else "env")
        return cls._platform_default(info, env)

    @classmethod
    def _from_override(cls, info: PlatformInfo, root: str, *, source: str) -> "PlatformPaths":
        pathmod = _pathmod(info.os)
        base = _abspath(pathmod, root)
        return cls(platform=info, source=source, config_dir=base, state_dir=base,
                   logs_dir=pathmod.join(base, "logs"),
                   cache_dir=pathmod.join(base, "cache"))

    @classmethod
    def _platform_default(cls, info: PlatformInfo,
                          environ: Mapping[str, str]) -> "PlatformPaths":
        pathmod = _pathmod(info.os)
        if info.os == SYSTEM_WINDOWS:
            user = pathmod.expanduser("~")
            appdata = environ.get("APPDATA") or pathmod.join(user, "AppData", "Roaming")
            local = environ.get("LOCALAPPDATA") or pathmod.join(user, "AppData", "Local")
            config_dir = _abspath(pathmod, pathmod.join(str(appdata), APP_DIRNAME))
            state_dir = _abspath(pathmod, pathmod.join(str(local), APP_DIRNAME))
            return cls(platform=info, source="platform", config_dir=config_dir,
                       state_dir=state_dir, logs_dir=pathmod.join(state_dir, "logs"),
                       cache_dir=pathmod.join(state_dir, "cache"))
        if info.os == SYSTEM_DARWIN:
            library = pathmod.join(pathmod.expanduser("~"), "Library")
            config_dir = _abspath(pathmod,
                                  pathmod.join(library, "Application Support", APP_DIRNAME))
            return cls(platform=info, source="platform", config_dir=config_dir,
                       state_dir=config_dir,
                       logs_dir=_abspath(pathmod, pathmod.join(library, "Logs", APP_DIRNAME)),
                       cache_dir=_abspath(pathmod, pathmod.join(library, "Caches",
                                                                APP_DIRNAME)))
        root = _abspath(pathmod, pathmod.join(pathmod.expanduser("~"), LINUX_HOME_DIRNAME))
        return cls(platform=info, source="platform", config_dir=root, state_dir=root,
                   logs_dir=pathmod.join(root, "logs"),
                   cache_dir=pathmod.join(root, "cache"))

    # ─────────────────────────────────────────────────── 视图
    @property
    def pathmod(self) -> Any:
        """该平台对应的路径模块（ntpath / posixpath）。"""
        return _pathmod(self.platform.os)

    @property
    def home(self) -> str:
        """兼容别名：config.json 所在的目录（Linux 上等于 S1 的 home）。"""
        return self.config_dir

    @property
    def config_file(self) -> str:
        return self.pathmod.join(self.config_dir, "config.json")

    @property
    def credentials_file(self) -> str:
        return self.pathmod.join(self.config_dir, "credentials", "credentials.json")

    @property
    def keys_dir(self) -> str:
        return self.pathmod.join(self.config_dir, "keys")

    @property
    def buffer_key_file(self) -> str:
        return self.pathmod.join(self.keys_dir, "buffer.key")

    @property
    def provider_key_file(self) -> str:
        return self.pathmod.join(self.keys_dir, "receipt_provider.key")

    @property
    def db_file(self) -> str:
        return self.pathmod.join(self.state_dir, "state.db")

    @property
    def runtime_dir(self) -> str:
        return self.pathmod.join(self.state_dir, "runtime")

    @property
    def socket_file(self) -> str:
        return self.pathmod.join(self.runtime_dir, "daemon.sock")

    @property
    def pid_file(self) -> str:
        return self.pathmod.join(self.runtime_dir, "daemon.pid")

    @property
    def versions_dir(self) -> str:
        return self.pathmod.join(self.state_dir, "versions")

    @property
    def diagnostics_dir(self) -> str:
        return self.pathmod.join(self.state_dir, "diagnostics")

    @property
    def log_file(self) -> str:
        return self.pathmod.join(self.logs_dir, facts.LOG_FILE)

    # ─────────────────────────────────────────────────── 落盘（唯一副作用，显式调用）
    def prepare(self) -> None:
        """创建 0700 目录树（幂等；不创建文件）。

        Windows 上 chmod 基本是空操作，``ensure_dir`` 已忽略 OSError；POSIX 上严格 0700。
        """
        for directory in (self.config_dir, self.pathmod.dirname(self.credentials_file),
                          self.keys_dir, self.state_dir, self.logs_dir,
                          self.runtime_dir, self.versions_dir, self.diagnostics_dir,
                          self.cache_dir):
            ensure_dir(directory, 0o700)

    def existing(self) -> bool:
        """本机是否已有安装（目录 + state.db 都存在）；只对宿主真实路径有意义。"""
        return os.path.isdir(self.config_dir) and os.path.exists(self.db_file)

    def snapshot(self) -> dict[str, Any]:
        """诊断 / 卸载自检用：路径与权限（不含内容）。"""
        return {
            "home": self.home,
            "data_dir": self.state_dir,
            "logs_dir": self.logs_dir,
            "cache_dir": self.cache_dir,
            "db_file": self.db_file,
            "db_mode": oct(file_mode(self.db_file) or 0),
            "credentials_file": self.credentials_file,
            "credentials_mode": oct(file_mode(self.credentials_file) or 0),
            "config_mode": oct(file_mode(self.config_file) or 0),
        }

    def to_dict(self) -> dict[str, Any]:
        """给 status / diagnose 的投影（无凭据、无文件内容）。"""
        return {
            "source": self.source,
            "platform": self.platform.to_dict(),
            "config_dir": self.config_dir,
            "state_dir": self.state_dir,
            "logs_dir": self.logs_dir,
            "cache_dir": self.cache_dir,
        }


def default_paths(*, platform: PlatformInfo | None = None,
                  environ: Mapping[str, str] | None = None) -> PlatformPaths:
    """平台默认布局（忽略 --home / DJB_HOME；纯字符串，不落盘）。"""
    env = os.environ if environ is None else environ
    info = platform or current_platform(environ=env)
    return PlatformPaths._platform_default(info, env)


def default_home(explicit: str | None = None, *,
                 platform: PlatformInfo | None = None,
                 environ: Mapping[str, str] | None = None) -> str:
    """``config.json`` 所在的目录，按 ``--home > DJB_HOME > 平台默认`` 解析。

    这是 ``config.resolve_home()`` 的等价实现（Linux 逐字一致，测试已逐项比对）；
    ``config`` 侧是否接线由该模块决定，本层只提供纯函数、不改既有行为。

    * 有 ``explicit`` / ``DJB_HOME`` → 单根模式（各平台一致，与 S1 的
      ``os.path.abspath(os.path.expanduser(value))`` 逐字相同）；
    * 否则 Linux / 未知 POSIX → ``~/.djb``（与 S1 逐字一致），
      macOS → ``~/Library/Application Support/DJB``，Windows → ``%APPDATA%\\DJB``。
    """
    return PlatformPaths.from_env(explicit, platform=platform, environ=environ).config_dir
