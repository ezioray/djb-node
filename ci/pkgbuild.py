#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""仓内打包器（B7 / RC8.3-C 0.3.1）：把「五通道里的三条 Linux 侧制品 + Windows 载体」做成
可复现、可校验、CI 里能跑的单一入口。

## 为什么要在仓内再放一份（而不是直接用 ops/release/build/*.sh）

`ops/release/build/{linux-build,official-supply-build}.sh` 是**工作区工具**：它把
`$WS/repos/djb-node` 当成源码仓、把 `$WS/ops/rc/artifacts/downloads` 当成发布面。
CI 里 checkout 出来的就是仓本身，没有 `$WS/repos` 这一层，也没有发布面——那两把脚本
在 CI 里根本跑不起来（B7 的 `linux-matrix-l0` 因此一直是占位）。

本脚本只做「从当前 checkout 出一个和 0.3.0 **同名同结构**的包」，护栏与老脚本一致：

* 源码只从 commit 出（``git archive <sha> -- <白名单>``），绝不打工作树里未提交的改动；
* 版本号必须与 ``djb_node/__init__.py`` 一致；
* 拒绝写进发布面（``*/ops/rc/artifacts/downloads*``）——定版入库归 release-manager；
* 产物自带 VERSION.txt / SHA256SUMS.txt / README-INSTALL.txt；
* 0.3.1 起 VERSION.txt 与 README 里**显式写出 update_source**（五通道口径，见
  :mod:`djb_node.update_source`）。

## 用法

    python3 ci/pkgbuild.py --kind linux-src        --out /tmp/pkg
    python3 ci/pkgbuild.py --kind official-supply  --out /tmp/pkg
    python3 ci/pkgbuild.py --kind machine-room-offline --out /tmp/pkg
    python3 ci/pkgbuild.py --kind windows-assets   --out /tmp/pkg   # 载体，不是 exe
    python3 ci/pkgbuild.py --kind linux-src --plan                   # 只校验入参、不写盘

每个 kind 结束时打印一行 ``BUILD_RESULT {...}``（CI 用它取 sha256 / bytes）。

退出码：0 成功 · 2 参数/输入不合格 · 3 构建或自检失败 · 4 护栏违规。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

REPO = Path(__file__).resolve().parent.parent

#: 会进包的顶层路径白名单（与 0.3.0 的老脚本一致；tests/ docs/ deploy/ 不进包，
#: 它们带内网测试夹具，dl-preflight 会判「内网地址残留」拒发）。
WHITELIST_CONSUMER = ("djb_node", "pyproject.toml", "README-CLIENT.md", "LICENSE", "SECURITY.md")
WHITELIST_SUPPLY = WHITELIST_CONSUMER + ("profiles",)

LAUNCHER = """#!/bin/sh
# djb 启动器：客户端只用标准库 ⇒ 既不需要 pip install，也不需要装包。
set -eu
SELF=$(readlink -f "$0" 2>/dev/null || printf "%s" "$0")
HERE=$(CDPATH= cd -- "$(dirname -- "$SELF")" && pwd)
PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
if [ -x "$HERE/.venv/bin/python" ]; then
  exec "$HERE/.venv/bin/python" -m djb_node "$@"
fi
exec "${PYTHON:-python3}" -m djb_node "$@"
"""


def say(text: str) -> None:
    print(text, flush=True)


def die(text: str, code: int = 2) -> NoReturn:
    print("refuse: " + text, file=sys.stderr)
    raise SystemExit(code)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd or REPO), capture_output=True, text=True)
    if result.returncode != 0:
        die("git " + " ".join(args) + " 失败：" + (result.stderr or "").strip()[:200])
    return result.stdout


@dataclass
class Plan:
    kind: str
    version: str
    commit: str
    describe: str
    built_at: str
    stem: str
    out: Path
    channel: str
    edition: str
    platform: str
    update_source: str
    whitelist: tuple[str, ...] = field(default_factory=tuple)
    notes: list[str] = field(default_factory=list)


def module_version() -> str:
    """**只读源码文本**取版本（不 import，避免在构建机上产生 __pycache__/副作用）。"""
    text = (REPO / "djb_node" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__") and "=" in line:
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    die("读不到 djb_node/__init__.py 的 __version__", 3)


def update_source_constant() -> str:
    text = (REPO / "djb_node" / "update_source.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("MANIFEST_URL"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    die("读不到 djb_node/update_source.py 的 MANIFEST_URL", 3)


def channel_spec(channel: str) -> dict:
    """从 update_source.py 的 CHANNELS 里薅出该通道的契约（**单一真源**，不在这里重抄）。"""
    sys.path.insert(0, str(REPO))
    from djb_node import update_source as module       # noqa: PLC0415 - 延迟导入，参数错早退

    return module.spec(channel).to_dict()


def build_plan(kind: str, out_dir: Path, *, version: str, sha: str, now: str) -> Plan:
    specs = {
        "linux-src": ("consumer-linux-src", WHITELIST_CONSUMER),
        "official-supply": ("official-supply", WHITELIST_SUPPLY),
        "machine-room-offline": ("machine-room", WHITELIST_CONSUMER),
        "windows-assets": ("consumer-windows", WHITELIST_CONSUMER),
    }
    if kind not in specs:
        die("未知 --kind：" + kind + "（可选：" + " / ".join(specs) + "）")
    channel, whitelist = specs[kind]
    spec = channel_spec(channel)
    resolved = git("rev-parse", "--verify", sha + "^{commit}").strip()
    source_version = git("show", resolved + ":djb_node/__init__.py")
    src_ver = ""
    for line in source_version.splitlines():
        if line.startswith("__version__") and "=" in line:
            src_ver = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not src_ver:
        die("读不到 " + resolved + ":djb_node/__init__.py 的 __version__", 3)
    if src_ver != version:
        die("版本号不一致：源码 " + src_ver + "，参数 " + version, 2)
    missing = [name for name in whitelist
               if not git("ls-tree", "--name-only", resolved, "--", name).strip()]
    if missing:
        die("白名单条目在 " + resolved[:12] + " 里不存在：" + ", ".join(missing), 3)
    describe = git("describe", "--tags", "--always", resolved).strip() or resolved[:12]
    stem = {
        "linux-src": "djb-node-" + version + "-linux-x64-src",
        "official-supply": "djb-node-" + version + "-official-supply-x64",
        "machine-room-offline": "djb-node-" + version + "-machine-room-offline",
        "windows-assets": "djb-node-" + version + "-windows-x64-artifacts",
    }[kind]
    plan = Plan(kind=kind, version=version, commit=resolved, describe=describe, built_at=now,
                stem=stem, out=out_dir, channel=str(spec["channel"]),
                edition=str(spec["edition"]), platform=str(spec["platform"]),
                update_source=str(spec["update_source"]), whitelist=tuple(whitelist))
    if plan.update_source != update_source_constant():
        plan.notes.append("通道源与 MANIFEST_URL 不一致（应改 update_source.py 而不是打包器）")
    return plan


def version_txt(plan: Plan, *, method: str) -> str:
    lines = [
        "product          : djb-node (DJB 联脑 node client)",
        "version          : " + plan.version,
        "edition          : " + plan.edition,
        "channel          : " + plan.channel,
        "commit           : " + plan.commit,
        "git describe     : " + plan.describe,
        "built_at         : " + plan.built_at,
        "platform         : " + plan.platform,
        "signature_status : unsigned-test (no code signing; TEST BUILD)",
        "build_method     : " + method,
        # 0.3.1（RC8.3-C）：五通道显式更新源。manifest-gen.parse_version_txt 只认
        # 它认识的键，未知键**忽略**，所以加这行不会让清单生成器读错。
        "update_source    : " + plan.update_source,
    ]
    return "\n".join(lines) + "\n"


def init_command(plan: Plan, *, gateway: str = "$DJB_DEFAULT_GATEWAY",
                 home: str = "$DJB_HOME") -> str:
    """README 里那条「照抄」的 init：update_source 与 edition 都显式写出来。"""
    return ("DJB_HOME=" + home + " ./djb init --gateway \"" + gateway + "\" --preset 省心"
            " --edition " + plan.edition + " --update-source " + plan.update_source)


def readme_install(plan: Plan) -> str:
    return """DJB 联脑 · 节点客户端（{platform}）— {edition} edition / TEST BUILD（未签名）
====================================================================

版本    : {version}
edition : {edition}
通道    : {channel}
commit  : {commit}
构建时间: {built_at}

这是**源码包，不是二进制**；客户端零第三方运行时依赖，只要有 python3 >= 3.10。

初始化 / 启动（**更新源已显式写出**，照抄即可）
----------------------------------------------
     {init_cmd}
     ./djb env
     ./djb auth --bind <网页端一次性绑定码>
     ./djb start
     ./djb status

* `--edition {edition}` 只是登记档位；真正决定「从哪拉清单」的是 `--update-source`。
* 更新源 = 官方签名清单（Ed25519 验签，公钥内置；私钥在发布侧，客户端不持有）。
* 离线/内网：把 `--update-source` 换成内网镜像的清单 URL 或本地目录；留空 = mock 源（不联网）。
* 想确认这轮到底接的哪个源：`./djb update --status`（JSON 里的 source / source_kind / source_origin）。

注意
----
* 启动入口必须显式带 gateway（--gateway 或 DJB_DEFAULT_GATEWAY），否则首启自举可能写生产地址。
* 未签名测试构建；无入站端口（daemon 只绑 127.0.0.1:1717）。
""".format(platform=plan.platform, edition=plan.edition, version=plan.version,
           channel=plan.channel, commit=plan.commit, built_at=plan.built_at,
           init_cmd=init_command(plan))


def write_sha256sums(root: Path) -> int:
    lines = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == "SHA256SUMS.txt":
            continue
        lines.append(sha256_file(path) + "  " + rel)
    (root / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def stage_from_git(plan: Plan, stage: Path) -> Path:
    target = stage / plan.stem
    target.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar",
                             plan.commit, "--", *plan.whitelist],
                            capture_output=True)
    if result.returncode != 0:
        die("git archive 失败：" + result.stderr.decode("utf-8", "replace")[:200], 3)
    archive = stage / "src.tar"
    archive.write_bytes(result.stdout)
    with tarfile.open(archive, "r") as handle:
        handle.extractall(target)          # noqa: S202 - 内容来自本仓 git archive
    archive.unlink()
    if not (target / "djb_node" / "__init__.py").is_file():
        die("解包后缺 djb_node/__init__.py", 3)
    return target


def selfcheck(plan: Plan, root: Path) -> dict:
    """真解包真跑：``./djb --version`` 必须等于本版，且输出里不得出现生产域名。"""
    env = dict(os.environ)
    env.update({"DJB_HOME": str(root.parent / "sc-home"),
                "DJB_BOOTSTRAP_DISABLE": "1",
                "DJB_DEFAULT_GATEWAY": env.get("DJB_DEFAULT_GATEWAY", "http://192.168.51.83:18100"),
                "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                "PYTHONPATH": str(root)})
    launcher = root / "djb"
    result = subprocess.run([str(launcher), "--version"], cwd=str(root), env=env,
                            capture_output=True, text=True, timeout=120)
    out = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        die("自检 ./djb --version 失败 rc=" + str(result.returncode) + "：" + out.strip()[:300], 3)
    if plan.version not in out:
        die("自检版本号不符：跑出来 " + out.strip()[:120] + "，期望 " + plan.version, 3)
    if "tkbank.ai" in out.lower():
        die("自检输出里出现生产域名 tkbank.ai（护栏违规，不发包）", 4)
    return {"version_ok": True, "guard_tkbank_hit": False}


def build_linux_like(plan: Plan) -> dict:
    root_out = plan.out
    root_out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="djb-pkg-" + plan.kind + "-"))
    try:
        stage = work / "stage"
        root = stage_from_git(plan, stage)
        launcher = root / "djb"
        launcher.write_text(LAUNCHER, encoding="utf-8")
        launcher.chmod(0o755)
        (root / "VERSION.txt").write_text(
            version_txt(plan, method="ci/pkgbuild.py --kind " + plan.kind), encoding="utf-8")
        (root / "README-INSTALL.txt").write_text(readme_install(plan), encoding="utf-8")
        files = write_sha256sums(root)
        archive = root_out / (plan.stem + ".tar.gz")
        tmp = archive.with_suffix(".tar.gz.tmp")
        with tarfile.open(tmp, "w:gz") as handle:
            def norm(info: tarfile.TarInfo) -> tarfile.TarInfo:
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                return info
            handle.add(str(root), arcname=plan.stem, filter=norm)
        tmp.replace(archive)
        checks = selfcheck(plan, root)
        (root_out / (plan.stem + ".tar.gz.sha256")).write_text(
            sha256_file(archive) + "  " + archive.name + "\n", encoding="utf-8")
        return {"artifact": archive.name, "path": str(archive),
                "bytes": archive.stat().st_size, "sha256": sha256_file(archive),
                "payload_files": files, **checks}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def build_machine_room_offline(plan: Plan) -> dict:
    """机房版离线包：复用仓内 deploy/machine-room/offline/build.sh（wheel + 清单），
    再补上 0.3.1 的显式载体（config.template.json）并打成发布形态 tar.gz。"""
    root_out = plan.out
    root_out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="djb-pkg-mr-"))
    try:
        dist = work / "dist"
        script = REPO / "deploy" / "machine-room" / "offline" / "build.sh"
        result = subprocess.run(["/bin/sh", str(script), "--out", str(dist)],
                                cwd=str(REPO), capture_output=True, text=True, timeout=600,
                                env={**os.environ, "PYTHON": sys.executable,
                                     "DJB_OFFLINE_UPDATE_SOURCE": plan.update_source})
        if result.returncode != 0:
            die("离线包构建失败 rc=" + str(result.returncode) + "："
                + ((result.stdout or "") + (result.stderr or "")).strip()[-400:], 3)
        root = work / "stage" / plan.stem
        root.mkdir(parents=True, exist_ok=True)
        for item in sorted(dist.iterdir()):
            if item.is_file():
                shutil.copy2(item, root / item.name)
        (root / "VERSION.txt").write_text(
            version_txt(plan, method="ci/pkgbuild.py --kind machine-room-offline"),
            encoding="utf-8")
        (root / "README-INSTALL.txt").write_text(readme_install(plan), encoding="utf-8")
        write_sha256sums(root)
        archive = root_out / (plan.stem + ".tar.gz")
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(str(root), arcname=plan.stem)
        (root_out / (plan.stem + ".tar.gz.sha256")).write_text(
            sha256_file(archive) + "  " + archive.name + "\n", encoding="utf-8")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("update_source") != plan.update_source:
            die("离线包 manifest.json 没写出 update_source（载体缺失）", 3)
        return {"artifact": archive.name, "path": str(archive),
                "bytes": archive.stat().st_size, "sha256": sha256_file(archive),
                "payload_files": len(list(root.rglob("*"))),
                "manifest_update_source": manifest.get("update_source"),
                "manifest_edition": manifest.get("edition")}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def build_windows_assets(plan: Plan) -> dict:
    """Windows 通道的**载体**（不是 exe）：config.json + start-windows.bat + VERSION.txt +
    README-WINDOWS.txt。win-build.py 在 4070 上产出 exe 后，把这几个文件并进 zip 即可
    （补丁见 reports/RC8-DEV/client-031/WINDOWS-SIGNING-BLOCKER.md §5）。"""
    root = plan.out / plan.stem
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps({
        "gateway_url": "https://api.example.invalid",
        "mirror_port": 1717,
        "update_channel": "stable",
        "update_source": plan.update_source,
        "edition": plan.edition,
        "log_level": "info",
        "yield_enabled": False,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    bat = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "setlocal\r\n"
        "cd /d \"%~dp0\"\r\n"
        "rem 0.3.1（RC8.3-C）：更新源显式写出，不靠默认值\r\n"
        "set \"DJB_UPDATE_SOURCE=" + plan.update_source + "\"\r\n"
        "set \"DJB_EDITION=" + plan.edition + "\"\r\n"
        "djb-node.exe init --preset 省心 --edition %DJB_EDITION% "
        "--update-source %DJB_UPDATE_SOURCE%\r\n"
        "djb-node.exe env\r\n"
    )
    (root / "start-windows.bat").write_text(bat, encoding="utf-8")
    (root / "VERSION.txt").write_text(
        version_txt(plan, method="ci/pkgbuild.py --kind windows-assets"), encoding="utf-8")
    (root / "README-WINDOWS.txt").write_text(readme_install(plan), encoding="utf-8")
    write_sha256sums(root)
    archive = plan.out / (plan.stem + ".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as handle:
        for item in sorted(root.iterdir()):
            handle.write(item, arcname=plan.stem + "/" + item.name)
    return {"artifact": archive.name, "path": str(archive),
            "bytes": archive.stat().st_size, "sha256": sha256_file(archive),
            "payload_files": len(list(root.iterdir()))}


def check_release_surface(out: Path) -> None:
    resolved = out.resolve().as_posix()
    if "/ops/rc/artifacts/downloads" in resolved:
        die("拒绝写进发布面（" + resolved + "）：定版入库归 release-manager", 2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DJB 客户端五通道仓内打包器（0.3.1）")
    parser.add_argument("--kind", required=True,
                        choices=("linux-src", "official-supply", "machine-room-offline",
                                 "windows-assets"))
    parser.add_argument("--out", default="", help="输出目录（默认 build/pkg）")
    parser.add_argument("--version", default="", help="版本（默认取 djb_node.__version__）")
    parser.add_argument("--sha", default="HEAD", help="源码 commit（默认 HEAD）")
    parser.add_argument("--plan", action="store_true", help="只校验入参并打印计划，不写盘")
    args = parser.parse_args(argv)

    version = args.version or module_version()
    out = Path(args.out or (REPO / "build" / "pkg"))
    check_release_surface(out)
    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    plan = build_plan(args.kind, out, version=version, sha=args.sha, now=built_at)
    say("== kind     " + plan.kind + "（channel=" + plan.channel + " edition=" + plan.edition
        + "）")
    say("== 版本     " + plan.version + "  commit " + plan.commit)
    say("== 输出     " + str(plan.out / plan.stem))
    # 人读行只给分类（**不打 URL**）：构建流水线对 tkbank.ai 有硬护栏，
    # 完整 URL 只出现在 BUILD_RESULT 的机器可读字段里（清单草案要用）。
    say("== 更新源   " + channel_spec(plan.channel)["update_source"].rsplit("/", 1)[-1]
        + "（" + plan.channel + "；完整 URL 见 BUILD_RESULT.update_source）")
    for note in plan.notes:
        say("== 注意     " + note)
    if args.plan:
        say("== plan OK（未写盘）")
        return 0
    if plan.kind in ("linux-src", "official-supply"):
        result = build_linux_like(plan)
    elif plan.kind == "machine-room-offline":
        result = build_machine_room_offline(plan)
    else:
        result = build_windows_assets(plan)
    say("BUILD_RESULT " + json.dumps({
        "kind": plan.kind, "channel": plan.channel, "edition": plan.edition,
        "version": plan.version, "commit": plan.commit, "built_at": plan.built_at,
        "update_source": plan.update_source, **result,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
