# SPDX-License-Identifier: Apache-2.0
"""Safe, read-only desktop projections. Never display raw JSON or credentials."""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..gui.theme import COLORS, MONO, apply_theme
from .notifications import TEMPLATE_TITLES

PAGES = ("运行", "护栏", "上游", "收益", "通知", "设置")


def safe_value(value: Any) -> str:
    if value is None or isinstance(value, (bool, dict, list, tuple)):
        return "— 暂不可用"
    # Only scalar numeric values for money/metrics, no incidental credentials.
    text = str(value).strip()
    if not text or len(text) > 48 or any(c not in "0123456789.,-+ $USDusd%" for c in text):
        return "— 暂不可用"
    return text


def project_pages(status: Mapping[str, Any] | None,
                  notifications: Sequence[Mapping[str, Any]] | None, *,
                  status_known: bool, notifications_known: bool) -> dict[str, str]:
    data = status or {}
    states = {"online": "● 运行中", "starting": "◐ 启动中", "paused": "Ⅱ 已暂停",
              "updating": "◐ 更新中", "stopped": "■ 已停止", "error": "! 错误"}
    state = states.get(data.get("state"), "? 状态未知") if status_known else "? 状态读取失败"
    accepting = ("是" if data.get("accepting") is True else
                 "否" if data.get("accepting") is False else "暂不可用") if status_known else "暂不可用"
    reasons = {"user_paused": "用户暂停", "yield_paused": "资源让路",
               "quota_exhausted": "配额用尽", "policy_expired": "策略过期",
               "credential_invalid": "凭据失效（不展示凭据）"}
    reason = reasons.get(data.get("reason"), "— 暂不可用")
    policy = data.get("policy") if isinstance(data.get("policy"), Mapping) else {}
    usable = ("可用" if policy.get("usable") is True else "不可用 / 保守模式"
              if policy.get("usable") is False else "— 暂不可用")
    pages = {
        "运行": f"01 / LOCAL RUNTIME\n\n{state}\n接收新任务：{accepting}\n原因 / 让路：{reason}\n\nCPU / GPU / 带宽 / 当前任务：— 暂不可用\n\n下一步：刷新回读；暂停/恢复入口在托盘菜单。\n未知状态不代表服务已停止。",
        "护栏": f"02 / GUARDRAILS\n\n最终有效规则 = 本地规则与平台策略中更严格者\n策略可用性：{usable}\n让路原因：{reason}\n\n资源 / 日周月配额 / 时段：— 暂不可用\n\n共享设置仅保存草稿，不修改运行规则。\n无完整 apply/reload 接口，不提供虚假应用按钮。",
        "上游": "03 / UPSTREAM BOUNDARY\n\n实时上游健康 / 模型：— 暂不可用\n\nA1：仅本机系统安全存储\nA2：仅本机 agent 持有\nB：统一端点自行保管\n\n此面板不读取、显示、复制或上传凭据。\n下一步：使用既有上游 CLI；本页不伪造接入向导。",
        "收益": "04 / SERVER VALUES ONLY\n\n余额：" + safe_value(data.get("balance") if status_known else None)
                  + "\n今日收益：" + safe_value(data.get("today_earnings") if status_known else None)
                  + "\n\n待结算 / 已结算 / 冻结 / COP：— 暂不可用\n\n无数据不代表零；金额仅原样展示，不换算、不推算。",
        "设置": "06 / LOCAL SETTINGS\n\n共享偏好：djb gui（只保存草稿，尚未生效）\n网络参与：只记录草稿意向，不开启组网\n\n暂停/恢复默认仅命令指南；受控动作需已登记开关、\n未到期且每次用户明确确认。未知状态拒绝恢复。\n\n通知无声音、无自动浏览器跳转。\n退出窗口/托盘不停止 daemon。",
    }
    if not notifications_known:
        pages["通知"] = "05 / NOTIFICATIONS\n\n! 消息可能不是最新；读取失败，不把未知显示为零。\n下一步：刷新或查看命令指南 djb notify list --json。"
    else:
        rows = [row for row in (notifications or ()) if isinstance(row, Mapping)]
        rows.sort(key=lambda row: (str(row.get("severity")) != "P0", bool(row.get("read_at"))))
        lines = []
        for row in rows[:200]:
            severity = str(row.get("severity") or "未知级别").upper()
            if severity not in ("P0", "P1", "P2"):
                severity = "未知级别"
            # Known local title table only: raw title/body/payload/url never render.
            title = TEMPLATE_TITLES.get(str(row.get("template_key")), "通知（标题未映射，请使用命令指南查看）")
            marker = "! 重要" if severity == "P0" else "·"
            unread = "已读" if row.get("read_at") else "未读"
            lines.append(f"{marker} [{severity} / {unread}] {title}")
        pages["通知"] = ("05 / NOTIFICATIONS · SILENT\n\n" + ("\n\n".join(lines) if lines else
                         "暂无本地消息。\n有新的分润或告警会出现在这里。")
                         + "\n\n本地列表最多 200 条；不代表服务端已同步。\nP0 视觉置顶、保留角标；不播放声音、不自动打开浏览器。\n下一步：查看命令指南 djb notify list --json；本页不标记已读。")
    return pages


class DesktopPanel:
    """One reusable notebook, not a new window on each poll."""
    def __init__(self, root, tk, *, refresh):
        from tkinter import ttk
        self._window = tk.Toplevel(root)
        self._window.title("DJB / NIGHT SIGNAL · 本地只读控制台")
        self._window.geometry("900x720")
        self._window.minsize(640, 480)
        apply_theme(self._window, ttk)
        self._window.protocol("WM_DELETE_WINDOW", self._window.withdraw)
        self._window.bind("<Escape>", lambda _e: self._window.withdraw())
        header = ttk.Frame(self._window, padding=16)
        header.pack(fill="x")
        ttk.Label(header, text="◢ DJB / NIGHT SIGNAL   // READBACK", style="Signal.TLabel").pack(side="left")
        ttk.Button(header, text="刷新 / F5", command=refresh).pack(side="right")
        self._window.bind("<F5>", lambda _e: refresh())
        self._notebook = ttk.Notebook(self._window)
        self._notebook.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self._texts = {}
        for name in PAGES:
            frame = ttk.Frame(self._notebook, padding=12)
            self._notebook.add(frame, text=name)
            text = tk.Text(frame, background=COLORS["panel"], foreground=COLORS["text"],
                           insertbackground=COLORS["cyan"], selectbackground=COLORS["input"],
                           selectforeground=COLORS["cyan"], font=MONO, wrap="word",
                           highlightthickness=2, highlightbackground=COLORS["border"],
                           highlightcolor=COLORS["cyan"], relief="flat", padx=16, pady=16,
                           state="disabled", takefocus=True)
            scrollbar = ttk.Scrollbar(frame, command=text.yview)
            text.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side="right", fill="y")
            text.pack(fill="both", expand=True)
            self._texts[name] = text
        self._window.withdraw()

    def render(self, pages):
        for name, content in pages.items():
            text = self._texts[name]
            position = text.yview()
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", content)
            text.configure(state="disabled")
            if position:
                text.yview_moveto(position[0])

    def show(self, page="运行"):
        self._notebook.select(PAGES.index(page))
        self._window.deiconify()
        self._window.lift()
        self._texts[page].focus_set()
