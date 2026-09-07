"""Tier 2: #5896 stage ③ (owner-hit P0) —
``reyn.runtime.services.history_body_migration.migrate_inline_history_bodies``,
the offline operator command that moves an already-written
``history.jsonl`` row's inline tool-result body out to a
``history-content/`` file.

Real ``MediaStore`` writes (a real ``save_tool_result`` call, via the
production-shaped ``save_fn`` closure the CLI itself uses), real
``history.jsonl`` files under ``tmp_path`` — no fakes, matching
``test_5366_cross_session_eviction_driver.py``'s own idiom for this
subsystem.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
from reyn.runtime.chat_message import (
    CONTENT_REF_META_KEY,
    SPILL_TARGET_CONTENT_HASH_META_KEY,
    SPILLED_META_KEY,
)
from reyn.runtime.services.history_body_migration import (
    migrate_inline_history_bodies,
)


def _real_save_fn(tmp_path: Path, *, agent_name: str = "alice") -> "Callable[[str], str]":
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name=agent_name,
        session_id="storage-migrate",
    )

    def _save(content: str) -> str:
        return store.save_tool_result(content, spilled=False, tool="test")["path"]

    return _save


def _write_history(tmp_path: Path, lines: "list[dict]") -> Path:
    hist_dir = tmp_path / ".reyn" / "agents" / "alice"
    hist_dir.mkdir(parents=True, exist_ok=True)
    hist_path = hist_dir / "history.jsonl"
    with hist_path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return hist_path


def _tool_row(seq: int, content: str, meta: "dict | None" = None) -> dict:
    return {
        "role": "tool", "content": content, "ts": "", "seq": seq,
        "meta": meta or {}, "tool_calls": None, "tool_call_id": f"c{seq}",
        "name": "t", "spillability": "last_resort", "disclosure": None,
    }


# ── 1. fresh write, never spilled before ────────────────────────────────


def test_migrates_a_large_inline_row_to_a_fresh_ref(tmp_path: Path) -> None:
    """Tier 2: a large, never-spilled inline row is rewritten to the
    exact stage-① un-spilled shape (content="", meta.content_ref set,
    no meta.spilled) — read back through the SAME resolver every other
    un-spilled row already goes through, not a new code path.

    Strip witness: passing a save_fn that returns a fixed dummy ref
    without actually being called (verified via the sibling
    "never re-derives from something already in-memory" test below) —
    the real proof here is the row's own rewritten shape."""
    big = "x" * 2_000_000  # 2 MB, over the 1 MiB default floor
    hist_path = _write_history(tmp_path, [
        _tool_row(1, "small, stays inline"),
        _tool_row(2, big),
    ])
    save_fn = _real_save_fn(tmp_path)

    result = migrate_inline_history_bodies(hist_path, save_fn=save_fn)

    assert result == {"migrated": 1, "reused_ref": 0, "bytes_written": len(big.encode("utf-8"))}

    lines = [json.loads(ln) for ln in hist_path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["content"] == "small, stays inline", "the small row must be untouched"
    assert lines[1]["content"] == "", "the migrated row's body must be gone from the durable line"
    ref = lines[1]["meta"][CONTENT_REF_META_KEY]
    assert ref, "the migrated row must carry a content_ref"
    assert not lines[1]["meta"].get(SPILLED_META_KEY), (
        "a freshly-migrated row must use the un-spilled shape (stage ①'s own precedent)"
    )
    written = (tmp_path / ref).read_text(encoding="utf-8")
    assert written == big, "the written file must hold the EXACT original content"


def test_migration_never_reads_the_whole_file_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: lead-coder BLOCKING, PR #5947 — a real ``history.jsonl``
    from the owner-hit incident this module exists for runs to hundreds
    of MB with a single row past 300 MB. Pulling the whole file into one
    ``str`` (``Path.read_text()``/``readlines()``) — even once, let alone
    twice for a two-pass design — defeats the entire point of a tool
    meant to run on a host already near its RAM ceiling; an earlier
    version of this function did exactly that (``read_text().
    splitlines()`` plus a persisted ``parsed`` list holding every row's
    own body a second time) and was caught by this exact review.

    Strip witness: reverting either pass back to
    ``path.read_text().splitlines()`` makes ``Path.read_text`` fire on
    this test's own ``hist_path`` — verified directly, restored after."""
    import pathlib

    real_read_text = pathlib.Path.read_text
    calls_on_target: "list[Path]" = []

    def _wrapped(self: Path, *args: object, **kwargs: object) -> str:
        if self == hist_path:
            calls_on_target.append(self)
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    big = "s" * 2_000_000
    hist_path = _write_history(tmp_path, [_tool_row(1, big)])
    monkeypatch.setattr(pathlib.Path, "read_text", _wrapped)

    migrate_inline_history_bodies(hist_path, save_fn=_real_save_fn(tmp_path))

    assert calls_on_target == [], (
        "migrate_inline_history_bodies must never call Path.read_text() "
        "on the history.jsonl file itself (a whole-file read) — it must "
        "stream line by line via Path.open() instead"
    )


def test_leaves_a_bak_and_the_original_content_is_recoverable(tmp_path: Path) -> None:
    """Tier 2: the pre-migration file survives as a real, readable
    ``.bak`` — not just implicitly via git or a snapshot."""
    big = "y" * 2_000_000
    hist_path = _write_history(tmp_path, [_tool_row(1, big)])
    original_text = hist_path.read_text(encoding="utf-8")

    migrate_inline_history_bodies(hist_path, save_fn=_real_save_fn(tmp_path))

    bak_path = hist_path.with_name(hist_path.name + ".bak")
    assert bak_path.is_file(), "a .bak must exist after a migration that changed something"
    assert bak_path.read_text(encoding="utf-8") == original_text, (
        "the .bak must be byte-identical to the PRE-migration file"
    )


def test_a_second_run_refuses_when_a_bak_already_exists(tmp_path: Path) -> None:
    """Tier 2: interrupted-run safety — a leftover ``.bak`` from an
    earlier run must never be silently overwritten. Strip witness:
    removing the ``bak_path.exists()`` guard would let this second call
    proceed and clobber the first run's own backup — verified directly,
    restored after."""
    big = "z" * 2_000_000
    hist_path = _write_history(tmp_path, [_tool_row(1, big)])
    migrate_inline_history_bodies(hist_path, save_fn=_real_save_fn(tmp_path))

    # A second, fresh inline row appears (as if the file were rewritten
    # by something else in between) — the .bak from the FIRST run is
    # still on disk, so this call must refuse before touching anything.
    hist_path.write_text(
        hist_path.read_text(encoding="utf-8")
        + json.dumps(_tool_row(2, "w" * 2_000_000), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    before = hist_path.read_text(encoding="utf-8")

    with pytest.raises(FileExistsError):
        migrate_inline_history_bodies(hist_path, save_fn=_real_save_fn(tmp_path))

    assert hist_path.read_text(encoding="utf-8") == before, (
        "a refused run must not have touched history.jsonl at all"
    )


# ── 2. reuse an existing spill_record's ref — zero new bytes ────────────


def test_reuses_an_existing_spill_records_ref_instead_of_writing_again(
    tmp_path: Path,
) -> None:
    """Tier 2: when a PRIOR reactive spill already durably wrote this
    row's exact content (a spill_record row naming it by content hash),
    migration reuses that existing ref — zero new bytes written, not a
    second copy of the same body.

    Strip witness: dropping the ``existing_refs.get(content_hash)``
    check (always writing fresh) would make ``bytes_written`` and
    ``reused_ref`` wrong here — verified directly, restored after."""
    import hashlib

    save_fn = _real_save_fn(tmp_path)
    big = "already spilled body " * 100_000  # comfortably over 1 MiB
    # Simulate a prior reactive spill: the body already lives in
    # history-content/ (written via the SAME save_fn a real spill uses),
    # and a spill_record row already names it by content hash.
    existing_ref = save_fn(big)
    content_hash = "sha256:" + hashlib.sha256(big.encode("utf-8")).hexdigest()

    hist_path = _write_history(tmp_path, [
        {
            "role": "spill_record", "content": "preview text", "ts": "", "seq": 2,
            "meta": {
                SPILLED_META_KEY: True,
                CONTENT_REF_META_KEY: existing_ref,
                SPILL_TARGET_CONTENT_HASH_META_KEY: content_hash,
            },
            "tool_calls": None, "tool_call_id": None, "name": None,
            "spillability": "never", "disclosure": None,
        },
        # The ORIGINAL row still carries its full inline body on disk —
        # a reactive spill never rewrites it (see module docstring).
        _tool_row(1, big),
    ])

    def _fail_if_called(content: str) -> str:
        raise AssertionError("save_fn must not be called when an existing ref can be reused")

    result = migrate_inline_history_bodies(hist_path, save_fn=_fail_if_called)

    assert result == {"migrated": 1, "reused_ref": 1, "bytes_written": 0}
    lines = [json.loads(ln) for ln in hist_path.read_text(encoding="utf-8").splitlines()]
    migrated_row = next(ln for ln in lines if ln.get("seq") == 1)
    assert migrated_row["meta"][CONTENT_REF_META_KEY] == existing_ref, (
        "the migrated row must point at the SAME file the spill already wrote, not a new one"
    )


# ── 3. min_bytes floor, non-candidates, no-op cases ─────────────────────


def test_rows_under_min_bytes_are_never_migrated(tmp_path: Path) -> None:
    """Tier 2: accept-side — a row under the floor is left byte-identical."""
    hist_path = _write_history(tmp_path, [_tool_row(1, "short")])
    original = hist_path.read_text(encoding="utf-8")

    result = migrate_inline_history_bodies(
        hist_path, save_fn=_real_save_fn(tmp_path), min_bytes=1024,
    )

    assert result == {"migrated": 0, "reused_ref": 0, "bytes_written": 0}
    assert hist_path.read_text(encoding="utf-8") == original
    assert not hist_path.with_name("history.jsonl.bak").exists(), (
        "a no-op run must not create a .bak — nothing changed"
    )


def test_a_row_that_already_has_a_content_ref_is_never_re_migrated(
    tmp_path: Path,
) -> None:
    """Tier 2: idempotency — a row stage ① already migrated (or a prior
    run of THIS command already migrated) is not a candidate again, even
    if it happens to be huge."""
    row = _tool_row(1, "", meta={CONTENT_REF_META_KEY: "already/somewhere.txt"})
    hist_path = _write_history(tmp_path, [row])
    original = hist_path.read_text(encoding="utf-8")

    result = migrate_inline_history_bodies(hist_path, save_fn=_real_save_fn(tmp_path))

    assert result == {"migrated": 0, "reused_ref": 0, "bytes_written": 0}
    assert hist_path.read_text(encoding="utf-8") == original


def test_non_tool_roles_are_never_candidates(tmp_path: Path) -> None:
    """Tier 2: accept-side — a large user/assistant row is untouched;
    only role="tool" rows are ever migration candidates (matching stage
    ①'s own scope)."""
    big = "u" * 2_000_000
    row = {
        "role": "user", "content": big, "ts": "", "seq": 1, "meta": {},
        "tool_calls": None, "tool_call_id": None, "name": None,
        "spillability": "last_resort", "disclosure": None,
    }
    hist_path = _write_history(tmp_path, [row])
    original = hist_path.read_text(encoding="utf-8")

    result = migrate_inline_history_bodies(hist_path, save_fn=_real_save_fn(tmp_path))

    assert result == {"migrated": 0, "reused_ref": 0, "bytes_written": 0}
    assert hist_path.read_text(encoding="utf-8") == original


def test_a_missing_file_is_a_harmless_no_op(tmp_path: Path) -> None:
    """Tier 2: accept-side — no file, nothing to migrate, no error."""
    missing = tmp_path / "does" / "not" / "exist" / "history.jsonl"
    result = migrate_inline_history_bodies(missing, save_fn=_real_save_fn(tmp_path))
    assert result == {"migrated": 0, "reused_ref": 0, "bytes_written": 0}


# ── 4. build_history-equivalent readback: the resolver round-trips ──────


def test_migrated_row_reads_back_to_the_exact_original_content(
    tmp_path: Path,
) -> None:
    """Tier 2: the strongest witness — reading a migrated row back
    through the SAME resolver production uses
    (``reyn.runtime.services.router_history_buffer.resolve_history_content``
    — the exact wrapper ``Session._parse_history_line`` calls in
    production) returns the EXACT pre-migration content, proving
    migration is a storage-location change only, never a content change
    (mirrors #5896 stage ①'s own "build_history output is byte-identical"
    witness)."""
    from reyn.runtime.services.router_history_buffer import resolve_history_content

    big = "round-trip me exactly, including \n newlines and 日本語" * 50_000
    hist_path = _write_history(tmp_path, [_tool_row(1, big)])
    save_fn = _real_save_fn(tmp_path)

    migrate_inline_history_bodies(hist_path, save_fn=save_fn)

    lines = [json.loads(ln) for ln in hist_path.read_text(encoding="utf-8").splitlines()]
    meta = lines[0]["meta"]

    resolved = resolve_history_content(
        lines[0]["content"], meta, lambda: tmp_path, None, None,
        read_text=lambda ref: (tmp_path / ref).read_text(encoding="utf-8"),
    )
    assert resolved == big, "the resolved content must be byte-identical to the pre-migration body"
