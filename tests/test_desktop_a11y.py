# SPDX-License-Identifier: Apache-2.0
"""读屏投影（``djb_node.desktop.a11y``）：节点树口径 + 降级路径。

这里是**纯投影**断言：不装 COM、不画窗口，所以 Linux 上就能跑。
真机（oleacc 跨进程 walk / NVDA）证据在 ``reports/**/evidence/…/a11y/`` 里。
"""
from __future__ import annotations

import pathlib

import pytest

from djb_node.desktop import datasource as ds
from djb_node.desktop import views as vw
from djb_node.desktop.a11y import msaa
from djb_node.desktop.a11y.tree import (
    ROLE_ACTION,
    ROLE_CARD,
    ROLE_CELL,
    ROLE_COLUMNHEADER,
    ROLE_ROW,
    ROLE_STATIC,
    ROLE_TABLE,
    ROLE_TEXT,
    STATE_FOCUSABLE,
    STATE_PROTECTED,
    STATE_READONLY,
    STATE_UNAVAILABLE,
    TABLE_MAX_ROWS,
    build_tree,
    flatten,
    flatten_for_provider,
    ids_by_key,
)
from djb_node.desktop.app import demo_snapshot


def _nodes(view):
    return flatten(build_tree(view))


def _by_name(view, name):
    return [node for _id, node in _nodes(view) if node.name == name]


def _find(view, role=None, name=None):
    hits = [node for _id, node in _nodes(view)
            if (role is None or node.role == role) and (name is None or node.name == name)]
    assert hits, "没找到节点 role=%r name=%r" % (role, name)
    return hits[0]


# ── 一页一棵树：overview ──────────────────────────────────────────────────────
def test_overview_tree_mirrors_the_page_in_reading_order():
    view = vw.build_view("overview", demo_snapshot())
    nodes = _nodes(view)
    root = nodes[0][1]
    assert root.node_id == "view:overview"
    assert root.name.startswith("总览")
    # 卡片按页面上出现的顺序
    cards = [node.name for _id, node in nodes if node.role == ROLE_CARD]
    assert cards[:6] == ["今日贡献", "当前任务", "余额（可用）", "接单状态", "资源", "让路 / 节能"]
    # 让路卡片在总览里出现的档位文案，和 M7 的「只记录偏好」口径一致
    assert "本版仅记录档位偏好" in root_text(view)


def root_text(view):
    return "\n".join(node.name + " " + node.value for _id, node in _nodes(view))


def test_overview_table_exposes_header_and_rows():
    view = vw.build_view("overview", demo_snapshot())
    table = _find(view, role=ROLE_TABLE)
    # 压平后行是「单元格用 | 拼接」，所以表节点自己必须先说清列序 + 总行数
    assert table.name == "表格：最近收据（列：收据号 | 时间 | Tokens | 状态 | 本机校验），共 5 行"
    headers = [child.name for child in table.children[0].children]
    assert headers == ["收据号", "时间", "Tokens", "状态", "本机校验"]
    rows = [child for child in table.children[1:]]
    assert rows, "至少有一行数据"
    # 行名取首列内容（不是「第 i 行」—— 筛完就错）
    assert rows[0].name == rows[0].children[0].name
    assert all(not row.name.startswith("第 ") for row in rows)
    assert all(len(row.children) == len(headers) for row in rows)


def test_actions_report_disabled_state_instead_of_hiding():
    view = vw.build_view("overview", demo_snapshot())
    resume = _find(view, role=ROLE_ACTION, name="恢复接单")
    assert resume.state & STATE_UNAVAILABLE
    assert resume.description            # 为什么不能点，写在 description 里


# ── 表格截断：说清「共多少、给了多少」 ──────────────────────────────────────────
def test_big_table_is_capped_and_says_so_twice():
    rows = tuple(vw.Row(("r%03d" % i, "12:00", "4096", "✓ 已受理", "· 未校验"), key="r%03d" % i)
                 for i in range(TABLE_MAX_ROWS + 30))
    table = vw.Table(("收据号", "时间", "Tokens", "状态", "本机校验"), rows)
    view = vw.View(key="receipts", title="收据", subtitle="", cards=(), table=table)
    nodes = _nodes(view)
    node = _find(view, role=ROLE_TABLE)
    assert "共 %d 行" % len(rows) in node.name
    assert "显示前 %d 行" % TABLE_MAX_ROWS in node.name
    assert len(node.children) == 1 + TABLE_MAX_ROWS          # 表头 + 50 行
    # 顺序遍历也会读到同一句话
    assert any(n.role == ROLE_STATIC and "显示前 %d 行" % TABLE_MAX_ROWS in n.name
               for _id, n in nodes)


def test_table_name_carries_column_order_and_total():
    """压平唯一能补回列语义的地方：表名里的「（列：…）」+「共 N 行」。

    lead 的真机验收原话：行读出来是 `a | b | c`，用户不知道哪列是哪列，所以进表格第一句
    必须把列序念出来。
    """
    rows = tuple(vw.Row(("r%03d" % i, "12:00", "4096", "✓", "·"), key="r%03d" % i)
                 for i in range(3))
    table = vw.Table(("收据号", "时间", "Tokens", "状态", "本机校验"), rows, title="收据明细")
    view = vw.View(key="receipts", title="收据", subtitle="", cards=(), table=table)
    node = _find(view, role=ROLE_TABLE)
    assert node.name == "表格：收据明细（列：收据号 | 时间 | Tokens | 状态 | 本机校验），共 3 行"


def test_empty_table_says_empty_instead_of_fake_rows():
    table = vw.Table(("级别", "时间", "事件"), ())
    view = vw.View(key="logs", title="日志", subtitle="", cards=(), table=table)
    node = _find(view, role=ROLE_TABLE)
    assert node.children[1].role == ROLE_STATIC
    assert node.children[1].name == table.empty


# ── 字段：能改的别说是只读 ──────────────────────────────────────────────────────
def test_editable_field_is_focusable_not_readonly():
    view = vw.View(key="access", title="本机设置", subtitle="", cards=(), fields=(
        vw.Field("服务端地址", "https://api.example/v1", key="base_url", editable=True),
        vw.Field("节点凭证", "djb_ns_…4e11", key="node_secret", secret=True),
        vw.Field("价格", "跟随平台建议价", key="price_mode"),
    ))
    editable = _find(view, role=ROLE_TEXT, name="服务端地址")
    assert editable.state & STATE_FOCUSABLE
    assert not (editable.state & STATE_READONLY)
    assert editable.value == "https://api.example/v1"

    secret = _find(view, role=ROLE_TEXT, name="节点凭证")
    assert secret.state & STATE_READONLY
    assert secret.state & STATE_PROTECTED
    assert secret.value == "djb_ns_…4e11"          # 投影层已掩码，tree 不再二次处理

    readonly = _find(view, role=ROLE_TEXT, name="价格")
    assert readonly.state & STATE_READONLY
    assert not (readonly.state & STATE_FOCUSABLE)


# ── id 稳定：刷新前后同一行还是同一个 id ───────────────────────────────────────
def _logs_snapshot(entries):
    """走**真实**产品路径：`views._logs` 生成 Row.key，不在用例里手写 key。"""
    return ds.Snapshot({"logs": {"rows": [
        {"ts": ts, "level": level, "event": event, "reason": "", "next": ""}
        for event, ts, level in entries]}})


def _log_entries(*events):
    return [(event, "2026-11-16T10:%02d:00Z" % index, "info")
            for index, event in enumerate(events)]


def test_row_paths_survive_a_refresh_with_different_rows():
    """真实日志页：换一版数据（少一行）后，还在的行路径必须原样。"""
    first = vw.build_view("logs", _logs_snapshot(_log_entries("a", "b", "c")))
    second = vw.build_view("logs", _logs_snapshot(
        _log_entries("a", "b", "c")[:1] + _log_entries("a", "b", "c")[2:]))
    rows_first = {row.key for row in first.table.rows}
    rows_second = {row.key for row in second.table.rows}
    assert rows_first & rows_second, "两版之间应至少共有一行"
    assert len(rows_first) == len(first.table.rows), "key 必须唯一（Treeview iid 会撞）"


def test_flatten_is_preorder_and_ids_by_key_matches():
    view = vw.build_view("overview", demo_snapshot())
    root = build_tree(view)
    flat = flatten(root)
    assert [child_id for child_id, _node in flat] == list(range(len(flat)))
    assert flat[0][1] is root
    mapping = ids_by_key(root)
    assert mapping[root.node_id] == 0
    for child_id, node in flat:
        assert mapping[node.node_id] == child_id


# ── 图表不逐柱 ────────────────────────────────────────────────────────────────
def test_chart_is_one_summary_node_not_one_per_bar():
    view = vw.build_view("overview", demo_snapshot())
    chart_nodes = [node for _id, node in _nodes(view) if node.name.startswith("图表：")]
    assert len(chart_nodes) == 1
    assert "共 " in chart_nodes[0].value and "根柱" in chart_nodes[0].value
    assert not chart_nodes[0].children


# ── 降级：非 Windows / 逃生阀 / 装不上 ─────────────────────────────────────────
@pytest.mark.skipif(msaa.sys.platform == "win32", reason="Windows 上这条不适用")
def test_provider_is_a_noop_off_windows():
    assert msaa.available() is False
    assert msaa.attach(object()) is None


def test_escape_hatch_disables_provider(monkeypatch):
    monkeypatch.setenv(msaa.ENV_DISABLE, "0")
    assert msaa.available() is False


def test_provider_resolves_the_root_after_refresh():
    """根对象是 attach 时用空路径建的；refresh 后 ``""`` 必须解析成树根。

    真机现场：provider 装上了、AccessibleObjectFromWindow 也成功，但 accChildCount=0
    （根对象拿空路径查不到节点）——读屏看到一棵空树。
    """
    provider = msaa.Provider(root=None)
    view = vw.build_view("overview", demo_snapshot())
    provider.refresh(view)
    root = provider.node("")
    assert root is not None and root.node_id == "view:overview"
    kids = provider.children("")
    assert kids and kids[0].startswith("view:overview/")


def test_a11y_is_imported_statically_for_pyinstaller():
    """冻结 exe 的门禁（票面 §6.5 第 1 条）：`a11y` 一旦改成动态 import，
    PyInstaller 就收不进 exe，功能会**静默消失**——所以这里把 import 形态钉死。
    """
    import re
    source = (pathlib.Path(__file__).resolve().parents[1]
              / "djb_node" / "desktop" / "app.py").read_text(encoding="utf-8")
    assert re.search(r"^from \.a11y import ", source, re.MULTILINE), "app.py 必须静态 from .a11y import …"
    assert "import_module" not in source or "a11y" not in source.split("import_module")[1][:40], \
        "别把 a11y 改成 import_module（PyInstaller 看不见）"


def test_child_ids_are_dense_so_screen_readers_never_hit_a_hole():
    """MF-2：childId 必须密集 1..N，且 ``accChildCount == len(children)``。

    稀疏 id 的后果（真机实测）：日志筛 5→3 行后 `accChildCount=12` 而孩子只有 10 个，
    读屏按 1..count 遍历必撞空号（`accChild` 回 S_FALSE、`accName` 回 BADINDEX）。
    """
    view = vw.build_view("logs", _logs_snapshot(_log_entries("a", "b", "c", "d", "e")))
    provider = msaa.Provider(root=None)
    provider.refresh(view)
    kids = provider.children("")
    assert kids, "日志页应有子元素"
    assert [provider.child_id(path) for path in kids] == list(range(1, len(kids) + 1))
    for child_id in range(1, len(kids) + 1):
        assert provider.path_for_id(child_id) == kids[child_id - 1]
    assert provider.path_for_id(len(kids) + 1) == ""      # 越界不撞已有元素
    assert provider.path_for_id(0) == ""

    # 筛掉两行：孩子变少，但编号仍然 1..N 连续
    filtered = vw.build_view("logs", _logs_snapshot(_log_entries("a", "c", "e")))
    provider.refresh(filtered)
    kids = provider.children("")
    assert [provider.child_id(path) for path in kids] == list(range(1, len(kids) + 1))


def test_focus_follows_the_business_row_when_rows_shift():
    """MF-2 契约：**焦点跟着业务行走**（不是「数字不变」）。

    在顶部插一行 → 焦点那行整体下移 → `accFocus` 必须仍指向同一个业务路径，
    并且 provider 用**新的** childId 重发了焦点事件。
    """
    base = _log_entries("a", "b", "c")
    provider = msaa.Provider(root=None)
    provider.refresh(vw.build_view("logs", _logs_snapshot(base)))

    target_path = [path for path in provider.children("") if "/row:" in path]
    assert len(target_path) == 3, target_path
    focused_path = target_path[1]                       # 中间那一行
    provider.notify_focus(focused_path)
    before_id = provider.child_id(focused_path)
    provider._event_log.clear()

    provider.refresh(vw.build_view("logs", _logs_snapshot(
        [("new", "2026-11-16T09:00:00Z", "info")] + base)))   # 顶部插一行

    focused_paths = provider.focusable_paths()
    # 判据是**业务行**不是路径字符串：路径是身份，行名才是用户看到的那一行
    focused_node = provider._node_by_path.get(provider._focused_path)
    assert focused_node is not None, "刷新后焦点行不见了（身份丢了）"
    assert "10:01:00Z" in focused_node.name, focused_node.name   # 被聚焦的那一行
    assert provider.child_id(provider._focused_path) == before_id + 1, "位置动了，号就该跟着动"
    assert focused_paths, "可聚焦集合不能空"
    refires = [entry for entry in provider._event_log if entry[0] == 0x8005]
    assert refires, "位置变了必须重发 EVENT_OBJECT_FOCUS（否则读屏还以为是旧那一行）"
    assert refires[-1][1] == before_id + 1, refires[-1]
    assert refires[-1][2] == focused_path


def test_invalid_child_ids_are_tolerated_not_raised():
    """childId 的边界：0（自己）/ 超界 / 负数 都只许「查不到」，不许抛、不许乱指。"""
    provider = msaa.Provider(root=None)
    provider.refresh(vw.build_view("overview", demo_snapshot()))
    assert provider.path_for_id(0) == ""            # CHILDID_SELF 不是子元素
    assert provider.path_for_id(-3) == ""
    assert provider.path_for_id(10 ** 6) == ""
    assert provider.child_id("不存在的路径") == 0
    # 有效 id 必须能反查回来，且双向一致
    for path in provider.children("")[:5]:
        assert provider.path_for_id(provider.child_id(path)) == path


def test_every_acc_method_crosses_com_boundary_inside_try():
    """COM 边界契约：`acc*` / `get_acc*` / `put_acc*` 方法体必须整段在 try 里。

    跨 COM 边界抛异常会打断宿主的调用栈（读屏软件那边表现是「控件消失」）。这条用 AST 扫，
    新加方法忘了包 try 就直接红。
    """
    import ast
    source = (pathlib.Path(__file__).resolve().parents[1]
              / "djb_node" / "desktop" / "a11y" / "msaa.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    checked = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not (node.name.startswith(("acc", "get_acc", "put_acc"))):
            continue
        checked.append(node.name)
        assert any(isinstance(stmt, ast.Try) for stmt in node.body), \
            "%s 没有 try/except 包住方法体（跨 COM 边界不许抛）" % node.name
    for required in ("accSelect", "accDoDefaultAction", "put_accName", "put_accValue",
                     "get_accChild", "get_accName", "get_accState"):
        assert required in checked, "没扫到 %s（用例失效）" % required
    # 同一条链上也不许残留重复 except（复核 MUST-FIX #5）
    assert source.count('_error("accChild", exc)') == 1


def test_focus_and_selection_events_use_stable_child_ids():
    """焦点/选中跨 refresh 不丢：路径还在，记录就还在，且不会重发事件。"""
    provider = msaa.Provider(root=None)
    provider.refresh(vw.build_view("overview", demo_snapshot()))
    rows = provider.selectable_paths()
    assert len(rows) >= 3, rows
    target = rows[1]
    before = provider.child_id(target)
    provider.notify_focus(target)
    provider.select(target)

    provider.refresh(vw.build_view("overview", demo_snapshot()))   # 自动刷新
    assert provider.child_id(target) == before
    assert provider._focused_path == target
    assert provider._selection_path == target


def test_notify_focus_at_clamps_instead_of_raising():
    """越界的焦点位置就地夹住——焦点事件是锦上添花，不许把键盘路径带崩。"""
    provider = msaa.Provider(root=None)
    provider.refresh(vw.build_view("overview", demo_snapshot()))
    provider.notify_focus_at(10 ** 6)
    assert provider._focused_path == provider.focusable_paths()[-1]
    provider.notify_focus_at(-5)
    assert provider._focused_path == provider.focusable_paths()[0]


def test_trace_is_off_unless_env_points_to_a_file(monkeypatch, tmp_path):
    monkeypatch.delenv(msaa.ENV_TRACE, raising=False)
    provider = msaa.Provider(root=None)
    provider._trace("accName", 1, "x")          # 不设环境变量：一行都不写
    assert not list(tmp_path.iterdir())

    log = tmp_path / "a11y.log"
    monkeypatch.setenv(msaa.ENV_TRACE, str(log))
    provider = msaa.Provider(root=None)
    provider._trace("accName", 1, "x")
    assert log.exists() and "method=accName" in log.read_text(encoding="utf-8")


def test_accvalue_trace_is_length_only_at_the_call_site(monkeypatch, tmp_path):
    """复核 SHOULD-FIX 4：这条以前是「自己调 `_trace` 再断言 `_trace`」= 同义反复。

    现在直接扫 **`get_accValue` 的调用点**：交给 `_trace` 的必须是 ``"len=%d" % len(...)``
    这种形式；哪天有人改成把凭据原文传进去，这条立刻红。
    """
    import ast

    source = (pathlib.Path(__file__).resolve().parents[1]
              / "djb_node" / "desktop" / "a11y" / "msaa.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "get_accValue")
    calls = [node for node in ast.walk(function)
             if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "_trace"]
    assert calls, "get_accValue 里没有 trace（S3 取证需要它）"
    fmt = calls[0].args[2]
    assert isinstance(fmt, ast.BinOp), "trace 第三参必须是格式化的长度，不是原始值：%s" % ast.dump(fmt)[:80]
    assert isinstance(fmt.left, ast.Constant) and "len=" in str(fmt.left.value), ast.dump(fmt)[:80]
    assert isinstance(fmt.right, ast.Call) and getattr(fmt.right.func, "id", "") == "len", \
        "只许传长度：%s" % ast.dump(fmt.right)[:80]

    # 行为面兜底：真跑一次，凭据原文一个字都不许进日志
    log = tmp_path / "a11y.log"
    monkeypatch.setenv(msaa.ENV_TRACE, str(log))
    msaa.Provider(root=None)._trace("accValue", 2, "len=%d" % len("secret-value"))
    text = log.read_text(encoding="utf-8")
    assert "secret-value" not in text and "len=12" in text
