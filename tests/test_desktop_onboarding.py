# SPDX-License-Identifier: Apache-2.0
"""RC14 B：首次引导（3 步 + 完成页，每步可跳过）的状态机、面板投影与真窗口渲染。

硬要求（设计 §3.4，逐条钉住）：

* 每一步都有「跳过这一步」，第一步另有「跳过设置，直接进入」；``Esc`` = 跳过当前步；
* **跳过不写任何凭据**：只写 ``desktop/onboarding.json``（这个用例会逐字读回文件断言）；
* 跳过后主界面照常可用：面板消失、全功能不拦；重跑入口**永久**在「账户与安全」；
* ``djb onboarding --status/--reset`` 可用；开关 ``client.onboarding.enabled`` 默认关。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from djb_node import facts, onboarding as onb_mod
from djb_node.desktop import controller as ctrl_mod
from djb_node.desktop import datasource as ds
from djb_node.desktop import views as vw


# ─────────────────────────────────────────────── 状态机（不碰 Tk、不碰网络）
def test_state_file_path_and_shape_match_the_design():
    assert onb_mod.state_path("/h") == "/h/desktop/onboarding.json"
    state = onb_mod.OnboardingState()
    assert state.should_show() is True                     # 全新机器：该出现
    assert set(state.as_dict()) == {"version", "started_at", "completed_at",
                                    "dismissed_at", "skipped_at_step", "last_step",
                                    "skipped_steps"}


def test_skip_writes_only_progress_and_no_credentials(tmp_path):
    store = onb_mod.OnboardingStore(tmp_path)
    store.skip_step(2)
    raw = json.loads((tmp_path / "desktop" / "onboarding.json").read_text(encoding="utf-8"))
    assert raw["skipped_at_step"] == 2 and raw["started_at"]
    # 文件里不许出现任何像凭据的东西（键名白名单 + 内容不含 sk-）
    assert set(raw) <= {"version", "started_at", "completed_at", "dismissed_at",
                        "skipped_at_step", "last_step", "skipped_steps"}
    text = (tmp_path / "desktop" / "onboarding.json").read_text(encoding="utf-8")
    assert "sk-" not in text and "secret" not in text.lower()


def test_state_file_is_0600(tmp_path):
    store = onb_mod.OnboardingStore(tmp_path)
    store.skip_all()
    mode = (tmp_path / "desktop" / "onboarding.json").stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)


def test_skip_all_marks_dismissed_and_hides_the_panel(tmp_path):
    store = onb_mod.OnboardingStore(tmp_path)
    state = store.skip_all()
    assert state.dismissed_at and state.skipped_at_step == 1
    assert store.load().should_show() is False


def test_complete_stops_showing_but_keeps_history(tmp_path):
    store = onb_mod.OnboardingStore(tmp_path)
    store.complete()
    assert store.load().should_show() is False
    # 重新运行：清 dismissed、保留 completed 历史（设计 §3.4 表格最后一行）
    state = store.load()
    state.dismissed_at = None
    store.save(state)
    assert store.load().completed_at


def test_reset_removes_the_file(tmp_path):
    store = onb_mod.OnboardingStore(tmp_path)
    store.skip_all()
    state = store.reset()
    assert state.should_show() and not store.exists()
    assert (tmp_path / "desktop").exists()                 # 目录保留，不删别人的东西


def test_unknown_and_broken_fields_are_ignored(tmp_path):
    path = tmp_path / "desktop" / "onboarding.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": "x", "last_step": 99, "skipped_steps": [0, 9, "2"],
                                "api_key": "sk-should-not-be-read"}), encoding="utf-8")
    state = onb_mod.OnboardingStore(tmp_path).load()
    assert state.version == onb_mod.SCHEMA_VERSION       # 坏值回默认
    assert state.last_step == onb_mod.TOTAL_STEPS        # 夹到 1..3
    assert state.skipped_steps == [2]                    # 只留合法整数
    assert "api_key" not in state.as_dict()


def test_write_failure_does_not_raise(tmp_path, monkeypatch):
    store = onb_mod.OnboardingStore(tmp_path)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(onb_mod, "write_json", boom)
    assert store.skip_step(1) is not None                # 不抛：引导本身不能被磁盘拦住
    assert store.save(onb_mod.OnboardingState()) is False


# ─────────────────────────────────────────────── 面板投影
def test_panel_has_a_skip_on_every_step():
    for step in (1, 2, 3):
        panel = vw.onboarding_panel(step)
        keys = [action.key for action in panel.actions]
        assert "onboarding.skip" in keys, step
        assert panel.title.startswith("步骤") or "步骤 " + str(step) in panel.title
    first = [action.key for action in vw.onboarding_panel(1).actions]
    assert "onboarding.skip_all" in first                # 只有第一步有「全部跳过」


def test_done_page_reports_what_was_achieved():
    panel = vw.onboarding_panel(4, bound=True, upstream_count=2)
    assert panel.done and panel.title == "完成"
    assert "已绑定" in panel.body and "已提供 2 个接入" in panel.body
    keys = [action.key for action in panel.actions]
    assert "onboarding.finish" in keys and "onboarding.rerun" in keys


def test_done_page_does_not_claim_success_when_nothing_was_done():
    panel = vw.onboarding_panel(4, bound=False, upstream_count=0)
    assert "还没绑定" in panel.body and "还没提供上游" in panel.body
    assert "✓ 已绑定" not in panel.body


# ─────────────────────────────────────────────── 控制器动作
class _Source:
    def __init__(self, home):
        self._home = str(home)
        self.calls = 0

    def snapshot(self, sections=None):
        self.calls += 1
        base = ds.demo_snapshot().sections if hasattr(ds, "demo_snapshot") else {}
        sections_map = dict(base)
        sections_map.setdefault("access", {"config": {}, "binding": {"state": "unbound"}})
        return ds.Snapshot(sections_map)


@pytest.fixture()
def home(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"home": str(tmp_path)}), encoding="utf-8")
    return tmp_path


def _controller(home, monkeypatch, *, enabled=True):
    controller = ctrl_mod.DesktopController(_Source(home))
    monkeypatch.setattr(controller, "onboarding_enabled", lambda: enabled)
    return controller


def test_visible_only_when_flagged_and_incomplete(home, monkeypatch):
    controller = _controller(home, monkeypatch, enabled=False)
    assert controller.onboarding_visible() is False       # 开关关 → 不渲染
    controller = _controller(home, monkeypatch, enabled=True)
    assert controller.onboarding_visible() is True
    controller.onboarding_step = None
    onb_mod.OnboardingStore(home).skip_all()
    assert controller.onboarding_visible() is False       # 跳过后不再自动出现


def test_walk_through_the_three_steps_and_finish(home, monkeypatch):
    controller = _controller(home, monkeypatch)
    controller.onboarding_step = 1
    for expected in (2, 3):
        controller.action("onboarding.next")
        assert controller.onboarding_step == expected
    controller.action("onboarding.next")                  # 第 3 步 → 完成页
    assert controller.onboarding_step == 4
    assert controller.onboarding_panel().done is True
    controller.action("onboarding.finish")
    assert controller.onboarding_step is None
    assert onb_mod.OnboardingStore(home).load().completed_at


def test_skip_step_advances_and_records_which_step(home, monkeypatch):
    controller = _controller(home, monkeypatch)
    controller.onboarding_step = 2
    result = controller.action("onboarding.skip")
    assert result.ok and "已跳过第 2 步" in result.message
    assert controller.onboarding_step == 3
    assert onb_mod.OnboardingStore(home).load().skipped_at_step == 2


def test_skip_all_hides_the_panel_and_keeps_everything_usable(home, monkeypatch):
    controller = _controller(home, monkeypatch)
    controller.onboarding_step = 1
    result = controller.action("onboarding.skip_all")
    assert result.ok and "主界面照常可用" in result.message
    assert controller.onboarding_visible() is False
    # 主界面照常可用：切页/取数不受影响
    controller.show("token_plan")
    assert controller.view == "token_plan"


def test_back_never_goes_below_one(home, monkeypatch):
    controller = _controller(home, monkeypatch)
    controller.onboarding_step = 1
    controller.action("onboarding.back")
    assert controller.onboarding_step == 1


def test_rerun_clears_dismissed_and_starts_over(home, monkeypatch):
    controller = _controller(home, monkeypatch)
    onb_mod.OnboardingStore(home).skip_all()
    result = controller.action("onboarding.rerun")
    assert result.ok
    assert controller.onboarding_step == 1
    state = onb_mod.OnboardingStore(home).load()
    assert state.dismissed_at is None and state.should_show() is True


def test_account_page_rerun_entry_is_flag_gated(home, monkeypatch):
    controller = _controller(home, monkeypatch)
    view = controller.show("account")
    keys = [action.key for action in view.actions]
    assert "account.rerun_onboarding" in keys             # 开关开 → 入口永久在
    controller = _controller(home, monkeypatch, enabled=False)
    keys = [action.key for action in controller.show("account").actions]
    assert "account.rerun_onboarding" not in keys         # 开关关 → 隐藏


def test_account_rerun_refuses_politely_when_the_flag_is_off(home, monkeypatch):
    controller = _controller(home, monkeypatch, enabled=False)
    result = controller.action("account.rerun_onboarding")
    assert not result.ok and "client.onboarding.enabled" in result.message


def test_flag_is_registered_with_three_fields_and_off_by_default():
    spec = facts.FEATURE_FLAGS[facts.ONBOARDING_FLAG]
    assert spec["default"] is False
    for field in facts.FLAG_REQUIRED_FIELDS:
        assert spec.get(field) not in (None, "")
    assert "onboarding.json" in str(spec["rollback_rule"])


# ─────────────────────────────────────────────── CLI（--status / --reset）
def test_cli_status_and_reset(tmp_path, capsys):
    from djb_node import cli

    (tmp_path / "config.json").write_text(json.dumps({"home": str(tmp_path)}), encoding="utf-8")
    onb_mod.OnboardingStore(tmp_path).skip_all()
    code = cli.main(["onboarding", "--home", str(tmp_path), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["show"] is False and payload["state"]["dismissed_at"]
    assert payload["flag"] == facts.ONBOARDING_FLAG
    assert len(payload["steps"]) == 3

    code = cli.main(["onboarding", "--home", str(tmp_path), "--reset"])
    assert code == 0
    assert "已清除引导状态" in capsys.readouterr().out
    assert not onb_mod.OnboardingStore(tmp_path).exists()


# ─────────────────────────────────────────────── 真窗口：面板渲染 + 跳过按钮可点
def test_panel_renders_and_skip_button_works(home, monkeypatch):
    tk = pytest.importorskip("tkinter")
    from djb_node.desktop.app import DesktopApp

    try:
        root = tk.Tk()
    except Exception:                                    # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")
    controller = _controller(home, monkeypatch)
    controller.onboarding_step = 1
    app = DesktopApp(controller, root=root)
    try:
        app.show_view("overview")
        root.update()
        panel = getattr(app, "_onboarding_panel", None)
        assert panel is not None                          # 面板画出来了
        assert controller.onboarding_visible() is True
        # 面板不是模态：主界面照常可切页
        app.show_view("token_plan")
        root.update()
        assert app._controller.view == "token_plan"
        # Esc = 跳过当前步
        app._on_escape()
        root.update()
        assert controller.onboarding_step == 2
        assert onb_mod.OnboardingStore(home).load().skipped_at_step == 1
    finally:
        app.stop()
        try:
            root.destroy()
        except Exception:                                # pragma: no cover
            pass


def test_no_panel_when_the_flag_is_off(home, monkeypatch):
    tk = pytest.importorskip("tkinter")
    from djb_node.desktop.app import DesktopApp

    try:
        root = tk.Tk()
    except Exception:                                    # pragma: no cover
        pytest.skip("没有可用的 Tk 显示")
    controller = _controller(home, monkeypatch, enabled=False)
    app = DesktopApp(controller, root=root)
    try:
        app.show_view("overview")
        root.update()
        assert getattr(app, "_onboarding_panel", None) is None
        # 开关关时状态文件不该被创建
        assert not (pathlib.Path(home) / "desktop" / "onboarding.json").exists()
    finally:
        app.stop()
        try:
            root.destroy()
        except Exception:                            # pragma: no cover
            pass
