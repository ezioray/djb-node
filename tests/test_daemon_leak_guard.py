# SPDX-License-Identifier: Apache-2.0
"""回归守卫：`djb start` 拉起的 daemon **进程树**不许活过用例（task-8）。

背景（2026-11-05 实测）
-----------------------
`tests/test_daemon_lifecycle.py::test_cli_start_stop_subprocess` 在 Windows 上每次跑都会
漏下一整棵树（外层 venv 启动器 + 内层 `python311 -m djb_node.daemon`）：`djb start` 抛错时
用例在 `paths = Paths(...)` 之前就炸了，清理代码根本没机会执行。存量因此累积到 17 棵，
而同一张进程表里还有 `hermes.exe` / `gpu-broker` 这类业务进程 —— 误杀即事故。

这里守两条：
1. **机制层**：合成一棵两层树（外层 -> 内层），`kill_daemon_tree` 必须把**两层都**收掉
   （只杀外层会留下真正接单的内层；只杀内层会留下外层）。
2. **真实路径层**：连跑两次真实 `djb start`，断言**第一轮的进程在第二轮开始前已不存在**，
   且两轮结束后没有任何残留。
"""
from __future__ import annotations

import socket
import subprocess
import sys

import pytest

from djb_node.cli import main
from djb_node.config import Paths
from tests import process_tree
from tests.mock_m02 import MockServer


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_kill_daemon_tree_reaps_both_levels(tmp_path):
    """合成树：外层进程 -> 内层子进程，两边命令行都带 `djb_node.daemon` 与同一 `--home`。"""
    home = tmp_path / "home"
    home.mkdir()
    marker = str(home)
    # 内层：睡到被杀；命令行带 djb_node.daemon 与 --home，形态贴近真实两层结构
    inner_code = (
        "import sys,time;time.sleep(120)"
    )
    # 外层：先拉内层，再自己睡
    outer_code = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c'," + repr(inner_code) + ","
        "'djb_node.daemon','--home'," + repr(marker) + "]);"
        "time.sleep(120)"
    )
    outer = subprocess.Popen([sys.executable, "-c", outer_code,
                              "djb_node.daemon", "--home", marker])
    try:
        def two_levels():
            found = process_tree.daemon_pids_for_home(marker)
            # 外层先起来、内层后 spawn：必须等"真的两层都在"，否则偶发只认到 1 层
            return found if len(found) >= 2 else None

        pids = _wait_for(two_levels, timeout=15.0)
        assert len(pids) >= 2, "预期识别到外层 + 内层两层 daemon 进程，实际 %r" % (pids,)

        killed = process_tree.kill_daemon_tree(marker)
        assert set(pids) <= set(killed)
        leftover = process_tree.wait_gone(pids, timeout=15.0)
        assert leftover == [], "整棵树都必须退出；仍存活：%r" % (leftover,)
        assert process_tree.daemon_pids_for_home(marker) == []
    finally:
        process_tree.kill_daemon_tree(marker)
        outer.poll()


def _wait_for(probe, timeout=10.0, interval=0.1):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(interval)
    return probe()


def _spawn_daemon_like_leaky_test(home, capsys):
    """复刻 `test_cli_start_stop_subprocess` 的起法（有 mock 服务端，daemon 起得来）。

    返回：这一轮 spawn 出来的 daemon 进程 pid（**不做任何清理**，交给调用方回收）。
    """
    paths = Paths(str(home))
    server = MockServer().start()
    try:
        assert main(["init", "--no-env", "--gateway", server.base_url,
                     "--port", str(_free_port()), "--cpu", "100", "--gpu", "100",
                     "--mem", "99"]) == 0
        capsys.readouterr()
        assert main(["upstream", "add", "--access", "A2",
                     "--base-url", server.upstream_base_url,
                     "--models", "mock-model", "--json"]) == 0
        capsys.readouterr()
        # 成功或超时都行：本用例只关心"树有没有留下"。Windows 上控制通道不可用会返回 4。
        main(["start", "--timeout", "3", "--json"])
        capsys.readouterr()
        return _wait_for(lambda: process_tree.daemon_pids_for_home(paths.home), timeout=15.0)
    finally:
        server.stop()


def test_second_round_starts_only_after_first_round_is_gone(home, capsys):
    """连跑两次真实 `djb start`：第一轮进程必须在第二轮开始前消失，且两轮后无残留。"""
    paths = Paths(str(home))
    rounds = []
    try:
        for index in (1, 2):
            spawned = _spawn_daemon_like_leaky_test(home, capsys)
            rounds.append(spawned)
            assert spawned, "第 %d 轮没有识别到 daemon 进程（守卫会变成空转，必须查清）" % index

            leftover = process_tree.reap_daemon_tree(paths.home)
            assert leftover == [], "第 %d 轮清理后仍有残留：%r" % (index, leftover)
            still = process_tree.wait_gone(spawned, timeout=10.0)
            assert still == [], "第 %d 轮 spawn 的进程未退出：%r" % (index, still)
            assert process_tree.daemon_pids_for_home(paths.home) == [], (
                "第 %d 轮清理后还能查到 daemon 进程" % index)

        # 第二轮开始时第一轮的进程必须已不存在（pid 不得重叠）
        assert set(rounds[1]).isdisjoint(set(rounds[0])), (
            "第二轮开始时第一轮的进程还在：round1=%r round2=%r" % (rounds[0], rounds[1]))
    finally:
        process_tree.reap_daemon_tree(paths.home)
        assert process_tree.daemon_pids_for_home(paths.home) == []
