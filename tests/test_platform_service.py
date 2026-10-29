# SPDX-License-Identifier: Apache-2.0
"""服务渲染（C2）：三端都是纯字符串，不执行命令、不提权、不写系统目录。"""
from __future__ import annotations

import ast
import inspect

import pytest

from djb_node.errors import UsageError
from djb_node.platform import service
from djb_node.platform.service import (ServiceSpec, daemon_service_spec, launchd_plist_name,
                                       render_launchd_plist, render_systemd_unit,
                                       render_windows_sc_commands, systemd_unit_name)

LINUX_SPEC = ServiceSpec(
    name="djb-node",
    argv=("/usr/bin/python3", "-m", "djb_node.daemon", "--home", "/home/u/.djb"),
    log_file="/home/u/.djb/logs/djb.log",
)


def test_systemd_unit_has_required_sections():
    unit = render_systemd_unit(LINUX_SPEC)
    assert unit.startswith("# 由 djb_node.platform.service 生成")
    assert "[Unit]" in unit and "[Service]" in unit and "[Install]" in unit
    assert "Description=DJB 联脑共享节点客户端" in unit
    assert ("ExecStart=/usr/bin/python3 -m djb_node.daemon --home /home/u/.djb") in unit
    assert "Restart=on-failure" in unit and "RestartSec=5" in unit
    assert "StandardOutput=append:/home/u/.djb/logs/djb.log" in unit
    assert "StandardError=append:/home/u/.djb/logs/djb.log" in unit
    assert unit.endswith("WantedBy=default.target\n")
    for forbidden in ("sudo", "systemctl", "launchctl", "sc.exe"):
        assert forbidden not in unit


def test_systemd_unit_quotes_and_escapes_arguments():
    spec = ServiceSpec(name="djb-node",
                       argv=("/opt/My App/python3", "--flag=a b", 'q"x', "p$q%r"))
    unit = render_systemd_unit(spec)
    assert ('ExecStart="/opt/My App/python3" "--flag=a b" "q\\"x" "p$$q%%r"') in unit


def test_systemd_unit_renders_environment_sorted_and_literal():
    spec = ServiceSpec(name="djb-node", argv=("python3", "-m", "djb_node.daemon"),
                       env={"DJB_HOME": "/a b", "K": "a$b", "A": "1"})
    unit = render_systemd_unit(spec)
    assert 'Environment="A=1"' in unit
    assert 'Environment="DJB_HOME=/a b"' in unit
    assert 'Environment="K=a$b"' in unit                 # Environment= 不做变量展开
    assert unit.index('Environment="A=1"') < unit.index('Environment="DJB_HOME=')


def test_systemd_unit_workdir_and_no_log_redirect():
    spec = ServiceSpec(name="djb-node", argv=("python3",), workdir="/srv/d jb")
    unit = render_systemd_unit(spec)
    assert 'WorkingDirectory="/srv/d jb"' in unit
    assert "StandardOutput" not in unit and "StandardError" not in unit


def test_systemd_unit_quotes_log_path_with_spaces():
    spec = ServiceSpec(name="djb-node", argv=("python3",), log_file="/var/log/d jb.log")
    unit = render_systemd_unit(spec)
    assert 'StandardOutput=append:"/var/log/d jb.log"' in unit


def test_launchd_plist_is_valid_xml_shape():
    plist = render_launchd_plist(LINUX_SPEC)
    assert plist.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "<!DOCTYPE plist" in plist
    assert "<key>Label</key>" in plist and "com.djb.djb-node" in plist
    assert "<key>ProgramArguments</key>" in plist
    assert "<key>RunAtLoad</key>" in plist and "<true/>" in plist
    assert "<key>KeepAlive</key>" in plist and "<key>SuccessfulExit</key>" in plist
    assert "<key>ThrottleInterval</key>" in plist and "<integer>10</integer>" in plist
    assert "<key>StandardOutPath</key>" in plist
    assert "<key>StandardErrorPath</key>" in plist
    assert "/home/u/.djb/logs/djb.log" in plist
    for forbidden in ("launchctl", "sudo"):
        assert forbidden not in plist
    assert plist.count("<array>") == 1 and plist.count("</array>") == 1


def test_launchd_plist_escapes_xml_and_env():
    spec = ServiceSpec(name="djb-node", argv=("python3", "<&>"),
                       env={"DJB_HOME": "C:\\Users\\a&b"}, restart_seconds=30)
    plist = render_launchd_plist(spec)
    assert "<string>&lt;&amp;&gt;</string>" in plist
    assert "C:\\Users\\a&amp;b" in plist
    assert "<integer>30</integer>" in plist
    assert "<key>EnvironmentVariables</key>" in plist


def test_launchd_custom_label_and_workdir():
    spec = ServiceSpec(name="djb-node", argv=("python3",), label="org.example.djb",
                       workdir="/srv/djb")
    plist = render_launchd_plist(spec)
    assert "<string>org.example.djb</string>" in plist
    assert "<key>WorkingDirectory</key>" in plist
    assert launchd_plist_name(spec) == "org.example.djb.plist"


def test_windows_sc_commands_are_four_pure_strings():
    python = r"C:\Program Files\Python\python.exe"
    home = r"C:\Users\me\AppData\Roaming\DJB"
    commands = render_windows_sc_commands(daemon_service_spec(home, python=python))
    assert isinstance(commands, tuple) and len(commands) == 4
    assert commands[0].startswith("sc.exe create djb-node ")
    assert 'binPath= "' in commands[0] and "start= auto" in commands[0]
    assert r'\"C:\Program Files\Python\python.exe\"' in commands[0]
    assert home in commands[0]
    assert commands[1].startswith("sc.exe description djb-node ")
    assert commands[2].startswith("sc.exe failure djb-node ")
    assert "actions= restart/5000/restart/30000/restart/300000" in commands[2]
    assert commands[3] == "sc.exe start djb-node"
    for command in commands:
        for forbidden in ("sudo", "runas", "/RU", "powershell", "reg add", "schtasks"):
            assert forbidden not in command


def test_windows_rejects_env_instead_of_silently_dropping_it():
    spec = ServiceSpec(name="djb-node", argv=("python3",), env={"DJB_HOME": "/x"})
    with pytest.raises(UsageError):
        render_windows_sc_commands(spec)


def test_daemon_service_spec_shape():
    spec = daemon_service_spec("/home/u/.djb", python="/usr/bin/python3",
                               log_file="/home/u/.djb/logs/djb.log")
    assert spec.argv == ("/usr/bin/python3", "-m", "djb_node.daemon",
                         "--home", "/home/u/.djb")
    assert spec.env == ()
    assert spec.name == "djb-node"
    assert systemd_unit_name(spec) == "djb-node.service"
    assert launchd_plist_name(spec) == "com.djb.djb-node.plist"
    assert render_systemd_unit(spec)
    assert render_launchd_plist(spec)
    assert render_windows_sc_commands(spec)


def test_daemon_service_spec_rejects_empty_home():
    with pytest.raises(UsageError):
        daemon_service_spec("  ")


@pytest.mark.parametrize("kwargs", [
    {"name": "bad name", "argv": ("x",)},
    {"name": "-owner", "argv": ("x",)},
    {"name": "", "argv": ("x",)},
    {"name": "d" * 70, "argv": ("x",)},
    {"name": "djb", "argv": ()},
    {"name": "djb", "argv": ("x", "")},
    {"name": "djb", "argv": ("a\nb",)},
    {"name": "djb", "argv": ("x",), "env": {"1BAD": "v"}},
    {"name": "djb", "argv": ("x",), "env": {"OK": "a\nb"}},
    {"name": "djb", "argv": ("x",), "restart_seconds": -1},
    {"name": "djb", "argv": ("x",), "display_name": "a\rb"},
    {"name": "djb", "argv": ("x",), "label": "bad label"},
])
def test_invalid_spec_raises_usage_error(kwargs):
    with pytest.raises(UsageError):
        ServiceSpec(**kwargs)


def test_rendering_is_deterministic_and_has_no_side_effects():
    assert render_systemd_unit(LINUX_SPEC) == render_systemd_unit(LINUX_SPEC)
    assert render_launchd_plist(LINUX_SPEC) == render_launchd_plist(LINUX_SPEC)


def test_module_imports_no_process_or_os_control():
    tree = ast.parse(inspect.getsource(service))
    imported = {alias.name for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module or "" for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom)}
    for forbidden in ("subprocess", "os", "shutil", "ctypes", "winreg"):
        assert forbidden not in imported
    assert not hasattr(service, "subprocess")
