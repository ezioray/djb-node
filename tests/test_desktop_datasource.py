# SPDX-License-Identifier: Apache-2.0
"""数据层回归：真实现降级、GPU 解析、日志 ring buffer、假实现、协议形状。

原则：**任何外部依赖失败都必须是「暂不可用」，不许抛栈、不许空白、不许用 0 冒充无数据。**
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from djb_node.desktop import datasource as ds


# ─────────────────────────────────────────────────────────────── 基础类型
def test_unavailable_requires_reason_and_reads_clearly():
    with pytest.raises(ValueError):
        ds.Unavailable("")
    item = ds.Unavailable("云端未接通")
    assert item.text == "暂不可用（云端未接通）"
    assert item.to_dict()["status"] == "unavailable"


def test_snapshot_projection_helpers():
    snapshot = ds.Snapshot({"status": {"state": "online"}, "gpu": ds.Unavailable("没有 GPU")})
    assert snapshot.ok("status") is True
    assert snapshot.ok("gpu") is False
    assert snapshot.missing("gpu").reason == "没有 GPU"
    assert snapshot.text_of("gpu") == "暂不可用（没有 GPU）"
    assert snapshot.text_of("status", "state") == "online"
    assert snapshot.text_of("status", "missing", "—") == "—"


def test_sections_cover_thirteen_domains():
    assert len(ds.SECTIONS) == 13
    assert ds.SECTIONS[0] == "status" and ds.SECTIONS[-1] == "logs"
    assert "token_plan" in ds.SECTIONS


# ─────────────────────────────────────────────────────────────── 假实现
def test_null_source_is_all_unavailable():
    snapshot = ds.NullDataSource("测试：无网络").snapshot()
    assert set(snapshot.sections) == set(ds.SECTIONS)
    for name in ds.SECTIONS:
        assert isinstance(snapshot.get(name), ds.Unavailable)
        assert "暂不可用" in snapshot.text_of(name)


def test_fake_source_reports_missing_sections():
    source = ds.FakeDataSource({"status": {"state": "paused"}})
    snapshot = source.snapshot()
    assert snapshot.get("status")["state"] == "paused"
    assert "测试未提供 gpu" in snapshot.get("gpu").reason
    assert source.calls == 1


# ─────────────────────────────────────────────────────────────── GPU 解析
def test_parse_nvidia_smi():
    text = "NVIDIA GeForce RTX 4070, 12282, 1024, 45, 12\nNVIDIA A100, 40960, 0, 33, 0\n"
    devices = ds.parse_nvidia_smi(text)
    assert len(devices) == 2
    assert devices[0]["name"] == "NVIDIA GeForce RTX 4070"
    assert devices[0]["memory_total_mb"] == 12282
    assert devices[0]["temperature_c"] == 45
    assert devices[1]["utilization_pct"] == 0


def test_parse_nvidia_smi_ignores_noise():
    assert ds.parse_nvidia_smi("") == []
    assert ds.parse_nvidia_smi("No devices were found\n") == []
    assert ds.parse_nvidia_smi("gpu, N/A, N/A, N/A, N/A\n")[0]["memory_total_mb"] is None


def test_parse_rocm_smi_tolerates_field_drift():
    payload = {"card0": {"Card series": "Radeon RX 7900", "VRAM Total Memory (B)":
                         str(24 * 1024 ** 3), "VRAM Total Used Memory (B)": str(1024 ** 3),
                         "Temperature (Sensor edge) (C)": "51.0", "GPU use (%)": "7"}}
    devices = ds.parse_rocm_smi(payload)
    assert devices[0]["vendor"] == "amd"
    assert devices[0]["memory_total_mb"] == 24 * 1024
    assert devices[0]["utilization_pct"] == 7
    assert ds.parse_rocm_smi("not json") == []
    assert ds.parse_rocm_smi({"not-a-card": {}}) == []


def test_gpu_without_tools_says_no_gpu():
    result = ds.gpu_devices(which=lambda _name: None)
    assert result["present"] is False
    assert result["devices"] == []
    assert "本机无可用 GPU" in result["note"]


def test_gpu_prefers_nvidia_and_falls_back(monkeypatch):
    calls = []

    def run(argv):
        calls.append(argv[0])
        if argv[0] == "nvidia-smi":
            raise OSError("boom")
        return json.dumps({"card0": {"Card series": "MI210", "GPU use (%)": "3"}})

    result = ds.gpu_devices(run=run, which=lambda name: "/usr/bin/" + name)
    assert calls == ["nvidia-smi", "rocm-smi"]
    assert result["present"] is True and result["note"] == "rocm-smi"
    assert result["devices"][0]["name"] == "MI210"


# ─────────────────────────────────────────────────────────────── 日志 ring buffer
def _write_log(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                    encoding="utf-8")


def test_ring_log_tail_and_filter(tmp_path):
    path = tmp_path / "logs" / "djb.log"
    _write_log(path, [{"ts": "t1", "level": "info", "event": "a"},
                      {"ts": "t2", "level": "error", "event": "b"},
                      {"ts": "t3", "level": "info", "event": "c"}])
    ring = ds.RingLog(path)
    assert [row["event"] for row in ring.tail(limit=2)] == ["b", "c"]
    assert [row["event"] for row in ring.read(level="error")] == ["b"]
    assert len(ring.read(level="all")) == 3


def test_ring_log_marks_unparsable_lines(tmp_path):
    path = tmp_path / "djb.log"
    path.write_text('{"ts":"t1","level":"info","event":"ok"}\nnot-json\n', encoding="utf-8")
    rows = ds.RingLog(path).tail()
    assert rows[0]["event"] == "ok"
    assert rows[1]["level"] == "unknown" and rows[1]["event"] == "not-json"


def test_ring_log_missing_file_is_provider_error(tmp_path):
    with pytest.raises(ds.ProviderError) as info:
        ds.RingLog(tmp_path / "nope.log").tail()
    assert "尚未生成" in info.value.reason


def test_ring_log_respects_limit(tmp_path):
    path = tmp_path / "djb.log"
    _write_log(path, [{"ts": str(i), "level": "info", "event": "e" + str(i)} for i in range(50)])
    assert len(ds.RingLog(path, limit=10).tail(limit=100)) == 10


# ─────────────────────────────────────────────────────────────── 本机事实 / 跑分
def test_local_facts_shape():
    facts = ds.local_facts()
    for key in ("cpu_count", "mem_total_bytes", "disk_total_bytes", "platform", "python"):
        assert key in facts


def test_benchmark_respects_budget():
    result = ds.benchmark(budget_s=1.0)
    for key in ("hash_mbps", "float_mops", "int_mops", "elapsed_s"):
        assert key in result
    assert result["hash_mbps"] > 0
    assert result["elapsed_s"] < 5


# ─────────────────────────────────────────────────────────────── 真实现：降级
class FakeCloud:
    def __init__(self, **methods):
        self.calls = []
        for name, value in methods.items():
            setattr(self, name, self._wrap(name, value))

    def _wrap(self, name, value):
        def call(*args, **kwargs):
            self.calls.append(name)
            if isinstance(value, Exception):
                raise value
            return value(*args, **kwargs) if callable(value) else value
        return call


def make_source(tmp_path, **kwargs):
    defaults = dict(home=tmp_path,
                    runner=lambda argv: {"state": "online", "accepting": True},
                    which=lambda _name: None,
                    facts=lambda: {"cpu_count": 4, "mem_total_bytes": 1024,
                                   "mem_available_bytes": 512, "disk_total_bytes": 2048,
                                   "disk_free_bytes": 1024, "checked_at": "t"},
                    bench=lambda **_: {"hash_mbps": 1.0},
                    store_factory=lambda _path: (_ for _ in ()).throw(OSError("no db")))
    defaults.update(kwargs)
    return ds.LocalDataSource(**defaults)


def test_token_plan_lists_store_upstreams_when_status_has_none(tmp_path):
    """回归（任务 A）：daemon 没起时状态子进程只回 ``state``、不带 ``upstreams``，
    这时不许把本机库里已经接入的上游显示成「0 条上游 / 你还没有接入任何 Token Plan」。"""
    from djb_node.config import Paths
    from djb_node.secure import CredentialStore
    from djb_node.store import Store
    from djb_node.upstream import UpstreamManager

    paths = Paths(str(tmp_path))
    paths.prepare()
    store = Store.create(paths.db_file, version="0.1.0")
    record = UpstreamManager(store, CredentialStore(paths)).add(
        access_type="A1", base_url="https://api.example.invalid/v1",
        models="m1,m2", secret="sk-test")
    source = make_source(tmp_path, runner=lambda argv: {"state": "stopped"},
                         store_factory=Store)
    section = source.snapshot(sections=("token_plan",)).get("token_plan")
    assert [row["id"] for row in section["upstreams"]] == [record.id]
    assert section["upstreams"][0]["model_count"] == 2
    assert section["upstreams"][0]["verified_models"] == 0      # 没探过就是 0，不说已验证
    from djb_node.desktop.views import build_view

    text = build_view("token_plan", source.snapshot(sections=("token_plan",))).plain_text()
    assert "1 条上游" in text
    assert "0 条上游" not in text
    assert "你还没有接入任何 Token Plan" not in text
    assert "未启用" not in text                    # 限额读不到 ≠ 周/月没开


def test_status_ok(tmp_path):
    snapshot = make_source(tmp_path).snapshot(sections=("status",))
    assert snapshot.get("status")["state"] == "online"


def test_status_cli_failure_degrades(tmp_path):
    def runner(argv):
        raise ds.ProviderError("本地命令超时")

    snapshot = make_source(tmp_path, runner=runner).snapshot(sections=("status",))
    item = snapshot.get("status")
    assert isinstance(item, ds.Unavailable)
    assert "超时" in item.reason


def test_unexpected_exception_never_escapes(tmp_path):
    class Boom(Exception):
        pass

    def runner(argv):
        raise Boom("内部错误带路径 /home/secret")

    snapshot = make_source(tmp_path, runner=runner).snapshot()
    item = snapshot.get("status")
    assert isinstance(item, ds.Unavailable)
    assert "Boom" in item.reason
    assert "secret" not in item.reason            # 不把原始异常文本、路径、令牌带上屏


def test_status_without_state_field_is_incomplete(tmp_path):
    snapshot = make_source(tmp_path, runner=lambda argv: {"other": 1}).snapshot(sections=("status",))
    assert isinstance(snapshot.get("status"), ds.Unavailable)
    assert "状态回显不完整" in snapshot.get("status").reason


def test_cloud_sections_unavailable_without_client(tmp_path):
    """没有云端客户端时：纯云端域一律「暂不可用」，本地有真数据的域保留本地部分。"""
    snapshot = make_source(tmp_path).snapshot()
    for name in ("models", "tasks", "receipts", "earnings"):
        assert isinstance(snapshot.get(name), ds.Unavailable), name
    account = snapshot.get("account")
    assert account["profile"] is None                     # 资料没拿到，就没有资料
    assert "暂不可用" in account["note"]
    assert account["security"]["storage"] == "file-0600"   # 本机存储事实仍然给


def test_models_uses_cloud_catalog(tmp_path):
    cloud = FakeCloud(model_catalog={"items": [{"id": "m1", "context": 8192}]})
    snapshot = make_source(tmp_path, cloud=cloud).snapshot(sections=("models",))
    section = snapshot.get("models")
    assert section["online"] is True
    assert section["catalog"][0]["id"] == "m1"


def test_models_cloud_error_keeps_local(tmp_path):
    def runner(argv):
        if argv[0] == "model-sync":
            return {"applied": [{"model": "m-local", "state": "applied"}]}
        return {"state": "online"}

    cloud = FakeCloud(model_catalog=RuntimeError("boom"))
    snapshot = make_source(tmp_path, runner=runner, cloud=cloud).snapshot(sections=("models",))
    section = snapshot.get("models")
    assert section["online"] is False
    assert section["local"][0]["id"] == "m-local"
    assert "暂不可用" in section["note"]


def test_models_without_any_catalog_is_unavailable(tmp_path):
    snapshot = make_source(tmp_path, runner=lambda argv: {"applied": []}).snapshot(sections=("models",))
    assert isinstance(snapshot.get("models"), ds.Unavailable)


def test_tasks_falls_back_to_local_inflight(tmp_path):
    runner = lambda argv: {"state": "online", "inflight": [{"id": "t-1", "state": "running"}]}
    snapshot = make_source(tmp_path, runner=runner).snapshot(sections=("tasks",))
    section = snapshot.get("tasks")
    assert section["origin"] == "local"
    assert section["rows"][0]["id"] == "t-1"
    assert "暂不可用" in section["note"]


def test_receipts_without_cloud_and_without_store(tmp_path):
    snapshot = make_source(tmp_path).snapshot(sections=("receipts",))
    assert isinstance(snapshot.get("receipts"), ds.Unavailable)


def test_earnings_degrades_to_status_values(tmp_path):
    runner = lambda argv: {"state": "online", "balance": "9.9", "today_earnings": "1.1"}
    snapshot = make_source(tmp_path, runner=runner).snapshot(sections=("earnings",))
    section = snapshot.get("earnings")
    assert section["balance"] == "9.9" and section["today"] == "1.1"
    assert section["online"] is False
    assert "暂不可用" in section["note"]


def test_gpu_section_without_tools(tmp_path):
    snapshot = make_source(tmp_path).snapshot(sections=("gpu",))
    assert snapshot.get("gpu")["present"] is False
    assert "本机无可用 GPU" in snapshot.get("gpu")["note"]


def test_agent_and_access_come_from_local_file(tmp_path):
    from djb_node.desktop.access import AccessConfig, FileAccessStore

    config = AccessConfig(node_id="node-1", api_key="djb_sk_abcdefgh1234",
                          openai_base_url="http://127.0.0.1:1717/v1",
                          usdt_address="TQn9Y2khhvnJv8rKdT9jXb1sVJg8J3f7dQ")
    FileAccessStore(tmp_path / "desktop" / "access-config.json").save(config)
    snapshot = make_source(tmp_path).snapshot(sections=("agent", "access"))
    agent = snapshot.get("agent")
    assert agent["upstream"]["openai"] == "http://127.0.0.1:1717/v1"
    assert "abcdefgh1234" not in json.dumps(agent, ensure_ascii=False)
    access = snapshot.get("access")
    assert access["valid"] is True
    assert "abcdefgh1234" not in json.dumps(access, ensure_ascii=False)


def test_logs_section_reads_ring(tmp_path):
    _write_log(tmp_path / "logs" / "djb.log", [{"ts": "t", "level": "info", "event": "x"}])
    snapshot = make_source(tmp_path).snapshot(sections=("logs",))
    assert snapshot.get("logs")["rows"][0]["event"] == "x"


def test_logs_missing_file_degrades(tmp_path):
    snapshot = make_source(tmp_path).snapshot(sections=("logs",))
    assert isinstance(snapshot.get("logs"), ds.Unavailable)


def test_unknown_section_rejected(tmp_path):
    with pytest.raises(ValueError):
        make_source(tmp_path).snapshot(sections=("nope",))


def test_snapshot_is_always_sections_or_unavailable(tmp_path):
    snapshot = make_source(tmp_path).snapshot()
    for name in ds.SECTIONS:
        value = snapshot.get(name)
        assert isinstance(value, (dict, ds.Unavailable)), name
        if isinstance(value, dict):
            assert value, name                        # 空 dict 会被当成「有数据但全空」


# ───────────────────────────────────────────── 环境检测行：字段名与状态词都要真的映射上
def test_bench_checks_come_from_envcheck_with_real_fields(tmp_path):
    """真联调抓到：探针签名/字段名猜错，界面上三行全是「—」和「检测失败（TypeError）」。"""
    section = ds.LocalDataSource(home=tmp_path)._bench_section()
    checks = section.get("checks") or []
    assert len(checks) == 3
    assert [row["id"] for row in checks] == ["system", "hardware", "runtime"]
    for row in checks:
        assert row["level"] in ("ok", "warn", "error")
        assert row["summary"] and "：" in row["summary"]      # 标题 + 结论，不是空串
        assert row["status"] in ("ok", "warn", "fail", "na")
        assert "TypeError" not in row["summary"]


def test_bench_view_shows_probe_conclusions(tmp_path):
    from djb_node.desktop.views import build_view

    text = build_view("bench", ds.LocalDataSource(home=tmp_path).snapshot()).plain_text()
    assert "系统与架构" in text or "硬件" in text
    assert "检测失败" not in text


def test_cli_runner_passes_its_own_home_so_temp_windows_never_read_the_real_one(tmp_path):
    """本机命令必须带自己的 DJB_HOME：否则临时 home 的窗口会显示**别人家**节点的状态。"""
    import subprocess

    from djb_node.desktop import datasource as ds_mod

    seen = {}

    def fake_run(command, **kwargs):
        seen["env_home"] = kwargs.get("env", {}).get("DJB_HOME")
        return subprocess.CompletedProcess(command, 0, stdout='{"state": "stopped"}', stderr="")

    original = ds_mod.subprocess.run
    ds_mod.subprocess.run = fake_run
    try:
        source = ds_mod.LocalDataSource(home=tmp_path)
        assert source._cli_runner(["status"])["state"] == "stopped"
    finally:
        ds_mod.subprocess.run = original
    assert seen["env_home"] == str(tmp_path)


def test_cli_runner_surfaces_the_cli_sentence_not_just_an_exit_code(tmp_path):
    """失败时要带 CLI 的原话（「本机未初始化：先执行 djb init」），不是「退出码 3」。"""
    import subprocess

    import pytest

    from djb_node.desktop import datasource as ds_mod
    from djb_node.desktop.datasource import ProviderError

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 3, stdout="",
                                           stderr="djb: 本机未初始化：先执行 djb init\n")

    original = ds_mod.subprocess.run
    ds_mod.subprocess.run = fake_run
    try:
        with pytest.raises(ProviderError) as excinfo:
            ds_mod.LocalDataSource(home=tmp_path)._cli_runner(["status"])
    finally:
        ds_mod.subprocess.run = original
    assert "本机未初始化" in str(excinfo.value)


def test_cli_runner_refuses_when_home_is_not_the_configured_one(tmp_path, monkeypatch):
    """自检：子进程 home ≠ 数据源 home 时必须抛，绝不「照跑然后读别人的库」。"""
    from djb_node.desktop import datasource as ds_mod
    from djb_node.desktop.datasource import ProviderError

    source = ds_mod.LocalDataSource(home=tmp_path)
    monkeypatch.setattr(ds_mod.os, "environ", {"DJB_HOME": "/some/other/home"})
    # 就算环境里已有别的 DJB_HOME，也应以数据源 home 覆盖后运行；构造「覆盖失败」场景：
    monkeypatch.setattr(source, "_home", None)
    import pytest

    with pytest.raises(ProviderError) as excinfo:
        source._cli_runner(["status"])
    assert "home" in str(excinfo.value)


# ─────────────── CLI 子进程解码：中文 Windows 上整屏「暂不可用」的根因（0.2.3 回归）
def test_cli_runner_forces_utf8_decoding(tmp_path, monkeypatch):
    """父进程必须显式按 UTF-8 解子进程 stdout。

    子进程是我们自己的 CLI，``cli.main()`` 会 ``_enable_utf8_stdio()`` 强制 UTF-8；
    父进程若沿用本地代码页（中文 Windows = cp936）解码，UTF-8 中文三字节判非法 →
    ``UnicodeDecodeError`` → payload 为空 → ``status()`` 报「状态回显不完整」→
    token_plan / access / account 一起变「暂不可用」。这里锁死调用参数。
    """
    seen: dict = {}

    def fake_run(command, **kwargs):
        seen.update(kwargs)
        seen["command"] = list(command)
        return subprocess.CompletedProcess(command, 0,
                                           stdout='{"state": "online", "reason_label": "等待平台审核"}',
                                           stderr="")

    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    snapshot = make_source(tmp_path, runner=None).snapshot(sections=("status",))
    assert snapshot.get("status")["reason_label"] == "等待平台审核"
    assert seen.get("encoding") == "utf-8"
    assert seen.get("errors") == "replace"
    assert "text" not in seen          # text=True 会让 stdlib 按本地代码页解码


@pytest.mark.skipif(os.name == "nt", reason="需要一个 POSIX 可执行垫片脚本")
def test_cli_runner_decodes_utf8_child_output(tmp_path, monkeypatch):
    """真起一个子进程，输出真 UTF-8 中文 → 父进程必须解出中文（不是乱码、不是空）。"""
    script = _cli_stub(tmp_path, "child_utf8.py",
                       "sys.stdout.buffer.write('{\"state\": \"online\", "
                       "\"reason_label\": \"\\u7b49\\u5f85\\u5e73\\u53f0\\u5ba1\\u6838\"}'"
                       ".encode('utf-8'))")
    monkeypatch.setattr(ds.sys, "executable", str(script))
    payload = make_source(tmp_path, runner=None).status()
    assert payload["state"] == "online"
    assert payload["reason_label"] == "等待平台审核"


@pytest.mark.skipif(os.name == "nt", reason="需要一个 POSIX 可执行垫片脚本")
def test_cli_runner_tolerates_invalid_bytes(tmp_path, monkeypatch):
    """坏字节不许把整个视图打成不可用：errors="replace" 兜底成 U+FFFD。"""
    script = _cli_stub(tmp_path, "child_badbytes.py",
                       "sys.stdout.buffer.write(b'{\"state\": \"online\", \"note\": \"\\xff\\xfe\"}')")
    monkeypatch.setattr(ds.sys, "executable", str(script))
    payload = make_source(tmp_path, runner=None).status()
    assert payload["state"] == "online"
    assert "\ufffd" in payload["note"]


def _cli_stub(tmp_path, name, body):
    """造一个可执行垫片：``sys.executable -m djb_node ...`` 会被换成它。"""
    script = tmp_path / name
    script.write_text("#!/usr/bin/env python3\nimport sys\n" + body + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


# ──────────────────────────────────────────── 首启竞态：云端客户端必须能"后装配"
def _late_cloud_source(tmp_path, factory):
    """构造一个只取 account 域的数据源（不跑 CLI / GPU / 环境检测）。"""
    return ds.LocalDataSource(home=tmp_path, cloud_factory=factory,
                              runner=lambda argv: {"state": "stopped"},
                              facts=lambda: {}, which=lambda name: None,
                              bench=lambda **kw: {})


def test_cloud_is_reassembled_after_daemon_registration(tmp_path):
    """正式环境实测（0.3.0）：首帧 daemon 还没注册完 → cloud=None；注册落盘后点「刷新」
    必须自动接上，**不能只在重启窗口后才恢复**。"""
    from djb_node.config import Paths
    from djb_node.secure import CredentialStore

    calls = []

    class FakeCloud:
        def profile(self):
            return {"node_id": "n_late", "state": "pending"}

    def factory(home):
        calls.append(home)
        return None if len(calls) == 1 else FakeCloud()

    source = _late_cloud_source(tmp_path, factory)
    first = source.snapshot(sections=("account",)).sections["account"]
    assert "未登录节点" in first["note"] and "本机尚未注册节点" in first["note"]

    # daemon 注册完成：node_id + client_key 落盘（这一步真实发生在首启后几秒内）
    CredentialStore(Paths(str(tmp_path))).set_node("n_late", "djb_ck_late_0001")

    second = source.snapshot(sections=("account",)).sections["account"]
    assert not isinstance(second, ds.Unavailable)
    assert second["profile"]["node_id"] == "n_late"
    assert second["note"] == ""
    assert len(calls) == 2                      # 每轮刷新重新装配一次，不无限重试


def test_no_cloud_reason_distinguishes_registered_but_unbound(tmp_path):
    """「未登录节点」要分两种：本机还没注册 / 注册了但没绑定账号。"""
    from djb_node.config import Paths
    from djb_node.secure import CredentialStore

    CredentialStore(Paths(str(tmp_path))).set_node("n_registered", "djb_ck_reg_0001")
    source = _late_cloud_source(tmp_path, lambda home: None)
    account = source.snapshot(sections=("account",)).sections["account"]
    assert "未登录节点" in account["note"]
    assert "已注册但未绑定账号" in account["note"]


# ─────────────────────────── 接入配置「节点标识」三级回退（P0-node，2026-11-12）
# 背景：注册只写凭据（secure.CredentialStore.set_node）+ 状态库（daemon 的 set_kv("node")），
# 不写 access.json；接入配置页若只读 AccessConfig.node_id，注册成功的机器也永远显示「—」，
# 客户就拿不到网页端绑定要填的节点 ID。
def test_access_section_reads_node_id_from_credentials_when_config_empty(tmp_path):
    """凭证有 node_id 而 access.json 没有 → 用凭证里的值（且投影不变）。"""
    from djb_node.config import Paths
    from djb_node.secure import CredentialStore

    paths = Paths(str(tmp_path))
    paths.prepare()
    CredentialStore(paths).set_node("nd_from_credentials", "djb_ck_test_0001")

    section = make_source(tmp_path)._access_section()      # 默认 store_factory 打不开 → 只能走凭证

    assert section["node_id"] == "nd_from_credentials"
    assert section["node_id_source"] == "credentials"
    assert section["config"]["node_id"] == "—"             # 旧投影保持原样（没填就是「—」）


def test_access_section_falls_back_to_store_kv_node(tmp_path):
    """凭证也没有时，第三级用状态库 ``kv("node")``（daemon 注册后写入）。"""
    from djb_node.config import Paths
    from djb_node.store import Store

    paths = Paths(str(tmp_path))
    paths.prepare()
    store = Store.create(paths.db_file, version="0.1.0")
    store.set_kv("node", {"node_id": "nd_from_store", "state": "active",
                          "registered_at": "2026-11-12T00:00:00+0800"})

    section = make_source(tmp_path, store_factory=Store)._access_section()

    assert section["node_id"] == "nd_from_store"
    assert section["node_id_source"] == "store"


def test_access_section_without_any_node_id_stays_empty(tmp_path):
    """三者都没有 → 空字符串（视图据此显示「先启动一次」，不编占位 ID）。"""
    section = make_source(tmp_path)._access_section()

    assert section["node_id"] == ""
    assert section["node_id_source"] == ""
    assert section["config"]["node_id"] == "—"
