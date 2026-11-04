# SPDX-License-Identifier: Apache-2.0
"""Opt-in Windows control IPC. ctypes bindings are lazy; no secrets are persisted.

Trust boundary: equal user SID, session and integrity. No TCP/pickle fallback.
Cancellation that cannot be confirmed poisons this process's transport and holds
kernel-owned objects until exit (at most two), rather than risking use-after-free.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
import struct
import sys
import threading
import time
from typing import Any, Callable

from . import facts
from .config import feature_flag
from .errors import DjbError, NotRunningError, UsageError

FLAG = "client.ipc.windows.enabled"
ACCESS = 0x0012019B  # no FILE_CREATE_PIPE_INSTANCE (0x4), no GENERIC_WRITE
MAX_REQUEST = 64 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
MAX_INFLIGHT = 2
REQUEST_TIMEOUT = 10.0
CANCEL_GRACE = 1.0
ERROR_IO_PENDING = 997
ERROR_NO_TOKEN = 1008
ERROR_OPERATION_ABORTED = 995
ERROR_PIPE_CONNECTED = 535
INVALID_HANDLE = C.c_void_p(-1).value


class IpcUnavailable(NotRunningError):
    """Safe fixed diagnostics only; never forward native messages or identities."""


class IpcPoisoned(IpcUnavailable):
    pass


def unavailable() -> IpcUnavailable:
    return IpcUnavailable("Windows 控制不可用：安全能力、身份或通信校验未通过")


def windows_ipc_enabled(config: Any, *, today: date | None = None) -> bool:
    spec = facts.FEATURE_FLAGS.get(FLAG, {})
    try:
        # tripwire：本开关的声明默认值必须与发布决策一致（2026-11-05 起首发默认开启）。
        # 任何人把 default 改回/改成别的值，本函数立刻 fail closed —— 默认值的变更
        # 必须是一次看得见的改动，而不是悄悄生效。三字段仍逐个校验（见下）。
        #
        # ⚠ 改这一行之前先读：**到期转正（expire_at=2026-12-31 之后）或按 rollback_rule
        #   关掉本开关时，必须同步处理这段**。按 rollback_rule 回滚 = 把 facts.FEATURE_FLAGS
        #   里的 default 改回 False；那一刻本 tripwire 会立刻 fail closed（这是**预期行为**，
        #   不是故障），随后要在这里把判据改成与新的发布决策一致（`is not False`），
        #   否则回滚后所有 Windows 控制用例都会以「开关不可用」skip。
        #   ⇒ 到期/回滚是一次**成对改动**：facts.py 的 default + 这一行，缺一个就是半截回滚。
        if spec.get("default") is not True or spec.get("owner") != "client-seat":
            return False
        if not isinstance(spec.get("rollback_rule"), str) or not spec["rollback_rule"].strip():
            return False
        expires = date.fromisoformat(spec["expire_at"])
        return (today or datetime.now(timezone.utc).date()) <= expires and feature_flag(config, FLAG)
    except (ValueError, TypeError, KeyError):
        return False


def fresh_enabled(loader: Callable[[], Any]) -> bool:
    try:
        return windows_ipc_enabled(loader())
    except Exception:
        return False


@dataclass(frozen=True, repr=False)
class Identity:
    sid: str
    session: int
    integrity: int

    def __repr__(self):
        return "Identity(<private>)"

    def validate(self):
        if not self.sid.startswith("S-1-") or any(c not in "S-0123456789" for c in self.sid):
            raise unavailable()
        if self.session < 0 or self.integrity not in (0x1000, 0x2000, 0x3000):
            raise unavailable()
        return self


def pipe_name(home: str, identity: Identity) -> str:
    identity.validate()
    canonical = os.path.normcase(os.path.abspath(home))
    digest = hashlib.sha256((canonical + "\0" + identity.sid + "\0" +
                             str(identity.session) + "\0" + str(identity.integrity)).encode()).hexdigest()
    return "\\\\.\\pipe\\djb-control-" + digest


def security_descriptor(identity: Identity) -> str:
    identity.validate()
    return f"D:P(A;;0x0012019B;;;{identity.sid})S:(ML;;NRNW;;;S-1-16-{identity.integrity})"


def encode_frame(value: dict, maximum: int) -> bytes:
    if type(value) is not dict:
        raise unavailable()
    # Bounded prewalk avoids serializing an arbitrarily large/deep caller object.
    stack, budget = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        budget += 1
        if depth > 32 or budget > maximum or type(item) not in (dict, list, str, int, float, bool, type(None)):
            raise unavailable()
        if isinstance(item, dict):
            if len(item) > maximum:
                raise unavailable()
            for key, child in item.items():
                if type(key) is not str or len(key) > maximum:
                    raise unavailable()
                budget += len(key)
                stack.append((child, depth+1))
        elif isinstance(item, (list, tuple)):
            if len(item) > maximum:
                raise unavailable()
            stack.extend((child, depth+1) for child in item)
        elif isinstance(item, str):
            budget += len(item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise unavailable()
        elif isinstance(item, int) and item.bit_length() > maximum:
            raise unavailable()
        elif item is not None and not isinstance(item, (int, float, bool)):
            raise unavailable()
        if budget > maximum:
            raise unavailable()
    try:
        chunks, size = [], 0
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False)
        for chunk in encoder.iterencode(value):
            encoded = chunk.encode("utf-8")
            size += len(encoded)
            if size > maximum:
                raise unavailable()
            chunks.append(encoded)
        payload = b"".join(chunks)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise unavailable() from None
    if not payload:
        raise unavailable()
    return struct.pack("<I", len(payload)) + payload


def decode_payload(payload: bytes) -> dict:
    def invalid(_):
        raise ValueError()
    def strict_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError()
        return number
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        value = json.loads(payload.decode("utf-8"), parse_constant=invalid,
                           parse_float=strict_float, object_pairs_hook=unique_pairs)
        if not isinstance(value, dict):
            raise ValueError()
        stack = [(value, 0)]
        while stack:
            item, depth = stack.pop()
            if depth > 32:
                raise ValueError()
            if isinstance(item, dict):
                stack.extend((child, depth+1) for child in item.values())
            elif isinstance(item, list):
                stack.extend((child, depth+1) for child in item)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise unavailable() from None


def dispatch(handler, request):
    try:
        return handler(request)
    except DjbError as exc:
        return {"ok": False, "error": {"code": exc.code, "type": exc.error_type,
                                       "message": "控制请求被拒绝"}}
    except Exception:
        return {"ok": False, "error": {"code": 50000, "type": "server_error",
                                       "message": "控制处理失败"}}


class OVERLAPPED(C.Structure):
    _fields_ = [("Internal", C.c_size_t), ("InternalHigh", C.c_size_t),
                ("Offset", W.DWORD), ("OffsetHigh", W.DWORD), ("hEvent", W.HANDLE)]


class SECURITY_ATTRIBUTES(C.Structure):
    _fields_ = [("nLength", W.DWORD), ("lpSecurityDescriptor", W.LPVOID),
                ("bInheritHandle", W.BOOL)]


class SID_AND_ATTRIBUTES(C.Structure):
    _fields_ = [("Sid", W.LPVOID), ("Attributes", W.DWORD)]


class Registry:
    """Two slots; issue under mutex, NEVER wait for I/O while holding mutex."""
    def __init__(self):
        self.condition = threading.Condition()
        self.active: dict[int, Any] = {}
        self.quarantine: list[Any] = []
        self.poisoned = False

    def check(self):
        with self.condition:
            if self.poisoned:
                raise IpcPoisoned("Windows 控制已安全隔离；本进程不再接受控制")

    def issue(self, op, deadline, issue):
        with self.condition:
            while len(self.active) >= MAX_INFLIGHT or op.handle in self.active:
                if self.poisoned:
                    raise unavailable()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise unavailable()
                self.condition.wait(min(.1, remaining))
            if self.poisoned or time.monotonic() >= deadline:
                raise unavailable()
            self.active[op.handle] = op
            try:
                return issue()  # only nonblocking Win32 call; atomic against poison
            except BaseException:
                if not getattr(op, "submitted", False):
                    self.active.pop(op.handle, None)
                    self.condition.notify_all()
                raise

    def release(self, op):
        with self.condition:
            self.active.pop(op.handle, None)
            self.condition.notify_all()

    def poison(self, op):
        with self.condition:
            self.poisoned = True
            if op not in self.quarantine:
                self.quarantine.append(op)
            # A quarantined operation retains its slot: no new kernel calls.
            self.condition.notify_all()

    def held(self, handle):
        with self.condition:
            return any(op.handle == handle for op in self.quarantine)


_REGISTRY = Registry()


@dataclass(eq=False)
class Pending:
    handle: int
    event: int
    overlap: OVERLAPPED
    buffer: Any
    api: Any
    submitted: bool = False


class Win32:
    """Explicit pointer-width-safe ABI. Instantiation is Windows-only."""
    def __init__(self):
        if sys.platform != "win32":
            raise unavailable()
        try:
            self.k = C.WinDLL("kernel32", use_last_error=True)
            self.a = C.WinDLL("advapi32", use_last_error=True)
            H, D, P, B = W.HANDLE, W.DWORD, W.LPVOID, W.BOOL
            PD = C.POINTER(D)
            self._bind(self.k, "CloseHandle", [H], B)
            self._bind(self.k, "GetCurrentProcess", [], H)
            self._bind(self.k, "GetCurrentThread", [], H)
            self._bind(self.k, "OpenProcess", [D, B, D], H)
            self._bind(self.k, "GetExitCodeProcess", [H, PD], B)
            self._bind(self.k, "CreateEventW", [P, B, B, W.LPCWSTR], H)
            self._bind(self.k, "WaitForSingleObject", [H, D], D)
            self._bind(self.k, "GetOverlappedResult", [H, C.POINTER(OVERLAPPED), PD, B], B)
            self._bind(self.k, "CancelIoEx", [H, C.POINTER(OVERLAPPED)], B)
            self._bind(self.k, "ConnectNamedPipe", [H, C.POINTER(OVERLAPPED)], B)
            self._bind(self.k, "DisconnectNamedPipe", [H], B)
            self._bind(self.k, "ReadFile", [H, P, D, PD, C.POINTER(OVERLAPPED)], B)
            self._bind(self.k, "WriteFile", [H, P, D, PD, C.POINTER(OVERLAPPED)], B)
            self._bind(self.k, "CreateNamedPipeW", [W.LPCWSTR, D, D, D, D, D, D, C.POINTER(SECURITY_ATTRIBUTES)], H)
            self._bind(self.k, "CreateFileW", [W.LPCWSTR, D, D, P, D, D, H], H)
            self._bind(self.k, "GetNamedPipeClientProcessId", [H, PD], B)
            self._bind(self.k, "GetNamedPipeServerProcessId", [H, PD], B)
            self._bind(self.k, "LocalFree", [P], P)
            self._bind(self.a, "OpenProcessToken", [H, D, C.POINTER(H)], B)
            self._bind(self.a, "OpenThreadToken", [H, D, B, C.POINTER(H)], B)
            self._bind(self.a, "GetTokenInformation", [H, D, P, D, PD], B)
            self._bind(self.a, "ConvertSidToStringSidW", [P, C.POINTER(W.LPWSTR)], B)
            self._bind(self.a, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [W.LPCWSTR, D, C.POINTER(P), PD], B)
            self._bind(self.a, "ImpersonateNamedPipeClient", [H], B)
            self._bind(self.a, "RevertToSelf", [], B)
        except (AttributeError, OSError):
            raise unavailable() from None

    @staticmethod
    def _bind(dll, name, args, result):
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, result

    def close(self, handle):
        if handle not in (None, 0, INVALID_HANDLE) and not _REGISTRY.held(handle):
            self.k.CloseHandle(handle)

    def no_thread_token(self):
        token = W.HANDLE()
        if self.a.OpenThreadToken(self.k.GetCurrentThread(), 8, True, C.byref(token)):
            self.close(token.value)
            raise unavailable()
        if C.get_last_error() != ERROR_NO_TOKEN:
            raise unavailable()

    def _info(self, token, kind):
        needed = W.DWORD()
        if self.a.GetTokenInformation(token, kind, None, 0, C.byref(needed)) or C.get_last_error() != 122:
            raise unavailable()
        minimum = C.sizeof(W.DWORD) if kind == 12 else C.sizeof(SID_AND_ATTRIBUTES)
        if not minimum <= needed.value <= 65536:
            raise unavailable()
        buf = C.create_string_buffer(needed.value)
        if not self.a.GetTokenInformation(token, kind, buf, needed, C.byref(needed)):
            raise unavailable()
        return buf

    def _sid(self, pointer):
        text = W.LPWSTR()
        if not self.a.ConvertSidToStringSidW(pointer, C.byref(text)):
            raise unavailable()
        try:
            return text.value
        finally:
            self.k.LocalFree(C.cast(text, W.LPVOID))

    def token_identity(self, token):
        user = self._info(token, 1)
        sid = self._sid(C.cast(user, C.POINTER(SID_AND_ATTRIBUTES)).contents.Sid)
        session = C.cast(self._info(token, 12), C.POINTER(W.DWORD)).contents.value
        label = self._info(token, 25)
        il_sid = self._sid(C.cast(label, C.POINTER(SID_AND_ATTRIBUTES)).contents.Sid)
        try:
            integrity = int(il_sid.rsplit("-", 1)[1])
        except (ValueError, AttributeError):
            raise unavailable() from None
        return Identity(sid, session, integrity).validate()

    def current_identity(self):
        self.no_thread_token()
        token = W.HANDLE()
        if not self.a.OpenProcessToken(self.k.GetCurrentProcess(), 8, C.byref(token)):
            raise unavailable()
        try:
            return self.token_identity(token)
        finally:
            self.close(token.value)

    def peer(self, pipe, *, server: bool, expected: Identity):
        pid = W.DWORD()
        query = self.k.GetNamedPipeServerProcessId if server else self.k.GetNamedPipeClientProcessId
        if not query(pipe, C.byref(pid)) or not pid.value:
            raise unavailable()
        process = self.k.OpenProcess(0x1000, False, pid.value)
        if not process:
            raise unavailable()
        token = W.HANDLE()
        try:
            if not self.a.OpenProcessToken(process, 8, C.byref(token)):
                raise unavailable()
            if self.token_identity(token) != expected:
                raise unavailable()
            exit_code = W.DWORD()
            if not self.k.GetExitCodeProcess(process, C.byref(exit_code)) or exit_code.value != 259:
                raise unavailable()
            # Re-query the pipe association after opening process/token to reject races.
            again = W.DWORD()
            if not query(pipe, C.byref(again)) or again.value != pid.value:
                raise unavailable()
            return process, token.value
        except BaseException:
            self.close(token.value)
            self.close(process)
            raise

    def effective_client(self, pipe, expected):
        self.no_thread_token()
        if not self.a.ImpersonateNamedPipeClient(pipe):
            raise unavailable()
        token = W.HANDLE()
        valid = False
        try:
            if not self.a.OpenThreadToken(self.k.GetCurrentThread(), 8, True, C.byref(token)):
                raise unavailable()
            valid = self.token_identity(token) == expected
        finally:
            self.close(token.value)
            if not self.a.RevertToSelf():
                # Permanent global poison, no handler can run under uncertain identity.
                with _REGISTRY.condition:
                    _REGISTRY.poisoned = True
                    _REGISTRY.condition.notify_all()
                raise IpcPoisoned("Windows 控制身份恢复失败；停止处理")
        if not valid:
            raise unavailable()

    def create(self, name, identity):
        _REGISTRY.check()
        self.no_thread_token()
        descriptor = W.LPVOID()
        if not self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                security_descriptor(identity), 1, C.byref(descriptor), None):
            raise unavailable()
        try:
            attributes = SECURITY_ATTRIBUTES(C.sizeof(SECURITY_ATTRIBUTES), descriptor, False)
            with _REGISTRY.condition:
                _REGISTRY.check()
                handle = self.k.CreateNamedPipeW(name, 0x00000003 | 0x40000000 | 0x00080000,
                                                0x00000008, 1, 65536, 65536, 1000,
                                                C.byref(attributes))
            if handle in (None, 0, INVALID_HANDLE):
                raise unavailable()
            return handle
        finally:
            self.k.LocalFree(descriptor)

    def operation(self, handle, kind, data, deadline, cancelled):
        event = self.k.CreateEventW(None, True, False, None)
        if not event:
            raise unavailable()
        try:
            buffer = C.create_string_buffer(data if isinstance(data, bytes) else data)
            overlap = OVERLAPPED()
            overlap.hEvent = event
            op = Pending(handle, event, overlap, buffer, self)
        except BaseException:
            self.close(event)
            raise
        submitted = False
        confirmed = False
        registered = False
        transferred = W.DWORD()
        try:
            def issue():
                nonlocal submitted
                if cancelled():
                    raise unavailable()
                if kind == "connect":
                    ok = self.k.ConnectNamedPipe(handle, C.byref(overlap))
                else:
                    fn = self.k.ReadFile if kind == "read" else self.k.WriteFile
                    size = len(data) if isinstance(data, bytes) else data
                    ok = fn(handle, buffer, size, None, C.byref(overlap))
                # Conservatively retain ownership if post-call bookkeeping raises.
                submitted = op.submitted = True
                error = 0 if ok else C.get_last_error()
                submitted = op.submitted = bool(ok) or error == ERROR_IO_PENDING
                return bool(ok), error
            ok, error = _REGISTRY.issue(op, deadline, issue)
            registered = True
            if kind == "connect" and error == ERROR_PIPE_CONNECTED:
                confirmed = True
                return b""
            if not ok and error != ERROR_IO_PENDING:
                confirmed = True  # operation was never pending
                raise unavailable()
            if ok:
                confirmed = True
            else:
                while time.monotonic() < deadline and not cancelled() and not _REGISTRY.poisoned:
                    result = self.k.WaitForSingleObject(event, max(1, min(100, int((deadline-time.monotonic())*1000))))
                    if result == 0:
                        confirmed = True
                        break
                    if result != 258:
                        break
            if not confirmed:
                raise unavailable()
            if not self.k.GetOverlappedResult(handle, C.byref(overlap), C.byref(transferred), False):
                if C.get_last_error() == 996:  # IO_INCOMPLETE: event alone did not establish completion
                    confirmed = False
                raise unavailable()
            if kind != "connect":
                if not transferred.value:
                    raise unavailable()
                return bytes(buffer.raw[:transferred.value]) if kind == "read" else transferred.value
            return b""
        finally:
            if submitted and not confirmed:
                try:
                    self.k.CancelIoEx(handle, C.byref(overlap))
                    result = self.k.WaitForSingleObject(event, int(CANCEL_GRACE * 1000))
                    if result == 0:
                        ok = self.k.GetOverlappedResult(handle, C.byref(overlap), C.byref(transferred), False)
                        confirmed = bool(ok) or C.get_last_error() != 996
                    else:
                        confirmed = False
                except BaseException:
                    confirmed = False
                if not confirmed:
                    _REGISTRY.poison(op)
            if not _REGISTRY.held(handle):
                if registered or op.submitted:
                    _REGISTRY.release(op)
                self.close(event)

    def read_exact(self, handle, size, deadline, cancelled):
        output = bytearray()
        while len(output) < size:
            output.extend(self.operation(handle, "read", min(65536, size-len(output)), deadline, cancelled))
        return bytes(output)

    def receive(self, handle, maximum, deadline, cancelled):
        size = struct.unpack("<I", self.read_exact(handle, 4, deadline, cancelled))[0]
        if not 0 < size <= maximum:
            raise unavailable()
        return decode_payload(self.read_exact(handle, size, deadline, cancelled))

    def send(self, handle, payload, deadline, cancelled):
        offset = 0
        while offset < len(payload):
            offset += self.operation(handle, "write", payload[offset:offset+65536], deadline, cancelled)


class WindowsControlServer(threading.Thread):
    def __init__(self, home, handler, *, stop_event, enabled, api=None):
        super().__init__(name="djb-control-win", daemon=True)
        self.api = api
        self.home, self.handler = home, handler
        self.stop_event, self.enabled = stop_event, enabled
        self.handle = None
        self.identity = None

    def _policy_disabled(self):
        try:
            return not self.enabled()
        except Exception:
            return True

    def _disabled(self):
        return self.stop_event.is_set() or self._policy_disabled()

    def open(self):
        if self._disabled() or self.handle is not None:
            raise unavailable()
        _REGISTRY.check()
        self.api = self.api or Win32()
        self.identity = self.api.current_identity()
        self.handle = self.api.create(pipe_name(self.home, self.identity), self.identity)

    def close_bound(self):
        if self.is_alive():
            raise UsageError("Windows 控制线程正在运行，拒绝关闭绑定句柄")
        if self.handle is not None:
            self.api.close(self.handle)
            self.handle = None

    def run(self):
        try:
            self.api.no_thread_token()
            while not self._disabled() and not _REGISTRY.poisoned:
                peer = ()
                try:
                    self.api.operation(self.handle, "connect", 0, time.monotonic()+REQUEST_TIMEOUT, self._disabled)
                    peer = self.api.peer(self.handle, server=False, expected=self.identity)
                    deadline = time.monotonic()+REQUEST_TIMEOUT
                    request = self.api.receive(self.handle, MAX_REQUEST, deadline, self._disabled)
                    self.api.effective_client(self.handle, self.identity)
                    _REGISTRY.check()
                    if self._disabled() or time.monotonic() >= deadline:
                        raise unavailable()
                    response = dispatch(self.handler, request)
                    _REGISTRY.check()
                    if self._policy_disabled():
                        raise unavailable()
                    # Stop only prevents new business; finish this authorized response with deadline.
                    self.api.send(self.handle, encode_frame(response, MAX_RESPONSE), deadline, self._policy_disabled)
                    # DisconnectNamedPipe discards unread bytes; bounded ACK replaces FlushFileBuffers.
                    if self.api.read_exact(self.handle, 1, deadline, self._policy_disabled) != b"\x06":
                        raise unavailable()
                except Exception:
                    # Fixed diagnostic boundary; no arbitrary error strings/payload logging.
                    if _REGISTRY.poisoned:
                        break
                finally:
                    for handle in peer:
                        self.api.close(handle)
                    if not _REGISTRY.held(self.handle):
                        self.api.k.DisconnectNamedPipe(self.handle)
        finally:
            if self.handle is not None:
                self.api.close(self.handle)
                self.handle = None


def windows_call(home, command, *, timeout=5.0, enabled, api=None):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise UsageError("控制超时必须为(0,30]秒")
    try:
        if not enabled():
            raise unavailable()
    except Exception:
        raise unavailable() from None
    deadline = time.monotonic()+timeout
    _REGISTRY.check()
    api = api or Win32()
    identity = api.current_identity()
    payload = encode_frame(command, MAX_REQUEST)
    name = pipe_name(home, identity)
    handle = None
    peer = ()
    def disabled():
        try:
            return not enabled()
        except Exception:
            return True
    try:
        while time.monotonic() < deadline and not disabled():
            with _REGISTRY.condition:
                _REGISTRY.check()
                handle = api.k.CreateFileW(name, ACCESS, 0, None, 3,
                                          0x40000000 | 0x00100000 | 0x00010000, None)
            if handle not in (None, 0, INVALID_HANDLE):
                break
            handle = None
            if C.get_last_error() not in (2, 231):
                raise unavailable()
            time.sleep(min(.02, max(0, deadline-time.monotonic())))
        if handle is None:
            raise unavailable()
        peer = api.peer(handle, server=True, expected=identity)
        api.no_thread_token()
        api.send(handle, payload, deadline, disabled)
        response = api.receive(handle, MAX_RESPONSE, deadline, disabled)
        api.send(handle, b"\x06", deadline, disabled)
        if response.get("ok") is False:
            raise UsageError("Windows 控制命令被拒绝")
        return response
    finally:
        for item in peer:
            api.close(item)
        api.close(handle)
