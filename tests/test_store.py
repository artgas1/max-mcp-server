from __future__ import annotations

import os
import stat

import pytest

from max_mcp_server.errors import InsecurePermissions, ProfileLocked
from max_mcp_server.session.store import ProfileLock, SecureFileStore


def mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_write_creates_private_dir_and_file(tmp_path):
    store = SecureFileStore(tmp_path / "a" / "b")
    old = os.umask(0o022)
    try:
        store.write({"client_kind": "web", "session": {"token": "T"}})
    finally:
        os.umask(old)
    assert mode(store.dir) == 0o700 and mode(store.path) == 0o600
    assert store.read() == {"version": 1, "client_kind": "web", "session": {"token": "T"}}
    assert not [p for p in store.dir.iterdir() if p.name.endswith(".tmp")]


def test_insecure_permissions_refused_and_fixable(tmp_path):
    store = SecureFileStore(tmp_path / "s")
    store.write({"x": 1})
    os.chmod(store.path, 0o644)
    with pytest.raises(InsecurePermissions):
        store.read()
    store.check(fix=True)
    assert mode(store.path) == 0o600
    os.chmod(store.dir, 0o755)
    with pytest.raises(InsecurePermissions):
        store.check()
    store.check(fix=True)
    assert mode(store.dir) == 0o700


def test_symlink_refused(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}")
    os.chmod(real, 0o600)
    d = tmp_path / "s"
    d.mkdir(mode=0o700)
    (d / "session.json").symlink_to(real)
    with pytest.raises(InsecurePermissions):
        SecureFileStore(d).read()
    link_dir = tmp_path / "linkdir"
    link_dir.symlink_to(d)
    with pytest.raises(InsecurePermissions):
        SecureFileStore(link_dir).check()


def test_corrupt_or_foreign_file_is_no_session(tmp_path):
    store = SecureFileStore(tmp_path / "s")
    store.write({"a": 1})
    store.path.write_bytes(b"\xff\xfe not json")
    assert store.read() is None
    store.path.write_text('{"version": 99}')
    assert store.read() is None


def test_delete(tmp_path):
    store = SecureFileStore(tmp_path / "s")
    assert store.delete() is False
    store.write({"a": 1})
    assert store.delete() is True and not store.exists()


def test_profile_lock_exclusive(tmp_path):
    a, b = ProfileLock(tmp_path / "s"), ProfileLock(tmp_path / "s")
    a.acquire()
    try:
        with pytest.raises(ProfileLocked):
            b.acquire()
    finally:
        a.release()
    with b:
        assert b.held
    assert mode(tmp_path / "s" / "serve.lock") == 0o600
