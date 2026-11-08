"""控制器：部分域刷新必须合并，不能把其它页清空（R2 功能回归）。"""
from __future__ import annotations

from djb_node.desktop.controller import DesktopController
from djb_node.desktop.datasource import FakeDataSource


def _controller() -> DesktopController:
    return DesktopController(source=FakeDataSource({
        "logs": {"path": "/tmp/demo.log",
                 "rows": [{"level": "info", "ts": "2026-11-01T00:00:00Z", "event": "boot"}]},
        "gpu": {"name": "RTX 4070", "memory_total_mb": 12282},
        "status": {"node_id": "node-7c31", "state": "running"},
    }))


def test_partial_refresh_keeps_other_sections() -> None:
    controller = _controller()
    controller.refresh()
    assert controller.snapshot.ok("logs")
    assert controller.snapshot.ok("gpu")

    controller.refresh(sections=("gpu",))
    assert controller.snapshot.ok("gpu")
    assert controller.snapshot.ok("logs"), "刷新 GPU 后日志域被清空"
    assert controller.snapshot.ok("status"), "刷新 GPU 后状态域被清空"
    rows = controller.snapshot.get("logs")["rows"]
    assert rows and rows[0]["event"] == "boot"


def test_partial_refresh_updates_only_named_section() -> None:
    controller = _controller()
    controller.refresh()
    before = dict(controller.snapshot.get("gpu"))
    controller.refresh(sections=("gpu",))
    after = controller.snapshot.get("gpu")
    assert after["name"] == before["name"]
    assert controller.snapshot.get("logs")["path"] == "/tmp/demo.log"


class _Broken:
    def snapshot(self, *, sections=None):
        raise RuntimeError("boom")


def test_failed_partial_refresh_marks_only_named_sections() -> None:
    controller = _controller()
    controller.refresh()
    controller.source = _Broken()                   # type: ignore[assignment]
    controller.refresh(sections=("gpu",))
    assert not controller.snapshot.ok("gpu")
    assert controller.snapshot.ok("logs"), "失败的部分刷新不应清空其它域"
    assert "刷新失败" in controller.message
