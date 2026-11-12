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

    export SOURCE_DATE_EPOCH=$(git show -s --format=%ct <sha>)       # 0.3.5 起必填
    python3 ci/pkgbuild.py --kind linux-src --sha <40位全sha> --out /tmp/pkg
    python3 ci/pkgbuild.py --kind official-supply --sha <sha> --out /tmp/pkg
    python3 ci/pkgbuild.py --kind machine-room-offline --sha <sha> --out /tmp/pkg
    python3 ci/pkgbuild.py --kind windows-assets --sha <sha> --out /tmp/pkg   # 载体，不是 exe
    python3 ci/pkgbuild.py --kind linux-src --sha <sha> --plan               # 只校验入参、不写盘
    python3 ci/pkgbuild.py --kind linux-src --sha <sha> --allow-wallclock    # 仅本地冒烟

每个 kind 结束时打印一行 ``BUILD_RESULT {...}``（CI 用它取 sha256 / bytes）。

## 可复现（0.3.5 / B-PREP P1）

同一 commit + 同一 ``SOURCE_DATE_EPOCH`` 必须得到**字节相同**的 tar.gz。
三个不确定性来源逐个钉死：

* ``built_at``：有 ``SOURCE_DATE_EPOCH`` 就用它（UTC），没有才退回墙钟；
* tar 成员 mtime 归一到 epoch（uid/gid/uname/gname 早已归一）；
* gzip 头 mtime=0 且不写文件名（等价 ``gzip -n``）—— 否则每次构建包头都不同。

CI 里请显式 ``export SOURCE_DATE_EPOCH=$(git show -s --format=%ct <sha>)``。
**没设就拒**（退出码 2）：默认路径不允许"悄悄退回墙钟"，那等于默认不可复现；
只在本地冒烟时用 ``--allow-wallclock`` 显式放行，产物 ``VERSION.txt`` 里会写
``built_at_source : wallclock`` 自证。

## provenance（0.3.5 / B-PREP P2）

``--sha`` **必填、且必须是 40 位全 sha**（短 sha 会写进包内 ``source_ref``/``VERSION.txt``，
同一份代码换个写法就换一个 sha）。``--src <目录>`` 从**已摊在盘上的源码树**打包时：

* **不再问 git 要 commit** —— 源码目录若落在某个外层仓库里，``git rev-parse HEAD``
  会拿到外层 HEAD，``VERSION.txt`` 就写了错的 commit（0.3.4 的 macOS 入口实测踩过）；
* commit 一律取 ``--sha``；打包后**回读包内 ``VERSION.txt``** 断言 ``commit == --sha``，
  对不上就拒发（退出码 3）。

退出码：0 成功 · 2 参数/输入不合格 · 3 构建或自检失败 · 4 护栏违规。
"""
from __future__ import annotations

import argparse
import gzip
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


def source_epoch() -> int:
    """``SOURCE_DATE_EPOCH``（reproducible-builds 约定）。取值不合法就拒，不悄悄退回墙钟。"""
    raw = (os.environ.get("SOURCE_DATE_EPOCH") or "").strip()
    if not raw:
        return 0
    try:
        epoch = int(raw)
    except ValueError:
        die("SOURCE_DATE_EPOCH 不是整数：" + raw, 2)
    if epoch < 0:
        die("SOURCE_DATE_EPOCH 不能为负：" + raw, 2)
    return epoch


def build_timestamp(epoch: int) -> str:
    """built_at 只有一个来源：给了 epoch 就由 epoch 推出，没给才用墙钟。"""
    moment = datetime.fromtimestamp(epoch, timezone.utc) if epoch else datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def tar_normalize(epoch: int):
    """tar 成员归一：属主归零 + mtime 钉到 epoch（epoch=0 时保留原 mtime，兼容老行为）。"""
    def norm(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        if epoch:
            info.mtime = epoch
        return info
    return norm


def write_targz(root: Path, archive: Path, *, arcname: str, epoch: int) -> None:
    """写字节可复现的 tar.gz：成员过滤 + gzip 头 mtime=0（等价 ``gzip -n``）。

    不用 ``tarfile.open(mode="w:gz")``：它拿不到 gzip 头的时间戳，两次构建头就不同。
    ``gzip.GzipFile(fileobj=...)`` 不传 filename ⇒ 头里也不写文件名。
    """
    tmp = archive.with_name(archive.name + ".tmp")
    with open(tmp, "wb") as raw, \
            gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w") as handle:
        handle.add(str(root), arcname=arcname, filter=tar_normalize(epoch))
    tmp.replace(archive)


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
    epoch: int = 0                      # SOURCE_DATE_EPOCH（0=未设，须 --allow-wallclock 才放行）
    built_at_source: str = "wallclock"  # source-date-epoch | wallclock（写进 VERSION.txt 自证）


def read_module_version(root: Path) -> str:
    """**只读源码文本**取版本（不 import，避免在构建机上产生 __pycache__/副作用）。"""
    text = (root / "djb_node" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__") and "=" in line:
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    die("读不到 djb_node/__init__.py 的 __version__", 3)


def module_version() -> str:
    return read_module_version(REPO)


def update_source_constant(root: Path = REPO) -> str:
    text = (root / "djb_node" / "update_source.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("MANIFEST_URL"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    die("读不到 djb_node/update_source.py 的 MANIFEST_URL", 3)


def channel_spec(channel: str, root: Path = REPO) -> dict:
    """从 update_source.py 的 CHANNELS 里薅出该通道的契约（**单一真源**，不在这里重抄）。

    单进程只服务一个源码根：git 模式是 ``REPO``，``--src`` 模式是传入的目录
    （同进程换根会命中 import 缓存，故不提供「一个进程打两个根」的用法）。
    """
    sys.path.insert(0, str(root))
    from djb_node import update_source as module       # noqa: PLC0415 - 延迟导入，参数错早退

    return module.spec(channel).to_dict()


def build_plan(kind: str, out_dir: Path, *, version: str, sha: str, now: str,
               epoch: int = 0, src: Path | None = None,
               built_at_source: str = "wallclock") -> Plan:
    specs = {
        "linux-src": ("consumer-linux-src", WHITELIST_CONSUMER),
        "official-supply": ("official-supply", WHITELIST_SUPPLY),
        "machine-room-offline": ("machine-room", WHITELIST_CONSUMER),
        "windows-assets": ("consumer-windows", WHITELIST_CONSUMER),
    }
    if kind not in specs:
        die("未知 --kind：" + kind + "（可选：" + " / ".join(specs) + "）")
    channel, whitelist = specs[kind]
    source_root = Path(src).resolve() if src else REPO
    spec = channel_spec(channel, source_root)
    if src:
        # --src：源码树已经在盘上。**绝不问 git 要 commit** —— 源码目录若落在某个外层
        # 仓库里，git rev-parse HEAD 拿到的是外层 HEAD，VERSION.txt 就会写错 commit。
        resolved = sha
        src_ver = read_module_version(source_root)
        missing = [name for name in whitelist if not (source_root / name).exists()]
        if missing:
            die("--src 里缺白名单条目：" + ", ".join(missing), 3)
        describe = sha[:12] + "-src"
    else:
        resolved = git("rev-parse", "--verify", sha + "^{commit}").strip()
        source_version = git("show", resolved + ":djb_node/__init__.py")
        src_ver = ""
        for line in source_version.splitlines():
            if line.startswith("__version__") and "=" in line:
                src_ver = line.split("=", 1)[1].strip().strip('"').strip("'")
        if not src_ver:
            die("读不到 " + resolved + ":djb_node/__init__.py 的 __version__", 3)
        missing = [name for name in whitelist
                   if not git("ls-tree", "--name-only", resolved, "--", name).strip()]
        if missing:
            die("白名单条目在 " + resolved[:12] + " 里不存在：" + ", ".join(missing), 3)
        describe = git("describe", "--tags", "--always", resolved).strip() or resolved[:12]
    if src_ver != version:
        die("版本号不一致：源码 " + src_ver + "，参数 " + version, 2)
    stem = {
        "linux-src": "djb-node-" + version + "-linux-x64-src",
        "official-supply": "djb-node-" + version + "-official-supply-x64",
        "machine-room-offline": "djb-node-" + version + "-machine-room-offline",
        "windows-assets": "djb-node-" + version + "-windows-x64-artifacts",
    }[kind]
    plan = Plan(kind=kind, version=version, commit=resolved, describe=describe, built_at=now,
                stem=stem, out=out_dir, channel=str(spec["channel"]),
                edition=str(spec["edition"]), platform=str(spec["platform"]),
                update_source=str(spec["update_source"]), whitelist=tuple(whitelist),
                epoch=epoch, built_at_source=built_at_source)
    # P2 的硬断言：计划里的 commit 必须就是入参 --sha（不再接受"git 猜出来的另一个 commit"）。
    if plan.commit != sha:
        die("provenance mismatch: plan.commit=%s != --sha=%s" % (plan.commit, sha), 2)
    if plan.update_source != update_source_constant(source_root):
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
        "built_at_source  : " + plan.built_at_source,
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


def stage_from_src(plan: Plan, stage: Path, src: Path) -> Path:
    """``--src`` 模式：把源码树里的白名单条目拷进暂存区（不碰 .git / 缓存）。"""
    target = stage / plan.stem
    target.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.pyo")
    for name in plan.whitelist:
        source = src / name
        destination = target / name
        if source.is_dir():
            shutil.copytree(source, destination, ignore=ignore)
        else:
            shutil.copyfile(source, destination)
    if not (target / "djb_node" / "__init__.py").is_file():
        die("--src 拷贝后缺 djb_node/__init__.py", 3)
    return target


def version_txt_commit(text: str) -> str:
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "commit":
            return value.strip()
    return ""


def assert_archive_commit(archive: Path, stem: str, expected: str) -> None:
    """打包后回读包内 VERSION.txt：``commit`` 必须 == ``--sha``，否则拒发。

    只断言"计划里的字段"不算数 —— 断言的是**真正被打进制品的那份文本**。
    """
    member = stem + "/VERSION.txt"
    if archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as handle:
            payload = handle.extractfile(member)
            text = payload.read().decode("utf-8") if payload else ""
    elif archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as handle:
            text = handle.read(member).decode("utf-8")
    else:
        die("不认识的制品类型：" + archive.name, 3)
    got = version_txt_commit(text)
    if got != expected:
        die("provenance mismatch: 包内 VERSION.txt commit=%s != --sha=%s（%s）"
            % (got or "<缺失>", expected, archive.name), 3)


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


def build_linux_like(plan: Plan, src: Path | None = None) -> dict:
    root_out = plan.out
    root_out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="djb-pkg-" + plan.kind + "-"))
    try:
        stage = work / "stage"
        root = stage_from_src(plan, stage, src) if src else stage_from_git(plan, stage)
        launcher = root / "djb"
        launcher.write_text(LAUNCHER, encoding="utf-8")
        launcher.chmod(0o755)
        (root / "VERSION.txt").write_text(
            version_txt(plan, method="ci/pkgbuild.py --kind " + plan.kind), encoding="utf-8")
        (root / "README-INSTALL.txt").write_text(readme_install(plan), encoding="utf-8")
        files = write_sha256sums(root)
        archive = root_out / (plan.stem + ".tar.gz")
        write_targz(root, archive, arcname=plan.stem, epoch=plan.epoch)
        assert_archive_commit(archive, plan.stem, plan.commit)
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
        # 与 linux 侧同一把「可复现」尺子：成员 mtime 归一 + gzip 头 mtime=0。
        # 注意：包内 wheel 是 zip，自带时间戳，所以这条通道**仍不保证**字节复现（见 B-PREP 残余风险）。
        write_targz(root, archive, arcname=plan.stem, epoch=plan.epoch)
        assert_archive_commit(archive, plan.stem, plan.commit)
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
    assert_archive_commit(archive, plan.stem, plan.commit)
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
    parser.add_argument("--sha", required=True,
                        help="源码 commit（**必填**，40 位全 sha；0.3.5 起不再默认 HEAD）")
    parser.add_argument("--allow-wallclock", action="store_true",
                        help="允许不设 SOURCE_DATE_EPOCH（默认拒绝：墙钟构建不可复现，产物会标 "
                             "built_at_source=wallclock，只用于本地冒烟）")
    parser.add_argument("--src", default="",
                        help="源码目录（已摊在盘上的树）：给了就不走 git archive，也不问 git 要 commit")
    parser.add_argument("--plan", action="store_true", help="只校验入参并打印计划，不写盘")
    args = parser.parse_args(argv)

    sha = args.sha.strip().lower()
    if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
        parser.error("--sha 必须是 40 位十六进制全 sha（收到 %r）—— 短 sha 会进包内 source_ref，"
                     "同一份代码换个写法就换一个制品 sha" % args.sha)
    src = Path(args.src).resolve() if args.src else None
    if src is not None:
        if not src.is_dir():
            die("--src 不是目录：" + str(src), 2)
        if args.kind == "machine-room-offline":
            die("--src 不支持 machine-room-offline（该通道要跑仓内 deploy/machine-room/offline/build.sh）", 2)
    version = args.version or (read_module_version(src) if src else module_version())
    out = Path(args.out or (REPO / "build" / "pkg"))
    check_release_surface(out)
    epoch = source_epoch()
    if epoch == 0 and not args.allow_wallclock:
        die("未设 SOURCE_DATE_EPOCH：可复现构建必须显式给时间戳，否则 built_at / 成员 mtime / "
            "gzip 头随墙钟变，同一 commit 两次构建 sha 不同。\n"
            "  正解：export SOURCE_DATE_EPOCH=$(git show -s --format=%ct <sha>)\n"
            "  确实要墙钟（本地冒烟）：加 --allow-wallclock，产物会自证 built_at_source=wallclock", 2)
    if epoch == 0:
        say("!! WARNING: --allow-wallclock —— 本产物用墙钟时间，**不可字节复现**，"
            "只能本地冒烟，不得进发布面。")
        print("!! WARNING: wallclock build —— not reproducible; smoke test only.",
              file=sys.stderr, flush=True)
    built_at = build_timestamp(epoch)
    plan = build_plan(args.kind, out, version=version, sha=sha, now=built_at, epoch=epoch, src=src,
                      built_at_source="source-date-epoch" if epoch else "wallclock")
    say("== kind     " + plan.kind + "（channel=" + plan.channel + " edition=" + plan.edition
        + "）")
    say("== 版本     " + plan.version + "  commit " + plan.commit
        + ("（--src " + str(src) + "）" if src else ""))
    say("== 输出     " + str(plan.out / plan.stem))
    # 人读行只给分类（**不打 URL**）：构建流水线对 tkbank.ai 有硬护栏，
    # 完整 URL 只出现在 BUILD_RESULT 的机器可读字段里（清单草案要用）。
    say("== 更新源   " + plan.update_source.rsplit("/", 1)[-1]
        + "（" + plan.channel + "；完整 URL 见 BUILD_RESULT.update_source）")
    say("== 可复现   SOURCE_DATE_EPOCH=" + (str(epoch) if epoch else "（未设，用墙钟）"))
    for note in plan.notes:
        say("== 注意     " + note)
    if args.plan:
        say("== plan OK（未写盘）")
        return 0
    if plan.kind in ("linux-src", "official-supply"):
        result = build_linux_like(plan, src)
    elif plan.kind == "machine-room-offline":
        result = build_machine_room_offline(plan)
    else:
        result = build_windows_assets(plan)
    say("BUILD_RESULT " + json.dumps({
        "kind": plan.kind, "channel": plan.channel, "edition": plan.edition,
        "version": plan.version, "commit": plan.commit, "built_at": plan.built_at,
        "built_at_source": plan.built_at_source,
        "update_source": plan.update_source, **result,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
