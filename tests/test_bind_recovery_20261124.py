# SPDX-License-Identifier: Apache-2.0
"""R8 F1（2026-11-24）：解绑撤销密钥后，``auth --bind`` / ``--pair`` 自动恢复再重试。

验收线（lead 给定）：

1. 401 ``client_key_revoked`` 不直接失败：用本机硬件指纹重新注册（不带旧密钥），
   成功后才重试**原操作一次**；
2. 注册返回的 node_id 与本地不一致 → 报「节点身份变化，请联系支持」，且不改本机凭据；
3. 恢复失败 / 重试失败都只给一句「下一步」，不出现 client_key / register / poll；
4. 不循环：一次恢复 + 一次重试是上限。
"""
from __future__ import annotations

import json

from djb_node import facts
from djb_node.cli import main
from djb_node.config import Paths
from djb_node.secure import CredentialStore
from tests.mock_m02 import CLIENT_KEY, NODE_ID

RECOVERED_KEY = "ck_recovered_key_0123456789abcdef"


def _prepare(home, mock_server, *, key: str = CLIENT_KEY, node: str = NODE_ID) -> Paths:
    assert main(["init", "--no-env", "--gateway", mock_server.base_url]) == 0
    paths = Paths(str(home))
    CredentialStore(paths).set_node(node, key)
    return paths


def _posts(mock_server, path: str) -> list[dict]:
    return [item for item in mock_server.requests
            if item["method"] == "POST" and item["path"] == path]


def _assert_customer_copy(text: str) -> None:
    lowered = text.lower()
    for internal in ("client_key", "register", "poll"):
        assert internal not in lowered, (internal, text)
    assert "下一步" in text


# ─────────────────────────────────────────────── bind：撤销 → 恢复 → 重试一次
def test_bind_recovers_revoked_key_then_retries_once(home, mock_server, capsys):
    paths = _prepare(home, mock_server)
    mock_server.binding_revoked_count = 1
    mock_server.register_client_key = RECOVERED_KEY
    capsys.readouterr()

    assert main(["auth", "--bind", "code-r8", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["recovered"] is True and payload["bound"] is True
    assert payload["node_id"] == NODE_ID

    binds = _posts(mock_server, "/api/v1/node/binding")
    registers = _posts(mock_server, "/api/v1/node/register")
    assert len(binds) == 2, "先 401 一次，恢复后必须重试原绑定一次"
    assert len(registers) == 1, "只恢复一次，不许循环"
    # 恢复请求必须用硬件指纹、且**不带已撤销的旧密钥**（带了只会再 401）
    register = registers[0]
    assert register["body"]["hardware_fingerprint"]
    headers = {key.lower(): value for key, value in register["headers"].items()}
    assert "x-client-key" not in headers
    # 密钥确实换成了云端轮换后的新钥
    assert CredentialStore(paths).client_key() == RECOVERED_KEY


def test_bind_recovery_human_output_says_recovered(home, mock_server, capsys):
    _prepare(home, mock_server)
    mock_server.binding_revoked_count = 1
    capsys.readouterr()
    assert main(["auth", "--bind", "code-r8"]) == 0
    out = capsys.readouterr().out
    assert "已自动恢复节点密钥并完成绑定" in out


def test_pair_recovers_revoked_key_then_retries_once(home, mock_server, capsys):
    _prepare(home, mock_server)
    mock_server.pair_revoked_count = 1
    capsys.readouterr()

    assert main(["auth", "--pair", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["recovered"] is True and payload["rotated"] is True
    assert len(_posts(mock_server, "/api/v1/node/pair")) == 2
    assert len(_posts(mock_server, "/api/v1/node/register")) == 1


def test_explicit_recover_command_swaps_key_and_keeps_identity(home, mock_server, capsys):
    paths = _prepare(home, mock_server)
    mock_server.register_client_key = RECOVERED_KEY
    capsys.readouterr()
    assert main(["auth", "--recover", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["recovered"] is True and payload["node_id"] == NODE_ID
    assert CredentialStore(paths).node_id() == NODE_ID
    assert CredentialStore(paths).client_key() == RECOVERED_KEY


# ─────────────────────────────────────────────── 身份变化：拒绝改写本机身份
def test_node_id_mismatch_refuses_to_rebind_and_tells_owner_to_contact_support(
        home, mock_server, capsys):
    paths = _prepare(home, mock_server)
    mock_server.binding_revoked_count = 1
    mock_server.register_node_id = "n_other_machine"
    capsys.readouterr()

    assert main(["auth", "--bind", "code-r8", "--json"]) == facts.EXIT_AUTH
    err = capsys.readouterr().err
    assert "节点身份变化，请联系支持" in err
    _assert_customer_copy(err)
    # 本机凭据一个字段都不许被改：旧 node_id 与旧密钥原样保留
    assert CredentialStore(paths).node_id() == NODE_ID
    assert CredentialStore(paths).client_key() == CLIENT_KEY
    assert len(_posts(mock_server, "/api/v1/node/binding")) == 1, "身份不一致后不许再试原操作"


# ─────────────────────────────────────────────── 恢复失败：下一步句式 + 不循环
def test_recovery_failure_gives_next_step_without_internal_words(home, mock_server, capsys):
    _prepare(home, mock_server)
    mock_server.binding_revoked_count = 1
    mock_server.fail_register_count = 1
    capsys.readouterr()

    assert main(["auth", "--bind", "code-r8", "--json"]) == facts.EXIT_AUTH
    err = capsys.readouterr().err
    assert "自动恢复失败" in err and "到网页端解绑后重新绑定" in err
    _assert_customer_copy(err)
    assert len(_posts(mock_server, "/api/v1/node/register")) == 1
    assert len(_posts(mock_server, "/api/v1/node/binding")) == 1


def test_recovery_attempt_is_bounded_to_one_register_and_one_retry():
    """纯函数级：恢复/重试各一次（用假 operation 验证不会第二次恢复）。"""
    from djb_node import cli as cli_mod
    from djb_node.errors import AuthError, DjbError

    calls = {"op": 0, "recover": 0}

    class _Creds:
        def set_node(self, node_id, key):
            return None

        def client_key(self):
            return "ck_new"

    def operation():
        calls["op"] += 1
        if calls["op"] == 1:
            raise AuthError("revoked", code=40102, reason="client_key_revoked")
        raise DjbError("still failing", code=40102)

    original = cli_mod._recover_client_key

    def fake_recover(**_kwargs):
        calls["recover"] += 1
        return {"node_id": NODE_ID}

    cli_mod._recover_client_key = fake_recover
    try:
        try:
            cli_mod._with_revoked_key_recovery(
                operation, config=type("C", (), {"gateway_url": "http://x"})(),
                credentials=_Creds(), expected_node_id=NODE_ID)
        except AuthError as exc:
            assert "原操作仍未完成" in exc.message
        else:
            raise AssertionError("重试仍失败时必须报错")
    finally:
        cli_mod._recover_client_key = original
    assert calls == {"op": 2, "recover": 1}, "一次恢复 + 一次重试就是上限"
