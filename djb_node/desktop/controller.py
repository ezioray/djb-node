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
                     AccessConfig, AccessService, FileAccessStore, SaveResult, mask_secret,
                     validate_address)
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
        from ..config import Paths, load_config, resolve_home
        from ..tray.actions import apply_control, control_enabled
        from ..tray.model import TrayAction
        # home=None 时必须用 resolve_home()（Windows = %APPDATA%\DJB）：写死 ~/.djb 会连到
        # 不存在的控制口 → 暂停 / 继续永远「控制通道未接通」。
        paths = Paths(os.fspath(self._home)) if self._home else Paths(resolve_home())
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
    #: 异步刷新通道（GUI 侧注入 worker）；None = 同步刷新（单测 / 无 Tk 环境）
    refresh_via: Callable[[Sequence[str] | None], Any] | None = None
    benchmark: Mapping[str, Any] | None = None
    #: G4（RC14 P0）：更新检查/安装走独立 worker（网络 + 签名校验，绝不在主线程）
    update_via: Callable[[str], Any] | None = None
    #: 探针异步通道（RC14 D）：返回 True = 已派给 worker（网络在 worker 线程里跑）
    probe_via: Callable[[], Any] | None = None
    #: RC14 B：首次引导状态（``djb_node.onboarding.OnboardingStore``；None = 未接本机目录）
    onboarding: Any | None = None
    #: 当前引导步骤（1..3；4 = 完成页；None = 不显示面板）
    onboarding_step: int | None = None
    #: RC14 B 读取模型清单（``GET {base}/models``）的异步通道；返回 True = 已派给 worker。
    #: 签名 ``(base_url, protocol, secret) -> bool``；没有通道时**同步**跑（单测/无 Tk）。
    discover_via: Callable[[str, str, str], Any] | None = None
    #: 最近一次「读取模型清单」的结果（``datasource.discover_upstream_models`` 原样）
    last_discover: Mapping[str, Any] | None = None
    #: 造 UpdateManager 的工厂（默认按本机 home 装；单测注入假管理器）
    update_factory: Callable[[], Any] | None = None
    #: 最近一次检查结果（``check()`` 原样 payload；界面/日志都用它）
    last_update: Mapping[str, Any] | None = None
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
        # RC14 B：账户页的「重新运行首次引导」入口由**开关**决定（默认关 → 按钮隐藏）
        onboarding_meta = {"enabled": self.onboarding_enabled(),
                           "visible": self.onboarding_visible(),
                           "step": self.onboarding_step}
        access = sections.get("access")
        merged_access = dict(access) if isinstance(access, Mapping) else {}
        merged_access["onboarding"] = onboarding_meta
        sections["access"] = merged_access
        # RC14 I：Token Plan 页的「选中行 + 该行已存底价」由本层注入（视图层不持有选中态）
        plan = sections.get("token_plan")
        if isinstance(plan, Mapping):
            merged_plan = dict(plan)
            selected_model = str(self.selected.get("token_plan") or "")
            merged_plan["selected_model"] = selected_model
            if selected_model:
                try:
                    merged_plan["selected_price"] = self._bids().price_of(selected_model)
                except Exception:
                    merged_plan["selected_price"] = ""
            if self.last_discover is not None:
                merged_plan["discover_note"] = str(
                    (self.last_discover or {}).get("note") or "")
                merged_plan["base_url"] = str(
                    (self.last_discover or {}).get("base_url") or "")
                merged_plan["protocol"] = str(
                    (self.last_discover or {}).get("protocol") or
                    merged_plan.get("protocol") or "")
            sections["token_plan"] = merged_plan
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
        """**同步**刷新（单测 / ``--shot`` / 没有事件循环时用）。

        交互路径见 :meth:`request_refresh`：GUI 里取数在 worker 线程跑，
        主线程只调 :meth:`apply_refresh`（Tk 不是线程安全的）。
        """
        names = tuple(sections) if sections else SECTIONS
        try:
            fresh = self.source.snapshot(sections=sections)
        except Exception as exc:                       # 数据层理论上不抛；兜底不让窗口空白
            return self.apply_refresh(None, sections=names, error=exc)
        return self.apply_refresh(fresh, sections=sections)

    def request_refresh(self, sections: Sequence[str] | None = None) -> bool:
        """动作内部请求刷新：接了异步通道就交给它（不阻塞 Tk 主线程），否则同步刷。

        返回 ``True`` = 已交给异步通道（结果稍后到）；``False`` = 已经同步刷完。
        """
        hook = self.refresh_via
        if hook is not None:
            try:
                hook(sections)
            except Exception:                          # 异步通道坏了也不许把动作打崩
                self.refresh(sections)
                return False
            return True
        self.refresh(sections)
        return False

    def apply_refresh(self, fresh: Any = None, *, sections: Sequence[str] | None = None,
                      error: BaseException | None = None) -> View:
        """把一次取数结果落到快照上（**必须在主线程调用**）。

        * ``error``：取数整体失败 → 只把这次点名的域标成「刷新失败（类型）」，
          其它域保留上一次结果（老口径一字不改：界面不空白、不静默）；
        * ``sections``：只刷部分域时**必须合并**进旧快照——数据源只返回被点名的域，
          直接替换会把其它 12 页一起变成「暂不可用」；
        * ``fresh is None`` 且无 error：按「没取到数据」处理，同样保留旧结果。
        """
        names = tuple(sections) if sections else SECTIONS
        if error is not None:
            merged = dict(self._snapshot.sections) if self._snapshot is not None else {}
            merged.update({name: Unavailable("刷新失败（" + type(error).__name__ + "）")
                           for name in names})
            self._snapshot = Snapshot(sections=merged, source="error")
            self.message = "刷新失败：界面保留上一次结果，详情见日志。"
        elif fresh is None:
            self.message = "刷新失败：没有取到数据；界面保留上一次结果，详情见日志。"
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

    #: 「留空 = 不改」的字段：秘密字段（空 = 保留原值）与**掩码回填**字段。
    #: ``usdt_address`` 属于后者：接入页的输入框不预填掩码（``Field.prefill=False``），
    #: 用户没动它就是空串——若照常合并会把已保存的收款地址**清空**（比原来的「掩码写回」
    #: 更糟）。要改地址就填新值，点「提交到云端」；本版次不提供 UI 清空（没有这个需求）。
    KEEP_WHEN_EMPTY = ("node_secret", "api_key", "scoped_token", "usdt_address")

    def _config_for_save(self) -> tuple[AccessConfig, list[str]]:
        """把草稿合并进当前配置；``KEEP_WHEN_EMPTY`` 里的字段空值表示「保持不变」。"""
        service = self._service()
        current = service.load()
        warnings: list[str] = []
        changes: dict[str, str] = {}
        for key, value in self.drafts.items():
            text = str(value or "")
            if key in self.KEEP_WHEN_EMPTY and not text.strip():
                continue                               # 空 = 不覆盖已有值
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
        if self.request_refresh(None):
            return ActionResult("正在刷新…", effect="refresh")
        return ActionResult(self.message, effect="refresh")

    # ── 探针（RC14 D）：手动补一次体检，明细回「模型与探针」页
    def _act_probe_run(self, *, confirm=None) -> ActionResult:
        """「立即探测」：逐上游跑 ``djb upstream probe``（**网络 + 服务端回报，必须走 worker**）。

        与「检查更新」同一套通道（``probe_via``）：主线程只负责派发与播报。
        """
        hook = self.probe_via
        if hook is not None:
            try:
                started = hook()
            except Exception:                      # 异步通道坏了 → 回落同步
                started = False
            if started:
                return ActionResult("正在探测上游（L1 列表 + L2 推理 / L3 流式，通常 10 秒内）…",
                                    effect="probe", payload={"state": "probing"})
        return self.run_probe()

    def run_probe(self) -> ActionResult:
        """worker 侧：逐个上游探测，并把结论翻成一条人话（不碰控件）。"""
        if self.cli is None:
            return ActionResult("暂不可用（本机命令通道未接通）；没有发起探测。", ok=False)
        try:
            listing = self.cli(["upstream", "list"])
        except ProviderError as exc:
            return ActionResult("探测未开始：" + exc.reason, ok=False)
        except Exception as exc:
            return ActionResult("探测未开始（" + type(exc).__name__ + "）", ok=False)
        rows = listing.get("upstreams") if isinstance(listing, Mapping) else None
        ids = [str(item.get("id")) for item in (rows or [])
               if isinstance(item, Mapping) and item.get("id")]
        if not ids:
            return ActionResult("还没有接入任何上游：先去「提供 Token Plan」填请求链接与密钥。",
                                ok=False)
        verified = failed = 0
        reasons: list[str] = []
        for identifier in ids:
            try:
                payload = self.cli(["upstream", "probe", "--id", identifier])
            except Exception as exc:
                failed += 1
                reasons.append(identifier + "：" + type(exc).__name__)
                continue
            checks = payload.get("model_checks") if isinstance(payload, Mapping) else None
            passed = sum(1 for item in (checks or [])
                         if isinstance(item, Mapping) and item.get("ok"))
            if passed:
                verified += 1
            else:
                failed += 1
                error = str(payload.get("error_class") or "") if isinstance(payload, Mapping) else ""
                reasons.append(identifier + ("：" + error if error else "：没有模型通过"))
        parts = ["探测完成：" + str(verified) + " 个上游有模型通过"]
        if failed:
            parts.append(str(failed) + " 个没通过（" + "；".join(reasons[:3])
                         + ("…" if len(reasons) > 3 else "") + "）"
                         + "：先看「原因」列逐条修（改 Key / 改链接 / 等上游）")
        parts.append("明细见下表")
        return ActionResult("；".join(parts) + "。", ok=bool(verified) or not failed,
                            effect="probe",
                            payload={"state": "done", "verified": verified, "failed": failed})

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
        """G2-1（RC14 P0）：照 :meth:`_act_refresh` 走异步通道，**不在 Tk 主线程取数**。

        ``gpu`` 域要跑 ``nvidia-smi``/``rocm-smi``（各 ``timeout=4.0``，两次尝试 ≤8 s），
        同步刷会把窗口冻住，客户看到的是「点了像卡住」。
        """
        if self.request_refresh(("gpu",)):
            return ActionResult("正在刷新 GPU 信息…（可以继续拖动/切页）", effect="refresh")
        return ActionResult("已刷新 GPU 信息。", effect="refresh")

    def _act_tasks_refresh(self, *, confirm=None) -> ActionResult:
        """G2-2（RC14 P0）：同 :meth:`_act_gpu_refresh`（任务域含 CLI 子进程，≤8 s）。"""
        if self.request_refresh(("tasks",)):
            return ActionResult("正在刷新任务队列…（可以继续拖动/切页）", effect="refresh")
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

    def _act_access_submit_payout(self, *, confirm=None) -> ActionResult:
        """「提交到云端」：本地先校验（格式 + **链**），**不发请求**。

        真正的 ``POST`` 在 worker 线程里由 :meth:`submit_payout` 执行（Tk 主线程不碰网络，
        S2 口径）；这里只把「能提交什么」判清楚，返回 ``effect="payout_submit"`` 让渲染层
        去派发。TRC20 在本地就拦下并说清楚，不把请求打到云端等 409——客户会误以为重试就行。
        """
        from . import payout as payout_mod

        address = self.payout_address()
        if not address:
            return ActionResult("暂不可用（先填 USDT 收款地址，再点「提交到云端」）", ok=False)
        try:
            address = validate_address(address)        # 格式（TRC20 / EVM 都认，老配置兼容）
        except ValueError as exc:
            return ActionResult("提交失败：" + str(exc) + "（未发送到云端）", ok=False)
        blocked = payout_mod.submit_blocker(address)   # 链闸：只放行 0x（Polygon）
        if blocked:
            return ActionResult(blocked + "（未发送到云端）", ok=False)
        if not callable(getattr(self.source, "submit_payout_address", None)):
            return ActionResult("暂不可用（当前数据源不支持提交收款地址）；地址未变更。", ok=False)
        return ActionResult("正在提交收款地址到云端…（可以继续操作）",
                            effect="payout_submit", payload=address)

    def payout_address(self) -> str:
        """要提交的收款地址：草稿优先（用户刚填的），否则用已保存的配置值。"""
        draft = str(self.drafts.get("usdt_address") or "").strip()
        if draft:
            return draft
        try:
            return str(self._service().load().usdt_address or "").strip()
        except Exception:                          # 配置坏了：当没填（动作会说下一步）
            return ""

    def submit_payout(self, address: str) -> ActionResult:
        """**在 worker 线程里**提交收款地址（由 ``app._ensure_payout_worker`` 调用）。

        本地闸重判一次（防御：调用方可能绕过动作直接调），再调数据层；任何失败都带
        「地址未变更」——客户最怕的是「不知道到底改没改」。
        """
        from . import payout as payout_mod

        blocked = payout_mod.submit_blocker(address)
        if blocked:
            return ActionResult("提交失败：" + blocked + "（未发送到云端）", ok=False)
        handler = getattr(self.source, "submit_payout_address", None)
        if not callable(handler):
            return ActionResult("暂不可用（当前数据源不支持提交收款地址）；地址未变更。",
                                ok=False)
        try:
            data = handler(address)
        except ProviderError as exc:
            hint = ("；" + exc.hint) if exc.hint else ""
            return ActionResult("提交失败：" + exc.reason + hint + "（地址未变更）", ok=False)
        except Exception as exc:                   # 数据层理论上有归一，兜底不冒栈
            return ActionResult("提交失败（" + type(exc).__name__ + "）；地址未变更。", ok=False)
        return ActionResult(payout_mod.submit_message(data), ok=True)

    def _act_access_copy_node_id(self, *, confirm=None) -> ActionResult:
        """复制节点 ID（网页端绑定要填的那个）：只读快照，不写真机、不发请求。

        节点 ID 由服务端在注册时下发，落在本机凭据 / 状态库里（``datasource._resolve_node_id``
        已做三级回退）；这里只把它交给剪贴板，取不到就说清下一步。
        """
        access = self.snapshot.get("access")
        node_id = str(access.get("node_id") or "") if isinstance(access, Mapping) else ""
        if not node_id:
            # 旧文案「先启动一次客户端」在 3 级回退全空时是**误导**：客户已经启动了，
            # 真因是「节点进程没起来 / 注册没成功」。这里说清「去哪儿看原因」。
            daemon = access.get("daemon") if isinstance(access, Mapping) else None
            reason = str((daemon or {}).get("reason") or "") if isinstance(daemon, Mapping) else ""
            detail = ("；节点进程原因：" + reason) if reason else "；原因见「接入配置」页的注册状态"
            return ActionResult("暂不可用（还没有节点 ID" + detail + "）", ok=False)
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

    # ── RC14 I/J：一 Key 多模型 + 逐模型底价
    def _bids(self):
        """底价账本（懒建：只有真用到时才碰本机库）。"""
        from .. import bids as bids_mod

        store = getattr(self.source, "bids_store", None)
        if callable(store):
            return bids_mod.BidBook(store())
        store = getattr(self.source, "_store", None)
        if callable(store):
            try:
                return bids_mod.BidBook(store())
            except Exception:
                return bids_mod.BidBook(None)
        return bids_mod.BidBook(None)

    # ── RC14 B 首次引导（应用内面板，不是模态；每步都能跳）
    def _onboarding_store(self):
        from ..onboarding import OnboardingStore

        if self.onboarding is not None:
            return self.onboarding
        home = getattr(self.source, "_home", None)
        if not home:
            return None
        self.onboarding = OnboardingStore(str(home))
        return self.onboarding

    def onboarding_state(self):
        store = self._onboarding_store()
        return store.load() if store is not None else None

    def onboarding_enabled(self) -> bool:
        """开关（默认关）：关 → 不渲染面板，状态文件保留但不读。"""
        from .. import facts as facts_mod
        from ..config import Paths, feature_flag, load_config

        home = getattr(self.source, "_home", None)
        if not home:
            return False
        try:
            return bool(feature_flag(load_config(Paths(str(home))),
                                     facts_mod.ONBOARDING_FLAG))
        except Exception:
            return False

    def onboarding_visible(self) -> bool:
        if not self.onboarding_enabled():
            return False
        if self.onboarding_step is not None:
            return True
        state = self.onboarding_state()
        return bool(state is not None and state.should_show())

    def onboarding_panel(self):
        """当前该显示的面板（不需要显示就返回 None）。"""
        if not self.onboarding_visible():
            return None
        from .views import onboarding_panel

        step = self.onboarding_step
        if step is None:
            state = self.onboarding_state()
            step = (state.last_step if state is not None else 1) or 1
        return onboarding_panel(step, bound=self._bound_flag(),
                                upstream_count=self._upstream_count())

    def _bound_flag(self) -> bool | None:
        sections = self.snapshot.sections
        account = sections.get("account") if isinstance(sections.get("account"), Mapping) else {}
        access = sections.get("access") if isinstance(sections.get("access"), Mapping) else {}
        from .views import _account_bound

        profile = account.get("profile") if isinstance(account.get("profile"), Mapping) else {}
        security = (account.get("security")
                    if isinstance(account.get("security"), Mapping) else {})
        binding = access.get("binding") if isinstance(access.get("binding"), Mapping) else {}
        return _account_bound(profile, security, binding)

    def _upstream_count(self) -> int:
        sections = self.snapshot.sections
        plan = sections.get("token_plan") if isinstance(sections.get("token_plan"), Mapping) else {}
        rows = plan.get("upstreams")
        return len(rows) if isinstance(rows, list) else 0

    def _onboarding_move(self, step: int, *, skip: bool = False) -> ActionResult:
        store = self._onboarding_store()
        if store is None:
            return ActionResult("引导状态不可用（没有本机配置目录）", ok=False)
        state = store.load()
        if skip:
            state = store.skip_step(step, state)
        state = store.touch_step(step, state)
        self.onboarding_step = step
        self.refresh(sections=("account", "overview"))
        label = ("已跳过第 " + str(step) + " 步" if skip else "第 " + str(step) + " 步")
        return ActionResult(label + "（进度只记在本机 desktop/onboarding.json，跳过不写凭据）",
                            effect="refresh")

    def _act_onboarding_next(self, *, confirm=None) -> ActionResult:
        current = self.onboarding_step or 1
        panel_total = 3
        if current >= panel_total:
            self.onboarding_step = panel_total + 1
            store = self._onboarding_store()
            if store is not None:
                store.touch_step(panel_total + 1)
            return ActionResult("引导最后一步了：下面是完成页", effect="refresh")
        return self._onboarding_move(current + 1)

    def _act_onboarding_back(self, *, confirm=None) -> ActionResult:
        current = self.onboarding_step or 1
        return self._onboarding_move(max(1, current - 1))

    def _act_onboarding_skip(self, *, confirm=None) -> ActionResult:
        """「跳过这一步」：**任意步都可用**；写盘失败不阻断，只把话说清楚。"""
        current = self.onboarding_step or 1
        store = self._onboarding_store()
        saved = True
        if store is not None:
            saved = store.save(store.skip_step(current))
        self.onboarding_step = current + 1
        if saved and store is not None:
            store.touch_step(min(self.onboarding_step, 3))   # 进度只记 1..3
        self.refresh(sections=("account", "overview"))
        if not saved:
            return ActionResult("引导进度没存上（磁盘不可写），下次还会出现；本步已跳过",
                                ok=True, effect="refresh")
        return ActionResult("已跳过第 " + str(current) + " 步（进度只记在本机 "
                            "desktop/onboarding.json，跳过不写凭据）", effect="refresh")

    def _act_onboarding_skip_all(self, *, confirm=None) -> ActionResult:
        store = self._onboarding_store()
        if store is not None:
            store.skip_all()
        self.onboarding_step = None
        return ActionResult("已跳过首次引导：主界面照常可用；要再跑一次，"
                            "去「账户与安全 → 重新运行首次引导」（或 djb onboarding --reset）",
                            effect="refresh")

    def _act_onboarding_finish(self, *, confirm=None) -> ActionResult:
        store = self._onboarding_store()
        done = store.complete() if store is not None else None
        self.onboarding_step = None
        if done is None:
            return ActionResult("引导结束（进度未能写盘，下次启动可能还会出现）", ok=True,
                                effect="refresh")
        return ActionResult("引导完成：随时可在「账户与安全」重新运行", effect="refresh")

    def _act_onboarding_rerun(self, *, confirm=None) -> ActionResult:
        """「重新运行首次引导」（完成页与账户页共用）：清 dismissed、保留 completed 历史。"""
        store = self._onboarding_store()
        if store is None:
            return ActionResult("引导状态不可用（没有本机配置目录）", ok=False)
        state = store.load()
        state.dismissed_at = None
        store.save(state)
        self.onboarding_step = 1
        self.refresh(sections=("account",))
        return ActionResult("已重新开始首次引导（第 1 步，可随时跳过）", effect="refresh")

    def _act_account_rerun_onboarding(self, *, confirm=None) -> ActionResult:
        if not self.onboarding_enabled():
            return ActionResult("首次引导开关关闭（client.onboarding.enabled）："
                                "先 djb flags --enable client.onboarding.enabled 再重跑", ok=False)
        return self._act_onboarding_rerun(confirm=confirm)

    def _act_token_plan_discover(self, *, confirm=None) -> ActionResult:
        """第 2 步：``GET {base}/models`` 拉清单（**网络在 worker 线程**，主线程不等）。"""
        base_url = str(self.drafts.get("base_url") or "").strip()
        protocol = str(self.drafts.get("protocol") or "").strip()
        secret = str(self.drafts.get("upstream_secret") or "").strip()
        if not base_url:
            return ActionResult("先填「请求链接」（例如 https://api.openai.com/v1）", ok=False)
        hook = self.discover_via
        if hook is not None:
            try:
                if hook(base_url, protocol, secret):
                    return ActionResult("正在读取模型清单…（可以继续操作）", effect="refresh")
            except Exception:
                pass                                   # 异步通道坏了：退回同步，别把动作打崩
        return self.run_discover(base_url, protocol, secret)

    def run_discover(self, base_url: str, protocol: str, secret: str) -> ActionResult:
        """同步执行「读取模型清单」（worker 与无事件循环环境共用这一段）。"""
        fn = getattr(self.source, "discover_upstream_models", None)
        if not callable(fn):
            return ActionResult("暂不可用（当前数据源不支持读取模型清单）", ok=False)
        try:
            result = fn(base_url, protocol, secret)
        except ProviderError as exc:
            return ActionResult("读取失败：" + exc.reason, ok=False)
        except Exception as exc:
            return ActionResult("读取失败（" + type(exc).__name__ + "）；未保存任何配置。",
                                ok=False)
        self.last_discover = result
        error = str((result or {}).get("error_class") or "")
        if error:
            return ActionResult("读取失败：" + str((result or {}).get("note") or error), ok=False)
        return ActionResult("读到 " + str((result or {}).get("total") or 0) + " 个模型（能对上目录 "
                            + str((result or {}).get("mapped") or 0) + " 个）。",
                            effect="refresh",
                            payload={"state": "done",
                                     "models": [row.get("model") for row
                                                in (result or {}).get("models") or []]})

    def _act_token_plan_save_price(self, *, confirm=None) -> ActionResult:
        """第 3 步：把「最低申报价」写进选中那一行（本地账本，不上报）。"""
        model = str(self.selected.get("token_plan") or "").strip()
        price = str(self.drafts.get("min_price") or "").strip()
        if not model:
            return ActionResult("先在上面的表格里选一行模型", ok=False)
        try:
            saved = self._bids().set_price(model, price)
        except ValueError as exc:
            return ActionResult(str(exc), ok=False)
        except RuntimeError as exc:
            return ActionResult("保存失败：" + str(exc), ok=False)
        self.drafts.pop("min_price", None)
        if not saved:
            return ActionResult("已移除「" + model + "」的底价（这一行不再申报）",
                                effect="refresh")
        return ActionResult("已保存「" + model + "」底价：" + saved
                            + " USD / 百万 token（只在本机，未上报）", effect="refresh")

    def _act_token_plan_preview_report(self, *, confirm=None) -> ActionResult:
        """预览「将上报什么」：把本地账本摊成契约形状（不发请求）。"""
        payload = self._bids().report_payload()
        models = payload.get("models") or []
        if not models:
            return ActionResult("本地还没有任何底价：先在表格里选行、填价、保存", ok=False)
        lines = "、".join(str(item.get("model")) + "=" + str(item.get("min_price"))
                         for item in models[:6])
        more = "（等 " + str(len(models)) + " 个）" if len(models) > 6 else ""
        return ActionResult("将上报 " + str(len(models)) + " 个模型的底价：" + lines + more
                            + "；上报目标：" + " / ".join(payload.get("paths") or ())
                            + "（现在还不会发出去）",
                            payload={"state": "preview", "payload": payload})

    def _act_token_plan_report(self, *, confirm=None) -> ActionResult:
        """上报逐模型底价：开关默认关闭；云端未开通时**明确说没发**，不假成功。"""
        from .. import bids as bids_mod
        from ..config import Paths, load_config

        config = None
        home = getattr(self.source, "_home", None)
        try:
            if home:
                config = load_config(Paths(str(home)))
        except Exception:
            config = None
        blocker = bids_mod.report_blocker(config) if config is not None else (
            "本机配置读不到：无法确认上报开关状态（底价只保存在本机）")
        if blocker:
            return ActionResult(blocker + "（未发送任何内容）", ok=False)
        payload = self._bids().report_payload()
        if not payload.get("models"):
            return ActionResult("本地还没有任何底价：没有可上报的内容", ok=False)
        return ActionResult("上报通道已打开，但云端接口仍需上线后才能提交："
                            "本次未发送（预览见「预览将上报的内容」）", ok=False)

    def _act_token_plan_add_upstream(self, *, confirm=None) -> ActionResult:
        """E（RC14 IA 拆分）：「添加上游」不再跳到「本机设置」（那里根本不能加）。

        第 1 步就在本页下方「添加接入」（协议 / 请求链接 / 密钥 → 读取模型清单）。
        """
        return ActionResult("在下方「添加接入」填：协议 → 请求链接 → 密钥，再点「读取模型清单」。",
                            effect="goto", payload="token_plan")

    def _act_token_plan_bind_node(self, *, confirm=None) -> ActionResult:
        """绑定只有**一个**入口（「账户与安全」）；这里只负责把客户送过去。"""
        return ActionResult("已切到「账户与安全」：绑定 / 解绑 / 收款地址都在那一页。",
                            effect="goto", payload="account")

    def _act_access_goto_account(self, *, confirm=None) -> ActionResult:
        return ActionResult("绑定、解绑与 USDT 收款地址都在「账户与安全」页。",
                            effect="goto", payload="account")

    def _act_account_submit_payout(self, *, confirm=None) -> ActionResult:
        """账户页的「提交收款地址」（唯一位）；实现与旧的 ``access.submit_payout`` 同一段。"""
        return self._act_access_submit_payout(confirm=confirm)

    def _act_token_plan_refresh(self, *, confirm=None) -> ActionResult:
        """G2-3（RC14 P0）：三个域一起刷，走 worker；``apply_refresh`` 按 section 合并，
        **不整表替换**（否则正在看的那一列会被半截数据顶掉）。"""
        if self.request_refresh(("status", "token_plan", "receipts")):
            return ActionResult("正在刷新 Token Plan…（可以继续拖动/切页）",
                                effect="refresh")
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
        self.request_refresh(None)
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
        self.request_refresh(None)
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

    # ── 更新（G4 / RC14 P0）
    def _update_manager(self) -> Any:
        """造一个 UpdateManager：注入了工厂用工厂，否则按 home 现装（与 CLI 同参数）。"""
        factory = self.update_factory
        if factory is not None:
            return factory()
        # 没有工厂时按数据源的 home 现装；数据源没 home（假源/演示源）就**报错而不是**
        # 退回 ``resolve_home(None)``——那会去读客户真机的 ~/.djb（S3 的老坑）。
        home = getattr(self.source, "home", None)
        if home is None:
            raise RuntimeError("找不到本机配置目录（数据源没有 home）")
        from ..update import open_manager
        return open_manager(home)

    def _act_update_check(self, *, confirm=None) -> ActionResult:
        """「检查更新」：**必须走 worker**（网络 15 s + 签名校验）。

        接了异步通道（``update_via``）就派出去、立刻返回「正在检查…」；没有事件循环
        （截图脚本 / 单测）时原地同步跑一次——动作不许因为「没 worker」变成空操作。
        """
        cached = self._snapshot
        if cached is not None and getattr(cached, "source", "") == "demo":
            # 演示数据不联网（截图基线要确定性）；也绝不落到客户真机的 ~/.djb 上
            return ActionResult("演示数据：不联网检查更新（真实客户端会核对清单并校验签名）。",
                                ok=False, effect="update", payload={"state": "no_source"})
        hook = self.update_via
        if hook is not None:
            try:
                started = hook("check")
            except Exception:                      # 异步通道坏了 → 回落同步
                started = False
            if started:
                return ActionResult("正在检查更新…（联网核对清单 + 校验签名，通常几秒）",
                                    effect="update", payload={"state": "checking"})
        return self.run_update_check()

    def run_update_check(self) -> ActionResult:
        """同步执行检查（**只能在 worker 线程或测试里调**），结果落 ``last_update``。"""
        try:
            manager = self._update_manager()
            result = dict(manager.check() or {})
        except Exception as exc:                   # 未初始化 / 库坏 / 配置坏
            result = {"state": "offline", "current_version": None,
                      "message": "本机更新检查不可用（" + type(exc).__name__ + "："
                                 + str(exc)[:80] + "）"}
        self.last_update = result
        return self.update_result_to_action(result)

    @staticmethod
    def update_result_to_action(result: Mapping[str, Any]) -> ActionResult:
        """``check()`` 结果 → 一句可执行的话（九状态全覆盖，见 ``update.STATE_GUIDANCE``）。"""
        from ..update import guidance
        info = guidance(dict(result or {}))
        text = info["title"]
        if info["detail"]:
            text += "（" + info["detail"] + "）"
        text += " — 下一步：" + info["next"]
        return ActionResult(text, ok=bool(info["ok"]), effect="update",
                            payload=dict(result or {}))

    def _act_update_apply(self, *, confirm=None) -> ActionResult:
        """「安装更新」：二次确认后走 worker（下载 + 校验 + 装到版本目录，重启生效）。

        与 CLI ``djb update --apply`` 同一条代码路径；桌面端**永不自动装**。
        """
        if confirm is not None and not confirm(
                "现在下载并安装更新？装完需要重启客户端才生效。\n"
                "（校验失败会自动拒绝安装，版本不变）"):
            return ActionResult("已取消安装（版本未变）。", ok=False, effect="cancelled")
        hook = self.update_via
        if hook is not None:
            try:
                started = hook("apply")
            except Exception:
                started = False
            if started:
                return ActionResult("正在下载并安装更新…（校验签名与哈希，完成后提示重启）",
                                    effect="update", payload={"state": "applying"})
        return self.run_update_apply()

    def run_update_apply(self) -> ActionResult:
        """同步执行安装（worker 线程/测试用）；结果同 ``check()`` 的形状 + ``installed``。"""
        try:
            manager = self._update_manager()
            result = dict(manager.apply() or {})
        except Exception as exc:
            result = {"state": "failed_verify", "installed": False,
                      "message": "安装失败（" + type(exc).__name__ + "：" + str(exc)[:80] + "）"}
        self.last_update = result
        if result.get("installed"):
            version = str(result.get("version") or "")
            return ActionResult("已安装 " + version + "（重启客户端后生效）。", ok=True,
                                effect="update", payload=result)
        state = str(result.get("state") or "")
        reason = str(result.get("message") or "更新器没有给出原因（state=" + state + "）")
        return ActionResult("未安装：" + reason, ok=False, effect="update", payload=result)

    def _act_update_download(self, *, confirm=None) -> ActionResult:
        """「去下载页手动下载」：只在 ``no_artifact`` 时出现（本平台没发包 ≠ 没有更新）。"""
        url = str(facts.WEB_CONSOLE_URL or "").rstrip("/") + facts.UPDATE_DOWNLOAD_PATH
        return ActionResult("在浏览器打开下载页：" + url, effect="open_url", payload=url)

    # 未接线动作：如实说，不假装成功
    def _act_account_enable_2fa(self, *, confirm=None) -> ActionResult:
        """兜底：界面上这颗按钮已经是**禁用态**（并写明为什么），正常路径点不到。

        G1-2 口径：本机不代理 2FA 绑定（TOTP 的 enroll/confirm 只在网页控制台），
        所以正确做法是「禁用 + 灰字原因 + 旁边给一个真能打开控制台的按钮」，
        而不是让客户点了才看到「暂不可用」。
        """
        return ActionResult("暂不可用（2FA 只能在网页控制台开启）；本机未做任何改动。", ok=False)

    def _act_account_open_2fa_page(self, *, confirm=None) -> ActionResult:
        """G1-2：2FA 的**真入口**——用系统浏览器打开控制台的安全页（TOTP enroll/confirm）。

        地址来自 ``facts.WEB_CONSOLE_URL``（``DJB_WEB_CONSOLE`` 可覆盖）；渲染层
        ``_open_url`` 会再校验协议头，配错域名时**报错而不是静默**。
        """
        url = str(facts.WEB_CONSOLE_URL or "").rstrip("/") + facts.WEB_CONSOLE_SECURITY_PATH
        return ActionResult("在浏览器打开控制台安全页：" + url, effect="open_url",
                            payload=url)

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
