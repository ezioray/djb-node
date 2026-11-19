#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""纯 Python wheel 构建入口（0.3.4 / W6）。

## 为什么单独一个入口

五通道制品（`linux-src` / `official-supply` / `machine-room-offline` / `windows-assets`）
都由 `ci/pkgbuild.py` 出，其中 official-supply 与 machine-room 两个通道**都依赖 wheel**；
但 0.3.3 及以前仓内从没产出过 `djb_node-<ver>-py3-none-any.whl`，于是
「同一份 wheel + `profiles/official-supply/` 四件」这条安装路径在制品层面是断的。

本脚本只做一件事：产出

    dist/djb_node-<version>-py3-none-any.whl
    dist/djb_node-<version>-py3-none-any.whl.sha256

**它不是「edition 包」**：特供档 = 同一个 wheel + `profiles/official-supply/` 的四个文件
（`config.json` / `install.sh` / `README.md` / 系统单元），代码里没有 edition 之分；
`edition` 只决定**更新源通道**（见 `djb_node/update_source.py`）。

## 纪律

* 版本强校验：wheel 文件名里的版本必须等于 `djb_node.__version__`（防止 pyproject 漂移）。
* 只写 `dist/`：不打 tag、不签名、不碰发布面、不删旧制品以外的东西。
* 优先 `python -m build --wheel --no-isolation`；没有 `build` 模块时回退
  `pip wheel --no-deps --no-build-isolation`（两者都**不联网**）。
* 纯 Python：产物必须是 `py3-none-any`。出现平台标签说明混进了编译产物，直接失败。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DIST = REPO / "dist"


def package_version() -> str:
    """从仓库根读 `djb_node.__version__`（不依赖安装态、不写字节码）。"""
    code = "import djb_node; print(djb_node.__version__)"
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=str(REPO),
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("读版本失败：" + ((result.stderr or "").strip()[-300:]))
    return result.stdout.strip()


def expected_wheel_name(version: str) -> str:
    """纯 Python 包的 wheel 名（PEP 427）：`<dist>-<ver>-py3-none-any.whl`。"""
    return "djb_node-" + str(version) + "-py3-none-any.whl"


def check_wheel(path: Path, version: str) -> None:
    """文件名 + 标签校验：版本不符或不是 py3-none-any 都要失败（不许悄悄出平台包）。"""
    want = expected_wheel_name(version)
    if path.name != want:
        raise SystemExit("wheel 名不符：期望 " + want + "，实际 " + path.name)
    if not path.is_file() or path.stat().st_size <= 0:
        raise SystemExit("wheel 不存在或为空：" + str(path))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(REPO), capture_output=True, text=True)


def build_wheel(dist: Path = DIST, python: str | None = None) -> dict[str, object]:
    """构建 wheel 并返回结果字典（不打印；调用方决定怎么呈现）。"""
    python = python or sys.executable
    version = package_version()
    dist.mkdir(parents=True, exist_ok=True)
    target = dist / expected_wheel_name(version)
    if target.exists():                      # 幂等：同名旧件先删，避免 build 追加 -1 后缀
        target.unlink()
    checksum = target.with_name(target.name + ".sha256")
    if checksum.exists():
        checksum.unlink()

    attempts: list[str] = []
    result = _run([python, "-m", "build", "--wheel", "--no-isolation",
                   "--outdir", str(dist), "."])
    if result.returncode == 0:
        attempts.append("python -m build --wheel --no-isolation")
    else:
        attempts.append("python -m build 失败："
                        + ((result.stderr or result.stdout or "").strip().splitlines() or [""])[-1])
        result = _run([python, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                       "--wheel-dir", str(dist), "."])
        if result.returncode != 0:
            raise SystemExit("wheel 构建失败：\n" + (result.stdout or "") + (result.stderr or ""))
        attempts.append("pip wheel --no-deps --no-build-isolation")

    check_wheel(target, version)
    digest = sha256_file(target)
    checksum.write_text(digest + "  " + target.name + "\n", encoding="utf-8")
    return {"version": version, "wheel": str(target), "name": target.name,
            "bytes": target.stat().st_size, "sha256": digest,
            "checksum_file": str(checksum), "method": attempts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="构建纯 Python wheel（dist/djb_node-<ver>-py3-none-any.whl）")
    parser.add_argument("--dist", default=str(DIST), help="输出目录（默认 ./dist）")
    parser.add_argument("--plan", action="store_true", help="只校验环境与版本，不构建")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)

    dist = Path(args.dist).resolve()
    version = package_version()
    if args.plan:
        payload = {"version": version, "wheel_name": expected_wheel_name(version),
                   "dist": str(dist), "plan": "OK（未写盘）"}
        print(json.dumps(payload, ensure_ascii=False) if args.json else
              "== 版本   " + version + "\n== 产物   " + str(dist / expected_wheel_name(version))
              + "\n== plan OK（未写盘）")
        return 0

    try:
        result = build_wheel(dist)
    except SystemExit as exc:                 # 构建/校验失败：退出码 3，与 pkgbuild 一致
        print("refuse: " + str(exc), file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("== wheel   " + str(result["wheel"]))
        print("== 大小    " + str(result["bytes"]) + " bytes")
        print("== sha256  " + str(result["sha256"]))
        print("== 方式    " + "; ".join(result["method"]) if isinstance(result["method"], list)
              else "== 方式    " + str(result["method"]))
        print("== 说明    特供档 = 同一 wheel + profiles/official-supply/ 四件（无 edition 包）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
