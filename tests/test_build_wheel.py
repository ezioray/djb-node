# SPDX-License-Identifier: Apache-2.0
"""wheel 构建入口的窄测（0.3.4 / W6）：只验名字与校验逻辑，**不真构建**。

真构建在构建机/QA 后跑（`python3 ci/build_wheel.py`）；本文件保证：
纯 Python 包的 wheel 名固定为 `djb_node-<ver>-py3-none-any.whl`、版本不符必须拒绝。
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("build_wheel", REPO / "ci" / "build_wheel.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_wheel_name_is_pure_python():
    module = _load()
    assert module.expected_wheel_name("0.3.4") == "djb_node-0.3.4-py3-none-any.whl"


def test_package_version_matches_pyproject():
    module = _load()
    import djb_node
    assert module.package_version() == djb_node.__version__


def test_check_wheel_rejects_wrong_version_and_tags(tmp_path):
    module = _load()
    good = tmp_path / "djb_node-0.3.4-py3-none-any.whl"
    good.write_bytes(b"x")
    module.check_wheel(good, "0.3.4")                       # 不抛 = 通过

    with pytest.raises(SystemExit):
        module.check_wheel(good, "0.3.5")                   # 版本不符
    platform_tagged = tmp_path / "djb_node-0.3.4-cp311-cp311-linux_x86_64.whl"
    platform_tagged.write_bytes(b"x")
    with pytest.raises(SystemExit):
        module.check_wheel(platform_tagged, "0.3.4")        # 混进编译产物 → 失败
