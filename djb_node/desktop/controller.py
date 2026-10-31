# SPDX-License-Identifier: Apache-2.0
"""窗口逻辑（**不含 Tk**）：视图切换、刷新、动作路由、草稿编辑、保存回读。

Tk 层（:mod:`djb_node.desktop.app`）只做三件事：把 :class:`View` 画出来、
把点击交回 :meth:`DesktopController.action`、把结果消息放进状态栏。因此无图形环境
也能跑全部逻辑测试（注入 :class:`~djb_node.desktop.datasource.FakeDataSource`）。

动作口径
========

* 能真做且能回读的才置为可用：刷新 / 跑分 / 复制 / 打开日志目录 / 保存接入配置 /
  暂停·恢复（走既有 ``tray.actions.apply_control`` 的确认 + 回读三重校验）。
* 云端还没有的接口（模型意向上报、收据校验、出款导出、2FA、登出）**不假装成功**：
  返回「暂不可用（原因）」并说明改动未上报。
* 保存类动作先校验、再落盘、再回读，最后询问 daemon 是否重载；重载不支持就如实说
  「重启 daemon 后生效」。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Protocol, Sequence

from .access import (RELOAD_DAEMON_DOWN, RELOAD_FAILED, RELOAD_OK, RELOAD_UNSUPPORTED,
                     AccessConfig, AccessService, FileAccessStore, SaveResult, mask_secret)
from .datasource import (SECTIONS, DataSource, LocalDataSource, ProviderError, Snapshot,
                         Unavailable, benchmark)
from .views import VIEW_KEYS, VIEW_TITLES, View, build_view

__all__ = ["ActionResult", "ControlOutcome", "DesktopController", "LocalControlChannel",
           "SUB_CONTROL", "SUB_RECEIPTS", "SUB_TASKS"]

#: 详情抽屉的数据域（点表格行时展示）
SUB_TASKS = "tasks"
SUB_RECEIPTS = "receipts"
SUB_CONTROL = "control"

DEFAULT_MESSAGE = "就绪：F5 刷新 · Ctrl+1…9 切换视图 · 关闭窗口不会停止 daemon"


@dataclass(frozen=True)
class ControlOutcome:
    """暂停/恢复的结果：``verified`` 只能是「回读确认过」的状态。"""

    verified: bool
    message: str
    state: str = "unknown"


@dataclass(frozen=True)
class ActionResult:
    """动作结果：``effect`` 让 Tk 层知道要不要复制文本 / 打开目录 / 切视图。"""

    message: str
    ok: bool = True
    effect: str = ""
    payload: str = ""


class ControlChannel(Protocol):
    """暂停/恢复通道（默认走 daemon 控制套接字；测试注入替身）。"""

    def control(self, action: str, confirm: Callable[[str], bool]) -> ControlOutcome:
        ...


class LocalControlChannel:
    """真实现：复用 :func:`djb_node.tray.actions.apply_control`（确认 + 前置检查 + 回读）。"""

    def __init__(self, home: str | os.PathLike | None = None, *,
                 config: Any | None = None) -> None:
        self._home = home
        self._config = config

    def control(self, action: str, confirm: Callable[[str], bool]) -> ControlOutcome:
        from ..daemon import ControlClient
        from ..config import Paths, load_config
        from ..tray.actions import apply_control, control_enabled
        from ..tray.model import TrayAction
        paths = Paths(str(self._home)) if self._home else Paths(os.path.expanduser("~/.djb"))
        config = self._config if self._config is not None else load_config(paths)
        client = ControlClient(paths)
        tray_action = TrayAction.PAUSE if action == "pause" else TrayAction.RESUME
        result = apply_control(tray_action,
                               call=lambda payload, timeout=5.0: client.call(payload,
                                                                             timeout=timeout),
                               enabled=lambda: control_enabled(config),
                               confirm=confirm)
        return ControlOutcome(result.verified, result.message, result.state)


@dataclass
class DesktopController:
    """12 视图的状态机：唯一入口 ``show`` / ``action`` / ``refresh``。"""

    source: DataSource
    access: AccessService | None = None
    control: ControlChannel | None = None
    bus: Any | None = None
    benchmark_runner: Callable[..., Mapping[str, Any]] = benchmark
    log_level: str = "all"
    message: str = DEFAULT_MESSAGE
    view: str = "overview"
    drafts: dict[str, str] = field(default_factory=dict)
    benchmark: Mapping[str, Any] | None = None
    _snapshot: Snapshot | None = None

    # ── 快照 / 视图
    @property
    def snapshot(self) -> Snapshot:
        if self._snapshot is None:
            self._snapshot = self.source.snapshot()
        return self._snapshot

    @property
    def views(self) -> dict[str, View]:
        return {key: build_view(key, self._snapshot_for_views()) for key in VIEW_KEYS}

    def _snapshot_for_views(self) -> Snapshot:
        """在快照上叠加本层产生的状态（跑分结果 / 日志级别筛选）。"""
        base = self.snapshot
        sections = dict(base.sections)
        if self.benchmark is not None:
            bench = sections.get("bench")
            merged = dict(bench) if isinstance(bench, Mapping) else {}
            merged["benchmark"] = dict(self.benchmark)
            sections["bench"] = merged
        if self.log_level and self.log_level != "all":
            logs = sections.get("logs")
            if isinstance(logs, Mapping):
                rows = [row for row in (logs.get("rows") or [])
                        if isinstance(row, Mapping)
                        and str(row.get("level")) == self.log_level]
                sections["logs"] = {**logs, "rows": rows}
        return Snapshot(sections=sections, fetched_at=base.fetched_at, source=base.source,
                        notes=base.notes)

    def current(self) -> View:
        return build_view(self.view, self._snapshot_for_views())

    def show(self, key: str) -> View:
        if key not in VIEW_KEYS:
            raise ValueError("未知视图：" + str(key))
        self.view = key
        return self.current()

    def next_view(self, step: int = 1) -> View:
        index = (VIEW_KEYS.index(self.view) + int(step)) % len(VIEW_KEYS)
        return self.show(VIEW_KEYS[index])

    def refresh(self, sections: Sequence[str] | None = None) -> View:
        """重新取数；单个数据域失败会在视图里显示「暂不可用」，不影响其他页。"""
        try:
            self._snapshot = self.source.snapshot(sections=sections)
        except Exception as exc:                       # 数据层理论上不抛；兜底不让窗口空白
            names = tuple(sections) if sections else SECTIONS
            self._snapshot = Snapshot(
                sections={name: Unavailable("刷新失败（" + type(exc).__name__ + "）")
                          for name in names}, source="error")
            self.message = "刷新失败：界面保留上一次结果，详情见日志。"
        else:
            self.message = "已刷新：" + (self._snapshot.fetched_at or "刚刚")
        self._publish()
        return self.current()

    def _publish(self) -> None:
        """把 status 投影广播给托盘（谁回读到新状态谁广播）。"""
        bus = self.bus
        if bus is None:
            return
        status = self.snapshot.get("status")
        if isinstance(status, Mapping):
            yield_state = self.snapshot.get("yield")
            mode = str(yield_state.get("mode") or "") if isinstance(yield_state, Mapping) else ""
            try:
                bus.publish_status(status, mode=mode, source="window")
            except Exception:
                pass

    # ── 草稿编辑（接入配置 / Agent 上行地址）
    def draft_value(self, key: str) -> str:
        return self.drafts.get(key, "")

    def set_draft(self, key: str, value: str) -> None:
        self.drafts[str(key)] = str(value)

    def _config_for_save(self) -> tuple[AccessConfig, list[str]]:
        """把草稿合并进当前配置；秘密字段空值表示「保持不变」。"""
        service = self._service()
        current = service.load()
        warnings: list[str] = []
        changes: dict[str, str] = {}
        for key, value in self.drafts.items():
            text = str(value or "")
            if key in ("node_secret", "api_key", "scoped_token") and not text.strip():
                continue                               # 空 = 不覆盖已有秘密
            changes[key] = text.strip() if key != "price_note" else text
        known = {name for name in AccessConfig().__dict__ if name != "extra"}
        unknown = [key for key in changes if key not in known]
        if unknown:
            warnings.append("草稿字段不被支持：" + "、".join(unknown))
        accepted = {key: value for key, value in changes.items() if key in known}
        try:
            updated = replace(current, **accepted)
        except (TypeError, ValueError) as exc:
            warnings.append("草稿合并失败：" + type(exc).__name__)
            updated = current
        return updated, warnings

    def _service(self) -> AccessService:
        if self.access is None:
            self.access = AccessService(FileAccessStore())
        return self.access

    def save_access(self) -> ActionResult:
        config, warnings = self._config_for_save()
        result: SaveResult = self._service().save(config)
        if result.saved and result.readback_ok:
            self.drafts.clear()
            self.refresh(sections=("access", "agent"))
        prefix = ("；".join(warnings) + "。") if warnings else ""
        return ActionResult(prefix + result.message, ok=result.effective,
                            effect="access", payload=result.reload)

    def save_agent(self) -> ActionResult:
        """Agent 上行地址编辑后保存（同一份本机配置，走同一保存+回读路径）。"""
        return self.save_access()

    def probe_reload(self) -> ActionResult:
        """只请求重载、不改配置；用于「保存后 daemon 没重载」的补救。"""
        service = self._service()
        state = service.reload()
        from .access import RELOAD_TEXT
        return ActionResult(RELOAD_TEXT.get(state, RELOAD_TEXT[RELOAD_FAILED]),
                            ok=state == RELOAD_OK, effect="reload", payload=state)

    # ── 动作路由
    def action(self, key: str, *, confirm: Callable[[str], bool] | None = None) -> ActionResult:
        handler = getattr(self, "_act_" + str(key).replace(".", "_"), None)
        if handler is None:
            result = ActionResult("暂不可用（未接线动作：" + str(key) + "）", ok=False)
        else:
            try:
                result = handler(confirm=confirm or (lambda _m: False))
            except ProviderError as exc:
                result = ActionResult("暂不可用（" + exc.reason + "）", ok=False)
            except Exception as exc:                   # 动作失败不许冒栈到 UI
                result = ActionResult("操作失败（" + type(exc).__name__ + "）；未生效。", ok=False)
        self.message = result.message
        return result

    # 真做的动作
    def _act_refresh(self, *, confirm=None) -> ActionResult:
        self.refresh()
        return ActionResult(self.message, effect="refresh")

    def _act_bench_run(self, *, confirm=None) -> ActionResult:
        try:
            result = dict(self.benchmark_runner(budget_s=10.0))
        except Exception as exc:
            return ActionResult("跑分失败（" + type(exc).__name__ + "）；本机计算未完成。", ok=False)
        self.benchmark = result
        return ActionResult("跑分完成：sha256 " + str(result.get("hash_mbps")) + " MB/s，用时 "
                            + str(result.get("elapsed_s")) + " s。", effect="bench")

    def _act_bench_copy(self, *, confirm=None) -> ActionResult:
        lines = [("sha256", "hash_mbps"), ("float", "float_mops"), ("int", "int_mops")]
        data = self.benchmark or {}
        if not data:
            return ActionResult("暂不可用（还没有跑分结果，先点「开始跑分」）", ok=False)
        text = "  ".join(name + "=" + str(data.get(key)) for name, key in lines)
        return ActionResult("已复制跑分结果。", effect="copy", payload=text)

    def _act_gpu_refresh(self, *, confirm=None) -> ActionResult:
        self.refresh(sections=("gpu",))
        return ActionResult("已刷新 GPU 信息。", effect="refresh")

    def _act_tasks_refresh(self, *, confirm=None) -> ActionResult:
        self.refresh(sections=("tasks",))
        return ActionResult("已刷新任务队列。", effect="refresh")

    def _act_logs_copy(self, *, confirm=None) -> ActionResult:
        view = self.current()
        if view.table is None or not view.table.rows:
            return ActionResult("暂不可用（当前筛选下没有日志行）", ok=False)
        text = "\n".join(" | ".join(row.cells) for row in view.table.rows)
        return ActionResult("已复制 " + str(len(view.table.rows)) + " 行日志。",
                            effect="copy", payload=text)

    def _act_logs_open_dir(self, *, confirm=None) -> ActionResult:
        logs = self.snapshot.get("logs")
        path = str(logs.get("path") if isinstance(logs, Mapping) else "")
        if not path:
            return ActionResult("暂不可用（日志路径未知）", ok=False)
        return ActionResult("日志目录：" + os.path.dirname(path), effect="open_dir",
                            payload=os.path.dirname(path))

    def _act_access_save(self, *, confirm=None) -> ActionResult:
        return self.save_access()

    def _act_agent_save(self, *, confirm=None) -> ActionResult:
        return self.save_agent()

    def _act_access_reload(self, *, confirm=None) -> ActionResult:
        return self.probe_reload()

    def _act_yield_pause(self, *, confirm=None) -> ActionResult:
        return self._control("pause", confirm)

    def _act_yield_resume(self, *, confirm=None) -> ActionResult:
        return self._control("resume", confirm)

    def _control(self, action: str, confirm: Callable[[str], bool]) -> ActionResult:
        channel = self.control
        if channel is None:
            return ActionResult("暂不可用（控制通道未接通：需要 daemon 运行）", ok=False)
        outcome = channel.control(action, confirm)
        self.refresh(sections=("status", "yield"))
        return ActionResult(outcome.message, ok=outcome.verified, effect="control",
                            payload=outcome.state)

    # 未接线动作：如实说，不假装成功
    def _act_models_save(self, *, confirm=None) -> ActionResult:
        return ActionResult("暂不可用（云端意向上报接口未接线）；改动未上报，未生效。", ok=False)

    def _act_receipts_verify(self, *, confirm=None) -> ActionResult:
        return ActionResult("暂不可用（收据校验接口未接线）；本地未做任何验证。", ok=False)

    def _act_earnings_export(self, *, confirm=None) -> ActionResult:
        return ActionResult("暂不可用（出款明细导出未接线）；未生成任何文件。", ok=False)

    def _act_account_enable_2fa(self, *, confirm=None) -> ActionResult:
        return ActionResult("暂不可用（2FA 入口在服务端控制台）；本机未做任何改动。", ok=False)

    def _act_account_logout(self, *, confirm=None) -> ActionResult:
        return ActionResult("暂不可用（登出接口未接线）；本机凭据未改动。", ok=False)

    # ── 筛选
    def set_log_level(self, level: str) -> View:
        self.log_level = str(level or "all")
        return self.current()

    def status_line(self) -> str:
        """状态栏文案：daemon 状态 + 让路 + 最近一次消息。"""
        status = self.snapshot.get("status")
        if isinstance(status, Unavailable):
            state_text = status.text
        elif isinstance(status, Mapping):
            from ..gui.state import STATE_LABELS
            state_text = STATE_LABELS.get(str(status.get("state")), "状态未知")
        else:
            state_text = "暂不可用"
        return "daemon：" + state_text + "    ·    " + self.message


def secret_hint(value: str) -> str:
    """给 UI 的占位提示（永不回显原值）。"""
    return mask_secret(value) if value else "未设置"
