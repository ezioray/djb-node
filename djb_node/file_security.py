# SPDX-License-Identifier: Apache-2.0
"""Windows private-object snapshots, not a lease for a later pathname open.

Only newly created directories receive a security descriptor. Existing objects
are never repaired. No SQLite, IPC, logging, environment switches or ACL setters.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import ntpath
import os

__all__ = ["FileSecurityError", "create_private_directory", "assert_private_path"]

_FILE = 0x0013019F
_DIRECTORY = 0x001301FF
_DANGEROUS = 0x000D0156
_KNOWN_FILE_RIGHTS = 0x001F01FF
_SYSTEM = "S-1-5-18"
_ADMINS = "S-1-5-32-544"


class FileSecurityError(OSError):
    """Fixed reason only: never includes a path, SID or native error text."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("file_security:" + reason)


@dataclass(frozen=True, repr=False)
class _Ace:
    kind: int
    flags: int
    mask: int
    sid: str


@dataclass(frozen=True, repr=False)
class _Security:
    owner: str
    present: bool
    null_dacl: bool
    protected: bool
    aces: tuple[_Ace, ...]
    directory: bool
    reparse: bool
    links: int


def _path_parts(path: str) -> tuple[str, tuple[str, ...]]:
    if (not isinstance(path, str) or not path or len(path) > 240
            or any(c in path for c in '\x00/<>"|?*')):
        raise FileSecurityError("unsafe_path")
    drive, tail = ntpath.splitdrive(path)
    if (len(drive) != 2 or drive[1] != ":" or drive[0] not in
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            or not tail.startswith("\\") or ":" in tail):
        raise FileSecurityError("unsafe_path")
    parts = tail[1:].split("\\")
    reserved = {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
    reserved |= {prefix + n for prefix in ("COM", "LPT") for n in "123456789¹²³"}
    for part in parts:
        if (not part or part in (".", "..") or part.endswith((".", " "))
                or any(ord(c) < 32 for c in part)
                or part.split(".", 1)[0].upper() in reserved):
            raise FileSecurityError("unsafe_path")
    root = drive.upper() + "\\"
    return root, tuple(parts)


def _expand_mask(mask: int) -> int:
    # FILE_GENERIC_* mapping, not pipe access rights.
    mappings = ((0x80000000, 0x00120089), (0x40000000, 0x00120116),
                (0x20000000, 0x001200A0), (0x10000000, 0x001F01FF))
    for generic, specific in mappings:
        if mask & generic:
            mask = (mask & ~generic) | specific
    return mask


def _basic_object(security: _Security, *, directory: bool) -> None:
    if security.reparse or security.directory != directory:
        raise FileSecurityError("unsafe_object")
    if not directory and security.links != 1:
        raise FileSecurityError("unsafe_links")
    if not security.present or security.null_dacl or not security.aces:
        raise FileSecurityError("unsafe_dacl")


def _assert_explicit_private_target(security: _Security, sid: str, *, directory: bool) -> None:
    _basic_object(security, directory=directory)
    if security.owner != sid:
        raise FileSecurityError("unsafe_owner")
    expected = (_Ace(0, 0, _DIRECTORY, sid), _Ace(0, 0x09, _FILE, sid)) if directory else (
        _Ace(0, 0, _FILE, sid),)
    if not security.protected or security.aces != expected:
        raise FileSecurityError("unsafe_dacl")


def _assert_ancestor(security: _Security, sid: str) -> None:
    _basic_object(security, directory=True)
    trusted = {sid, _SYSTEM, _ADMINS}
    if security.owner not in trusted:
        raise FileSecurityError("untrusted_ancestor")
    for ace in security.aces:
        mask = _expand_mask(ace.mask)
        # Deny/conditional/object/unknown ACEs are intentionally unsupported.
        if ace.kind != 0 or ace.flags & ~0x1F or mask & ~_KNOWN_FILE_RIGHTS:
            raise FileSecurityError("untrusted_ancestor")
        # Include inherit-only ACEs. ADD_SUBDIRECTORY alone is conservatively
        # rejected, not asserted to be a demonstrated replacement exploit.
        if ace.sid not in trusted and mask & _DANGEROUS:
            raise FileSecurityError("untrusted_ancestor")


class _Win32:
    """Small independent backend. Native allocations never leave this object."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise FileSecurityError("unsupported_platform")
        import ctypes as C
        from ctypes import wintypes as W
        self.C, self.W = C, W
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.a = C.WinDLL("advapi32", use_last_error=True)
        P = C.c_void_p
        DWORD = W.DWORD

        class SA(C.Structure):
            _fields_ = [("nLength", DWORD), ("lpSecurityDescriptor", P), ("bInheritHandle", W.BOOL)]

        class INFO(C.Structure):
            _fields_ = [("attributes", DWORD), ("creation", W.FILETIME),
                        ("access", W.FILETIME), ("write", W.FILETIME),
                        ("volume", DWORD), ("size_high", DWORD), ("size_low", DWORD),
                        ("links", DWORD), ("index_high", DWORD), ("index_low", DWORD)]

        class ACLINFO(C.Structure):
            _fields_ = [("count", DWORD), ("in_use", DWORD), ("free", DWORD)]

        self.SA, self.INFO, self.ACLINFO = SA, INFO, ACLINFO

        def bind(dll, name, args, result):
            fn = getattr(dll, name)
            fn.argtypes, fn.restype = args, result
            return fn

        self.current_process = bind(self.k, "GetCurrentProcess", [], P)
        self.current_thread = bind(self.k, "GetCurrentThread", [], P)
        self.close_handle = bind(self.k, "CloseHandle", [P], W.BOOL)
        self.local_free = bind(self.k, "LocalFree", [P], P)
        self.open_thread_token = bind(self.a, "OpenThreadToken", [P, DWORD, W.BOOL, C.POINTER(P)], W.BOOL)
        self.open_process_token = bind(self.a, "OpenProcessToken", [P, DWORD, C.POINTER(P)], W.BOOL)
        self.get_token_information = bind(self.a, "GetTokenInformation", [P, C.c_int, P, DWORD, C.POINTER(DWORD)], W.BOOL)
        self.sid_valid = bind(self.a, "IsValidSid", [P], W.BOOL)
        self.sid_string = bind(self.a, "ConvertSidToStringSidW", [P, C.POINTER(P)], W.BOOL)
        self.convert_sd = bind(self.a, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                               [W.LPCWSTR, DWORD, C.POINTER(P), C.POINTER(DWORD)], W.BOOL)
        self.create_directory = bind(self.k, "CreateDirectoryW", [W.LPCWSTR, C.POINTER(SA)], W.BOOL)
        self.create_file = bind(self.k, "CreateFileW", [W.LPCWSTR, DWORD, DWORD, P, DWORD, DWORD, P], P)
        self.file_information = bind(self.k, "GetFileInformationByHandle", [P, C.POINTER(INFO)], W.BOOL)
        self.final_path = bind(self.k, "GetFinalPathNameByHandleW", [P, W.LPWSTR, DWORD, DWORD], DWORD)
        self.get_security = bind(self.a, "GetSecurityInfo",
                                 [P, C.c_int, DWORD, C.POINTER(P), C.POINTER(P), C.POINTER(P), C.POINTER(P), C.POINTER(P)], DWORD)
        self.get_control = bind(self.a, "GetSecurityDescriptorControl", [P, C.POINTER(W.WORD), C.POINTER(DWORD)], W.BOOL)
        self.get_dacl = bind(self.a, "GetSecurityDescriptorDacl", [P, C.POINTER(W.BOOL), C.POINTER(P), C.POINTER(W.BOOL)], W.BOOL)
        self.get_acl_info = bind(self.a, "GetAclInformation", [P, P, DWORD, C.c_int], W.BOOL)
        self.get_ace = bind(self.a, "GetAce", [P, DWORD, C.POINTER(P)], W.BOOL)
        self.drive_type = bind(self.k, "GetDriveTypeW", [W.LPCWSTR], W.UINT)
        self.volume_information = bind(self.k, "GetVolumeInformationW",
            [W.LPCWSTR, W.LPWSTR, DWORD, C.POINTER(DWORD), C.POINTER(DWORD),
             C.POINTER(DWORD), W.LPWSTR, DWORD], W.BOOL)

    def _fail(self) -> None:
        raise FileSecurityError("native_error")

    def close(self, handle) -> None:
        if not self.close_handle(handle):
            self._fail()

    def _free(self, pointer) -> None:
        if pointer and self.local_free(pointer):
            self._fail()

    def _sid(self, pointer) -> str:
        C = self.C
        if not pointer or not self.sid_valid(pointer):
            self._fail()
        value = C.c_void_p()
        if not self.sid_string(pointer, C.byref(value)):
            self._fail()
        try:
            return C.wstring_at(value)
        finally:
            self._free(value)

    def identity(self) -> str:
        C, W = self.C, self.W
        thread = C.c_void_p()
        if self.open_thread_token(self.current_thread(), 0x0008, True, C.byref(thread)):
            try:
                raise FileSecurityError("impersonation")
            finally:
                self.close(thread)
        if C.get_last_error() != 1008:  # Only ERROR_NO_TOKEN permits process identity.
            self._fail()
        token = C.c_void_p()
        if not self.open_process_token(self.current_process(), 0x0008, C.byref(token)):
            self._fail()
        try:
            size = W.DWORD()
            ok = self.get_token_information(token, 1, None, 0, C.byref(size))
            class SID_AND_ATTRIBUTES(C.Structure):
                _fields_ = [("sid", C.c_void_p), ("attributes", W.DWORD)]
            if (ok or C.get_last_error() != 122
                    or not C.sizeof(SID_AND_ATTRIBUTES) <= size.value <= 65536):
                self._fail()
            capacity = size.value
            buffer = C.create_string_buffer(capacity)
            if not self.get_token_information(token, 1, buffer, capacity, C.byref(size)):
                self._fail()
            if not C.sizeof(SID_AND_ATTRIBUTES) <= size.value <= capacity:
                self._fail()
            pointer = C.cast(buffer, C.POINTER(C.c_void_p))[0]
            base = C.addressof(buffer)
            if not pointer or not base + C.sizeof(SID_AND_ATTRIBUTES) <= pointer <= base + size.value - 8:
                self._fail()
            sid_header = C.string_at(pointer, 8)
            if (sid_header[0] != 1 or sid_header[1] > 15
                    or pointer + 8 + 4 * sid_header[1] > base + size.value):
                self._fail()
            return self._sid(pointer)
        finally:
            self.close(token)

    def volume(self, root: str) -> None:
        C, W = self.C, self.W
        if self.drive_type(root) != 3:  # DRIVE_FIXED
            raise FileSecurityError("unsupported_volume")
        name = C.create_unicode_buffer(64)
        flags = W.DWORD()
        if not self.volume_information(root, None, 0, None, None, C.byref(flags), name, len(name)):
            self._fail()
        if name.value.upper() != "NTFS" or not flags.value & 0x00000008:
            raise FileSecurityError("unsupported_volume")

    def open(self, path: str):
        C = self.C
        handle = self.create_file(path, 0x00020080, 0x00000003, None, 3,
                                  0x02200000, None)  # BACKUP_SEMANTICS|OPEN_REPARSE_POINT
        if handle is None or handle == C.c_void_p(-1).value:
            self._fail()
        try:
            # Reject short-name / alias resolution rather than claiming the checked
            # name and opened object are necessarily identical.
            needed = self.final_path(handle, None, 0, 0)
            if not 1 <= needed <= 32768:
                self._fail()
            buffer = C.create_unicode_buffer(needed + 1)
            length = self.final_path(handle, buffer, len(buffer), 0)
            if not length or length >= len(buffer):
                self._fail()
            actual = buffer.value
            if actual.startswith("\\\\?\\"):
                actual = actual[4:]
            if actual.rstrip("\\").casefold() != path.rstrip("\\").casefold():
                raise FileSecurityError("unsafe_path")
            return handle
        except BaseException:
            self.close(handle)
            raise

    def snapshot(self, handle) -> _Security:
        C, W = self.C, self.W
        info = self.INFO()
        if not self.file_information(handle, C.byref(info)):
            self._fail()
        owner, dacl, sd = C.c_void_p(), C.c_void_p(), C.c_void_p()
        result = self.get_security(handle, 1, 0x00000005, C.byref(owner), None,
                                   C.byref(dacl), None, C.byref(sd))
        if result:
            self._free(sd)
            self._fail()
        try:
            control, revision = W.WORD(), W.DWORD()
            if not sd or not self.get_control(sd, C.byref(control), C.byref(revision)):
                self._fail()
            present, defaulted, actual_dacl = W.BOOL(), W.BOOL(), C.c_void_p()
            if not self.get_dacl(sd, C.byref(present), C.byref(actual_dacl), C.byref(defaulted)):
                self._fail()
            aces: list[_Ace] = []
            if present and actual_dacl:
                acl = self.ACLINFO()
                if not self.get_acl_info(actual_dacl, C.byref(acl), C.sizeof(acl), 2):
                    self._fail()
                # ACL is WORD-sized; bound every ACE before copying native memory.
                header = C.string_at(actual_dacl, 8)
                declared = int.from_bytes(header[2:4], "little")
                declared_count = int.from_bytes(header[4:6], "little")
                if (header[0] not in (2, 4) or not 8 <= acl.in_use <= declared <= 65535
                        or acl.count != declared_count or acl.count > (acl.in_use - 8) // 16):
                    raise FileSecurityError("unsafe_dacl")
                low, high = actual_dacl.value + 8, actual_dacl.value + acl.in_use
                previous_end = low
                for index in range(acl.count):
                    ace = C.c_void_p()
                    if not self.get_ace(actual_dacl, index, C.byref(ace)) or not ace:
                        self._fail()
                    if not previous_end <= ace.value <= high - 4:
                        raise FileSecurityError("unsafe_dacl")
                    header = C.string_at(ace, 4)
                    kind, flags = header[0], header[1]
                    size = int.from_bytes(header[2:4], "little")
                    if kind != 0 or size < 16 or size % 4 or ace.value + size > high:
                        raise FileSecurityError("unsafe_dacl")
                    previous_end = ace.value + size
                    raw = C.string_at(ace, size)
                    # SID at offset 8: revision/count + identifier authority + subauthorities.
                    if raw[8] != 1 or raw[9] > 15 or 8 + 8 + 4 * raw[9] != size:
                        raise FileSecurityError("unsafe_dacl")
                    aces.append(_Ace(kind, flags, int.from_bytes(raw[4:8], "little"),
                                     self._sid(ace.value + 8)))
            return _Security(self._sid(owner), bool(present), not bool(actual_dacl),
                bool(control.value & 0x1000), tuple(aces), bool(info.attributes & 0x10),
                bool(info.attributes & 0x400), int(info.links))
        finally:
            self._free(sd)

    def mkdir(self, path: str, sid: str) -> None:
        C = self.C
        sddl = (f"O:{sid}D:P(A;;0x001301ff;;;{sid})"
                f"(A;OIIO;0x0013019f;;;{sid})")
        sd = C.c_void_p()
        if not self.convert_sd(sddl, 1, C.byref(sd), None):
            self._fail()
        try:
            sa = self.SA(C.sizeof(self.SA), sd, False)
            if not self.create_directory(path, C.byref(sa)):
                if C.get_last_error() != 183:  # ERROR_ALREADY_EXISTS: validate, never repair.
                    self._fail()
        finally:
            self._free(sd)


@contextmanager
def _pinned_ancestors(backend, root: str, parts: tuple[str, ...], sid: str):
    handles = []
    try:
        current = root
        for component in (None, *parts[:-1]):
            if component is not None:
                current = ntpath.join(current, component)
            handle = backend.open(current)
            handles.append(handle)
            _assert_ancestor(backend.snapshot(handle), sid)
        yield
    finally:
        failure = False
        for handle in reversed(handles):
            try:
                backend.close(handle)
            except Exception:
                failure = True
        if failure:
            raise FileSecurityError("cleanup_failed") from None


def _operate(path: str, *, directory: bool, create: bool) -> None:
    root, parts = _path_parts(path)
    if type(directory) is not bool:
        raise FileSecurityError("unsafe_argument")
    try:
        backend = _Win32()
        sid = backend.identity()
        backend.volume(root)
        canonical = ntpath.join(root, *parts)
        with _pinned_ancestors(backend, root, parts, sid):
            if create:
                backend.mkdir(canonical, sid)
            handle = backend.open(canonical)
            try:
                _assert_explicit_private_target(backend.snapshot(handle), sid, directory=directory)
            finally:
                backend.close(handle)
    except FileSecurityError:
        raise
    except Exception:
        raise FileSecurityError("native_error") from None


def create_private_directory(path: str) -> None:
    """Create one private directory or validate an existing one; never repair it.

    Ancestor pins end before return. This does not authorize a subsequent SQLite
    pathname open. A failed post-create check can leave an empty directory.
    """
    _operate(path, directory=True, create=True)


def assert_private_path(path: str, directory: bool = False) -> None:
    """Read-only strict protected-ACL snapshot, not a persistent object lease."""
    _operate(path, directory=directory, create=False)


# R6 object-only APIs deliberately do NOT weaken _operate/assert_private_path.
__all__ += ["create_private_file_object", "assert_private_file_object"]


def _file_object_only(path: str, *, create: bool) -> None:
    """File-object DACL snapshot only; no ancestor pins or persistent path lease."""
    root, parts = _path_parts(path)
    canonical = root + "\\".join(parts)
    if os.name != "nt":
        raise FileSecurityError("unsupported_platform")
    try:
        backend = _Win32()
        sid = backend.identity()
        backend.volume(root)
        if create:
            C = backend.C
            descriptor = C.c_void_p()
            sddl = f"O:{sid}D:P(A;;0x{_FILE:08X};;;{sid})"
            if not backend.convert_sd(sddl, 1, C.byref(descriptor), None):
                backend._fail()
            try:
                attributes = backend.SA(C.sizeof(backend.SA), descriptor, False)
                handle = backend.create_file(canonical, _FILE, 3, C.byref(attributes),
                                             1, 0x00200000, None)  # CREATE_NEW, OPEN_REPARSE_POINT
                if handle is None or handle == C.c_void_p(-1).value:
                    if C.get_last_error() not in (80, 183):  # never overwrite/repair existing ACL
                        backend._fail()
                else:
                    try:
                        _assert_explicit_private_target(backend.snapshot(handle), sid, directory=False)
                    finally:
                        backend.close(handle)
            finally:
                backend._free(descriptor)
        handle = backend.open(canonical)
        try:
            _assert_explicit_private_target(backend.snapshot(handle), sid, directory=False)
        finally:
            backend.close(handle)
    except FileSecurityError:
        raise
    except Exception:
        raise FileSecurityError("native_error") from None


def create_private_file_object(path: str) -> None:
    """Create a new empty current-user-only protected-DACL file, or validate existing.

    Only the file object is checked (owner, exact DACL, no reparse/hardlink).
    Does NOT pin/authenticate ancestors: 不认证路径安全性/不构成持久租约.
    Does not authorize a subsequent SQLite pathname open, and never repairs an
    existing file. Failed checks may leave an empty newly created file.
    """
    _file_object_only(path, create=True)


def assert_private_file_object(path: str) -> None:
    """Read-only file-object DACL snapshot; 不认证路径安全性/不构成持久租约.

    No ancestor pinning; not interchangeable with assert_private_path.
    """
    _file_object_only(path, create=False)
