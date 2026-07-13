"""Tier 1/2: the runtime ``canonical_degraded`` invariant (FP-0056 v2 piece #2).

The tool-result canonical mapper has a silent-loss mode M2: a *success*-mapper treats a non-trivial
result as success but emits an EMPTY canonical view — no text, no attachments — so the user/LLM sees
nothing. Piece #2 is the cheapest runtime safety net for M2 + any unknown future mapper bug: a pure
helper :func:`canonical_degraded_reason`, consulted at the two ``to_canonical`` call sites, fires a
``canonical_degraded`` P6 audit event (+ a warn log) when a NON-error result canonicalizes to
text-empty AND attachments-empty.

The nuance is the false-positive guard: a *genuinely*-empty success (an empty file read, a no-output
command, a 0-match grep/glob) must NOT fire. Those mappers were fixed to render an EXPLICIT empty
marker (``(empty file)`` / ``(no output)`` / ``(no matches)`` …) — better LLM UX and non-empty text,
so the invariant stays silent on them. A ``data: []`` structured attachment is likewise an explicit
empty the LLM sees (attachments non-empty) → does NOT fire.

The event is an AUDIT / P6 event — NOT a WAL / recovery-core event (no truncate-falsify obligation).

Covered here with real instances (no mocks — a real ``PipelineExecutor``, a real ``EventLog``, the real
registration seam, real mapper functions):

- Tier 1: :func:`canonical_degraded_reason` fires on a non-error empty-empty view, stays ``None`` on a
  normal non-empty result, on each legit-empty-now-explicit mapper, on a ``data: []`` attachment, and
  on any error-classified result.
- Tier 2: the pipeline tool-step chokepoint EMITS ``canonical_degraded`` (source id only, no body) and
  logs a warn when a MAPPED producer canonicalizes to an empty view; a producer whose result carries
  content does NOT emit it (falsify control).
"""
from __future__ import annotations

import json
import logging

import pytest

# Importing the op-runtime package + building the default registry populates every canonical
# declaration (identically to the coverage-gate / fallback-event tests).
import reyn.core.op_runtime as _op_runtime  # noqa: F401
from reyn.core.events.events import EventLog
from reyn.core.offload.canonical import (
    CANONICAL_DEGRADED_EVENT,
    CanonicalToolResult,
    canonical_degraded_reason,
    chunks_to_canonical,
    declare_canonical,
    file_to_canonical,
    reyn_repo_to_canonical,
    sandboxed_exec_to_canonical,
    web_search_to_canonical,
)
from reyn.core.pipeline.executor import Pipeline, PipelineExecutor, ToolStep
from reyn.tools import get_default_registry

get_default_registry()


# A module-level mapper that deliberately emits an EMPTY canonical view for a NON-error success — the
# M2 shape the invariant exists to catch. Declared ONCE at import (stable identity → declare_canonical
# is idempotent for the identical re-declaration a per-call lambda would violate). A real production
# mapper never does this; this stands in for a buggy / not-yet-fixed one.
def _deliberately_empty_mapper(result: dict) -> CanonicalToolResult:
    return CanonicalToolResult(text="", attachments=[], source_ref=None, meta={})


_EMPTY_PRODUCER_ID = "test_deliberately_empty_producer"
declare_canonical(_EMPTY_PRODUCER_ID, _deliberately_empty_mapper)


def test_degraded_reason_fires_on_nonerror_empty_view() -> None:
    """Tier 1: a NON-error result whose canonical view is text-empty AND attachments-empty is the M2
    silent-loss shape → :func:`canonical_degraded_reason` returns a reason (the event fires). Whitespace
    is stripped, so a whitespace-only body counts as empty."""
    result = {"kind": "some_mapped_op", "status": "ok"}
    empty = CanonicalToolResult(text="", attachments=[], source_ref=None, meta={})
    assert canonical_degraded_reason(result, empty) is not None

    whitespace = CanonicalToolResult(text="   \n\t", attachments=[], source_ref=None, meta={})
    assert canonical_degraded_reason(result, whitespace) is not None


def test_degraded_reason_none_for_normal_nonempty_result() -> None:
    """Tier 1: FALSIFY — a normal result with a non-empty text body does NOT fire (no false positive)."""
    result = {"kind": "some_mapped_op", "status": "ok"}
    canonical = CanonicalToolResult(text="the answer", attachments=[], source_ref=None, meta={})
    assert canonical_degraded_reason(result, canonical) is None


def test_degraded_reason_none_for_error_classified_result() -> None:
    """Tier 1: an error-classified result never fires — neither via ``_is_error`` (``status==error`` /
    ``isError``) nor via the canonical ``meta.isError`` a broader per-mapper error check set. An error
    may legitimately carry a terse/empty message; it is not a silent SUCCESS loss."""
    empty = CanonicalToolResult(text="", attachments=[], source_ref=None, meta={})
    # status==error → _is_error True → exempt.
    assert canonical_degraded_reason({"status": "error"}, empty) is None
    # isError flag → _is_error True → exempt.
    assert canonical_degraded_reason({"isError": True}, empty) is None
    # meta.isError set by a broader per-mapper error check (file denied/not_found, error field) → exempt
    # even when the raw result dict itself does not trip _is_error.
    meta_err = CanonicalToolResult(text="", attachments=[], source_ref=None, meta={"isError": True})
    assert canonical_degraded_reason({"kind": "file", "op": "read"}, meta_err) is None


def test_degraded_reason_none_for_data_empty_attachment() -> None:
    """Tier 1: a ``data: []`` structured attachment is an EXPLICIT empty the LLM sees → attachments is
    non-empty → does NOT fire. There is deliberately NO 'trivial attachment' check. Verified through the
    real ``web_search`` (empty results) and ``semantic_search`` (empty chunks) mappers."""
    ws = web_search_to_canonical({"kind": "web_search", "status": "ok", "results": []})
    assert ws["text"] == "" and ws["attachments"] == [{"kind": "structured", "data": []}]
    assert canonical_degraded_reason({"kind": "web_search", "status": "ok", "results": []}, ws) is None

    chunks = chunks_to_canonical({"kind": "semantic_search", "chunks": []})
    assert chunks["attachments"] == [{"kind": "structured", "data": []}]
    assert canonical_degraded_reason({"kind": "semantic_search", "chunks": []}, chunks) is None


def test_degraded_reason_none_for_legit_empty_now_explicit_mappers() -> None:
    """Tier 1: the false-positive guard — each mapper that can legitimately produce no output now
    renders an EXPLICIT empty marker (non-empty text), so a genuine empty-success does NOT fire.
    Enumerated legit-empty cases fixed for piece #2:

    - ``file`` read of an empty file → ``(empty file)``.
    - ``file`` grep / glob with 0 matches → ``(no matches)``.
    - ``sandboxed_exec`` with no stdout/stderr → ``(no output)``.
    - ``reyn_repo`` read of an empty file → ``(empty file)``."""
    # file read, empty file (no media blocks) → explicit marker, non-empty text.
    read_empty = {"kind": "file", "op": "read", "status": "ok", "path": "empty.txt", "content": ""}
    c = file_to_canonical(read_empty)
    assert c["text"] == "(empty file)"
    assert canonical_degraded_reason(read_empty, c) is None

    # file grep, 0 matches (content mode) → "(no matches)".
    grep0 = {"kind": "file", "op": "grep", "status": "ok", "output_mode": "content", "matches": []}
    cg = file_to_canonical(grep0)
    assert cg["text"] == "(no matches)"
    assert canonical_degraded_reason(grep0, cg) is None

    # file glob, 0 matches → "(no matches)".
    glob0 = {"kind": "file", "op": "glob", "status": "ok", "matches": []}
    cgl = file_to_canonical(glob0)
    assert cgl["text"] == "(no matches)"
    assert canonical_degraded_reason(glob0, cgl) is None

    # sandboxed_exec, no output (returncode 0) → "(no output)".
    exec0 = {"kind": "sandboxed_exec", "status": "ok", "returncode": 0, "stdout": "", "stderr": ""}
    ce = sandboxed_exec_to_canonical(exec0)
    assert ce["text"] == "(no output)"
    assert canonical_degraded_reason(exec0, ce) is None

    # reyn_repo read, empty file → "(empty file)".
    rs_empty = {"content": "", "path": "docs/empty.md"}
    cr = reyn_repo_to_canonical(rs_empty)
    assert cr["text"] == "(empty file)"
    assert canonical_degraded_reason(rs_empty, cr) is None


@pytest.mark.asyncio
async def test_empty_mapper_at_call_site_emits_degraded_event_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: a pipeline tool step whose MAPPED producer canonicalizes to an empty view emits
    ``canonical_degraded`` naming the source (source id only — no result body leaks) AND logs a warn.
    Real ``PipelineExecutor`` + real ``EventLog`` + the real registration seam (no mocks)."""
    secret_body = "SECRET-CONTENT-that-the-buggy-mapper-dropped-should-never-reach-an-audit-event"

    def _dispatch(name: str, args: dict) -> dict:
        # A result tagged with the deliberately-empty mapper's identity: the mapper drops the body,
        # producing the empty-empty canonical view (the M2 shape). The distinctive string must never
        # leak into any event payload.
        return {
            "kind": "some_kind",
            "content": secret_body,
            "_canonical_source": _EMPTY_PRODUCER_ID,
        }

    events = EventLog()
    pipeline = Pipeline(steps=[ToolStep(name="buggy_tool", args={}, output="r")])
    with caplog.at_level(logging.WARNING, logger="reyn.core.pipeline.executor"):
        await PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=_dispatch, state_log=None, run_id="run-fp0056-degraded", events=events,
        )

    # Exactly one degraded event for the one empty-mapping tool step (unpack asserts the count).
    [degraded] = [e for e in events.all() if e.type == CANONICAL_DEGRADED_EVENT]
    assert degraded.data["source"] == _EMPTY_PRODUCER_ID
    assert degraded.data["reason"]  # a non-empty reason category

    # No result content bytes in ANY event payload — the event carries the source identity only.
    all_payloads = json.dumps([e.data for e in events.all()])
    assert secret_body not in all_payloads
    assert "content" not in degraded.data

    # A warn was logged naming the degrade (degrade-with-audit: audit event + operator-visible warn).
    warn_records = [r for r in caplog.records if "canonical_degraded" in r.getMessage()]
    assert warn_records, "a canonical_degraded warn must be logged"
    assert secret_body not in "\n".join(r.getMessage() for r in warn_records)


@pytest.mark.asyncio
async def test_mapped_producer_with_content_does_not_emit_degraded_event() -> None:
    """Tier 2: FALSIFY — a producer whose real mapper surfaces content (``sandboxed_exec`` with real
    stdout) canonicalizes to a non-empty view → NO ``canonical_degraded`` event fires."""

    def _dispatch(name: str, args: dict) -> dict:
        return {
            "kind": "sandboxed_exec",
            "stdout": "real output",
            "returncode": 0,
            "_canonical_source": "sandboxed_exec",
        }

    events = EventLog()
    pipeline = Pipeline(steps=[ToolStep(name="run_shell", args={}, output="r")])
    await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-fp0056-degraded-ok", events=events,
    )

    assert not [e for e in events.all() if e.type == CANONICAL_DEGRADED_EVENT], (
        "a producer that surfaced content must not emit the canonical_degraded audit event"
    )
