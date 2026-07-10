"""Tier 1/2: FP-0056 v2 piece #3 — the M3 inner-dispatch FAIL-VISIBLE seam.

M3 (the third canonical silent-loss mode, after M1 error-seam #2752 and M2
``canonical_degraded`` #2748): a mapper that sub-dispatches on an inner discriminator
(``file``'s ``op``, ``reyn_src``'s body key) used to fall through, on a missing/unknown
discriminator, to a status-only catch-all that emitted ``f"{op}: {status}"`` = the literal
``"None: ok"`` garbage (#2695). Non-empty, so M2's empty-check misses it; not an error, so
M1's shared seam misses it — the user/LLM got meaningless text instead of the real result.

Piece #3 makes such a discriminator-miss FAIL-VISIBLE: the mapper raises
``CanonicalDiscriminatorMiss``; :func:`to_canonical` catches it and takes the SAME lossless
whole-dict fallback a genuine unknown source takes, marked so the caller emits the EXISTING
``canonical_fallback_used`` audit-event (reason ``"discriminator_miss"``). Full dict
recoverable + audit signal, never silent garbage.

``canonical_fallback_used`` is a P6 AUDIT-event (observability), NOT WAL-derived recovery
state — the recovery-PR-gate (truncate-falsify) does not apply.

Real instances only (no mocks): the real registration seams, a real ``PipelineExecutor``, a
real ``EventLog``. Assertions are on the public canonical shape + the public classifier + the
emitted audit-event, never on private state or exact formatting.
"""
from __future__ import annotations

import json

import pytest

# Eagerly register every op handler + its canonical declaration, and build the default tool
# registry so every ToolDefinition's declaration is populated (the classifier resolves against these).
import reyn.core.op_runtime as _op_runtime  # noqa: F401
from reyn.core.events.events import EventLog
from reyn.core.offload.canonical import (
    _DECLARATIONS,
    CANONICAL_FALLBACK_EVENT,
    CanonicalDiscriminatorMiss,
    CanonicalToolResult,
    canonical_fallback_reason,
    file_to_canonical,
    reyn_src_to_canonical,
    to_canonical,
)
from reyn.core.pipeline.executor import Pipeline, PipelineExecutor, ToolStep
from reyn.tools import get_default_registry

get_default_registry()

# A distinctive status value a correct mapper must never echo into its ``text`` body.
_PROBE_STATUS = "__M3_PROBE_STATUS_SENTINEL__"


def _structured_data(canonical: CanonicalToolResult) -> object | None:
    """The whole-dict data of the sole ``structured`` attachment, or ``None``."""
    for att in canonical.get("attachments", []) or []:
        if att.get("kind") == "structured":
            return att.get("data")
    return None


# --------------------------------------------------------------------------------------------------
# #2695 acceptance — a ``file`` result with a missing/unknown ``op`` no longer renders "None: ok".
# --------------------------------------------------------------------------------------------------

def test_file_missing_op_is_fail_visible_not_none_ok_garbage() -> None:
    """Tier 1: a ``file`` result with NO ``op`` renders the whole-dict fallback (recoverable) — never
    the #2695 ``"None: ok"`` status-only garbage — and the classifier reports ``discriminator_miss``."""
    result = {"kind": "file", "status": "ok", "matches": ["a.txt", "b.txt"]}
    canonical = to_canonical(result, source="file")

    assert canonical.get("text", "") != "None: ok"  # the #2695 symptom is gone
    # The whole dict survives losslessly as a structured attachment (nothing dropped).
    assert _structured_data(canonical) == result
    # The mapped ``file`` source that fell back inside the mapper is classified fail-visible.
    assert canonical_fallback_reason("file", canonical=canonical) == "discriminator_miss"


def test_file_unknown_op_is_fail_visible() -> None:
    """Tier 1: an UNKNOWN ``op`` value (not read/grep/glob/status-op) is also fail-visible."""
    result = {"kind": "file", "op": "teleport", "status": "ok"}
    canonical = to_canonical(result, source="file")
    assert canonical.get("text", "") != "teleport: ok"
    assert _structured_data(canonical) == result
    assert canonical_fallback_reason("file", canonical=canonical) == "discriminator_miss"


def test_file_mapper_raises_discriminator_miss_directly() -> None:
    """Tier 1: the mapper itself raises ``CanonicalDiscriminatorMiss`` on a missing ``op`` (the
    fail-visible signal), rather than returning status-only garbage."""
    with pytest.raises(CanonicalDiscriminatorMiss):
        file_to_canonical({"kind": "file", "status": "ok"})


def test_valid_file_op_still_dispatches_no_fallback() -> None:
    """Tier 1: NON-REGRESSION — a VALID ``op`` still dispatches to its per-op body and does NOT
    trip the discriminator-miss path (the fix triggers only on a miss)."""
    result = {"kind": "file", "op": "glob", "status": "ok", "matches": ["alpha.txt", "beta.txt"]}
    canonical = to_canonical(result, source="file")
    assert "alpha.txt" in canonical["text"] and "beta.txt" in canonical["text"]
    assert not _structured_data(canonical)  # a mapped success is NOT a whole-dict fallback
    assert canonical_fallback_reason("file", canonical=canonical) is None


# --------------------------------------------------------------------------------------------------
# The other inner-dispatch mapper — reyn_src (body-key discriminator).
# --------------------------------------------------------------------------------------------------

def test_reyn_src_no_body_key_is_fail_visible() -> None:
    """Tier 1: a ``reyn_src`` result with none of content/entries/matches is fail-visible (whole-dict
    fallback + ``discriminator_miss``), not a SILENT (unaudited) inline whole-dict return."""
    result = {"status": "ok", "unexpected_shape": True}
    canonical = to_canonical(result, source="reyn_src_read")
    assert _structured_data(canonical) == result
    assert canonical_fallback_reason("reyn_src_read", canonical=canonical) == "discriminator_miss"


def test_reyn_src_no_body_key_raises_directly() -> None:
    """Tier 1: the ``reyn_src`` mapper raises ``CanonicalDiscriminatorMiss`` on an unknown shape."""
    with pytest.raises(CanonicalDiscriminatorMiss):
        reyn_src_to_canonical({"status": "ok"})


def test_valid_reyn_src_shape_still_dispatches() -> None:
    """Tier 1: NON-REGRESSION — a ``reyn_src`` read (``content``) still renders its body as text."""
    canonical = to_canonical(
        {"path": "docs/x.md", "content": "hello body"}, source="reyn_src_read"
    )
    assert canonical["text"] == "hello body"
    assert canonical_fallback_reason("reyn_src_read", canonical=canonical) is None


# --------------------------------------------------------------------------------------------------
# The registry-derived guard: NO registered mapper has a silent status-only catch-all.
# --------------------------------------------------------------------------------------------------

# Mappers with NO inner discriminator — they render the ENTIRE input value as ``text`` verbatim (no
# per-field selection, so there is no sub-view to silently drop on a "miss"). The probe's heuristic
# ("does the rendered text contain the bare status echoed back") cannot distinguish "the WHOLE input
# legitimately IS ``{status: ...}``" from a discriminator-miss catch-all for such a mapper — narrowly
# exempted by name, not blanket-skipped:
# - ``shell_to_canonical`` (#2681 Bucket A): ``shell``'s stdout can be ANY JSON shape (including one
#   whose only key happens to be ``status`` — a health-check-style command's own output), and the
#   mapper's whole job is to render that value faithfully — never select/drop a sub-field the way
#   ``file``/``reyn_src`` dispatch on ``op``/body-key. Nothing is ever dropped, so M3 cannot recur.
_NO_INNER_DISCRIMINATOR_MAPPERS = frozenset({"shell_to_canonical"})


def _registered_mappers() -> set:
    """Every distinct real canonical mapper function born at the two registration seams (the
    sentinels STRUCTURED_PASSTHROUGH / CANONICAL_TODO are not callable → excluded), minus the
    non-discriminating mappers this probe structurally cannot evaluate
    (:data:`_NO_INNER_DISCRIMINATOR_MAPPERS`)."""
    return {
        d for d in _DECLARATIONS.values()
        if callable(d) and d.__name__ not in _NO_INNER_DISCRIMINATOR_MAPPERS
    }


def _status_echo_offense(mapper) -> str | None:
    """Drive ``mapper`` with a discriminator-less result carrying ONLY a distinctive status. Return
    the offending text when the mapper ECHOES that status into its ``text`` body WITHOUT raising
    ``CanonicalDiscriminatorMiss`` (the #2695 ``"None: ok"`` silent-catch-all pattern), else ``None``.

    A fail-visible mapper raises (→ no offense); a mapper with no inner status-dispatch renders an
    explicit-empty marker that does not contain the probe (→ no offense)."""
    try:
        canonical = mapper({"status": _PROBE_STATUS})
    except CanonicalDiscriminatorMiss:
        return None
    text = canonical.get("text", "") or ""
    return text if _PROBE_STATUS in text else None


def test_no_registered_mapper_has_silent_status_only_catch_all() -> None:
    """Tier 2: registry-derived guard — driven with a discriminator-less status-only probe, NO
    registered canonical mapper echoes the status into its text without being fail-visible. This is
    the general rule that prevents a NEW inner-dispatch mapper from re-introducing M3 (#2695)."""
    mappers = _registered_mappers()
    assert mappers, "no canonical mappers registered — the registration seams did not populate"
    offenders = {
        mapper.__name__: offense
        for mapper in mappers
        if (offense := _status_echo_offense(mapper)) is not None
    }
    assert not offenders, (
        "these mappers emit status-only garbage on a discriminator-miss (the #2695 'None: ok' "
        f"class) — raise CanonicalDiscriminatorMiss instead: {offenders}"
    )


def test_guard_catches_a_hypothetical_silent_catch_all_mapper() -> None:
    """Tier 2: FALSIFY the guard — a hypothetical NEW inner-dispatch mapper with a silent status-only
    catch-all (the #2695 anti-pattern) IS flagged by the guard's probe, and a fail-visible variant is
    NOT (proving the guard discriminates real fail-visibility, not merely 'has a catch-all')."""

    def _garbage_mapper(result: dict) -> CanonicalToolResult:
        op = result.get("op")
        if op == "read":
            return CanonicalToolResult(
                text=str(result.get("content", "")), attachments=[], source_ref=None, meta={},
            )
        # silent status-only catch-all — the exact #2695 shape
        return CanonicalToolResult(
            text=f"{op}: {result.get('status', 'ok')}", attachments=[], source_ref=None, meta={},
        )

    def _fail_visible_mapper(result: dict) -> CanonicalToolResult:
        op = result.get("op")
        if op == "read":
            return CanonicalToolResult(
                text=str(result.get("content", "")), attachments=[], source_ref=None, meta={},
            )
        raise CanonicalDiscriminatorMiss(f"unknown op {op!r}")

    assert _status_echo_offense(_garbage_mapper) is not None  # caught
    assert _status_echo_offense(_fail_visible_mapper) is None  # allowed


# --------------------------------------------------------------------------------------------------
# End-to-end: the live chokepoint fires canonical_fallback_used on the discriminator-miss path.
# --------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_discriminator_miss_emits_fallback_event_end_to_end() -> None:
    """Tier 2: a pipeline tool step whose ``file`` result dropped its ``op`` (the #2695 shape) emits
    ``canonical_fallback_used`` with reason ``discriminator_miss`` naming the source — and NO result
    content bytes leak into any audit-event payload (audit signal, not data)."""
    secret_match = "SECRET-MATCH-PATH-should-never-reach-an-audit-event.txt"

    def _dispatch(name: str, args: dict) -> dict:
        # A ``file`` result MISSING ``op`` (the #2695 glob/list-adapter normalization) — pre-#3 this
        # canonicalized to the "None: ok" garbage with NO event; post-#3 it is fail-visible.
        return {
            "kind": "file",
            "status": "ok",
            "matches": [secret_match],
            "_canonical_source": "file",
        }

    events = EventLog()
    pipeline = Pipeline(steps=[ToolStep(name="glob_files", args={}, output="r")])
    await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-fp0056-m3", events=events,
    )

    [fallback_event] = [e for e in events.all() if e.type == CANONICAL_FALLBACK_EVENT]
    assert fallback_event.data["source"] == "file"
    assert fallback_event.data["reason"] == "discriminator_miss"

    # No result content bytes in ANY event payload — the event carries the source identity only.
    all_payloads = json.dumps([e.data for e in events.all()])
    assert secret_match not in all_payloads
