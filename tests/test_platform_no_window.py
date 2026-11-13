# SPDX-License-Identifier: Apache-2.0
"""S1 回归：所有 console 子进程都必须带「不弹窗」kwargs。

判定标准（客户看到的是黑框，不是代码风格）：从 windowed 进程（``djb-desktop.exe``）起任何
console 子程序（powershell / nvidia-smi / djb-node.exe / import / taskkill…）都必须传
``creationflags=CREATE_NO_WINDOW`` + ``startupinfo(SW_HIDE)``。漏一个就是一个黑框，
所以除了分支单测，这里还有一条**源码扫描**用例：谁再写裸 ``subprocess.run`` 就红。
"""
from __future__ import annotations

import os
import re
import subprocess

import pytest

from djb_node.platform import no_window


# ─────────────────────────────────────────────────────────── 分支
def test_posix_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(no_window, "WINDOWS", False)
    assert no_window.subprocess_window_kwargs() == {}


def test_posix_kwargs_can_be_unpacked_into_real_subprocess():
    """POSIX 上 ``**{}`` 必须真的能跑（调用点是无条件展开的）。"""
    if no_window.WINDOWS:                      # pragma: no cover - Windows 上验另一条
        pytest.skip("POSIX 分支只在非 Windows 有意义")
    done = subprocess.run(["python3", "-c", "print(1)"], capture_output=True, text=True,
                          **no_window.subprocess_window_kwargs())
    assert done.returncode == 0 and done.stdout.strip() == "1"


class _FakeStartupInfo:
    def __init__(self) -> None:
        self.dwFlags = 0
        self.wShowWindow = 99


class _FakeSubprocess:
    CREATE_NO_WINDOW = 0x08000000
    STARTUPINFO = _FakeStartupInfo

    class SubprocessError(Exception):
        pass

    class TimeoutExpired(SubprocessError):
        pass


def test_windows_returns_create_no_window_and_hidden_window(monkeypatch):
    monkeypatch.setattr(no_window, "WINDOWS", True)
    monkeypatch.setattr(no_window, "subprocess", _FakeSubprocess)

    kwargs = no_window.subprocess_window_kwargs()

    assert kwargs["creationflags"] == 0x08000000            # CREATE_NO_WINDOW
    info = kwargs["startupinfo"]
    assert info.dwFlags & no_window.STARTF_USESHOWWINDOW    # wShowWindow 生效
    assert info.wShowWindow == no_window.SW_HIDE            # 隐藏


def test_windows_kwargs_are_fresh_objects(monkeypatch):
    """每次新建：调用方改了一个 dict / STARTUPINFO，不能污染下一个调用点。"""
    monkeypatch.setattr(no_window, "WINDOWS", True)
    monkeypatch.setattr(no_window, "subprocess", _FakeSubprocess)

    first = no_window.subprocess_window_kwargs()
    second = no_window.subprocess_window_kwargs()
    assert first is not second and first["startupinfo"] is not second["startupinfo"]
    first["creationflags"] = 0
    assert second["creationflags"] == 0x08000000


def test_windows_flag_falls_back_when_subprocess_lacks_constant(monkeypatch):
    """老 Python / 非 Windows 解释器上 ``subprocess.CREATE_NO_WINDOW`` 不存在时给常量兜底。

    传 0 等于没传（黑框照旧），所以这里必须回落到 0x08000000。
    """

    class _BareSubprocess:
        STARTUPINFO = _FakeStartupInfo

    monkeypatch.setattr(no_window, "WINDOWS", True)
    monkeypatch.setattr(no_window, "subprocess", _BareSubprocess)

    assert no_window.subprocess_window_kwargs()["creationflags"] == 0x08000000


def test_module_flag_matches_host_os():
    assert no_window.WINDOWS is (os.name == "nt")


# ─────────────────────────────────────────────────────────── 覆盖性（源码扫描）
_CALL = re.compile(r"subprocess\.(run|Popen|check_output|check_call)\(")
_SKIP_FILES = {"no_window.py"}


def test_every_subprocess_call_site_passes_window_kwargs():
    """``djb_node/`` 里不存在「裸」subprocess 调用（S1 的结构性防回归）。"""
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "djb_node")
    offenders: list[str] = []
    sites = 0
    for base, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".py") or name in _SKIP_FILES:
                continue
            path = os.path.join(base, name)
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            for index, line in enumerate(lines):
                if line.lstrip().startswith("#") or not _CALL.search(line):
                    continue
                if "_CALL =" in line or "_CALL.search" in line:      # 本文件自己的正则
                    continue
                sites += 1
                window = "\n".join(lines[index:index + 8])
                if "subprocess_window_kwargs()" not in window:
                    rel = os.path.relpath(path, root)
                    offenders.append("%s:%d: %s" % (rel, index + 1, line.strip()[:70]))
    assert sites >= 12, "扫描到的调用点太少（扫描逻辑坏了？）：%d" % sites
    assert offenders == [], "这些子进程调用点没带 CREATE_NO_WINDOW：\n" + "\n".join(offenders)
