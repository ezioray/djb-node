# SPDX-License-Identifier: Apache-2.0
"""M15-TR1：客户端代理偏好只做纯函数，不联网、不伪造账单。"""
import json
import unittest

from djb_node.errors import UsageError
from djb_node.proxy import (EFFORTS, ProxyPreferences, build_chat_request)


class ProxyPreferencesTests(unittest.TestCase):
    def test_default_adds_nothing(self) -> None:
        payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}],
                   "max_tokens": 8}
        out = build_chat_request(payload, ProxyPreferences())
        self.assertEqual(out, payload)
        self.assertEqual(json.dumps(out, ensure_ascii=False),
                         json.dumps(payload, ensure_ascii=False))

    def test_no_prefs_object_adds_nothing(self) -> None:
        payload = {"model": "m", "messages": []}
        self.assertEqual(build_chat_request(payload), payload)

    def test_all_four_efforts_are_selectable(self) -> None:
        for effort in EFFORTS:
            prefs = ProxyPreferences(effort=effort)
            self.assertEqual(build_chat_request({}, prefs), {"djb_effort": effort})
            self.assertEqual(prefs.describe()["effort"], effort)

    def test_pair_composition_and_explicit_effort(self) -> None:
        prefs = ProxyPreferences(composition="mono_pair", effort="high",
                                 proxy_mode="balanced")
        self.assertEqual(build_chat_request({}, prefs), {
            "djb_composition": "mono_pair",
            "djb_effort": "high",
            "djb_proxy_mode": "balanced",
        })

    def test_auto_is_rejected_for_pair(self) -> None:
        for composition in ("mono_pair", "hetero_pair"):
            prefs = ProxyPreferences(composition=composition, effort="auto")
            with self.assertRaises(UsageError) as caught:
                prefs.validate()
            self.assertIn("single", caught.exception.message)
            self.assertEqual(caught.exception.details["effort"], "auto")

    def test_auto_is_allowed_for_single(self) -> None:
        prefs = ProxyPreferences(effort="auto")
        self.assertEqual(prefs.fields(), {"djb_effort": "auto"})

    def test_unknown_values_are_rejected(self) -> None:
        for prefs in (ProxyPreferences(effort="turbo"),
                      ProxyPreferences(composition="triple"),
                      ProxyPreferences(proxy_mode="max")):
            with self.assertRaises(UsageError):
                prefs.validate()

    def test_defaults_are_described_without_fake_money(self) -> None:
        described = ProxyPreferences().describe()
        self.assertEqual(described["composition"], "single")
        self.assertIn("auto", described["effort"])
        self.assertIn("fidelity", described["proxy_mode"])
        self.assertNotIn("saved", described)
        self.assertNotIn("billable", described)

    def test_build_does_not_mutate_payload(self) -> None:
        payload = {"model": "m", "messages": []}
        build_chat_request(payload, ProxyPreferences(effort="low"))
        self.assertEqual(payload, {"model": "m", "messages": []})

    def test_invalid_prefs_do_not_partially_apply(self) -> None:
        with self.assertRaises(UsageError):
            build_chat_request({"model": "m"}, ProxyPreferences(
                composition="mono_pair", effort="auto"))


if __name__ == "__main__":
    unittest.main()
