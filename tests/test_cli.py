# SPDX-License-Identifier: Apache-2.0
import json
import os
import zipfile

import pytest

from djb_node import __version__, cli, facts
from djb_node.cli import build_parser, main


COMMANDS = ["init", "bootstrap", "env", "auth", "start", "stop", "status", "pause", "resume",
            "limits", "upstream", "token", "flags", "attest", "announce", "quotes",
            "update", "logs", "diagnose", "tray", "uninstall", "model-sync", "notify",
            "proxy", "gui", "plan", "daemon", "reload", "machine"]


def test_every_command_has_help(capsys):
    for command in COMMANDS:
        with pytest.raises(SystemExit) as info:
            main([command, "--help"])
        assert info.value.code == 0
        assert command in capsys.readouterr().out


# ───────────────────────────── Windows 控制台中文乱码（任务 B3）
class _FakeStream:
    """只记录 reconfigure 调用的替身流：不需要真控制台。"""

    def __init__(self, *, explode: bool = False) -> None:
        self.calls: list[dict] = []
        self._explode = explode

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)
        if self._explode:
            raise OSError("no console")
        return None


def test_stdio_is_reconfigured_to_utf8(monkeypatch):
    """CLI 输出 UTF-8、控制台按 GBK 解码 → 「鑱旇剳」；这里把输出编码对齐 UTF-8。"""
    out, err = _FakeStream(), _FakeStream()
    monkeypatch.setattr(cli.sys, "stdout", out, raising=False)
    monkeypatch.setattr(cli.sys, "stderr", err, raising=False)
    cli._enable_utf8_stdio()
    assert out.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert err.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_stdio_reconfigure_never_raises(monkeypatch):
    """reconfigure 抛错 / 流没有 reconfigure（StringIO、被替换的捕获流）都必须静默。"""
    import io

    monkeypatch.setattr(cli.sys, "stdout", _FakeStream(explode=True), raising=False)
    monkeypatch.setattr(cli.sys, "stderr", _FakeStream(explode=True), raising=False)
    cli._enable_utf8_stdio()                       # 不抛
    monkeypatch.setattr(cli.sys, "stdout", io.StringIO(), raising=False)
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO(), raising=False)
    cli._enable_utf8_stdio()                       # 没有 reconfigure 也不抛
    # 真跑一次 main（流不支持 reconfigure）也不能因为编码问题炸掉
    stream = io.StringIO()
    monkeypatch.setattr(cli.sys, "stdout", stream, raising=False)
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO(), raising=False)
    with pytest.raises(SystemExit) as info:        # --version 走 argparse 的 exit(0)
        main(["--version"])
    assert info.value.code == 0
    assert __version__ in stream.getvalue()


def test_stdio_fix_never_touches_console_codepage():
    """不许用 ``chcp``：改代码页会连带影响调用方同一窗口里的其它程序。"""
    import inspect

    # 只看可执行部分（docstring 里正是解释「为什么不用 chcp」，不能算进来源）
    body = inspect.getsource(cli._enable_utf8_stdio).split('"""')[-1].lower()
    assert "chcp" not in body
    assert "subprocess" not in body and "os.system" not in body


def test_parser_lists_all_commands():
    parser = build_parser()
    actions = [action for action in parser._actions if action.dest == "command"]
    assert actions
    assert set(actions[0].choices) == set(COMMANDS)


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_unknown_command_exits_2():
    with pytest.raises(SystemExit) as info:
        main(["nope"])
    assert info.value.code == 2


def test_status_without_init_exits_3(home, capsys):
    assert main(["status", "--json"]) == 3
    assert "djb init" in capsys.readouterr().err


def test_init_and_status_roundtrip(home, capsys):
    assert main(["init", "--no-env"]) == 0
    capsys.readouterr()
    assert main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["state"] == "stopped"
    assert payload["daemon_running"] is False
    assert payload["install"]["version"]
    assert "credentials" in payload


def test_limits_validation_and_update(home, capsys):
    main(["init", "--no-env"])
    capsys.readouterr()
    assert main(["limits", "--concurrent", "0"]) == 2
    assert main(["limits", "--daily-tokens", "0"]) == 2
    assert main(["limits", "--cpu", "1"]) == 2
    assert main(["limits", "--concurrent", "4", "--daily-tokens", "10000",
                 "--schedule", "09:00-23:00", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["profile"]["limits"]["concurrent"] == 4
    assert payload["profile"]["schedule"][0]["start"] == "09:00"
    assert main(["limits", "--clear-schedule", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["profile"]["schedule"] == []


def test_auth_status_and_missing_key(home, capsys):
    main(["init", "--no-env"])
    capsys.readouterr()
    assert main(["auth", "--status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["bound"] is False
    assert payload["credentials"]["storage"] == "file-0600"
    # 未绑定时文本输出要给出「网页端 → 账户/节点 → 生成绑定码」的可照抄下一步
    assert main(["auth", "--status"]) == 0
    text = capsys.readouterr().out
    assert "绑定：网页端 → 账户/节点 → 生成绑定码，然后 djb auth --bind <码>" in text
    assert facts.WEB_CONSOLE_URL in text
    assert main(["upstream", "add", "--access", "A2",
                 "--base-url", "http://127.0.0.1:11434/v1",
                 "--models", "m1"]) == 0
    capsys.readouterr()
    assert main(["announce"]) == 5


def test_upstream_lifecycle_without_server(home, capsys):
    main(["init", "--no-env"])
    capsys.readouterr()
    assert main(["upstream", "add", "--access", "A2",
                 "--base-url", "http://127.0.0.1:11434/v1",
                 "--models", "m1,m2", "--secret", "s3cret-value", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    upstream_id = payload["upstream"]["id"]
    assert payload["credential_stored"] is True
    assert payload["declare"]["declared"] is False
    assert main(["upstream", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out.strip())
    assert listed["count"] == 1
    assert main(["upstream", "price", "--id", upstream_id, "--model", "m1",
                 "--min-price", "0.25", "--json"]) == 0
    capsys.readouterr()
    assert main(["upstream", "disable", "--id", upstream_id, "--model", "m1",
                 "--json"]) == 0
    capsys.readouterr()
    assert main(["upstream", "a2-prompt", "--id", upstream_id, "--json"]) == 0
    prompt = json.loads(capsys.readouterr().out.strip())
    assert set(prompt["sections"]) == {"目标", "约束", "输入", "输出", "验收"}
    assert "s3cret-value" not in json.dumps(prompt)
    assert main(["upstream", "remove", "--id", upstream_id]) == 2
    assert main(["upstream", "remove", "--id", upstream_id, "--yes"]) == 0


def test_env_and_logs_and_stop_pause_resume(home, capsys):
    main(["init", "--no-env"])
    capsys.readouterr()
    assert main(["env", "--no-persist", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert len(payload["checks"]) == 6
    assert all(check["next_step"] for check in payload["checks"])
    assert main(["env", "--authorize", "--json"]) == 0
    capsys.readouterr()
    assert main(["env", "--rollback", "--json"]) == 0
    capsys.readouterr()
    assert main(["logs", "--tail", "5"]) == 0
    capsys.readouterr()
    assert main(["stop"]) == 0
    assert main(["pause"]) == 0
    assert main(["resume"]) == 0
    capsys.readouterr()


def test_update_check_and_status(home, capsys):
    main(["init", "--no-env"])
    capsys.readouterr()
    assert main(["update", "--check", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["state"] == "no_source"
    assert main(["update", "--status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out.strip())
    assert "current_version" in status
    assert main(["update", "--rollback"]) == 8


def test_diagnose_creates_redacted_bundle(home, capsys):
    main(["init", "--no-env"])
    capsys.readouterr()
    # 写入一条含疑似凭据的日志，确认诊断包仍然干净
    from djb_node.config import Paths
    from djb_node.util import ensure_dir
    paths = Paths(str(home))
    ensure_dir(paths.logs_dir)
    fake_key = "ck_" + "supersecret_0123456789"
    fake_sk = "sk-" + "abcdefghijklmnopqrstuvwxyz"
    with open(paths.log_file, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "x", "client_key": fake_key}) + "\n")
        handle.write(json.dumps({"event": "y", "body": fake_sk}) + "\n")
    assert main(["diagnose", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["bytes"] > 0
    assert payload["redaction"]["hits"] == 0
    assert os.path.exists(payload["path"])
    assert oct(os.stat(payload["path"]).st_mode)[-3:] == "600"
    with zipfile.ZipFile(payload["path"]) as archive:
        text = ""
        for name in archive.namelist():
            text += archive.read(name).decode("utf-8", errors="ignore")
    assert fake_key not in text
    assert fake_sk not in text
    assert "REDACTED" in text


def test_uninstall_dry_run_and_real(home, capsys):
    main(["init", "--no-env"])
    capsys.readouterr()
    assert main(["uninstall"]) == 2
    assert main(["uninstall", "--dry-run", "--json"]) == 0
    preview = json.loads(capsys.readouterr().out.strip())
    assert preview["dry_run"] is True
    assert preview["removed"]
    assert main(["uninstall", "--yes", "--json"]) == 0
    result = json.loads(capsys.readouterr().out.strip())
    assert result["residual_count"] == 0
    assert not os.path.exists(os.path.join(str(home), "state.db"))
    assert main(["status", "--json"]) == 3
