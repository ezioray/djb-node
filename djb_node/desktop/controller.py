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
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .access import (RELOAD_DAEMON_DOWN, RELOAD_FAILED, RELOAD_OK, RELOAD_UNSUPPORTED,
                     AccessConfig, AccessService, FileAccessStore, SaveResult, mask_secret)
from .datasource import (SECTIONS, DataSource, LocalDataSource, ProviderError, Snapshot,
                         Unavailable, benchmark)
from .views import VIEW_KEYS, VIEW_TITLES, View, build_view
from .. import facts

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
    """13 视图的状态机：唯一入口 ``show`` / ``action`` / ``refresh``。"""

    source: DataSource
    access: AccessService | None = None
    control: ControlChannel | None = None
    bus: Any | None = None
    benchmark_runner: Callable[..., Mapping[str, Any]] = benchmark
    cli: Callable[[Sequence[str]], Any] | None = None
    #: 一次性绑定码输入通道（GUI 注入 simpledialog；无输入通道时动作返回「暂不可用」）
    ask_code: Callable[[], str | None] | None = None
    save_path: Callable[[str], str | None] | None = None
    selected: dict[str, str] = field(default_factory=dict)
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
        """重新取数；单个数据域失败会在视图里显示「暂不可用」，不影响其他页。

        只刷部分域（``sections=("gpu",)``）时**必须合并**进旧快照：数据源只返回
        被点名的域，直接替换会把其它 12 页一起变成「暂不可用」（点一次「刷新 GPU」
        后日志/收据全空，直到整页刷新）。
        """
        names = tuple(sections) if sections else SECTIONS
        try:
            fresh = self.source.snapshot(sections=sections)
        except Exception as exc:                       # 数据层理论上不抛；兜底不让窗口空白
            merged = dict(self._snapshot.sections) if self._snapshot is not None else {}
            merged.update({name: Unavailable("刷新失败（" + type(exc).__name__ + "）")
                           for name in names})
            self._snapshot = Snapshot(sections=merged, source="error")
            self.message = "刷新失败：界面保留上一次结果，详情见日志。"
        else:
            if sections and self._snapshot is not None:
                merged = dict(self._snapshot.sections)
                merged.update(fresh.sections)
                self._snapshot = Snapshot(sections=merged,
                                          fetched_at=fresh.fetched_at or self._snapshot.fetched_at,
                                          source=fresh.source, notes=fresh.notes)
            else:
                self._snapshot = fresh
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
        message = RELOAD_TEXT.get(state, RELOAD_TEXT[RELOAD_FAILED])
        detail = getattr(service, "last_reload_detail", "")
        if detail:
            message += "（" + detail + "）"
        return ActionResult(message, ok=state == RELOAD_OK, effect="reload", payload=state)

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

    def _act_access_copy_node_id(self, *, confirm=None) -> ActionResult:
        """复制节点 ID（网页端绑定要填的那个）：只读快照，不写真机、不发请求。

        节点 ID 由服务端在注册时下发，落在本机凭据 / 状态库里（``datasource._resolve_node_id``
        已做三级回退）；这里只把它交给剪贴板，取不到就说清下一步。
        """
        access = self.snapshot.get("access")
        node_id = str(access.get("node_id") or "") if isinstance(access, Mapping) else ""
        if not node_id:
            return ActionResult("暂不可用（还没有节点 ID：先启动一次客户端，注册后自动生成）",
                                ok=False)
        return ActionResult("已复制节点 ID：" + node_id, effect="copy", payload=node_id)

    def _act_agent_save(self, *, confirm=None) -> ActionResult:
        return self.save_agent()

    def _act_access_reload(self, *, confirm=None) -> ActionResult:
        return self.probe_reload()

    # 共享 Token Plan 页：主操作只是「换视图 + 复用接入配置页的保存/回读」，
    # 不发新请求、不写真机配置 —— 真正的写入仍走 access.save 的那条老路。
    def _act_token_plan_open_access(self, *, confirm=None) -> ActionResult:
        return ActionResult("已切到「接入配置」：在那里添加上游凭据、或完成节点绑定。",
                            effect="goto", payload="access")

    def _act_token_plan_refresh(self, *, confirm=None) -> ActionResult:
        self.refresh(sections=("status", "token_plan", "receipts"))
        return ActionResult(self.message, effect="refresh")

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

    def _act_account_logout(self, *, confirm=None) -> ActionResult:
        """登出 = 解绑节点（服务端吊销 + 本机清凭据）。必须二次确认，失败不动凭据。"""
        account = self.snapshot.get("account")
        security = account.get("security") if isinstance(account, Mapping) else None
        node_id = str((security or {}).get("node_id") or "") if isinstance(security, Mapping) else ""
        if not node_id:
            return ActionResult("尚未登录节点，无需登出。", ok=False)
        if not confirm("登出会解绑节点并清除本机密钥，daemon 需要重新配对才能接单。确认登出？"):
            return ActionResult("已取消登出（本机凭据未改动）。", ok=False, effect="cancelled")
        if self.cli is None:
            return ActionResult("暂不可用（本机命令通道未接通）；本机凭据未改动。", ok=False)
        try:
            payload = self.cli(["auth", "--unbind", "--yes"])
        except ProviderError as exc:
            return ActionResult("登出未成功：" + exc.reason + "；本机凭据未改动。", ok=False)
        except Exception as exc:
            return ActionResult("登出未成功（" + type(exc).__name__ + "）；本机凭据未改动。",
                                ok=False)
        if isinstance(payload, Mapping) and payload.get("ok") is False:
            return ActionResult("登出未成功：" + str(payload.get("error") or "服务端拒绝")
                                + "；本机凭据未改动。", ok=False)
        self.refresh()
        return ActionResult("已登出：节点已解绑、本机密钥已清除；重新配对后才能接单。",
                            effect="refresh")

    def _act_account_bind(self, code: str | None = None, *, confirm=None) -> ActionResult:
        """绑定网页端：取一次性码 → ``djb auth --bind <码>`` → 刷新快照。

        ``code`` 可显式传入（测试 / 调用方已有码）；为 ``None`` 时走 ``ask_code`` 输入框。
        绑定码只进子进程参数，不进日志、不回显到状态栏。
        """
        if code is None:
            if self.ask_code is None:
                return ActionResult("暂不可用（没有绑定码输入通道）", ok=False)
            code = self.ask_code()
        code = str(code or "").strip()
        if not code:
            return ActionResult("已取消绑定（没有输入绑定码）。", ok=False, effect="cancelled")
        if self.cli is None:
            return ActionResult("暂不可用（本机命令通道未接通）；绑定未执行。", ok=False)
        try:
            payload = self.cli(["auth", "--bind", code])
        except ProviderError as exc:
            return ActionResult("绑定未成功：" + exc.reason + "；本机凭据未改动。", ok=False)
        except Exception as exc:
            return ActionResult("绑定未成功（" + type(exc).__name__ + "）；本机凭据未改动。",
                                ok=False)
        if isinstance(payload, Mapping) and payload.get("ok") is False:
            return ActionResult("绑定未成功：" + str(payload.get("error") or "服务端拒绝")
                                + "；本机凭据未改动。", ok=False)
        self.refresh()
        return ActionResult("已绑定网页端；daemon 会用新凭据重新上线。", effect="refresh")

    def _act_account_open_web(self, *, confirm=None) -> ActionResult:
        """在系统浏览器打开网页端（只给地址与动作，不在 controller 里碰 GUI）。"""
        return ActionResult("网页端：" + facts.WEB_CONSOLE_URL, effect="open_url",
                            payload=facts.WEB_CONSOLE_URL)

    def select_row(self, view_key: str, key: str) -> None:
        """记住当前视图选中的行（第一列文本）；动作据此作用于「这一条」。"""
        if key:
            self.selected[str(view_key)] = str(key)
        else:
            self.selected.pop(str(view_key), None)

    # ── 收据校验：拉云端单条 + 与本机密封缓存逐字段比对
    def _act_receipts_verify(self, *, confirm=None) -> ActionResult:
        receipts = self.snapshot.get("receipts")
        rows = receipts.get("rows") if isinstance(receipts, Mapping) else None
        rows = [row for row in (rows or []) if isinstance(row, Mapping)]
        if not rows:
            return ActionResult("暂不可用（列表里没有收据可校验）", ok=False)
        ident = str(self.selected.get("receipts") or "")
        if not ident:
            return ActionResult("先在列表里选中一条收据，再点「校验收据」。", ok=False)
        local_row = next((row for row in rows
                          if str(row.get("receipt_id") or row.get("id") or "") == ident), None)
        if local_row is None:
            return ActionResult("选中的收据不在当前页（刷新后再试）。", ok=False)
        fetch_local = getattr(self.source, "local_receipt", None)
        fetch_cloud = getattr(self.source, "cloud_receipt", None)
        if fetch_local is None or fetch_cloud is None:
            return ActionResult("暂不可用（数据源未提供校验收据能力）", ok=False)
        try:
            local = fetch_local(ident)
        except ProviderError as exc:
            return ActionResult("暂不可用（" + exc.reason + "）", ok=False)
        try:
            cloud = fetch_cloud(ident)
        except ProviderError as exc:
            return ActionResult("暂不可用（" + exc.reason + "）", ok=False)
        if local is None:
            return ActionResult("本机没有这条收据的缓存（可能已过期清理）；云端状态："
                                + str(cloud.get("state") or "—") + "。", ok=False)
        differences = _receipt_differences(local, local_row, cloud)
        if not differences:
            return ActionResult("已验证：云端与本机一致（收据 " + ident + "，状态 "
                                + str(cloud.get("state") or "—") + "）。", effect="verify")
        detail = "；".join(field + " 本机=" + mine + " 云端=" + theirs
                           for field, mine, theirs in differences[:4])
        more = "" if len(differences) <= 4 else ("；另有 " + str(len(differences) - 4) + " 处不同")
        return ActionResult("不一致：" + detail + more + "。", ok=False, effect="verify")

    # ── 收益明细导出（本地 CSV，不联网）
    def _act_earnings_export(self, *, confirm=None) -> ActionResult:
        earnings = self.snapshot.get("earnings")
        rows = [row for row in ((earnings or {}).get("payouts") or [])
                if isinstance(row, Mapping)] if isinstance(earnings, Mapping) else []
        if not rows:
            return ActionResult("暂不可用（没有可导出的记录）", ok=False)
        if self.save_path is None:
            return ActionResult("暂不可用（没有可用的保存对话框）", ok=False)
        default = "djb-earnings-" + time.strftime("%Y%m%d-%H%M", time.localtime(time.time())) + ".csv"
        target = self.save_path(default)
        if not target:
            return ActionResult("已取消导出（未生成文件）。", ok=False, effect="cancelled")
        header = ("时间", "任务", "请求", "收据", "Token", "金额")
        lines = [",".join(header)]
        for row in rows:
            lines.append(",".join(_csv_cell(value) for value in (
                row.get("created_at") or row.get("recorded_at") or "—",
                row.get("task_id") or "—", row.get("id") or "—",
                row.get("receipt_id") or "—", row.get("tokens") or "—",
                row.get("amount") or "—")))
        text = "\ufeff" + "\r\n".join(lines) + "\r\n"      # BOM 让 Excel 认 UTF-8
        try:
            path = Path(str(target))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            return ActionResult("导出失败（" + type(exc).__name__ + "）；未生成完整文件。", ok=False)
        counted = max(0, len(written) - 1)
        if counted != len(rows):
            return ActionResult("导出校验失败：写入 " + str(counted) + " 行，应为 "
                                + str(len(rows)) + " 行。", ok=False)
        return ActionResult("已导出 " + str(counted) + " 行到 " + str(target)
                            + "（回读 " + str(counted) + " 行一致）。", effect="export",
                            payload=str(target))

    # 未接线动作：如实说，不假装成功
    def _act_models_save(self, *, confirm=None) -> ActionResult:
        return ActionResult("暂不可用（云端意向上报接口未接线）；改动未上报，未生效。", ok=False)

    def _act_account_enable_2fa(self, *, confirm=None) -> ActionResult:
        return ActionResult("暂不可用（2FA 入口在服务端控制台）；本机未做任何改动。", ok=False)

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
        prefix = "【演示数据】" if getattr(self.snapshot, "source", "") == "demo" else ""
        return prefix + "daemon：" + state_text + "    ·    " + self.message


def secret_hint(value: str) -> str:
    """给 UI 的占位提示（永不回显原值）。"""
    return mask_secret(value) if value else "未设置"

# ─────────────────────────────────────────────────────────────── 小工具（导出 / 校验）
def _csv_cell(value: Any) -> str:
    """CSV 单元格：含逗号/引号/换行就加引号转义（不改变内容）。"""
    text = str(value if value is not None else "—")
    if any(char in text for char in (',', '"', '\n', '\r')):
        return '"' + text.replace('"', '""') + '"'
    return text


def _local_hash(receipt: Mapping[str, Any]) -> str:
    """本机 sig_provider 的摘要前 8 位（与云端 ``hash`` 同一脱敏口径）。"""
    signature = str(receipt.get("sig_provider") or "")
    digest = signature.rsplit(":", 1)[-1].strip()
    return digest[:8] if len(digest) >= 8 else ""


def _amount_text(value: Any) -> str:
    """十进制字符串 → micro-USD 显示文本（≥2 位、≤6 位、去尾零）；算不出给空串。"""
    try:
        micro = round(float(str(value)) * 1_000_000)
    except (TypeError, ValueError):
        return ""
    whole, frac = divmod(abs(micro), 1_000_000)
    text = f"{frac:06d}".rstrip("0")
    return ("-" if micro < 0 else "") + f"{whole}." + text.ljust(2, "0")


def _receipt_differences(local: Mapping[str, Any], cloud_row: Mapping[str, Any],
                         cloud_status: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """本机密封收据 vs 云端（列表行 + 单条状态）逐字段比对；只比对两边都有的字段。"""
    differences: list[tuple[str, str, str]] = []

    def compare(name: str, mine: Any, theirs: Any) -> None:
        mine_text = str(mine if mine not in (None, "") else "")
        theirs_text = str(theirs if theirs not in (None, "") else "")
        if mine_text and theirs_text and mine_text != theirs_text:
            differences.append((name, mine_text, theirs_text))

    compare("receipt_id", local.get("receipt_id"), cloud_row.get("receipt_id")
            or cloud_status.get("receipt_id"))
    compare("task_id", local.get("task_id"), cloud_row.get("task_id"))

    mine_hash = _local_hash(local)
    # 云端 ``hash`` 只给前 8 位 + …（脱敏，完整签名不出库），所以只能拿它去比本机的前缀
    cloud_hash = str(cloud_row.get("hash") or "").rstrip("…").strip().lower()
    if mine_hash and cloud_hash and not mine_hash.lower().startswith(cloud_hash):
        differences.append(("hash", mine_hash[:len(cloud_hash)] + "…", cloud_hash + "…"))

    price = local.get("price") if isinstance(local.get("price"), Mapping) else {}
    mine_amount = _amount_text(price.get("amount"))
    # 正式字段 revenue_micro；缺了回落 revenue_minor（同值同列，允许的回落）
    cloud_revenue = cloud_row.get("revenue_micro")
    if not isinstance(cloud_revenue, int):
        cloud_revenue = cloud_row.get("revenue_minor")
    theirs_amount = (_amount_text(int(cloud_revenue) / 1_000_000)
                     if isinstance(cloud_revenue, int) else "")
    if mine_amount and theirs_amount and mine_amount != theirs_amount:
        differences.append(("金额", mine_amount, theirs_amount))
    return differences
