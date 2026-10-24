# SPDX-License-Identifier: Apache-2.0
"""C4 服务安装/卸载计划（纯数据）：三端命令 + 待落盘文件内容，**不执行、不提权**。

设计边界
========

* **只描述、不执行**：本模块不 import subprocess / os / shutil / ctypes，不调用
  ``systemctl`` / ``launchctl`` / ``sc.exe``，不写 ``/etc`` / ``~/Library`` / 注册表，
  也不生成 sudo / runas。是否执行、何时执行、由谁批准，属于后续安装器 / C4 壳。
* **文件内容随计划返回**：unit / plist / PowerShell 脚本放在 :attr:`ServicePlan.files`
  （path + content + mode），由安装器按 ``mode`` 落盘；本函数自身不创建任何文件。
* ``requires_admin`` 只做标注：Windows SCM 的 ``sc.exe create`` 需要管理员权限；
  Linux / macOS 的用户级 unit / LaunchAgent 不需要 root。
* 结果**幂等**：同样的输入两次构建得到逐字相同的计划（无时间戳、无随机数）。
* ``paths`` 必须是同一平台的 :class:`~djb_node.platform.paths.PlatformPaths`（拿日志路径）；
  平台不一致直接报错，避免把 Windows 路径写进 systemd 单元。
"""
from __future__ import annotations

import ntpath
import posixpath
import shlex
from dataclasses import dataclass
from typing import Any

from .. import facts
from ..errors import UsageError
from . import (SYSTEM_DARWIN, SYSTEM_LINUX, SYSTEM_WINDOWS, PlatformInfo,
               normalize_system)
from .service import (daemon_service_spec, launchd_plist_name, render_launchd_plist,
                      render_systemd_unit, render_windows_sc_commands, systemd_unit_name)

__all__ = [
    "PlanFile",
    "ServicePlan",
    "build_service_plan",
    "DAEMON_SERVICE_NAME",
]

#: 三端统一的服务名（systemd unit / launchd Label / SCM 服务名由此派生）
DAEMON_SERVICE_NAME = "djb-node"
_PLAN_PLATFORMS = (SYSTEM_LINUX, SYSTEM_DARWIN, SYSTEM_WINDOWS)
_POWERSHELL_SCRIPT_NAME = "djb-node-service.ps1"


@dataclass(frozen=True)
class PlanFile:
    """计划落盘的一份文件（安装器负责写；本层只给内容与权限）。"""

    path: str
    content: str
    mode: str = "0644"

    def __post_init__(self) -> None:
        path = str(self.path or "").strip()
        if not path:
            raise UsageError("PlanFile.path 不能为空")
        if not isinstance(self.content, str) or not self.content:
            raise UsageError("PlanFile.content 不能为空")
        mode = str(self.mode or "").strip()
        if len(mode) != 4 or any(char not in "01234567" for char in mode):
            raise UsageError("PlanFile.mode 必须是 4 位八进制（例如 0644）")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "mode", mode)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "mode": self.mode, "content": self.content}


def _commands(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    items = tuple(str(value).strip() for value in values)
    if not items:
        raise UsageError(field + " 不能为空")
    if any(not item for item in items):
        raise UsageError(field + " 不能含空命令")
    for item in items:
        if "\n" in item or "\r" in item or "\x00" in item:
            raise UsageError(field + " 的单条命令不能含换行 / NUL")
    return items


@dataclass(frozen=True)
class ServicePlan:
    """三端服务安装/卸载计划（命令与文件内容都是纯字符串）。"""

    platform: str
    install_cmds: tuple[str, ...]
    uninstall_cmds: tuple[str, ...]
    verify_cmds: tuple[str, ...]
    notes: tuple[str, ...] = ()
    requires_admin: bool = False
    files: tuple[PlanFile, ...] = ()

    def __post_init__(self) -> None:
        system = normalize_system(self.platform)
        if system not in _PLAN_PLATFORMS:
            raise UsageError("ServicePlan.platform 只支持 linux / darwin / windows："
                             + repr(self.platform))
        object.__setattr__(self, "platform", system)
        object.__setattr__(self, "install_cmds", _commands(self.install_cmds,
                                                           "install_cmds"))
        object.__setattr__(self, "uninstall_cmds", _commands(self.uninstall_cmds,
                                                             "uninstall_cmds"))
        object.__setattr__(self, "verify_cmds", _commands(self.verify_cmds,
                                                          "verify_cmds"))
        object.__setattr__(self, "notes", tuple(str(note) for note in self.notes))
        if not isinstance(self.requires_admin, bool):
            raise UsageError("ServicePlan.requires_admin 必须是 bool")
        object.__setattr__(self, "requires_admin", bool(self.requires_admin))
        object.__setattr__(self, "files", tuple(self.files))

    @property
    def all_commands(self) -> tuple[str, ...]:
        return self.install_cmds + self.uninstall_cmds + self.verify_cmds

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "requires_admin": self.requires_admin,
            "install_cmds": list(self.install_cmds),
            "uninstall_cmds": list(self.uninstall_cmds),
            "verify_cmds": list(self.verify_cmds),
            "files": [item.to_dict() for item in self.files],
            "notes": list(self.notes),
        }

    def render_text(self) -> str:
        """纯文本计划（给人看 / 落日志；不执行）。"""
        lines = [
            "DJB 联脑 · " + self.platform + " 服务安装计划（需要管理员："
            + ("是" if self.requires_admin else "否") + "）",
        ]
        if self.files:
            lines.append("文件（先由安装器写盘，再执行命令）：")
            lines.extend("  - " + item.path + " (" + item.mode + ")" for item in self.files)
        lines.append("安装命令：")
        lines.extend("  " + str(index) + ") " + command
                     for index, command in enumerate(self.install_cmds, 1))
        lines.append("卸载命令：")
        lines.extend("  " + str(index) + ") " + command
                     for index, command in enumerate(self.uninstall_cmds, 1))
        lines.append("校验命令：")
        lines.extend("  " + str(index) + ") " + command
                     for index, command in enumerate(self.verify_cmds, 1))
        if self.notes:
            lines.append("备注：")
            lines.extend("  - " + note for note in self.notes)
        return "\n".join(lines)


def _resolve_system(platform: Any) -> str:
    if isinstance(platform, PlatformInfo):
        system = platform.os
    else:
        system = normalize_system(str(platform or ""))
    if system not in _PLAN_PLATFORMS:
        raise UsageError("build_service_plan 只覆盖 linux / darwin / windows："
                         + repr(platform))
    return system


def _paths_system(paths: Any) -> str | None:
    info = getattr(paths, "platform", None)
    return getattr(info, "os", None)


def _log_file(paths: Any, system: str, home: str) -> str:
    value = getattr(paths, "log_file", None)
    if isinstance(value, str) and value.strip():
        return value
    if system == SYSTEM_WINDOWS:                           # pragma: no cover - 兜底
        return ntpath.join(home, "logs", facts.LOG_FILE)
    if system == SYSTEM_DARWIN:                            # pragma: no cover - 兜底
        return posixpath.join(posixpath.expanduser("~"), "Library", "Logs",
                              "DJB", facts.LOG_FILE)
    return posixpath.join(home, "logs", facts.LOG_FILE)


def _default_unit_dir(system: str) -> str:
    if system == SYSTEM_DARWIN:
        return posixpath.join(posixpath.expanduser("~"), "Library", "LaunchAgents")
    return posixpath.join(posixpath.expanduser("~"), ".config", "systemd", "user")


def _powershell_plan(spec: Any, install_cmds: tuple[str, ...]) -> str:
    lines = [
        "# DJB 联脑 · Windows 服务安装计划（C4 脚手架生成；纯文本，本仓不执行）",
        "# 需要管理员权限：以「管理员身份运行 PowerShell」后执行本脚本。",
        "$ErrorActionPreference = 'Stop'",
        *install_cmds,
        "sc.exe query " + spec.name,
    ]
    return "\n".join(lines) + "\n"


def build_service_plan(platform: Any, paths: Any, python_executable: str,
                       home: str, *, unit_dir: str | None = None) -> ServicePlan:
    """构建三端服务安装计划（纯数据；不执行、不写文件、不提权）。

    ``platform`` 可传 ``PlatformInfo`` 或 ``"linux"`` / ``"darwin"`` / ``"win32"`` 字符串；
    ``paths`` 传同平台 ``PlatformPaths``（用其日志路径）；``unit_dir`` 仅供安装器/测试指定
    unit / plist 的目标目录，默认用系统约定目录。
    """
    system = _resolve_system(platform)
    actual = _paths_system(paths)
    if actual is not None and actual != system:
        raise UsageError("paths 与 platform 不一致：" + str(actual) + " vs " + system)
    executable = str(python_executable or "").strip()
    if not executable:
        raise UsageError("python_executable 不能为空")
    root = str(home or "").strip()
    if not root:
        raise UsageError("home 不能为空")

    log_file = _log_file(paths, system, root)
    spec = daemon_service_spec(root, python=executable, log_file=log_file,
                               name=DAEMON_SERVICE_NAME)
    shared_note = ("本计划只描述命令与文件内容：先按 files[] 写盘（path/mode/content），"
                   "再执行 install_cmds；本层不执行、不提权。")

    if system == SYSTEM_LINUX:
        directory = str(unit_dir or _default_unit_dir(system))
        unit_path = posixpath.join(directory, systemd_unit_name(spec))
        unit_name = systemd_unit_name(spec)
        return ServicePlan(
            platform=system,
            install_cmds=(
                "systemctl --user daemon-reload",
                "systemctl --user enable --now " + unit_name,
            ),
            uninstall_cmds=(
                "systemctl --user disable --now " + unit_name,
                "systemctl --user daemon-reload",
                "rm -f " + shlex.quote(unit_path),
            ),
            verify_cmds=(
                "systemctl --user is-enabled " + unit_name,
                "systemctl --user is-active " + unit_name,
                "djb status --json",
            ),
            notes=(
                shared_note,
                "用户级 unit：不需要 root；unit 文件 " + unit_path,
                "无图形登录会话也要常驻时，由用户自行执行 loginctl enable-linger <user>",
            ),
            requires_admin=False,
            files=(PlanFile(unit_path, render_systemd_unit(spec)),),
        )

    if system == SYSTEM_DARWIN:
        directory = str(unit_dir or _default_unit_dir(system))
        plist_path = posixpath.join(directory, launchd_plist_name(spec))
        domain = "gui/$(id -u)"
        return ServicePlan(
            platform=system,
            install_cmds=(
                "launchctl bootstrap " + domain + " " + shlex.quote(plist_path),
                "launchctl enable " + domain + "/" + spec.label,
                "launchctl kickstart -k " + domain + "/" + spec.label,
            ),
            uninstall_cmds=(
                "launchctl bootout " + domain + "/" + spec.label,
                "rm -f " + shlex.quote(plist_path),
            ),
            verify_cmds=(
                "launchctl print " + domain + "/" + spec.label,
                "djb status --json",
            ),
            notes=(
                shared_note,
                "LaunchAgent 只对当前登录用户生效，不需要 sudo；plist 文件 " + plist_path,
                "命令里的 $(id -u) 由壳在执行时展开（本层不执行）",
            ),
            requires_admin=False,
            files=(PlanFile(plist_path, render_launchd_plist(spec)),),
        )

    sc_commands = render_windows_sc_commands(spec)
    script_path = ntpath.join(root, _POWERSHELL_SCRIPT_NAME)
    return ServicePlan(
        platform=system,
        install_cmds=sc_commands,
        uninstall_cmds=(
            "sc.exe stop " + spec.name,
            "sc.exe delete " + spec.name,
        ),
        verify_cmds=(
            "sc.exe query " + spec.name,
            "sc.exe qc " + spec.name,
            "djb status --json",
        ),
        notes=(
            shared_note,
            "sc.exe create / delete 需要管理员权限（requires_admin=True）",
            "PowerShell 计划脚本需以管理员身份运行；本仓只生成文本，不执行也不提权",
            "SCM 不支持逐服务环境变量：数据目录通过 argv 的 --home 传入",
        ),
        requires_admin=True,
        files=(PlanFile(script_path, _powershell_plan(spec, sc_commands)),),
    )
