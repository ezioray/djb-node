# SPDX-License-Identifier: Apache-2.0
"""``View`` → MSAA 节点树（**纯投影**，无平台依赖、无 IO、可快照断言）。

为什么单独一层
==============

读屏提供方（:mod:`djb_node.desktop.a11y.msaa`）只在 Windows 存在，但「哪些控件该被读到、
念出来的第一句是什么、表格怎么截断」这些**口径**必须能在 Linux 上断言。所以这里只把
:class:`~djb_node.desktop.views.View` 翻成 :class:`Node` 树，不碰 COM、不碰 Tk。

四条硬口径
==========

* **只读镜像，但别把「能改」说成「只读」**：``Field.editable`` 为真 → ``TEXT + FOCUSABLE``
  （能 Tab 进去、能读当前值）；否则 ``TEXT + READONLY``；``Field.secret`` 另加
  ``STATE_SYSTEM_PROTECTED``。本模块不提供任何写回路径，输入仍走真正的 Tk 输入框。
* **id 是路径键**：每个节点带 :attr:`Node.node_id`（``view/table:1/row:2026-11-16-003/cell:金额``
  这种），**不随筛选或刷新改变**；数字 childId 由 :func:`flatten` 按前序给，只用于本次遍历。
  提供方按 ``node_id`` 保留 id，刷新前后同一行仍是同一个 id。
* **行名用内容，不用位置**：「第 3 行」筛完会变成「第 1 行」，读屏念出来就是错的。
  行名取首列内容；位置信息放 :attr:`Node.description`（或不写）。
* **规模上限**：单表最多 :data:`TABLE_MAX_ROWS` 行（读屏逐条遍历会卡），超出时表节点名字里
  写「共 N 行，显示前 M 行」，并在表后补一条同样的 :data:`ROLE_STATIC` 节点，保证顺序遍历必读到。
"""
from __future__ import annotations

import hashlib

from dataclasses import dataclass, replace
from typing import Any

from ..views import Action, Card, Chart, Field, Filter, Hero, RailCard, Table, Toggle, View

__all__ = [
    "NODE_STATES",
    "ROLE_ACTION",
    "ROLE_CARD",
    "ROLE_CELL",
    "ROLE_COLUMNHEADER",
    "ROLE_FILTER",
    "ROLE_PAGE",
    "ROLE_ROW",
    "ROLE_STATIC",
    "ROLE_TABLE",
    "ROLE_TEXT",
    "ROLE_TOGGLE",
    "STATE_CHECKED",
    "STATE_FOCUSABLE",
    "STATE_PROTECTED",
    "STATE_READONLY",
    "STATE_SELECTABLE",
    "STATE_SELECTED",
    "STATE_UNAVAILABLE",
    "TABLE_MAX_ROWS",
    "Node",
    "build_tree",
    "flatten",
    "flatten_for_provider",
    "ids_by_key",
]

# ── MSAA 角色（`roleti.h`；只列这里用得到的） ──────────────────────────────────
ROLE_PAGE = 0x0000000A          # ROLE_SYSTEM_CLIENT
ROLE_TABLE = 0x00000013         # ROLE_SYSTEM_TABLE
ROLE_CARD = 0x00000014          # ROLE_SYSTEM_GROUPING
ROLE_COLUMNHEADER = 0x00000019  # ROLE_SYSTEM_COLUMNHEADER
ROLE_ROW = 0x0000001C           # ROLE_SYSTEM_ROW
ROLE_CELL = 0x0000001D          # ROLE_SYSTEM_CELL
ROLE_TEXT = 0x00000028          # ROLE_SYSTEM_TEXT
ROLE_STATIC = 0x00000029        # ROLE_SYSTEM_STATICTEXT
ROLE_ACTION = 0x0000002B        # ROLE_SYSTEM_PUSHBUTTON
ROLE_TOGGLE = 0x0000002C        # ROLE_SYSTEM_CHECKBUTTON
ROLE_FILTER = 0x0000002E        # ROLE_SYSTEM_COMBOBOX

# ── MSAA 状态位（`oleacc.h` STATE_SYSTEM_*） ──────────────────────────────────
STATE_UNAVAILABLE = 0x00000001
STATE_SELECTED = 0x00000002
STATE_FOCUSED = 0x00000004
STATE_CHECKED = 0x00000010
STATE_READONLY = 0x00000040
STATE_FOCUSABLE = 0x00100000
STATE_SELECTABLE = 0x00200000
STATE_PROTECTED = 0x20000000

#: 名字 → 状态位（证据/测试里按名字断言，避免到处写魔数）
NODE_STATES: dict[str, int] = {
    "unavailable": STATE_UNAVAILABLE,
    "selected": STATE_SELECTED,
    "focused": STATE_FOCUSED,
    "checked": STATE_CHECKED,
    "readonly": STATE_READONLY,
    "focusable": STATE_FOCUSABLE,
    "selectable": STATE_SELECTABLE,
    "protected": STATE_PROTECTED,
}

#: 单张表最多暴露多少行（读屏逐条遍历会卡；够翻一屏、总数另外说明）
TABLE_MAX_ROWS = 50


@dataclass(frozen=True)
class Node:
    """一个读屏可见节点。

    ``node_id`` 是**路径键**（跨刷新稳定）；``key`` 只在有动作语义的元素上填
    （字段 key / 行 key / 动作 key），S3 拿它做 ``accSelect`` / ``accDoDefaultAction`` 的落点。
    """

    role: int
    name: str
    value: str = ""
    description: str = ""
    state: int = 0
    key: str = ""
    node_id: str = ""
    children: tuple["Node", ...] = ()

    def child_count(self) -> int:
        return len(self.children)


def _seg(segment: str) -> str:
    """路径段：斜杠换成竖线（名字里本来就可能带「让路 / 节能」这种）。"""
    return str(segment).replace("/", "|")


def _text(label: str, value: str = "", description: str = "", state: int = 0) -> Node:
    return Node(ROLE_STATIC, label, value, description, state)


def _card(card: Card) -> Node:
    return Node(ROLE_CARD, card.title, card.value, card.meta, node_id=_seg("card:" + card.title))


def _hero(hero: Hero) -> Node:
    children: list[Node] = []
    if hero.chip:
        children.append(_text(hero.chip))
    if hero.body:
        children.append(_text(hero.body))
    if hero.gauge is not None:
        children.append(_text(hero.gauge.label or "进度", hero.gauge.value))
    for metric in hero.metrics:
        value = metric.value + ((" " + metric.unit) if metric.unit else "")
        children.append(_text(metric.label, value, metric.note))
    for action in hero.actions:
        children.append(_action(action))
    if hero.runtime:
        children.append(_text(hero.runtime_label, hero.runtime))
    if hero.hint:
        children.append(_text(hero.hint))
    if hero.note:
        children.append(_text(hero.note))
    return Node(ROLE_CARD, hero.title, hero.body, hero.kicker, node_id="hero",
                children=tuple(children))


def _chart(chart: Chart) -> Node:
    """柱图不逐柱建节点（读屏会把每根柱子的数字念一遍），只给一句摘要。"""
    if chart.empty:
        return _text("图表：" + (chart.title or chart.kicker or "时序"), chart.empty)
    count = len(chart.values)
    name = "图表：" + (chart.title or chart.kicker or "时序")
    value = "共 " + str(count) + " 根柱"
    if chart.peak:
        value += "，" + chart.peak
    return _text(name, value, chart.caption)


def _rail_card(rail: RailCard) -> Node:
    children: list[Node] = []
    if rail.badge:
        children.append(_text(rail.badge))
    for stat in rail.stats:
        value = stat.value + ((" " + stat.unit) if stat.unit else "")
        children.append(_text(stat.label, value, stat.note))
    for item in rail.fields:
        children.append(_field(item))
    for label, value, tone in rail.rows:
        children.append(_text(label, value, tone if tone not in ("", "default") else ""))
    if rail.toggle is not None:
        label, value = rail.toggle
        children.append(Node(ROLE_TOGGLE, label, state=STATE_CHECKED if value else 0))
    if rail.progress is not None:
        label, _ratio, text = rail.progress
        children.append(_text(label, text))
    for action in rail.actions:
        children.append(_action(action))
    if rail.foot:
        children.append(_text(rail.foot))
    return Node(ROLE_CARD, rail.title, "", rail.note, node_id=_seg("rail:" + rail.title),
                children=tuple(children))


def _field(item: Field) -> Node:
    """字段是**只读镜像**，但「能不能改」必须说对：

    * ``editable=True`` → ``TEXT + FOCUSABLE``（Tab 能进去、能读当前值）；
    * ``editable=False`` → ``TEXT + READONLY``；
    * ``secret=True`` → 另加 ``PROTECTED``（值本来就是掩码，不额外处理）。
    """
    state = STATE_FOCUSABLE if item.editable else STATE_READONLY
    if item.secret:
        state |= STATE_PROTECTED
    description = item.hint
    if item.error:
        description = (description + "；" if description else "") + "错误：" + item.error
        state |= STATE_UNAVAILABLE
    return Node(ROLE_TEXT, item.label, item.value, description, state, key=item.key,
                node_id=_seg("field:" + (item.key or item.label)))


def _filter(item: Filter) -> Node:
    return Node(ROLE_FILTER, item.label, item.current_text(), item.hint,
                STATE_READONLY, key=item.key, node_id=_seg("filter:" + (item.key or item.label)))


def _toggle(item: Toggle) -> Node:
    return Node(ROLE_TOGGLE, item.label, "", item.hint,
                (STATE_FOCUSABLE | STATE_CHECKED) if item.value else STATE_FOCUSABLE,
                key=item.key, node_id=_seg("switch:" + (item.key or item.label)))


def _action(item: Action) -> Node:
    state = STATE_FOCUSABLE if item.enabled else (STATE_FOCUSABLE | STATE_UNAVAILABLE)
    return Node(ROLE_ACTION, item.label, "", item.reason, state, key=item.key,
                node_id=_seg("action:" + (item.key or item.label)))


def _row_id(row: Any, index: int) -> str:
    """行 id = **业务身份**，不许含序号。

    优先级：``Row.key``（收据号/任务号）→ 全部单元格内容的短哈希 → 真正的空行才退序号。

    * 为什么首列不够：日志页首列是级别（``WARN``），好几行一样 ⇒ 路径撞车 ⇒ 上层按出现顺序
      补 ``#2``/``#3``，序号就混进 id 里了——顶部插一行，下面每行的 id 全变（真机复现：
      同一收据 `8→10`）。
    * 内容哈希对「同内容两行」仍会撞（它们本来也分不出来），此时上层补的 ``#n`` 后缀就是
      唯一的不稳定点，已在 :func:`build_tree` 文档里写明。
    """
    if row.key:
        return str(row.key)
    cells = tuple(str(cell) for cell in row.cells)
    if any(cells):
        digest = hashlib.sha1("\x1f".join(cells).encode("utf-8")).hexdigest()[:8]
        return "h:" + digest
    return "#" + str(index + 1)                  # 真·空行：无身份可言，见文档说明


def _table(table: Table, ordinal: int = 0, *, max_rows: int = TABLE_MAX_ROWS
           ) -> tuple[Node, Node | None]:
    """一张表 → (表节点, 截断说明节点或 None)。

    表节点名字里带总数（读屏进表时会念），截断时再补一条静态文本节点——顺序遍历也能读到。
    行名用**首列内容**（「第 i 行」会随筛选变化，念出来是错的），位置落在 ``description``。
    """
    total = len(table.rows)
    shown = table.rows[:max_rows]
    truncated = total > len(shown)

    base = _seg("table:" + table.title) if table.title else _seg(
        "table" + ((":" + str(ordinal + 1)) if ordinal else ""))
    # 压平后每一行是「单元格用 | 拼起来」，用户听不出哪列是哪列——所以表节点自己的名字里
    # 必须带**列序 + 总行数**：读屏进表格先念这一句，后面每行才能对号入座。
    label = table.title or ("表格" + ((" " + str(ordinal + 1)) if ordinal else ""))
    name = label if label.startswith("表格") else ("表格：" + label)
    if table.columns and total:
        name += "（列：" + " | ".join(table.columns) + "）"
    if total:
        name += "，共 " + str(total) + " 行" + ("，显示前 " + str(len(shown)) + " 行" if truncated else "")

    head = Node(ROLE_ROW, "表头",
                children=tuple(Node(ROLE_COLUMNHEADER, column, node_id=_seg("col:" + column))
                               for column in table.columns),
                node_id="head")
    children: list[Node] = [head]
    for index, row in enumerate(shown):
        cells = tuple(Node(ROLE_CELL, cell,
                           node_id=_seg("cell:" + (table.columns[i] if i < len(table.columns)
                                                   else str(i))))
                      for i, cell in enumerate(row.cells))
        row_name = row.cells[0] if row.cells else ""
        description = row.note
        state = STATE_SELECTABLE | (STATE_UNAVAILABLE if row.disabled else 0)
        children.append(Node(ROLE_ROW, row_name, description=description, state=state,
                             key=row.key, node_id=_seg("row:" + _row_id(row, index)),
                             children=cells))
    if not shown:
        children.append(_text(table.empty))

    node = Node(ROLE_TABLE, name, table.note, node_id=base, children=tuple(children))
    note = None
    if truncated:
        note = _text("表格" + ((" " + str(ordinal + 1)) if ordinal else "") + "共 " + str(total)
                     + " 行，显示前 " + str(len(shown)) + " 行")
    return node, note


def build_tree(view: View, *, max_rows: int = TABLE_MAX_ROWS) -> Node:
    """一页 ``View`` → 树。
    # 稳定性契约（#31，2026-11-16 复核 MUST-FIX 后收紧）

    * 行 id = ``Row.key`` → 全单元格内容哈希 → （仅真·空行）序号；
    * 因此**顶部插入/删除一行不会改动其它行的 id**，正则筛选也不会；
    * 唯一不稳定：两行内容**逐字相同**且都没有 ``key``——它们本来不可区分，上层补 ``#2``/``#3``
      后缀，删掉其中一行会让幸存者换后缀。这类表在 ``View`` 里应给 ``Row.key``。
    """
    """一页 :class:`View` → 一棵 :class:`Node` 树（``node_id`` 已填成绝对路径键）。

    顺序与 :meth:`View.plain_text` 一致（不可用提示 → 卡片 → 主卡 → 图 → 右栏 → 开关 →
    筛选 → 表格 → 字段 → 动作 → 备注），这样「读屏读到的顺序」和证据里的文本快照能一一对上。
    """
    title = view.title + (("  ·  " + view.subtitle) if view.subtitle else "")
    children: list[Node] = []
    for item in view.unavailable:
        children.append(_text(item))
    for card in view.cards:
        children.append(_card(card))
    if view.hero is not None:
        children.append(_hero(view.hero))
    if view.chart is not None:
        children.append(_chart(view.chart))
    for rail in view.rail:
        children.append(_rail_card(rail))
    for switch in view.switches:
        children.append(_toggle(switch))
    for flt in view.filters:
        children.append(_filter(flt))
    tables = ((view.table,) if view.table is not None else ()) + tuple(view.tables)
    for ordinal, table in enumerate(tables):
        node, note = _table(table, ordinal if len(tables) > 1 else 0, max_rows=max_rows)
        children.append(node)
        if note is not None:
            children.append(note)
    for item in view.fields:
        children.append(_field(item))
    for action in view.actions:
        children.append(_action(action))
    for index, note in enumerate(view.notes):
        children.append(_text(note))

    root = Node(ROLE_PAGE, title, "", "", STATE_READONLY, key=view.key,
                node_id=_seg("view:" + view.key), children=tuple(children))
    return _assign_ids(root)


def _assign_ids(node: Node, prefix: str = "", index: int = 0) -> Node:
    """把相对 ``node_id`` 拼成绝对路径键（``view:overview/card:今日贡献``）。

    没有显式 ``node_id`` 的子节点用序号占位（``.../hero/3``）——只为让 ``ids_by_key`` 唯一，
    真正需要跨刷新稳定的节点（卡片/行/字段/动作）都有业务键。
    """
    own = node.node_id or ("#" + str(index))
    absolute = (prefix + "/" + own) if (prefix and own) else (own or prefix)
    children = tuple(_assign_ids(child, absolute, i) for i, child in enumerate(node.children))
    if children == node.children and absolute == node.node_id:
        return node
    return replace(node, node_id=absolute, children=children)


def flatten_for_provider(root: Node) -> tuple[Node, ...]:
    """把树压成**一层**子元素——MSAA 跨进程只能可靠地传 childId。

    为什么必须压平：``get_accChild`` 交出去的是活的对象指针，跨进程要靠 COM 编组；
    自定义 COM 对象没有 ``IMarshal`` / 代理-桩，客户端拿到的是 NULL（2026-11-16 真机实测：
    服务端指针有效、客户端 ``get_accChild`` 返回 hr=0 但指针为空）。所以 0.3.9 的提供方
    只暴露「根 + 一层简单子元素」：每个子元素只有 childId，没有子对象。

    压平后仍保证读屏读到的东西**一字不少**：

    * 表格行 → ``ROLE_ROW``，名字 = 各单元格用 `` | `` 拼起来（单元格不再单独建节点）；
    * 表头 → ``ROLE_ROW`` 改名「表头：…」；
    * 其余节点（卡片/分组/按钮/字段/静态文本）原角色保留，只是不再嵌套。
    """
    flat: list[Node] = []

    def visit(node: Node) -> None:
        if node.role == ROLE_CELL:
            return                                   # 单元格文本已并进行名
        if node.role == ROLE_ROW:
            cells = [child.name for child in node.children]
            name = (" | ".join(cells)) if cells else node.name
            if node.name == "表头":
                name = "表头：" + name
            flat.append(replace(node, name=name, children=()))
            return
        if node.role in (ROLE_TABLE, ROLE_CARD):
            flat.append(replace(node, children=()))
        else:
            flat.append(replace(node, children=()))
        for child in node.children:
            visit(child)

    for child in root.children:
        visit(child)
    return tuple(flat)


def flatten(root: Node) -> list[tuple[int, Node]]:
    """前序展开成 ``(childId, node)``；根是 ``childId=0``（``CHILDID_SELF``），子从 1 起。

    childId 只对**本次遍历**有效；跨刷新要认 :attr:`Node.node_id`（见 :func:`ids_by_key`）。
    """
    flat: list[tuple[int, Node]] = [(0, root)]
    for node in root.children:
        _flatten_into(node, flat)
    return flat


def _flatten_into(node: Node, flat: list[tuple[int, Node]]) -> None:
    flat.append((len(flat), node))
    for child in node.children:
        _flatten_into(child, flat)


def ids_by_key(root: Node) -> dict[str, int]:
    """``node_id → childId``（提供方用它把「稳定的路径键」映射到本次遍历的 childId）。"""
    return {node.node_id: child_id for child_id, node in flatten(root)}
