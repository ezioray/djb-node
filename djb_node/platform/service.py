# SPDX-License-Identifier: Apache-2.0
"""服务定义渲染（C2）：systemd / launchd / Windows SCM 的**纯字符串**描述。

硬边界
======

* 本模块**只拼字符串**：不 import subprocess、不执行 ``systemctl`` / ``launchctl`` /
  ``sc.exe``、不写 ``/etc`` / ``~/Library`` / 注册表、不提权（不生成 sudo / runas / /RU）。
  是否安装、什么时候安装、由谁批准，都是三端壳 / 安装器的事。
* 渲染结果必须能直接落成单元文件（systemd / launchd）或由用户/安装器复制执行（Windows）。
* 所有输入在 :class:`ServiceSpec` 构造时校验：服务名白名单、argv 非空且无换行 / NUL、
  环境变量键名合法。非法输入抛 ``djb_node.errors.UsageError``（错误体系与 CLI 一致）。

三端差异（本层显式建模，不猜）
==============================

* systemd → ``~/.config/systemd/user/<name>.service``：用户级单元，
  ``WantedBy=default.target``，失败按 ``RestartSec`` 重启；
* launchd → ``~/Library/LaunchAgents/<label>.plist``：``RunAtLoad`` + ``KeepAlive``，
  由 ``ThrottleInterval`` 控制重启间隔（launchd 最短 10 s）；
* Windows SCM → ``sc.exe create ...``：自启 + ``sc.exe failure`` 三档重启。
  ``sc.exe`` 不支持逐服务环境变量，需要传参请写进 argv（如 ``--home``）。

日志：systemd 用 ``StandardOutput=append:``（systemd ≥ 240），launchd 用
``StandardOutPath``；Windows SCM 没有逐服务重定向，daemon 自己写日志文件。
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from xml.sax.saxutils import escape as _xml_escape

from ..errors import UsageError

__all__ = [
    "ServiceSpec",
    "daemon_service_spec",
    "render_systemd_unit",
    "render_launchd_plist",
    "render_windows_sc_commands",
    "systemd_unit_name",
    "launchd_plist_name",
    "SYSTEMD_UNIT_SUFFIX",
    "LAUNCHD_LABEL_PREFIX",
]

SYSTEMD_UNIT_SUFFIX = ".service"
LAUNCHD_LABEL_PREFIX = "com.djb."
#: 服务名 / 单元名的白名单（systemd 单元名、launchd Label、SCM 服务名都满足）
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
#: 环境变量键名
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
#: systemd 里不需要引号的参数（其余一律加双引号）
_SYSTEMD_SAFE_ARG_RE = re.compile(r"^[A-Za-z0-9_./:=@+,-]+$")
_FORBIDDEN_CHARS = ("\r", "\n", "\x00")


@dataclass(frozen=True)
class ServiceSpec:
    """一份服务描述（与平台无关）；三个渲染器各自把它投影成目标平台的写法。

    ``env`` 只对 POSIX（systemd / launchd）生效：``sc.exe`` 不支持逐服务环境变量，
    Windows 侧请把配置写进 ``argv``（例如 ``--home``），渲染时会显式报错而不是静默丢弃。
    """

    name: str
    argv: tuple[str, ...]
    display_name: str = "DJB 联脑 节点客户端"
    description: str = "DJB 联脑共享节点客户端（本机镜像 + daemon）"
    env: Mapping[str, str] | Sequence[tuple[str, str]] = ()
    workdir: str | None = None
    log_file: str | None = None
    restart_seconds: int = 5
    label: str = ""

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if not _NAME_RE.match(name):
            raise UsageError("服务名不合法：" + repr(self.name)
                             + "（字母数字开头，只含 _ . -，长度 ≤64）")
        object.__setattr__(self, "name", name)

        argv = tuple(str(item) for item in (self.argv or ()))
        if not argv or any(not item for item in argv):
            raise UsageError("ServiceSpec.argv 不能为空，且每个参数必须是非空字符串")
        for item in argv:
            _require_single_line(item, "argv")
        object.__setattr__(self, "argv", argv)

        _require_single_line(self.display_name, "display_name")
        _require_single_line(self.description, "description")
        if self.workdir is not None:
            _require_single_line(self.workdir, "workdir")
        if self.log_file is not None:
            _require_single_line(self.log_file, "log_file")
        object.__setattr__(self, "env", _normalize_env(self.env))

        restart = int(self.restart_seconds)
        if restart < 0:
            raise UsageError("restart_seconds 不能为负")
        object.__setattr__(self, "restart_seconds", restart)

        label = str(self.label or "").strip() or (LAUNCHD_LABEL_PREFIX + name)
        if not _NAME_RE.match(label):
            raise UsageError("launchd Label 不合法：" + repr(label))
        object.__setattr__(self, "label", label)

    @property
    def env_dict(self) -> dict[str, str]:
        return dict(self.env)


def _require_single_line(value: Any, field: str) -> None:
    text = str(value)
    if any(char in text for char in _FORBIDDEN_CHARS):
        raise UsageError(field + " 不能含换行 / NUL")


def _normalize_env(env: Mapping[str, str] | Sequence[tuple[str, str]]
                   ) -> tuple[tuple[str, str], ...]:
    if not env:
        return ()
    items: Iterable[tuple[Any, Any]] = env.items() if isinstance(env, Mapping) else env
    pairs: list[tuple[str, str]] = []
    for key, value in items:
        key_text = str(key).strip()
        if not _ENV_KEY_RE.match(key_text):
            raise UsageError("环境变量名不合法：" + repr(key))
        value_text = str(value)
        _require_single_line(value_text, "env." + key_text)
        pairs.append((key_text, value_text))
    return tuple(sorted(pairs))


# ─────────────────────────────────────────────────────────── systemd

def systemd_unit_name(spec: ServiceSpec) -> str:
    """用户级单元文件名（``~/.config/systemd/user/`` 下）。"""
    return spec.name + SYSTEMD_UNIT_SUFFIX


def _systemd_escape_token(value: str) -> str:
    """systemd 的 token 转义：``\\`` / ``"`` 需转义，``$`` 会被变量展开，``%`` 是说明符。"""
    text = value.replace("\\", "\\\\").replace('"', '\\"')
    return text.replace("$", "$$").replace("%", "%%")


def _systemd_arg(value: str) -> str:
    escaped = _systemd_escape_token(value)
    if _SYSTEMD_SAFE_ARG_RE.match(value):
        return escaped
    return '"' + escaped + '"'


def _systemd_env_value(value: str) -> str:
    # Environment= 不做变量展开，只处理引号与反斜杠（保持值逐字）
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_systemd_unit(spec: ServiceSpec) -> str:
    """渲染 systemd 用户单元；纯字符串，不落盘。"""
    lines = [
        "# 由 djb_node.platform.service 生成（C2）：纯字符串，未执行任何命令",
        "[Unit]",
        "Description=" + spec.description.replace("%", "%%"),
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=" + " ".join(_systemd_arg(item) for item in spec.argv),
        "Restart=on-failure",
        "RestartSec=" + str(spec.restart_seconds),
        "KillMode=mixed",
        "KillSignal=SIGTERM",
    ]
    if spec.workdir:
        lines.append("WorkingDirectory=" + _systemd_arg(spec.workdir))
    for key, value in spec.env:
        lines.append("Environment=" + _systemd_env_value(key + "=" + value))
    if spec.log_file:
        lines.append("StandardOutput=append:" + _systemd_arg(spec.log_file))
        lines.append("StandardError=append:" + _systemd_arg(spec.log_file))
    lines.extend(["", "[Install]", "WantedBy=default.target"])
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────── launchd

def launchd_plist_name(spec: ServiceSpec) -> str:
    """LaunchAgent plist 文件名（``~/Library/LaunchAgents/`` 下）。"""
    return spec.label + ".plist"


def _plist_string(value: str, indent: int = 1) -> str:
    return "  " * indent + "<string>" + _xml_escape(value) + "</string>"


def render_launchd_plist(spec: ServiceSpec) -> str:
    """渲染 macOS LaunchAgent plist（XML）；纯字符串，不落盘、不 launchctl load。"""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        "<dict>",
        _plist_string_key("Label"),
        _plist_string(spec.label, 2),
        _plist_string_key("ProgramArguments"),
        "  <array>",
    ]
    lines.extend(_plist_string(item, 3) for item in spec.argv)
    lines.extend([
        "  </array>",
        _plist_string_key("RunAtLoad"),
        "  <true/>",
        _plist_string_key("KeepAlive"),
        "  <dict>",
        _plist_string_key("SuccessfulExit", 2),
        "    <false/>",
        "  </dict>",
        # launchd 对反复退出的进程有最短 10 s 重启间隔；这里取与配置一致的显式值
        _plist_string_key("ThrottleInterval"),
        "  <integer>" + str(max(spec.restart_seconds, 10)) + "</integer>",
        _plist_string_key("ProcessType"),
        _plist_string("Background", 2),
    ])
    if spec.workdir:
        lines.extend([_plist_string_key("WorkingDirectory"), _plist_string(spec.workdir, 2)])
    if spec.env:
        lines.extend([_plist_string_key("EnvironmentVariables"), "  <dict>"])
        for key, value in spec.env:
            lines.extend([_plist_string_key(key, 2), _plist_string(value, 3)])
        lines.append("  </dict>")
    if spec.log_file:
        lines.extend([_plist_string_key("StandardOutPath"), _plist_string(spec.log_file, 2),
                      _plist_string_key("StandardErrorPath"), _plist_string(spec.log_file, 2)])
    lines.extend(["</dict>", "</plist>"])
    return "\n".join(lines) + "\n"


def _plist_string_key(key: str, indent: int = 1) -> str:
    return "  " * indent + "<key>" + _xml_escape(key) + "</key>"


# ─────────────────────────────────────────────────────────── Windows SCM

def _windows_command_line(argv: Sequence[str]) -> str:
    """Windows 命令行拼装（CreateProcess 口径的保守实现：含空格/引号的参数加引号）。"""
    parts: list[str] = []
    for item in argv:
        if item and not any(char in item for char in ' \t"'):
            parts.append(item)
        else:
            parts.append('"' + item.replace('"', '\\"') + '"')
    return " ".join(parts)


def _sc_quote(value: str) -> str:
    """``sc.exe`` 的取值：整体加引号，内部引号转义（sc 的 ``key= value`` 要求空格后取值）。"""
    return '"' + value.replace('"', '\\"') + '"'


def render_windows_sc_commands(spec: ServiceSpec) -> tuple[str, ...]:
    """渲染 Windows SCM 安装命令（字符串元组，需管理员权限由调用方自行决定）。

    ``sc.exe create`` / ``description`` / ``failure`` / ``start`` 四条命令：
    自启、失败按 5 s / 30 s / 300 s 三档重启、随后拉起。本函数不执行、不提权、
    不写注册表；``sc.exe`` 也不支持逐服务环境变量，``spec.env`` 非空时直接报错。
    """
    if spec.env:
        raise UsageError("Windows 服务不支持环境变量（sc.exe 限制）："
                         "请把配置写进 argv（例如 --home），不要用 env")
    binpath = _sc_quote(_windows_command_line(spec.argv))
    return (
        "sc.exe create " + spec.name
        + " binPath= " + binpath
        + " start= auto"
        + " DisplayName= " + _sc_quote(spec.display_name),
        "sc.exe description " + spec.name + " " + _sc_quote(spec.description),
        "sc.exe failure " + spec.name
        + " reset= 86400 actions= restart/5000/restart/30000/restart/300000",
        "sc.exe start " + spec.name,
    )


# ─────────────────────────────────────────────────────────── 常用 spec

def daemon_service_spec(home: str, *, python: str | None = None,
                        log_file: str | None = None,
                        name: str = "djb-node") -> ServiceSpec:
    """标准 daemon 服务描述：``<python> -m djb_node.daemon --home <home>``。

    只给 argv，不给 env：这样三端渲染都成立（Windows SCM 不支持环境变量）。
    """
    if not str(home or "").strip():
        raise UsageError("daemon_service_spec 需要非空 home")
    executable = python or sys.executable or "python3"
    return ServiceSpec(name=name, log_file=log_file,
                       argv=(executable, "-m", "djb_node.daemon", "--home", str(home)))
