# SPDX-License-Identifier: Apache-2.0
"""平台判定（C2）：纯函数、三平台分支，不依赖宿主 OS 的 monkeypatch 覆盖。"""
from __future__ import annotations

import dataclasses
import sys

import pytest

from djb_node import platform as platform_mod
from djb_node.platform import (KNOWN_SYSTEMS, SYSTEM_DARWIN, SYSTEM_LINUX, SYSTEM_OTHER,
                               SYSTEM_WINDOWS, PlatformInfo, current_platform, get_platform,
                               normalize_arch, normalize_system)


@pytest.fixture(autouse=True)
def _clear_platform_cache():
    """get_platform 是进程级缓存：每个用例前后清掉，避免 monkeypatch 串味。"""
    get_platform.cache_clear()
    yield
    get_platform.cache_clear()


@pytest.mark.parametrize("raw, expected", [
    ("linux", SYSTEM_LINUX),
    ("linux2", SYSTEM_LINUX),
    ("darwin", SYSTEM_DARWIN),
    ("macos", SYSTEM_DARWIN),
    ("win32", SYSTEM_WINDOWS),
    ("win64", SYSTEM_WINDOWS),
    ("cygwin", SYSTEM_WINDOWS),
    ("msys", SYSTEM_WINDOWS),
    ("freebsd13", SYSTEM_OTHER),
    ("sunos5", SYSTEM_OTHER),
    ("", SYSTEM_OTHER),
    (None, SYSTEM_OTHER),
])
def test_normalize_system_table(raw, expected):
    assert normalize_system(raw) == expected
    assert expected in KNOWN_SYSTEMS


@pytest.mark.parametrize("raw, expected", [
    ("AMD64", "x86_64"),
    ("x86-64", "x86_64"),
    ("x86_64", "x86_64"),
    ("arm64", "aarch64"),
    ("aarch64", "aarch64"),
    ("riscv64", "riscv64"),
    ("i686", "x86"),
    ("", "unknown"),
])
def test_normalize_arch_table(raw, expected):
    assert normalize_arch(raw) == expected


def test_platform_info_normalizes_and_is_frozen():
    info = PlatformInfo(os="win32", arch="AMD64", is_wsl=0, is_container=1)
    assert (info.os, info.arch) == (SYSTEM_WINDOWS, "x86_64")
    assert info.is_wsl is False and info.is_container is True
    assert info.is_windows and not info.is_linux and not info.is_darwin
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.os = "linux"                                      # type: ignore[misc]


def test_detect_linux_ignores_platform_probes_when_disabled():
    info = PlatformInfo.detect(system="linux", machine="aarch64", environ={},
                               probe_files=False)
    assert info == PlatformInfo(os=SYSTEM_LINUX, arch="aarch64",
                                is_wsl=False, is_container=False)


@pytest.mark.parametrize("key", ["WSL_DISTRO_NAME", "WSL_INTEROP", "WSLENV"])
def test_wsl_detected_from_environment(key):
    info = PlatformInfo.detect(system="linux", machine="x86_64",
                               environ={key: "1"}, probe_files=False)
    assert info.is_wsl is True


def test_wsl_marker_ignored_on_non_linux():
    info = PlatformInfo.detect(system="darwin", machine="arm64",
                               environ={"WSL_DISTRO_NAME": "Ubuntu"}, probe_files=False)
    assert info.is_wsl is False


def test_wsl_detected_from_kernel_text(monkeypatch):
    monkeypatch.setattr(platform_mod, "_read_text",
                        lambda path: "Linux version 5.15.0-microsoft-standard-WSL2")
    info = PlatformInfo.detect(system="linux", machine="x86_64", environ={},
                               probe_files=True)
    assert info.is_wsl is True


def test_container_detected_from_environment():
    info = PlatformInfo.detect(system="linux", machine="x86_64",
                               environ={"KUBERNETES_SERVICE_HOST": "10.0.0.1"},
                               probe_files=False)
    assert info.is_container is True


def test_container_detected_from_marker_file(monkeypatch):
    monkeypatch.setattr(platform_mod, "_exists", lambda path: path == "/.dockerenv")
    info = PlatformInfo.detect(system="linux", machine="x86_64", environ={},
                               probe_files=True)
    assert info.is_container is True


def test_container_probe_can_be_disabled(monkeypatch):
    monkeypatch.setattr(platform_mod, "_exists", lambda path: True)
    info = PlatformInfo.detect(system="linux", machine="x86_64", environ={},
                               probe_files=False)
    assert info.is_container is False


def test_current_platform_defaults_follow_host_without_new_fields():
    info = current_platform(probe_files=False)
    assert info.os in KNOWN_SYSTEMS
    assert info.arch
    assert set(info.to_dict()) == {"os", "arch", "is_wsl", "is_container"}


def test_current_platform_honours_monkeypatched_sys_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    info = current_platform(machine="AMD64", environ={}, probe_files=False)
    assert info.os == SYSTEM_WINDOWS and info.arch == "x86_64"


def test_get_platform_is_cached_until_cache_clear(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    first = get_platform()
    assert first.os == SYSTEM_LINUX
    monkeypatch.setattr(sys, "platform", "darwin")
    assert get_platform() is first                     # 缓存命中：同一对象
    get_platform.cache_clear()
    assert get_platform().os == SYSTEM_DARWIN


def test_platform_info_dict_has_no_paths_or_credentials():
    payload = current_platform(probe_files=False).to_dict()
    text = repr(payload).lower()
    for forbidden in ("key", "secret", "token", "password", "home"):
        assert forbidden not in text
