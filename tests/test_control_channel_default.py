# SPDX-License-Identifier: Apache-2.0
"""首发默认配置下 Windows 本地控制通道必须可用（task-13）。

三条口径（lead 2026-11-05 验收标准）：

1. **用户真实路径**：发布默认配置（不额外写任何 config）⇒ 开关为真、控制口可用；
   用例故意**不注入**开关 —— 注入只能证明代码能跑，不能证明用户能用。
2. **kill switch 仍然有效**：关掉后降级有**可行动提示**、不崩、`stop --force` 仍然干净；
3. **回滚元数据不许丢**：owner / expire_at / rollback_rule 三字段缺一个就回滚不回去。
"""
from __future__ import annotations

from datetime import date
import os
import subprocess
import sys
from pathlib import Path

import pytest

from djb_node import facts
from djb_node import ipc_windows
from djb_node.cli import main
from djb_node.config import ClientConfig, Paths, feature_flag
from tests import process_tree
from tests import windows_control

WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt",
    reason="本地控制通道只在 Windows 存在；其它平台走 unix socket，与此开关无关",
)


# ─────────────────────────────────────────── 1. 发布默认值本身就是「开」
@WINDOWS_ONLY
def test_shipped_default_config_enables_windows_control():
    """全新配置（没写过任何 feature_flags）⇒ 开关为真（用户装完就能用）。"""
    config = ClientConfig(gateway_url="http://127.0.0.1:9/", mirror_port=1717)
    assert config.extra == {}, "夹具假设：全新配置的 extra 为空"
    assert feature_flag(config, ipc_windows.FLAG) is True


@WINDOWS_ONLY
def test_config_without_feature_flags_section_still_enables_control(tmp_path):
    """即使 config.json 里完全没有 feature_flags 段（老用户升级上来的形态）也必须为真。"""
    from djb_node.config import ClientConfig as _C
    from djb_node.config import load_config

    paths = Paths(str(tmp_path / "home"))
    paths.prepare()
    from djb_node.config import save_config

    save_config(paths, _C(gateway_url="http://127.0.0.1:9/", mirror_port=1717))
    reloaded = load_config(paths)
    assert feature_flag(reloaded, ipc_windows.FLAG) is True


# ─────────────────────────────────────────── 2. kill switch 依然是真开关
def test_kill_switch_metadata_intact():
    """default=True 是「首发默认开」，但三字段必须齐全 —— 否则它不叫开关。"""
    spec = facts.FEATURE_FLAGS[ipc_windows.FLAG]
    assert spec["default"] is True
    assert str(spec.get("owner") or "").strip(), "owner 不能为空（回滚责任人）"
    assert str(spec.get("rollback_rule") or "").strip(), "rollback_rule 不能为空"
    expires = date.fromisoformat(str(spec["expire_at"]))
    assert expires > date(2026, 11, 5), "到期日必须晚于首发日（到期即强制关闭）"


def test_disabled_flag_is_really_off(tmp_path):
    """写盘关掉后，求值必须为假（不是写了个没人读的字段）。"""
    paths = Paths(str(tmp_path / "home"))
    paths.prepare()
    from djb_node.config import save_config

    save_config(paths, ClientConfig(gateway_url="http://127.0.0.1:9/", mirror_port=1717))
    assert windows_control.enabled(paths) is (os.name == "nt")
    if os.name != "nt":
        pytest.skip("kill switch 只作用于 Windows 控制通道")
    assert windows_control.disable(paths) is True
    assert windows_control.enabled(paths) is False


# ─────────────────────────────────────────── 3. 降级：提示可行动 + stop 仍干净
@WINDOWS_ONLY
def test_control_disabled_start_hint_and_force_stop_clean(home, capsys):
    """kill switch 关掉后：`start` 失败但给可行动提示；`stop --force` 仍能收干净。"""
    from tests.mock_m02 import MockServer
    from tests.test_daemon_lifecycle import _free_port

    server = MockServer().start()
    paths = Paths(str(home))
    try:
        assert main(["init", "--no-env", "--gateway", server.base_url,
                     "--port", str(_free_port()), "--cpu", "100", "--gpu", "100",
                     "--mem", "99"]) == 0
        capsys.readouterr()
        assert windows_control.disable(paths) is True

        assert main(["start", "--timeout", "5"]) == 4
        err = capsys.readouterr().err
        # 不是一句"不可用"：必须告诉用户发生了什么、下一步怎么做
        assert "本地控制通道已关闭" in err
        assert "djb stop --force" in err
        assert "config.json" in err
        assert "--foreground" in err

        # 降级路径的真实困境：start 报错，但 daemon 其实已经在跑（这正是必须给提示的理由）
        running = process_tree.daemon_pids_for_home(str(paths.home))
        assert running, "控制口关掉时 daemon 仍会被拉起（这正是提示里要给 --force 的理由）"

        # 提示里写了 `djb stop --force`，就必须真的能用，而且**不许误杀无关进程**：
        # 造一个"看起来像 daemon、但属于别的 home"的诱饵进程，验证它活下来。
        decoy_home = str(home) + "-decoy"
        # 第二个诱饵：--home 取**真实 home 的父目录**（字符串上是被真实 home 前缀的真前缀）。
        # 这是最容易被写成 startswith/子串匹配的地方：若身份校验退化成 startswith 或
        # "cmdline 里出现 home 子串即算自己人"，它就会被误杀（task-12 实现用
        # os.path.normcase(normpath(abspath(...))) 全等比对，本用例是它的护栏）。
        parent_home = str(Path(home).parent)
        decoy = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)",
             "djb_node.daemon", "--home", decoy_home],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        decoy_parent = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)",
             "djb_node.daemon", "--home", parent_home],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            assert decoy.poll() is None and decoy_parent.poll() is None
            assert main(["stop", "--force"]) == 0
            capsys.readouterr()
            decoy.wait(timeout=5) if decoy.poll() is not None else None
            assert decoy.poll() is None, "stop --force 误杀了同名前缀的无关进程"
            assert decoy_parent.poll() is None, "stop --force 误杀了 --home 为父目录的进程（疑似退化成前缀匹配）"
        finally:
            for proc in (decoy, decoy_parent):
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
        # 真的停了：该 home 下不应再有 daemon 进程（不是"打印了未运行"就算了）
        assert process_tree.wait_gone(process_tree.daemon_pids_for_home(str(paths.home)),
                                      timeout=10.0) == []
    finally:
        process_tree.reap_daemon_tree(paths.home)
        server.stop()
