# SPDX-License-Identifier: Apache-2.0
"""D16（owner 2026-11-24）：客户端品牌位全部替换为 TokenBank，**标识类不动**。

两条边界互相牵制，缺一条就会跑偏：

* **品牌位**（窗口标题 / 托盘 / 对话框标题 / macOS 关于菜单 / 通知 app_name /
  界面文案 / 诊断包说明 / CLI 帮助）→ ``TokenBank``；
* **标识**（目录名 ``DJB``、``DJB_*`` 环境变量、``djb-node`` 服务名、``DJB-*`` 协议头、
  Tk 字体族名 ``DJB.Body``、包名 ``djb_node``、日志文件名）→ **一个都不许动**，动了就是兼容性事故。

第二类（服务 display_name / 服务安装计划标题）owner 口径可解释为「服务名」也可解释为「品牌位」，
本轮**保持原样**并登记在票面等 lead 裁决，因此这里把它们列进允许残留清单（``BRAND_PENDING``）。
"""
from __future__ import annotations

import inspect
import pathlib

from djb_node import diagnose, facts
from djb_node.desktop import app as desktop_app
from djb_node.desktop import platform_ui, ui_kit, views
from djb_node.platform import paths as platform_paths
from djb_node.platform import service as platform_service
from djb_node import protocol
from djb_node.tray import app as tray_app

PACKAGE = pathlib.Path(desktop_app.__file__).resolve().parents[1]

#: 允许暂时保留 ``DJB`` 品牌字样的文件（第二类，等 lead 裁决；裁决后删掉对应行即可收紧）
BRAND_PENDING = {
    "__init__.py",            # 模块 docstring（内部文档，不显示给客户）
    "service.py",             # Windows 服务 display_name / description（服务身份）
    "service_plan.py",        # 服务安装计划标题（release-manager 用的纯文本计划）
}


def _brand_hits() -> list[tuple[str, int, str]]:
    """扫出代码里对外露出的 ``DJB 联脑`` / ``DJB / ``（注释与 docstring 不算品牌位）。"""
    hits: list[tuple[str, int, str]] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name in BRAND_PENDING:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("*"):
                continue
            if "DJB 联脑" in line or "DJB / " in line:
                hits.append((str(path.relative_to(PACKAGE)), number, stripped[:90]))
    return hits


def test_no_user_visible_djb_brand_left_in_code():
    """除第二类待裁决文件外，代码里不许再有对外露出的 ``DJB 联脑`` / ``DJB / ``。"""
    assert _brand_hits() == []


def test_identifiers_are_untouched():
    """标识类一个不许动：目录名 / 环境变量 / 服务单元名 / 协议头 / 字体族名 / 日志文件名。"""
    assert platform_paths.APP_DIRNAME == "DJB"                    # %APPDATA%\DJB
    assert platform_paths.DJB_HOME_ENV == "DJB_HOME"
    assert facts.LOCAL_TOKEN_ENV == "DJB_LOCAL_TOKEN"
    assert facts.DAEMON_SINGLE_INSTANCE_ENV == "DJB_DAEMON_SINGLE_INSTANCE"
    assert facts.UPDATE_TEST_ENV == "DJB_ENV"
    assert facts.LOG_FILE == "djb.log"          # 日志文件名/前缀是标识，不是品牌
    assert protocol.PROTOCOL_HEADER == "DJB-Protocol-Version"
    assert protocol.IDEMPOTENCY_HEADER == "X-DJB-Idempotency-Key"
    assert ui_kit.Fonts.NAMES["body"].startswith("DJB.")
    spec = platform_service.default_spec() if hasattr(platform_service, "default_spec") else None
    if spec is not None:                                          # pragma: no cover - 平台相关
        assert platform_service.systemd_unit_name(spec).startswith("djb-node")


def test_window_and_tray_titles_use_tokenbank():
    assert tray_app.DEFAULT_TITLE == "TokenBank 联脑"
    signature = inspect.signature(desktop_app.DesktopApp.__init__)
    assert signature.parameters["title"].default == "TokenBank 联脑 · 桌面控制台"


def test_about_menu_and_notification_app_name_use_tokenbank():
    """macOS「关于 …」菜单与通知 app_name 的默认值走同一处（``platform_ui``）。"""
    source = pathlib.Path(platform_ui.__file__).read_text(encoding="utf-8")
    assert 'actions.get("app_name") or "TokenBank 联脑"' in source
    assert "关于 " + "DJB" not in source


def test_views_copy_and_diagnose_bundle_use_tokenbank():
    assert "TokenBank 下行" in pathlib.Path(views.__file__).read_text(encoding="utf-8")
    assert diagnose.BUNDLE_README.splitlines()[0].startswith("TokenBank 联脑 · 诊断包")


def test_onboarding_and_upstream_copy_use_tokenbank():
    for module in ("onboarding.py", "upstream.py"):
        source = (PACKAGE / module).read_text(encoding="utf-8")
        assert "TokenBank 联脑" in source, module
        assert "DJB 联脑" not in source, module
