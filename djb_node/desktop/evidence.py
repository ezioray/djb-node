# SPDX-License-Identifier: Apache-2.0
"""取证工具：把「界面上到底写了什么」导出成可 diff 的文本。

用法（在 ``repos/djb-node`` 下）::

    python -m djb_node.desktop.evidence --out ../../ops/uta/evidence/wp6

产出
====

* ``views.txt``     12 视图的正常态 + 全不可用态文本快照（谁改文案都会在这留痕）
* ``palette-contrast.txt``  主题对比度现算表（WCAG 比值 + 阈值 + 通过与否）
* ``headless.txt``  无图形环境启动的退出码（必须是 3，且不冒 traceback）
* ``manifest.txt``  生成时间、解释器版本、git 描述（只读，不提交改动）

不写生产数据、不联网、不读用户真实配置（用 ``--demo`` 的确定性数据）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from ..gui import theme
from .datasource import SECTIONS, NullDataSource, Snapshot, Unavailable
from .views import VIEW_KEYS, build_view

__all__ = ["write_all", "main"]


def _now() -> str:
    return _dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def _views_report(snapshots: Sequence[tuple[str, Snapshot]]) -> str:
    lines: list[str] = []
    for label, snapshot in snapshots:
        lines.append("=" * 78)
        lines.append("数据来源：" + label)
        lines.append("=" * 78)
        for key in VIEW_KEYS:
            view = build_view(key, snapshot)
            lines.append("")
            lines.append(view.plain_text())
            lines.append("")
    return "\n".join(lines)


#: 实际出厂的配色对：(用途, 前景, 背景, 阈值, 说明)
SHIPPED_PAIRS: tuple[tuple[str, str, str, float, str], ...] = (
    ("正文 / 白卡", theme.TOKENS["ink"], theme.TOKENS["surface"], 4.5, "TLabel / Card"),
    ("正文 / 页面底", theme.TOKENS["ink"], theme.TOKENS["bg"], 4.5, "App 背景上的文字"),
    ("次级正文", theme.TOKENS["ink2"], theme.TOKENS["surface"], 4.5, "说明、副标题"),
    ("弱化说明", theme.MUTED_TEXT, theme.TOKENS["surface"], 4.5, "Meta.TLabel"),
    ("弱化说明 / 页面底", theme.MUTED_TEXT, theme.TOKENS["bg"], 4.5, "侧栏说明文字"),
    ("主色文字", theme.MINT_TEXT, theme.TOKENS["surface"], 4.5, "Signal / Success"),
    ("等待文字", theme.AMBER_TEXT, theme.TOKENS["surface"], 4.5, "Warning"),
    ("失败文字", theme.RED_TEXT, theme.TOKENS["surface"], 4.5, "Danger"),
    ("信息文字", theme.INFO_TEXT, theme.TOKENS["surface"], 4.5, "Info / 链接"),
    ("强调文字", theme.TOKENS["violet"], theme.TOKENS["surface"], 4.5, "极少用"),
    ("主色按钮底", theme.TOKENS["surface"], theme.TOKENS["mint"], 3.0, "白字压主色（非正文）"),
    ("控件边框", theme.BORDER, theme.TOKENS["surface"], 3.0, "输入框 / 表格边界"),
    ("控件边框 / 输入底", theme.BORDER, theme.TOKENS["surface2"], 3.0, "Entry fieldbackground"),
    ("焦点环", theme.TOKENS["focus"], theme.TOKENS["bg"], 3.0, "键盘焦点可见性"),
    ("卡片分隔线", theme.TOKENS["line"], theme.TOKENS["surface"], 1.2, "装饰性，仅需可见"),
)


def _contrast_report() -> str:
    lines = ["Daylight Signal 对比度（现算 WCAG 比值，不手抄）", "",
             "一、实际出厂配色（阈值按 WCAG 2.1 AA：正文 4.5，控件/图形 3.0）", ""]
    lines.append(f"{'用途':<16}{'前景':<9}{'背景':<9}{'比值':>7}  {'阈值':>5}  {'结论':<5}控件")
    failures = 0
    for purpose, fg, bg, threshold, widget in SHIPPED_PAIRS:
        ratio = theme.contrast_ratio(fg, bg)
        ok = ratio >= threshold
        failures += 0 if ok else 1
        lines.append(f"{purpose:<16}{fg:<9}{bg:<9}{ratio:>7.2f}  {threshold:>5.1f}  "
                     f"{'通过' if ok else '不通过':<5}{widget}")
    lines.append("")
    lines.append("二、状态三重冗余（底色 + 图标 + 文案，颜色不单独表意）")
    for pill, bg_token, fg_value in (("完成 ✓", "mint3", theme.MINT_TEXT),
                                     ("等待 ◐", "amber_bg", theme.AMBER_TEXT),
                                     ("失败 ✕", "red_bg", theme.RED_TEXT),
                                     ("未知 ?", "bg2", theme.MUTED_TEXT)):
        ratio = theme.contrast_ratio(fg_value, theme.TOKENS[bg_token])
        failures += 0 if ratio >= 4.5 else 1
        lines.append(f"  {pill:<8}{theme.TOKENS[bg_token]:<9}+ {fg_value:<9}{ratio:.2f}  "
                     f"{'通过' if ratio >= 4.5 else '不通过'}")
    lines.append("")
    lines.append("三、基准原值 vs 出厂值（原值保留做填充/圆点/徽标；纯文字会用覆盖值）")
    for name, token, override in (("muted", "muted", theme.MUTED_TEXT),
                                  ("mint", "mint", theme.MINT_TEXT),
                                  ("amber", "amber", theme.AMBER_TEXT),
                                  ("cyan", "cyan", theme.INFO_TEXT),
                                  ("red", "red", theme.RED_TEXT)):
        raw = theme.contrast_ratio(theme.TOKENS[token], theme.TOKENS["surface"])
        fixed = theme.contrast_ratio(override, theme.TOKENS["surface"])
        lines.append(f"  {name:<7}基准 {theme.TOKENS[token]} 白卡 {raw:.2f} → "
                     f"文字用 {override} 白卡 {fixed:.2f}")
    lines.append("")
    lines.append("汇总：" + ("全部达到阈值" if failures == 0 else f"{failures} 项未达阈值"))
    return "\n".join(lines)


def _headless_report() -> str:
    command = [sys.executable, "-m", "djb_node.desktop", "--demo"]
    env = dict(os.environ)
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    env["DJB_HOME"] = "/tmp/djb-evidence-home"
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=180,
                              env=env, cwd=os.getcwd())
        code, out, err = done.returncode, done.stdout, done.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        code, out, err = -1, "", type(exc).__name__
    lines = [
        "无图形环境启动（模拟：DISPLAY 未设置）",
        "命令：" + " ".join(command),
        "解释器：" + sys.executable,
        "退出码：" + str(code) + "（约定 3 = 无图形环境，见 gui.window.EXIT_HEADLESS）",
        "stdout：" + (out.strip() or "（空）"),
        "stderr：" + (err.strip() or "（空）"),
        "结论：" + ("通过：返回 3 且无 traceback" if code == 3 and "Traceback" not in err
                  else "不通过"),
    ]
    return "\n".join(lines)


def _manifest(out: Path, paths: Sequence[Path]) -> str:
    lines = ["WP6 桌面客户端取证清单", "",
             "生成时间：" + _now(),
             "Python：" + sys.version.split()[0] + " @ " + sys.executable,
             "工作目录：" + os.getcwd(),
             "数据来源：--demo 确定性演示数据（不读真实 ~/.djb，不联网）",
             "",
             "产出："]
    for path in paths:
        size = path.stat().st_size if path.exists() else 0
        lines.append(f"  {path.name:<24}{size:>8} bytes")
    shots = sorted(p.name for p in out.glob("*.png"))
    if shots:
        lines.append("")
        lines.append("真实数据截图（Xvfb + import，共 " + str(len(shots)) + " 张）："
                     + "  ".join(shots))
    demo = sorted(p.name for p in (out / "demo").glob("*.png"))
    if demo:
        lines.append("")
        lines.append("演示数据截图（--demo，界面带「演示数据（--demo）」角标，共 "
                     + str(len(demo)) + " 张）：" + "  ".join(demo))
    return "\n".join(lines)


def write_all(out_dir: str | os.PathLike) -> dict[str, Path]:
    from .app import demo_snapshot

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    outage = Snapshot({name: Unavailable("演示：该数据域本次不可用") for name in SECTIONS},
                      source="outage")
    views_text = _views_report((("--demo 演示数据", demo_snapshot()),
                                ("全数据域不可用（故障演练）", outage),
                                ("NullDataSource", NullDataSource("未接线").snapshot())))
    files = {"views.txt": views_text,
             "palette-contrast.txt": _contrast_report(),
             "headless.txt": _headless_report()}
    written: dict[str, Path] = {}
    for name, text in files.items():
        path = out / name
        path.write_text(text.rstrip() + "\n", encoding="utf-8")
        written[name] = path
    manifest = out / "manifest.txt"
    manifest.write_text(_manifest(out, list(written.values())) + "\n", encoding="utf-8")
    written["manifest.txt"] = manifest
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="djb desktop.evidence",
                                     description="导出桌面客户端取证文本")
    parser.add_argument("--out", required=True, help="输出目录")
    args = parser.parse_args(list(argv) if argv is not None else None)
    written = write_all(args.out)
    for name, path in written.items():
        print(name, "->", path)
    return 0


if __name__ == "__main__":                              # pragma: no cover
    sys.exit(main())
