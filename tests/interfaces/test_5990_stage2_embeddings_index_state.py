"""Tier 1: #5990 (part of, stage 2) — `embeddings._read_index_state`'s own
`except (sqlite3.DatabaseError, OSError): n = 0` and
`except OSError: last_built = "(never)"` each swallowed a read failure
with no visible report -- an operator reading `reyn embeddings status`
would see "0 rows" / "(never)" with no way to tell a genuinely empty
index apart from a corrupted one. Now each warns before falling back,
same convention #6038 established.
"""
from __future__ import annotations

import logging

from reyn.interfaces.cli.commands.embeddings import _read_index_state

_LOGGER_NAME = "reyn.interfaces.cli.commands.embeddings"


def test_corrupted_index_db_warns_and_reports_zero_rows(tmp_path, caplog) -> None:
    """Tier 1: a present but corrupted `index.db` (not a valid SQLite
    file) warns AND `_read_index_state` still returns `(0, ...)` rather
    than raising.

    Strip-falsify (verified by hand: the `_log.warning(...)` call
    removed from the `sqlite3.DatabaseError, OSError` branch): this test
    goes red — no record at all."""
    (tmp_path / "index.db").write_bytes(b"not a real sqlite file")
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        n, _last_built = _read_index_state(tmp_path)
    assert n == 0
    assert any("could not be read" in r.message for r in caplog.records)


def test_absent_index_db_does_not_warn(tmp_path, caplog) -> None:
    """Tier 1: negative control -- no `index.db` at all is the documented
    `(0, "(never)")` case and must not warn (that path returns before
    ever reaching either `try`)."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        n, last_built = _read_index_state(tmp_path)
    assert (n, last_built) == (0, "(never)")
    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []


def test_mtime_read_failure_warns_and_reports_never_built(tmp_path, monkeypatch, caplog) -> None:
    """Tier 1: `index.db`'s own mtime read failing (an OS-level race
    between the earlier `.exists()` check and this `.stat()` call) warns
    AND `_read_index_state` still returns `"(never)"` for `last_built`
    rather than raising.

    Strip-falsify (verified by hand: the `_log.warning(...)` call
    removed from the second `except OSError`): this test goes red — no
    record at all."""
    import pathlib
    import sqlite3

    db_path = tmp_path / "index.db"
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY)")
        con.commit()
    finally:
        con.close()

    real_stat = pathlib.Path.stat
    calls_on_db_path = 0

    def _stat(self, *args, **kwargs):
        # `Path.exists()` calls `self.stat(...)` internally too -- on
        # Python 3.11 with NO `follow_symlinks` kwarg at all (that
        # parameter was only added in 3.12), so filtering by "no kwargs"
        # (an earlier version of this test did that, and it broke under
        # 3.11 in CI: `.exists()`'s own stat call has no kwargs there
        # either, so it got intercepted too, and `.exists()` re-raised
        # before `_read_index_state` ever reached either `try` block --
        # a Python-version-dependent test bug, not a production one).
        # Order-based instead, version-independent: the FIRST stat() on
        # `db_path` is always `.exists()`'s own (called once, before any
        # try block); only the SECOND ONWARD is the mtime read this test
        # targets.
        nonlocal calls_on_db_path
        if self == db_path:
            calls_on_db_path += 1
            if calls_on_db_path > 1:
                raise OSError("simulated stat failure")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", _stat)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _n, last_built = _read_index_state(tmp_path)
    assert last_built == "(never)"
    assert any("mtime could not be read" in r.message for r in caplog.records)


def test_well_formed_index_db_does_not_warn(tmp_path, caplog) -> None:
    """Tier 1: negative control -- a real, readable SQLite file with a
    `chunks` table parses through with no warning at all."""
    import sqlite3

    db_path = tmp_path / "index.db"
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY)")
        con.execute("INSERT INTO chunks DEFAULT VALUES")
        con.commit()
    finally:
        con.close()

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        n, last_built = _read_index_state(tmp_path)
    assert n == 1
    assert last_built != "(never)"
    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []
