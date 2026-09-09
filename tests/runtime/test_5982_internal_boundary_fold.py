"""Tier 2: #5982 -- an internal (non-HTTP) reader of `MediaStore`'s
tool-result content must not be stopped by an out-of-boundary `ref`
(`PermissionError`), and the fold must not erase the distinction between
"file genuinely missing" (normal) and "ref resolves outside the storage
boundary" (abnormal -- something constructed an invalid ref).

Real `MediaStore` throughout (no fakes) -- a REAL file written OUTSIDE
both `history_content_root`/`tool_results_dir` so `read_tool_result`'s
own boundary check genuinely raises `PermissionError`, the same as
production would for a malformed/malicious ref. `resolve_history_content`
is exercised directly (the same minimal, real-`read_text`-closure
pattern `tests/runtime/test_5896_stage3_migrate_bodies.py` already
established for this exact function), not through a full session --
the session-level wiring (which refs reach here at all) is out of this
file's scope.

`resources.py` (the ONE external-boundary caller, which must keep
raising) is untouched by this PR and out of this file's scope --
`tests/interfaces/web` already covers its 400 response.
"""
from __future__ import annotations

from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
from reyn.runtime.chat_message import CONTENT_REF_META_KEY, SPILLED_META_KEY, LostReason
from reyn.runtime.services.router_history_buffer import resolve_history_content
from tests._support.events import collect_events


def _store(tmp_path: Path) -> MediaStore:
    return MediaStore(
        MediaStoreConfig(), project_root=tmp_path,
        agent_name="boundary-agent", session_id="s1",
    )


def _outside_boundary_ref(tmp_path: Path, *, body: str = "real body text") -> str:
    """A ref that is a REAL, readable file on disk, project-relative, but
    NOT under either of `MediaStore`'s two valid roots (both live under
    `.reyn/` per `media_store.py`'s own construction) -- `read_tool_
    result` genuinely raises `PermissionError` for this path, the same
    as it would for any malformed/malicious ref an internal reader was
    handed."""
    outside = tmp_path / "definitely-outside-the-boundary.txt"
    outside.write_text(body, encoding="utf-8")
    return "definitely-outside-the-boundary.txt"


# ─── resolve_history_content (session.py:4733 / router_history_buffer.py's
# own read_text closures both funnel through this one function) ──────────


def test_a_boundary_violating_ref_folds_to_lost_with_a_distinct_reason(
    tmp_path: Path,
) -> None:
    """Tier 2: accept -- an un-spilled entry (empty content, a ref) whose
    ref resolves OUTSIDE MediaStore's own boundary no longer raises
    PermissionError uncaught: resolve_history_content catches it and
    returns the SAME "lost" shape a missing file gets, but with
    LostReason.OUTSIDE_BOUNDARY (never GC/EXTERNAL -- see that reason's
    own docstring for why conflating them would bury the abnormal case),
    and DISTINCT wording (never claims the file "no longer exists" --
    it may well still be sitting right there, just outside the
    boundary)."""
    store = _store(tmp_path)
    events = EventLog()
    collected = collect_events(events)
    ref = _outside_boundary_ref(tmp_path)

    result = resolve_history_content(
        "", {CONTENT_REF_META_KEY: ref, SPILLED_META_KEY: False},
        lambda: tmp_path, events, set(),
        read_text=lambda r: store.read_tool_result(r)[0],
    )

    assert f"(reason: {LostReason.OUTSIDE_BOUNDARY})" in result
    assert "no longer exists" not in result
    assert "deleted or garbage-collected" not in result

    (unavailable,) = [e for e in collected if e.type == "offloaded_content_unavailable"]
    assert unavailable.data["reason"] == str(LostReason.OUTSIDE_BOUNDARY)
    assert unavailable.data["ref"] == ref


def test_a_genuinely_missing_file_still_says_gc_not_outside_boundary(
    tmp_path: Path,
) -> None:
    """Tier 2: deny sibling -- an un-spilled entry whose ref names a file
    that is simply ABSENT (never reaches read_text at all -- resolve()'s
    own file_exists check returns False first) is UNAFFECTED by this fix:
    still `gc`, still the ordinary "no longer exists" wording. Proves the
    new fold is additive, not a behavior change for the case it does not
    apply to."""
    events = EventLog()
    collected = collect_events(events)

    def _read_text_never_called(_ref: str) -> str:
        raise AssertionError(
            "read_text must not be called when file_exists already said False"
        )

    result = resolve_history_content(
        "", {CONTENT_REF_META_KEY: "genuinely-missing.txt", SPILLED_META_KEY: False},
        lambda: tmp_path, events, set(),
        read_text=_read_text_never_called,
    )

    # Un-spilled + missing derives `external` (`resolve_history_content`'s
    # own existing branch: `elif spilled: GC else: EXTERNAL`) -- `gc` is
    # the SPILLED sibling's reason, not this cell's; either way, neither
    # is `outside_boundary`, which is this test's own point.
    assert "(reason: external)" in result
    assert str(LostReason.OUTSIDE_BOUNDARY) not in result
    assert "no longer exists" in result

    (unavailable,) = [e for e in collected if e.type == "offloaded_content_unavailable"]
    assert unavailable.data["reason"] == "external"


def test_a_present_in_boundary_file_reads_normally_unaffected(tmp_path: Path) -> None:
    """Tier 2: FP gate -- an ordinary, in-boundary ref with a real backing
    file reads its actual body through resolve_history_content exactly
    as before this fix, no placeholder, no event."""
    store = _store(tmp_path)
    events = EventLog()
    collected = collect_events(events)
    ref_block = store.save_tool_result("the real body", tool="t", seq=1)

    result = resolve_history_content(
        "", {CONTENT_REF_META_KEY: ref_block["path"], SPILLED_META_KEY: False},
        lambda: tmp_path, events, set(),
        read_text=lambda r: store.read_tool_result(r)[0],
    )

    assert result == "the real body"
    assert not [e for e in collected if e.type == "offloaded_content_unavailable"]


# ─── read_tool_result_preview_for_internal_reader (router_history_buffer.py's
# bounded_content_preview / chat_message.py's _derive_body_bytes) ─────────


def test_preview_for_internal_reader_folds_a_boundary_violation_and_warns(
    tmp_path: Path, caplog,
) -> None:
    """Tier 2: accept -- the internal-reader preview entrypoint returns
    found=False (never raises) for a ref outside the boundary, AND logs
    a WARNING distinct from an ordinary not-found (asserted in the
    sibling FP test below) -- #5982's own condition that folding must
    not make the defect (an invalid ref reaching an internal reader)
    permanently unobservable."""
    import logging

    store = _store(tmp_path)
    ref = _outside_boundary_ref(tmp_path)

    with caplog.at_level(logging.WARNING, logger="reyn.data.workspace.media_store"):
        preview, found, total_bytes = store.read_tool_result_preview_for_internal_reader(
            ref, max_bytes=100,
        )

    assert (preview, found, total_bytes) == ("", False, 0)
    assert any(
        "outside the storage boundary" in r.message for r in caplog.records
    ), f"expected a boundary-violation WARNING, got: {[r.message for r in caplog.records]}"


def test_preview_for_internal_reader_missing_file_returns_not_found_without_warning(
    tmp_path: Path, caplog,
) -> None:
    """Tier 2: deny sibling -- a genuinely missing (but in-boundary) ref
    also returns found=False, but WITHOUT the boundary-violation
    WARNING -- proves the log is a DISTINCT record for the abnormal case,
    not merely tacked onto every not-found."""
    import logging

    store = _store(tmp_path)

    with caplog.at_level(logging.WARNING, logger="reyn.data.workspace.media_store"):
        # In-boundary (under tool_results_dir) but the file itself does
        # not exist -- a genuine not-found, distinct from the boundary
        # violation the sibling test above exercises.
        preview, found, total_bytes = store.read_tool_result_preview_for_internal_reader(
            ".reyn/tool-results/genuinely-missing.txt", max_bytes=100,
        )

    assert (preview, found, total_bytes) == ("", False, 0)
    assert not caplog.records, (
        f"an ordinary not-found must not warn — got: {[r.message for r in caplog.records]}"
    )


def test_preview_for_internal_reader_present_file_reads_normally(tmp_path: Path) -> None:
    """Tier 2: FP gate -- an ordinary, in-boundary, present ref previews
    exactly as :meth:`MediaStore.read_tool_result_preview` already does."""
    store = _store(tmp_path)
    ref_block = store.save_tool_result("hello world", tool="t", seq=1)

    preview, found, total_bytes = store.read_tool_result_preview_for_internal_reader(
        ref_block["path"], max_bytes=100,
    )

    assert found is True
    assert preview == "hello world"
    assert total_bytes == len("hello world")
