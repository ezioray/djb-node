# SPDX-License-Identifier: Apache-2.0
"""S3 共享偏好模型/窗口单元测试（stdlib unittest；不创建 Tk）。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from djb_node.gui.share import (PRESETS, ShareSettings, build_share_view,
                                validate_settings)
from djb_node.gui.window import ShareWindow, save_local


class FakeBackend:
    def __init__(self, settings: ShareSettings) -> None:
        self.settings = settings
        self.views = []
        self.saves = 0
        self.on_change = None
        self.on_save = None
        self.running = False

    def set_handlers(self, *, on_change, on_save) -> None:
        self.on_change, self.on_save = on_change, on_save

    def current_settings(self) -> ShareSettings:
        return self.settings

    def render(self, view) -> None:
        self.views.append(view)

    def run(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False


class ShareModelTests(unittest.TestCase):
    def test_three_presets_network_default_off(self) -> None:
        settings = ShareSettings()
        self.assertEqual(set(PRESETS), {"saver", "balanced", "full"})
        self.assertFalse(settings.network)
        self.assertTrue(settings.realtime and settings.batch)

    def test_view_marks_selected_and_falls_back_to_dash(self) -> None:
        view = build_share_view(ShareSettings(preset="saver"), network_ok=True,
                                upload_mbps=100)
        self.assertEqual([o.key for o in view.options], ["saver", "balanced", "full"])
        self.assertTrue(view.options[0].selected)
        self.assertFalse(view.options[1].selected)
        self.assertIn("只开实时 —", view.estimates)
        self.assertTrue(view.save_enabled)

    def test_network_warning_is_independent_switch(self) -> None:
        view = build_share_view(ShareSettings(network=True), network_ok=False,
                                upload_mbps=18.0, min_upload_mbps=50.0)
        self.assertIn("18", view.warnings[0])
        self.assertIn("50", view.warnings[0])
        self.assertIn("未达标", view.network_badge)
        self.assertTrue(view.network_label.endswith("开启"))

    def test_network_off_has_no_warning(self) -> None:
        view = build_share_view(ShareSettings(network=False), network_ok=False,
                                upload_mbps=18.0)
        self.assertEqual(view.warnings, ())
        self.assertIn("未达标", view.network_badge)

    def test_at_least_one_tier_required(self) -> None:
        with self.assertRaises(ValueError):
            validate_settings(ShareSettings(realtime=False, batch=False))

    def test_bad_inputs_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ShareSettings(preset="turbo").validate()
        with self.assertRaises(ValueError):
            ShareSettings(price_mode="fixed").validate()
        with self.assertRaises(ValueError):
            build_share_view(ShareSettings(), network_ok=True, upload_mbps=-1)


class ShareWindowTests(unittest.TestCase):
    def test_save_only_emits_intent(self) -> None:
        saved = []
        backend = FakeBackend(ShareSettings(preset="saver"))
        window = ShareWindow(backend, backend.settings, save_sink=saved.append,
                             network_ok=True, upload_mbps=120)
        self.assertEqual(len(backend.views), 1)
        window.save()
        self.assertEqual(saved, [ShareSettings(preset="saver")])
        self.assertFalse(backend.running)

    def test_change_recomputes_view(self) -> None:
        backend = FakeBackend(ShareSettings())
        window = ShareWindow(backend, backend.settings, save_sink=lambda _s: None,
                             network_ok=True, upload_mbps=120)
        backend.settings = ShareSettings(preset="full", network=True)
        backend.on_change()
        self.assertEqual(window.settings.preset, "full")
        self.assertEqual(len(backend.views), 2)
        self.assertTrue(backend.views[-1].options[2].selected)

    def test_advanced_toggle(self) -> None:
        backend = FakeBackend(ShareSettings())
        window = ShareWindow(backend, backend.settings, save_sink=lambda _s: None,
                             network_ok=True, upload_mbps=120)
        self.assertFalse(window.view().advanced)
        window.toggle_advanced()
        self.assertTrue(backend.views[-1].advanced)

    def test_save_local_atomic_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "share.json"
            written = save_local(ShareSettings(preset="full", network=True), target)
            self.assertEqual(written, target)
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["preset"], "full")
            self.assertTrue(payload["network"])
            self.assertNotIn("secret", target.read_text(encoding="utf-8").lower())
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
