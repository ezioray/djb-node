"""清退恢复（2026-11-19 真机实测）：`retired` 不可逆，恢复 = 本机重启节点 → 重新注册。

云端 214 起，同指纹再注册会**新建** node_id（旧节点留作审计档案）。本机只负责
「停掉持有旧身份的 daemon，再启动一次」——注册发生在启动时，不需要清凭据。
"""
from __future__ import annotations

from djb_node.desktop import controller as ctrl_mod
from djb_node.desktop.datasource import FakeDataSource
from djb_node.desktop.datasource import ProviderError


class _Cli:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.fail_on and self.fail_on in argv:
            raise ProviderError("djb start 失败（控制口 10 秒未就绪）")
        return {"ok": True}


def _controller(reason: str, *, cli=None) -> ctrl_mod.DesktopController:
    source = FakeDataSource({"access": {"daemon": {"running": True, "state": "paused",
                                                  "reason": "已清退" if reason == "retired"
                                                            else "未绑定",
                                                  "reason_code": reason}}})
    controller = ctrl_mod.DesktopController(source=source, cli=cli or _Cli())
    controller.refresh()
    return controller


def test_reset_node_is_a_noop_when_the_node_is_not_retired():
    cli = _Cli()
    controller = _controller("unbound", cli=cli)
    result = controller.action("access.reset_node", confirm=lambda _m: True)
    assert not result.ok and "没有被清退" in result.message
    assert cli.calls == []


def test_reset_node_requires_confirmation_and_touches_nothing_when_cancelled():
    cli = _Cli()
    controller = _controller("retired", cli=cli)
    result = controller.action("access.reset_node", confirm=lambda _m: False)
    assert not result.ok and "已取消" in result.message
    assert cli.calls == []


def test_reset_node_restarts_the_daemon_so_it_registers_again():
    cli = _Cli()
    controller = _controller("retired", cli=cli)
    result = controller.action("access.reset_node", confirm=lambda _m: True)
    assert cli.calls == [["stop"], ["start"]]          # 顺序固定：先停旧的，再启动注册
    assert result.ok and "重新注册" in result.message
    assert "绑定" in result.message                     # 新节点还要重新绑定，得说清


def test_reset_node_reports_a_failed_restart_instead_of_pretending():
    cli = _Cli(fail_on="start")
    controller = _controller("retired", cli=cli)
    result = controller.action("access.reset_node", confirm=lambda _m: True)
    assert not result.ok and "重新注册未完成" in result.message
    assert "logs/djb.log" in result.message
