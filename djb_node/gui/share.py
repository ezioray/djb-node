# SPDX-License-Identifier: Apache-2.0
"""S3 共享偏好模型：预设优先、参数在后（产品 §5.7.1）。

纯函数，无 tkinter / 网络 / store 依赖；金额预估由调用方下发，本层只展示。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

PRESETS: dict[str, tuple[str, str]] = {
    "saver": ("省心模式", "只在我不用电脑时工作，几乎感觉不到"),
    "balanced": ("均衡模式", "工作时段实时算力，其余时段慢任务"),
    "full": ("全力以赴", "全天共享，含参与超大模型组网"),
}


@dataclass(frozen=True)
class ShareSettings:
    """一份待保存的共享偏好；默认不开启组网（独立开关，默认关）。"""

    preset: str = "balanced"
    realtime: bool = True
    batch: bool = True
    network: bool = False
    price_mode: str = "auto"

    def validate(self) -> None:
        if self.preset not in PRESETS:
            raise ValueError("preset 必须是 " + " / ".join(PRESETS))
        if not self.realtime and not self.batch:
            raise ValueError("至少开启实时算力或慢任务之一")
        if self.price_mode not in ("auto", "manual"):
            raise ValueError("price_mode 必须是 auto / manual")


@dataclass(frozen=True)
class ShareOption:
    key: str
    label: str
    summary: str
    selected: bool


@dataclass(frozen=True)
class ShareView:
    title: str
    options: tuple[ShareOption, ...]
    realtime_label: str
    batch_label: str
    network_label: str
    network_badge: str
    network_ok: bool
    warnings: tuple[str, ...]
    estimates: tuple[str, ...]
    save_enabled: bool
    advanced: bool


def validate_settings(settings: ShareSettings) -> None:
    settings.validate()


def _yes_no(value: bool) -> str:
    return "开启" if value else "关闭"


def build_share_view(settings: ShareSettings, *,
                     network_ok: bool, upload_mbps: float,
                     min_upload_mbps: float = 50.0,
                     estimates: Mapping[str, str] | None = None,
                     advanced: bool = False) -> ShareView:
    """把设置 + 网络事实投影成可渲染视图；缺预估显示「—」，不编造金额。

    允许传入「尚未通过校验」的设置（例如用户把两档都关掉），由窗口层补警告并禁用保存；
    这里只做类型/数值边界检查，避免 GUI 在编辑过程中崩掉。
    """
    if upload_mbps < 0:
        raise ValueError("upload_mbps 必须 >= 0")
    options = tuple(
        ShareOption(key=key, label=label, summary=summary,
                    selected=(key == settings.preset))
        for key, (label, summary) in PRESETS.items()
    )
    warnings: list[str] = []
    if settings.network and not network_ok:
        warnings.append(
            f"组网参与未达标：上行 {upload_mbps:g} Mbps（需 ≥{min_upload_mbps:g}）"
        )
    estimates = dict(estimates or {})
    lines: list[str] = []
    for key, label in (("realtime", "只开实时"), ("batch", "只开慢任务"),
                       ("both", "都开")):
        lines.append(f"{label} {estimates.get(key, '—')}")
    return ShareView(
        title="你想怎么共享？",
        options=options,
        realtime_label=f"实时算力 · {_yes_no(settings.realtime)}",
        batch_label=f"慢任务 · {_yes_no(settings.batch)}",
        network_label=f"组网参与 · {_yes_no(settings.network)}",
        network_badge="✅ 达标" if network_ok else "❌ 未达标",
        network_ok=network_ok,
        warnings=tuple(warnings),
        estimates=tuple(lines),
        save_enabled=settings.realtime or settings.batch,
        advanced=advanced,
    )
