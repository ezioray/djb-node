# SPDX-License-Identifier: Apache-2.0
"""IA 骨架的可执行裁决（0.3.5 · M1-T1）。

对应 ``reports/CLIENT-UX-20261112/02-IA-TOKENPLAN.md``：§2 方案 A（5 组、共享前置）、
§4 兼容（13 key 不变 / ``DJB_UI_IA=legacy``）、§5 量化验收（一屏可见、GPU 不进前 5）。

两条边界必须同时成立，因此本文件有两组互相牵制的断言：

* **新表合格**：方案 A 满足全部 IA 不变量；
* **旧表未动**：``views.NAV_SECTIONS`` 仍是 0.3.4 的 4 组 ⇒ 本轮零 UI 外观变化。
  接线（T4）时第二组会红，那时应当**改测试**而不是改回归基线。
"""
from __future__ import annotations

import ast
import inspect

import pytest

from djb_node.desktop import ia
from djb_node.desktop import views


# ────────────────────────────────────────────────────────────── 不变量
def test_invariants_have_no_violation():
    assert ia.violations() == []


def test_thirteen_pages_are_mounted_exactly_once():
    """§1 约束：13 项不删不并。"""
    keys = ia.flat_keys()
    assert len(keys) == 13
    assert sorted(keys) == sorted(views.VIEW_KEYS)
    assert len(set(keys)) == 13


def test_share_group_is_first_and_starts_with_token_plan():
    """§2：首屏第一组 = 共享 SHARE，首项 = 共享 Token Plan（≤1 次点击）。"""
    first = ia.IA_SECTIONS_SHARE[0]
    assert (first.key, first.title, first.micro) == ("share", "共享", "SHARE")
    assert first.keys[0] == ia.FOCUS_KEY == "token_plan"


def test_token_plan_comes_before_access():
    """既有产品语义：先说「共享了什么」再进「接入配置」动手。"""
    assert ia.index_of("token_plan") < ia.index_of("access")
    assert ia.index_of("token_plan") == 0


def test_gpu_left_the_primary_path():
    """§1/§5.3：GPU 从第 3 项降到系统组，且不在前 5 项（首屏不出现 GPU 主 CTA）。"""
    index = ia.index_of("gpu")
    assert ia.MIN_GPU_INDEX == 5
    assert index > ia.MIN_GPU_INDEX
    assert index == 10                      # 共享3 + 运行3 + 收益2 + 接入1 + 让路1
    assert ia.section_for("gpu").key == "system"


def test_gpu_is_beta_with_a_subtitle():
    gpu = ia.page("gpu")
    assert gpu.beta is True
    assert "BETA" in gpu.subtitle
    assert gpu.title == "GPU 监控"


def test_five_groups_in_the_documented_order():
    assert [section.key for section in ia.IA_SECTIONS_SHARE] == [
        "share", "run", "earn", "connect", "system",
    ]
    assert [section.micro for section in ia.IA_SECTIONS_SHARE] == [
        "SHARE", "RUN", "EARN", "CONNECT", "SYSTEM",
    ]


# ────────────────────────────────────────────────────────────── 校验器不是空转
@pytest.mark.parametrize(
    ("table", "expected"),
    [
        # GPU 抢回主路径第 1 项
        ((ia.SectionSpec("run", "运行", "RUN", ("gpu", "overview", "bench", "models", "tasks")),
          ia.SectionSpec("earn", "收益", "EARN", ("receipts", "earnings", "token_plan")),
          ia.SectionSpec("connect", "接入", "CONNECT", ("agent", "access")),
          ia.SectionSpec("system", "系统", "SYSTEM", ("yield", "account", "logs"))),
         "GPU 在侧栏第 1 项"),
        # token_plan 掉到 access 之后
        ((ia.SectionSpec("share", "共享", "SHARE", ("models", "access", "token_plan")),
          ia.SectionSpec("run", "运行", "RUN", ("overview", "bench", "tasks")),
          ia.SectionSpec("earn", "收益", "EARN", ("receipts", "earnings")),
          ia.SectionSpec("connect", "接入", "CONNECT", ("agent",)),
          ia.SectionSpec("system", "系统", "SYSTEM", ("yield", "gpu", "account", "logs"))),
         "没有排在 access"),
        # 重复挂载
        ((ia.SectionSpec("share", "共享", "SHARE", ("token_plan", "models", "access")),
          ia.SectionSpec("run", "运行", "RUN", ("overview", "bench", "tasks", "tasks")),
          ia.SectionSpec("earn", "收益", "EARN", ("receipts", "earnings")),
          ia.SectionSpec("connect", "接入", "CONNECT", ("agent",)),
          ia.SectionSpec("system", "系统", "SYSTEM", ("yield", "gpu", "account", "logs"))),
         "页面重复出现"),
        # 漏挂一页
        ((ia.SectionSpec("share", "共享", "SHARE", ("token_plan", "models", "access")),
          ia.SectionSpec("run", "运行", "RUN", ("overview", "bench", "tasks")),
          ia.SectionSpec("earn", "收益", "EARN", ("receipts", "earnings")),
          ia.SectionSpec("connect", "接入", "CONNECT", ("agent",)),
          ia.SectionSpec("system", "系统", "SYSTEM", ("yield", "gpu", "account"))),
         "页面未挂载：logs"),
        # 空组 + 非 ASCII 微标
        ((ia.SectionSpec("share", "共享", "SHARE", ("token_plan", "models", "access")),
          ia.SectionSpec("run", "运行", "运行中", ("overview", "bench", "tasks")),
          ia.SectionSpec("earn", "收益", "EARN", ("receipts", "earnings")),
          ia.SectionSpec("connect", "接入", "CONNECT", ("agent",)),
          ia.SectionSpec("system", "系统", "SYSTEM", ("yield", "gpu", "account", "logs")),
          ia.SectionSpec("extra", "多余", "EXTRA", ())),
         "空组"),
    ],
)
def test_validator_catches_mutations(table, expected):
    """变异测试：把设计违规塞回去，校验器必须**指名**报出来（否则它是空转的）。"""
    problems = ia.violations(table)
    assert problems, "违规表却报告合格"
    assert any(expected in problem for problem in problems), problems


def test_validator_catches_a_page_registered_in_the_wrong_section():
    pages = dict(ia.PAGES)
    pages["gpu"] = ia.PageSpec("gpu", "GPU 监控", "GPU", "share", beta=True,
                               subtitle="GPU 监控（BETA）")
    assert any("不一致" in problem for problem in ia.violations(pages=pages))


# ────────────────────────────────────────────────────────────── 与现有视图层一致
def test_titles_and_kickers_match_the_view_layer():
    """页面注册表不许和 ``views`` 漂移：标题/眉标/13 个 key 逐项相等。"""
    assert tuple(ia.PAGES) != () and sorted(ia.PAGES) == sorted(views.VIEW_TITLES)
    for key, spec in ia.PAGES.items():
        assert spec.title == views.VIEW_TITLES[key], key
        assert spec.kicker == views.PAGE_MICROS[key], key


def test_legacy_table_equals_the_0_3_4_navigation():
    """回滚路径：``DJB_UI_IA=legacy`` 必须逐字还原 0.3.4 的 ``NAV_SECTIONS``。"""
    assert len(ia.IA_SECTIONS_LEGACY) == len(views.NAV_SECTIONS) == 4
    for legacy, current in zip(ia.IA_SECTIONS_LEGACY, views.NAV_SECTIONS):
        assert (legacy.key, legacy.title, legacy.micro, legacy.keys) == (
            current.key, current.title, current.micro, current.keys)


def test_ui_appearance_is_untouched_this_slice():
    """本轮只落数据结构：侧栏仍是 4 组、GPU 仍在第 3 项（接线前不许改外观）。"""
    assert len(views.NAV_SECTIONS) == 4
    assert views.VIEW_KEYS.index("gpu") == 2
    assert views.VIEW_KEYS.index("token_plan") == 7


# ────────────────────────────────────────────────────────────── 取数据 / 模式解析
def test_page_lookup_raises_on_unknown_key():
    with pytest.raises(KeyError) as caught:
        ia.page("dashboard")
    assert "dashboard" in str(caught.value)


def test_index_of_and_section_for_are_total_functions():
    assert ia.index_of("nope") == -1
    assert ia.section_for("nope") is None
    assert ia.section_for("token_plan").key == "share"
    assert ia.index_of("logs") == 12


def test_resolve_ia_defaults_to_share_and_honours_legacy():
    """§6：``DJB_UI_IA=legacy`` 回旧顺序（代码保留旧表）。"""
    default = ia.resolve_ia()
    assert (default.mode, default.raw, default.ignored) == ("share", None, False)
    assert default.sections == ia.IA_SECTIONS_SHARE

    legacy = ia.resolve_ia("legacy")
    assert legacy.mode == "legacy" and legacy.sections == ia.IA_SECTIONS_LEGACY
    assert ia.flat_keys(legacy.sections)[2] == "gpu"        # 旧顺序里 GPU 仍第 3

    assert ia.resolve_ia("  LEGACY ").mode == "legacy"
    assert ia.resolve_ia("share").mode == "share"


def test_unrecognised_ia_mode_is_reported_not_silently_swallowed():
    """未知值不静默当默认：回 share 生效，但 ``ignored=True`` 让诊断能说一句。"""
    for raw in ("", "v2", "经典"):
        decision = ia.resolve_ia(raw)
        assert decision.mode == "share"
        assert decision.ignored is True
        assert decision.raw == raw


def test_ia_module_is_pure_python_without_tk():
    tree = ast.parse(inspect.getsource(ia))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "tkinter" not in imported
    assert "os" not in imported            # env 由调用方注入，便于回放
    assert imported <= {"__future__", "dataclasses", "typing"}
