# SPDX-License-Identifier: Apache-2.0
"""P4 / P6 / E2E-06：`djb status` 必须把「通道 / 最近探针 / 掉容量原因 / 暂停细节」说出来。

口径是**不得谎报**：拿不到证据就说「从未探测 / 已断开 / 未开启」，绝不编一句「正常」。
这几类信息正是 E2E-04 / E2E-06 / E2E-10 / E2E-12 的对客缺口 —— 修前它们只在
（或者根本不在）日志里，用户看到的是「接单=是」而实际一单不接。
"""
from __future__ import annotations

import json
import os
from datetime import timedelta

from djb_node import cli as cli_module
from djb_node import facts
from djb_node.util import iso, utcnow
from djb_node.state import Snapshot
from djb_node.store import Store
from djb_node.desktop import datasource as ds


# ───────────────────────────────────────────── 通道三态（P4 落点 C）
def test_channel_line_says_connected_only_when_polling():
    text = cli_module._channel_text({"wanted": True, "running": True, "state": "polling",
                                     "wait_s": 25, "jobs_done": 2})
    assert text.startswith("已连接")
    assert "已接单 2" in text
    executing = cli_module._channel_text({"wanted": True, "running": True,
                                          "state": "executing"})
    assert executing.startswith("已连接")


def test_channel_line_says_off_with_actionable_flag():
    text = cli_module._channel_text({"wanted": False, "running": False, "state": "idle"})
    assert text.startswith("未开启")
    assert facts.CHANNEL_FLAG in text            # 必须给出可行动命令，不是一句「关闭」


def test_channel_line_never_claims_connected_on_inconsistent_state():
    """线程在跑但状态机已经 stopping/退出中：只能报**已断开**。"""
    for state in ("idle", "stopping"):
        text = cli_module._channel_text({"wanted": True, "running": True, "state": state})
        assert text.startswith("已断开"), state
    down = cli_module._channel_text({"wanted": True, "running": False, "state": "idle",
                                     "last_error": "NetworkError"})
    assert down.startswith("已断开") and "NetworkError" in down


def test_channel_line_distinguishes_offline_and_server_required():
    offline = cli_module._channel_text({"wanted": True, "running": False, "state": "offline"})
    assert offline == "已断开（daemon 未运行）"
    required = cli_module._channel_text({"wanted": True, "running": True, "state": "polling",
                                         "required": True})
    assert "服务端要求通道" in required


# ───────────────────────────────────────────── 最近探针 / 掉容量（P6）
def test_probe_line_says_never_probed_without_faking_a_time():
    assert "从未探测" in cli_module._probe_status_line([])
    assert "从未探测" in cli_module._probe_status_line([{"model": "m1"}])


def test_probe_line_shows_remaining_freshness():
    now = utcnow()
    line = cli_module._probe_status_line(
        [{"model": "m1", "inference_ok": True,
          "last_probe_at": (now - timedelta(seconds=300)).isoformat()}],
        now=now, fresh_s=facts.PROBE_FRESH_S)
    assert line.startswith("最近探针：ok @ ")
    assert "后掉容量" in line and "已掉容量" not in line


def test_probe_line_reports_failure_class_and_lost_capacity():
    now = utcnow()
    line = cli_module._probe_status_line(
        [{"model": "m1", "inference_ok": False, "probe_error_class": "timeout",
          "last_probe_at": (now - timedelta(seconds=facts.PROBE_FRESH_S + 600)).isoformat()}],
        now=now, fresh_s=facts.PROBE_FRESH_S)
    assert "timeout" in line                      # E2E-11 的那一类失败原因要看得到
    assert "已掉容量" in line


def test_capacity_lines_explain_why_each_model_dropped():
    value = {"probe": {"periodic_enabled": True, "interval_s": facts.PROBE_PERIODIC_CAP_S,
                       "models": [
                           {"model": "ok-model", "enabled": True, "inference_ok": True,
                            "announceable": True},
                           {"model": "slow", "enabled": True, "inference_ok": False,
                            "probe_error_class": "timeout", "announceable": False},
                           {"model": "stale", "enabled": True, "inference_ok": True,
                            "probe_age_s": facts.PROBE_FRESH_S + 60,
                            "announceable": False},
                           {"model": "off", "enabled": False, "inference_ok": True,
                            "announceable": False},
                       ]}}
    text = "\n".join(cli_module._capacity_lines(value))
    assert "1/4 个模型可广播" in text
    assert "slow —— 探针未通过（timeout）" in text
    assert "stale —— 探针过期" in text
    assert "off —— 上游已停用" in text
    # D-R3-02：450 s 必须说成「7.5 分钟」而不是 round() 出来的「8 分钟」（越过自己的上限）
    assert "周期复探：每 7.5 分钟" in text
    assert "每 8 分钟" not in text


def test_capacity_lines_periodic_display_never_exceeds_cap():
    """D-R3-02 不变量：**显示出来的周期 ≤ 上限**（任何间隔都不许被说大）。

    修前 `_minutes()` 用 round()：450 s → 「8 分钟」，而同一行还写着「上限=新鲜度一半」
    （7.5 分钟）—— 用户看到的是「8 ≤ 7.5」，凭空一个矛盾。
    """
    for interval in (60, 90, 120, 450, 600, 1200, 3600, 21600):
        cap = facts.PROBE_PERIODIC_CAP_S if interval <= facts.PROBE_PERIODIC_CAP_S \
            else facts.PROBE_PERIODIC_MAX_S
        text = "\n".join(cli_module._capacity_lines({
            "probe": {"periodic_enabled": True, "interval_s": interval, "cap_s": cap}}))
        shown = text.split("每 ", 1)[1].split("（", 1)[0]
        if shown.endswith("秒"):
            shown_s = float(shown[:-1])
        else:
            shown_s = float(shown.replace(" 分钟", "")) * 60
        assert shown_s <= interval + 0.01, (interval, shown)
        assert shown_s <= cap + 0.01, (interval, cap, shown)


def test_capacity_lines_flag_disabled_periodic_probe_as_real_cause():
    value = {"probe": {"periodic_enabled": False, "models": [
        {"model": "m1", "enabled": True, "inference_ok": None, "announceable": False}]}}
    text = "\n".join(cli_module._capacity_lines(value))
    assert "从未探测" in text
    assert "周期复探：关" in text
    assert facts.PROBE_FLAG_PERIODIC in text


def test_upstream_line_carries_recent_probe():
    line = cli_module._upstream_line({
        "id": "u1", "access_type": "A2", "base_url": "http://127.0.0.1:9",
        "state": "verified",
        "models": [{"id": "qwen", "inference_ok": True,
                    "last_probe_at": utcnow().isoformat()}]})
    assert "最近探针：ok @" in line


def test_status_text_renders_channel_probe_and_capacity():
    now = utcnow()
    value = {
        "state": "paused", "reason": "yield_paused", "reason_label": "让路（用户在用）",
        "accepting": False, "version": facts.CLIENT_VERSION, "protocol_version": "1.8.1",
        "daemon_running": True,
        "channel": {"wanted": True, "running": True, "state": "polling", "wait_s": 25},
        "pause_detail": {"source": "load", "signals": [
            {"source": "load", "state": "in_use", "detail": "GPU 97%"}]},
        "probe": {"periodic_enabled": True, "interval_s": facts.PROBE_PERIODIC_CAP_S,
                  "fresh_window_s": facts.PROBE_FRESH_S,
                  "models": [{"model": "qwen", "enabled": True, "inference_ok": True,
                              "announceable": True,
                              "last_probe_at": now.isoformat()}]},
        "receipts": {"count": 0, "oldest_age_s": 0},
        "credentials": {"client_key_set": True, "storage": "0600"},
        "resume_note": "已恢复接单；同时关闭了「自动让路」",
    }
    text = cli_module._status_text(value)
    assert "通道：已连接" in text
    assert "最近探针：ok @" in text
    assert "容量：1/1 个模型可广播" in text
    assert "暂停细节：让路（load）；load：GPU 97%" in text
    assert "提示：已恢复接单" in text


def test_offline_status_keeps_periodic_line_with_planned_rhythm(tmp_path):
    """daemon 未运行时也要打印「周期复探：每 …」（真机取证就是停止态）。

    修前 `_offline_status` 给的是 ``interval_s=None`` ⇒ 整行消失，读者会以为周期复探没开；
    而 Windows 0.2.4 的取证正是停止态采的（win-probe.json）。
    """
    from djb_node.config import Paths, save_config
    from djb_node.store import Store
    paths = Paths(str(tmp_path / "home"))
    paths.prepare()
    save_config(paths, cli_module.ClientConfig())
    store = Store.create(paths.db_file, version=facts.CLIENT_VERSION)
    value = cli_module._offline_status(paths, store)
    assert value["probe"]["periodic_enabled"] is True          # 干净 home：默认开
    assert value["probe"]["interval_s"] == facts.PROBE_PERIODIC_DEFAULT_S
    assert value["probe"]["planned"] is True
    text = "\n".join(cli_module._capacity_lines(value))
    assert "周期复探：每 7.5 分钟" in text
    assert "daemon 未运行，启动后生效" in text
    # 空闲状态下没有模型 → 不该凭空编出容量行
    assert "容量：" not in text
    store.close() if hasattr(store, "close") else None


def test_install_line_flags_home_record_when_it_lags_running_version():
    """`安装：` 打的是**本机 home 安装记录**，不是运行版本（ROUND3 结论）。

    覆盖安装不会更新这条记录（只有 djb init / 自升级 apply·rollback 会写），于是 0.2.5 的
    二进制会显示「安装：0.2.2」。两个数字并列而不解释，就是又一次「谎报」——
    不一致时加事实括注；一致时输出**与改前逐字相同**（不制造噪音，老断言不受影响）。
    """
    base = {
        "state": "online", "accepting": True, "version": "0.2.5",
        "protocol_version": "1.8.2", "daemon_running": True,
        "install": {"version": "0.2.2", "channel": "stable", "bucket": 3},
        "receipts": {"count": 0, "oldest_age_s": 0},
        "credentials": {"client_key_set": True, "storage": "0600"},
    }
    text = cli_module._status_text(base)
    assert "安装：0.2.2 · 频道 stable · 桶 3（本机 home 记录，运行版本 0.2.5）" in text

    same = dict(base)
    same["install"] = {"version": "0.2.5", "channel": "stable", "bucket": 3}
    text = cli_module._status_text(same)
    assert "安装：0.2.5 · 频道 stable · 桶 3" in text
    assert "本机 home 记录" not in text


# ───────────────────────────────────────────── 暂停细节（E2E-06）
def test_pause_detail_text_renders_resource_limit():
    assert cli_module._pause_detail_text({}) == ""
    text = cli_module._pause_detail_text({"over": {"cpu": {"value": 99.0, "cap": 60}}})
    assert text == "资源超上限：cpu 99.0 > 60"


def test_pause_detail_text_renders_only_triggering_yield_signals():
    text = cli_module._pause_detail_text({
        "source": "thermal", "signals": [
            {"source": "thermal", "state": "in_use", "detail": "温度 88℃"},
            {"source": "battery", "state": "ok", "detail": ""}]})
    assert "让路（thermal）" in text and "thermal：温度 88℃" in text
    assert "battery" not in text                   # 只解释**触发**的那一条

# ─────────────────────────────────── 顶层 edition（W6 演练 R-3）
def test_status_json_carries_edition_without_changing_old_fields(home, capsys):
    """`djb status --json` 顶层要有 `edition`（只加不改：旧字段一个不少）。

    W6 演练要按 `status --json` 认档位；没有这个字段时只能去翻 `config.json`。
    """
    import json
    from djb_node.cli import main
    assert main(["init", "--no-env", "--edition", "official-supply"]) == 0
    capsys.readouterr()
    assert main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["edition"] == "official-supply"
    # 旧字段必须还在（daemon 未运行也要如实回答）
    for key in ("state", "daemon_running", "version", "channel", "probe", "install"):
        assert key in payload, "旧字段丢了：" + key
    assert payload["daemon_running"] is False


def test_offline_status_keeps_last_error_state_and_next_step(tmp_path):
    """2026-11-24 生产 E2E（Windows / 0.5.11）：daemon 撞 `register_conflict` 死掉后，
    `djb status` 只剩「状态：stopped（daemon 未运行）/ 下一步：djb start 启动 daemon」，
    daemon 早就写好的处置指引（解绑 / 复用密钥）整段丢失，用户只会原地重试再撞一次。

    口径：离线态保留最后一次**非 stopped** 的结论与下一步（只读快照，不猜不补）。
    """
    from djb_node.config import Paths, save_config
    from djb_node.state import Snapshot, StateMachine
    from djb_node.store import Store
    from djb_node.util import utcnow

    paths = Paths(str(tmp_path / "home"))
    paths.prepare()
    save_config(paths, cli_module.ClientConfig())
    store = Store.create(paths.db_file, version=facts.CLIENT_VERSION)
    machine = StateMachine(Snapshot(state="starting", since=utcnow()))
    machine.fail(next_step="先登录网页端解绑这台机器，再重启客户端重新注册",
                 detail={"reason": "register_conflict"})
    store.set_kv("daemon_state", machine.snapshot.to_dict())
    value = cli_module._offline_status(paths, store)
    assert value["state"] == "error"
    assert value["reason_label"] and "daemon 未运行" != value["reason_label"]
    assert "解绑" in value["next_step"]
    store.close() if hasattr(store, "close") else None


# ─────────────── 2026-11-24 真机：控制口不可达 ≠ daemon 未运行（F35）

def test_offline_status_reports_running_daemon_from_pidfile(tmp_path, monkeypatch):
    """跨会话执行 `djb status` 时管道名对不上（会话号/完整性级别进了哈希）。

    旧实现把这种「连不上」写成「daemon 未运行」，运维据此判节点掉线 = 假阴性。
    现在 pid 文件里有可验证的 daemon 就如实说「在跑，只是本终端连不上」。
    """
    from djb_node import cli, pidfile
    from djb_node.config import Paths
    from djb_node.state import Snapshot, utcnow, iso
    from djb_node.store import Store

    paths = Paths(str(tmp_path))
    os.makedirs(paths.runtime_dir, exist_ok=True)
    Store.create(paths.db_file)
    store = Store(paths.db_file)
    store.set_kv("daemon_state", Snapshot(state="online", since=iso(utcnow())).to_dict())

    record = {"schema": pidfile.SCHEMA, "pid": os.getpid(),
              "start_token": pidfile.probe.start_token(os.getpid()),
              "started_at": "2026-11-24T04:10:44+0800",
              "home": str(paths.home), "marker": pidfile.probe.DAEMON_MARKER}
    with open(paths.pid_file, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    # 本测试只验「pid 文件可信时怎么报」，进程身份校验本身由 test_pidfile.py 覆盖：
    # 拿 pytest 进程去跑真实校验必然判 NOT_DAEMON。
    monkeypatch.setattr(pidfile, "verify", lambda rec, p: (True, ""))

    value = cli._offline_status(paths, store)
    assert value["daemon_running"] is True, value
    assert "控制口不可达" in value["control_unreachable"]
    assert value["channel"]["state"] == "unreachable"
    assert value["channel"]["running"] is True          # snapshot 说 online 就一定在接单
    text = cli._status_text(value)
    assert "daemon：运行中（本终端控制口不可达）" in text
    assert "通道：本终端连不上控制口" in text
    assert "接单：是" in text, "快照 online ⇒ 接单必须是「是」，不许因为连不上管道就说否"
    assert "控制口不可达，周期以 daemon 日志为准" in text


def test_offline_status_stays_offline_without_pidfile(tmp_path):
    """没有 daemon（也没有 pid 文件）时仍然是「未运行」，不许无脑乐观。"""
    from djb_node import cli
    from djb_node.config import Paths
    from djb_node.store import Store

    paths = Paths(str(tmp_path))
    os.makedirs(paths.runtime_dir, exist_ok=True)
    Store.create(paths.db_file)
    value = cli._offline_status(paths, Store(paths.db_file))
    assert value["daemon_running"] is False
    assert value["control_unreachable"] == ""
    assert value["channel"]["state"] == "offline"


def test_offline_status_does_not_fabricate_accepting_false(tmp_path, monkeypatch):
    """F50（2026-11-24 真机）：daemon 刚起、pidfile 还没写出来时，旧实现把 accepting 压成 False，
    而 state 仍是快照里的 online —— 同一份 payload 自相矛盾。现在读不到就给 None。"""
    from djb_node.config import Paths
    from djb_node.desktop import views as vw

    paths = Paths(str(tmp_path))
    store = Store.create(paths.db_file)
    snapshot = Snapshot(state="online", since=iso(utcnow()), next_step="")
    store.set_kv("daemon_state", snapshot.to_dict())
    payload = cli_module._offline_status(paths, store)
    assert payload["state"] == "online"
    assert payload["accepting"] is None
    text = vw.build_view("overview", ds.Snapshot({"node": payload, "yield": payload})).plain_text()
    assert "不接新任务" not in text
