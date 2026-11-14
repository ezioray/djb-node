# SPDX-License-Identifier: Apache-2.0
"""S3 回归：「daemon 已启用但节点 ID 是空的」——可复现、可判定、有下一步。

三条真 bug（都已核实，不是猜的）：

1. ``bootstrap.start_daemon`` 的 ``no-cli`` / ``timeout`` 两种失败**没有任何文案**
   （``summary_text`` 只处理 started/already-running/errors）→ 状态栏一片安静；
2. 桌面端 ``_access_section`` 只用 ``config.node_id``（用户手填字段）查云端绑定，
   而注册成功根本不写 access.json → 真机上绑定卡恒为「暂不可用」；
3. ``NodeCloudClient`` 没有 ``binding()`` 方法 → ``_cloud_call("binding", …)`` 永远拿到
   「云端接口未接线（binding）」，即便传对了 node_id 也查不到。

复现路径（不需要真机）：临时 ``DJB_HOME`` + "冻结的桌面端旁边没有 djb-node.exe"
→ ``start_daemon`` 返回 ``no-cli`` → 三级回退全空 → 页面必须说出真因 + 下一步。
"""
from __future__ import annotations

import sys

import pytest

from djb_node import bootstrap
from djb_node.desktop import views as vw
from djb_node.desktop.datasource import LocalDataSource, ProviderError


# ─────────────────────────────────────────── 1) 自举失败不再静默
@pytest.fixture
def frozen_without_cli(tmp_path, monkeypatch):
    """模拟「只解压出 djb-desktop.exe」：冻结 + 同目录没有 djb-node.exe。"""
    desktop = tmp_path / "app"
    desktop.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(desktop / "djb-desktop.exe"))
    monkeypatch.setattr(bootstrap, "control_ping", lambda *a, **k: False)
    return tmp_path / "home"


def test_start_daemon_no_cli_reason(frozen_without_cli):
    result = bootstrap.start_daemon(home=frozen_without_cli, wait_seconds=0)
    assert result["reason"] == "no-cli"
    assert result["started"] is False


def test_summary_text_explains_missing_exe(frozen_without_cli):
    result = bootstrap.start_daemon(home=frozen_without_cli, wait_seconds=0)
    line = bootstrap.summary_text({}, result)
    assert "djb-node.exe" in line, line
    assert "整体解压" in line, line
    assert "先启动一次" not in line


def test_summary_text_explains_timeout(tmp_path, monkeypatch):
    """启动超时：给日志路径（客户能自己去看），不是沉默。"""
    monkeypatch.setattr(bootstrap, "control_ping", lambda *a, **k: False)
    monkeypatch.setattr(bootstrap, "_daemon_command", lambda paths, config: ["fake-node", "start"])
    monkeypatch.setattr(bootstrap, "load_config", lambda paths: object())

    class FakePopen:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(bootstrap.subprocess, "Popen", FakePopen)
    result = bootstrap.start_daemon(home=tmp_path, wait_seconds=0)
    assert result["reason"] == "timeout"
    line = bootstrap.summary_text({}, result)
    assert "超时" in line and "日志" in line, line
    assert "daemon.out" in line or "logs" in line


def test_daemon_reason_text_is_empty_for_unknown():
    assert bootstrap.daemon_reason_text("started") == ""
    assert bootstrap.daemon_reason_text("no-cli")
    assert bootstrap.daemon_reason_text("timeout", "/x/logs/daemon.out").endswith("daemon.out 末尾的报错")


# ─────────────────────────────────────────── 2) 三级回退全空 → 页面说真因
def _source(tmp_path, boot=None):
    return LocalDataSource(home=str(tmp_path), cloud_factory=lambda _h: None,
                           which=lambda _n: None, boot=boot)


def test_access_section_reports_every_level(tmp_path):
    access = _source(tmp_path, boot={"reason": "no-cli"})._access_section()

    assert access["node_id"] == ""
    levels = access["node_id_diag"]
    assert [row["level"] for row in levels] == ["access.json（手填）", "本机凭据（注册下发）",
                                               "状态库 kv(node)"]
    assert all(row["ok"] is False for row in levels)
    assert all(row["reason"] for row in levels), "每一级都要说清为什么读不到"
    # 真因来自自举结果（status 只知道「daemon 未运行」）
    assert "djb-node.exe" in access["daemon"]["reason"]
    assert access["daemon"]["running"] is False


def test_view_shows_actionable_next_step_without_node_id(tmp_path):
    snapshot = _source(tmp_path, boot={"reason": "no-cli"}).snapshot()
    view = vw.build_view("access", snapshot)

    field = next(item for item in view.fields if item.key == "node_id")
    assert "尚未生成" in field.value and field.value != "先启动一次"
    assert "djb-node.exe" in field.hint

    daemon_field = next(item for item in view.fields if item.key == "daemon_state")
    assert "未运行" in daemon_field.value
    assert "djb-node.exe" in daemon_field.hint

    register = next(item for item in view.fields if item.key == "register_state")
    assert "未注册" in register.value
    # 最靠后的失败原因（凭据级）就是要客户执行的下一步：把节点进程拉起来
    assert register.hint and ("djb-node.exe" in register.hint or "注册" in register.hint)


def test_view_no_longer_says_start_once_anywhere(tmp_path):
    snapshot = _source(tmp_path, boot={"reason": "no-cli"}).snapshot()
    text = vw.build_view("access", snapshot).plain_text()
    assert "先启动一次" not in text
    assert "djb-node.exe" in text


# ─────────────────────────────────────────── 3) 有凭据就能显示 + 真去查绑定
def _write_credentials(home, node_id="nd_registered"):
    from djb_node.config import Paths
    from djb_node.secure import CredentialStore

    paths = Paths(str(home))
    paths.prepare()
    store = CredentialStore(paths)
    store.set_node(node_id, "ck_live_fixture")
    return store


def test_node_id_comes_from_credentials_and_is_readonly(tmp_path):
    _write_credentials(tmp_path)
    snapshot = _source(tmp_path).snapshot()

    access = snapshot.get("access")
    assert access["node_id"] == "nd_registered"
    assert access["node_id_source"] == "credentials"
    field = next(item for item in vw.build_view("access", snapshot).fields if item.key == "node_id")
    assert field.value == "nd_registered" and field.editable is False


class _RecordingCloud:
    def __init__(self, payload=None, error: str = ""):
        self.calls: list[tuple[str, tuple]] = []
        self._payload = payload or {"node_id": "nd_registered", "bound": True}
        self._error = error

    def binding(self, node_id):
        self.calls.append(("binding", (node_id,)))
        if self._error:
            raise ProviderError(self._error)
        return dict(self._payload)


def test_binding_queried_with_resolved_node_id(tmp_path):
    """注册下发的 node_id 也要去查绑定（旧实现只查 config.node_id → 永远查不到）。"""
    _write_credentials(tmp_path)
    cloud = _RecordingCloud()
    source = LocalDataSource(home=str(tmp_path), cloud=cloud, which=lambda _n: None)
    access = source._access_section()

    assert cloud.calls == [("binding", ("nd_registered",))]
    assert access["binding"]["bound"] is True


def test_binding_failure_keeps_card_honest(tmp_path):
    _write_credentials(tmp_path)
    cloud = _RecordingCloud(error="云端连不上（超时）")
    source = LocalDataSource(home=str(tmp_path), cloud=cloud, which=lambda _n: None)
    access = source._access_section()

    assert access["binding"] is None
    assert "云端绑定状态暂不可用" in access["note"]


def test_nodecloud_client_has_binding_method_and_right_path():
    """``_cloud_call("binding")`` 之前 100% 落空：客户端 side 补上这个方法。"""
    from djb_node.desktop.nodecloud import PATHS, NodeCloudClient

    assert PATHS["binding"] == "/api/v1/node/binding"
    seen = {}

    def transport(kind, params):
        seen["kind"] = kind
        return {"node_id": "nd_1", "bound": True}

    client = NodeCloudClient("https://api.example.invalid", "ck_live_x", transport=transport)
    assert client.binding("nd_1") == {"node_id": "nd_1", "bound": True}
    assert seen["kind"] == "binding"


def test_nodecloud_binding_requires_node_id():
    from djb_node.desktop.nodecloud import NodeCloudClient

    client = NodeCloudClient("https://api.example.invalid", "ck_live_x",
                            transport=lambda kind, params: {})
    with pytest.raises(ProviderError):
        client.binding("")
