# SPDX-License-Identifier: Apache-2.0
"""UX-E2E 第二轮真机回归 · R2-8：既有测试机的 RC 云端地址 → 只读展示 + 明确确认的「恢复生产」。

证据：``reports/UX-E2E-20261121/evidence/round2-client-20261121T1355/``。
背景：``bootstrap.ensure_ready`` **只**修占位符地址（自建/内网部署也是合法配置，不能自动覆盖），
所以「切回生产」只能是客户明确要的动作：先让他看见「我这台在连哪」，再给一颗**要确认**的按钮。

**不写生产、不联网**：全部用例在 ``tmp_path`` 里造 ``config.json``，``cli`` 用假实现。
"""
from __future__ import annotations

import pathlib
import tempfile
from typing import Any

import pytest

from djb_node.desktop import controller as desktop_controller
from djb_node.desktop.datasource import FakeDataSource, Snapshot
from djb_node.desktop.views import build_view


# ───────────────────────────────────────────── R2-8：当前网关（只读）+ 恢复生产（要确认）
RC_URL = "https://rc.example.test"
PROD_URL = "https://tkbank.ai"


@pytest.fixture()
def rc_home(tmp_path: pathlib.Path) -> pathlib.Path:
    """临时 HOME + 一份写着 RC 地址的 config.json（真机测试机的形状）。"""
    from djb_node.config import Paths, load_config, save_config

    home = tmp_path / "home"
    home.mkdir()
    paths = Paths(str(home))
    paths.prepare()
    config = load_config(paths)
    config.gateway_url = RC_URL
    save_config(paths, config)
    return home


def _gateway_of(home: pathlib.Path) -> str:
    from djb_node.config import Paths, load_config

    return str(load_config(Paths(str(home))).gateway_url)


def _access_snapshot(home: pathlib.Path) -> Snapshot:
    from djb_node.desktop.datasource import LocalDataSource

    source = LocalDataSource(home=str(home), runner=lambda args: {"state": "stopped"},
                             cloud=None, cloud_factory=lambda _home: None, boot={})
    section = dict(source._access_section())             # noqa: SLF001 - 直接测数据域
    section.update({"daemon": {"running": False, "reason": "", "reason_code": ""}, "diag": []})
    return Snapshot({"access": section})


def _rc_controller(home: pathlib.Path, **kwargs: Any):
    """控制器 + 一个有 ``_home`` 的数据源（动作里读配置就是靠它）；cli 默认不接。"""
    source = FakeDataSource(_access_snapshot(home).sections)
    source._home = str(home)                             # noqa: SLF001 - 真实数据源同名字段
    return desktop_controller.DesktopController(source, **kwargs)


def _gateway_field(view) -> Any:
    return next(field for field in view.fields if field.key == "gateway_url")


def _gateway_action(view) -> Any:
    return next((a for a in view.actions if a.key == "gateway.restore_production"), None)


def test_gateway_is_shown_read_only_and_marked_off_production(rc_home):
    view = build_view("access", _access_snapshot(rc_home))
    field = _gateway_field(view)
    assert field.value == RC_URL
    assert field.tone == "amber" and not field.editable
    assert PROD_URL in field.hint and "与生产不一致" in field.hint
    action = _gateway_action(view)
    assert action is not None and "恢复生产地址" in action.label


def test_production_gateway_shows_green_and_no_restore_button():
    from djb_node.config import Paths, load_config, save_config

    import tempfile

    home = pathlib.Path(tempfile.mkdtemp(prefix="djb-r2-prod-"))
    paths = Paths(str(home))
    paths.prepare()
    config = load_config(paths)
    config.gateway_url = PROD_URL
    save_config(paths, config)
    view = build_view("access", _access_snapshot(home))
    field = _gateway_field(view)
    assert field.value == PROD_URL and field.tone == "mint"
    assert _gateway_action(view) is None, "已经是生产地址还挂「恢复生产」会让客户以为坏了"


def test_restore_cancel_changes_nothing(rc_home):
    controller = _rc_controller(rc_home)
    asked: list[str] = []
    result = controller.action("gateway.restore_production",
                               confirm=lambda message: (asked.append(message), False)[1])
    assert result.ok is False and result.effect == "cancelled"
    assert asked and RC_URL in asked[0] and PROD_URL in asked[0]   # 确认框写明「从哪到哪」
    assert _gateway_of(rc_home) == RC_URL, "取消后地址被改了"


def test_restore_writes_production_and_restarts_the_node(rc_home):
    calls: list[list[str]] = []
    controller = _rc_controller(
        rc_home, cli=lambda args: (calls.append([str(a) for a in args]), {"ok": True})[1])
    result = controller.action("gateway.restore_production", confirm=lambda _m: True)
    assert result.ok, result.message
    assert _gateway_of(rc_home) == PROD_URL
    assert calls == [["stop"], ["start"]], "改完地址必须重启节点，否则新地址不生效"
    assert PROD_URL in result.message


def test_restore_without_cli_says_it_did_not_restart(rc_home):
    """没有命令通道：地址改了但节点没重启 —— 必须说出来，不能假装成功。"""
    controller = _rc_controller(rc_home, cli=None)
    result = controller.action("gateway.restore_production", confirm=lambda _m: True)
    assert _gateway_of(rc_home) == PROD_URL
    assert "没能重启" in result.message and "重启客户端后生效" in result.message


def test_restore_failure_after_write_keeps_the_fact_that_it_changed(rc_home):
    def broken(_args):
        raise RuntimeError("命令通道炸了")

    controller = _rc_controller(rc_home, cli=broken)
    result = controller.action("gateway.restore_production", confirm=lambda _m: True)
    assert result.ok is False
    assert _gateway_of(rc_home) == PROD_URL          # 写入已发生，不能谎报「没动」
    assert "已改成" in result.message and "重启节点失败" in result.message


def test_restore_on_production_address_is_a_noop(rc_home):
    from djb_node.config import Paths, load_config, save_config

    paths = Paths(str(rc_home))
    config = load_config(paths)
    config.gateway_url = PROD_URL
    save_config(paths, config)
    controller = _rc_controller(rc_home)
    result = controller.action("gateway.restore_production", confirm=lambda _m: True)
    assert "已经是生产地址" in result.message
    assert _gateway_of(rc_home) == PROD_URL
