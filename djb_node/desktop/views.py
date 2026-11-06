# SPDX-License-Identifier: Apache-2.0
"""13 个视图的**纯投影**：:class:`Snapshot` → :class:`View`（无 Tk、无 IO、可快照断言）。

为什么单独一层
==============

* 视图口径（三冗余状态、`暂不可用`、金额不换算、秘密掩码）全部在这里定，
  Tk 层只按 :class:`View` 的字段画，不重新解释数据 —— 因此无图形环境也能断言 13 个视图。
* :meth:`View.plain_text` 给「文本快照」：既是 `ops/uta/evidence/wp6/views.txt` 的来源，
  也是无显示环境下的验收替代物。
* 状态语义（基准 §3）：`完成/已验证` = 薄荷 + 勾 + 文案；`等待` = 琥珀 + 半环 + 文案；
  `失败` = 珊瑚 + 叉 + 文案；`未知/未接线` = 灰 + 问号 + **暂不可用**。
  这里用 :data:`MARK_*` 前缀字符 + :class:`Card.tone` 双通道表达，颜色由主题层决定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .datasource import Snapshot, UNAVAILABLE, Unavailable
from .nodecloud import STATE_TEXT
from .. import facts

__all__ = [
    "Action", "Card", "Field", "Mark", "Row", "Table", "VIEWS", "VIEW_KEYS", "View",
    "build_view", "build_views",
]

#: 三冗余状态前缀（文字通道；颜色通道由 tone 决定）
MARK_DONE = "✓"
MARK_WAIT = "◐"
MARK_FAIL = "✕"
MARK_UNKNOWN = "?"
MARK_IDLE = "·"


class Mark:
    """状态前缀常量（类只是命名空间，避免到处写散字符串）。"""

    DONE = MARK_DONE
    WAIT = MARK_WAIT
    FAIL = MARK_FAIL
    UNKNOWN = MARK_UNKNOWN
    IDLE = MARK_IDLE


#: 13 视图（key, 侧栏标题）——顺序即 IA 顺序
#:
#: 第 9 项「共享 Token Plan」（键名 ``token_plan``）回应「客户找不到共享 Token Plan 入口」：
#: 它**聚合**已有数据域（access/account 的绑定态 + 上游清单 + 限额预算 + 今日用量），
#: 不新造后端接口；放在「接入配置」之前，客户第一眼就能看到「共享了什么、还能共享多少」，
#: 主操作一步跳到「接入配置」去加第一条上游。
VIEWS: tuple[tuple[str, str], ...] = (
    ("overview", "总览"),
    ("bench", "性能基准"),
    ("models", "可提供模型"),
    ("gpu", "GPU 监控"),
    ("tasks", "任务队列"),
    ("receipts", "收据"),
    ("earnings", "收益"),
    ("agent", "Agent 接入"),
    ("token_plan", "共享 Token Plan"),
    ("access", "接入配置"),
    ("yield", "让路 / 节能"),
    ("account", "账户与安全"),
    ("logs", "日志"),
)
VIEW_KEYS: tuple[str, ...] = tuple(key for key, _ in VIEWS)
VIEW_TITLES: dict[str, str] = dict(VIEWS)


@dataclass(frozen=True)
class Card:
    """一张指标卡：``tone`` 决定颜色（default/mint/amber/red/muted）。"""

    title: str
    value: str
    meta: str = ""
    tone: str = "default"


@dataclass(frozen=True)
class Row:
    """表格行：``key`` 用于详情抽屉，``disabled`` 用于不可选/不兼容并给 ``note``。"""

    cells: tuple[str, ...]
    key: str = ""
    disabled: bool = False
    note: str = ""
    #: 行级语义色（default/mint/amber/red/muted）：给日志级别这类整行状态用
    tone: str = "default"
    #: 窄窗下的紧凑单元格（同长度、更短文案）；详情抽屉仍用完整 ``cells``
    narrow: tuple[str, ...] = ()


@dataclass(frozen=True)
class Table:
    columns: tuple[str, ...]
    rows: tuple[Row, ...] = ()
    note: str = ""
    empty: str = "暂无数据"
    #: 窄窗（E10 口径 390）下先让位的列；数据不丢，详情抽屉里仍是全字段
    narrow_hide: tuple[str, ...] = ()
    #: 窄窗下的表头短名（只改表头，不改数据）
    narrow_headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Field:
    """一行「标签 / 值」；``secret`` 表示只显示掩码，``editable`` 表示可编辑输入。"""

    label: str
    value: str
    key: str = ""
    editable: bool = False
    secret: bool = False
    hint: str = ""
    tone: str = "default"


@dataclass(frozen=True)
class Action:
    """可点动作；``enabled=False`` 时按钮置灰并显示 ``reason``（不隐藏）。"""

    key: str
    label: str
    enabled: bool = True
    reason: str = ""
    primary: bool = False


@dataclass(frozen=True)
class View:
    key: str
    title: str
    subtitle: str = ""
    cards: tuple[Card, ...] = ()
    table: Table | None = None
    fields: tuple[Field, ...] = ()
    actions: tuple[Action, ...] = ()
    notes: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()

    def plain_text(self) -> str:
        """文本快照（证据 / 无显示环境验收）：稳定顺序，无时间戳抖动。"""
        lines = ["# " + self.title + (("  ·  " + self.subtitle) if self.subtitle else "")]
        for item in self.unavailable:
            lines.append("! " + item)
        if self.cards:
            lines.append("")
            for card in self.cards:
                suffix = ("  " + card.meta) if card.meta else ""
                lines.append("[" + card.title + "] " + card.value + suffix)
        if self.table is not None:
            lines.append("")
            lines.append(" | ".join(self.table.columns))
            if not self.table.rows:
                lines.append("（" + self.table.empty + "）")
            for row in self.table.rows:
                prefix = "x " if row.disabled else "- "
                line = prefix + " | ".join(row.cells)
                if row.note:
                    line += "   " + row.note
                lines.append(line)
            if self.table.note:
                lines.append("… " + self.table.note)
        if self.fields:
            lines.append("")
            for item in self.fields:
                lines.append(item.label + "：" + item.value
                             + (("   " + item.hint) if item.hint else ""))
        if self.actions:
            lines.append("")
            for action in self.actions:
                state = "" if action.enabled else ("（置灰：" + (action.reason or "当前不可用") + "）")
                lines.append("[" + action.label + "]" + state)
        for note in self.notes:
            lines.append("· " + note)
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────── 小工具
def _num(value: Any) -> str:
    """金额 / 数值原样展示（不换算、不四舍五入）；缺失给「—」而不是 0。"""
    if value is None or value == "" or isinstance(value, (bool, Mapping, list, tuple)):
        return "—"
    return str(value)


def _money(snapshot: Snapshot, name: str, value: Any, currency: str = "") -> str:
    """金额卡：整域不可用就统一说「暂不可用」；缺单个值给「—」，绝不补 0。"""
    if not snapshot.ok(name):
        return UNAVAILABLE
    text = _num(value)
    return text + " " + currency if currency and text != "—" else text


def _flag(value: Any, true: str, false: str, unknown: str = "暂不可用") -> str:
    if value is True:
        return MARK_DONE + " " + true
    if value is False:
        return MARK_FAIL + " " + false
    return MARK_UNKNOWN + " " + unknown


def _bytes(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB":
            return (str(int(number)) if unit == "B" else f"{number:.1f}") + " " + unit
        number /= 1024
    return "—"


def _missing(snapshot: Snapshot, names: Sequence[str]) -> tuple[str, ...]:
    """不可用数据域的中文原因（渲染在视图顶部，不隐藏页面）。"""
    return tuple(snapshot.missing(name).text for name in names
                 if snapshot.missing(name) is not None)


def _count(snapshot: Snapshot, name: str, text: str) -> str:
    """数据域不可用时，计数只能说「暂不可用」——绝不把缺数据说成 0。"""
    return text if snapshot.ok(name) else UNAVAILABLE


def _empty_text(snapshot: Snapshot, name: str, text: str) -> str:
    return text if snapshot.ok(name) else UNAVAILABLE


def _section(snapshot: Snapshot, name: str) -> Mapping[str, Any]:
    value = snapshot.get(name)
    return value if isinstance(value, Mapping) else {}


# ─────────────────────────────────────────────────────────────── 13 个构建器
def _overview(snapshot: Snapshot) -> View:
    status, earnings = _section(snapshot, "status"), _section(snapshot, "earnings")
    receipts = _section(snapshot, "receipts")
    yield_state = _section(snapshot, "yield")
    tasks = _section(snapshot, "tasks")
    task_rows = [row for row in (tasks.get("rows") or []) if isinstance(row, Mapping)]
    if tasks and task_rows:
        count = sum(1 for row in task_rows if row.get("inflight"))
        task_text = str(count) + " 个在途"
    else:
        inflight = status.get("inflight")
        task_text = (str(len(inflight)) + " 个在途") if isinstance(inflight, list) else "—"
    reason = status.get("reason_label") or status.get("reason")
    state_map = {"online": (MARK_DONE + " 运行中", "mint"), "starting": (MARK_WAIT + " 启动中", "amber"),
                 "paused": (MARK_WAIT + " 已暂停", "amber"), "updating": (MARK_WAIT + " 更新中", "amber"),
                 "stopped": (MARK_IDLE + " 已停止", "muted"), "error": (MARK_FAIL + " 错误", "red")}
    state_text, tone = state_map.get(str(status.get("state")), (MARK_UNKNOWN + " 状态未知", "muted"))
    cards = (
        Card("今日贡献", _num(earnings.get("today") or status.get("today_earnings")) + " USDT",
             _today_note(earnings.get("today") or status.get("today_earnings")), "mint"),
        Card("当前任务", task_text, "在途任务", "default"),
        Card("余额（可用）",
             ("暂不可用" if earnings.get("balance_note") and earnings.get("balance") is None
              else _num(earnings.get("balance") or status.get("balance"))),
             ("账户预付款 · 不可提现" if earnings.get("balance") is not None
              else str(earnings.get("balance_note") or "额度不可提现，仅抵扣推理")),
             "default"),
        Card("接单状态", state_text, str(reason or "无让路原因"), tone),
        Card("资源", _bytes(_section(snapshot, "bench").get("mem_available_bytes")) + " 可用内存",
             "CPU " + str(_section(snapshot, "bench").get("cpu_count") or "—") + " 核", "default"),
        Card("让路 / 节能", str(yield_state.get("mode") or "未设置"),
             "暂停后与托盘一致", "muted"),
    )
    rows = _receipt_rows(receipts.get("rows"))
    return View(
        key="overview", title="总览",
        subtitle="今日贡献 · 当前任务 · 资源 · 让路 · 收据",
        cards=cards,
        table=Table(("收据号", "时间", "Tokens", "状态", "本机校验"), tuple(rows[:5]),
                    narrow_hide=("时间", "Tokens"),
                    narrow_headers=(("本机校验", "校验"),),
                    note=str(receipts.get("note") or ""),
                    empty="还没有收据（完成后自动生成）"),
        unavailable=_missing(snapshot, ("status", "earnings")),
        notes=("数值全部原样展示服务端字段；无数据显示「—」，不显示 0。",),
    )


#: 计量收据拒收码 → 中文解释（源：cloud ``app/metering/errors.py`` 的错误码表）
REJECT_TEXT = {
    "40001": "请求不合法",
    "40013": "请求体超上限",
    "40101": "未认证",
    "40303": "无权代表该节点",
    "40401": "收据不存在",
    "46001": "收据签名不匹配",
    "50001": "服务端未就绪",
}


def _reject_text(code: Any, reason: Any, state: Any) -> str:
    """``rejected``/``expired`` 行必须说清为什么被拒；码没登记就如实说「未知拒绝码」。"""
    text = str(state or "")
    code_text = str(code).strip() if code not in (None, "") else ""
    if not code_text:
        return text
    meaning = REJECT_TEXT.get(code_text, "未知拒绝码")
    extra = ("；" + str(reason)) if reason else ""
    return text + "（" + code_text + " " + meaning + extra + "）"


def _short_id(value: Any, keep: int = 8) -> str:
    """窄窗里的主键：太长就留尾段（``…rj_0001``），完整值在详情抽屉里。"""
    text = str(value or "")
    return text if len(text) <= keep + 2 else "…" + text[-keep:]


#: 窄窗下的校验结论短名（4 个字以内，免得被列宽截断成「无副」）
_LOCAL_CHECK_SHORT = {"match": "一致", "mismatch": "不一致",
                      "no_local": "无副本", "unreadable": "不可读"}


def _local_check_text(row: Mapping[str, Any]) -> str:
    """本机校验结论：没有本机副本就如实说没有，绝不把云端受理当成「已验证」。"""
    check = str(row.get("local_check") or "")
    if check == "match":
        return MARK_DONE + " 一致"
    if check == "mismatch":
        return MARK_FAIL + " 不一致"
    if check == "no_local":
        return MARK_IDLE + " 本机无副本"
    if check == "unreadable":
        return MARK_WAIT + " 本机不可读"
    return MARK_IDLE + " 未校验"


def _receipt_rows(rows: Any) -> list[Row]:
    result: list[Row] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        identifier = str(row.get("receipt_id") or row.get("id") or "")
        state = str(row.get("state") or row.get("status") or "")
        chinese = str(row.get("state_text") or STATE_TEXT.get(state, ""))
        mark = (MARK_DONE if state in ("accepted", "verified", "ok", "done")
                else MARK_WAIT if state in ("pending", "buffered")
                else MARK_FAIL if state in ("rejected", "expired", "failed", "error")
                else MARK_UNKNOWN)
        if state in ("rejected", "expired"):
            label = _reject_text(row.get("rejected_code"), row.get("reject_reason"), chinese or state)
        elif chinese:
            label = chinese
        elif state:
            label = state + "（未知状态）"          # 没登记的词原样显示，不猜
        else:
            label = "待回执"
        check_text = _local_check_text(row)
        code = str(row.get("rejected_code") or "").strip()
        short = (mark + " " + (chinese or state) + ((" " + code) if code else "")).strip()
        result.append(Row((identifier or "—", str(row.get("created_at") or row.get("ts") or "—"),
                           _num(row.get("tokens") or row.get("tokens_total")),
                           mark + " " + label, check_text),
                          key=identifier,
                          # 窄窗只留码，中文解释在详情抽屉/备注里，不丢信息也不挤爆列
                          narrow=((_short_id(identifier) or "—"),
                                  str(row.get("created_at") or row.get("ts") or "—"),
                                  _num(row.get("tokens") or row.get("tokens_total")),
                                  short,
                                  _LOCAL_CHECK_SHORT.get(
                                      str(row.get("local_check") or ""), check_text))))
    return result


def _bench(snapshot: Snapshot) -> View:
    bench = _section(snapshot, "bench")
    gpu = _section(snapshot, "gpu")
    checks = bench.get("checks") if isinstance(bench.get("checks"), list) else []
    rows = []
    for check in checks:
        if not isinstance(check, Mapping):
            continue
        level = str(check.get("level") or "")
        mark = MARK_DONE if level == "ok" else MARK_FAIL if level == "error" else MARK_WAIT
        rows.append(Row((mark + " " + str(check.get("id") or "—"),
                         str(check.get("summary") or "—"),
                         str(check.get("next_step") or "—"))))
    devices = gpu.get("devices") if isinstance(gpu.get("devices"), list) else []
    result = bench.get("benchmark") if isinstance(bench.get("benchmark"), Mapping) else {}
    cards = (
        Card("CPU", str(bench.get("cpu_count") or "—") + " 核",
             "1 分钟负载 " + _num(bench.get("cpu_load_1m")), "default"),
        Card("内存", _bytes(bench.get("mem_total_bytes")), "可用 " + _bytes(bench.get("mem_available_bytes")), "default"),
        Card("磁盘可用", _bytes(bench.get("disk_free_bytes")), "总量 " + _bytes(bench.get("disk_total_bytes")), "default"),
        Card("GPU", _count(snapshot, "gpu",
                           (str(len(devices)) + " 块") if devices else "无可用 GPU"),
             str(gpu.get("note") or "—"), "mint" if devices else "muted"),
        Card("sha256", (str(result.get("hash_mbps")) + " MB/s") if result else "未运行",
             "本地计算，不联网", "default"),
        Card("浮点 / 整数", ((str(result.get("float_mops")) + " / " + str(result.get("int_mops")) + " Mops/s")
                            if result else "未运行"), "≤10 秒预算", "default"),
    )
    return View(
        key="bench", title="性能基准",
        subtitle="环境检测 + CPU/内存/磁盘 + 可选 GPU",
        cards=cards,
        table=Table(("检查项", "结论", "下一步"), tuple(rows),
                    note="检查项来自本机只读探测", empty="环境检测暂不可用"),
        actions=(Action("bench.run", "开始跑分（≤10 秒）", primary=True),
                 Action("bench.copy", "复制结果")),
        unavailable=_missing(snapshot, ("bench",)),
        notes=("跑分只做本地计算：sha256 吞吐 / 浮点 / 整数；不上传、不写 store。",),
    )


def _models(snapshot: Snapshot) -> View:
    models = _section(snapshot, "models")
    catalog = models.get("catalog") if isinstance(models.get("catalog"), list) else []
    rows = []
    for row in catalog:
        if not isinstance(row, Mapping):
            continue
        offerable = bool(row.get("offered", True))
        reason = str(row.get("reason") or "")
        mark = MARK_DONE + " 可提供" if offerable else MARK_FAIL + " 不兼容"
        rows.append(Row((str(row.get("id") or "—"), _num(row.get("context")), mark,
                         reason or ("按当前资源可提供" if offerable else "原因未下发")),
                        key=str(row.get("id") or ""), disabled=not offerable))
    local = models.get("local") if isinstance(models.get("local"), list) else []
    return View(
        key="models", title="可提供模型",
        subtitle="逐模型开关 · 不兼容标灰并给原因",
        cards=(Card("云端目录", _count(snapshot, "models", str(len(catalog)) + " 个"),
                    "来自 /models 目录", "default"),
               Card("本地已应用", _count(snapshot, "models", str(len(local)) + " 个"),
                    "model-sync 状态", "default")),
        table=Table(("模型", "上下文", "状态", "原因 / 说明"), tuple(rows),
                    note=str(models.get("note") or ""), empty="云端目录暂不可用"),
        actions=(Action("models.save", "保存并上报意向", enabled=bool(rows),
                        reason="没有可编辑的模型目录"),),
        unavailable=_missing(snapshot, ("models",)),
        notes=("开关变更只上报意向；服务端确认后才会真正分配任务。",),
    )


def _gpu(snapshot: Snapshot) -> View:
    gpu = _section(snapshot, "gpu")
    devices = gpu.get("devices") if isinstance(gpu.get("devices"), list) else []
    rows = []
    for device in devices:
        if not isinstance(device, Mapping):
            continue
        rows.append(Row((str(device.get("name") or "—"), str(device.get("vendor") or "—"),
                         (str(device.get("memory_used_mb")) + " / "
                          + str(device.get("memory_total_mb")) + " MB"),
                         (str(device.get("temperature_c")) + " °C"),
                         (str(device.get("utilization_pct")) + " %"))))
    present = bool(gpu.get("present")) and bool(rows)
    return View(
        key="gpu", title="GPU 监控",
        subtitle="nvidia-smi 优先，rocm-smi 兜底",
        cards=(Card("GPU 数量",
                    _count(snapshot, "gpu", str(len(rows)) if present else "0（无可用 GPU）"),
                    str(gpu.get("note") or "—"), "mint" if present else "muted"),),
        table=Table(("型号", "厂商", "显存 已用/总", "温度", "利用率"), tuple(rows),
                    note=str(gpu.get("note") or ""),
                    empty="本机无可用 GPU（nvidia-smi / rocm-smi 都没有输出）"),
        actions=(Action("gpu.refresh", "刷新"),),
        unavailable=_missing(snapshot, ("gpu",)),
        notes=("没有 GPU 不隐藏本页；显存/温度/利用率只在真读到时展示。",),
    )


def _tasks(snapshot: Snapshot) -> View:
    tasks = _section(snapshot, "tasks")
    rows = []
    for row in tasks.get("rows") if isinstance(tasks.get("rows"), list) else []:
        if not isinstance(row, Mapping):
            continue
        status = str(row.get("state") or row.get("status") or "")
        chinese = str(row.get("state_text") or STATE_TEXT.get(status, ""))
        # 状态列：中文 + 原词（原词是契约里的词，别只给翻译）；没登记的词原样 + 「未知状态」
        label = (chinese + "（" + status + "）") if chinese else (
            status + "（未知状态）" if status else "—")
        # 「在途」是独立的布尔字段，不能拿来顶替 state
        if row.get("inflight"):
            label += " · 进行中"
        mark = (MARK_DONE if status in ("settled", "corrected", "done", "completed", "ok")
                else MARK_WAIT if (row.get("inflight") or status in
                                   ("running", "queued", "pending"))
                else MARK_FAIL if status in ("failed", "error", "rejected", "expired")
                else MARK_UNKNOWN)
        tone = ("mint" if mark == MARK_DONE else "amber" if mark == MARK_WAIT
                else "red" if mark == MARK_FAIL else "muted")
        elapsed = str(row.get("elapsed_s") or row.get("duration_s") or "")
        rows.append(Row((str(row.get("id") or row.get("task_id") or "—"), mark + " " + label,
                         elapsed or ("进行中" if row.get("inflight") else "—"),
                         _num(row.get("tokens") or row.get("tokens_total")),
                         str(row.get("receipt_id") or "—")),
                        key=str(row.get("id") or row.get("task_id") or ""), tone=tone))
    return View(
        key="tasks", title="任务队列",
        subtitle="本地在途 / 云端任务（谁先有数据用谁）",
        cards=(Card("队列", _count(snapshot, "tasks", str(len(rows)) + " 条"),
                    ("来源：" + str(tasks.get("origin") or "local")), "default"),),
        table=Table(("任务", "状态", "耗时", "Tokens", "收据号"), tuple(rows),
                    narrow_hide=("耗时", "收据号"),
                    note=str(tasks.get("note") or ""),
                    empty=_empty_text(snapshot, "tasks", "当前没有任务")),
        actions=(Action("tasks.refresh", "刷新"),),
        unavailable=_missing(snapshot, ("tasks",)),
        notes=("状态列给云端原词（recorded/settled/…）+ 中文；「进行中」来自 inflight 字段。",
               "点行看详情：状态三态、耗时、tokens、收据号。"),
    )


def _receipts(snapshot: Snapshot) -> View:
    receipts = _section(snapshot, "receipts")
    rows = _receipt_rows(receipts.get("rows"))
    raw_rows = [row for row in (receipts.get("rows") or []) if isinstance(row, Mapping)]
    verified = sum(1 for row in raw_rows if str(row.get("local_check") or "") == "match")
    mismatch = sum(1 for row in raw_rows if str(row.get("local_check") or "") == "mismatch")
    no_local = sum(1 for row in raw_rows if str(row.get("local_check") or "") == "no_local")
    if not raw_rows:
        verify_meta = "没有收据可校验"
    elif no_local == len(raw_rows):
        verify_meta = "本机无副本：云端受理 ≠ 本机校验过"
    else:
        verify_meta = "本机无副本 " + str(no_local) + " 张" + (
            "；不一致 " + str(mismatch) + " 张" if mismatch else "")
    return View(
        key="receipts", title="收据",
        subtitle="双签字段 · Merkle 路径 · 本地校验",
        cards=(Card("收据总数", _count(snapshot, "receipts", str(len(rows)) + " 张"),
                    "来源：" + str(receipts.get("origin") or "local"), "default"),
               Card("本机已验证", _count(snapshot, "receipts", str(verified) + " 张"),
                    verify_meta, "mint" if verified else "muted")),
        table=Table(("收据号", "时间", "Tokens", "状态", "本机校验"), tuple(rows),
                    note=str(receipts.get("note") or ""),
                    empty=_empty_text(snapshot, "receipts", "还没有收据"),
                    narrow_hide=("时间", "Tokens"),
                    narrow_headers=(("本机校验", "校验"),)),
        actions=(Action("receipts.verify", "校验选中收据", enabled=bool(rows),
                        reason="没有可校验的收据"),),
        unavailable=_missing(snapshot, ("receipts",)),
        notes=("状态列是云端受理结果（accepted/rejected/expired）；「本机校验」列才是本机比对结论。",
               "哈希 / seq 用等宽字体 + 弱化颜色；证据常驻但不发光。"),
    )


def _earnings(snapshot: Snapshot) -> View:
    earnings = _section(snapshot, "earnings")
    available = snapshot.ok("earnings")
    payouts = earnings.get("payouts") if isinstance(earnings.get("payouts"), list) else []
    rows = []
    for row in payouts:
        if not isinstance(row, Mapping):
            continue
        ident = str(row.get("id") or row.get("txid") or "—")
        amount = _num(row.get("amount"))
        currency = str(row.get("currency") or "USDT")
        status = str(row.get("status") or "—")
        created = str(row.get("created_at") or "—")
        # 窄窗状态只留中文（「已记录（recorded）」在 163px 里会被切一半，原词在详情抽屉里）
        short_state = status.split("（", 1)[0] if "（" in status else status
        rows.append(Row((ident, amount, currency, status, created),
                        narrow=(_short_id(ident) or "—", amount, currency, short_state, created)))
    currency = str(earnings.get("currency") or "USDT")
    return View(
        key="earnings", title="收益",
        subtitle="按完成任务直接结算 " + currency,
        cards=(Card("今日", _money(snapshot, "earnings", earnings.get("today"), currency),
                    _today_note(earnings.get("today")) or "完成即结算", "mint"),
               Card("待结算", _money(snapshot, "earnings", earnings.get("pending")),
                    "结算中不代表到账", "amber"),
               Card("已结算", _money(snapshot, "earnings", earnings.get("settled")),
                    "链上确认后计入", "default"),
               Card("余额（可用）",
                    ("暂不可用" if earnings.get("balance_note") and
                     earnings.get("balance") is None
                     else _money(snapshot, "earnings", earnings.get("balance"))),
                    "账户预付款 · 不可提现", "muted")),
        table=Table(("明细", "金额", "币种", "状态", "时间"), tuple(rows),
                    narrow_hide=("币种", "时间"),
                    note=str(earnings.get("note") or ""),
                    empty=(UNAVAILABLE if not available else "暂无计费明细")),
        actions=(Action("earnings.export", "导出明细", enabled=bool(rows),
                        reason=("暂不可用" if not available else "没有可导出的记录")),),
        unavailable=_missing(snapshot, ("earnings",)),
        notes=tuple(filter(None, (
            "金额原样展示，不预测、不承诺收益；额度只能抵扣推理。",
            ("余额暂不可用：" + str(earnings.get("balance_note"))) if (
                earnings.get("balance_note") and earnings.get("balance") is None) else "",
        ))),
    )


def _agent(snapshot: Snapshot) -> View:
    agent = _section(snapshot, "agent")
    upstream = agent.get("upstream") if isinstance(agent.get("upstream"), Mapping) else {}
    downlink = agent.get("downlink") if isinstance(agent.get("downlink"), Mapping) else {}
    return View(
        key="agent", title="Agent 接入",
        subtitle="① 客户端 → Agent 上行   ② Agent → DJB 下行",
        cards=(Card("上行", "OpenAI + Anthropic 两路", "可编辑请求地址", "default"),
               Card("下行", "已下发" if downlink.get("url") else "未设置",
                    "只读地址 + scoped token（掩码）", "muted")),
        fields=(
            Field("上行 OpenAI", str(upstream.get("openai") or "未设置"),
                  key="openai_base_url", editable=True, hint="Agent 把请求发到这里"),
            Field("上行 Anthropic", str(upstream.get("anthropic") or "未设置"),
                  key="anthropic_base_url", editable=True, hint="Anthropic 兼容协议"),
            Field("下行 本机入口", str(downlink.get("url") or "未设置"), key="agent_downlink_url",
                  hint="只读：由服务端下发"),
            Field("下行 scoped token", str(downlink.get("token") or "—"), key="scoped_token",
                  secret=True, hint="只显示掩码，无法在此复制"),
        ),
        actions=(Action("agent.save", "保存上行地址", primary=True),),
        unavailable=_missing(snapshot, ("agent",)),
        notes=(str(agent.get("note") or "凭据不上屏、不进日志。"),),
    )


def _binding_bound(binding: Mapping[str, Any]) -> bool | None:
    """绑定态兼容：布尔 ``bound`` 优先，其次 ``state`` 字符串（active/bound/paired…）。"""
    if not binding:
        return None
    value = binding.get("bound")
    if isinstance(value, bool):
        return value
    state = str(binding.get("state") or "").strip().lower()
    if not state:
        return None
    if state in ("bound", "active", "paired", "online", "ok"):
        return True
    if state in ("unbound", "none", "detached", "revoked", "inactive"):
        return False
    return None


#: 上游验证状态 → （前缀，中文，语义色）。没登记的状态原样显示 + 「未知状态」，不猜。
_TOKEN_STATE = {
    "verified": (MARK_DONE, "已验证", "mint"),
    "declared": (MARK_WAIT, "已申报（待验证）", "amber"),
    "probing": (MARK_WAIT, "验证中", "amber"),
    "rejected": (MARK_FAIL, "验证未通过", "red"),
}

#: 空态文案（客户投诉的正是「找不到入口」，所以这句必须给出下一步而不是只说「暂无数据」）
TOKEN_PLAN_EMPTY = "你还没有接入任何 Token Plan，点下面「去接入配置」开始"


def _token_plan_rate(value: Any) -> str:
    """QPS 原样展示（整数值不带 .0）；None/缺字段给「—」，绝不补 0。"""
    if value is None or value == "" or isinstance(value, (bool, Mapping, list, tuple)):
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return str(int(number)) if number.is_integer() else str(number)


def _token_plan_rows(rows: Any) -> list[Row]:
    """上游行：``id | 接入 | 验证状态 | 模型 | 最大 QPS | 最近验证``。

    「模型」列给 ``总数（已验证 n）``——只有探针 L2 通过且新鲜、且上游启用的模型才会进容量，
    所以「总数」和「已验证」必须分开，不能把申报过的模型说成已共享。
    """
    result: list[Row] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        identifier = str(row.get("id") or "")
        state = str(row.get("state") or "")
        mark, label, tone = _TOKEN_STATE.get(
            state, (MARK_UNKNOWN, (state + "（未知状态）") if state else "状态未知", "muted"))
        if row.get("enabled") is False:
            label += " · 已停用"                     # 停用 = 不公告，别让客户以为还在共享
            tone = "muted"
        total, verified = row.get("model_count"), row.get("verified_models")
        models = _num(total) + " 个" if isinstance(total, int) else "—"
        if isinstance(verified, int) and isinstance(total, int):
            models += "（已验证 " + str(verified) + "）"
        last = str(row.get("last_probe_at") or "—")
        result.append(Row((_short_id(identifier, 12) or "—", str(row.get("access_type") or "—"),
                           mark + " " + label, models, _token_plan_rate(row.get("max_qps")),
                           last),
                          key=identifier, tone=tone,
                          narrow=(_short_id(identifier, 6) or "—",
                                  str(row.get("access_type") or "—"),
                                  mark + " " + label.split("（")[0], models.split("（")[0],
                                  _token_plan_rate(row.get("max_qps")), last)))
    return result


def _token_plan_usage(plan: Mapping[str, Any], window: str) -> Mapping[str, Any]:
    """限额窗口（daily/weekly/monthly）的 ``used / limit``；缺了就是 {}（不编 0）。"""
    for item in plan.get("usage") or []:
        if isinstance(item, Mapping) and str(item.get("name") or "") == window:
            return item
    return {}


def _token_plan(snapshot: Snapshot) -> View:
    """共享 Token Plan：共享状态 + 我的上游 + 预算 + 今日用量 + 一个去配置的入口。"""
    plan = _section(snapshot, "token_plan")
    access = _section(snapshot, "access")
    config = access.get("config") if isinstance(access.get("config"), Mapping) else {}
    binding = access.get("binding") if isinstance(access.get("binding"), Mapping) else {}
    account = _section(snapshot, "account")
    profile = account.get("profile") if isinstance(account.get("profile"), Mapping) else {}
    security = account.get("security") if isinstance(account.get("security"), Mapping) else {}
    status = _section(snapshot, "status")
    receipts = _section(snapshot, "receipts")
    receipt_rows = [row for row in (receipts.get("rows") or []) if isinstance(row, Mapping)]
    # 绑定态直接复用账户/接入页的判定（服务端 binding → profile.bound → 本机凭据）
    bound = _account_bound(profile, security, binding)
    online = str(status.get("state") or "") == "online"
    state = str(status.get("state") or "")
    state_text, state_tone = (
        (MARK_DONE + " 已上线", "mint") if online else
        (MARK_WAIT + " 启动中", "amber") if state in ("starting", "updating") else
        (MARK_WAIT + " 已暂停", "amber") if state == "paused" else
        (MARK_IDLE + " 未运行", "muted") if state == "stopped" else
        (MARK_FAIL + " 错误", "red") if state == "error" else
        (MARK_UNKNOWN + " 状态未知", "muted"))
    if not snapshot.ok("status"):
        state_text, state_tone = MARK_UNKNOWN + " 暂不可用", "muted"
    bound_text, bound_tone = (
        (_flag(True, "已绑定", "未绑定"), "mint") if bound is True else
        (_flag(False, "已绑定", "未绑定"), "amber") if bound is False else
        (_flag(None, "已绑定", "未绑定"), "muted"))
    node_id = str(profile.get("node_id") or binding.get("node_id")
                  or config.get("node_id") or "—")
    rows = _token_plan_rows(plan.get("upstreams"))
    verified_upstreams = sum(1 for row in (plan.get("upstreams") or [])
                             if isinstance(row, Mapping)
                             and str(row.get("state") or "") == "verified")
    limits = plan.get("limits") if isinstance(plan.get("limits"), Mapping) else {}
    daily = _token_plan_usage(plan, "daily")
    used = daily.get("used")
    daily_limit = daily.get("limit", limits.get("daily_tokens"))
    budget = ("并发 " + _num(limits.get("concurrent")) + " · 每日 "
              + _num(limits.get("daily_tokens")) + " tokens")
    budget_meta = "周 " + (str(limits.get("weekly_tokens")) if limits.get("weekly_tokens")
                          else "未启用") + " · 月 " + (
        str(limits.get("monthly_tokens")) if limits.get("monthly_tokens") else "未启用")
    if not snapshot.ok("token_plan"):
        # 限额读不到时不许说「周/月未启用」—— 那是把缺数据编成配置结论
        budget, budget_meta = UNAVAILABLE, ""
    shared_value = (_num(used) + " tokens") if snapshot.ok("token_plan") else UNAVAILABLE
    shared_meta = ((str(len(receipt_rows)) + " 张收据 · 今日上限 " + _num(daily_limit))
                   if snapshot.ok("receipts") else UNAVAILABLE)
    notes: list[str] = []
    if bound is False:
        notes.append("节点还没绑定：先在「接入配置」用网页端一次性绑定码完成绑定，"
                     "再把 Token Plan 共享出去。")
    if snapshot.ok("token_plan") and not rows:
        notes.append(TOKEN_PLAN_EMPTY + "（A1 直填 Key / A2 本机代理，都在「接入配置」里）。")
    notes.append("验证状态来自本机探针（L2 推理通过 + 结论新鲜）；"
                 "未验证 / 已过期的模型不进容量，也不会产生共享收益。")
    notes.append("「最大 QPS」是本机按并发上限分摊给已验证模型的数值（公告口径），不是实测吞吐。")
    if not (snapshot.ok("access") and snapshot.ok("account")):
        notes.append("接入配置 / 账户数据暂不可用：绑定状态暂时无法确认，点「刷新」重试。")
    if plan.get("note"):
        notes.append(str(plan.get("note")))
    return View(
        key="token_plan", title="共享 Token Plan",
        subtitle="节点与绑定 · 我的上游 · 共享预算 · 今日用量",
        cards=(
            Card("共享状态", state_text, bound_text + " · 节点 " + node_id,
                 "amber" if bound is False else state_tone),
            Card("我的 Token Plan（上游）",
                 _count(snapshot, "token_plan", str(len(rows)) + " 条上游"),
                 (("已验证 " + str(verified_upstreams) + " 条 · 详见下表") if rows
                  else "点下面「去接入配置」添加上游") if snapshot.ok("token_plan") else "",
                 "mint" if rows else "amber"),
            Card("共享预算", budget, budget_meta + "（本机限额设置）", "default"),
            Card("今日已共享", shared_value, shared_meta, "mint" if used else "muted"),
        ),
        table=Table(("上游", "接入", "验证状态", "模型", "最大 QPS", "最近验证"), tuple(rows),
                    narrow_hide=("最近验证",),
                    note="验证通过且新鲜的模型才会进入共享容量",
                    empty=(TOKEN_PLAN_EMPTY if snapshot.ok("token_plan") else UNAVAILABLE)),
        actions=(Action("token_plan.open_access", "去接入配置（添加上游 / 绑定节点）", primary=True),
                 Action("token_plan.refresh", "刷新")),
        unavailable=_missing(snapshot, ("token_plan",)),
        notes=tuple(notes),
    )


def _access(snapshot: Snapshot) -> View:
    access = _section(snapshot, "access")
    config = access.get("config") if isinstance(access.get("config"), Mapping) else {}
    binding = access.get("binding") if isinstance(access.get("binding"), Mapping) else {}
    bound = _binding_bound(binding)
    valid = access.get("valid")
    return View(
        key="access", title="接入配置",
        subtitle="保存 → daemon reload → 回读一致",
        cards=(Card("配置有效性",
                    "尚未保存" if access.get("saved") is False
                    else _flag(valid, "通过校验", "有字段需修正"), "",
                    "amber" if access.get("saved") is False or not valid else "mint"),
               Card("节点绑定", _flag(bound if bound is not None else None, "已绑定", "未绑定"),
                    str(binding.get("node_id") or config.get("node_id") or "—"), "default")),
        fields=(
            Field("节点标识", str(config.get("node_id") or "—"), key="node_id", editable=True),
            Field("节点凭证", str(config.get("node_secret") or "—"), key="node_secret",
                  editable=True, secret=True, hint="掩码显示；粘贴新值才会覆盖"),
            Field("API Key", str(config.get("api_key") or "—"), key="api_key",
                  editable=True, secret=True, hint="djb_sk_… 形式，只显示尾 4 位"),
            Field("价格模式", str(config.get("price_mode") or "—"), key="price_mode",
                  editable=True,
                  hint="auto / manual；当前：" + str(config.get("price_mode_label") or "—")),
            Field("USDT 收款地址", str(config.get("usdt_address") or "—"), key="usdt_address",
                  editable=True, hint="TRC20（T 开头 34 位）或 EVM（0x + 40 位）"),
            Field("OpenAI 请求地址", str(config.get("openai_base_url") or "未设置"),
                  key="openai_base_url", editable=True),
            Field("Anthropic 请求地址", str(config.get("anthropic_base_url") or "未设置"),
                  key="anthropic_base_url", editable=True),
        ),
        actions=(Action("access.save", "保存并回读", primary=True),
                 Action("access.reload", "请求 daemon 重载")),
        unavailable=_missing(snapshot, ("access",)),
        notes=(str(access.get("note") or ""),
               "配置文件：" + str(access.get("path") or "—") + "（0600，只在本机）"),
    )


def _today_note(today: Any) -> str:
    """「今日」按 UTC 日界（服务端口径）；本地时区跨日时数字会看起来对不上，得写明。"""
    if today in (None, ""):
        return "服务端未下发今日字段；不拿总计冒充今日"
    return "服务端下发值（UTC 日界）；本机不换算"


def _yield_state_text(state: str) -> str:
    """让路页的状态卡：没跑就别说「运行中」（本地状态词只有 stopped/paused/online…）。"""
    if state == "paused":
        return MARK_WAIT + " 已暂停"
    if state in ("online", "running", "full", "balanced", "saver"):
        return MARK_DONE + " 运行中"
    if state == "stopped":
        return MARK_IDLE + " 已停止"
    return MARK_UNKNOWN + " 状态未知（" + (state or "未知") + "）"


def _yield_state_tone(state: str) -> str:
    if state == "paused":
        return "amber"
    if state in ("online", "running", "full", "balanced", "saver"):
        return "mint"
    return "muted"


def _yield(snapshot: Snapshot) -> View:
    data = _section(snapshot, "yield")
    state = str(data.get("state") or "unknown")
    paused = state == "paused"
    reason = data.get("reason_label") or data.get("reason")
    mode = str(data.get("mode") or "unknown")
    modes = (("full", "全速", "只要接单就满载"),
             ("balanced", "均衡", "工作时段实时算力，其余慢任务"),
             ("saver", "省心", "只在我不使用电脑时工作"),
             ("paused", "暂停", "不接新任务；在途任务按护栏处理"))
    active = mode if mode != "unknown" else ("paused" if paused else "")
    rows = tuple(Row((label, key, summary,
                      (MARK_DONE + " 当前") if key == active else MARK_IDLE), key=key)
                 for key, label, summary in modes)
    return View(
        key="yield", title="让路 / 节能",
        subtitle="四档模式 · 暂停后与托盘状态一致",
        cards=(Card("当前状态", _yield_state_text(state),
                    str(reason or data.get("reason_label") or "无让路原因"),
                    _yield_state_tone(state)),
               Card("接单", _flag(data.get("accepting"), "允许", "禁止"), "", "default"),
               Card("模式", mode, "本机配置", "muted")),
        table=Table(("模式", "键", "说明", "当前"), rows, empty="模式暂不可用"),
        actions=(Action("yield.pause", "暂停接单", enabled=not paused,
                        reason="已经是暂停状态"),
                 Action("yield.resume", "恢复接单", enabled=paused,
                        reason="当前不是暂停状态", primary=True)),
        unavailable=_missing(snapshot, ("yield",)),
        notes=("暂停只停止接新任务，不停止服务；托盘与窗口读同一份状态。",),
    )


def _account_bound(profile: Mapping[str, Any], security: Mapping[str, Any],
                   binding: Mapping[str, Any]) -> bool | None:
    """账户页的绑定态：服务端 binding 优先，其次 profile.bound，最后看本机有没有节点凭据。

    ``binding.state`` 是网页端/云端口径（bound / active / unbound…）；本机没登录时
    两者都缺，按「未绑定」处理，这样「绑定网页端」入口才会出现在新用户眼前。
    """
    server = _binding_bound(binding) if isinstance(binding, Mapping) else None
    if server is not None:
        return server
    value = profile.get("bound")
    if isinstance(value, bool):
        return value
    if profile.get("node_id") or security.get("node_id"):
        return True
    return False if (profile or security) else None


def _account(snapshot: Snapshot) -> View:
    account = _section(snapshot, "account")
    profile = account.get("profile") if isinstance(account.get("profile"), Mapping) else {}
    security = account.get("security") if isinstance(account.get("security"), Mapping) else {}
    access = _section(snapshot, "access")
    binding = access.get("binding") if isinstance(access.get("binding"), Mapping) else {}
    unavailable = _missing(snapshot, ("account",))
    # 节点侧接口给的是**节点身份**（node_id/state/region/platform/bound），
    # 旧/演示形状给的是账户（email/role/2FA）；两种都如实展示，缺的字段给「—」。
    node_id = profile.get("node_id")
    email = profile.get("email")
    identity = str(node_id or email or "—")
    identity_meta = str(profile.get("state") or profile.get("role") or "—")
    bound = _account_bound(profile, security, binding)
    can_bind = bound is not True and not unavailable
    twofa = profile.get("twofa_enabled", profile.get("mfa_enabled"))
    if bound is not None:
        second = Card("节点绑定", _flag(bound, "已绑定", "未绑定"),
                      "服务端视图（只读）", "mint" if bound is True else "amber")
    else:
        second = Card("双因素验证", _flag(twofa, "已开启", "未开启", "暂不可用"),
                      "登录保护的第二步", "mint" if twofa is True else "amber")
    if node_id:
        fields: list[Field] = [Field("节点标识", identity, key="node_id",
                                     hint="节点身份由服务端下发，本机只读")]
    else:
        fields = [Field("邮箱", identity, key="email", hint="登录后在服务端修改")]
    if node_id:
        fields.extend([
            Field("节点状态", str(profile.get("state") or "—"), key="state"),
            Field("地域", str(profile.get("region") or "—"), key="region"),
            Field("平台", str(profile.get("platform") or "—"), key="platform"),
            Field("最后在线", str(profile.get("last_seen_at") or "—"), key="last_seen_at"),
        ])
    if node_id and email:
        fields.append(Field("邮箱", str(email), key="email", hint="登录后在服务端修改"))
    if profile.get("role"):
        fields.append(Field("角色", str(profile.get("role") or "—"), key="role"))
    if twofa is not None:
        fields.append(Field("2FA 状态", _flag(twofa, "已开启", "未开启"), key="twofa"))
    if can_bind:
        fields.append(Field("网页端", facts.WEB_CONSOLE_URL, key="web_console",
                            hint="在网页端 → 账户/节点 → 生成绑定码，然后粘贴到这里"))
    fields.append(Field("会话", "本机窗口", key="session", hint="关闭窗口不会退出登录"))
    actions: list[Action] = []
    if can_bind:
        actions.append(Action("account.bind", "绑定网页端", primary=True))
    actions.append(Action("account.enable_2fa", "启用双因素验证",
                          enabled=twofa is not True,
                          reason="已开启" if twofa is True else "登录后可用",
                          primary=not can_bind and twofa is not True))
    actions.append(Action("account.logout", "登出", enabled=bool(security.get("node_id")),
                          reason=("尚未登录" if not node_id
                                  else "本机无凭据（登出要清本机密钥）")))
    if can_bind:
        actions.append(Action("account.open_web", "打开网页端"))
    notes = [str(account.get("note") or "未登录时只显示本机信息。")]
    if can_bind:
        notes = [
            "绑定：在网页端 → 账户/节点 → 生成绑定码，然后点「绑定网页端」粘贴到这里"
            "（或 djb auth --bind <码>）",
            "网页端：" + facts.WEB_CONSOLE_URL,
        ] + notes
    return View(
        key="account", title="账户与安全",
        subtitle="节点身份 · 绑定 · 双因素 · 登出",
        cards=(Card("账户", identity, identity_meta, "default"), second,
               Card("本机凭据", str(security.get("storage") or "—"),
                    str(security.get("node_id") or "本机未保存"), "muted")),
        fields=tuple(fields),
        actions=tuple(actions),
        unavailable=unavailable,
        notes=tuple(notes),
    )


def _logs(snapshot: Snapshot) -> View:
    logs = _section(snapshot, "logs")
    rows = []
    for row in logs.get("rows") if isinstance(logs.get("rows"), list) else []:
        if not isinstance(row, Mapping):
            continue
        level = str(row.get("level") or "unknown")
        mark = (MARK_FAIL if level in ("error", "critical") else
                MARK_WAIT if level == "warning" else
                MARK_UNKNOWN if level == "unknown" else MARK_IDLE)
        tone = ("red" if level in ("error", "critical") else
                "amber" if level == "warning" else
                "muted" if level == "unknown" else "default")
        rows.append(Row((mark + " " + level, str(row.get("ts") or "—"),
                         str(row.get("event") or "—")), key=str(row.get("ts") or ""),
                        tone=tone))
    return View(
        key="logs", title="日志",
        subtitle="本地 ring buffer（最近 2000 行，按需筛选）",
        cards=(Card("行数", _count(snapshot, "logs", str(len(rows))), "尾部 200 行", "default"),),
        table=Table(("级别", "时间", "事件"), tuple(rows),
                    note=str(logs.get("note") or ""),
                    empty=_empty_text(snapshot, "logs", "日志文件尚未生成")),
        actions=(Action("logs.copy", "复制当前筛选", enabled=bool(rows), reason="没有日志行"),
                 Action("logs.open_dir", "打开日志目录")),
        unavailable=_missing(snapshot, ("logs",)),
        notes=("级别筛选：all / debug / info / warning / error；不改日志文件。",),
    )


#: 数据域 → 构建器
_BUILDERS = {
    "overview": _overview, "bench": _bench, "models": _models, "gpu": _gpu,
    "tasks": _tasks, "receipts": _receipts, "earnings": _earnings, "agent": _agent,
    "token_plan": _token_plan, "access": _access, "yield": _yield, "account": _account,
    "logs": _logs,
}


def build_view(key: str, snapshot: Snapshot) -> View:
    """构建单个视图；未知 key 抛 ``ValueError``（拼错不静默给空页）。"""
    builder = _BUILDERS.get(str(key or ""))
    if builder is None:
        raise ValueError("未知视图：" + str(key))
    return builder(snapshot)


def build_views(snapshot: Snapshot) -> dict[str, View]:
    """一次构建 13 个视图（顺序同 :data:`VIEWS`）。"""
    return {key: _BUILDERS[key](snapshot) for key in VIEW_KEYS}
