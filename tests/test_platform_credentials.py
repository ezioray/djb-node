# SPDX-License-Identifier: Apache-2.0
"""凭据后端（C2）：默认文件后端行为不变 + 三端 OS 存储只探测、不自动切换。"""
from __future__ import annotations

import ast
import inspect
import os
import stat

import pytest

from djb_node.errors import AuthError
from djb_node.platform import PlatformInfo, credentials
from djb_node.platform.credentials import (FILE_BACKEND, CredentialBackend, FileBackend,
                                           LibsecretStore, MacKeychainStore,
                                           WindowsCredentialManagerStore, default_backend,
                                           probe_backends)
from djb_node.platform.paths import PlatformPaths


def test_file_backend_is_default_and_protocol_compliant(paths):
    backend = default_backend(paths)
    assert isinstance(backend, FileBackend)
    assert isinstance(backend, CredentialBackend)
    assert isinstance(object(), CredentialBackend) is False
    assert backend.name == FILE_BACKEND == "file-0600"
    assert backend.kind == "file"
    assert backend.available() is True


def test_file_backend_roundtrip_uses_0600_store(paths):
    backend = default_backend(paths)
    assert backend.exists() is False
    backend.set_node("node-1", "ck_secret_value")
    backend.set_upstream_secret("up-1", "sk-upstream-secret")

    assert backend.node_id() == "node-1"
    assert backend.client_key() == "ck_secret_value"
    assert backend.require_client_key() == "ck_secret_value"
    assert backend.upstream_secret("up-1") == "sk-upstream-secret"
    assert os.path.exists(paths.credentials_file)
    assert stat.S_IMODE(os.stat(paths.credentials_file).st_mode) == 0o600
    assert backend.ensure_permissions() == []

    assert backend.delete_upstream_secret("up-1") is True
    assert backend.delete_upstream_secret("up-1") is False
    removed = backend.clear()
    assert paths.credentials_file in removed
    assert backend.exists() is False


def test_file_backend_describe_masks_secrets(paths):
    backend = default_backend(paths)
    backend.set_node("node-1", "ck_super_secret_value")
    described = backend.describe()
    text = repr(described)
    assert "ck_super_secret_value" not in text
    assert described["client_key_masked"] == "*" * (len("ck_super_secret_value") - 4) + "alue"
    assert described["backend"] == FILE_BACKEND
    assert described["storage"] == FILE_BACKEND


def test_file_backend_fails_closed_without_key(paths):
    backend = default_backend(paths)
    with pytest.raises(AuthError):
        backend.require_client_key()


def test_file_backend_accepts_platform_paths(tmp_path):
    layout = PlatformPaths.from_env(str(tmp_path / "portable"))
    backend = FileBackend(layout)
    backend.set_node("node-2", "ck_two")
    assert os.path.exists(layout.credentials_file)
    assert stat.S_IMODE(os.stat(layout.credentials_file).st_mode) == 0o600
    assert layout.credentials_file.startswith(str(tmp_path / "portable"))


def test_keychain_probe_requires_darwin_and_security_binary():
    mac = MacKeychainStore(platform=PlatformInfo("darwin", "arm64"),
                           which=lambda name: "/usr/bin/security")
    assert mac.available() is True
    assert MacKeychainStore(platform=PlatformInfo("darwin", "arm64"),
                            which=lambda name: None).available() is False
    assert MacKeychainStore(platform=PlatformInfo("linux", "x86_64"),
                            which=lambda name: "/usr/bin/security").available() is False


def test_windows_credential_manager_probe(monkeypatch):
    monkeypatch.setattr(credentials, "_windll_available", lambda: True)
    assert WindowsCredentialManagerStore(
        platform=PlatformInfo("win32", "AMD64")).available() is True
    monkeypatch.setattr(credentials, "_windll_available", lambda: False)
    assert WindowsCredentialManagerStore(
        platform=PlatformInfo("win32", "AMD64")).available() is False
    monkeypatch.setattr(credentials, "_windll_available", lambda: True)
    assert WindowsCredentialManagerStore(
        platform=PlatformInfo("linux", "x86_64")).available() is False


def test_libsecret_probe_needs_binary_and_dbus():
    environ = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
    which = lambda name: "/usr/bin/secret-tool"            # noqa: E731
    linux = PlatformInfo("linux", "x86_64")
    assert LibsecretStore(platform=linux, environ=environ, which=which).available() is True
    assert LibsecretStore(platform=linux, environ={}, which=which).available() is False
    assert LibsecretStore(platform=linux, environ=environ,
                          which=lambda name: None).available() is False
    assert LibsecretStore(platform=PlatformInfo("darwin", "arm64"), environ=environ,
                          which=which).available() is False


def test_probe_backends_lists_all_but_keeps_file_default(monkeypatch, paths):
    monkeypatch.setattr(credentials, "_windll_available", lambda: True)
    monkeypatch.setattr(credentials.MacKeychainStore, "available", lambda self: True)
    monkeypatch.setattr(credentials.WindowsCredentialManagerStore, "available",
                        lambda self: True)
    monkeypatch.setattr(credentials.LibsecretStore, "available", lambda self: True)

    probes = probe_backends(platform=PlatformInfo("linux", "x86_64"), environ={},
                            which=lambda name: "/usr/bin/" + name)
    assert probes[FILE_BACKEND] is True
    assert set(probes) == {FILE_BACKEND, "macos-keychain", "windows-credential-manager",
                           "linux-libsecret"}
    assert all(probes.values())
    # 即使 OS 存储全部「可用」，默认后端仍是文件后端（S1 行为不变）
    assert isinstance(default_backend(paths), FileBackend)


def test_os_store_describe_is_probe_only():
    store = LibsecretStore(platform=PlatformInfo("linux", "x86_64"), environ={},
                           which=lambda name: None)
    payload = store.describe()
    assert payload["status"] == "probe-only"
    assert payload["kind"] == "os"
    assert payload["name"] == "linux-libsecret"
    assert payload["available"] is False


def test_credentials_module_never_spawns_processes():
    tree = ast.parse(inspect.getsource(credentials))
    imported = {alias.name for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module or "" for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom)}
    assert "subprocess" not in imported
    assert not hasattr(credentials, "subprocess")
