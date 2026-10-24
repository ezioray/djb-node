# SPDX-License-Identifier: Apache-2.0
"""平台目录布局（C2）：Linux 与现状逐字一致 + macOS / Windows 分支，不落真实目录。"""
from __future__ import annotations

import ntpath
import os
import stat
import sys

import pytest

from djb_node import facts
from djb_node.config import DEFAULT_HOME_DIRNAME, Paths, resolve_home
from djb_node.errors import UsageError
from djb_node.platform import PlatformInfo
from djb_node.platform import paths as platform_paths
from djb_node.platform.paths import PlatformPaths, default_home, default_paths

#: S1 config.Paths 的全部路径属性（Linux 零变化 = 逐个相等）
LEGACY_ATTRS = (
    "home", "config_file", "db_file", "credentials_file", "keys_dir",
    "buffer_key_file", "provider_key_file", "logs_dir", "log_file", "runtime_dir",
    "socket_file", "pid_file", "versions_dir", "diagnostics_dir", "cache_dir",
)


@pytest.fixture(autouse=True)
def _linux_host(monkeypatch):
    """宿主固定成 Linux + 干净的 HOME / DJB_HOME，用例再按需覆盖。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DJB_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)


def test_linux_default_home_equals_current_behaviour(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    expected = os.path.abspath(os.path.join(os.path.expanduser("~"), ".djb"))
    assert default_home() == expected
    assert default_home() == resolve_home()            # 与现有实现逐字一致
    assert expected.endswith(os.sep + platform_paths.LINUX_HOME_DIRNAME)
    assert platform_paths.LINUX_HOME_DIRNAME == DEFAULT_HOME_DIRNAME == ".djb"


@pytest.mark.parametrize("value", ["explicit-home", "sub/dir"])
def test_linux_override_matches_resolve_home(tmp_path, monkeypatch, value):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    explicit = str(tmp_path / value) if "/" not in value else value
    assert default_home(explicit) == resolve_home(explicit)


def test_linux_djb_home_env_matches_resolve_home(tmp_path, monkeypatch):
    target = tmp_path / "portable"
    monkeypatch.setenv("DJB_HOME", str(target))
    assert default_home() == resolve_home() == str(target)


def test_linux_layout_matches_existing_paths_verbatim(tmp_path, monkeypatch):
    home = tmp_path / "djbhome"
    monkeypatch.setenv("DJB_HOME", str(home))
    platform_layout = PlatformPaths.from_env()
    legacy = Paths(str(home))
    assert platform_layout.source == "env"
    for attr in LEGACY_ATTRS:
        assert getattr(platform_layout, attr) == getattr(legacy, attr), attr
    assert platform_layout.platform.os == "linux"


def test_linux_platform_defaults_ignore_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    layout = PlatformPaths.from_env()
    expected = default_paths()
    assert layout.source == "platform"
    assert layout.to_dict() == expected.to_dict()
    root = layout.config_dir
    assert layout.state_dir == root and layout.home == root
    assert layout.logs_dir == os.path.join(root, "logs")
    assert layout.cache_dir == os.path.join(root, "cache")
    assert layout.log_file == os.path.join(root, "logs", facts.LOG_FILE)


def test_other_posix_falls_back_to_linux_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "freebsd13")
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    layout = PlatformPaths.from_env()
    assert layout.platform.os == "other"
    assert layout.config_dir == os.path.join(str(tmp_path / "user"), ".djb")


def test_darwin_layout_separates_library_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    user = tmp_path / "macuser"
    monkeypatch.setenv("HOME", str(user))
    layout = PlatformPaths.from_env()
    library = os.path.join(str(user), "Library")
    assert layout.platform.os == "darwin"
    assert layout.config_dir == os.path.join(library, "Application Support", "DJB")
    assert layout.state_dir == layout.config_dir
    assert layout.logs_dir == os.path.join(library, "Logs", "DJB")
    assert layout.cache_dir == os.path.join(library, "Caches", "DJB")
    assert layout.config_file == os.path.join(layout.config_dir, "config.json")
    assert layout.db_file == os.path.join(layout.config_dir, "state.db")
    assert layout.log_file == os.path.join(layout.logs_dir, facts.LOG_FILE)
    assert not os.path.exists(layout.config_dir)       # 纯字符串，不落盘


def test_darwin_djb_home_keeps_single_root(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    target = tmp_path / "portable"
    monkeypatch.setenv("DJB_HOME", str(target))
    layout = PlatformPaths.from_env()
    assert layout.source == "env"
    assert layout.config_dir == layout.state_dir == str(target)
    assert layout.logs_dir == os.path.join(str(target), "logs")


def test_windows_layout_appdata_vs_localappdata(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    appdata = tmp_path / "appdata"
    local = tmp_path / "localappdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    layout = PlatformPaths.from_env()
    assert layout.platform.os == "windows"
    assert layout.config_dir == ntpath.abspath(ntpath.join(str(appdata), "DJB"))
    assert layout.state_dir == ntpath.abspath(ntpath.join(str(local), "DJB"))
    assert layout.logs_dir == ntpath.join(layout.state_dir, "logs")
    assert layout.cache_dir == ntpath.join(layout.state_dir, "cache")
    assert ntpath.basename(layout.config_file) == "config.json"
    assert ntpath.dirname(layout.credentials_file) == ntpath.join(layout.config_dir,
                                                                 "credentials")
    assert layout.db_file == ntpath.join(layout.state_dir, "state.db")
    assert layout.log_file == ntpath.join(layout.logs_dir, facts.LOG_FILE)
    assert "/" not in layout.config_file               # Windows 分支用 ntpath（反斜杠）


def test_windows_missing_appdata_falls_back_to_userprofile(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    user = tmp_path / "winuser"
    monkeypatch.setenv("USERPROFILE", str(user))
    layout = PlatformPaths.from_env()
    assert layout.config_dir == ntpath.abspath(
        ntpath.join(str(user), "AppData", "Roaming", "DJB"))
    assert layout.state_dir == ntpath.abspath(
        ntpath.join(str(user), "AppData", "Local", "DJB"))


def test_windows_default_home_is_roaming_appdata(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    expected = ntpath.abspath(ntpath.join(str(appdata), "DJB"))
    assert default_home() == expected
    assert default_paths().config_dir == expected


def test_override_precedence_and_source(tmp_path, monkeypatch):
    monkeypatch.setenv("DJB_HOME", str(tmp_path / "env-home"))
    from_env_paths = PlatformPaths.from_env()
    assert from_env_paths.source == "env"
    assert from_env_paths.config_dir == os.path.abspath(str(tmp_path / "env-home"))

    explicit = tmp_path / "arg-home"
    chosen = PlatformPaths.from_env(str(explicit))
    assert chosen.source == "arg"
    assert chosen.config_dir == os.path.abspath(str(explicit))
    assert chosen.logs_dir == os.path.join(str(explicit), "logs")
    assert chosen.config_dir != from_env_paths.config_dir


def test_from_env_accepts_explicit_platform_info(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    layout = PlatformPaths.from_env(platform=PlatformInfo("darwin", "arm64"))
    assert layout.platform.arch == "aarch64"
    assert "Application Support" in layout.config_dir


def test_platform_defaults_never_touch_disk(tmp_path, monkeypatch):
    target = tmp_path / "user"
    monkeypatch.setenv("HOME", str(target))
    for system in ("linux", "darwin"):
        monkeypatch.setattr(sys, "platform", system)
        layout = PlatformPaths.from_env()
        assert not os.path.exists(layout.config_dir)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_prepare_creates_private_tree_and_existing(tmp_path, monkeypatch):
    home = tmp_path / "djbhome"
    monkeypatch.setenv("DJB_HOME", str(home))
    layout = PlatformPaths.from_env()
    assert layout.existing() is False
    layout.prepare()
    for directory in (layout.config_dir, layout.keys_dir, layout.logs_dir,
                      layout.runtime_dir, layout.versions_dir,
                      layout.diagnostics_dir, layout.cache_dir):
        assert os.path.isdir(directory), directory
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700, directory
    assert os.path.isdir(os.path.dirname(layout.credentials_file))
    assert not os.path.exists(layout.db_file)          # prepare 只建目录，不建文件
    assert layout.existing() is False
    (home / "state.db").write_text("", encoding="utf-8")
    assert layout.existing() is True


def test_snapshot_and_to_dict_expose_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DJB_HOME", str(tmp_path / "djbhome"))
    layout = PlatformPaths.from_env()
    snapshot = layout.snapshot()
    for key in ("home", "data_dir", "logs_dir", "cache_dir", "db_file", "db_mode",
                "credentials_file", "credentials_mode", "config_mode"):
        assert key in snapshot
    payload = layout.to_dict()
    assert payload["source"] == "env"
    assert payload["platform"]["os"] == "linux"
    assert "client_key" not in repr(payload).lower()


def test_empty_directory_is_rejected():
    with pytest.raises(UsageError):
        PlatformPaths(platform=PlatformInfo("linux", "x86_64"), source="platform",
                      config_dir="", state_dir="/x", logs_dir="/x", cache_dir="/x")
