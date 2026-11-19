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

import hashlib
import re

from dataclasses import dataclass, field, fields as dataclass_fields, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from . import payout as payout_mod
from .access import mask_address
from .datasource import Snapshot, UNAVAILABLE, Unavailable
from .nodecloud import STATE_TEXT
from .. import facts

__all__ = [
    "Action", "Card", "Chart", "Field", "GaugeData", "Hero", "Mark", "Metric", "NAV_SECTIONS",
    "NavSection", "PAGE_MICROS", "RailCard", "Row", "SECTION_FOR_VIEW", "Table", "Toggle",
    "VIEWS",
    "VIEW_KEYS", "VIEW_TITLES", "View", "build_view", "build_views", "page_kicker",
    "YIELD_PRESET_BY_KEY", "YIELD_PRESET_BY_LABEL", "YIELD_TIERS",
    "Filter", "with_field_errors",
]

#: 三冗余状态前缀（文字通道；颜色通道由 tone 决定）
MARK_DONE = "✓"
MARK_WAIT = "◐"
MARK_FAIL = "✕"
MARK_UNKNOWN = "?"
MARK_IDLE = "·"

#: 上次更新检查的结果（``Store.update_state().verify_state``）→ 客户能看懂的一句话。
#:
#: 九状态全覆盖（``update.STATE_GUIDANCE`` 是权威表，这里只是**短标签**用于表格单元格；
#: 详细「下一步」由 toast 用的 ``update.guidance()`` 给）。缺失的状态退回显示状态码，
#: 绝不显示成「已是最新」。
UPDATE_STATE_TEXT = {
    "offline": "网络不通，没拿到清单",
    "invalid": "更新源异常，不要安装",
    "no_source": "本渠道暂无更新源",
    "failed_verify": "校验失败，已拒装",
    "channel_mismatch": "清单渠道与本机不一致",
    "up_to_date": "已是最新",
    "not_eligible": "灰度未命中，本轮不更新",
    "no_artifact": "本平台暂无安装包",
    "eligible": "有可用更新",
}


class Mark:
    """状态前缀常量（类只是命名空间，避免到处写散字符串）。"""

    DONE = MARK_DONE
    WAIT = MARK_WAIT
    FAIL = MARK_FAIL
    UNKNOWN = MARK_UNKNOWN
    IDLE = MARK_IDLE


#: 每个视图的侧栏标题（**语义**，与顺序无关；13 项一个不少、一个不并）
#:
#: 「共享 Token Plan」（键名 ``token_plan``，1.8.1 契约线新增）回应「客户找不到入口」：
#: 它**聚合**已有数据域（access/account 的绑定态 + 上游清单 + 限额预算 + 今日用量），
#: 不新造后端接口；放在「接入配置」之前，客户第一眼就能看到「共享了什么、还能共享多少」，
#: 主操作一步跳到「接入配置」去加第一条上游。
VIEW_TITLES: dict[str, str] = {
    "overview": "总览",
    "bench": "性能基准",
    "gpu": "GPU 监控",
    "models": "模型与探针",
    "tasks": "任务队列",
    "receipts": "收据",
    "earnings": "收益",
    "token_plan": "提供 Token Plan",
    "agent": "Agent 接入",
    "access": "本机设置",
    "yield": "让路 / 节能",
    "account": "账户与安全",
    "logs": "日志",
}


@dataclass(frozen=True)
class NavSection:
    """侧栏分组（IA 的中间层）：一个使用场景 = 一段连续视图。

    **这是 0.3.4 的 4 组基线**：0.3.5 起侧栏由 :mod:`djb_node.desktop.ia` 驱动
    （``DJB_UI_IA=legacy`` 才回到本表），此处仅作回滚/对照保留，不要再往这里加视图。
    """

    #: 组 key（英文小写，给测试/样式用）
    key: str
    #: 中文组名（侧栏分组头的主文案）
    title: str
    #: 拉丁微标（分组头的 mono 小字；只放 ASCII，别让中文进 mono）
    micro: str
    #: 组成员（顺序即侧栏顺序；必须恰好覆盖一次全部视图）
    keys: tuple[str, ...]


#: 侧栏 4 组——按**用户旅程**排，不按后端模块排：
#:
#: 运行 RUN     先看「这台机器现在怎么样」（总览 → 基准 → 算力/模型 → 任务）
#: 收益 EARN    再看「跑出来的东西值多少钱」（收据 → 收益 → 共享 Token Plan 预算）
#: 接入 CONNECT 想扩大供给才需要（Agent 接入 → 接入配置）
#: 系统 SYSTEM  平时不动、出事才找（让路节能 → 账户与安全 → 日志）
#:
#: 约束：13 项不删不并；``token_plan`` 必须仍在 ``access`` 之前（既有产品语义），
#: 因此它落在「收益」组末尾——先看到「共享了什么、还能共享多少」，再进「接入」动手。
NAV_SECTIONS: tuple[NavSection, ...] = (
    NavSection("run", "运行", "RUN",
               ("overview", "bench", "gpu", "models", "tasks")),
    NavSection("earn", "收益", "EARN",
               ("receipts", "earnings", "token_plan")),
    NavSection("connect", "接入", "CONNECT",
               ("agent", "access")),
    NavSection("system", "系统", "SYSTEM",
               ("yield", "account", "logs")),
)

#: 视图 → 所属分组（渲染层查面包屑/分组头用；未知 key 直接 KeyError，不猜）
SECTION_FOR_VIEW: dict[str, NavSection] = {
    key: section for section in NAV_SECTIONS for key in section.keys
}

#: 13 视图（key, 侧栏标题）——顺序即 IA 顺序（由 :data:`NAV_SECTIONS` 展平而来）
VIEWS: tuple[tuple[str, str], ...] = tuple(
    (key, VIEW_TITLES[key]) for section in NAV_SECTIONS for key in section.keys
)
VIEW_KEYS: tuple[str, ...] = tuple(key for key, _ in VIEWS)

#: 页头眉标的**页名**部分（拉丁；纯 ASCII，可交给 mono 字族）
PAGE_MICROS: dict[str, str] = {
    "overview": "OVERVIEW",
    "bench": "BENCH",
    "gpu": "GPU",
    "models": "MODELS",
    "tasks": "TASKS",
    "receipts": "RECEIPTS",
    "earnings": "EARNINGS",
    "token_plan": "TOKEN PLAN",
    "agent": "AGENT",
    "access": "ACCESS",
    "yield": "YIELD",
    "account": "ACCOUNT",
    "logs": "LOGS",
}


def page_kicker(key: str) -> str:
    """页头面包屑眉标：``组名 / 页名`` 两级（如 ``RUN / OVERVIEW``）。

    为什么是两级：页头标题（中文 18px）只说「是哪一页」，眉标补上「在哪一组」，
    侧栏收起（窄窗下拉）时也不会失去位置感。整串是 ASCII，走 mono 不会跳基线。
    """
    return SECTION_FOR_VIEW[key].micro + " / " + PAGE_MICROS[key]


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
    #: 表格自称的名字（读屏进表格时念的第一句）。空 = 用「表格」兜底。
    #: 压平后行是 ` | ` 拼接，这个名字 + 列序 + 总行数是唯一能补回列语义的地方。
    title: str = ""
    note: str = ""
    empty: str = "暂无数据"
    #: 窄窗（E10 口径 390）下先让位的列；数据不丢，详情抽屉里仍是全字段
    narrow_hide: tuple[str, ...] = ()
    #: 窄窗下的表头短名（只改表头，不改数据）
    narrow_headers: tuple[tuple[str, str], ...] = ()
    #: RC15-039 #17：回车/双击**激活**这一行时派发的动作（``""`` = 老行为：只看详情）。
    #: 只给「行本身就是一个可执行选择」的表用（让路页的三档就是）；收据/日志这类
    #: 行不是动作，保持默认空，回车仍然只是打开详情。
    activate: str = ""


@dataclass(frozen=True)
class Field:
    """一行「标签 / 值」；``secret`` 表示只显示掩码，``editable`` 表示可编辑输入。

    两个只影响**渲染**的开关（默认值 = 老行为，老调用方无需改）：

    * ``advanced``：折叠进「高级（排障用）」区。用于「必须保留但客户平时不该动」的字段
      （节点凭证 = 节点密钥，排障要用，可它绝不该占首屏）。
    * ``prefill``：可编辑框是否预填 ``value``。``False`` 时输入框留空、``value`` 进 hint——
      掩码值（如收款地址 ``TQn9Xa…7f3d``）若预填进输入框，客户直接点「保存并回读」就会把
      掩码当地址写回配置（真机上会被 ``validate_address`` 拒，等于保存永远失败）。
    * ``revealable``：密码框右侧给不给「显示 / 隐藏」按钮。``None``（默认）= 跟着
      ``secret`` 走（**秘密字段默认可显示**）；显式 ``False`` 才关掉。理由：客户粘贴
      长密钥时打错一个字符，保存后只看到「401 / 密钥被拒」，而输入框里是一串黑点，
      除了整段重粘没有任何补救手段（RC14 客户反馈「看不见就改不了」）。
      显示**只改输入框的 ``show`` 选项**：不复制到剪贴板、不写日志、不进快照。
    """

    label: str
    value: str
    key: str = ""
    editable: bool = False
    secret: bool = False
    hint: str = ""
    tone: str = "default"
    advanced: bool = False
    prefill: bool = True
    revealable: bool | None = None
    #: 非空 ⇒ 渲染成**只读下拉**（``ttk.Combobox`` + ``state="readonly"``）。
    #: 只影响渲染；校验仍在动作侧（下拉选不出非法值，但手改配置/老快照可能带别的值）。
    choices: tuple[str, ...] = ()
    #: 字段级错误（一句话）。非空 ⇒ 红字渲染在**该字段下方**并参与「聚焦首个错误」
    #: （RC15-039 #1）。默认空 = 0.3.7 行为；错误不由投影层产生，由动作层回投
    #: （见 :func:`with_field_errors`），所以 13 个视图的既有快照一字不变。
    error: str = ""
    #: 字段分区标题（2026-11-19 客户报障「账户页太拥挤，绑定节点与 USDT 地址要完全隔离」）。
    #: 空串 = 老行为（整页一个字段块）。非空时渲染层按分区插标题、**每组各成一块**
    #: （各自的字段 + 各自的动作按钮），于是「填地址」和「点绑定」不再在同一屏同一条按钮栏里。
    group: str = ""

    def can_reveal(self) -> bool:
        """是否画「显示 / 隐藏」：只有**可编辑**的秘密字段才有意义（只读值本就是掩码）。"""
        if not (self.editable and self.secret):
            return False
        return True if self.revealable is None else bool(self.revealable)


@dataclass(frozen=True)
class Filter:
    """表格上方的一条下拉筛选（RC15-039 #5：日志级别筛选的**真**入口）。

    为什么单独一个类型而不是复用 ``Field``：``Field`` 会被 ``app._on_action`` 收成草稿
    交给「保存配置」，而日志筛选是**本机只读视图状态**——混进草稿会写出「草稿字段不被支持」
    的告警，甚至把 `log_level` 写进接入配置。筛选有自己的动作 key（``action``）。
    """

    key: str
    label: str
    #: (值, 显示文案)：值是内部契约（``all`` / ``debug`` …），文案是客户看到的
    options: tuple[tuple[str, str], ...] = ()
    value: str = ""
    #: 选中后派发的动作 key；动作侧从草稿 ``key`` 读新值
    action: str = ""
    #: 一句话说明「筛选影响什么、不影响什么」
    hint: str = ""

    def current_text(self) -> str:
        for value, text in self.options:
            if value == self.value:
                return text
        return self.value or "—"


@dataclass(frozen=True)
class Toggle:
    """一条开关（RC15-039 #4：自动刷新在「本机设置」里可以关掉）。

    值来自 controller 的**本机界面状态**（不是服务端配置），投影层只负责说明它是什么。
    """

    key: str
    label: str
    value: bool = True
    hint: str = ""
    #: 选中后派发的动作 key（把值写成草稿再派发，与 ``Filter`` 同一条路）
    action: str = ""

    def text(self) -> str:
        return "开" if self.value else "关"


@dataclass(frozen=True)
class Action:
    """可点动作；``enabled=False`` 时按钮置灰并显示 ``reason``（不隐藏）。

    ``nav`` 非空表示这是**页面内跳转**（不发任何后端请求），渲染层直接 ``show_view(nav)``；
    这样「调整时段」这类入口也能留在投影层里被断言，而不是散在 Tk 代码里。
    """

    key: str
    label: str
    enabled: bool = True
    reason: str = ""
    primary: bool = False
    nav: str = ""
    #: 动作分组标题（D6，2026-11-18 客户要求「绑定节点」与「提交 USDT 地址」隔离成两块）。
    #: 空串 = 不分组（0.3.11 行为：整页一条按钮栏）。渲染层按「分组变化」插一条小标题 +
    #: 另起一行按钮，于是同一页也能有两个互不干扰的动作区。
    group: str = ""


#: D6（2026-11-18 客户要求）：账户页两个**互不干扰**的动作区标题。
#: 「绑定节点」= 设备码/手输码；「提交 USDT 收款地址」= 单独一次 Payout 提交。
#:
#: 2026-11-19 客户再次报障「绑定页面设置 UI 太拥挤，把绑定节点和 USDT 地址完全隔离开」：
#: 只给动作按钮加小标题不够——字段还挤在同一块里。现在**字段也带 group**，渲染层按分区
#: 各成一块（标题 → 该组字段 → 该组按钮），绑定区与收款区之间不再共享任何一块版面。
GROUP_IDENTITY = "① 本机与身份"
GROUP_BIND = "② 绑定节点"
GROUP_PAYOUT = "③ 提交 USDT 收款地址（与绑定是两件事）"


def action_groups(actions: Sequence[Action]) -> list[tuple[str, list[Action]]]:
    """按 ``Action.group`` 把动作切成有序块（同组相邻的合在一块；空组名不插标题）。

    D6：客户要求「绑定节点」和「提交 USDT 地址」是两个**互不干扰**的动作。此前整页
    只有一条按钮栏，用户填完地址顺手点了「绑定网页端」，就会误以为地址一起提交了。
    """
    groups: list[tuple[str, list[Action]]] = []
    for action in actions:
        title = str(getattr(action, "group", "") or "")
        if groups and groups[-1][0] == title:
            groups[-1][1].append(action)
        else:
            groups.append((title, [action]))
    return groups


@dataclass(frozen=True)
class Metric:
    """一个指标格：微标 + 值 + 单位 + 说明（状态卡四指标 / TODAY 四宫格共用）。"""

    label: str
    value: str
    unit: str = ""
    note: str = ""


@dataclass(frozen=True)
class GaugeData:
    """仪表环投影。``ratio=None`` = **没有数字**：只画轨道，中心显示 ``value``（通常是「—」）。

    这里绝不把「没有数据」折算成 0——0% 和「没有数据」在仪表盘上是两件事。
    """

    ratio: float | None = None
    value: str = "—"
    label: str = ""


@dataclass(frozen=True)
class Hero:
    """状态主卡（只有 overview 填）：标题 + 徽标 + 说明 + 运行时长 + 仪表环 + 四指标 + 动作。"""

    kicker: str
    title: str
    chip: str = ""
    chip_tone: str = "muted"
    body: str = ""
    runtime_label: str = "连续贡献"
    runtime: str = "—"
    gauge: GaugeData | None = None
    metrics: tuple[Metric, ...] = ()
    actions: tuple[Action, ...] = ()
    hint: str = ""
    note: str = ""


@dataclass(frozen=True)
class Chart:
    """时序柱图投影（只有 overview 填）。

    ``values`` 是 0..1 的**归一化高度**；``empty`` 非空 = 没有可用时序，
    渲染层据此画空态并只显示 ``empty`` 这句话（不画假柱子）。
    """

    kicker: str = ""
    title: str = ""
    caption: str = ""
    values: tuple[float, ...] = ()
    labels: tuple[str, ...] = ()
    peak: str = ""
    foot: str = ""
    empty: str = ""


@dataclass(frozen=True)
class RailCard:
    """右栏卡（只有 overview 填）。

    ``kind``：``setup``（只读字段 + 跳转）/ ``stats``（四宫格）/ ``throttle``（标签-值行 + 开关）。
    """

    kicker: str
    title: str
    kind: str = "stats"
    badge: str = ""
    badge_tone: str = "muted"
    note: str = ""
    stats: tuple[Metric, ...] = ()
    fields: tuple[Field, ...] = ()
    rows: tuple[tuple[str, str, str], ...] = ()
    toggle: tuple[str, bool] | None = None
    progress: tuple[str, "float | None", str] | None = None   # (标签, 比例, 值文案)
    actions: tuple[Action, ...] = ()
    foot: str = ""


@dataclass(frozen=True)
class View:
    key: str
    title: str
    subtitle: str = ""
    cards: tuple[Card, ...] = ()
    table: Table | None = None
    #: 附加表（RC14 D：模型与探针页要同时显示「探针事实」和「云端目录」两张表）。
    #: 只加不改：``table`` 仍是主表，渲染顺序 = 主表 → 附加表；空元组时行为与 0.3.6 完全一致。
    tables: tuple[Table, ...] = ()
    fields: tuple[Field, ...] = ()
    actions: tuple[Action, ...] = ()
    notes: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    #: 设计样新增的三块（只有 overview 填；其余 12 页走通用卡片+表格渲染）
    hero: Hero | None = None
    chart: Chart | None = None
    rail: tuple[RailCard, ...] = ()
    #: 表格上方的下拉筛选（RC15-039 #5）。默认空 = 0.3.7 行为（没有筛选控件）。
    filters: tuple[Filter, ...] = ()
    #: 开关（RC15-039 #4）。默认空 = 0.3.7 行为（页面上没有开关）。
    switches: tuple[Toggle, ...] = ()
    #: 渲染后要**获得键盘焦点**的字段 key（RC15-039 #1：校验失败聚焦首个错误字段）。
    #: 空 = 不动焦点（0.3.7 行为）。由 :func:`with_field_errors` 填，投影层自己不产生。
    focus_field: str = ""

    def plain_text(self) -> str:
        """文本快照（证据 / 无显示环境验收）：稳定顺序，无时间戳抖动。

        ``hero`` / ``chart`` / ``rail`` 也写进来：设计改造后视觉信息变多，
        但**说出来的每句话都必须可断言**，所以主卡、柱图、右栏同样落成文本。
        """
        lines = ["# " + self.title + (("  ·  " + self.subtitle) if self.subtitle else "")]
        for item in self.unavailable:
            lines.append("! " + item)
        if self.cards:
            lines.append("")
            for card in self.cards:
                suffix = ("  " + card.meta) if card.meta else ""
                lines.append("[" + card.title + "] " + card.value + suffix)
        if self.hero is not None:
            lines.extend(_hero_text(self.hero))
        if self.chart is not None:
            lines.extend(_chart_text(self.chart))
        if self.rail:
            lines.extend(_rail_text(self.rail))
        for item in self.switches:
            lines.append("[" + item.label + "：" + item.text() + "]"
                         + (("   " + item.hint) if item.hint else ""))
        for item in self.filters:
            lines.append("")
            lines.append("筛选 " + item.label + "：" + item.current_text()
                         + (("   " + item.hint) if item.hint else ""))
        for table in ((self.table,) + self.tables if self.table is not None else self.tables):
            lines.append("")
            lines.append(" | ".join(table.columns))
            if not table.rows:
                lines.append("（" + table.empty + "）")
            for row in table.rows:
                prefix = "x " if row.disabled else "- "
                line = prefix + " | ".join(row.cells)
                if row.note:
                    line += "   " + row.note
                lines.append(line)
            if table.note:
                lines.append("… " + table.note)
        if self.fields:
            lines.append("")
            for item in self.fields:
                line = item.label + "：" + item.value
                if item.hint:
                    line += "   " + item.hint
                if item.error:
                    # 红字的文本形态：与屏幕上同一条文案（证据/读屏都靠它）
                    line += "   ！" + item.error
                lines.append(line)
        if self.actions:
            lines.append("")
            for action in self.actions:
                state = "" if action.enabled else ("（置灰：" + (action.reason or "当前不可用") + "）")
                lines.append("[" + action.label + "]" + state)
        for note in self.notes:
            lines.append("· " + note)
        return "\n".join(lines)


def with_field_errors(view: View, errors: Mapping[str, str] | None) -> View:
    """把动作层回投的**字段级错误**贴到视图上（RC15-039 #1）。

    为什么在这里而不是在 ``View`` 的构造里：错误是**一次动作的结果**，不是数据的投影。
    投影层保持纯函数（13 个视图的快照不因错误而变），渲染层只在渲染前调用这一个纯函数。
    ``errors`` 为空时原样返回同一个对象（``is`` 相等），老路径零开销、零行为变化。

    只贴到**同名的可编辑字段**上：键不在本页的字段（例如在别的页保存）不许凭空造出红字。
    """
    if not errors:
        return view
    applied: list[Field] = []
    focus = ""
    for item in view.fields:
        message = str(errors.get(item.key) or "") if item.key else ""
        if not message:
            applied.append(item)
            continue
        applied.append(replace(item, error=message))
        if not focus:
            focus = item.key
    if not focus:                      # 本页没有这些字段：什么都没贴，别造空焦点
        return view
    return replace(view, fields=tuple(applied), focus_field=focus)


def _hero_text(hero: Hero) -> list[str]:
    """主卡的文本形态（与渲染顺序一致：标题 → 说明 → 指标 → 动作 → 提示）。"""
    lines = ["", "{ 主卡 } " + hero.title + (("  " + hero.chip) if hero.chip else "")]
    if hero.kicker:
        lines.append("  " + hero.kicker)
    if hero.body:
        lines.append("  " + hero.body)
    lines.append("  " + hero.runtime_label + "：" + hero.runtime)
    if hero.gauge is not None:
        lines.append("  " + (hero.gauge.label or "仪表环") + "：" + hero.gauge.value)
    if hero.metrics:
        lines.append("  指标：" + " · ".join(
            item.label + " " + item.value
            + ((" " + item.unit) if item.unit and item.value not in ("—", "") else "")
            for item in hero.metrics))
    for action in hero.actions:
        state = "" if action.enabled else ("（置灰：" + (action.reason or "当前不可用") + "）")
        lines.append("  [" + action.label + "]" + state)
    if hero.hint:
        lines.append("  " + hero.hint)
    if hero.note:
        lines.append("  · " + hero.note)
    return lines


def _chart_text(chart: Chart) -> list[str]:
    lines = ["", "{ " + (chart.title or "图表") + " } " + chart.caption]
    if chart.empty:
        lines.append("  " + chart.empty)
        return lines
    if chart.peak:
        lines.append("  " + chart.peak)
    if chart.foot:
        lines.append("  " + chart.foot)
    return lines


def _rail_text(rail: tuple[RailCard, ...]) -> list[str]:
    lines: list[str] = []
    for card in rail:
        lines.append("")
        head = "{ " + card.title + " }"
        if card.note:
            head += "  " + card.note
        if card.badge:
            head += "  " + card.badge
        lines.append(head)
        for stat in card.stats:
            lines.append("  " + stat.label + "：" + stat.value
                         + ((" " + stat.unit) if stat.unit else ""))
        for field in card.fields:
            lines.append("  " + field.label + "：" + field.value)
        for label, value, _tone in card.rows:
            lines.append("  " + label + "：" + value)
        if card.toggle is not None:
            lines.append("  " + card.toggle[0] + "：" + ("开" if card.toggle[1] else "关"))
        if card.progress is not None:
            lines.append("  " + card.progress[0] + "：" + card.progress[2])
        for action in card.actions:
            state = "" if action.enabled else ("（置灰：" + (action.reason or "当前不可用") + "）")
            lines.append("  [" + action.label + "]" + state)
        if card.foot:
            lines.append("  " + card.foot)
    return lines


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


#: RC15-039 #34：结算口径的**唯一出处**（owner 2026-11-16 D-2 原文，逐字照抄，不许改写）。
#: 全客户端只许引用这个名字，别处不许再手写同类句子（验收口径见票面 §0.1 D-2）。
#: 为什么合成一句：走查里那两句（一句说「完成就结算」，一句说「不能提」）同屏出现，
#: 客户读成两个互相打脸的承诺。现在只剩这一句，收益页四卡的去向都对得上它。
SETTLE_NOTE = "结算币种：USDT（按 1 USDT = 1 USD 展示）；收益不等于可提现余额。"


def _section(snapshot: Snapshot, name: str) -> Mapping[str, Any]:
    value = snapshot.get(name)
    return value if isinstance(value, Mapping) else {}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# ─────────────────────────────────────────────── 总览：主卡 / 节奏图 / 右栏（设计样 v2.1）
#: 让路模式的短说明（总览主卡/右栏用；改文案只改这里）。
#: RC15-039 复核 M7（owner a′）：原来这三档写的是"只要接单就满载 / 工作时段实时算力 /
#: 只在我不使用电脑时工作"——本版 daemon 不按档位改并发（`facts.PRESET_CONCURRENCY` 零消费），
#: 这三句就是假承诺；统一改成"只记录偏好"。``paused`` 是**真**行为（停止接新任务），保留。
YIELD_MODE_SUMMARY = {
    "full": "本版仅记录档位偏好",
    "balanced": "本版仅记录档位偏好",
    "saver": "本版仅记录档位偏好",
    "paused": "不接新任务；在途任务按护栏处理",
}

#: 「请求地址」统一口径（RC15-039 #37）：标签只叫「…请求地址」，填法提示**逐字一致**。
#: 以前一页叫「请求链接」、一页叫「上行 OpenAI」、还有一页叫「OpenAI 请求地址」，
#: 同一件事三种名字，客户以为是三个不同的字段。
BASE_URL_HINT = "填到 /v1 就行（例如 https://api.openai.com/v1）；不要带 key 参数"

#: CLI 命令的识别（RC15-039 #11）：`djb init` / `djb auth --bind <码>` / `djb start` …
_CLI_CALL = re.compile(r"djb\s+[a-z][a-z0-9_-]*(?:\s+--?[a-z][\w-]*(?:\s+<[^>]*>)?)?")


def terminalize(text: Any) -> str:
    """渲染边界统一处理「这一步要去终端跑」（RC15-039 #11）。

    客户端里 CLI 的 ``next_step`` 是从 daemon / envcheck 原样搬上屏的，客户手上没有终端，
    看到「执行 djb init 会收紧到 0700」只会一脸问号。规矩：**要么做成界面内动作，要么
    明说这条要去终端跑并给完整命令**。这里统一补一句，而不是逐页打补丁——逐页补的结果
    就是复核抓到的「只扫了 4 页，接入页/基准页漏了」。
    """
    value = str(text or "")
    if not value or "终端" in value:
        return value
    match = _CLI_CALL.search(value)
    if match is None:
        return value
    return value + "（这条要在终端里跑：" + match.group(0) + "；桌面端没有对应入口，界面里点不到）"


def _terminalize_tree(value: Any) -> Any:
    """把 :func:`terminalize` 应用到整个视图树上（只改含 CLI 命令的字符串）。"""
    if isinstance(value, str):
        return terminalize(value)
    if isinstance(value, tuple):
        return tuple(_terminalize_tree(item) for item in value)
    if isinstance(value, list):
        return [_terminalize_tree(item) for item in value]
    if isinstance(value, frozenset):
        return frozenset(_terminalize_tree(item) for item in value)
    if dataclass_is_instance(value):
        return replace(value, **{
            f.name: _terminalize_tree(getattr(value, f.name))
            for f in dataclass_fields(value)})
    return value


#: 贡献节奏图的时间窗（小时）
CHART_HOURS = 24


def _parse_ts(value: Any) -> Any:
    """解析 RFC3339（带 ``Z`` / ``+08:00`` / 无时区三种写法）；解析不了给 ``None``。"""
    text = str(value or "").strip()
    if not text:
        return None
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _seconds(value: Any) -> int | None:
    """非负整数秒；``None``/非法/负数 → ``None``（缺失不许变成 0）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number != number:
        return None
    return int(number)


def _runtime_text(value: Any) -> str:
    """运行时长 → ``HH:MM:SS``；没有这个字段就给「—」（本机数据源暂未提供 uptime）。"""
    seconds = _seconds(value)
    if seconds is None:
        return "—"
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _first_device(gpu: Mapping[str, Any]) -> Mapping[str, Any]:
    """GPU 域的第一块卡（字段名与 ``parse_nvidia_smi`` 一致：utilization_pct / temperature_c…）。"""
    devices = gpu.get("devices")
    if isinstance(devices, (list, tuple)):
        for device in devices:
            mapping = _mapping(device)
            if mapping:
                return mapping
    return {}


def _gigabytes(megabytes: Any, *, digits: int = 1) -> str:
    """MB → GB 文本（只做单位换算，不改口径）；缺失给「—」。"""
    try:
        number = float(megabytes)
    except (TypeError, ValueError):
        return "—"
    if number < 0:
        return "—"
    return f"{number / 1024.0:.{digits}f}"


def _hourly_chart(snapshot: Snapshot, *, hours: int = CHART_HOURS) -> Chart:
    """贡献节奏：**用已有收据的时间戳按小时聚合 tokens**（不新增数据域、不联网）。

    时间窗的锚点是 :attr:`Snapshot.fetched_at`（这次取数的时刻）——所以「近 24 小时」
    就是「到现在为止的 24 小时」。某个小时没有收据 = 该柱为 0 高（诚实），
    整段都没有收据 = ``empty`` 文案（不画假柱子）。
    """
    receipts = _section(snapshot, "receipts")
    samples: list[tuple[Any, int]] = []
    for row in receipts.get("rows") or ():
        mapping = _mapping(row)
        moment = _parse_ts(mapping.get("created_at"))
        if moment is None:
            continue
        samples.append((moment, _seconds(mapping.get("tokens")) or 0))
    anchor = _parse_ts(snapshot.fetched_at)
    if anchor is None and samples:
        anchor = max(moment for moment, _ in samples)
    title, caption = "贡献节奏", "近 " + str(hours) + " 小时 · 每小时 tokens"
    if anchor is None:
        return Chart(kicker="CONTRIBUTION RHYTHM", title=title, caption=caption,
                     empty="暂不可用（当前没有收据时间戳）")
    end = anchor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    start = end - timedelta(hours=hours)
    totals = [0] * hours
    inside = 0                       # 只数落在时间窗里的收据（窗外的不能算进「N 张收据」）
    for moment, tokens in samples:
        if not (start <= moment < end):
            continue
        index = int((moment - start).total_seconds() // 3600)
        if 0 <= index < hours:
            totals[index] += tokens
            inside += 1
    total = sum(totals)
    if total <= 0:
        return Chart(kicker="CONTRIBUTION RHYTHM", title=title, caption=caption,
                     empty="暂不可用（当前没有收据时间戳）")
    peak_value = max(totals)
    peak_index = totals.index(peak_value)
    values = tuple(round(value / float(peak_value), 4) for value in totals)
    step = hours // 4
    # 4 个整点刻度 + 末尾「现在」：24 小时后与起点同一钟点，直接画第 5 个刻度会出现两个一样的时刻
    labels = tuple((start + timedelta(hours=step * position)).strftime("%H:00")
                   for position in range(4)) + ("现在",)
    peak_at = (start + timedelta(hours=peak_index)).strftime("%H:00")
    return Chart(kicker="CONTRIBUTION RHYTHM", title=title, caption=caption,
                 values=values, labels=labels,
                 peak="峰值 " + f"{peak_value:,}" + " tokens · " + peak_at,
                 foot="近 " + str(hours) + " 小时合计 " + f"{total:,}" + " tokens · "
                      + str(inside) + " 张收据")


def _hero(snapshot: Snapshot) -> Hero:
    """状态主卡：全部字段来自既有数据域；缺什么就写「—」，不补 0、不编运行时长。"""
    status = _section(snapshot, "status")
    gpu = _section(snapshot, "gpu")
    device = _first_device(gpu)
    yield_state = _section(snapshot, "yield")
    receipts = _section(snapshot, "receipts")
    bench = _section(snapshot, "bench")
    access = _section(snapshot, "access")
    binding = _mapping(access.get("binding"))
    profile = _mapping(_section(snapshot, "account").get("profile"))
    state = str(status.get("state") or "unknown")
    paused = state == "paused"
    titles = {"online": "这台机器，正在贡献。", "starting": "这台机器，正在准备。",
              "paused": "这台机器，正在让路。", "updating": "这台机器，正在更新。",
              "stopped": "这台机器，还没有开始。", "error": "这台机器，需要看一眼。"}
    chips = {"online": ("● LIVE", "mint"), "starting": ("◐ STARTING", "amber"),
             "paused": ("● PAUSED", "amber"), "updating": ("◐ UPDATING", "amber"),
             "stopped": ("○ STOPPED", "muted"), "error": ("✕ ERROR", "red")}
    chip, chip_tone = chips.get(state, ("? 状态未知", "muted"))
    node_id = str(profile.get("node_id") or binding.get("node_id") or "—")
    mode = str(yield_state.get("mode") or "")
    summary = YIELD_MODE_SUMMARY.get(mode)
    accepting = yield_state.get("accepting")
    if summary:
        body = summary
        if accepting is True:
            body += "；当前允许接单"
        elif accepting is False:
            body += "；当前不接新任务"
    elif snapshot.ok("yield"):
        body = "让路规则已启用，" + ("当前允许接单" if accepting is True else "接单状态未知")
    else:
        body = "让路规则暂不可用（数据源未提供）"
    util = _seconds(device.get("utilization_pct"))
    gauge = GaugeData(None if util is None else min(100, util) / 100.0,
                      "—" if util is None else str(util) + "%", "GPU 利用率")
    receipt_rows = [_mapping(row) for row in (receipts.get("rows") or ())]
    receipt_count = (str(len(receipt_rows)) + " 张") if snapshot.ok("receipts") else "—"
    memory = "—"
    if device.get("memory_used_mb") is not None and device.get("memory_total_mb") is not None:
        memory = _gigabytes(device.get("memory_used_mb")) + " / " \
            + _gigabytes(device.get("memory_total_mb"), digits=0)
    temperature = _seconds(device.get("temperature_c"))
    load = bench.get("cpu_load_1m")
    metrics = (
        Metric("收据", receipt_count, note="近 24 小时累计" if snapshot.ok("receipts") else ""),
        Metric("显存", memory, unit="GB", note="已用 / 总量"),
        Metric("温度", "—" if temperature is None else str(temperature), unit="°C"),
        Metric("负载", f"{float(load):.2f}" if _is_number(load) else "—", note="1 分钟平均"),
    )
    actions = (
        Action("yield.pause", "暂停 1 小时", enabled=not paused, reason="已经是暂停状态"),
        Action("yield.resume", "恢复接单", enabled=paused, reason="当前不是暂停状态",
               primary=paused),
        Action("", "调整时段", nav="yield"),
    )
    if binding.get("state") in ("bound", "active"):
        hint = "已授权 1 个客户端"
    elif snapshot.ok("access"):
        hint = "还没有客户端授权"
    else:
        hint = "授权状态暂不可用"
    return Hero(kicker="NODE STATUS / " + node_id, title=titles.get(state, "这台机器的状态未知。"),
                chip=chip, chip_tone=chip_tone, body=body,
                runtime=_runtime_text(status.get("uptime_s")), gauge=gauge, metrics=metrics,
                actions=actions, hint=hint)


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _rail(snapshot: Snapshot) -> tuple[RailCard, ...]:
    """右栏三张卡：SETUP（接入进度）/ TODAY（今日四格）/ THROTTLE（让路规则）。

    编辑表单仍只在「接入配置」页（同一个配置文件不允许两处可写）；这里只读展示 + 一键跳转。
    """
    access = _section(snapshot, "access")
    config = _mapping(access.get("config"))
    earnings = _section(snapshot, "earnings")
    status = _section(snapshot, "status")
    plan = _section(snapshot, "token_plan")
    limits = _mapping(plan.get("limits"))
    receipts = _section(snapshot, "receipts")
    yield_state = _section(snapshot, "yield")
    # RC15-039 #9：第一步「绑定」用**账户页同一个判定源**（服务端 binding），
    # 不再拿「本机有没有节点凭据」冒充绑定——那正是「3 / 3 接入完成」与「未绑定」同屏的根因。
    bound_ok, _bound_node_id = binding_state(snapshot)
    steps = (bound_ok is True, bool(config.get("price_mode")),
             bool(config.get("usdt_address")))
    done = sum(1 for step in steps if step)
    # E（RC14 IA 拆分）：总览右栏只做**只读摘要**，且把「去哪改」指到唯一入口——
    # 价格/凭证 → 本机设置；绑定/收款 → 账户与安全（这里不再出现可编辑的 usdt_address 键，
    # 否则又变成第二个「填了以为生效」的地方）。
    setup_fields = (
        Field("节点凭证", str(config.get("node_secret") or "未设置"), key="node_secret",
              secret=True),
        Field("价格", str(config.get("price_mode_label") or config.get("price_mode") or "未设置"),
              key="price_mode"),
        Field("USDT 收款地址", "已设置" if config.get("usdt_address") else "未设置",
              key="usdt_state"),
    )
    setup = RailCard(kicker="SETUP / 三步接上", title="三步，把节点接上。", kind="setup",
                     badge=("接入完成" if done == 3 else "待完成"), badge_tone=("mint" if done == 3
                                                                          else "amber"),
                     note=str(done) + " / 3", fields=setup_fields,
                     actions=(Action("", "绑定与收款（账户与安全）", nav="account",
                                     primary=done < 2),
                              Action("", "价格与凭证（本机设置）", nav="access")),
                     foot="绑定、解绑、USDT 收款地址只在「账户与安全」；价格模式与节点凭证在"
                          "「本机设置」。")
    daily = None
    for item in plan.get("usage") or ():
        mapping = _mapping(item)
        if str(mapping.get("name") or "") == "daily":
            daily = mapping
            break
    today_tokens = _seconds(_mapping(daily).get("used")) if daily else None
    verified = sum(1 for row in receipts.get("rows") or ()
                   if str(_mapping(row).get("state") or _mapping(row).get("status") or "")
                   in ("verified", "accepted"))
    total_receipts = len([1 for row in receipts.get("rows") or () if _mapping(row)])
    today = RailCard(
        kicker="TODAY / 今日算力", title="今日收益",
        badge="进行中" if str(yield_state.get("state") or "") == "online" else "",
        badge_tone="mint",
        # 四宫格只留「标签 + 数值」：口径写进卡脚一行，格子高度才装得进一屏（G-16）
        stats=(Metric("今日收益", _num(earnings.get("today") or status.get("today_earnings")),
                      unit=str(earnings.get("currency") or "USDT")),
               Metric("今日 tokens", "—" if today_tokens is None else f"{today_tokens:,}"),
               Metric("收据数", (str(total_receipts) if snapshot.ok("receipts") else "—")),
               Metric("验证通过", ("—" if not snapshot.ok("receipts")
                                 else f"{verified} / {total_receipts}")),
               ),
        foot="服务端记账口径；tokens 取共享用量；收据＝本机行数。")
    throttle_rows = (
        ("并发上限", _num(limits.get("concurrent")) + " 路", "default"),
        ("每日 tokens 上限", "—" if limits.get("daily_tokens") in (None, 0)
                            else f"{_seconds(limits.get('daily_tokens')):,}", "default"),
        ("让路模式", YIELD_MODE_SUMMARY.get(str(yield_state.get("mode") or ""),
                                            str(yield_state.get("mode") or "未设置")), "muted"),
    )
    daily_cap = limits.get("daily_tokens")
    used_today = today_tokens
    ratio = None
    if _is_number(daily_cap) and float(daily_cap) > 0 and used_today is not None:
        ratio = min(1.0, float(used_today) / float(daily_cap))
    progress = None
    if ratio is not None:
        progress = ("今日 tokens 已用", ratio,
                    f"{used_today:,} / {int(float(daily_cap)):,}（{ratio * 100:.0f}%）")
    throttle = RailCard(kicker="THROTTLE / 让路规则", title="让路规则",
                        badge="生效中" if yield_state.get("yield_enabled") else "已关闭",
                        badge_tone="mint" if yield_state.get("yield_enabled") else "muted",
                        kind="throttle", rows=throttle_rows,
                        toggle=("自动让路", bool(yield_state.get("yield_enabled"))),
                        progress=progress,
                        foot="只会用你让出来的算力；全屏游戏与会议期间自动让路。")
    return (setup, today, throttle)


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
             ("账户预付款 · 结算口径见「收益」页" if earnings.get("balance") is not None
              else str(earnings.get("balance_note") or "余额暂不可用；结算口径见「收益」页")),
             "default"),
        Card("接单状态", state_text, str(reason or "无让路原因"), tone),
        Card("资源", _bytes(_section(snapshot, "bench").get("mem_available_bytes")) + " 可用内存",
             "CPU " + str(_section(snapshot, "bench").get("cpu_count") or "—") + " 核", "default"),
        Card("让路 / 节能",
             YIELD_LABEL_BY_KEY.get(str(yield_state.get("mode") or ""),
                                    str(yield_state.get("mode") or "未设置")),
             "暂停后与托盘一致", "muted"),
    )
    rows = _receipt_rows(receipts.get("rows"))
    # 「最近收据」按时间倒序取最新几条（列表语义；不改任何字段与口径）
    recent = list(reversed(rows))[:5]
    return View(
        key="overview", title="总览",
        subtitle="今日贡献 · 当前任务 · 资源 · 让路 · 收据",
        cards=cards,
        table=Table(("收据号", "时间", "Tokens", "状态", "本机校验"), tuple(recent),
                    narrow_hide=("时间", "Tokens"),
                    narrow_headers=(("本机校验", "校验"),),
                    note=str(receipts.get("note") or ""),
                    empty="还没有收据（完成后自动生成）", title="最近收据"),
        # 设计样新增：左栏主卡 + 节奏图，右栏三卡（数据仍全部来自上面这些既有数据域）
        hero=_hero(snapshot),
        chart=_hourly_chart(snapshot),
        rail=_rail(snapshot),
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
        # recorded = 服务端已记录但尚未校验：算「进行中」，不算「未知」也算不上「已通过」
        mark = (MARK_DONE if state in ("accepted", "verified", "ok", "done")
                else MARK_WAIT if state in ("pending", "buffered", "recorded")
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
                    note="检查项来自本机只读探测", empty="环境检测暂不可用", title="环境检测"),
        actions=(Action("bench.run", "开始跑分（≤10 秒）", primary=True),
                 Action("bench.copy", "复制结果")),
        unavailable=_missing(snapshot, ("bench",)),
        notes=("跑分只做本地计算：sha256 吞吐 / 浮点 / 整数；不上传、不写 store。",),
    )


#: 探针失败分类 → 客户能看懂的原因（``upstream.ProbeOutcome.error_class`` 的取值）。
#:
#: 设计 40-UX §3.3「原因」一行：映射表 + 「其余原样 + （未登记的原因）」。
#: **不许**只显示英文错误码——客户看不懂 ``stream_failed`` 是什么意思。
PROBE_REASON_TEXT = {
    "timeout": "超时（上游没在时限内回话）",
    "unauthorized": "密钥被拒（401；检查 Key 是否过期/权限）",
    "http_5xx": "上游 5xx（对方服务异常，稍后重试）",
    "stream_failed": "流式不可用（stream_failed）；仍按非流式公告",
    "model_not_listed": "上游 /models 里没有这个模型（清单或模型名不对）",
    "not_listed": "上游 /models 里没有这个模型（清单或模型名不对）",
    "usage_missing": "上游没回 usage（用量对不上，报价会保守）",
    "budget": "本轮探测预算用完（下一轮继续）",
    "disabled": "模型已停用（不参与探测与公告）",
    "network": "网络不通（DNS/连接被拒）",
    "invalid_response": "上游返回的不是兼容格式（不是 OpenAI/Anthropic 形状）",
}

#: 探针「上次结论」的年龄 → 人话（设计 40-UX §3.3：相对时间 + 「从未」）
def _short_url(text: str) -> str:
    """``https://api.example.com/v1`` → ``api.example.com/v1``（列宽有限，去掉协议头）。"""
    value = str(text or "").strip()
    for prefix in ("https://", "http://"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
            break
    return value.rstrip("/") or "—"


def _ago(seconds: Any) -> str:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return "从未"
    if value < 0:
        return "刚刚（时钟回拨）"
    if value < 60:
        return str(value) + " 秒前"
    if value < 3600:
        return str(value // 60) + " 分钟前"
    return str(value // 3600) + " 小时前"


def _probe_state(row: Mapping[str, Any], *, fresh_window: int, quote_window: int) -> tuple[str, str]:
    """探针行 →（状态标签, tone）。三态不许混：``None``（没探过）≠ ``False``（探过但失败）。"""
    if not row.get("enabled", True):
        return "已停用", "muted"
    marked = [row.get("inference_ok"), row.get("stream_ok"), row.get("usage_ok")]
    if all(item is None for item in marked):
        return "未探测", "muted"
    if row.get("announceable") is True:
        return "可公告", "mint"
    if row.get("inference_ok") is True:
        if row.get("fresh") is False:
            age = row.get("probe_age_s")
            return ("结论过期（报价已停，公告还剩 " + str(max(0, int(fresh_window)
                                                          - int(age or 0)) // 60)
                    + " 分钟）" if isinstance(age, int) else "结论过期"), "amber"
        if row.get("stream_ok") is False:
            return "降级：流式不可用", "amber"
        return "降级", "amber"
    return "失败", "red"


def _models(snapshot: Snapshot) -> View:
    models = _section(snapshot, "models")
    probe = models.get("probe") if isinstance(models.get("probe"), Mapping) else {}
    probe_rows = probe.get("rows") if isinstance(probe.get("rows"), list) else []
    fresh_window = int(probe.get("fresh_window_s") or 900)
    quote_window = int(probe.get("quote_window_s") or 600)

    # ── 探针表（D 的主战场：纯投影 probe_details()，不重算门控）
    rows: list[Row] = []
    counts = {"announceable": 0, "failed": 0, "never": 0}
    for item in probe_rows:
        if not isinstance(item, Mapping):
            continue
        state_text, tone = _probe_state(item, fresh_window=fresh_window,
                                        quote_window=quote_window)
        # 三态渲染：True ✓ / False ✗ / None —（None **不是** False，设计 40-UX §3.3 硬要求）
        def tri(value: Any) -> str:
            return MARK_DONE if value is True else (MARK_FAIL if value is False else "—")
        error_class = str(item.get("probe_error_class") or "")
        reason = PROBE_REASON_TEXT.get(error_class)
        if reason is not None:
            # 人话在前、原值在后：客服照人话解释，排障还能按英文码搜日志
            reason = reason + " [" + error_class + "]"
        elif error_class:
            reason = str(error_class) + "（未登记的原因）"
        if state_text == "可公告":
            counts["announceable"] += 1
        elif state_text == "未探测":
            counts["never"] += 1
        elif state_text in ("失败", "已停用"):
            counts["failed"] += 1
        model_id = str(item.get("model") or "—")
        upstream = str(item.get("base_url") or item.get("upstream_id") or "—")
        rows.append(Row((model_id, _short_url(upstream), _ago(item.get("probe_age_s")),
                         tri(item.get("inference_ok")), tri(item.get("stream_ok")),
                         tri(item.get("usage_ok")), state_text, reason or "—"),
                        key=str(item.get("upstream_id") or "") + "/" + model_id,
                        tone=tone, note=("失败原因：" + reason) if reason else ""))
    probe_table = Table(("模型", "上游", "最近探测", "L2", "L3", "用量", "状态", "原因"),
                        tuple(rows),
                        note=str(probe.get("unavailable") or ""),
                        empty="还没有接入任何中转站：去「提供 Token Plan」接入后这里才有探针结论", title="模型探测")

    # ── 云端目录（原 4 列，保留为附加表：它是「云端认为该有哪些模型」，与探针事实互补）
    catalog = models.get("catalog") if isinstance(models.get("catalog"), list) else []
    catalog_rows: list[Row] = []
    for row in catalog:
        if not isinstance(row, Mapping):
            continue
        offerable = bool(row.get("offered", True))
        reason = str(row.get("reason") or "")
        mark = MARK_DONE + " 可提供" if offerable else MARK_FAIL + " 不兼容"
        catalog_rows.append(Row((str(row.get("id") or "—"), _num(row.get("context")), mark,
                                 reason or ("按当前资源可提供" if offerable else "原因未下发")),
                                key=str(row.get("id") or ""), disabled=not offerable))
    local = models.get("local") if isinstance(models.get("local"), list) else []
    probe_actions = [Action("probe.run", "立即探测（全部上游）", primary=True,
                            enabled=bool(probe_rows),
                            reason="还没有接入上游" if not probe_rows else "")]
    return View(
        key="models", title="模型与探针",
        subtitle="探针事实（L2/L3/用量/新鲜度/失败原因）· 云端目录",
        cards=(Card("可公告", str(counts["announceable"]) + " 个",
                    "探针通过且结论新鲜才会进容量", "mint" if counts["announceable"] else "muted"),
               Card("探针失败 / 停用", str(counts["failed"]) + " 个",
                    "看右列原因逐条修", "red" if counts["failed"] else "muted"),
               Card("从未探测", str(counts["never"]) + " 个",
                    "点「立即探测」补一次", "amber" if counts["never"] else "muted"),
               Card("本地已应用", _count(snapshot, "models", str(len(local)) + " 个"),
                    "model-sync 状态", "default")),
        table=probe_table,
        tables=(Table(("模型", "上下文", "状态", "原因 / 说明"), tuple(catalog_rows),
                      note=str(models.get("note") or ""), empty="云端目录暂不可用", title="模型目录"),),
        # G1-1（RC14 P0）：**摘掉**「保存并上报意向」这颗按钮（键名见 commit / NODE.md）。
        # 表里根本没有开关控件，副标题却写「逐模型开关」，按钮点了又永远无声
        # （云端意向上报接口未接线）——虚假承诺比缺按钮更伤。
        # 等「一 Key 多模型 + 逐模型底价」把上报端点接上再加回（见「提供 Token Plan」页）。
        actions=tuple(probe_actions),
        unavailable=_missing(snapshot, ("models",)),
        notes=("容量公告只看 " + str(fresh_window // 60) + " 分钟内的探针结论；报价更严，只看 "
               + str(quote_window // 60) + " 分钟内的（提前停报防半窗口）。",
               "没有探针结论 / 结论过期的模型不会进容量、也不会报价——不是「设置没生效」。",
               "模型开关与底价在「提供 Token Plan」页设置；本页只读探针事实。"),
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
                    empty="本机无可用 GPU（nvidia-smi / rocm-smi 都没有输出）", title="显卡"),
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
                    empty=_empty_text(snapshot, "tasks", "当前没有任务"), title="任务"),
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
                    narrow_headers=(("本机校验", "校验"),), title="收据明细"),
        actions=(Action("receipts.verify", "校验选中收据", enabled=bool(rows),
                        reason="没有可校验的收据"),),
        unavailable=_missing(snapshot, ("receipts",)),
        notes=("状态列是云端受理结果（accepted/rejected/expired）；「本机校验」列才是本机比对结论。",
               "哈希与 seq 可以原样复制给对方核对；颜色只用来弱化，不承载信息。"),
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
        # 副标题只做「这是哪一页」；结算口径统一引用常量（不再各处手写一句）
        subtitle="收益明细 · " + SETTLE_NOTE,
        cards=(Card("今日", _money(snapshot, "earnings", earnings.get("today"), currency),
                    # 「完成即结算」会和「待结算」自相矛盾：今日只说「已经进了结算流程」
                    _today_note(earnings.get("today")) or "今天完成，已进入结算流程", "mint"),
               Card("待结算", _money(snapshot, "earnings", earnings.get("pending")),
                    "结算中不代表到账", "amber"),
               Card("已结算", _money(snapshot, "earnings", earnings.get("settled")),
                    "链上确认后计入", "default"),
               Card("余额（可用）",
                    ("暂不可用" if earnings.get("balance_note") and
                     earnings.get("balance") is None
                     else _money(snapshot, "earnings", earnings.get("balance"))),
                    # 余额卡**不许**暗示可提现（owner D-2）：meta 只留「这一列是余额」
                    "可用余额 · 口径见本页副标题", "muted")),
        table=Table(("明细", "金额", "币种", "状态", "时间"), tuple(rows),
                    narrow_hide=("币种", "时间"),
                    note=str(earnings.get("note") or ""),
                    empty=(UNAVAILABLE if not available else "暂无计费明细"), title="计费明细"),
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
            Field("OpenAI 请求地址", str(upstream.get("openai") or "未设置"),
                  key="openai_base_url", editable=True, hint=BASE_URL_HINT),
            Field("Anthropic 请求地址", str(upstream.get("anthropic") or "未设置"),
                  key="anthropic_base_url", editable=True, hint=BASE_URL_HINT),
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


#: 底价列/卡片用的「生效来源」文案（RC14 J：客户得知道哪一层在起作用）
BID_SOURCE_TEXT = {
    "cloud": "云端覆盖",
    "local": "本机底价",
    "none": "未设价",
}


def _bid_rows(plan: Mapping[str, Any], bids: Mapping[str, Any]) -> list[Row]:
    """逐模型底价表：``模型 | 上游 | 目录 | 探针 | 本机底价 | 生效来源``。

    「生效来源」按「云端覆盖 > 本机底价 > 未设价」显示（云端没开通就给「待云端开通」，
    这一列是客户判断「我填的价到底有没有生效」的唯一依据，不许留空也不许写「未知」）。
    """
    local = bids.get("rows") if isinstance(bids.get("rows"), Mapping) else {}
    cloud = bids.get("cloud") if isinstance(bids.get("cloud"), Mapping) else {}
    cloud_pending = str(cloud.get("state") or "") != "ok"
    rows: list[Row] = []
    for item in plan.get("models") or []:
        if not isinstance(item, Mapping):
            continue
        model = str(item.get("model") or "")
        if not model:
            continue
        entry = local.get(model)
        price = str(entry.get("min_price") or "") if isinstance(entry, Mapping) else ""
        if cloud_pending & bool(price):
            source = "本机底价（云端未开通）"
        elif price:
            source = BID_SOURCE_TEXT["local"]
        else:
            source = BID_SOURCE_TEXT["none"]
        probe = ("✓ 可公告" if item.get("announceable") else
                 "✗ " + str(item.get("probe_error_class") or "未通过"))
        rows.append(Row((model,
                         _short_id(str(item.get("upstream_id") or ""), 10) or "—",
                         "✓" if item.get("canonical") else "未映射",
                         probe,
                         (price + " 元/百万") if price else "未设价",
                         source),
                        key=model,
                        tone="default" if price else "muted",
                        narrow=(model, price or "未设价", source)))
    return rows


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
    # 绑定态走**唯一判定源**（#9）：总览 / 账户页 / 这里三处必须同一个答案
    bound, _plan_node_id = binding_state(snapshot)
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
    if not limits:
        # 限额读不到时不许说「周/月未启用」—— 那是把缺数据编成配置结论
        # （daemon 没起时 status 只回 state、不带 profile，这一支就是常态）
        budget, budget_meta = UNAVAILABLE, ""
    shared_value = (_num(used) + " tokens") if snapshot.ok("token_plan") else UNAVAILABLE
    shared_meta = ((str(len(receipt_rows)) + " 张收据 · 今日上限 " + _num(daily_limit))
                   if snapshot.ok("receipts") else UNAVAILABLE)
    notes: list[str] = []
    if bound is False:
        notes.append("节点还没绑定：先去「账户与安全」用网页端一次性绑定码完成绑定，"
                     "再把 Token Plan 共享出去。")
    if snapshot.ok("token_plan") and not rows:
        notes.append(TOKEN_PLAN_EMPTY + "（A1 直填 Key / A2 本机代理，都在「接入配置」里）。")
    notes.append("验证状态来自本机探针（L2 推理通过 + 结论新鲜）；"
                 "未验证 / 已过期的模型不进容量，也不会产生共享收益。")
    notes.append("「最大 QPS」是本机按并发上限分摊给已验证模型的数值（公告口径），不是实测吞吐。")
    notes.append("逐模型底价最小 0.01（USD / 百万 token）：填 0 会被云端当「未申报」直接跳过，"
                 "所以本机就先拦下。")
    _ = None
    if not (snapshot.ok("access") and snapshot.ok("account")):
        notes.append("接入配置 / 账户数据暂不可用：绑定状态暂时无法确认，点「刷新」重试。")
    if plan.get("note"):
        notes.append(str(plan.get("note")))
    # E（RC14 IA 拆分）：原来只有一颗「去接入配置（添加上游 / 绑定节点）」——两件事挤在
    # 一句话里，跳到的是**本机设置**页，那里既不能添加上游也没有绑定按钮（死路）。
    # 现在拆成两颗，各自跳到真正能做这件事的地方：
    #   * 添加上游 → 本页「添加接入」（第 1 步就在下方，item 11 落地表单）
    #   * 绑定节点 → 账户与安全（绑定的唯一位）
    plan_actions = [Action("token_plan.add_upstream", "添加上游（填请求地址与密钥）",
                           primary=True)]
    if bound is True:
        plan_actions.append(Action("token_plan.bind_node", "已绑定这台电脑", enabled=False,
                                   reason="已绑定 · 解绑/换绑在「账户与安全」"))
    else:
        plan_actions.append(Action("token_plan.bind_node", "绑定这台电脑（拿共享收益）"))
    plan_actions.append(Action("token_plan.refresh", "刷新"))

    # ── RC14 I/J：一 Key 多模型 + 逐模型底价（三步同页）
    bids = plan.get("bids") if isinstance(plan.get("bids"), Mapping) else {}
    bid_rows = _bid_rows(plan, bids)
    cloud = bids.get("cloud") if isinstance(bids.get("cloud"), Mapping) else {}
    cloud_pending = str(cloud.get("state") or "") != "ok"
    cloud_text = (("来源 " + str(cloud.get("source"))
                   + ("（回退自 " + str(cloud.get("fallback_from")) + "）"
                      if cloud.get("fallback_from") else ""))
                  if not cloud_pending else str(cloud.get("text") or "待云端开通"))
    priced = sum(1 for row in bid_rows if row.cells[4] != "未设价")
    report_blocker = str(bids.get("report_blocker") or "")
    preview = bids.get("preview") if isinstance(bids.get("preview"), Mapping) else {}
    preview_models = [item for item in (preview.get("models") or [])
                      if isinstance(item, Mapping)]
    discover_note = str(plan.get("discover_note") or "")
    fields = (
        Field("协议", str(plan.get("protocol") or ""), key="protocol", editable=True,
              choices=tuple(facts.UPSTREAM_PROTOCOLS),
              hint="默认 " + facts.UPSTREAM_PROTOCOL_LABELS.get(
                  facts.DEFAULT_UPSTREAM_PROTOCOL, facts.DEFAULT_UPSTREAM_PROTOCOL)
              + "；切换不会改已填的链接"),
        Field("请求地址", str(plan.get("base_url") or ""), key="base_url", editable=True,
              prefill=False, hint=BASE_URL_HINT),
        # 密钥只存本机 0600；显示按钮由 Field.can_reveal 自动给（G7）
        Field("密钥", str(plan.get("secret_seen") or "未保存"), key="upstream_secret",
              editable=True, secret=True, prefill=False,
              hint="只存本机（0600）；云端只收「已登记」这一个布尔"),
        Field("最低申报价", str(plan.get("selected_price") or ""), key="min_price",
              editable=True, prefill=False,
              hint=((("当前选中的 " + str(plan.get("selected_model")) + " 已存 "
                       + str(plan.get("selected_price")) + "；") if plan.get("selected_price")
                      else (("当前选中：" + str(plan.get("selected_model")) + "（未设价）；")
                            if plan.get("selected_model") else "先在上面的表格里选一行；"))
                    + "单位 USD / 百万 token，最小 0.01（填 0 会被云端跳过，留空 = 不申报）")),
        Field("云端覆盖", cloud_text, key="bid_cloud",
              hint="生效顺序：云端覆盖 > 本机底价 > 未设价"),
    )
    if bid_rows:
        planned = "已设价 " + str(priced) + " / " + str(len(bid_rows)) + " 个模型"
    else:
        planned = "还没有上游模型：先填链接与密钥 → 读取模型清单"
    if cloud_pending:
        notes.append("云端「按模型设价」接口还没开通（ask-settings / quotes.models[]）："
                     "底价只保存在本机，上报按钮保持关闭，不会假成功。")
    plan_actions.extend([
        # CR-20261119-CLIENT-TOKENPLAN-NOOP：读清单同时**保存接入**（第 1 步与第 2 步
        # 合成一颗按钮）——旧文案只写「读取模型清单」，客户以为读完就完事，实际上
        # 桌面端根本没落盘，模型表永远空。
        Action("token_plan.discover", "读取模型清单并保存接入"),
        # F4：「点了没反应」是缺陷。这两颗不再置灰（置灰的按钮点了不播报），
        # 前提不满足时由动作回一句人话（「先在上面的表格里选一行模型」…）。
        Action("token_plan.save_price", "保存这一行"),
        Action("token_plan.preview_report", "预览将上报的内容"),
        # 上报默认关闭（云端 ask-settings / quotes.models[] 未开通）：置灰 + 说清原因，
        # 绝不点下去假成功
        Action("token_plan.report", "上报到云端（逐模型底价）", enabled=not report_blocker,
               reason=report_blocker),
    ])
    return View(
        key="token_plan", title="提供 Token Plan",
        subtitle="接入 / 模型清单 / 逐模型底价 / 探针（本页可操作）",
        cards=(
            Card("共享状态", state_text, bound_text + " · 节点 " + node_id,
                 "amber" if bound is False else state_tone),
            Card("我的 Token Plan（上游）",
                 _count(snapshot, "token_plan", str(len(rows)) + " 条上游"),
                 (("已验证 " + str(verified_upstreams) + " 条 · 详见下表") if rows
                  else "点下面「添加上游」填请求地址") if snapshot.ok("token_plan") else "",
                 "mint" if rows else "amber"),
            Card("共享预算", budget, budget_meta + "（本机限额设置）", "default"),
            Card("今日已共享", shared_value, shared_meta, "mint" if used else "muted"),
        ),
        table=Table(("模型", "上游", "目录", "探针", "本机底价", "生效来源"), tuple(bid_rows),
                    narrow_hide=("目录",),
                    note=planned + ("｜" + discover_note if discover_note else ""),
                    empty=("还没有模型行：填「请求地址」与「密钥」，再点「读取模型清单」。"
                           if snapshot.ok("token_plan") else UNAVAILABLE), title="上游报价"),
        tables=(Table(("上游", "接入", "验证状态", "模型", "最大 QPS", "最近验证"), tuple(rows),
                      narrow_hide=("最近验证",),
                      note="验证通过且新鲜的模型才会进入共享容量",
                      empty=(TOKEN_PLAN_EMPTY if snapshot.ok("token_plan") else UNAVAILABLE), title="上游接入"),),
        fields=fields,
        actions=tuple(plan_actions),
        unavailable=_missing(snapshot, ("token_plan",)),
        notes=tuple(notes),
    )


def _payout_state(access: Mapping[str, Any]) -> dict[str, Any]:
    """USDT 收款地址的云端状态（**唯一**计算处：「账户与安全」页用）。

    ok=True + 空 dict = 云端确实没登记过（尚未提交）；ok=False = 这一轮没读到。
    两种都不能显示成「已生效」——收款地址写错就是钱打到别人账上。
    """
    payout = _mapping(access.get("payout"))
    ok = payout.get("ok") is True
    row = dict(_mapping(payout.get("data"))) if ok else None
    disabled = payout.get("disabled") is True
    state = "服务未开放" if disabled else payout_mod.short_state(row)
    reason = str(payout.get("reason") or "").strip()
    if disabled:
        hint, meta = "服务未开放（等平台开启后再提交）", reason or "等平台开启"
    elif ok:
        hint = state
        meta = mask_address(str(row.get("address") or "")) or "—"
        # 2026-11-18 owner 纠正：`cooldown_until` 是「可出款时间」（新地址 72h 冷却），
        # **不是**「可再改时间」；改地址看 `changed_at + 24h`。两个时间点分开写。
        if payout_mod.cooldown_active(row):
            meta += "．" + payout_mod.cooldown_notice(row)
        change = payout_mod.change_notice(row)
        if change:
            meta += "．" + change
    else:
        hint = payout_mod.UNREADABLE
        meta = reason or "完成节点绑定后重试"
    tone = {"已生效": "mint", "待审核": "amber", "冷却中": "amber",
            "已拒绝": "red", "已撤销": "red"}.get(state, "muted")
    return {"ok": ok, "row": row, "disabled": disabled, "state": state, "hint": hint,
            "meta": meta, "tone": tone,
            "can_submit": payout.get("can_submit") is True,
            "local": str(_mapping(access.get("config")).get("usdt_address") or "—")}


def _access(snapshot: Snapshot) -> View:
    access = _section(snapshot, "access")
    config = access.get("config") if isinstance(access.get("config"), Mapping) else {}
    binding = access.get("binding") if isinstance(access.get("binding"), Mapping) else {}
    bound = _binding_bound(binding)
    valid = access.get("valid")
    # 节点标识：数据层已按 access.json → 本机凭据 → 状态库 三级回退（datasource._resolve_node_id）。
    # 只有用户手填的（source == "config"）才可编辑；服务端下发的一律只读——否则「保存并回读」
    # 会把展示文案当值写回 access.json（动作执行前会把所有可编辑字段收成草稿）。
    node_id = str(access.get("node_id") or "")
    node_source = str(access.get("node_id_source") or "")
    if not node_id:
        # 老调用方（demo 数据 / FakeDataSource / 旧快照）没有新字段：退回 config 里的手填值，
        # 保证行为与 0.3.2 一致；真数据源总会带 node_id（含三级回退结果）。
        from_config = str(config.get("node_id") or "")
        if from_config and from_config != "—":
            node_id, node_source = from_config, "config"
    diag = [row for row in (access.get("node_id_diag") or []) if isinstance(row, Mapping)]
    daemon = access.get("daemon") if isinstance(access.get("daemon"), Mapping) else {}
    # 「清退」是**不可逆**状态：判据用 code（reason 是本地化标签，不能拿来做条件）
    retired = str(daemon.get("reason_code") or "") == "retired"
    if node_id:
        node_hint = ("网页端绑定要填这个 ID（本机只读）" if node_source != "config"
                     else "网页端绑定要填这个 ID")
        node_field = Field("节点标识", node_id, key="node_id",
                           editable=node_source == "config", hint=node_hint)
    else:
        # 旧文案「先启动一次」把三种完全不同的故障说成一句话，客户照做也没用。
        # 现在按**已知原因**说下一步；原因不明时给可执行的排查入口。
        daemon_reason = str(daemon.get("reason") or "").strip()
        if not daemon.get("running") and daemon_reason:
            node_value = "尚未生成（节点进程没起来）"
            node_hint = daemon_reason
        elif not daemon.get("running"):
            node_value = "尚未生成（节点进程没起来）"
            node_hint = "先看「节点进程」这一行的原因；启动失败时日志在 logs/djb.log"
        else:
            node_value = "尚未生成（节点进程在跑，注册没成功）"
            node_hint = "看下面「注册状态」的原因；修好后点右上角「刷新」即可，不用重装"
        node_field = Field("节点标识", node_value, key="node_id", hint=node_hint)
    # 「节点绑定」卡的副标题也带节点 ID（云端口径优先；``display()`` 的占位「—」不算值）
    card_node = str(binding.get("node_id") or "").strip()
    for candidate in (config.get("node_id"), node_id):
        text = str(candidate or "").strip()
        if not card_node and text and text != "—":
            card_node = text
    # E（RC14 IA 拆分）：收款地址的**状态卡不再放本页**（唯一位在「账户与安全」，
    # 计算收敛到 :func:`_payout_state`，避免两页各算一遍、说法不一致）。：本页退为「本机设置」——只放**本机**配置（价格模式 / 节点凭证 /
    # 配置文件 / 只读诊断）。绑定与收款地址搬去「账户与安全」：同一件事只允许一个入口，
    # 否则客户在两边填、两边都以为生效（报告 §E 的原始投诉）。
    return View(
        key="access", title="本机设置",
        subtitle="价格模式 · 节点凭证 · 配置文件（绑定与收款在「账户与安全」）",
        cards=(Card("配置有效性",
                    "尚未保存" if access.get("saved") is False
                    else _flag(valid, "通过校验", "有字段需修正"), "",
                    "amber" if access.get("saved") is False or not valid else "mint"),
               Card("节点绑定", _flag(bound if bound is not None else None, "已绑定", "未绑定"),
                    card_node or "—", "default"),
               # 第三张卡给「本页能做什么」：客户在本页找不到绑定时，得知道去哪
               Card("绑定与收款", "在「账户与安全」",
                    "本页只存本机配置（0600）", "muted")),
        fields=(
            node_field,
            # 只读排障字段：节点进程 / 注册状态（有则显示事实，无则写原因 + 下一步）
            Field("节点进程",
                  ("运行中" if daemon.get("running") else "未运行")
                  + ("（" + str(daemon.get("state")) + "）" if daemon.get("state") else ""),
                  key="daemon_state", hint=str(daemon.get("reason") or "—")),
            Field("注册状态",
                  ("已注册" if node_id else "未注册"),
                  key="register_state",
                  hint=(("节点 ID 来源：" + str(access.get("node_id_source") or "—"))
                        if node_id else _diag_hint(diag))),
            Field("价格模式", str(config.get("price_mode") or "—"), key="price_mode",
                  editable=True,
                  hint="auto / manual；当前：" + str(config.get("price_mode_label") or "—")),
            # 节点凭证 = 节点密钥（排障要用，绝不能占首屏）→ 折叠进「高级（排障用）」
            Field("节点凭证", str(config.get("node_secret") or "—"), key="node_secret",
                  editable=True, secret=True, advanced=True,
                  hint="掩码显示；粘贴新值才会覆盖"),
        ),
        # RC15-039 #4：自动刷新是**本机界面**设置（不是服务端配置），所以单独一条开关，
        # 不进表单、不参与「保存并回读」（那会把界面偏好写进接入配置）。
        switches=(Toggle("auto_refresh", "自动刷新",
                         value=bool(access.get("auto_refresh", True)),
                         action="access.toggle_auto_refresh",
                         hint="页面可见时每 10 秒；最小化或空闲 60 秒；只读本机数据"),),
        actions=(Action("access.save", "保存并回读", primary=True),
                 Action("access.copy_node_id", "复制节点标识", enabled=bool(node_id),
                        reason=("还没有节点 ID，无法复制（原因见「注册状态」一行："
                                + _diag_hint(diag) + "）") if not node_id else ""),
                 Action("access.reset_node", "重新注册这台机器（清退恢复）",
                        enabled=retired,
                        reason="" if retired
                        else "只有节点被云端清退后才需要（当前原因："
                             + str(daemon.get("reason") or "—") + "）"),
                 Action("access.reload", "请求 daemon 重载"),
                 # 搬走的两个入口在这里只留**指路**（不是按钮）：本页不能做，就别假装能做
                 Action("access.goto_account", "绑定与收款在「账户与安全」")),
        unavailable=_missing(snapshot, ("access",)),
        notes=(str(access.get("note") or ""),
               *(() if node_id else tuple(
                   "节点 ID 三级回退：" + row.get("level", "?") + " → "
                   + ("读到" if row.get("ok") else str(row.get("reason") or "读不到"))
                   for row in diag)),
               (("节点进程下一步：节点已清退（不可逆）——「重新注册这台机器」会用同一台机器"
                 "开一个新节点 ID（旧节点保留为档案，不复活）；新节点要在「账户与安全」"
                 "重新绑定网页端才能接单。需要云端已上线 214（同机重注册）迁移。")
                if retired
                else (("节点进程下一步：" + str(daemon.get("next_step")))
                      if daemon.get("next_step") else "")),
               "配置文件：" + str(access.get("path") or "—") + "（0600，只在本机）",
               ("收款地址只支持 Polygon（ERC20，0x 开头 42 位）；TRC20 暂不支持"
                "（云端出款链只有 Polygon）—— 填地址与提交都在「账户与安全」。"),
               "该文件只存本机接入配置（节点密钥 / 收款地址 / 价格模式），"
               "不含账号登录态；账户资料要等节点绑定后从云端读取。"),
    )


def _diag_hint(diag: Sequence[Mapping[str, Any]]) -> str:
    """三级回退里**第一条真故障**的原因（跳过「手填为空」这种常态，也跳过最末级的连带现象）。

    「access.json 里没有手填值」是注册成功时的正常形态，不能当原因；
    「状态库读不到」往往是前一级失败后的连带现象（库都没建）。所以取第一条**有意义的**
    失败原因——它就是客户要动手的那一环。
    """
    for row in diag:
        if not isinstance(row, Mapping) or row.get("ok"):
            continue
        if str(row.get("level") or "").startswith("access.json"):
            continue
        reason = str(row.get("reason") or "")
        if reason:
            step = str(row.get("next_step") or "")
            return reason + ("（" + step + "）" if step else "")
    return "原因未知：看 logs/djb.log"


def _today_note(today: Any) -> str:
    """「今日」按 UTC 日界（服务端口径）；本地时区跨日时数字会看起来对不上，得写明。"""
    if today in (None, ""):
        return "服务端没有下发今天的数字（不是 0）"
    return "服务端下发值（UTC 日界）；本机不换算"


#: RC15-039 #17：档位**只有三个**（owner 2026-11-16 D-1：``paused`` 是状态，不是第四档）。
#: 每项 = (表内键, 档位文案, ``djb limits --preset`` 的取值)。
#: 第三列是**写命令**的取值（「全速」在本机命令里叫「全力以赴」），必须一一对上，
#: 否则切完回读永远不一致。前两列之外的地方不许再手写档位名。
#:
#: RC15-039 复核 M7（owner a′）：原来的第四列「一句话说明」写的是"只要接单就满载 /
#: 工作时段实时算力 / 只在我不使用电脑时工作"——这三句描述的是**本版 daemon 不做**的事
#: （`facts.PRESET_CONCURRENCY` 零消费），与页脚"本版不按档位调整"同屏矛盾，整列删掉。
YIELD_TIERS: tuple[tuple[str, str, str], ...] = (
    ("full", "全速", "全力以赴"),
    ("balanced", "均衡", "均衡"),
    ("saver", "省心", "省心"),
)
#: 表内键 → 写命令取值 / 档位文案 → 写命令取值（动作层按选中的**标签**查）
YIELD_PRESET_BY_KEY: dict[str, str] = {key: preset for key, _l, preset in YIELD_TIERS}
#: 键 → 中文档位名（复核 SHOULD-FIX 7：总览卡以前直接印原始键 `balanced`，中文界面里冒英文）
YIELD_LABEL_BY_KEY: dict[str, str] = {key: label for key, label, _p in YIELD_TIERS}
YIELD_PRESET_BY_LABEL: dict[str, str] = {label: preset for _k, label, preset in YIELD_TIERS}


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
    # RC15-039 #17：当前档位以**本机配置里的 preset** 为准（``djb limits --preset`` 写的就是它），
    # 不从 yield_mode 猜：以前那行「模式」读的是另一个键，跟切档根本不是一个东西。
    preset = str(data.get("preset") or "")
    # RC15-039 复核 M7：首屏**没有**任何回读动作（数据来自本机配置），所以只说「本机配置」；
    # 「已回读」只在切档动作成功后出现（控制器注入 `readback_ok`）。
    verified = bool(data.get("readback_ok"))
    active_key = next((key for key, _l, value in YIELD_TIERS if value == preset), "")
    rows = tuple(Row((label, key, (MARK_DONE + " 当前") if key == active_key else MARK_IDLE),
                     key=key)
                 for key, label, _value in YIELD_TIERS)
    if active_key:
        now_text = next(label for key, label, _v in YIELD_TIERS if key == active_key)
        now_meta, now_tone = ("本机配置 · 已回读" if verified else "本机配置"), "mint"
    else:
        now_text = "未知（没有回读到）"
        now_meta, now_tone = "读不到就不猜；点下面三档之一可重新写一次", "amber"
    return View(
        key="yield", title="让路 / 节能",
        # 「暂停」是**状态**不是档位（owner D-1）：它只由下面两个按钮进出
        subtitle="三档模式 · 暂停是状态，不占档位",
        cards=(Card("当前状态", _yield_state_text(state),
                    str(reason or data.get("reason_label") or "无让路原因"),
                    _yield_state_tone(state)),
               Card("接单", _flag(data.get("accepting"), "允许", "禁止"), "", "default"),
               Card("档位", now_text, now_meta, now_tone)),
        table=Table(("档位", "键", "当前"), rows, empty="档位暂不可用",
                    activate="yield.apply_mode", title="档位"),
        actions=(Action("yield.apply_mode", "切到选中的档位", enabled=bool(rows),
                        reason="当前没有可切换的档位", primary=True),
                 Action("yield.pause", "暂停接单", enabled=not paused,
                        reason="已经是暂停状态"),
                 Action("yield.resume", "恢复接单", enabled=paused,
                        reason="当前不是暂停状态")),
        unavailable=_missing(snapshot, ("yield",)),
        notes=("先选中一档（↑↓ 或点击），再点「切到选中的档位」或按回车："
               "会先问一次，确认后写本机配置并回读 daemon。",
               "切档不打断在跑的任务、不重启服务；暂停/恢复是另一个动作，"
               "与托盘读同一份状态。",
               # RC15-039 复核 M7（owner a′）：用**正面**口径说同一件事，别和上面的档位
               # 描述互相打脸——"只记录偏好"是这版真实做的事，"后续版本生效"是真实计划。
               "本版只记录档位偏好；并发与让路在后续版本生效。"),
    )


def binding_state(snapshot: Snapshot) -> tuple[bool | None, str]:
    """**绑定态的唯一判定源**（RC15-039 #9）：返回 ``(是否已绑定, 节点 ID)``。

    总览「三步接上」与账户页「节点绑定」都从这里取，不许各算各的——以前总览看「本机有没有
    节点凭据」、账户页看「服务端 binding」，于是同一屏同时出现「3 / 3 接入完成」和「未绑定」（#9），
    还把「节点 ID 存在」读成「没注册」（#33）。
    """
    account = _section(snapshot, "account")
    access = _section(snapshot, "access")
    profile = _mapping(account.get("profile"))
    security = _mapping(account.get("security"))
    binding = _mapping(access.get("binding"))
    bound = _account_bound(profile, security, binding)
    node_id = str(profile.get("node_id") or security.get("node_id") or "")
    return bound, node_id


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
    bound, bound_node_id = binding_state(snapshot)
    # 绑定入口**不能**被「本域暂不可用」挡掉：未登录时账户域本来就取不到云端数据，
    # 而那正是最需要「绑定网页端」的时刻（2026-11-11 正式环境实测：账户页只剩一行
    # 「账户资料暂不可用」，按钮全被隐藏，用户没有任何可点的下一步）。
    can_bind = bound is not True
    if unavailable and can_bind:
        # unavailable 是 tuple（每个不可用数据域一条原因）：只给账户这一条追加可操作解释，
        # 别把整页顶部变成一长串。
        unavailable = tuple(
            item + "（按下方步骤绑定后即可读取账户资料；不是配置损坏）" if index == 0 else item
            for index, item in enumerate(unavailable))
    twofa = profile.get("twofa_enabled", profile.get("mfa_enabled"))
    if bound is not None:
        # RC15-039 #33：「未绑定」只说**网页端账号**没绑到这个节点，不等于节点没注册。
        binding_meta = ("服务端视图（只读）" if bound is True else
                        ("本机已有节点凭据，只是网页端账号还没绑到它"
                         if (bound_node_id or security.get("node_id")) else
                         "服务端视图（只读）"))
        second = Card("节点绑定", _flag(bound, "已绑定", "未绑定"),
                      binding_meta, "mint" if bound is True else "amber")
    else:
        second = Card("双因素验证", _flag(twofa, "已开启", "未开启", "暂不可用"),
                      "登录保护的第二步", "mint" if twofa is True else "amber")
    if node_id:
        fields: list[Field] = [Field("节点标识", identity, key="node_id",
                                     hint="节点身份由服务端下发，本机只读",
                                     group=GROUP_IDENTITY)]
    else:
        fields = [Field("邮箱", identity, key="email", hint="登录后在服务端修改",
                        group=GROUP_IDENTITY)]
    if node_id:
        fields.extend([
            Field("节点状态", str(profile.get("state") or "—"), key="state",
                  group=GROUP_IDENTITY),
            Field("地域", str(profile.get("region") or "—"), key="region",
                  group=GROUP_IDENTITY),
            Field("平台", str(profile.get("platform") or "—"), key="platform",
                  group=GROUP_IDENTITY),
            Field("最后在线", str(profile.get("last_seen_at") or "—"), key="last_seen_at",
                  group=GROUP_IDENTITY),
        ])
    if node_id and email:
        fields.append(Field("邮箱", str(email), key="email", hint="登录后在服务端修改",
                            group=GROUP_IDENTITY))
    if profile.get("role"):
        fields.append(Field("角色", str(profile.get("role") or "—"), key="role",
                            group=GROUP_IDENTITY))
    if twofa is not None:
        fields.append(Field("2FA 状态", _flag(twofa, "已开启", "未开启"), key="twofa",
                            group=GROUP_IDENTITY))
    if can_bind:
        fields.append(Field("网页端", facts.WEB_CONSOLE_URL, key="web_console",
                            hint="在网页端 → 账户/节点 → 生成绑定码，然后粘贴到这里",
                            group=GROUP_BIND))
    # E（RC14 IA 拆分）：USDT 收款地址**只在这里**（原来在「接入配置」，两处都能填、
    # 两处都以为生效）。输入框不预填掩码（prefill=False），否则一点保存就把掩码写回配置。
    payout = _payout_state(access)
    fields.append(Field("USDT 收款地址", payout["local"], key="usdt_address", editable=True,
                        prefill=False,
                        hint=(payout_mod.CHAIN_HINT + "｜本机已存：" + payout["local"]
                              + "｜云端：" + payout["hint"]),
                        group=GROUP_PAYOUT))
    # 原来「USDT 收款地址（云端）」是首排第 4 张卡：和身份卡并排，等于把「收款」又和
    # 「身份/绑定」混在一屏。现在降成收款区里的一行只读状态，整页只剩 3 张身份卡。
    fields.append(Field("云端收款状态", payout["state"], key="payout_state",
                        hint=payout["meta"], tone=payout["tone"],
                        group=GROUP_PAYOUT))
    # RC14 C 设备码状态（开关关 → 整段不渲染，连状态都不显示）
    device = account.get("device_flow") if isinstance(account.get("device_flow"), Mapping) else {}
    device_on = bool(device.get("enabled"))
    device_state = str(device.get("state") or "idle")
    if device_on and can_bind:
        from .. import device_flow as flow_mod

        state_text = flow_mod.STATE_TEXT.get(device_state, device_state)
        detail = "｜".join(item for item in (
            str(device.get("user_code") or ""),
            "等待第 " + str(device.get("attempts") or 0) + " 次确认" if device_state == "pending" else "",
            str(device.get("reason") or "")) if item)
        fields.append(Field("设备码", detail or "未申请", key="device_state",
                            group=GROUP_BIND,
                            hint=(("打开 " + str(device.get("verify_url") or "确认链接")
                                   + " 并在网页里点「确认」；本机每 5/15/30 秒自动问一次，10 分钟停")
                                  if device_state == "pending" else
                                  "状态：" + state_text + "（"
                                  + (str(device.get("hint") or "") or "点左边按钮开始") + "）")))
    fields.append(Field("会话", "本机窗口", key="session", hint="关闭窗口不会退出登录",
                        group=GROUP_IDENTITY))
    actions: list[Action] = []
    # RC14 C 设备码：主按钮 = 「在浏览器确认绑定」（开关关着 → 只保留老的手输码入口）
    if device_on and can_bind:
        actions.append(Action("account.bind_device", "在浏览器确认绑定", primary=True,
                              enabled=device_state not in ("pending",),
                              reason=("正在等待网页确认（" + str(device.get("user_code") or "")
                                      + "）：确认后本机会自动接上；要重来先点「停止等待」"
                                      if device_state == "pending" else ""),
                              group=GROUP_BIND))
        if device_state == "pending":
            actions.append(Action("account.bind_device_cancel", "停止等待", group=GROUP_BIND))
            if device.get("verify_url"):
                actions.append(Action("account.copy_verify_url", "复制确认链接",
                                      group=GROUP_BIND))
    if can_bind:
        actions.append(Action("account.bind", "绑定网页端" if not device_on else "手输绑定码",
                             primary=not device_on, group=GROUP_BIND))
    # G1-2（RC14 P0）：2FA 不在本机做绑定 ⇒ ①禁用态 + 说清原因；②旁边给**真能点**的入口。
    # 旧实现是一颗可点的按钮，点下去只回一句「暂不可用」——客户会以为客户端坏了。
    actions.append(Action("account.enable_2fa", "启用双因素验证", enabled=False,
                          reason=("已开启（服务端）：如需关闭或换设备，去网页控制台安全页"
                                  if twofa is True else
                                  "在网页控制台开启（TOTP）；本机不代理 2FA 绑定"),
                          group=GROUP_IDENTITY))
    # 已开启也要给入口：客户要关闭 / 换设备 / 重装 TOTP 时同样得去这一页
    actions.append(Action("account.open_2fa_page",
                          "去控制台管理 2FA" if twofa is True else "去控制台开启 2FA",
                          primary=not can_bind and twofa is not True,
                          group=GROUP_IDENTITY))
    # ── G4（RC14 P0）：更新检查入口 + 九状态下的可操作项
    # 放在「账户与安全」而不是总览：低频操作，且本页已有「网页端」等外链字段。
    update = account.get("update") if isinstance(account.get("update"), Mapping) else {}
    update_unavailable = str(update.get("unavailable") or "")
    checked_at = str(update.get("checked_at") or "")
    verify_state = str(update.get("verify_state") or "")
    state_text = UPDATE_STATE_TEXT.get(verify_state) or (
        "尚未检查过" if not verify_state else verify_state)
    # D2（2026-11-18 客户报障）：版本卡必须写**实际在跑的程序版本**。
    # 旧实现读 ``current_version``（= 安装记录），手工解压换包不会写记录，于是
    # 侧栏 v0.3.11 / 本条 0.3.0 自相矛盾。记录值只在提示里出现（并且明说已校正），
    # 后端字段 ``package_version`` / ``install_record_version`` 见 ``update.status()``。
    package_version = str(update.get("package_version") or update.get("current_version") or "")
    recorded_version = str(update.get("install_record_version") or "")
    record_stale = bool(package_version and recorded_version
                        and recorded_version != package_version)
    version_hint = (str(update.get("channel") or "") + " 渠道 · "
                    + (str(update.get("edition") or "") or "本机"))
    if record_stale:
        version_hint += "（安装记录为 " + recorded_version + "，已按实际程序显示）"
    fields.extend([
        Field("当前版本", package_version or "暂不可用",
              key="update_version",
              hint=version_hint,
              tone="amber" if record_stale else "muted",
              group=GROUP_IDENTITY),
        Field("上次检查", checked_at or "还没检查过", key="update_checked_at",
              group=GROUP_IDENTITY),
        Field("上次结果", (update_unavailable or state_text), key="update_state",
              hint=str(update.get("hint") or ""),
              tone="amber" if update_unavailable else "muted",
              group=GROUP_IDENTITY),
    ])
    update_actions: list[Action] = [Action("update.check", "检查更新",
                                           enabled=not update_unavailable,
                                           reason=update_unavailable or "",
                                           group=GROUP_IDENTITY)]
    if verify_state == "eligible":
        update_actions.append(Action("update.apply", "安装更新（重启生效）", primary=True,
                                     group=GROUP_IDENTITY))
    elif verify_state == "no_artifact":
        # 只写「暂无安装包」客户会以为「没有更新」→ 必须给一条能点的路（G4-4）
        update_actions.append(Action("update.download", "去下载页手动下载", primary=True,
                                     group=GROUP_IDENTITY))
    actions.extend(update_actions)
    actions.append(Action("account.submit_payout", "提交收款地址到云端",
                          enabled=payout["can_submit"], group=GROUP_PAYOUT,
                          reason="" if payout["can_submit"] else
                          ("云端收款地址功能还没开放（等平台开启后再提交）"
                           if payout["disabled"] else
                           ("先填好 Polygon 收款地址再点这里提交"
                            if bound is True else
                            "节点还没绑定账户：先完成上面的绑定"))))
    # RC14 B：引导重跑入口**永久**在本页（不是只在首次启动出现）；开关关 → 隐藏
    onboarding = access.get("onboarding") if isinstance(access.get("onboarding"), Mapping) else {}
    if onboarding.get("enabled"):
        actions.append(Action("account.rerun_onboarding", "重新运行首次引导",
                              group=GROUP_IDENTITY))
    # 解绑 = 原来「接入配置」页那颗「粘贴绑定码绑定」的对偶：绑定与解绑都只在本页。
    # 键名保留 ``account.logout``（接口只加不改）：它就是「解绑 + 清本机密钥」那一个动作。
    actions.append(Action("account.logout", "解绑这台电脑（登出并清本机密钥）",
                          enabled=bool(security.get("node_id")),
                          reason=("尚未登录" if not node_id
                                  else "本机无凭据（解绑要清本机密钥）"),
                          group=GROUP_IDENTITY))
    if can_bind:
        actions.append(Action("account.open_web", "打开网页端", group=GROUP_IDENTITY))
    notes = [str(account.get("note") or "未登录时只显示本机信息。"),
             ("收款地址只支持 Polygon（ERC20，0x 开头 42 位）；TRC20 暂不支持"
              "（云端出款链只有 Polygon）。"),
             payout_mod.summarize(payout["row"]) if payout["ok"] else ""]
    if verify_state == "failed_verify":
        notes.insert(0, "上次更新校验失败，已拒装（安全拦截）；版本未变，请联系客服核查更新源。")
    elif verify_state == "no_artifact":
        notes.insert(0, "本平台暂无安装包——这不是「没有更新」，请点「去下载页手动下载」。")
    elif verify_state == "not_eligible":
        notes.insert(0, "上次未命中灰度桶（当前 " + str(update.get("bucket") or "?")
                     + "%），本轮不更新；下一波会自动再检查。")
    if can_bind:
        notes = [
            # RC15-039 #11：绑定是**界面内动作**（「手输绑定码」按钮弹输入框，界面自己调本机命令），
            # 不再甩半条 CLI 命令当解决方案。
            "绑定：在网页端 → 账户/节点 → 生成绑定码，回到这一页点「手输绑定码」"
            "（或「绑定网页端」）把码粘进去。",
            "网页端：" + facts.WEB_CONSOLE_URL,
        ] + notes
    if bound is False and (bound_node_id or security.get("node_id")):
        # RC15-039 #33：一句话说清三个状态的关系，别让「未绑定 / 在线 / 有凭据」互相打脸。
        notes = [
            "「未绑定」只说网页端账号还没绑到这个节点；节点本身是存在的"
            "（节点 ID 见「账户」卡，本机凭据见「本机凭据」卡）。"
        ] + notes
    return View(
        key="account", title="账户与安全",
        subtitle="节点身份 · 绑定 · 双因素 · 登出",
        # 2026-11-19：首排只留**身份**三张卡。USDT 云端状态挪进「收款区」（见上），
        # 否则收款信息又和身份并排出现，等于把刚分开的两件事重新混在一屏。
        cards=(Card("账户", identity, identity_meta, "default"), second,
               Card("本机凭据", str(security.get("storage") or "—"),
                    str(security.get("node_id") or "本机未保存"), "muted")),
        fields=tuple(fields),
        actions=tuple(actions),
        unavailable=unavailable,
        notes=tuple(notes),
    )


# ─────────────────────────────────────────────────────────── RC14 B 首次引导（应用内面板）
@dataclass(frozen=True)
class OnboardingPanel:
    """首启引导的**投影**（无 Tk）：标题 / 正文 / 步骤序号 / 动作。

    应用层把它画成主界面上方的一条面板（**不是模态**：不遮挡、不拦点击），并直接复用
    同一套 :class:`Action`（键名与其它页一致，点下去仍走 controller.action）。
    """

    step: int                  # 1..3；4 = 完成页
    total: int
    title: str
    body: str
    actions: tuple[Action, ...] = ()

    @property
    def done(self) -> bool:
        return self.step > self.total


def onboarding_panel(step: int, *, total: int = 3, bound: bool | None = None,
                     upstream_count: int = 0) -> OnboardingPanel:
    """按步骤给面板（跳过按钮在**每一步**都有：这是 owner 的硬要求）。"""
    step = max(1, min(total + 1, int(step)))
    from .. import onboarding as onboarding_mod

    if step > total:
        marks = []
        marks.append("✓ 已绑定" if bound is True else
                     "· 还没绑定（随时可在「账户与安全」绑定）")
        marks.append("✓ 已提供 " + str(upstream_count) + " 个接入"
                     if upstream_count else "· 还没提供上游（随时可在「提供 Token Plan」添加）")
        return OnboardingPanel(
            step=step, total=total, title=onboarding_mod.DONE_TITLE,
            body="  ".join(marks),
            actions=(Action("onboarding.finish", "进入主界面", primary=True),
                     Action("onboarding.rerun", "重新运行引导")))
    actions = [Action("onboarding.next", "开始" if step == 1 else "下一步", primary=True)]
    if step == 2:
        actions.append(Action("account.bind", "在浏览器确认绑定"))
    if step == 3:
        actions.append(Action("token_plan.add_upstream", "去填接入"))
    actions.append(Action("onboarding.skip", "跳过这一步"))
    if step == 1:
        actions.append(Action("onboarding.skip_all", "跳过设置，直接进入"))
    if step > 1:
        actions.append(Action("onboarding.back", "上一步"))
    return OnboardingPanel(
        step=step, total=total, title=onboarding_mod.step_title(step),
        body=onboarding_mod.step_body(step), actions=tuple(actions))


#: 日志级别 → 客户看到的筛选文案（值仍是契约里的 ``debug``/``info``/…）
LOG_LEVEL_TEXT = {"all": "全部级别", "debug": "debug（调试）", "info": "info（信息）",
                  "warning": "warning（警告）", "error": "error（错误）",
                  "critical": "critical（严重）"}


def dataclass_is_instance(value: Any) -> bool:
    """``is_dataclass`` 对**类**也回真；这里只认实例。"""
    return not isinstance(value, type) and is_dataclass(value)


def _content_key(prefix: str, parts: Any, seen: dict[str, int]) -> str:
    """列表项的内容身份：``<prefix>-<内容哈希>``；**逐字相同**的行才补 ``#2``。

    用途：Treeview 的 iid 必须唯一（撞了会 TclError 打死窗口），但 id 又不能用序号，
    否则上面插一行、下面全变。两行内容逐字相同时它们本来不可区分，计数器是唯一选择。
    """
    digest = hashlib.sha1("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:10]
    key = prefix + "-" + digest
    if key in seen:
        seen[key] += 1
        return "%s#%d" % (key, seen[key])
    seen[key] = 0
    return key


def _logs(snapshot: Snapshot) -> View:
    logs = _section(snapshot, "logs")
    total = logs.get("total")
    rows = []
    seen: dict[str, int] = {}
    for index, row in enumerate(logs.get("rows") if isinstance(logs.get("rows"), list) else []):
        if not isinstance(row, Mapping):
            continue
        level = str(row.get("level") or "unknown")
        mark = (MARK_FAIL if level in ("error", "critical") else
                MARK_WAIT if level == "warning" else
                MARK_UNKNOWN if level == "unknown" else MARK_IDLE)
        tone = ("red" if level in ("error", "critical") else
                "amber" if level == "warning" else
                "muted" if level == "unknown" else "default")
        # RC15-039 #5：错误行自带的原因/下一步**原样上屏**（`_parse_log_line` 已把它们
        # 摊在 row["fields"] 里）。没有就写「—」，绝不拿别的东西冒充一句原因。
        payload = row.get("fields") if isinstance(row.get("fields"), Mapping) else {}
        reason = str(payload.get("reason") or "").strip()
        next_step = str(payload.get("next_step") or "").strip()
        # key = **内容身份**，不许带序号（复核 MF-1：`logs-%04d-<ts>` 里的序号会让
        # 顶部插一行时下面每行的 a11y 路径全变，读屏焦点跟着跳到别人身上）。
        # 仍然必须唯一：同一微秒可能被写两遍（init 与 daemon 各写一遍同一事件），
        # 完全相同的两行只能用「同内容计数器」区分——那是这类行唯一的不稳定点，已写进
        # `a11y/tree._row_id` 的文档。
        rows.append(Row((mark + " " + level, str(row.get("ts") or "—"),
                         str(row.get("event") or "—"),
                         reason or "—", next_step or "—"),
                        key=_content_key("logs", (level, str(row.get("ts") or ""),
                                                  str(row.get("event") or ""),
                                                  reason, next_step), seen),
                        tone=tone))
    level = str(logs.get("level") or "all")
    levels = [str(item) for item in (logs.get("levels") or []) if str(item)]
    options = (("all", LOG_LEVEL_TEXT["all"]),) + tuple(
        (item, LOG_LEVEL_TEXT.get(item, item)) for item in levels)
    filtered = level != "all"
    count_meta = ("共 " + str(total) + " 行 · 已筛选" if filtered and _is_number(total)
                  else "共 " + str(total) + " 行" if _is_number(total) else "尾部 200 行")
    # RC15-039 复核 M4：筛到 0 行时旧文案写「日志文件尚未生成」——同屏卡片却写着「共 N 行」，
    # 屏幕上的事实性错误。行的有无有三种原因，文案必须分开说，不许拿「文件没生成」兜一切。
    if rows:
        empty_text = _empty_text(snapshot, "logs", "日志文件尚未生成")
    elif filtered:
        # 级别名用**裸名**（`LOG_LEVEL_TEXT` 里是「debug（调试）」，套进括号会变成套娃）
        empty_text = ("当前筛选（" + level + "）下没有日志行；换一个级别，或选「"
                      + LOG_LEVEL_TEXT["all"] + "」。")
    elif _is_number(total) and int(total) > 0:
        empty_text = "日志里有 " + str(total) + " 行，但没能解析出可显示的行（详情见日志文件）。"
    else:
        empty_text = "日志文件尚未生成（本机还没有任何日志行）。"
    return View(
        key="logs", title="日志",
        subtitle="本机日志 · 本页显示最近 200 行（级别筛选只改显示）",
        cards=(Card("行数", _count(snapshot, "logs", str(len(rows))), count_meta, "default"),),
        # 窄窗先让出「原因/下一步」（正文在下面详情抽屉里，点一行就能看全）
        table=Table(("级别", "时间", "事件", "原因", "下一步"), tuple(rows),
                    note=str(logs.get("note") or ""),
                    empty=empty_text,
                    narrow_hide=("时间", "原因", "下一步"),
                    narrow_headers=(("下一步", "下一步"),), title="日志"),
        filters=(Filter("log_level", "级别", options=options, value=level,
                        action="logs.filter",
                        hint="只改本页显示；日志文件不动。"),) if levels else (),
        actions=(Action("logs.copy", "复制当前筛选", enabled=bool(rows), reason="没有日志行"),
                 Action("logs.open_dir", "打开日志目录")),
        unavailable=_missing(snapshot, ("logs",)),
        # 旧页脚许诺「级别筛选：all / debug / …」却**没有**任何筛选控件（走查原话：
        # 「页脚承诺的功能根本不存在」）。现在筛选是真的，页脚只说它真做了什么。
        notes=("筛选只改本页显示；日志文件本身一个字都不动。",),
    )


#: 数据域 → 构建器
_BUILDERS = {
    "overview": _overview, "bench": _bench, "models": _models, "gpu": _gpu,
    "tasks": _tasks, "receipts": _receipts, "earnings": _earnings, "agent": _agent,
    "token_plan": _token_plan, "access": _access, "yield": _yield, "account": _account,
    "logs": _logs,
}


def build_view(key: str, snapshot: Snapshot) -> View:
    """构建单个视图；未知 key 抛 ``ValueError``（拼错不静默给空页）。

    出口处统一过一遍 :func:`terminalize`（RC15-039 #11）：CLI 的 ``next_step`` 无论从哪个
    域来（access / bench / daemon…），上屏前都会带上「这条要在终端里跑」。
    """
    builder = _BUILDERS.get(str(key or ""))
    if builder is None:
        raise ValueError("未知视图：" + str(key))
    return _terminalize_tree(builder(snapshot))


def build_views(snapshot: Snapshot) -> dict[str, View]:
    """一次构建 13 个视图（顺序同 :data:`VIEWS`）。"""
    return {key: build_view(key, snapshot) for key in VIEW_KEYS}
