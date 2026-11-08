# SPDX-License-Identifier: Apache-2.0
"""R3 pure tests first. No native calls unless explicitly marked native."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
import ctypes
import json
import struct
import threading
import time

import pytest

from djb_node import facts
from djb_node import ipc_windows as ipc


@pytest.fixture
def clean_registry(monkeypatch):
    registry = ipc.Registry()
    monkeypatch.setattr(ipc, "_REGISTRY", registry)
    return registry


@pytest.fixture
def flag(monkeypatch):
    monkeypatch.setitem(facts.FEATURE_FLAGS, ipc.FLAG, {
        "default": True, "owner": "client-seat", "expire_at": "2026-12-31",
        "rollback_rule": "关闭Windows命名管道控制并显示不可用；不退TCP/不更改daemon运行偏好",
    })


def test_flag_default_expiry_and_metadata(flag, monkeypatch):
    config = {"feature_flags": {ipc.FLAG: True}}
    assert ipc.windows_ipc_enabled(config, today=date(2026, 10, 31))
    # 2026-11-05 起首发默认开启：**空配置**也应为真（用户装完就能用，不需要写 config）
    assert ipc.windows_ipc_enabled({}, today=date(2026, 10, 31))
    # 显式关掉 ⇒ 立刻失效（kill switch）
    assert not ipc.windows_ipc_enabled({"feature_flags": {ipc.FLAG: False}},
                                       today=date(2026, 10, 31))
    assert not ipc.windows_ipc_enabled(config, today=date(2027, 1, 1))
    # default 的合法值是「与发布决策一致」（2026-11-05 起为 True）；改回 False ⇒ fail closed
    for field, invalid in (("default", False), ("owner", ""), ("expire_at", "bad"),
                           ("rollback_rule", "")):
        original = facts.FEATURE_FLAGS[ipc.FLAG][field]
        facts.FEATURE_FLAGS[ipc.FLAG][field] = invalid
        assert not ipc.windows_ipc_enabled(config, today=date(2026, 10, 31))
        facts.FEATURE_FLAGS[ipc.FLAG][field] = original


def test_fresh_loader_exception_fails_closed():
    def loader():
        raise ValueError("private-marker")
    assert not ipc.fresh_enabled(loader)


def test_identity_is_private_and_exact():
    identity = ipc.Identity("S-1-5-21-123", 1, 0x2000)
    assert "123" not in repr(identity)
    assert identity != ipc.Identity(identity.sid, 2, identity.integrity)
    assert identity != ipc.Identity(identity.sid, 1, 0x3000)
    assert identity != ipc.Identity("S-1-5-21-124", 1, 0x2000)
    with pytest.raises(ipc.IpcUnavailable):
        ipc.Identity(identity.sid, 1, 0x4000).validate()


def test_descriptor_and_name_exclude_create_instance_and_secrets(tmp_path):
    identity = ipc.Identity("S-1-5-21-123", 1, 0x2000)
    assert ipc.ACCESS & 4 == 0
    assert ipc.ACCESS & 0x40000000 == 0
    assert ipc.security_descriptor(identity) == "D:P(A;;0x0012019B;;;S-1-5-21-123)S:(ML;;NRNW;;;S-1-16-8192)"
    name = ipc.pipe_name(str(tmp_path / ("long" * 500)), identity)
    assert name.startswith("\\\\.\\pipe\\djb-control-")
    assert len(name) < 120 and identity.sid not in name


@pytest.mark.parametrize("payload", [b"[]", b"null", b'{"a":NaN}', b'{"a":Infinity}', b"\xff", b"{"])
def test_bad_json_rejected(payload):
    with pytest.raises(ipc.IpcUnavailable):
        ipc.decode_payload(payload)


def test_frame_bounds_and_roundtrip():
    encoded = ipc.encode_frame({"cmd": "echo"}, 64)
    length = struct.unpack("<I", encoded[:4])[0]
    assert length == len(encoded)-4
    assert ipc.decode_payload(encoded[4:]) == {"cmd": "echo"}
    for value in ([1], {"x": float("nan")}, {"x": "a" * 65}):
        with pytest.raises(ipc.IpcUnavailable):
            ipc.encode_frame(value, 64)


def operation(handle):
    return SimpleNamespace(handle=handle)


def test_registry_two_slots_and_poison_never_issues_new(clean_registry):
    registry = clean_registry
    a, b, c = operation(1), operation(2), operation(3)
    seen = []
    deadline = time.monotonic()+1
    registry.issue(a, deadline, lambda: seen.append(1))
    registry.issue(b, deadline, lambda: seen.append(2))
    with pytest.raises(ipc.IpcUnavailable):
        registry.issue(c, time.monotonic(), lambda: seen.append(3))
    registry.poison(a)
    registry.poison(b)
    assert len(registry.quarantine) == 2
    with pytest.raises(ipc.IpcUnavailable):
        registry.issue(c, deadline, lambda: seen.append(4))
    assert seen == [1, 2]


def test_registry_releases_slot_per_operation(clean_registry):
    a, b = operation(1), operation(1)
    seen = []
    clean_registry.issue(a, time.monotonic()+1, lambda: seen.append("first"))
    clean_registry.release(a)
    clean_registry.issue(b, time.monotonic()+1, lambda: seen.append("second"))
    clean_registry.release(b)
    assert seen == ["first", "second"] and not clean_registry.active


def test_poison_and_issue_atomic_not_issue_after_poison(clean_registry):
    started, allow_finish, poison_done = threading.Event(), threading.Event(), threading.Event()
    a = operation(1)
    def submit():
        def issue():
            started.set()
            assert allow_finish.wait(1)
        clean_registry.issue(a, time.monotonic()+2, issue)
    def poison():
        clean_registry.poison(a)
        poison_done.set()
    thread = threading.Thread(target=submit)
    thread.start()
    assert started.wait(1)
    poison_thread = threading.Thread(target=poison)
    poison_thread.start()
    assert not poison_done.wait(.02)
    allow_finish.set()
    thread.join(1)
    poison_thread.join(1)
    assert poison_done.is_set()
    with pytest.raises(ipc.IpcUnavailable):
        clean_registry.issue(operation(2), time.monotonic()+1, lambda: pytest.fail("issued after poison"))


def test_registry_issue_failure_clears_slot(clean_registry):
    def fail():
        raise OSError()
    with pytest.raises(OSError):
        clean_registry.issue(operation(1), time.monotonic()+1, fail)
    assert not clean_registry.active


class Kernel:
    def __init__(self, wait_result=0, cancel_raises=False):
        self.closed, self.cancelled = [], []
        self.wait_result = wait_result
        self.cancel_raises = cancel_raises
    def CreateEventW(self, *_): return 10
    def ConnectNamedPipe(self, *_): return False
    def CancelIoEx(self, handle, *_):
        self.cancelled.append(handle)
        if self.cancel_raises:
            raise OSError()
        return True
    def WaitForSingleObject(self, *_): return self.wait_result
    def CloseHandle(self, handle): self.closed.append(handle)
    def GetOverlappedResult(self, *_): return True


@pytest.mark.parametrize("wait_result,cancel_raises", [(258, False), (0xFFFFFFFF, False), (0, True)])
def test_cancel_unconfirmed_holds_kernel_objects(clean_registry, monkeypatch, wait_result, cancel_raises):
    api = ipc.Win32.__new__(ipc.Win32)
    api.k = Kernel(wait_result, cancel_raises)
    monkeypatch.setattr(ipc.C, "get_last_error", lambda: ipc.ERROR_IO_PENDING, raising=False)
    cancelled = iter([False, True])
    with pytest.raises(ipc.IpcUnavailable):
        api.operation(1, "connect", 0, time.monotonic()+1, lambda: next(cancelled, True))
    assert clean_registry.poisoned
    assert len(clean_registry.quarantine) == 1
    op = clean_registry.quarantine[0]
    assert op.buffer is not None and op.overlap is not None and op.event == 10
    api.close(1)
    assert api.k.closed == []


def test_confirmed_cancel_releases_slot_and_event(clean_registry, monkeypatch):
    api = ipc.Win32.__new__(ipc.Win32)
    api.k = Kernel()
    monkeypatch.setattr(ipc.C, "get_last_error", lambda: ipc.ERROR_IO_PENDING, raising=False)
    cancelled = iter([False, True])
    with pytest.raises(ipc.IpcUnavailable):
        api.operation(1, "connect", 0, time.monotonic()+1, lambda: next(cancelled, True))
    assert not clean_registry.poisoned and not clean_registry.active
    assert api.k.closed == [10]


def test_disabled_client_never_constructs_native_api(monkeypatch):
    monkeypatch.setattr(ipc, "Win32", lambda: pytest.fail("native api created"))
    with pytest.raises(ipc.IpcUnavailable):
        ipc.windows_call("isolated-home", {"cmd": "echo"}, enabled=lambda: False)


@pytest.mark.parametrize("timeout", [0, -1, 31, True, float("nan"), float("inf")])
def test_bad_timeout_never_constructs_native_api(timeout, monkeypatch):
    monkeypatch.setattr(ipc, "Win32", lambda: pytest.fail("native api created"))
    with pytest.raises(Exception):
        ipc.windows_call("isolated-home", {"cmd": "echo"}, enabled=lambda: True, timeout=timeout)


@pytest.mark.parametrize("payload", [b'{"x":1e999}', b'{"cmd":"a","cmd":"b"}',
                                    b'{"x":' + b'['*40 + b'0' + b']'*40 + b'}',
                                    b'{"x":' + b'['*2000 + b'0' + b']'*2000 + b'}'])
def test_overflow_duplicate_and_deep_json_never_dispatch(payload):
    with pytest.raises(ipc.IpcUnavailable):
        ipc.decode_payload(payload)


def test_handler_error_envelope_does_not_echo_private_message():
    def fail(_):
        raise ipc.UsageError("private-marker", code=40001)
    result = ipc.dispatch(fail, {})
    assert result["ok"] is False
    assert result["error"]["code"] == 40001
    assert result["error"]["type"] == "invalid_request_error"
    assert "private-marker" not in json.dumps(result)


def test_expired_operation_never_issues_even_with_free_slot(clean_registry):
    with pytest.raises(ipc.IpcUnavailable):
        clean_registry.issue(operation(1), time.monotonic()-1,
                             lambda: pytest.fail("issued after deadline"))


def test_signalled_but_incomplete_operation_quarantines(clean_registry, monkeypatch):
    api = ipc.Win32.__new__(ipc.Win32)
    api.k = Kernel()
    api.k.GetOverlappedResult = lambda *_: False
    errors = iter([ipc.ERROR_IO_PENDING, 996, 996])
    monkeypatch.setattr(ipc.C, "get_last_error", lambda: next(errors, 996), raising=False)
    with pytest.raises(ipc.IpcUnavailable):
        api.operation(1, "connect", 0, time.monotonic()+1, lambda: False)
    assert clean_registry.poisoned and len(clean_registry.quarantine) == 1
    assert not api.k.closed


def test_encode_rejects_subclasses_and_cycles():
    class Strange(dict):
        def items(self):
            pytest.fail("custom code called")
    with pytest.raises(ipc.IpcUnavailable):
        ipc.encode_frame(Strange(), 100)
    cyclic = {}; cyclic["x"] = cyclic
    with pytest.raises(ipc.IpcUnavailable):
        ipc.encode_frame(cyclic, 1000)


def test_thread_token_error_only_no_token_is_accepted(monkeypatch):
    api = ipc.Win32.__new__(ipc.Win32)
    api.k = SimpleNamespace(GetCurrentThread=lambda: 5)
    api.a = SimpleNamespace(OpenThreadToken=lambda *_: False)
    monkeypatch.setattr(ipc.C, "get_last_error", lambda: 5, raising=False)
    with pytest.raises(ipc.IpcUnavailable): api.no_thread_token()
    monkeypatch.setattr(ipc.C, "get_last_error", lambda: ipc.ERROR_NO_TOKEN, raising=False)
    api.no_thread_token()


def test_revert_failure_poison_before_handler(clean_registry):
    api = ipc.Win32.__new__(ipc.Win32)
    api.no_thread_token = lambda: None
    api.k = SimpleNamespace(GetCurrentThread=lambda: 5, CloseHandle=lambda *_: None)
    api.a = SimpleNamespace(ImpersonateNamedPipeClient=lambda *_: True,
                            OpenThreadToken=lambda *_: True, RevertToSelf=lambda: False)
    identity = ipc.Identity("S-1-5-21-123", 1, 0x2000)
    api.token_identity = lambda _: identity
    with pytest.raises(ipc.IpcPoisoned):
        api.effective_client(1, identity)
    assert clean_registry.poisoned


def test_local_listener_close_bound_never_shutdown_or_close_running():
    from djb_node.server import LocalApiServer
    server = LocalApiServer.__new__(LocalApiServer)
    closed = []
    server._server = SimpleNamespace(server_close=lambda: closed.append(True),
                                     shutdown=lambda: pytest.fail("deadlocking shutdown"))
    server._thread = SimpleNamespace(ident=123)
    with pytest.raises(ipc.UsageError): server.close_bound()
    assert not closed
    server._thread = None
    server.close_bound()
    assert closed == [True] and server._server is None


def test_windows_factory_off_has_no_getuid_or_native(monkeypatch):
    from djb_node import daemon
    from djb_node.config import Paths
    paths = Paths("isolated-placeholder")
    monkeypatch.setattr(daemon.os, "name", "nt")
    monkeypatch.setattr(daemon, "windows_control_enabled", lambda _: False)
    monkeypatch.setattr(ipc, "Win32", lambda: pytest.fail("native constructed"))
    with pytest.raises(ipc.IpcUnavailable): daemon.control_socket_file(paths)
    with pytest.raises(ipc.IpcUnavailable):
        daemon.control_server_factory(paths, lambda _: {}, stop_event=threading.Event())


def test_windows_start_bind_failure_only_closes_own_control(monkeypatch):
    from djb_node import daemon
    values = {"daemon_state": {"old": True}, "runtime": {"old": True}}
    initial = dict(values)
    closed = []
    control = SimpleNamespace(open=lambda: None, is_alive=lambda: False,
                              close_bound=lambda: closed.append("control"))
    monkeypatch.setattr(daemon, "control_server_factory", lambda *a, **k: control)
    monkeypatch.setattr(daemon, "setup_logging", lambda *a, **k: None)
    d = daemon.Daemon.__new__(daemon.Daemon)
    d.paths = SimpleNamespace(prepare=lambda: None, log_file="unused")
    d.control = lambda _: {}
    d._stop_event = threading.Event()
    d._control = None
    d.store = SimpleNamespace(get_kv=lambda key: values.get(key),
                             set_kv=lambda k,v: values.__setitem__(k,v), is_initialized=lambda: True)
    d.machine = SimpleNamespace(snapshot="old-snapshot", restore=lambda x: None)
    d.config = SimpleNamespace(log_level="INFO")
    d.credentials = SimpleNamespace(ensure_permissions=lambda: None)
    def fail(): raise OSError("bind failure")
    d.server = SimpleNamespace(bind=fail, close_bound=lambda: pytest.fail("not owned"))
    with pytest.raises(OSError): d._start_windows()
    assert values == initial and closed == ["control"] and not d._stop_event.is_set()


# Native tests are opt-in, Windows only, and use ONLY tmp_path/private echo pipes.
# No Daemon is instantiated; no stop/token/service command is issued.
def native_allowed():
    import os, sys
    return sys.platform == "win32" and os.environ.get("DJB_IPC_NATIVE_TEST") == "1"


def native_echo_handler(command):
    return {"ok": True, "echo": command.get("value", "")}


def test_native_same_process_echo_and_error_ack(tmp_path, clean_registry):
    if not native_allowed(): pytest.skip("explicit private native harness required")
    stop = threading.Event()
    def handler(command):
        if command.get("cmd") == "reject":
            raise ipc.UsageError("private-marker", code=40001)
        return native_echo_handler(command)
    server = ipc.WindowsControlServer(str(tmp_path), handler, stop_event=stop, enabled=lambda: True)
    server.open()
    server.start()
    try:
        for value in ("hello", "x"*60000):
            response = ipc.windows_call(str(tmp_path), {"cmd":"echo", "value":value}, enabled=lambda: True)
            assert response == {"ok":True, "echo":value}
        with pytest.raises(ipc.UsageError):
            ipc.windows_call(str(tmp_path), {"cmd":"reject"}, enabled=lambda: True)
        assert ipc.windows_call(str(tmp_path), {"cmd":"echo"}, enabled=lambda: True)["ok"]
    finally:
        stop.set(); server.join(3)
    assert not server.is_alive() and not clean_registry.active and not clean_registry.quarantine


def test_native_second_instance_rejected(tmp_path, clean_registry):
    if not native_allowed(): pytest.skip("explicit private native harness required")
    one = ipc.WindowsControlServer(str(tmp_path), native_echo_handler,
                                    stop_event=threading.Event(), enabled=lambda: True)
    two = ipc.WindowsControlServer(str(tmp_path), native_echo_handler,
                                    stop_event=threading.Event(), enabled=lambda: True)
    one.open()
    try:
        with pytest.raises(ipc.IpcUnavailable): two.open()
    finally:
        one.close_bound()


def test_native_two_process_identity_boolean(tmp_path, clean_registry):
    if not native_allowed(): pytest.skip("explicit private native harness required")
    import os, subprocess, sys
    stop = threading.Event()
    server = ipc.WindowsControlServer(str(tmp_path), native_echo_handler,
                                       stop_event=stop, enabled=lambda: True)
    server.open(); server.start()
    code = '''import sys
from djb_node.ipc_windows import windows_call
try:
 result=windows_call(sys.argv[1], {"cmd":"echo","value":"identity_matches"}, enabled=lambda: True)
 print("identity_matches=" + str(result.get("echo")=="identity_matches").lower())
except Exception:
 print("identity_matches=false")
 sys.exit(1)
'''
    try:
        result = subprocess.run([sys.executable,"-B","-c",code,str(tmp_path)],
                                capture_output=True,text=True,timeout=12,env=dict(os.environ))
        assert result.returncode == 0 and result.stdout.strip() == "identity_matches=true"
    finally:
        stop.set(); server.join(3)
    assert not server.is_alive() and not clean_registry.quarantine


def test_native_fake_stop_like_finishes_authorized_response(tmp_path, clean_registry):
    if not native_allowed(): pytest.skip("explicit private native harness required")
    stop = threading.Event()
    def handler(_):
        stop.set()  # only this temporary test listener, never daemon.stop()
        return {"ok": True, "result": "final-response"}
    server = ipc.WindowsControlServer(str(tmp_path), handler, stop_event=stop, enabled=lambda: True)
    server.open(); server.start()
    try:
        assert ipc.windows_call(str(tmp_path), {"cmd":"echo"}, enabled=lambda: True)["result"] == "final-response"
    finally:
        stop.set(); server.join(3)
    assert not server.is_alive() and not clean_registry.quarantine



def test_windows_stop_retained_unstarted_control_refuses_full_stop(monkeypatch):
    from djb_node import daemon
    d = daemon.Daemon.__new__(daemon.Daemon)
    d._started = False
    d._control = SimpleNamespace(join=lambda **_: pytest.fail("unstarted join"))
    d._quotes_stop = None            # 同上：缺字段会抛 AttributeError，掩盖真正的断言
    d._quotes_thread = None
    d.server = SimpleNamespace(shutdown=lambda: pytest.fail("unstarted shutdown"))
    monkeypatch.setattr(daemon.os, "name", "nt")
    with pytest.raises(ipc.UsageError, match="清理未完成"):
        d.stop()
    assert d._control is not None


def test_windows_stop_live_control_retains_ref(monkeypatch):
    from djb_node import daemon
    d = daemon.Daemon.__new__(daemon.Daemon)
    d._started = True
    retained = SimpleNamespace(join=lambda **_: None, is_alive=lambda: True)
    d._control = retained
    d._stop_event = threading.Event()
    d._stop_channel = d._stop_model_sync = d._stop_notifications = lambda: None
    # stop() 还会停报价环（CR-20261106 新增）；半成品 Daemon 必须把这些字段补齐，
    # 否则抛的是 AttributeError 而不是用例要验的 UsageError，而 os.name 被临时改成 "nt"
    # 时连 pytest 的失败报告都拼不出来（Path → WindowsPath），整个文件只会打出 INTERNALERROR。
    d._quotes_stop = None
    d._quotes_thread = None
    d.server = SimpleNamespace(shutdown=lambda: None)
    d.machine = SimpleNamespace(snapshot=SimpleNamespace(state="stopped"))
    d._persist = lambda: None
    d.store = SimpleNamespace(set_kv=lambda *args: None)
    monkeypatch.setattr(daemon.os, "name", "nt")
    with pytest.raises(ipc.UsageError, match="清理未完成"):
        d.stop()
    assert d._control is retained and not d._started


def test_expired_after_peer_auth_never_dispatches(clean_registry, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(ipc.time, "monotonic", lambda: clock[0])
    stop = threading.Event()
    calls = []
    identity = ipc.Identity("S-1-5-21-123", 1, 0x2000)
    def authenticate(*_): clock[0] += ipc.REQUEST_TIMEOUT + 1
    api = SimpleNamespace(no_thread_token=lambda: None, operation=lambda *a: None,
                          peer=lambda *a, **k: (), receive=lambda *a: {"cmd":"echo"},
                          effective_client=authenticate, close=lambda *_: None,
                          k=SimpleNamespace(DisconnectNamedPipe=lambda _: stop.set()))
    server = ipc.WindowsControlServer("private", lambda _: calls.append(True),
                                       stop_event=stop, enabled=lambda: True, api=api)
    server.handle = 1; server.identity = identity
    server.run()
    assert calls == []


def test_oversized_string_rejected_before_encoder(monkeypatch):
    monkeypatch.setattr(ipc.json.JSONEncoder, "iterencode", lambda *a, **k: pytest.fail("encoder called"))
    with pytest.raises(ipc.IpcUnavailable):
        ipc.encode_frame({"value": "x"*1024}, 100)


def test_preprocessing_timeout_never_opens_pipe(clean_registry, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(ipc.time, "monotonic", lambda: clock[0])
    def identity():
        clock[0] += 2
        return ipc.Identity("S-1-5-21-123", 1, 0x2000)
    api = SimpleNamespace(current_identity=identity, close=lambda _: None,
                          k=SimpleNamespace(CreateFileW=lambda *a: pytest.fail("pipe opened after deadline")))
    with pytest.raises(ipc.IpcUnavailable):
        ipc.windows_call("private", {"cmd":"echo"}, timeout=1, enabled=lambda: True, api=api)


def test_native_large_response_multichunk_ack(tmp_path, clean_registry):
    if not native_allowed(): pytest.skip("explicit private native harness required")
    stop = threading.Event()
    payload = "0123456789abcdef" * 12288  # 192 KiB response, request remains small.
    server = ipc.WindowsControlServer(str(tmp_path), lambda _: {"ok": True, "data": payload},
                                       stop_event=stop, enabled=lambda: True)
    server.open(); server.start()
    try:
        response = ipc.windows_call(str(tmp_path), {"cmd":"echo"}, enabled=lambda: True)
        assert response == {"ok": True, "data": payload}
    finally:
        stop.set(); server.join(3)
    assert not server.is_alive() and not clean_registry.active and not clean_registry.quarantine


def test_native_idle_connect_cancel_closes_handles(tmp_path, clean_registry):
    if not native_allowed(): pytest.skip("explicit private native harness required")
    api = ipc.Win32()
    api._bind(api.k, "GetProcessHandleCount", [ipc.W.HANDLE, ctypes.POINTER(ipc.W.DWORD)], ipc.W.BOOL)
    def handle_count():
        count = ipc.W.DWORD()
        assert api.k.GetProcessHandleCount(api.k.GetCurrentProcess(), ctypes.byref(count))
        return count.value
    def idle_cycle(index):
        stop = threading.Event()
        server = ipc.WindowsControlServer(str(tmp_path / str(index)), native_echo_handler,
                                           stop_event=stop, enabled=lambda: True)
        server.open(); server.start()
        # Wait for a pending Connect slot without creating a client or guessing a sleep.
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            with clean_registry.condition:
                pending = bool(clean_registry.active)
            if pending: break
            time.sleep(.005)
        assert pending
        stop.set(); server.join(3)
        assert not server.is_alive() and not clean_registry.active and not clean_registry.quarantine
    idle_cycle("warmup")
    before = handle_count()
    for index in range(5): idle_cycle(index)
    after = handle_count()
    assert after <= before, "private idle-pipe handle count grew after warmed cancellation loop"


@pytest.mark.parametrize("mismatch", [ipc.Identity("S-1-5-21-124", 1, 0x2000),
                                      ipc.Identity("S-1-5-21-123", 2, 0x2000),
                                      ipc.Identity("S-1-5-21-123", 1, 0x3000)])
def test_peer_identity_mismatch_closes_process_and_token(clean_registry, mismatch):
    api = ipc.Win32.__new__(ipc.Win32)
    closed = []
    def pid(_pipe, output): output._obj.value = 123; return True
    def token(_process, _access, output): output._obj.value = 200; return True
    api.k = SimpleNamespace(GetNamedPipeServerProcessId=pid, GetNamedPipeClientProcessId=pid,
                            OpenProcess=lambda *a: 100, CloseHandle=lambda h: closed.append(h))
    api.a = SimpleNamespace(OpenProcessToken=token)
    api.token_identity = lambda _: mismatch
    expected = ipc.Identity("S-1-5-21-123", 1, 0x2000)
    with pytest.raises(ipc.IpcUnavailable):
        api.peer(1, server=True, expected=expected)
    assert closed == [200, 100]


@pytest.mark.parametrize("mismatch", [ipc.Identity("S-1-5-21-124", 1, 0x2000),
                                      ipc.Identity("S-1-5-21-123", 2, 0x2000),
                                      ipc.Identity("S-1-5-21-123", 1, 0x3000)])
def test_effective_identity_mismatch_reverts_before_any_handler(clean_registry, mismatch):
    api = ipc.Win32.__new__(ipc.Win32)
    closed, reverted, handled = [], [], []
    api.no_thread_token = lambda: None
    def token(_thread, _access, _self, output): output._obj.value = 200; return True
    api.k = SimpleNamespace(GetCurrentThread=lambda: 10, CloseHandle=lambda h: closed.append(h))
    api.a = SimpleNamespace(ImpersonateNamedPipeClient=lambda _: True, OpenThreadToken=token,
                            RevertToSelf=lambda: reverted.append(True) or True)
    api.token_identity = lambda _: mismatch
    expected = ipc.Identity("S-1-5-21-123", 1, 0x2000)
    with pytest.raises(ipc.IpcUnavailable):
        api.effective_client(1, expected)
        handled.append(True)
    assert closed == [200] and reverted == [True] and handled == []
    assert not clean_registry.poisoned
