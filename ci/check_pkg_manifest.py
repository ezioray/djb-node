#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""制品校验器（B7 / RC8.3-C）：把「构建出来的包」和「清单口径」对上。

三件事必须同时成立才算过：

1. **包内自洽**：VERSION.txt 的 version/commit、包内 SHA256SUMS.txt 与实际文件、
   wheel 与 sha256sums 互相对得上（篡改/半包/混版本一律不过）；
2. **五通道显式更新源**：每个包里都能读到**显式的** ``update_source``，且与
   :data:`djb_node.update_source.MANIFEST_URL` 一致（空 = 不通过，不是警告）；
3. **与清单草案一致**（给了 ``--manifest`` 时）：MANIFEST.json 草案里该文件的
   ``sha256`` / ``size_bytes`` / ``version`` 必须与磁盘上的制品逐项相同。

CI 里不带 ``--manifest``（草案不在仓内，发布面归 release-manager）；
本地/发布前用它验 ``ops/rc/artifacts/downloads/draft-<ver>/MANIFEST.json``。

用法：
    python3 ci/check_pkg_manifest.py --pkg-dir dist
    python3 ci/check_pkg_manifest.py --pkg-dir dist \
        --manifest ../ops/rc/artifacts/downloads/draft-0.3.1/MANIFEST.json --version 0.3.1

退出码：0 全过 · 1 有失败 · 2 参数/输入不合格。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

KINDS = ("linux-src", "official-supply", "machine-room-offline", "windows-assets")


def say(text: str) -> None:
    print(text, flush=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_url() -> str:
    from djb_node import update_source

    return update_source.MANIFEST_URL


@dataclass
class Checks:
    kind: str
    file: str
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def expect(self, condition: bool, message: str) -> None:
        if not condition:
            self.failures.append(message)

    @property
    def ok(self) -> bool:
        return not self.failures


def read_tar_text(path: Path, suffix: str) -> str:
    """取包内**顶层目录**下名为 suffix 的文件内容（找不到返回空串）。"""
    with tarfile.open(path, "r:gz") as handle:
        for member in handle.getmembers():
            if not member.isfile():
                continue
            parts = member.name.split("/")
            if len(parts) == 2 and parts[1] == suffix:
                extracted = handle.extractfile(member)
                if extracted is not None:
                    return extracted.read().decode("utf-8", "replace")
    return ""


def tar_members(path: Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(path, "r:gz") as handle:
        return {member.name: member for member in handle.getmembers() if member.isfile()}


def verify_inner_sha256sums(path: Path, names: list[str], top: str) -> list[str]:
    """包内 SHA256SUMS.txt（``<sha>  <rel>``）与实际内容比对。"""
    problems: list[str] = []
    text = read_tar_text(path, "SHA256SUMS.txt")
    if not text.strip():
        return ["包内缺 SHA256SUMS.txt 或为空"]
    listed: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        listed[name.strip()] = digest.strip()
    with tarfile.open(path, "r:gz") as handle:
        for member in handle.getmembers():
            if not member.isfile():
                continue
            parts = member.name.split("/")
            if len(parts) != 2 or parts[0] != top:
                continue
            rel = parts[1]
            if rel == "SHA256SUMS.txt":
                continue
            if rel not in listed:
                problems.append("SHA256SUMS.txt 漏了 " + rel)
                continue
            extracted = handle.extractfile(member)
            digest = sha256_bytes(extracted.read() if extracted else b"")
            if digest != listed[rel]:
                problems.append("SHA256SUMS.txt 与实际内容不符：" + rel)
    for rel in listed:
        if rel not in names:
            problems.append("SHA256SUMS.txt 列了不存在的文件：" + rel)
    return problems


def check_linux_like(check: Checks, path: Path, *, version: str, edition: str,
                     supply: bool) -> None:
    with tarfile.open(path, "r:gz") as handle:
        names = [member.name for member in handle.getmembers()]
    tops = {name.split("/")[0] for name in names}
    check.expect(len(tops) == 1, "包顶层目录不唯一：" + ", ".join(sorted(tops)))
    top = next(iter(tops)) if tops else ""
    check.expect(top == path.name[: -len(".tar.gz")], "顶层目录名与文件名不一致：" + top)
    rels = {name.split("/", 1)[1] for name in names
            if name.startswith(top + "/") and name.count("/") == 1}
    for required in ("djb", "VERSION.txt", "README-INSTALL.txt", "SHA256SUMS.txt",
                     "djb_node/__init__.py", "pyproject.toml"):
        check.expect(required in rels, "包内缺 " + required)
    if supply:
        for required in ("install.sh", "config.json", "djb-official-supply.service",
                         "README.md"):
            check.expect("profiles/official-supply/" + required in names,
                         "包内缺 profiles/official-supply/" + required)

    version_txt = read_tar_text(path, "VERSION.txt")
    fields = {}
    for line in version_txt.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    check.expect(fields.get("version") == version,
                 "VERSION.txt version=" + str(fields.get("version")) + "，期望 " + version)
    check.expect(fields.get("edition") == edition,
                 "VERSION.txt edition=" + str(fields.get("edition")) + "，期望 " + edition)
    check.expect(bool(fields.get("update_source")),
                 "VERSION.txt 缺 update_source（五通道必须显式）")
    check.expect(fields.get("update_source") == manifest_url(),
                 "VERSION.txt update_source 与 MANIFEST_URL 不一致")
    readme = read_tar_text(path, "README-INSTALL.txt")
    check.expect(manifest_url() in readme, "README-INSTALL.txt 没写出显式 update_source")
    check.expect("--update-source" in readme, "README-INSTALL.txt 的 init 命令缺 --update-source")
    check.expect("--edition " + edition in readme,
                 "README-INSTALL.txt 的 init 命令缺 --edition " + edition)
    if supply:
        config = json.loads(read_tar_text(path, "profiles/official-supply/config.json") or "{}")
        check.expect(config.get("update_source") == manifest_url(),
                     "official-supply config.json update_source 不是显式官方清单")
        check.expect(config.get("edition") == "official-supply",
                     "official-supply config.json 缺 edition=official-supply")
    problems = verify_inner_sha256sums(path, sorted(rels), top)
    for item in problems:
        check.failures.append(item)


def check_machine_room(check: Checks, path: Path, *, version: str) -> None:
    with tarfile.open(path, "r:gz") as handle:
        names = [member.name for member in handle.getmembers() if member.isfile()]
    tops = {name.split("/")[0] for name in names}
    check.expect(len(tops) == 1, "包顶层目录不唯一")
    top = next(iter(tops)) if tops else ""
    rels = {name.split("/", 1)[1] for name in names}
    for required in ("manifest.json", "sha256sums", "install.sh", "verify.sh",
                     "config.template.json", "VERSION.txt"):
        check.expect(required in rels, "包内缺 " + required)
    manifest = json.loads(read_tar_text(path, "manifest.json") or "{}")
    check.expect(manifest.get("version") == version,
                 "manifest.json version=" + str(manifest.get("version")))
    check.expect(manifest.get("kind") == "offline", "manifest.json kind 不是 offline")
    check.expect(manifest.get("channel") == "machine-room", "manifest.json 缺 channel")
    check.expect(manifest.get("edition") == "machine-room", "manifest.json 缺 edition")
    check.expect(manifest.get("update_source") == manifest_url(),
                 "manifest.json 的 update_source 不是显式官方清单（0.3.1 必须显式）")
    template = json.loads(read_tar_text(path, "config.template.json") or "{}")
    check.expect(template.get("update_source") == manifest_url(),
                 "config.template.json 的 update_source 不对")
    check.expect(template.get("edition") == "machine-room",
                 "config.template.json 的 edition 不对")
    install = read_tar_text(path, "install.sh")
    check.expect("--update-source" in install, "install.sh 没有 --update-source 入口")
    # sha256sums 与实际内容逐一比对（wheel 混版本最常见）
    listed: dict[str, str] = {}
    for line in (read_tar_text(path, "sha256sums") or "").splitlines():
        if line.strip():
            digest, _, name = line.partition("  ")
            listed[name.strip()] = digest.strip()
    with tarfile.open(path, "r:gz") as handle:
        for member in handle.getmembers():
            if not member.isfile():
                continue
            parts = member.name.split("/")
            if len(parts) != 2:
                continue
            rel = parts[1]
            if rel in ("sha256sums", "VERSION.txt", "README-INSTALL.txt", "SHA256SUMS.txt"):
                continue
            if rel not in listed:
                check.failures.append("sha256sums 漏了 " + rel)
                continue
            extracted = handle.extractfile(member)
            if sha256_bytes(extracted.read() if extracted else b"") != listed[rel]:
                check.failures.append("sha256sums 与实际内容不符：" + rel)


def check_windows_assets(check: Checks, path: Path, *, version: str) -> None:
    with zipfile.ZipFile(path) as handle:
        names = handle.namelist()
        tops = {name.split("/")[0] for name in names}
        check.expect(len(tops) == 1, "zip 顶层目录不唯一")
        top = next(iter(tops)) if tops else ""
        inner = {name.split("/", 1)[1] for name in names
                 if name.startswith(top + "/") and name.count("/") == 1}
        for required in ("config.json", "start-windows.bat", "VERSION.txt",
                         "README-WINDOWS.txt", "SHA256SUMS.txt"):
            check.expect(required in inner, "zip 内缺 " + required)
        config = json.loads(handle.read(top + "/config.json").decode("utf-8"))
        check.expect(config.get("update_source") == manifest_url(),
                     "Windows config.json 的 update_source 不是显式官方清单")
        check.expect(config.get("edition") == "consumer", "Windows config.json 缺 edition")
        bat = handle.read(top + "/start-windows.bat").decode("utf-8", "replace")
        check.expect("--update-source" in bat, "start-windows.bat 的 init 缺 --update-source")
        version_txt = handle.read(top + "/VERSION.txt").decode("utf-8", "replace")
        check.expect("update_source    : " + manifest_url() in version_txt,
                     "VERSION.txt 缺显式 update_source")
        check.expect(("version          : " + version) in version_txt,
                     "VERSION.txt version 不符")


def detect(path: Path) -> str | None:
    name = path.name
    if name.endswith("-linux-x64-src.tar.gz"):
        return "linux-src"
    if name.endswith("-official-supply-x64.tar.gz"):
        return "official-supply"
    if name.endswith("-machine-room-offline.tar.gz"):
        return "machine-room-offline"
    if name.endswith("-windows-x64-artifacts.zip"):
        return "windows-assets"
    return None


def load_manifest(path: Path) -> tuple[str, dict[str, dict]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    body = payload.get("payload", payload)
    version = str(body.get("version") or "")
    rows = {}
    for item in body.get("artifacts") or body.get("channels") or []:
        file_name = item.get("file") or str(item.get("url", "")).rsplit("/", 1)[-1]
        if file_name:
            rows[file_name] = item
    return version, rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DJB 客户端制品校验（B7）")
    parser.add_argument("--pkg-dir", required=True)
    parser.add_argument("--version", default="", help="期望版本（默认取 djb_node.__version__）")
    parser.add_argument("--manifest", default="", help="MANIFEST.json / update-manifest 草案")
    parser.add_argument("--require", default="",
                        help="逗号分隔的 kind 列表；缺一类就失败（CI 用）")
    args = parser.parse_args(argv)

    pkg_dir = Path(args.pkg_dir)
    if not pkg_dir.is_dir():
        say("输入不合格：制品目录不存在 " + str(pkg_dir))
        return 2
    version = args.version
    if not version:
        from djb_node import __version__

        version = __version__
    expected_version, manifest_rows = ("", {})
    if args.manifest:
        expected_version, manifest_rows = load_manifest(Path(args.manifest))
        if expected_version and expected_version != version:
            say("输入不合格：清单版本 " + expected_version + " ≠ 期望版本 " + version)
            return 2

    checks: list[Checks] = []
    seen: dict[str, Path] = {}
    for path in sorted(pkg_dir.iterdir()):
        kind = detect(path)
        if kind is None:
            continue
        check = Checks(kind=kind, file=path.name)
        seen[kind] = path
        if kind == "linux-src":
            check_linux_like(check, path, version=version, edition="consumer", supply=False)
        elif kind == "official-supply":
            check_linux_like(check, path, version=version, edition="official-supply", supply=True)
        elif kind == "machine-room-offline":
            check_machine_room(check, path, version=version)
        else:
            check_windows_assets(check, path, version=version)
        digest = sha256_file(path)
        check.notes.append("sha256=" + digest)
        check.notes.append("bytes=" + str(path.stat().st_size))
        if manifest_rows:
            row = manifest_rows.get(path.name)
            if row is None:
                check.failures.append("清单里没有该文件（" + path.name + "）")
            else:
                if str(row.get("sha256")) != digest:
                    check.failures.append("sha256 与清单不一致")
                size = row.get("size_bytes") or row.get("size")
                if size is not None and int(size) != path.stat().st_size:
                    check.failures.append("size 与清单不一致")
                if str(row.get("version") or version) != version:
                    check.failures.append("清单 version 不一致")
        checks.append(check)

    required = [item for item in args.require.split(",") if item]
    for kind in required:
        if kind not in seen:
            missing = Checks(kind=kind, file="(缺)")
            missing.failures.append("缺制品：" + kind)
            checks.append(missing)

    failed = [item for item in checks if not item.ok]
    for item in checks:
        say("---- " + item.file + " [" + item.kind + "] "
            + ("OK" if item.ok else "FAIL"))
        for note in item.notes:
            say("     " + note)
        for problem in item.failures:
            say("     ✗ " + problem)
    say("PKG_CHECK_RESULT " + json.dumps({
        "version": version, "pkg_dir": str(pkg_dir), "checked": len(checks),
        "failed": len(failed),
        "kinds": sorted(seen), "manifest": args.manifest or None,
        "ok": not failed,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
