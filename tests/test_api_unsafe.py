"""Tier 2: reyn.api.unsafe wrapper behaviour.

These tests run the wrappers as plain Python imports — they do not
spin up the python step subprocess harness (= those would be
integration tests).
"""

from __future__ import annotations

import os
import sys

import pytest

from reyn.api.unsafe import env as unsafe_env
from reyn.api.unsafe import file as unsafe_file
from reyn.api.unsafe import shell as unsafe_shell
from reyn.api.unsafe import workspace as unsafe_workspace


# -- file --------------------------------------------------------------


def test_file_write_then_read(tmp_path) -> None:
    """Tier 2: write + read round-trip preserves content."""
    p = tmp_path / "hello.txt"
    unsafe_file.write(str(p), "héllo\nworld")
    assert unsafe_file.read(str(p)) == "héllo\nworld"


def test_file_exists_and_delete(tmp_path) -> None:
    """Tier 2: exists reflects filesystem state across delete."""
    p = tmp_path / "scratch.txt"
    unsafe_file.write(str(p), "x")
    assert unsafe_file.exists(str(p))
    unsafe_file.delete(str(p))
    assert not unsafe_file.exists(str(p))


def test_file_glob_recursive(tmp_path) -> None:
    """Tier 2: glob with ** finds nested files."""
    (tmp_path / "sub").mkdir()
    unsafe_file.write(str(tmp_path / "a.txt"), "")
    unsafe_file.write(str(tmp_path / "sub" / "b.txt"), "")
    matches = unsafe_file.glob(str(tmp_path / "**" / "*.txt"))
    assert any(m.endswith("a.txt") for m in matches)
    assert any(m.endswith("b.txt") for m in matches)


def test_file_stat_returns_size_mtime_mode(tmp_path) -> None:
    """Tier 2: stat returns the expected keys with sane types."""
    p = tmp_path / "s.txt"
    unsafe_file.write(str(p), "abc")
    st = unsafe_file.stat(str(p))
    assert st["size"] == 3
    assert isinstance(st["mtime"], float)
    assert isinstance(st["mode"], int)


# -- http (no network — only verify wiring) ----------------------------


def test_http_get_returns_envelope_on_invalid_url() -> None:
    """Tier 2: get raises a URLError for an unresolvable host (= wraps urllib)."""
    # We don't want to hit the network. Use an obviously invalid URL
    # to confirm the function reaches urllib (= raises URLError),
    # rather than returning an envelope built by us.
    from urllib.error import URLError

    with pytest.raises(URLError):
        unsafe_http_get("http://invalid.invalid.localdomain.test/")


def unsafe_http_get(url: str):
    from reyn.api.unsafe import http as unsafe_http

    return unsafe_http.get(url, timeout=1)


# -- shell -------------------------------------------------------------


def test_shell_run_happy() -> None:
    """Tier 2: run captures stdout/stderr and returncode."""
    out = unsafe_shell.run([sys.executable, "-c", "print('hi')"])
    assert out["returncode"] == 0
    assert "hi" in out["stdout"]
    assert out["stderr"] == ""


def test_shell_run_nonzero_returncode() -> None:
    """Tier 2: nonzero exit propagates via returncode (no raise)."""
    out = unsafe_shell.run([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert out["returncode"] == 3


# -- workspace ---------------------------------------------------------


def test_workspace_cwd_and_path(tmp_path, monkeypatch) -> None:
    """Tier 2: cwd reflects chdir; path joins onto cwd."""
    monkeypatch.chdir(tmp_path)
    assert os.path.samefile(unsafe_workspace.cwd(), str(tmp_path))
    joined = unsafe_workspace.path("sub", "file.txt")
    assert joined.endswith(os.path.join("sub", "file.txt"))


def test_workspace_list_artifacts(tmp_path, monkeypatch) -> None:
    """Tier 2: list_artifacts returns entries in cwd."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "alpha").write_text("")
    (tmp_path / "beta").mkdir()
    entries = set(unsafe_workspace.list_artifacts())
    assert {"alpha", "beta"} <= entries


# -- env ---------------------------------------------------------------


def test_env_get_present(monkeypatch) -> None:
    """Tier 2: env.get returns the value when the key is set."""
    monkeypatch.setenv("REYN_API_TEST_KEY", "value-1")
    assert unsafe_env.get("REYN_API_TEST_KEY") == "value-1"


def test_env_get_default(monkeypatch) -> None:
    """Tier 2: env.get returns default when key is absent."""
    monkeypatch.delenv("REYN_API_TEST_MISSING", raising=False)
    assert unsafe_env.get("REYN_API_TEST_MISSING", default="fallback") == "fallback"
