# SPDX-License-Identifier: Apache-2.0
"""R2.1 ③：「测试联通性」点了必须有反应 —— 200ms 内出状态、结果区、下一步。

来源：`CHG-20261123-R2-1-NODE-QUICKWINS` ③（真机缺陷：点下去窗口冻住、没有任何反馈）。

这条钉三件事：

1. **不许卡主线程**：动作返回时网络还没跑（实测 < 200ms），状态已经是「进行中」；
2. **结果区常驻**：成功 / 失败都写进页面（``token_plan.connectivity``），不是只闪一下状态栏；
3. **失败给下一步**：401 / 404 / 连不上各有可执行的一句话，不是「失败了」了事。
"""
from __future__ import annotations

import time

import pytest

from djb_node.desktop import views as vw
from djb_node.desktop.app import demo_snapshot
from djb_node.desktop.controller import DesktopController
from djb_node.desktop.datasource import FakeDataSource
from tests import tk_support


def _all_texts(widget) -> list[str]:
    """递归收集控件文字（版面是嵌套 Frame，只看直接子控件会漏）。"""
    found: list[str] = []
    try:
        keys = widget.keys()
        if "text" in keys:
            found.append(str(widget.cget("text")))
    except Exception:                                # pragma: no cover - 非文本控件
        pass
    try:
        for child in widget.winfo_children():
            found.extend(_all_texts(child))
    except Exception:                                # pragma: no cover - 已销毁
        pass
    return found


class SlowSource(FakeDataSource):
    """上游发现**故意慢 1 秒**：主线程若同步跑，200ms 断言必然失败。"""

    def __init__(self, *args, result=None, delay=1.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.result = result or {"total": 3, "mapped": 2, "latency_ms": 41}
        self.delay = delay
        self.discover_calls: list[tuple] = []

    def discover_upstream_models(self, base_url, protocol, secret):
        self.discover_calls.append((base_url, protocol, secret))
        time.sleep(self.delay)
        return dict(self.result)


def _controller(**kwargs):
    source = SlowSource(dict(demo_snapshot().sections), **kwargs)
    controller = DesktopController(source)
    controller.drafts.update({"base_url": "https://api.example.com/v1",
                              "protocol": "openai", "upstream_secret": "sk-test"})
    return controller, source


def test_action_returns_within_200ms_and_already_says_running():
    controller, source = _controller()
    dispatched: list[dict] = []
    controller.connectivity_via = lambda payload: dispatched.append(dict(payload)) or True
    started = time.monotonic()
    result = controller.action("token_plan.test_connectivity")
    elapsed = time.monotonic() - started
    assert elapsed < 0.2, "动作阻塞了 %.3fs（网络还在主线程里）" % elapsed
    assert result.effect == "connectivity" and result.payload.get("base_url")
    assert dispatched and dispatched[0]["base_url"] == "https://api.example.com/v1"
    assert controller.connectivity_state()["state"] == "running"
    assert "进行中" in controller.connectivity_state()["text"]
    assert source.discover_calls == []                      # 主线程一次都没碰网络


def test_view_shows_the_running_state_and_disables_the_button():
    controller, _source = _controller()
    controller.connectivity_via = lambda payload: True
    controller.action("token_plan.test_connectivity")
    view = vw.build_view("token_plan", controller._snapshot_for_views())
    field = next(item for item in view.fields if item.key == "connectivity_state")
    assert "进行中" in field.value and field.tone == "amber"
    assert field.hint                                # 有「下一步」，不是空白
    action = next(item for item in view.actions if item.key == "token_plan.test_connectivity")
    assert action.enabled is False and "正在测试" in action.reason


def test_success_writes_result_and_next_step():
    controller, _source = _controller()
    controller.connectivity_via = lambda payload: True
    controller.action("token_plan.test_connectivity")
    result = controller.run_connectivity({"base_url": "https://api.example.com/v1",
                                          "protocol": "openai", "secret": "sk-test"})
    state = controller.connectivity_state()
    assert result.ok and state["state"] == "ok"
    assert "联通正常" in state["text"] and "3 个模型" in state["text"]
    assert "读取模型清单并保存接入" in state["next"]
    view = vw.build_view("token_plan", controller._snapshot_for_views())
    field = next(item for item in view.fields if item.key == "connectivity_state")
    assert field.tone == "mint" and "联通正常" in field.value


@pytest.mark.parametrize("error_class,status,expect", [
    ("unauthorized", 401, "密钥"),
    ("no_models_api", 404, "/v1"),
    ("unreachable", None, "网络"),
])
def test_failure_maps_to_an_actionable_next_step(error_class, status, expect):
    controller, _source = _controller(result={"error_class": error_class,
                                              "http_status": status,
                                              "note": "连不上"})
    controller.connectivity_via = lambda payload: True
    controller.action("token_plan.test_connectivity")
    result = controller.run_connectivity({"base_url": "https://api.example.com/v1",
                                          "protocol": "openai", "secret": "sk-test"})
    state = controller.connectivity_state()
    assert result.ok is False and state["state"] == "fail"
    assert expect in state["next"], state["next"]


def test_missing_address_fails_fast_with_a_next_step():
    controller, source = _controller()
    controller.drafts["base_url"] = ""
    result = controller.action("token_plan.test_connectivity")
    assert result.ok is False and "请求地址" in result.message
    assert source.discover_calls == []
    assert controller.connectivity_state()["state"] == "fail"


# ── 真窗口：点按钮 → 200ms 内页面上出现「进行中」
@tk_support.needs_tk
def test_real_click_shows_status_text_within_200ms(monkeypatch):
    from djb_node.desktop.app import DesktopApp

    source = SlowSource(dict(demo_snapshot().sections), delay=1.0)
    controller = DesktopController(source)
    root = tk_support.new_root()
    app = DesktopApp(controller, root=root)
    try:
        app.show_view("token_plan")
        root.update()
        app._vars["base_url"].set("https://api.example.com/v1")
        app._vars["upstream_secret"].set("sk-test")
        started = time.monotonic()
        app._on_action("token_plan.test_connectivity")
        elapsed = time.monotonic() - started
        assert elapsed < 0.2, "点了 %.3fs 才回来（主线程被网络挡住）" % elapsed
        root.update()
        blob = "\n".join(_all_texts(app._body))
        assert "进行中" in blob, "点了之后页面上没有「进行中」"
        # 网络在 worker 里跑（1 秒慢调用），点击后主线程立刻回来并且状态已上屏
        assert getattr(app._connectivity_worker, "busy", False) is True
        assert len(source.discover_calls) <= 1
    finally:
        try:
            app.stop()
        except Exception:                            # pragma: no cover
            pass
        try:
            root.destroy()
        except Exception:                            # pragma: no cover
            pass
