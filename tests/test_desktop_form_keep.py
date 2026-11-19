# SPDX-License-Identifier: Apache-2.0
"""CR-20261119-CLIENT-TOKENPLAN-NOOP F2/F5：点一下按钮，输入框不许被清空。

客户原话：「客户端分享 tokenplan 页面无论是点击保存还是点击获取模型都显示清空页面后
没有任何反应」。根因之一（R2）是``_render`` 每次都 ``_clear()`` 重建，而
``prefill=False`` / ``secret`` 的输入框一律从空串起——客户刚填的请求地址、密钥、
最低申报价在**点击瞬间**就没了。

这一组用例只验两件事：
* 同页重画后，客户打好的字原样还在（含密钥，仍掩码显示）；
* 动作说「已经落盘」的字段（``payload["forget"]``）下一轮重画时被清掉；且不跨页串值。
"""
from __future__ import annotations

from types import SimpleNamespace

from djb_node.desktop import app as desktop_app
from djb_node.desktop import views as vw
from tests.tk_support import needs_tk


class _Snapshot(dict):
    """空快照：``get()`` / ``ok()`` 与真快照同形。"""

    source = "file"
    fetched_at = "2026-11-19T12:00:00+08:00"

    def ok(self, name: str) -> bool:
        return False


class _StubController:
    """只够画一页的控制器（不碰数据源、不跑动作）。"""

    view = "token_plan"
    message = ""
    auto_refresh = False
    save_path = None
    ask_code = None
    snapshot = _Snapshot()

    def current(self) -> vw.View:
        return _view()

    def show(self, key: str) -> vw.View:
        return _view(str(key))

    def onboarding_panel(self):                    # noqa: D102 - 引导面板不参与本组用例
        return None

    def set_draft(self, key: str, value: str) -> None:
        pass

    def status_line(self) -> str:
        return "测试状态条"

    def action(self, key: str, *, confirm=None):
        raise AssertionError("本组用例不跑动作：" + key)


def _view(key: str = "token_plan") -> vw.View:
    """与「提供 Token Plan」同形的字段组：预填 / 不预填 / 密钥三种都有。"""
    return vw.View(
        key=key, title="提供 Token Plan",
        fields=(vw.Field("协议", "openai-chat", key="protocol", editable=True,
                         choices=("openai-chat", "anthropic-messages")),
                vw.Field("请求地址", "", key="base_url", editable=True, prefill=False),
                vw.Field("密钥", "未保存", key="upstream_secret", editable=True, secret=True),
                vw.Field("最低申报价", "", key="min_price", editable=True, prefill=False)),
        actions=(vw.Action("token_plan.discover", "读取模型清单并保存接入"),))


def _app() -> object:
    return desktop_app.DesktopApp(_StubController())


def _type(app: object, values: dict[str, str]) -> None:
    for key, value in values.items():
        app._vars[key].set(value)                  # noqa: SLF001 - 等价于客户在框里打字


@needs_tk
def test_typed_values_survive_a_rerender():
    app = _app()
    try:
        app._render(_view())                       # noqa: SLF001
        _type(app, {"base_url": "https://api.example.com/v1",
                    "upstream_secret": "sk-typed-not-saved",
                    "min_price": "0.42"})
        # force=True：视图内容没变时 ``_render`` 会跳过重画，这里要的是**真的重建**
        app._render(_view(), force=True)           # noqa: SLF001 - 等价于点了一次动作后的重画
        assert app._vars["base_url"].get() == "https://api.example.com/v1"
        assert app._vars["upstream_secret"].get() == "sk-typed-not-saved"
        assert app._vars["min_price"].get() == "0.42"
        assert app._entries["upstream_secret"].cget("show") == "•"   # 仍然掩码
    finally:
        app.stop()                                 # noqa: SLF001


@needs_tk
def test_saved_field_is_forgotten_on_the_next_render():
    app = _app()
    try:
        app._render(_view())                       # noqa: SLF001
        _type(app, {"base_url": "https://api.example.com/v1",
                    "upstream_secret": "sk-typed-not-saved"})
        # 动作返回「这个字段已经落盘」→ 下一轮重画不再带回（密钥框回空），地址留着
        app._forget_keys = {"upstream_secret"}     # noqa: SLF001
        app._render(_view(), force=True)           # noqa: SLF001
        assert app._vars["upstream_secret"].get() == ""
        assert app._vars["base_url"].get() == "https://api.example.com/v1"
    finally:
        app.stop()                                 # noqa: SLF001


@needs_tk
def test_values_never_leak_across_pages():
    app = _app()
    try:
        app._render(_view("token_plan"))           # noqa: SLF001
        _type(app, {"base_url": "https://api.example.com/v1"})
        app._render(_view("access"), force=True)   # noqa: SLF001 - 切到另一页
        assert app._vars["base_url"].get() == ""   # 别的页不许看到这串
        app._render(_view("token_plan"), force=True)   # noqa: SLF001 - 切回来
        assert app._vars["base_url"].get() == "https://api.example.com/v1"
    finally:
        app.stop()                                 # noqa: SLF001


@needs_tk
def test_async_worker_result_also_clears_the_saved_secret():
    """异步通道（worker 线程）回来的结果不走 ``_on_action``，也要即用即清。"""
    app = _app()
    try:
        app._render(_view())                       # noqa: SLF001
        _type(app, {"base_url": "https://api.example.com/v1",
                    "upstream_secret": "sk-typed-not-saved"})
        app._absorb_forget(SimpleNamespace(          # noqa: SLF001 - worker 侧结果落地
            payload={"state": "done", "forget": ["upstream_secret"]}))
        app._render(_view(), force=True)           # noqa: SLF001
        assert app._vars["upstream_secret"].get() == ""
        assert app._vars["base_url"].get() == "https://api.example.com/v1"
    finally:
        app.stop()                                 # noqa: SLF001
