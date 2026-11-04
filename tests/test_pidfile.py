# SPDX-License-Identifier: Apache-2.0
"""`djb stop --force` 的逃生门：pid 文件必须是 (pid, 启动时刻) 双键，且强杀前必须校验。

背景（task-12，2026-11-05）
--------------------------
修前：`runtime/daemon.pid` **从来没有被写过**（全仓只有 `cli.py` 读它）⇒ `--force` 分支
`_read_pid()` 恒 `None` ⇒ 打印「停止超时；可用 djb stop --force」，而 `--force` 永远无效，
用户照提示再敲一次还是同一句 —— **死循环**。

修后：daemon 启动即原子写双键 pid 文件；`--force` 先校验再杀。这里的重点是**不误杀**：
pid 会被系统复用，只认 pid 的强杀就是「误杀门」。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from djb_node import pidfile
from djb_node.cli import _force_stop
from djb_node.config import Paths
from djb_node.platform import process as probe
from tests import process_tree

WINDOWS = sys.platform.startswith("win")


def _paths(home) -> Paths:
    paths = Paths(str(home))
    paths.prepare()
    return paths


def _base_python() -> str:
    """Windows 上 ``sys.executable`` 是 venv 启动器，会再 CreateProcess 一个子进程；
    测试里要单层进程，所以用 base 解释器。"""
    if WINDOWS:
        candidate = os.path.join(sys.base_prefix, "python.exe")
        if os.path.exists(candidate):
            return candidate
    return sys.executable


def _wait_token(pid: int, timeout: float = 10.0) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        token = probe.start_token(pid)
        if token:
            return token
        time.sleep(0.05)
    return None


def _spawn_fake_daemon(home) -> subprocess.Popen:
    """一个"长得像 daemon"的进程：命令行含 djb_node.daemon 与 --home <home>。"""
    return subprocess.Popen(
        [_base_python(), "-c", "import time; time.sleep(120)",
         "djb_node.daemon", "--home", str(home)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _spawn_other(home) -> subprocess.Popen:
    """一个"不像 daemon"的进程（模拟 pid 被复用给了别的程序）。"""
    return subprocess.Popen([_base_python(), "-c", "import time; time.sleep(120)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ── 1. 文件本身：双键 + 0600 + 原子写 ───────────────────────────────────────
def test_pid_file_is_dual_key_private_and_parsable(home):
    paths = _paths(home)
    record = pidfile.write(paths)

    assert os.path.exists(paths.pid_file)
    assert record["pid"] == os.getpid()
    assert record["start_token"] == probe.start_token(os.getpid())
    assert record["home"] == os.path.abspath(str(home))
    assert record["marker"] == probe.DAEMON_MARKER

    raw = open(paths.pid_file, "rb").read()
    assert b"start_token" in raw                      # 双键，不是一个裸 pid
    data = json.loads(raw.decode("utf-8"))
    assert data["schema"] == pidfile.SCHEMA
    if os.name != "nt":
        from djb_node.util import file_mode
        assert file_mode(paths.pid_file) == 0o600

    # 读回来一致；删掉之后读是 None（幂等）
    assert pidfile.read(paths)["pid"] == os.getpid()
    assert pidfile.remove(paths, pid=os.getpid()) is True
    assert pidfile.read(paths) is None


def test_pid_file_read_refuses_legacy_and_garbage(home):
    paths = _paths(home)
    with open(paths.pid_file, "w", encoding="ascii") as handle:
        handle.write("4242\n")                        # 旧版格式：只有 pid
    assert pidfile.read(paths) is None
    assert pidfile.verify({"schema": 0, "pid": 4242}, paths)[1] == pidfile.REASON.LEGACY

    with open(paths.pid_file, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    assert pidfile.read(paths) is None


# ── 2. PID 复用攻击：指向非 djb 进程 ⇒ 必须拒绝且不杀 ───────────────────────
def test_force_refuses_when_pid_points_to_another_program(home):
    paths = _paths(home)
    other = _spawn_other(home)
    try:
        token = _wait_token(other.pid)
        assert token, "拿不到目标进程的启动令牌，测试前置失败"
        pidfile.write(paths, pidfile.build_record(str(home), pid=other.pid, token=token))

        ok, reason = pidfile.verify(pidfile.read(paths), paths)
        assert (ok, reason) == (False, pidfile.REASON.NOT_DAEMON)

        assert _force_stop(paths) != 0                 # 拒绝强杀
        time.sleep(0.2)
        assert other.poll() is None                    # **它必须还活着**
    finally:
        other.kill()
        other.wait(timeout=10)


def test_force_refuses_when_home_differs(home, tmp_path):
    """真 daemon、真令牌，但属于另一个 --home ⇒ 也不杀（不同实例不互相误伤）。"""
    paths = _paths(home)
    other_home = tmp_path / "another-home"
    other_home.mkdir()
    daemon = _spawn_fake_daemon(other_home)
    try:
        token = _wait_token(daemon.pid)
        assert token
        pidfile.write(paths, pidfile.build_record(str(home), pid=daemon.pid, token=token))
        ok, reason = pidfile.verify(pidfile.read(paths), paths)
        assert (ok, reason) == (False, pidfile.REASON.HOME_MISMATCH)
        assert _force_stop(paths) != 0
        time.sleep(0.2)
        assert daemon.poll() is None
    finally:
        daemon.kill()
        daemon.wait(timeout=10)


# ── 3. 令牌不符（pid 被复用）/ 进程已退 ─────────────────────────────────────
def test_force_refuses_stale_token(home):
    paths = _paths(home)
    other = _spawn_other(home)
    try:
        assert _wait_token(other.pid)
        stale = pidfile.build_record(str(home), pid=other.pid, token="win:1" if WINDOWS
                                     else "linux:stale-boot:1")
        pidfile.write(paths, stale)
        ok, reason = pidfile.verify(pidfile.read(paths), paths)
        assert (ok, reason) == (False, pidfile.REASON.TOKEN_MISMATCH)
        assert _force_stop(paths) != 0
        assert other.poll() is None
    finally:
        other.kill()
        other.wait(timeout=10)


def test_force_refuses_when_process_already_gone(home):
    paths = _paths(home)
    probe_proc = _spawn_other(home)
    token = _wait_token(probe_proc.pid)
    probe_proc.kill()
    probe_proc.wait(timeout=10)
    pidfile.write(paths, pidfile.build_record(str(home), pid=probe_proc.pid, token=token))
    ok, reason = pidfile.verify(pidfile.read(paths), paths)
    assert (ok, reason) == (False, pidfile.REASON.MISSING)
    assert _force_stop(paths) != 0


# ── 4. 校验通过 ⇒ 真能停（模拟"daemon 挂死：控制面不响应"）──────────────────
def test_force_kills_verified_daemon(home):
    paths = _paths(home)
    daemon = _spawn_fake_daemon(home)
    try:
        token = _wait_token(daemon.pid)
        assert token
        pidfile.write(paths, pidfile.build_record(str(home), pid=daemon.pid, token=token))

        ok, reason = pidfile.verify(pidfile.read(paths), paths)
        assert (ok, reason) == (True, "")

        assert _force_stop(paths) == 0
        assert daemon.wait(timeout=10) is not None      # 真的退出了
        assert pidfile.read(paths) is None              # 事后把记录也清掉
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait(timeout=10)
        process_tree.reap_daemon_tree(str(home))


@pytest.mark.skipif(not hasattr(os, "kill"), reason="需要 os.kill")
def test_force_reports_untrusted_instead_of_silent_success(home, capsys):
    """修前的行为是「打印可用 --force，但 --force 什么都不做」；修后必须**出声拒绝**。"""
    paths = _paths(home)                     # 没有 pid 文件
    assert _force_stop(paths) != 0
    err = capsys.readouterr().err
    assert "拒绝强制结束" in err and "pid 文件不可信" in err


# ── 5. 接线：daemon 真的会写/删这个文件（不是只有一个没人调的 helper）──────────
def test_daemon_start_writes_pid_file_before_any_platform_work(paths, store, credentials,
                                                              monkeypatch):
    """`start()` **入口**就落 pid：这样"启动过程本身挂住"时 `--force` 才有东西可校验。

    做法：把 `_write_pid_file` 包一层（写完就抛哨兵），断言 start() 确实**先**写文件、
    且内容是本进程的双键 —— 不真的去起 daemon（那样会占端口、留线程）。
    """
    from djb_node.daemon import Daemon
    from djb_node.config import ClientConfig

    config = ClientConfig(gateway_url="http://127.0.0.1:9", mirror_port=1717)
    daemon = Daemon(paths, config=config, store=store, credentials=credentials,
                    local_token="t" * 40, port=0)
    assert pidfile.read(paths) is None

    original = Daemon._write_pid_file
    marker = "wm-pidfile-sentinel"

    def write_then_boom(self):
        original(self)
        raise RuntimeError(marker)

    monkeypatch.setattr(Daemon, "_write_pid_file", write_then_boom)
    with pytest.raises(RuntimeError) as excinfo:
        daemon.start()
    assert str(excinfo.value) == marker

    record = pidfile.read(paths)
    assert record is not None, "start 一进去就必须留下可校验的 pid 记录（逃生门的前提）"
    assert record["pid"] == os.getpid()
    assert record["start_token"] == probe.start_token(os.getpid())
    assert record["home"] == os.path.abspath(str(paths.home))


def test_daemon_stop_removes_only_own_pid_file(paths, store, credentials):
    """退出时删记录，但**不删别人的**（另一个实例的 pid 文件不能被顺手删掉）。"""
    from djb_node.daemon import Daemon
    from djb_node.config import ClientConfig

    config = ClientConfig(gateway_url="http://127.0.0.1:9", mirror_port=1717)
    daemon = Daemon(paths, config=config, store=store, credentials=credentials,
                    local_token="t" * 40, port=0)
    others = pidfile.build_record(paths.home, pid=os.getpid() + 1)
    pidfile.write(paths, others)
    daemon._remove_pid_file()
    assert pidfile.read(paths) is not None              # 不是自己写的，不许删

    pidfile.write(paths)                                # 自己写的
    daemon._remove_pid_file()
    assert pidfile.read(paths) is None


def test_stop_force_works_when_control_plane_is_unavailable(home, capsys):
    """控制面坏掉时也必须有逃生门（修前这里直接 print「未在运行」+ return 0）。

    场景：daemon 进程在跑、pid 文件在，但控制套接字/管道不可用 ⇒ `djb stop --force`
    必须走 pid 文件校验并真的停掉它，而不是回一句"未在运行"。
    """
    from djb_node.cli import main

    paths = _paths(home)
    daemon = _spawn_fake_daemon(home)
    try:
        token = _wait_token(daemon.pid)
        assert token
        pidfile.write(paths, pidfile.build_record(str(home), pid=daemon.pid, token=token))
        assert not os.path.exists(paths.socket_file) or os.name == "nt"   # 控制面确实没有

        assert main(["stop", "--force"]) == 0
        assert daemon.wait(timeout=10) is not None
        assert pidfile.read(paths) is None
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait(timeout=10)
        process_tree.reap_daemon_tree(str(home))


def test_stop_force_still_idempotent_when_nothing_ever_started(home, capsys):
    """什么都没起过（也没有 pid 文件）时，`stop --force` 仍然幂等返回 0。"""
    from djb_node.cli import main

    _paths(home)
    assert main(["stop", "--force"]) == 0
    assert "未在运行" in capsys.readouterr().out
