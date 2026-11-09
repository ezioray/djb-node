# SPDX-License-Identifier: Apache-2.0
"""C5 机房版交付物测试（容器 / Helm / 离线包 / 100 节点批量配置）。

设计原则：
  * 纯静态 + 临时目录：不联网、不写仓库、不依赖 helm / kubectl（本机没有这两个二进制，
    因此用 pyyaml 解析 values/Chart，用文本断言检查模板里的安全字段）；
  * 每个断言都对应任务卡里的一条硬约束（非 root、只读根、无入站、无凭据、离线安装、幂等）；
  * 需要外部工具的部分（wheel 构建工具链、openssl）缺失时 skip，不伪装成通过。

运行：
    cd worktrees/C5-machine-room
    PYTHONPATH=. <python> -m pytest tests/test_machine_room.py -o addopts="" -q
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy" / "machine-room"
CHART = DEPLOY / "helm" / "djb-node"
TEMPLATES = CHART / "templates"
OFFLINE = DEPLOY / "offline"
FLEET = DEPLOY / "fleet" / "djb-fleet.py"

sys.path.insert(0, str(REPO))
from djb_node import __version__ as CLIENT_VERSION            # noqa: E402
from djb_node import facts                                  # noqa: E402


# ─────────────────────────────────────────────────────────── 工具
def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        timeout: int = 300) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.pop("DJB_HOME", None)
    full_env["PYTHONPATH"] = str(REPO)
    if env:
        full_env.update(env)
    return subprocess.run(cmd, cwd=str(cwd or REPO), env=full_env, timeout=timeout,
                          capture_output=True, text=True)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_inventory(path: Path, rows: int, *, port: str = "1717") -> None:
    header = ("node_id,gateway,preset,port,update_channel,log_level,yield_enabled,labels,"
              "daily_tokens,weekly_tokens,monthly_tokens,concurrent,cpu,gpu,mem\n")
    presets = ("省心", "均衡", "全力以赴")
    lines = [header]
    for index in range(1, rows + 1):
        node_id = "mr-node-%03d" % index
        rack = (index - 1) // 10 + 1
        lines.append(
            "%s,https://api.example.invalid,%s,%s,stable,info,true,rack=r%02d;slot=%02d,"
            "500000,0,0,3,40,70,70\n" % (node_id, presets[index % 3], port, rack,
                                         (index - 1) % 10 + 1))
    path.write_text("".join(lines), encoding="utf-8")


def dockerfile_instructions() -> str:
    """只取 Dockerfile 的**指令行**（去注释、合并续行）。

    注释里会出现 "不 EXPOSE" / "禁止 :latest" 这类说明文字，直接全文 grep 会误判，
    所以断言一律跑在指令行上。
    """
    lines: list[str] = []
    pending: list[str] | None = None
    for raw in read(DEPLOY / "Dockerfile").splitlines():
        stripped = raw.strip()
        if pending is not None:
            pending.append(stripped)
            if not stripped.endswith("\\"):
                lines.append(" ".join(pending))
                pending = None
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending = [stripped[:-1].strip()]
            continue
        lines.append(stripped)
    if pending:
        lines.append(" ".join(pending))
    return "\n".join(lines)


def fake_wheel(directory: Path) -> Path:
    """造一个「形状像 wheel」的文件：离线包打包/校验逻辑不关心里面是什么。"""
    target = directory / ("djb_node-%s-py3-none-any.whl" % CLIENT_VERSION)
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("djb_node/__init__.py", "__version__ = %r\n" % CLIENT_VERSION)
        archive.writestr("djb_node-%s.dist-info/METADATA" % CLIENT_VERSION,
                         "Metadata-Version: 2.1\nName: djb-node\nVersion: %s\n"
                         % CLIENT_VERSION)
    return target


# ─────────────────────────────────────────────────────────── 1. Dockerfile / 镜像
def test_dockerfile_base_image_is_pinned_and_not_latest() -> None:
    instructions = dockerfile_instructions()
    froms = re.findall(r"^FROM\s+(\S+)", instructions, re.MULTILINE)
    assert froms == ["python:3.11-slim-bookworm"], froms
    assert "latest" not in instructions.lower()
    assert ":latest" not in read(DEPLOY / "Dockerfile").split("FROM", 1)[1].split("\n")[0]


def test_dockerfile_creates_non_root_uid_10001_and_runs_as_it() -> None:
    text = read(DEPLOY / "Dockerfile")
    assert "groupadd --gid 10001" in text
    assert "useradd --uid 10001" in text
    assert re.search(r"^USER 10001:10001$", text, re.MULTILINE)
    # 镜像里不能有「切回 root」的 USER 0 / USER root
    assert not re.search(r"^USER\s+(0|root)", text, re.MULTILINE)


def test_dockerfile_entrypoint_and_default_cmd() -> None:
    text = read(DEPLOY / "Dockerfile")
    assert 'ENTRYPOINT ["djb"]' in text
    assert 'CMD ["start", "--foreground", "--no-token"]' in text
    # 装的是本仓 wheel，且不引入第三方运行时依赖
    assert "pip install --no-build-isolation ." in text


def test_start_supports_no_token_flag() -> None:
    """机房制品默认 `--no-token`：一次性 local_token 不进入容器 stdout/日志。"""
    from djb_node.cli import build_parser

    args = build_parser().parse_args(["start", "--foreground", "--no-token"])
    assert args.no_token is True


def test_helm_workloads_use_no_token() -> None:
    for name in ("deployment.yaml", "daemonset.yaml"):
        text = read(DEPLOY / "helm" / "djb-node" / "templates" / name)
        assert '"start", "--foreground", "--no-token"' in text


def test_dockerfile_has_no_inbound_port_and_no_privileged_bits() -> None:
    instructions = dockerfile_instructions()
    # 指令里不能有 EXPOSE（注释里写「不 EXPOSE」是说明，不是指令）
    assert not re.search(r"^EXPOSE\b", instructions, re.MULTILINE)
    for banned in ("--privileged", "hostNetwork", "NET_ADMIN", "SYS_ADMIN",
                   "setcap", "chmod u+s", "chmod +s"):
        assert banned not in instructions, banned
    # 镜像里不装多余工具（健康检查用 python 与标准库）
    for tool in ("curl", "wget", "netcat", "apk add", "apt-get install"):
        assert tool not in instructions, tool


def test_dockerfile_volumes_are_data_config_and_healthcheck_is_loopback() -> None:
    instructions = dockerfile_instructions()
    assert 'VOLUME ["/data" "/config"]' in instructions or 'VOLUME ["/data", "/config"]' in instructions
    health = [line for line in instructions.splitlines() if line.startswith("HEALTHCHECK")]
    assert len(health) == 1, health
    # 健康检查只打容器内回环，不打外网、不依赖 curl/wget
    assert "http://127.0.0.1:" in health[0]
    assert not re.search(r"https?://(?!127\.0\.0\.1)", health[0])
    assert "DJB_HOME=/data" in instructions
    # 入口/停止信号/非 root 都在指令里
    assert 'ENTRYPOINT ["djb"]' in instructions
    assert "STOPSIGNAL SIGTERM" in instructions


def test_dockerignore_excludes_credentials_and_keeps_runtime_sources() -> None:
    ignore = read(DEPLOY / ".dockerignore")
    for pattern in (".git", "tests", "*.key", "credentials.json", "*.db", ".env"):
        assert pattern in ignore, pattern
    # 运行时必需的三样不能被忽略
    for needed in ("djb_node", "pyproject.toml", "README-CLIENT.md"):
        assert needed not in [line.strip() for line in ignore.splitlines()], needed


# ─────────────────────────────────────────────────────────── 2. Helm Chart
def test_chart_metadata_matches_client_version() -> None:
    chart = yaml.safe_load(read(CHART / "Chart.yaml"))
    assert chart["apiVersion"] == "v2"
    assert chart["name"] == "djb-node"
    assert chart["type"] == "application"
    assert chart["appVersion"] == CLIENT_VERSION
    assert re.match(r"^\d+\.\d+\.\d+$", str(chart["version"]))
    # Chart 也带三字段纪律（owner / expire_at / rollback_rule）
    annotations = chart["annotations"]
    assert annotations["djb.io/owner"]
    assert annotations["djb.io/expire-at"]
    assert annotations["djb.io/rollback-rule"]


def test_chart_required_template_files_exist() -> None:
    for name in ("deployment.yaml", "configmap.yaml", "serviceaccount.yaml",
                 "pdb.yaml", "NOTES.txt"):
        assert (TEMPLATES / name).is_file(), name
    assert (TEMPLATES / "daemonset.yaml").is_file()
    # 只绑回环 -> 不该有 Service / Ingress / NetworkPolicy 之类入站资源
    assert not (TEMPLATES / "service.yaml").exists()
    assert not (TEMPLATES / "ingress.yaml").exists()


def test_values_defaults_are_safe() -> None:
    values = yaml.safe_load(read(CHART / "values.yaml"))
    assert values["mode"] == "deployment"
    assert values["replicaCount"] >= 1
    assert values["hostNetwork"] is False
    # 镜像不许用 latest；tag 留空时用 appVersion
    assert values["image"]["tag"] == ""
    # repository/tag 里都不能出现 latest（注释里的「禁止 latest」不算）
    assert not values["image"]["repository"].endswith(":latest")
    assert "latest" not in str(values["image"]["tag"]).lower()
    # 凭据：只引用 existingSecret，默认不填、更不写明文
    assert values["auth"]["existingSecret"] == ""
    assert values["auth"]["clientKeyField"] == "client-key"
    # 网关默认是不可解析占位域，不能是生产域名
    assert values["node"]["gatewayUrl"].endswith(".invalid")
    # 特性开关默认全关
    assert values["node"]["featureFlags"] == {}
    # 资源 requests + limits 都必须有
    for section in ("requests", "limits"):
        assert values["resources"][section]["cpu"]
        assert values["resources"][section]["memory"]


def test_values_security_context_is_least_privilege() -> None:
    values = yaml.safe_load(read(CHART / "values.yaml"))
    pod = values["podSecurityContext"]
    assert pod["runAsNonRoot"] is True
    assert pod["runAsUser"] == 10001
    assert pod["runAsGroup"] == 10001
    assert pod["seccompProfile"]["type"] == "RuntimeDefault"
    container = values["securityContext"]
    assert container["runAsNonRoot"] is True
    assert container["readOnlyRootFilesystem"] is True
    assert container["allowPrivilegeEscalation"] is False
    assert container["privileged"] is False
    assert container["capabilities"]["drop"] == ["ALL"]


def test_values_carry_no_secret_material() -> None:
    for name in ("values.yaml", "Chart.yaml", "templates/NOTES.txt"):
        text = read(CHART / name)
        for pattern in (r"sk-[A-Za-z0-9]{8,}", r"BEGIN [A-Z ]*PRIVATE KEY",
                        r"gh[pousr]_[A-Za-z0-9]{8,}", r"AKIA[0-9A-Z]{16}"):
            assert not re.search(pattern, text), (name, pattern)
        assert "password:" not in text.lower() or "password input" in text.lower()
        assert "client-key:" not in text       # 不允许把 key 当字面量写进 values


def test_deployment_template_has_security_and_no_inbound() -> None:
    text = read(TEMPLATES / "deployment.yaml")
    # 安全上下文必须来自 values（模板不能自己发明宽松值）
    assert ".Values.securityContext" in text
    assert ".Values.podSecurityContext" in text
    # 硬值 / 禁项
    assert "hostNetwork: false" in text
    assert "hostNetwork: true" not in text
    assert "hostPID: false" in text and "hostIPC: false" in text
    assert "privileged: true" not in text
    # deployment 不许碰宿主目录（注释里提到 daemonset 的 hostPath 不算）
    assert not re.search(r"^\s*hostPath:", text, re.MULTILINE)
    # 无入站端口：容器不声明 ports
    assert not re.search(r"^\s+ports:\s*$", text, re.MULTILINE)
    # 探针只走容器内回环
    assert text.count("http://127.0.0.1:") >= 3    # startup/liveness/readiness
    # httpGet 会打 Pod IP，绑回环时必失败 -> 只允许 exec 探针
    assert not re.search(r"^\s*httpGet:", text, re.MULTILINE)
    assert "exec:" in text
    # 凭据只从 existingSecret 引用
    assert "existingSecret" in text
    assert "secretName:" in text
    # 只读根文件系统下 /tmp 必须挂可写卷
    assert "mountPath: /tmp" in text
    assert "readOnlyRootFilesystem" not in text or ".Values.securityContext" in text


def test_configmap_template_enforces_three_field_feature_flags() -> None:
    text = read(TEMPLATES / "configmap.yaml")
    assert "featureFlags" in text
    for field in ("owner", "expire_at", "rollback_rule"):
        assert field in text, field
    assert "fail" in text                       # 缺字段 -> 渲染直接失败
    assert "feature_flags" in text


def test_daemonset_is_gated_by_mode_and_pdb_only_for_deployment() -> None:
    daemonset = read(TEMPLATES / "daemonset.yaml")
    pdb = read(TEMPLATES / "pdb.yaml")
    assert 'eq .Values.mode "daemonset"' in daemonset
    assert 'eq .Values.mode "deployment"' in pdb
    assert "kind: DaemonSet" in daemonset
    assert "kind: PodDisruptionBudget" in pdb
    # DaemonSet 的 hostPath 必须是显式开关，默认关闭（values 里 enabled=false）
    assert "nodeLocalData.enabled" in daemonset
    values = yaml.safe_load(read(CHART / "values.yaml"))
    assert values["nodeLocalData"]["enabled"] is False


def test_templates_are_balanced_and_parseable_shapes() -> None:
    for path in sorted(TEMPLATES.iterdir()):
        if not path.is_file():
            continue
        text = read(path)
        assert text.count("{{") == text.count("}}"), path.name
        opens = len(re.findall(r"\{\{-?\s*(?:if|range|with|define|block)\b", text))
        ends = len(re.findall(r"\{\{-?\s*end\b", text))
        assert opens == ends, (path.name, opens, ends)
    # 除 _helpers.tpl / NOTES.txt 外，每个模板都要有 apiVersion + kind
    for name in ("deployment.yaml", "configmap.yaml", "serviceaccount.yaml",
                 "pdb.yaml", "daemonset.yaml"):
        text = read(TEMPLATES / name)
        assert "apiVersion:" in text and "kind:" in text, name


def _values_paths(node: object, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            path = prefix + str(key)
            paths.add(path)
            paths |= _values_paths(value, path + ".")
    return paths


def test_templates_reference_only_existing_values_and_helpers() -> None:
    """便宜的「拼写检查」：模板里引用的 .Values.* 必须在 values.yaml 里存在，
    include 的模板名必须在 _helpers.tpl 里定义过（本机没有 helm，这是能自动化的下限）。"""
    values = yaml.safe_load(read(CHART / "values.yaml"))
    known_paths = _values_paths(values)
    helpers = read(TEMPLATES / "_helpers.tpl")
    defined = set(re.findall(r'\{\{-?\s*define\s+"([^"]+)"', helpers))
    assert defined, "helpers 里没有任何 define"

    for path in sorted(TEMPLATES.iterdir()):
        if not path.is_file():
            continue
        text = read(path)
        for reference in set(re.findall(r"\.Values((?:\.[A-Za-z0-9_]+)+)", text)):
            dotted = reference.lstrip(".")
            assert dotted in known_paths, (path.name, dotted)
        for used in set(re.findall(r'include\s+"([^"]+)"', text)):
            assert used in defined, (path.name, used)


def _literal_template_output(text: str) -> str:
    """粗略模拟 Go 模板的空白裁剪：把每个 {{ ... }} 当作零输出，只保留字面文本。

    用途：本机没有 helm，但「{{- end -}} 把 apiVersion 粘到上一行注释里」这类
    灾难性错误可以靠它提前抓到（注释吞掉 API 版本 -> kubectl 直接拒绝）。
    """
    out: list[str] = []
    index = 0
    while index < len(text):
        start = text.find("{{", index)
        if start < 0:
            out.append(text[index:])
            break
        literal = text[index:start]
        if text.startswith("{{-", start):
            literal = literal.rstrip()              # 左裁剪：吃掉前导空白
        out.append(literal)
        end = text.find("}}", start)
        assert end > 0, "未闭合的 {{"
        right_trim = end - 1 >= 0 and text[end - 1] == "-"
        index = end + 2
        if right_trim:                              # 右裁剪：吃掉后续空白
            while index < len(text) and text[index] in " \t\r\n":
                index += 1
    return "".join(out)


def test_whitespace_trim_does_not_glue_manifests_into_comments() -> None:
    for name in ("deployment.yaml", "daemonset.yaml", "configmap.yaml",
                 "serviceaccount.yaml", "pdb.yaml", "pvc.yaml"):
        rendered = _literal_template_output(read(TEMPLATES / name))
        assert "\napiVersion:" in rendered, name
        line = next(item for item in rendered.splitlines() if item.startswith("apiVersion:"))
        assert line == "apiVersion:" + line[len("apiVersion:"):]
        # apiVersion 前面不能是同一行的注释内容
        assert "#" not in line


def test_notes_documents_rollback_and_no_inbound() -> None:
    notes = read(TEMPLATES / "NOTES.txt")
    for token in ("rollback", "uninstall", "djb auth --set-key", "Service", "127.0.0.1"):
        assert token in notes, token


# ─────────────────────────────────────────────────────────── 3. 离线包
def test_offline_scripts_are_offline_and_secret_free() -> None:
    install = read(OFFLINE / "install.sh")
    build = read(OFFLINE / "build.sh")
    verify = read(OFFLINE / "verify.sh")
    # 安装必须离线：--no-index + --find-links
    assert "--no-index" in install
    assert "--find-links" in install
    assert "PIP_NO_INDEX=1" in install
    # 装后自检必须用 -I（否则在仓库根里 import 到源码目录，装失败也会「看起来成功」）
    assert "-I -c" in install
    # 构建不要求联网（第一优先级是本地构建后端），且支持复用已有 wheel
    assert "--no-build-isolation" in build
    assert "--wheel" in build
    # 签名只靠环境变量，绝不硬编码密钥
    assert "DJB_OFFLINE_SIGNING_KEY" in build
    assert "BEGIN" not in build and "BEGIN" not in verify
    for text in (install, build, verify):
        assert not re.search(r"sk-[A-Za-z0-9]{8,}", text)
        assert "ssh-rsa" not in text


def test_offline_package_build_verify_and_tamper(tmp_path: Path) -> None:
    wheel = fake_wheel(tmp_path)
    out = tmp_path / "pkg"
    built = run([str(OFFLINE / "build.sh"), "--wheel", str(wheel), "--out", str(out)],
                env={"PYTHON": sys.executable})
    assert built.returncode == 0, built.stdout + built.stderr
    assert "未签名" in built.stdout                       # 没给私钥 -> 明确说未签名
    names = sorted(item.name for item in out.iterdir())
    assert "manifest.json" in names and "sha256sums" in names
    assert "verify.sh" in names and "install.sh" in names
    assert "sha256sums.sig" not in names                  # 没有密钥就不该有签名

    manifest = json.loads(read(out / "manifest.json"))
    assert manifest["name"] == "djb-node"
    assert manifest["version"] == CLIENT_VERSION
    assert manifest["kind"] == "offline"
    assert manifest["runtime_dependencies"] == []
    assert manifest["signature"] is None
    assert len(manifest["files"]) == 3

    ok = run([str(out / "verify.sh")])
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "verify: OK" in ok.stdout

    # 篡改任一文件都必须非 0
    for name in ("djb_node-%s-py3-none-any.whl" % CLIENT_VERSION, "manifest.json",
                 "install.sh"):
        target = out / name
        original = target.read_bytes()
        target.write_bytes(original + b"\n# tampered\n")
        broken = run([str(out / "verify.sh")])
        assert broken.returncode != 0, name
        target.write_bytes(original)
        assert run([str(out / "verify.sh")]).returncode == 0


def test_offline_package_signature_roundtrip(tmp_path: Path) -> None:
    if shutil.which("openssl") is None:
        pytest.skip("没有 openssl，跳过签名往返测试")
    key = tmp_path / "sign.pem"
    pub = tmp_path / "verify.pem"
    # RC8：离线包签名统一 Ed25519（openssl dgst 对 Ed25519 在 OpenSSL 3 直接失败）
    subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)],
                   check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub)],
                   check=True, capture_output=True)
    out = tmp_path / "pkg"
    built = run([str(OFFLINE / "build.sh"), "--wheel", str(fake_wheel(tmp_path)),
                 "--out", str(out)],
                env={"PYTHON": sys.executable, "DJB_OFFLINE_SIGNING_KEY": str(key),
                     "DJB_OFFLINE_KEY_ID": "djb-prod-test"})
    assert built.returncode == 0, built.stdout + built.stderr
    assert (out / "sha256sums.sig").is_file()
    manifest = json.loads(read(out / "manifest.json"))
    assert manifest["signature"]["file"] == "sha256sums.sig"
    assert manifest["signature"]["algorithm"] == "ed25519"
    assert manifest["signature"]["key_id"] == "djb-prod-test"

    good = run([str(out / "verify.sh")], env={"DJB_OFFLINE_VERIFY_KEY": str(pub)})
    assert good.returncode == 0, good.stdout + good.stderr
    assert "签名校验通过" in good.stdout

    # RC8：有签名但没公钥 -> **默认拒绝**（不再静默降级为仅哈希）；显式 STRICT=0 才允许
    assert run([str(out / "verify.sh")]).returncode != 0
    lenient = run([str(out / "verify.sh")], env={"DJB_OFFLINE_STRICT": "0"})
    assert lenient.returncode == 0, lenient.stdout + lenient.stderr
    assert "DJB_OFFLINE_STRICT=0" in lenient.stdout

    # RC8：非 Ed25519 私钥必须被 build.sh 直接拒（不许"签了但验不了"）
    rsa = tmp_path / "rsa.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
                    "rsa_keygen_bits:2048", "-out", str(rsa)],
                   check=True, capture_output=True)
    rejected = run([str(OFFLINE / "build.sh"), "--wheel", str(fake_wheel(tmp_path)),
                    "--out", str(tmp_path / "rsa-pkg")],
                   env={"PYTHON": sys.executable, "DJB_OFFLINE_SIGNING_KEY": str(rsa)})
    assert rejected.returncode == 2
    assert "Ed25519" in rejected.stderr + rejected.stdout

    # 改动 sha256sums -> 签名必然失败
    sums = out / "sha256sums"
    original = sums.read_bytes()
    sums.write_bytes(original.replace(b"manifest.json", b"manifest.json "))
    broken = run([str(out / "verify.sh")], env={"DJB_OFFLINE_VERIFY_KEY": str(pub)})
    assert broken.returncode != 0
    sums.write_bytes(original)
    assert run([str(out / "verify.sh")],
               env={"DJB_OFFLINE_VERIFY_KEY": str(pub)}).returncode == 0


def test_offline_install_rejects_broken_package(tmp_path: Path) -> None:
    out = tmp_path / "pkg"
    built = run([str(OFFLINE / "build.sh"), "--wheel", str(fake_wheel(tmp_path)),
                 "--out", str(out)], env={"PYTHON": sys.executable})
    assert built.returncode == 0, built.stdout + built.stderr
    (out / "install.sh").write_text(read(out / "install.sh") + "\n# tampered\n",
                                    encoding="utf-8")
    result = run([str(out / "install.sh"), "--python", sys.executable, "--dry-run"])
    assert result.returncode != 0                     # verify.sh 挡住了
    assert "verify" in (result.stdout + result.stderr)


# ─────────────────────────────────────────────────────────── 4. 100 节点批量配置
def test_fleet_constants_match_client_runtime() -> None:
    source = read(FLEET)
    assert 'PRESETS: tuple[str, ...] = ("省心", "均衡", "全力以赴")' in source
    assert "PORT_MIN = %d" % facts.PORT_MIN in source
    assert "PORT_MAX = %d" % facts.PORT_MAX in source
    assert "DEFAULT_MIRROR_PORT = %d" % facts.MIRROR_PORT in source
    assert facts.PRESETS == ("省心", "均衡", "全力以赴")


def test_fleet_100_nodes_generate_then_check_zero_diff(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.csv"
    write_inventory(inventory, 100)
    out = tmp_path / "out"
    common = [sys.executable, str(FLEET), "--inventory", str(inventory),
              "--template", str(DEPLOY / "fleet" / "config.json.template"),
              "--out", str(out)]

    first = run(common)
    assert first.returncode == 0, first.stdout + first.stderr
    assert (out / "manifest.json").is_file()
    manifest = json.loads(read(out / "manifest.json"))
    assert manifest["count"] == 100
    assert manifest["credentials"] == "none"
    assert len(manifest["nodes"]) == 100
    assert (out / "mr-node-001" / "config.json").is_file()
    assert (out / "mr-node-100" / "config.json").is_file()

    second = run(common + ["--check"])
    assert second.returncode == 0, second.stdout + second.stderr
    assert "差异=0" in second.stdout and "缺失=0" in second.stdout
    assert "未变=201" in second.stdout            # 100 config + 100 init.args + manifest

    # 生成的配置必须真的能被客户端解析（tolerant reader，不崩）
    from djb_node.config import ClientConfig
    config = ClientConfig.from_dict(json.loads(read(out / "mr-node-042" / "config.json")),
                                    tolerate=False)
    assert config.mirror_port == 1717
    assert config.gateway_url == "https://api.example.invalid"
    assert config.extra["node_id"] == "mr-node-042"
    assert config.extra["labels"] == {"rack": "r05", "slot": "02"}


def test_fleet_check_detects_drift_and_dry_run_does_not_write(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.csv"
    write_inventory(inventory, 5)
    out = tmp_path / "out"
    base = [sys.executable, str(FLEET), "--inventory", str(inventory),
            "--template", str(DEPLOY / "fleet" / "config.json.template"),
            "--out", str(out)]
    assert run(base).returncode == 0

    drifted = out / "mr-node-003" / "config.json"
    drifted.write_text('{"mirror_port": 1999}\n', encoding="utf-8")
    checked = run(base + ["--check"])
    assert checked.returncode == 1
    assert "差异=1" in checked.stdout

    dry = run(base + ["--dry-run"])
    assert dry.returncode == 1
    assert "--- 现有/mr-node-003/config.json" in dry.stdout
    assert read(drifted) == '{"mirror_port": 1999}\n'        # 没写盘

    fresh = tmp_path / "fresh"
    missing = run([sys.executable, str(FLEET), "--inventory", str(inventory),
                   "--template", str(DEPLOY / "fleet" / "config.json.template"),
                   "--out", str(fresh), "--dry-run"])
    assert missing.returncode == 1
    assert not fresh.exists()                                 # dry-run 不建目录


def test_fleet_rejects_duplicate_node_id(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.csv"
    inventory.write_text(
        "node_id,gateway,preset,port\n"
        "mr-node-001,https://api.example.invalid,省心,1717\n"
        "mr-node-001,https://api.example.invalid,均衡,1717\n", encoding="utf-8")
    result = run([sys.executable, str(FLEET), "--inventory", str(inventory),
                  "--template", str(DEPLOY / "fleet" / "config.json.template"),
                  "--out", str(tmp_path / "out")])
    assert result.returncode == 2
    assert "node_id 重复" in result.stderr


@pytest.mark.parametrize("port", ["0", "22", "1023", "65536", "70000", "abc", ""])
def test_fleet_rejects_invalid_port(tmp_path: Path, port: str) -> None:
    inventory = tmp_path / "inventory.csv"
    inventory.write_text(
        "node_id,gateway,preset,port\n"
        "mr-node-001,https://api.example.invalid,省心,%s\n" % port, encoding="utf-8")
    result = run([sys.executable, str(FLEET), "--inventory", str(inventory),
                  "--template", str(DEPLOY / "fleet" / "config.json.template"),
                  "--out", str(tmp_path / "out")])
    assert result.returncode == 2
    assert "port" in result.stderr


def test_fleet_rejects_credentials_and_bad_gateway(tmp_path: Path) -> None:
    template = str(DEPLOY / "fleet" / "config.json.template")
    # 1) 凭据列
    credential_col = tmp_path / "cred.csv"
    credential_col.write_text(
        "node_id,gateway,preset,port,client_key\n"
        "mr-node-001,https://api.example.invalid,省心,1717,sk-live-abc123456\n",
        encoding="utf-8")
    result = run([sys.executable, str(FLEET), "--inventory", str(credential_col),
                  "--template", template, "--out", str(tmp_path / "out")])
    assert result.returncode == 2
    assert "凭据" in result.stderr
    # 2) 值里藏凭据形态的字符串
    credential_value = tmp_path / "val.csv"
    credential_value.write_text(
        "node_id,gateway,preset,port,labels\n"
        "mr-node-001,https://api.example.invalid,省心,1717,key=sk-live-abc123456\n",
        encoding="utf-8")
    result = run([sys.executable, str(FLEET), "--inventory", str(credential_value),
                  "--template", template, "--out", str(tmp_path / "out")])
    assert result.returncode == 2
    assert "疑似包含凭据" in result.stderr
    # 3) gateway 带 userinfo（口令）必须拒绝
    userinfo = tmp_path / "userinfo.csv"
    userinfo.write_text(
        "node_id,gateway,preset,port\n"
        "mr-node-001,https://user:pass@api.example.invalid,省心,1717\n", encoding="utf-8")
    result = run([sys.executable, str(FLEET), "--inventory", str(userinfo),
                  "--template", template, "--out", str(tmp_path / "out")])
    assert result.returncode == 2
    assert "userinfo" in result.stderr
    # 4) 预设必须是登记过的三种
    preset = tmp_path / "preset.csv"
    preset.write_text(
        "node_id,gateway,preset,port\n"
        "mr-node-001,https://api.example.invalid,狂飙,1717\n", encoding="utf-8")
    result = run([sys.executable, str(FLEET), "--inventory", str(preset),
                  "--template", template, "--out", str(tmp_path / "out")])
    assert result.returncode == 2
    assert "preset" in result.stderr
    assert not (tmp_path / "out").exists()          # 出错时一个字都不写


def test_fleet_outputs_contain_no_secret_shapes(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.csv"
    write_inventory(inventory, 20)
    out = tmp_path / "out"
    assert run([sys.executable, str(FLEET), "--inventory", str(inventory),
                "--template", str(DEPLOY / "fleet" / "config.json.template"),
                "--out", str(out)]).returncode == 0
    for path in out.rglob("*"):
        if not path.is_file():
            continue
        text = read(path)
        for pattern in (r"sk-[A-Za-z0-9]{8,}", r"Bearer\s", r"gh[pousr]_",
                        r"BEGIN [A-Z ]*PRIVATE KEY"):
            assert not re.search(pattern, text), (path, pattern)
        # 也不许出现任何「token=...」形态
        assert not re.search(r"(?i)client[_-]?key\s*[:=]\s*\S", text), path


def test_fleet_example_inventory_is_100_nodes(tmp_path: Path) -> None:
    example = DEPLOY / "fleet" / "inventory.example.csv"
    lines = [line for line in read(example).splitlines() if line.strip()]
    assert len(lines) == 101                          # 表头 + 100 节点
    out = tmp_path / "out"
    assert run([sys.executable, str(FLEET), "--inventory", str(example),
                "--template", str(DEPLOY / "fleet" / "config.json.template"),
                "--out", str(out)]).returncode == 0
    assert json.loads(read(out / "manifest.json"))["count"] == 100
    assert sorted({line.split(",")[1] for line in lines[1:]}) == [
        "https://api.example.invalid"]


# ─────────────────────────────────────────────────────────── 5. README 验收
def test_readme_covers_three_install_paths_and_lifecycle() -> None:
    text = read(DEPLOY / "README.md")
    for token in ("Docker", "Helm", "离线包", "升级", "回滚", "卸载", "验收"):
        assert token in text, token
    assert "--no-index" in text
    assert "existingSecret" in text
    assert "127.0.0.1" in text
