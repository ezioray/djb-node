# SPDX-License-Identifier: Apache-2.0
"""UI-NODE pure regression tests: no Tk root, network, daemon or services."""
from __future__ import annotations

import argparse
import inspect
import json
from datetime import date
from pathlib import Path

import pytest

from djb_node import cli, facts
from djb_node.config import ClientConfig
from djb_node.gui import window
from djb_node.gui.share import ShareSettings, build_share_view
from djb_node.gui.theme import COLORS
from djb_node.tray import app
from djb_node.tray.actions import FLAG, apply_control, control_enabled
from djb_node.tray.model import INTENTS, TrayAction, TrayModel
from djb_node.tray.panels import PAGES, project_pages


class ShareBackend:
    def __init__(self, settings):
        self.settings, self.views, self.messages = settings, [], []
    def current_settings(self):
        return self.settings
    def set_handlers(self, **handlers):
        self.handlers = handlers
    def render(self, view):
        self.views.append(view)
    def set_status(self, message):
        self.messages.append(message)
    def run(self):
        pass
    def stop(self):
        pass


def share_window(settings=None, sink=lambda _s: None):
    backend = ShareBackend(settings or ShareSettings())
    win = window.ShareWindow(backend, backend.settings, save_sink=sink,
                             network_ok=False, upload_mbps=0)
    return win, backend


def test_save_is_always_draft_and_uses_current_controls():
    values = []
    win, backend = share_window(sink=values.append)
    backend.settings = ShareSettings(preset="saver", price_mode="manual")
    win.save()
    assert values == [backend.settings]
    assert "草稿已保存" in backend.messages[-1]
    assert "尚未生效" in backend.messages[-1]


def test_save_failure_visible_and_does_not_echo_exception():
    def fail(_settings):
        raise OSError("sensitive-error-marker")
    win, backend = share_window(sink=fail)
    win.save()
    assert "失败" in backend.messages[-1]
    assert "尚未生效" in backend.messages[-1]
    assert "sensitive-error-marker" not in backend.messages[-1]


def test_invalid_save_does_not_reach_sink():
    calls = []
    win, backend = share_window(sink=calls.append)
    backend.settings = ShareSettings(realtime=False, batch=False)
    win.save()
    assert calls == []
    assert not backend.views[-1].save_enabled


def test_edit_invalidates_saved_status():
    win, backend = share_window()
    win.save()
    backend.handlers["on_change"]()
    assert "未保存" in backend.messages[-1]


def test_local_draft_roundtrip_preserves_all_fields(tmp_path):
    path = tmp_path / "draft" / "share-preferences.json"
    settings = ShareSettings(preset="full", network=True, price_mode="manual", realtime=False)
    assert window.save_local(settings, path) == path
    assert window.load_local(path) == settings
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {
        "preset", "realtime", "batch", "network", "price_mode"}
    assert not list(path.parent.glob(".share-*"))


@pytest.mark.parametrize("value", [{"network": "false"}, {"preset": []},
                                    {"price_mode": {}}, [], {"realtime": 1}])
def test_malformed_draft_rejected(tmp_path, value):
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        window.load_local(path)


def test_readback_mismatch_is_not_success(tmp_path, monkeypatch):
    monkeypatch.setattr(window, "load_local", lambda _p: ShareSettings(preset="saver"))
    with pytest.raises(ValueError, match="回读"):
        window.save_local(ShareSettings(), tmp_path / "draft.json")


def test_draft_home_explicit_precedes_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DJB_HOME", str(tmp_path / "env"))
    assert window.draft_path() == tmp_path / "env" / "share-preferences.json"
    assert window.draft_path(str(tmp_path / "explicit")) == tmp_path / "explicit" / "share-preferences.json"


def test_gui_wrapper_preserves_home_and_does_not_force_preset(monkeypatch):
    seen = []
    monkeypatch.setattr(window, "main", lambda argv: seen.append(argv) or 0)
    args = argparse.Namespace(home="draft-home", preset=None, upload_mbps=0)
    assert cli.cmd_gui(args) == 0
    assert seen[0] == ["--upload-mbps", "0", "--home", "draft-home"]
    args.preset = "saver"
    cli.cmd_gui(args)
    assert seen[-1][-2:] == ["--preset", "saver"]


def test_main_loads_saved_draft_and_preserves_price_mode(tmp_path, monkeypatch):
    window.save_local(ShareSettings(preset="saver", price_mode="manual"),
                      tmp_path / "share-preferences.json")
    seen = []
    def backend(settings):
        seen.append(settings)
        return ShareBackend(settings)
    monkeypatch.setattr(window, "TkBackend", backend)
    assert window.main(["--home", str(tmp_path)]) == 0
    assert seen[0].preset == "saver" and seen[0].price_mode == "manual"


def test_main_unreadable_draft_refuses_overwrite(tmp_path, monkeypatch):
    path = tmp_path / "share-preferences.json"
    path.write_text("broken-json", encoding="utf-8")
    backends = []
    class SavingBackend(ShareBackend):
        def run(self):
            self.handlers["on_save"]()
    def factory(settings):
        backend = SavingBackend(settings)
        backends.append(backend)
        return backend
    monkeypatch.setattr(window, "TkBackend", factory)
    assert window.main(["--home", str(tmp_path)]) == 0
    assert path.read_text(encoding="utf-8") == "broken-json"
    assert "失败" in backends[0].messages[-1]


def test_unknown_network_not_presented_as_measured_zero():
    view = build_share_view(ShareSettings(network=True), network_ok=False, upload_mbps=0)
    assert "未检测" in view.network_badge
    assert "不会实际开启" in view.warnings[0]
    assert "0 Mbps" not in " ".join(view.warnings)


def test_render_retains_widgets_and_price_mode():
    assert "destroy(" not in inspect.getsource(window.TkBackend.render)
    assert "replace(self._settings" in inspect.getsource(window.TkBackend.current_settings)
    assert "<Control-s>" in inspect.getsource(window.TkBackend)
    assert "<Escape>" in inspect.getsource(window.TkBackend)


def _luminance(color):
    rgb = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return sum(a * b for a, b in zip(linear, (0.2126, 0.7152, 0.0722)))


@pytest.mark.parametrize("color", ["text", "muted", "cyan", "violet", "wait", "danger"])
def test_theme_text_contrast(color):
    assert (_luminance(COLORS[color]) + .05) / (_luminance(COLORS["panel"]) + .05) >= 4.5


def test_theme_control_boundary_contrast():
    assert (_luminance(COLORS["border"]) + .05) / (_luminance(COLORS["input"]) + .05) >= 3


def test_control_flag_canonical_default_and_expiry():
    spec = facts.FEATURE_FLAGS[FLAG]
    assert spec["default"] is False
    assert all(spec[key] for key in ("owner", "expire_at", "rollback_rule"))
    config = ClientConfig()
    assert not control_enabled(config, today=date(2026, 10, 31))
    config.extra = {"feature_flags": {FLAG: True}}
    assert control_enabled(config, today=date(2026, 10, 31))
    assert not control_enabled(config, today=date(2027, 1, 1))


def test_disabled_cancelled_and_unsupported_controls_never_call():
    calls = []
    for action, enabled, confirm in ((TrayAction.PAUSE, False, True),
                                     (TrayAction.PAUSE, True, False),
                                     (TrayAction.QUIT, True, True)):
        result = apply_control(action, call=lambda *a, **k: calls.append(a),
                               enabled=lambda: enabled, confirm=lambda _m: confirm)
        assert not result.verified
    assert calls == []


@pytest.mark.parametrize("action,before,after", [
    (TrayAction.PAUSE, {"state": "online"}, {"state": "paused", "accepting": False}),
    (TrayAction.RESUME, {"state": "paused", "reason": "user_paused"},
     {"state": "online", "accepting": True}),
])
def test_control_independent_readback(action, before, after):
    calls = []
    responses = iter([{"ok": True, "status": before}, {"ok": True}, {"ok": True, "status": after}])
    def call(command, **kwargs):
        calls.append(command["cmd"])
        return next(responses)
    result = apply_control(action, call=call, enabled=lambda: True, confirm=lambda _m: True)
    assert result.verified
    assert calls == ["status", action.value, "status"]


@pytest.mark.parametrize("status", [{}, {"state": "error"}, {"state": "updating"},
                                     {"state": "paused"}, {"state": "paused", "reason": "yield_paused"}])
def test_unknown_or_guard_pause_cannot_resume(status):
    calls = []
    def call(command, **kwargs):
        calls.append(command["cmd"])
        return {"ok": True, "status": status}
    result = apply_control(TrayAction.RESUME, call=call, enabled=lambda: True, confirm=lambda _m: True)
    assert not result.verified and calls == ["status"]


def test_readback_failure_does_not_claim_success_or_retry():
    calls = []
    def call(command, **kwargs):
        calls.append(command["cmd"])
        if len(calls) == 3:
            raise OSError("sensitive-marker")
        return {"ok": True, "status": {"state": "online"}}
    result = apply_control(TrayAction.PAUSE, call=call, enabled=lambda: True, confirm=lambda _m: True)
    assert not result.verified and "未知" in result.message
    assert "sensitive-marker" not in result.message
    assert calls == ["status", "pause", "status"]


def test_expired_while_confirmation_open_does_not_mutate():
    values = iter([True, False])
    calls = []
    def call(command, **kwargs):
        calls.append(command["cmd"])
        return {"ok": True, "status": {"state": "online"}}
    result = apply_control(TrayAction.PAUSE, call=call, enabled=lambda: next(values), confirm=lambda _m: True)
    assert not result.verified and calls == ["status"]


def test_strict_ui_providers_preserve_unknown(monkeypatch):
    monkeypatch.setattr(app, "_run_cli_json", lambda *a, **k: None)
    assert app.desktop_status() == {"state": "unknown"}
    assert app.desktop_notifications() == {"known": False}


def test_all_pages_and_unknowns_and_no_raw_credential_fields():
    data = {"state": "online", "accepting": True, "local_token": "hidden-marker",
            "credentials": {"key": "hidden-marker"}, "reason": "hidden-marker",
            "balance": "12.3400", "today_earnings": None}
    pages = project_pages(data, None, status_known=True, notifications_known=False)
    assert set(pages) == set(PAGES)
    text = "\n".join(pages.values())
    assert "hidden-marker" not in text
    assert "12.3400" in pages["收益"]
    assert "读取失败" in pages["通知"]
    assert "— 暂不可用" in pages["收益"]


def test_silent_p0_first_and_unknown_severity_not_p2():
    rows = [{"severity": "P2", "template_key": "notice.generic"},
            {"severity": "P0", "template_key": "quota.exhausted", "payload": "hidden-marker",
             "title": "hidden-marker", "action_url": "hidden-marker"},
            {"template_key": "unmapped"}]
    text = project_pages({}, rows, status_known=False, notifications_known=True)["通知"]
    assert text.index("P0") < text.index("P2")
    assert "未知级别" in text
    assert "hidden-marker" not in text
    assert "不播放声音" in text


def test_guides_are_visible_without_execution():
    class Backend:
        def set_control_enabled(self, enabled):
            pass
        def show_feedback(self, title, message):
            self.feedback = (title, message)
    backend = Backend()
    sink = app.desktop_intent_sink(backend, refresh=lambda: None)
    sink(INTENTS[TrayAction.DIAGNOSE])
    assert "未执行" in backend.feedback[0]
    assert "djb diagnose --json" in backend.feedback[1]


def test_menu_control_is_explicit_guide_by_default():
    backend = app.TkBackend.__new__(app.TkBackend)
    backend._context = ({"state": "online"}, ())
    backend._control_enabled = lambda: False
    item = TrayModel.build({"state": "online"}).item("pause")
    assert "命令指南·未执行" in backend._visible_label(item)


def test_no_exception_or_stderr_logging():
    assert "str(exc)" not in inspect.getsource(app.TrayApp._collect)
    assert "completed.stderr" not in inspect.getsource(app._run_cli_json)


@pytest.mark.parametrize("mode", ["raise", "none"])
def test_provider_failure_metadata_reaches_real_ui_context(mode):
    class Backend:
        def set_activate_handler(self, handler): pass
        def set_title(self, text): pass
        def set_menu_items(self, items): pass
        def set_badge(self, badge): pass
        def set_context(self, status, notifications):
            self.notifications = notifications
    def provider():
        if mode == "raise":
            raise OSError("private-marker")
        return None
    backend = Backend()
    tray = app.TrayApp(backend, lambda: {}, provider, lambda _intent: None)
    tray.refresh()
    assert backend.notifications == {"known": False}


def test_malformed_notification_rows_are_unknown_not_empty(monkeypatch):
    monkeypatch.setattr(app, "_run_cli_json", lambda *a, **k: {"items": [1, None]})
    assert app.desktop_notifications() == {"known": False}


def test_windows_tray_control_fail_closed_even_when_flag_on(monkeypatch):
    from djb_node import config
    class Backend:
        def set_control_enabled(self, enabled): self.enabled = enabled
    backend = Backend()
    monkeypatch.setattr(app.sys, "platform", "win32")
    monkeypatch.setattr(config, "load_config", lambda _paths: {"feature_flags": {FLAG: True}})
    app.desktop_intent_sink(backend, refresh=lambda: None)
    assert backend.enabled() is False
