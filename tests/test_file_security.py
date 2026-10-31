# SPDX-License-Identifier: Apache-2.0
"""R4: pure policy/fault tests plus a real Windows unsafe-ancestor rejection.

No real home, SQLite, daemon, ACL mutation, production identity logging or IPC.
Native secure-root positive coverage is intentionally NOT claimed by fake tests.
"""
from dataclasses import replace
import os

import pytest

from djb_node import file_security as fs

CURRENT = "S-1-5-21-100-200-300-1001"  # Synthetic, not the runner's SID.
OTHER = "S-1-5-21-100-200-300-1002"
ROOT = "E:\\"
TARGET = "E:\\job\\private"


def private(directory=True):
    aces = (fs._Ace(0, 0, fs._DIRECTORY, CURRENT), fs._Ace(0, 9, fs._FILE, CURRENT)) if directory else (
        fs._Ace(0, 0, fs._FILE, CURRENT),)
    return fs._Security(CURRENT, True, False, True, aces, directory, False, 1)


@pytest.mark.parametrize("value", [None, 1, "", "relative", "E:relative", "E:\\", "E:\\\\x",
    "E:/x", "E:\\a/leaf", "E:\\a\\..\\x", "E:\\a\\.\\x", "E:\\a\\",
    "E:\\file:stream", "E:\\x.", "E:\\x ", "E:\\NUL", "E:\\con.txt", "E:\\COM¹",
    "E:\\a\x00b", "E:\\a\nb", "E:\\a?b", "\\\\host\\share\\x", "\\\\?\\E:\\x",
    "\\\\.\\pipe\\x", "E:\\" + "a" * 241])
def test_unsafe_path_is_rejected(value):
    with pytest.raises(fs.FileSecurityError):
        fs._path_parts(value)


def test_absolute_path_normalization_is_narrow():
    assert fs._path_parts("e:\\job\\private") == (ROOT, ("job", "private"))


@pytest.mark.parametrize("directory", [False, True])
def test_exact_private_target_policy(directory):
    fs._assert_explicit_private_target(private(directory), CURRENT, directory=directory)


@pytest.mark.parametrize("change", [dict(owner=OTHER), dict(present=False), dict(null_dacl=True),
    dict(protected=False), dict(aces=()), dict(reparse=True), dict(directory=False),
    dict(aces=(fs._Ace(0, 0, fs._DIRECTORY, CURRENT),)),
    dict(aces=(fs._Ace(0, 0, fs._DIRECTORY, CURRENT), fs._Ace(0, 9, fs._FILE, OTHER))),
    dict(aces=(fs._Ace(0, 0, 0x1F01FF, CURRENT), fs._Ace(0, 9, fs._FILE, CURRENT))),
    dict(aces=(fs._Ace(0, 16, fs._DIRECTORY, CURRENT), fs._Ace(0, 9, fs._FILE, CURRENT)))])
def test_private_directory_rejects_every_non_exact_policy(change):
    with pytest.raises(fs.FileSecurityError):
        fs._assert_explicit_private_target(replace(private(), **change), CURRENT, directory=True)


@pytest.mark.parametrize("links", [0, 2, 3])
def test_private_file_rejects_hardlinks(links):
    with pytest.raises(fs.FileSecurityError):
        fs._assert_explicit_private_target(replace(private(False), links=links), CURRENT, directory=False)


@pytest.mark.parametrize("ace", [fs._Ace(1, 0, fs._FILE, CURRENT), fs._Ace(5, 0, fs._FILE, CURRENT),
    fs._Ace(9, 0, fs._FILE, CURRENT), fs._Ace(0, 0, fs._FILE, OTHER),
    fs._Ace(0, 16, fs._FILE, CURRENT), fs._Ace(0, 0, 0x1F01FF, CURRENT)])
def test_private_file_rejects_extra_kind_flags_mask_or_subject(ace):
    with pytest.raises(fs.FileSecurityError):
        fs._assert_explicit_private_target(replace(private(False), aces=(ace,)), CURRENT, directory=False)


@pytest.mark.parametrize("mask", [0x2, 0x4, 0x10, 0x40, 0x100, 0x10000, 0x40000, 0x80000,
    0x40000000, 0x10000000, 0x02000000, 0x01000000, 0x001301BF, 0xE0010000])
@pytest.mark.parametrize("flags", [0, 9, 16, 19])
def test_other_ancestor_dangerous_rights_fail_even_inherit_only(mask, flags):
    security = replace(private(), aces=(fs._Ace(0, flags, mask, OTHER),))
    with pytest.raises(fs.FileSecurityError):
        fs._assert_ancestor(security, CURRENT)


@pytest.mark.parametrize("sid", [CURRENT, fs._SYSTEM, fs._ADMINS])
def test_ancestor_os_privilege_subjects_are_not_target_subjects(sid):
    security = replace(private(), owner=sid, protected=False,
                       aces=(fs._Ace(0, 3, 0x10000000, sid),))
    fs._assert_ancestor(security, CURRENT)
    with pytest.raises(fs.FileSecurityError):
        fs._assert_explicit_private_target(security, CURRENT, directory=True)


@pytest.mark.parametrize("mask", [0x1, 0x8, 0x20, 0x80, 0x20000, 0x100000, 0x80000000, 0x20000000])
def test_other_read_traverse_ancestor_ace_is_supported(mask):
    fs._assert_ancestor(replace(private(), aces=(fs._Ace(0, 0, mask, OTHER),)), CURRENT)


@pytest.mark.parametrize("change", [dict(owner=OTHER), dict(null_dacl=True), dict(present=False),
    dict(reparse=True), dict(directory=False), dict(aces=()),
    dict(aces=(fs._Ace(1, 0, 1, CURRENT),)), dict(aces=(fs._Ace(5, 0, 1, CURRENT),)),
    dict(aces=(fs._Ace(0, 0x80, 1, CURRENT),)), dict(aces=(fs._Ace(0, 0, 0x200, CURRENT),))])
def test_ancestor_unknown_or_unsafe_policy_fails(change):
    with pytest.raises(fs.FileSecurityError):
        fs._assert_ancestor(replace(private(), **change), CURRENT)


@pytest.mark.parametrize("mask,expected", [(0x80000000, 0x120089), (0x40000000, 0x120116),
    (0x20000000, 0x1200A0), (0x10000000, 0x1F01FF), (0xE0010000, 0x1301BF)])
def test_file_generic_mapping(mask, expected):
    assert fs._expand_mask(mask) == expected


def test_creator_owner_is_not_treated_as_current_identity():
    security = replace(private(), aces=(fs._Ace(0, 0x0B, 0x10000000, "S-1-3-0"),))
    with pytest.raises(fs.FileSecurityError):
        fs._assert_ancestor(security, CURRENT)


def test_security_object_repr_never_exposes_identity():
    assert CURRENT not in repr(private())
    assert CURRENT not in repr(private().aces[0])


class FakeBackend:
    """Pure orchestration fake, never evidence of native access control."""
    def __init__(self, *, failure=None, snapshots=None, cleanup_fail=None):
        self.events = []
        self.live = []
        self.failure = failure
        self.snapshots = snapshots or {}
        self.cleanup_fail = cleanup_fail

    def event(self, label):
        self.events.append(label)
        if label == self.failure:
            raise RuntimeError("synthetic-sensitive-detail")

    def identity(self):
        self.event("identity")
        return CURRENT

    def volume(self, root):
        self.event("volume:" + root)

    def open(self, path):
        self.event("open:" + path)
        self.live.append(path)
        return path

    def snapshot(self, handle):
        self.event("snapshot:" + handle)
        return self.snapshots.get(handle, private())

    def mkdir(self, path, sid):
        assert self.live == [ROOT, "E:\\job"]
        assert sid == CURRENT
        self.event("mkdir:" + path)

    def close(self, handle):
        self.events.append("close:" + handle)
        self.live.remove(handle)
        if self.cleanup_fail == handle:
            raise RuntimeError("synthetic-sensitive-detail")


def install(monkeypatch, backend):
    monkeypatch.setattr(fs, "_Win32", lambda: backend)


def test_creation_pins_all_ancestors_until_target_readback(monkeypatch):
    backend = FakeBackend()
    install(monkeypatch, backend)
    assert fs.create_private_directory(TARGET) is None
    assert backend.events == ["identity", "volume:E:\\", "open:E:\\", "snapshot:E:\\",
        "open:E:\\job", "snapshot:E:\\job", "mkdir:" + TARGET, "open:" + TARGET,
        "snapshot:" + TARGET, "close:" + TARGET, "close:E:\\job", "close:E:\\"]
    assert not backend.live


def test_assert_never_creates_or_repairs(monkeypatch):
    backend = FakeBackend(snapshots={TARGET: private(False)})
    install(monkeypatch, backend)
    fs.assert_private_path(TARGET)
    assert not any(event.startswith("mkdir:") for event in backend.events)
    assert not backend.live


@pytest.mark.parametrize("failure", ["identity", "volume:E:\\", "open:E:\\", "snapshot:E:\\",
    "open:E:\\job", "snapshot:E:\\job", "mkdir:" + TARGET, "open:" + TARGET, "snapshot:" + TARGET])
def test_faults_close_every_acquired_handle_and_redact(monkeypatch, failure):
    backend = FakeBackend(failure=failure)
    install(monkeypatch, backend)
    with pytest.raises(fs.FileSecurityError) as error:
        fs.create_private_directory(TARGET)
    assert not backend.live
    assert "synthetic-sensitive-detail" not in str(error.value)
    assert CURRENT not in str(error.value)


@pytest.mark.parametrize("handle", [ROOT, "E:\\job", TARGET])
def test_cleanup_failure_never_returns_success_and_attempts_remaining_closes(monkeypatch, handle):
    backend = FakeBackend(cleanup_fail=handle)
    install(monkeypatch, backend)
    with pytest.raises(fs.FileSecurityError):
        fs.create_private_directory(TARGET)
    assert not backend.live


@pytest.mark.parametrize("ancestor", [ROOT, "E:\\job"])
def test_root_has_no_unsafe_ancestor_exception(monkeypatch, ancestor):
    unsafe = replace(private(), aces=(fs._Ace(0, 0, 0x1301BF, OTHER),))
    backend = FakeBackend(snapshots={ancestor: unsafe})
    install(monkeypatch, backend)
    with pytest.raises(fs.FileSecurityError):
        fs.create_private_directory(TARGET)
    assert not any(event.startswith("mkdir:") for event in backend.events)
    assert not backend.live


def test_existing_unsafe_target_does_not_get_repaired(monkeypatch):
    backend = FakeBackend(snapshots={TARGET: replace(private(), protected=False)})
    install(monkeypatch, backend)
    # Fake mkdir models ERROR_ALREADY_EXISTS; target must still be checked.
    with pytest.raises(fs.FileSecurityError):
        fs.create_private_directory(TARGET)
    assert not backend.live
    assert not any("set" in event or "delete" in event for event in backend.events)


@pytest.mark.parametrize("directory", [None, 1, "true"])
def test_directory_argument_is_strict_bool(monkeypatch, directory):
    backend = FakeBackend()
    install(monkeypatch, backend)
    with pytest.raises(fs.FileSecurityError):
        fs.assert_private_path(TARGET, directory)
    assert backend.events == []


def test_no_native_library_load_when_unsupported(monkeypatch):
    monkeypatch.setattr(fs.os, "name", "posix")
    with pytest.raises(fs.FileSecurityError) as error:
        fs._Win32()
    assert error.value.reason == "unsupported_platform"


def _parser_backend(*, ace_offset=8, ace_size=20, subauth=1, in_use=28,
                    declared=28, count=1):
    import ctypes as C
    from types import SimpleNamespace
    backend = object.__new__(fs._Win32)
    backend.C = C
    backend.W = SimpleNamespace(WORD=C.c_uint16, DWORD=C.c_uint32, BOOL=C.c_int32)

    class INFO(C.Structure):
        _fields_ = [("attributes", C.c_uint32), ("links", C.c_uint32)]

    class ACLINFO(C.Structure):
        _fields_ = [("count", C.c_uint32), ("in_use", C.c_uint32), ("free", C.c_uint32)]

    backend.INFO, backend.ACLINFO = INFO, ACLINFO
    raw = bytearray(128)
    raw[:8] = bytes([2, 0]) + declared.to_bytes(2, "little") + count.to_bytes(2, "little") + b"\0\0"
    raw[8:12] = bytes([0, 0]) + ace_size.to_bytes(2, "little")
    raw[12:16] = fs._FILE.to_bytes(4, "little")
    raw[16:24] = bytes([1, subauth, 0, 0, 0, 0, 0, 5])
    memory = C.create_string_buffer(bytes(raw))
    base = C.addressof(memory)
    backend._memory = memory
    freed = []
    backend._free = lambda p: freed.append(p.value)
    backend._sid = lambda p: CURRENT

    def file_info(handle, out):
        out._obj.attributes, out._obj.links = 0, 1
        return True

    def security(handle, kind, flags, owner, group, dacl, sacl, sd):
        owner._obj.value, dacl._obj.value, sd._obj.value = base + 16, base, base
        return 0

    def control(sd, out, revision):
        out._obj.value = 0x1000
        return True

    def dacl(sd, present, out, defaulted):
        present._obj.value, out._obj.value = 1, base
        return True

    def acl_info(pointer, out, size, kind):
        out._obj.count, out._obj.in_use = count, in_use
        return True

    def ace(pointer, index, out):
        out._obj.value = base + ace_offset
        return True

    backend.file_information, backend.get_security = file_info, security
    backend.get_control, backend.get_dacl = control, dacl
    backend.get_acl_info, backend.get_ace = acl_info, ace
    return backend, freed


def test_bounded_acl_parser_reads_valid_synthetic_memory_and_frees():
    backend, freed = _parser_backend()
    result = backend.snapshot(123)
    fs._assert_explicit_private_target(result, CURRENT, directory=False)
    assert len(freed) == 1


@pytest.mark.parametrize("changes", [dict(ace_offset=-1), dict(ace_offset=4),
    dict(ace_offset=26), dict(ace_size=64), dict(ace_size=18), dict(ace_size=12),
    dict(subauth=16), dict(subauth=2), dict(in_use=7), dict(in_use=40),
    dict(declared=7), dict(count=2), dict(count=65535)])
def test_acl_parser_bounds_fail_before_out_of_range_copy_and_free(changes):
    backend, freed = _parser_backend(**changes)
    with pytest.raises(fs.FileSecurityError):
        backend.snapshot(123)
    assert len(freed) == 1


@pytest.mark.parametrize("needed", [0, 32769, 0xFFFFFFFF])
def test_native_path_size_cap_closes_handle_without_allocating(needed):
    import ctypes as C
    backend = object.__new__(fs._Win32)
    backend.C = C
    backend.create_file = lambda *args: 123
    backend.final_path = lambda *args: needed
    closed = []
    backend.close = closed.append
    with pytest.raises(fs.FileSecurityError):
        backend.open(TARGET)
    assert closed == [123]


@pytest.mark.parametrize("first,second,pointer_mode", [(0, None, "valid"), (1, None, "valid"),
    (65537, None, "valid"), (64, 65, "valid"), (64, 1, "valid"),
    (64, 64, "outside"), (64, 64, "before"), (64, 64, "bad_count")])
def test_token_user_buffer_bounds_and_cleanup(monkeypatch, first, second, pointer_mode):
    import ctypes as C
    from types import SimpleNamespace
    last = [1008]
    monkeypatch.setattr(C, "get_last_error", lambda: last[0], raising=False)
    backend = object.__new__(fs._Win32)
    backend.C, backend.W = C, SimpleNamespace(DWORD=C.c_uint32)
    backend.current_thread = lambda: -2
    backend.current_process = lambda: -1
    backend.open_thread_token = lambda *args: False

    def open_process(handle, rights, out):
        out._obj.value = 123
        return True

    def token_info(handle, kind, buffer, capacity, size):
        if buffer is None:
            size._obj.value, last[0] = first, 122
            return False
        size._obj.value = second
        base = C.addressof(buffer)
        pointer = base + 32
        if pointer_mode == "outside":
            pointer = base + 100
        elif pointer_mode == "before":
            pointer = base
        C.cast(buffer, C.POINTER(C.c_void_p))[0] = pointer
        # Only write within actual synthetic allocation.
        C.memmove(base + 32, bytes([1, 16 if pointer_mode == "bad_count" else 1, 0, 0, 0, 0, 0, 5]), 8)
        return True

    backend.open_process_token, backend.get_token_information = open_process, token_info
    closed = []
    backend.close = closed.append
    backend._sid = lambda pointer: CURRENT
    with pytest.raises(fs.FileSecurityError):
        backend.identity()
    assert len(closed) == 1


@pytest.mark.parametrize("present,error", [(True, 0), (False, 5), (False, 1347)])
def test_thread_token_or_query_error_never_uses_process_identity(monkeypatch, present, error):
    import ctypes as C
    backend = object.__new__(fs._Win32)
    backend.C, backend.W = C, object()
    backend.current_thread = lambda: -2
    monkeypatch.setattr(C, "get_last_error", lambda: error, raising=False)
    def thread(handle, rights, as_self, out):
        out._obj.value = 123
        return present
    backend.open_thread_token = thread
    closed = []
    backend.close = closed.append
    with pytest.raises(fs.FileSecurityError):
        backend.identity()
    assert len(closed) == int(present)


@pytest.mark.skipif(os.name != "nt", reason="native Windows evidence requires Windows QA")
def test_native_low_level_synthetic_only_descriptor_roundtrip(tmp_path):
    """ABI/leaf-SD proof only: ancestors remain unsafe, no public safety claim."""
    assert tmp_path.drive.upper() == "E:"
    target = tmp_path / "r4-low-level-synthetic-only"
    assert not target.exists()
    backend = fs._Win32()
    sid = backend.identity()  # Runtime only; never emitted in assertions/logs.
    created = False
    try:
        backend.mkdir(str(target), sid)
        created = True
        handle = backend.open(str(target))
        try:
            first = backend.snapshot(handle)
            fs._assert_explicit_private_target(first, sid, directory=True)
        finally:
            backend.close(handle)
        # Existing path is validation-only at the public layer; backend mkdir
        # handles ERROR_ALREADY_EXISTS without any ACL update.
        backend.mkdir(str(target), sid)
        handle = backend.open(str(target))
        try:
            second = backend.snapshot(handle)
            if first != second:
                raise AssertionError("low-level descriptor changed")
        finally:
            backend.close(handle)
        with pytest.raises(fs.FileSecurityError) as error:
            fs.assert_private_path(str(target), directory=True)
        assert error.value.reason == "untrusted_ancestor"
    finally:
        if created:
            # Exact new empty test object only; never recursive removal/ownership takeover.
            target.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="native Windows evidence requires Windows QA")
def test_native_e_unsafe_ancestor_rejects_before_create(tmp_path):
    """Environment-specific NEGATIVE proof. No bypassed ancestors, no fake SID.

    QA independently established E's OTHER write ACE. If that changes this test
    must be reviewed, not silently converted into an unapproved positive run.
    """
    assert tmp_path.drive.upper() == "E:"
    target = tmp_path / "r4-must-not-be-created"
    assert not target.exists()
    with pytest.raises(fs.FileSecurityError) as error:
        fs.create_private_directory(str(target))
    assert error.value.reason == "untrusted_ancestor"
    assert not target.exists()
