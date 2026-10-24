# SPDX-License-Identifier: Apache-2.0
"""C4 服务安装计划：三端命令/文件内容、权限标注、纯数据（不执行、不落盘、不提权）。"""
from __future__ import annotations

import ast
import inspect
import json
import os

import pytest

from djb_node.errors import UsageError
from djb_node.platform import PlatformInfo
from djb_node.platform import service_plan as plan_mod
from djb_node.platform.paths import PlatformPaths
from djb_node.platform.service_plan import PlanFile, ServicePlan, build_service_plan


def make_paths(tmp_path, system: str) -> PlatformPaths:
    return PlatformPaths.from_env(str(tmp_path / ("home-" + system)),
                                 platform=PlatformInfo(system, "x86_64"))


def build(tmp_path, system: str, *, home: str = "/srv/djb",
          python: str = "/usr/bin/python3", unit_dir=None) -> ServicePlan:
    paths = make_paths(tmp_path, system)
    return build_service_plan(system, paths, python, home, unit_dir=unit_dir)


# ─────────────────────────────────────────────────────────── linux / systemd
def test_linux_plan_uses_user_systemd_unit(tmp_path):
    unit_dir = tmp_path / "units"
    plan = build(tmp_path, "linux", unit_dir=str(unit_dir))
    assert plan.platform == "linux"
    assert plan.requires_admin is False
    assert len(plan.files) == 1
    unit = plan.files[0]
    assert unit.path == str(unit_dir / "djb-node.service")
    assert unit.mode == "0644"
    assert "[Service]" in unit.content and "ExecStart=" in unit.content
    assert "--home /srv/djb" in unit.content
    assert plan.install_cmds == (
        "systemctl --user daemon-reload",
        "systemctl --user enable --now djb-node.service",
    )
    assert plan.uninstall_cmds[0] == "systemctl --user disable --now djb-node.service"
    assert "systemctl --user is-active djb-node.service" in plan.verify_cmds
    assert "djb status --json" in plan.verify_cmds
    assert any("loginctl enable-linger" in note for note in plan.notes)
    assert not unit_dir.exists()
    assert not os.path.exists(unit.path)


def test_linux_default_unit_dir_uses_home_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    plan = build(tmp_path, "linux")
    path = plan.files[0].path
    assert path.startswith(str(tmp_path / "user"))
    assert path.endswith(".config/systemd/user/djb-node.service")
    assert not os.path.exists(path)


# ─────────────────────────────────────────────────────────── darwin / launchd
def test_darwin_plan_uses_launch_agent(tmp_path):
    agent_dir = tmp_path / "agents"
    plan = build(tmp_path, "darwin", unit_dir=str(agent_dir))
    assert plan.platform == "darwin"
    assert plan.requires_admin is False
    plist = plan.files[0]
    assert plist.path == str(agent_dir / "com.djb.djb-node.plist")
    assert plist.content.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "<key>ProgramArguments</key>" in plist.content
    assert plan.install_cmds[0].startswith("launchctl bootstrap gui/$(id -u) ")
    assert plan.install_cmds[1] == "launchctl enable gui/$(id -u)/com.djb.djb-node"
    assert plan.uninstall_cmds[0] == "launchctl bootout gui/$(id -u)/com.djb.djb-node"
    assert "launchctl print gui/$(id -u)/com.djb.djb-node" in plan.verify_cmds
    assert "djb status --json" in plan.verify_cmds
    assert any("LaunchAgent" in note for note in plan.notes)
    assert not agent_dir.exists()


# ─────────────────────────────────────────────────────────── windows / SCM
def test_windows_plan_uses_sc_and_powershell(tmp_path):
    python = r"C:\Python311\python.exe"
    home = r"C:\Users\me\AppData\Roaming\DJB"
    plan = build(tmp_path, "windows", home=home, python=python)
    assert plan.platform == "windows"
    assert plan.requires_admin is True
    assert len(plan.install_cmds) == 4
    assert plan.install_cmds[0].startswith("sc.exe create djb-node ")
    assert "binPath= " in plan.install_cmds[0]
    assert "--home " + home in plan.install_cmds[0]
    assert plan.uninstall_cmds == ("sc.exe stop djb-node", "sc.exe delete djb-node")
    assert "sc.exe query djb-node" in plan.verify_cmds
    assert "djb status --json" in plan.verify_cmds

    script = plan.files[0]
    assert script.path.endswith(r"djb-node-service.ps1")
    assert "/" not in script.path
    assert "sc.exe create djb-node" in script.content
    assert "$ErrorActionPreference" in script.content
    assert "sc.exe query djb-node" in script.content
    assert not os.path.exists(script.path)


@pytest.mark.parametrize("system, expected_platform", [
    ("linux", "linux"),
    ("darwin", "darwin"),
    ("win32", "windows"),
    ("windows", "windows"),
])
def test_platform_aliases_are_normalized(tmp_path, system, expected_platform):
    plan = build(tmp_path, system)
    assert plan.platform == expected_platform


@pytest.mark.parametrize("system", ["linux", "darwin", "windows"])
def test_every_platform_plan_carries_home(tmp_path, system):
    plan = build(tmp_path, system, home="/srv/djb")
    text = "\n".join(item.content for item in plan.files) + "\n" + "\n".join(
        plan.all_commands)
    assert "--home" in text and "/srv/djb" in text
    assert "djb status --json" in plan.verify_cmds


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_posix_plans_redirect_logs_to_platform_path(tmp_path, system):
    paths = make_paths(tmp_path, system)
    plan = build_service_plan(system, paths, "/usr/bin/python3", "/srv/djb")
    assert paths.log_file in plan.files[0].content


# ─────────────────────────────────────────────────────────── 纯数据 / 幂等
def test_build_is_idempotent(tmp_path):
    first = build(tmp_path, "linux")
    second = build(tmp_path, "linux")
    assert first.to_dict() == second.to_dict()
    assert first.render_text() == second.render_text()
    assert json.loads(json.dumps(first.to_dict(), ensure_ascii=False)) == first.to_dict()


def test_platform_info_and_string_are_equivalent(tmp_path):
    paths = make_paths(tmp_path, "windows")
    home = r"C:\Users\me\AppData\Roaming\DJB"
    python = r"C:\Python311\python.exe"
    via_info = build_service_plan(PlatformInfo("win32", "AMD64"), paths, python, home)
    via_text = build_service_plan("windows", paths, python, home)
    assert via_info.to_dict() == via_text.to_dict()


def test_plan_does_not_write_or_create_anything(tmp_path):
    before = sorted(item.name for item in tmp_path.iterdir())
    plan = build(tmp_path, "linux", unit_dir=str(tmp_path / "units"))
    after = sorted(item.name for item in tmp_path.iterdir())
    assert before == after
    assert not os.path.exists(plan.files[0].path)


def test_module_imports_no_process_or_os_control():
    tree = ast.parse(inspect.getsource(plan_mod))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    imported |= {(node.module or "").split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom)}
    for forbidden in ("subprocess", "os", "shutil", "ctypes", "winreg", "sys"):
        assert forbidden not in imported
    assert not hasattr(plan_mod, "subprocess")


def test_render_text_covers_all_sections(tmp_path):
    text = build(tmp_path, "linux").render_text()
    for marker in ("服务安装计划", "文件（先由安装器写盘", "安装命令：", "卸载命令：",
                   "校验命令：", "备注：", "需要管理员：否"):
        assert marker in text
    assert "需要管理员：是" in build(tmp_path, "windows").render_text()


def test_commands_are_non_empty_strings(tmp_path):
    for system in ("linux", "darwin", "windows"):
        plan = build(tmp_path, system)
        for command in plan.all_commands:
            assert isinstance(command, str) and command.strip()
        assert plan.all_commands == (plan.install_cmds + plan.uninstall_cmds
                                     + plan.verify_cmds)


# ─────────────────────────────────────────────────────────── 校验失败
@pytest.mark.parametrize("platform", ["freebsd", "other", "", None, 3])
def test_unsupported_platform_is_rejected(tmp_path, platform):
    paths = make_paths(tmp_path, "linux")
    with pytest.raises(UsageError):
        build_service_plan(platform, paths, "/usr/bin/python3", "/srv/djb")


@pytest.mark.parametrize("python, home", [
    ("", "/srv/djb"), ("   ", "/srv/djb"), ("/usr/bin/python3", ""),
    ("/usr/bin/python3", "   "),
])
def test_empty_python_or_home_is_rejected(tmp_path, python, home):
    paths = make_paths(tmp_path, "linux")
    with pytest.raises(UsageError):
        build_service_plan("linux", paths, python, home)


def test_paths_platform_mismatch_is_rejected(tmp_path):
    linux_paths = make_paths(tmp_path, "linux")
    with pytest.raises(UsageError):
        build_service_plan("darwin", linux_paths, "/usr/bin/python3", "/srv/djb")


def test_service_plan_validation():
    with pytest.raises(UsageError):
        ServicePlan("other", ("a",), ("b",), ("c",))
    with pytest.raises(UsageError):
        ServicePlan("linux", (), ("b",), ("c",))
    with pytest.raises(UsageError):
        ServicePlan("linux", ("a\nb",), ("b",), ("c",))
    with pytest.raises(UsageError):
        ServicePlan("linux", ("a",), ("b",), ("c",), requires_admin="yes")  # type: ignore[arg-type]


def test_plan_file_validation():
    with pytest.raises(UsageError):
        PlanFile("", "content")
    with pytest.raises(UsageError):
        PlanFile("/tmp/x", "")
    with pytest.raises(UsageError):
        PlanFile("/tmp/x", "content", mode="644")
    with pytest.raises(UsageError):
        PlanFile("/tmp/x", "content", mode="0999")
