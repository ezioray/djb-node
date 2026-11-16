# SPDX-License-Identifier: Apache-2.0
"""读屏投影（``djb_node.desktop.a11y``）：节点树口径 + 降级路径。

这里是**纯投影**断言：不装 COM、不画窗口，所以 Linux 上就能跑。
真机（oleacc 跨进程 walk / NVDA）证据在 ``reports/**/evidence/…/a11y/`` 里。
"""
from __future__ import annotations

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
    assert table.name.startswith("表格：共 ")
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
def test_row_ids_survive_a_refresh_with_different_rows():
    def snapshot(rows):
        return ds.Snapshot({"logs": {"rows": rows}})

    def logs_rows(*events):
        return [{"ts": "2026-11-16T10:0%d:00Z" % i, "level": "info", "event": e,
                 "reason": "", "next": ""} for i, e in enumerate(events)]

    first = vw.build_view("logs", snapshot(logs_rows("a", "b", "c")))
    second = vw.build_view("logs", snapshot(logs_rows("b", "c")))
    ids_first = ids_by_key(build_tree(first))
    ids_second = ids_by_key(build_tree(second))
    shared = set(ids_first) & set(ids_second)
    rows_shared = {key for key in shared if "/row:" in key}
    assert rows_shared, "两版之间应至少共有一行"
    # 计数从 1 起并且唯一（客户端按 childId 逐个取，不能重号）
    for mapping in (ids_first, ids_second):
        values = list(mapping.values())
        assert len(values) == len(set(values))
        assert sorted(values) == list(range(len(values)))


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


def test_trace_never_writes_accvalue_verbatim(monkeypatch, tmp_path):
    log = tmp_path / "a11y.log"
    monkeypatch.setenv(msaa.ENV_TRACE, str(log))
    provider = msaa.Provider(root=None)
    secret = "djb_ns_super_secret"
    provider._trace("accValue", 2, "len=%d" % len(secret))
    text = log.read_text(encoding="utf-8")
    assert "super_secret" not in text
    assert "len=%d" % len(secret) in text
