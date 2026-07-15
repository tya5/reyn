"""Tier 1/2: the shared canonical error seam (FP-0056 v2 piece #1 — the M1-class linchpin).

The tool-result canonical mapper hand-wrote error handling per-mapper, so a mapper with NO error branch
(recall/task_ops ``{ok:False, error_message}`` — #2698; ``file`` ``denied``/``not_found``; ``memory_body``/
``reyn_repo`` ``{error}``; ``web_fetch`` errors) rendered an error to EMPTY text — the M1 silent-loss
class. Piece #1 eliminates the class STRUCTURALLY: :func:`to_canonical` applies a union error predicate
(:func:`is_error_result`) BEFORE per-mapper dispatch and routes any known error shape to a single
:func:`error_to_canonical`; the per-mapper error branches are removed (mappers become success-only).

Two contracts these tests pin:

- **the fixed-set union predicate** — :func:`is_error_result` recognizes the fixed set of known error
  shapes and, per tightening A, does NOT over-match a SUCCESS payload carrying a data-meaning ``ok`` /
  ``status`` key (a health-check ``{ok:False, service}``; ``sandboxed_exec``'s nonzero-exit
  ``{status:"error", stdout, returncode}``).
- **losslessness** — :func:`error_to_canonical` renders a non-empty ``text`` AND the whole result dict
  as a structured attachment (+ ``meta.isError``). This is what makes the predicate safe to run before
  every mapper: even a hypothetical misclassification only MIS-LABELS, it never loses data.

Real instances only (no mocks): the real registration seam (op-runtime import + default registry), the
real mapper functions, a real ``PipelineExecutor`` + ``EventLog`` at the chokepoint.
"""
from __future__ import annotations

import pytest

# Importing the op-runtime package + building the default registry populates every canonical
# declaration (identically to the coverage-gate / degraded / fallback-event tests).
import reyn.core.op_runtime as _op_runtime  # noqa: F401
from reyn.core.events.events import EventLog
from reyn.core.offload.canonical import (
    error_to_canonical,
    is_error_result,
    to_canonical,
)
from reyn.core.pipeline.executor import Pipeline, PipelineExecutor, ToolStep
from reyn.tools import get_default_registry

get_default_registry()


# ─────────────────────────────────────────────────────────────────────────────
# is_error_result — the fixed-set union predicate (Tier 1 contract)
# ─────────────────────────────────────────────────────────────────────────────


def test_is_error_result_true_for_every_fixed_error_shape() -> None:
    """Tier 1: :func:`is_error_result` returns True for the fixed set of known error shapes — the
    ``{isError}`` flag, and the dedicated error-message fields ``error`` / ``error_message`` /
    ``error_kind`` (which subsume the recall/task_ops ``{ok:False, error_message}`` + ``{error_kind}``,
    the ``file`` ``denied``/``not_found`` which carry ``error``, and every other removed per-mapper
    branch). The status-bearing shapes are the SHAPES REAL PRODUCERS EMIT — a status:error/denied/
    not_found always co-carries an ``error`` field."""
    for shape in (
        {"isError": True},                                              # MCP-style explicit flag
        {"ok": False, "error": "boom"},                                # present/render_template/file
        {"ok": False, "error_message": "recall requires [query]"},     # recall / task_ops (#2698)
        {"status": "error", "error": "syntax error"},                  # render_template/judge/web
        {"status": "denied", "error": "read-authority denied"},        # file / present
        {"status": "not_found", "error": "file not found: x.md"},      # file / render_template
        {"error_kind": "missing_required_arg", "error_message": "..."},  # recall's kind+message
        {"error_kind": "x"},                                           # bare error_kind
        {"error": "reyn_repo: path resolves outside repo"},             # memory_body / reyn_repo bare error
    ):
        assert is_error_result(shape) is True, shape


def test_is_error_result_false_for_success_and_data_meaning_payloads() -> None:
    """Tier 1: FALSIFY (tightening A — the misclassification safety). :func:`is_error_result` does NOT
    over-match a SUCCESS payload that carries a data-meaning ``ok`` / ``status`` key:

    - a health-check-style ``{ok: False, service: "db"}`` (``ok:False`` MEANS "DB is down" — success
      data, NOT a tool error) → False (a bare ``ok is False`` is never a trigger);
    - ``sandboxed_exec``'s ``{status:"error", returncode:2, stdout, stderr}`` (a nonzero exit is a
      SUCCESSFUL execution whose output is stdout/stderr) → False (a bare ``status`` value is never a
      trigger; there is no error-message field). Routing it through the error seam would drop
      stdout/stderr on the router error path — the concrete in-repo instance of the ``status`` hazard.

    Plus ordinary success shapes and a non-dict."""
    for shape in (
        {"ok": False, "service": "db", "region": "us"},                # health-check data-meaning
        {"status": "error", "returncode": 2, "stdout": "out", "stderr": "boom"},  # sandboxed_exec
        {"status": "cancelled", "returncode": -1, "stdout": "partial"},  # sandboxed_exec cancel
        {"status": "ok", "content": "hi"},                             # ordinary success
        {"chunks": [{"id": 1}]},                                       # recall success
        {"results": [{"url": "u"}]},                                   # web_search success
        {"content": "hello world"},                                    # reyn_repo / file read success
    ):
        assert is_error_result(shape) is False, shape
    assert is_error_result("not a dict") is False
    assert is_error_result(None) is False


# ─────────────────────────────────────────────────────────────────────────────
# error_to_canonical — losslessness (Tier 1 contract)
# ─────────────────────────────────────────────────────────────────────────────


def test_error_to_canonical_is_lossless_non_empty_text_plus_whole_dict_attachment() -> None:
    """Tier 1: :func:`error_to_canonical` is LOSSLESS — a NON-EMPTY ``text`` (the extracted union
    message) AND the WHOLE result dict as a structured attachment, plus ``meta.isError``. Proven with a
    data-meaning-key payload (the hypothetical misclassification): even if such a success were mislabeled
    an error, NO data is lost — every field survives in the attachment."""
    payload = {"ok": False, "service": "db", "region": "us-east", "latency_ms": 4200}
    canonical = error_to_canonical(payload)
    assert canonical["text"].strip(), "error text must be non-empty (the M1 fix)"
    assert canonical["meta"].get("isError") is True
    # The WHOLE dict is preserved (lossless) — no field dropped even on a (hypothetical) misclassification.
    assert canonical["attachments"] == [{"kind": "structured", "data": payload}]
    assert canonical["attachments"][0]["data"]["service"] == "db"
    assert canonical["attachments"][0]["data"]["latency_ms"] == 4200


def test_error_to_canonical_extracts_message_in_priority_order() -> None:
    """Tier 1: the union message extractor reads the first present error field (error_message → error →
    error_kind), always non-empty; a shape with only an error signal and no readable message still
    yields a non-empty line (the full dict remains in the attachment)."""
    assert error_to_canonical({"error_message": "msg-a", "error": "msg-b"})["text"] == "msg-a"
    assert error_to_canonical({"error": "msg-b"})["text"] == "msg-b"
    assert "kind-only" in error_to_canonical({"error_kind": "kind-only"})["text"]
    assert error_to_canonical({"isError": True})["text"].strip()  # non-empty even with no message field


# ─────────────────────────────────────────────────────────────────────────────
# #2698 subsumed — semantic_search's {ok:False, error_message} renders NON-EMPTY (the acceptance test)
# ─────────────────────────────────────────────────────────────────────────────


def test_recall_missing_arg_error_renders_non_empty_via_seam() -> None:
    """Tier 1: #2698 SUBSUMED — semantic_search's (FP-0057 Phase 2a; renamed from recall)
    missing-arg error ``{ok:False, error_kind, error_message}`` (which ``chunks_to_canonical``
    has NO branch for → rendered EMPTY on pre-#1 main, the M1 bug) now routes through the shared
    seam to a NON-EMPTY ``text`` with ``meta.isError``. This is the RED→GREEN acceptance test the
    #2698 issue asks for (RED against pre-#1 main / a neutered predicate)."""
    recall_error = {
        "ok": False,
        "error_kind": "missing_required_arg",
        "error_message": "semantic_search requires ['query', 'sources']. Available sources are listed under "
        "'Indexed sources' in the system prompt.",
        "missing": ["query", "sources"],
    }
    canonical = to_canonical(recall_error, source="semantic_search")
    assert canonical["text"].strip(), "semantic_search error must render NON-EMPTY (the #2698 fix)"
    assert "semantic_search requires" in canonical["text"]
    assert canonical["meta"].get("isError") is True
    # Lossless: the whole error dict (incl. ``missing``) survives in the attachment.
    assert canonical["attachments"] == [{"kind": "structured", "data": recall_error}]


# ─────────────────────────────────────────────────────────────────────────────
# Per-mapper spot-checks — each removed error branch still routes correctly through the seam
# ─────────────────────────────────────────────────────────────────────────────


def test_removed_branch_mappers_still_route_errors_through_the_seam() -> None:
    """Tier 1: each mapper whose per-mapper error branch was removed (success-only now) still surfaces
    its error as NON-EMPTY ``text`` + ``meta.isError`` via the shared seam — spot-checking ``file``
    (denied + not_found), ``reyn_repo``, ``memory_body``, ``compact``, ``present``,
    ``render_template``, and a ``task`` op (CANONICAL_TODO, also in seam scope)."""
    cases = [
        # (result, source, a substring expected in the rendered text)
        ({"kind": "file", "op": "read", "path": "missing.md", "status": "not_found",
          "error": "file not found: missing.md", "content": ""}, "file", "file not found"),
        ({"kind": "file", "op": "write", "path": "/etc/x", "status": "denied",
          "error": "write denied: outside workspace"}, "file", "denied"),
        ({"kind": "reyn_repo", "error": "reyn_repo: path '..' resolves outside repo"},
         "reyn_repo_read", "outside"),
        ({"error": "memory entry not clean", "layer": "user", "slug": "x"},
         "read_memory_body", "not clean"),
        ({"kind": "compact", "status": "error", "error_kind": "compaction_unavailable",
          "error": "no compaction context is wired here"}, "compact", "no compaction context"),
        ({"kind": "present", "status": "not_found", "ok": False, "error": "data_ref not found: r1"},
         "present", "data_ref not found"),
        ({"kind": "render_template", "status": "error", "ok": False,
          "error": "template syntax error at line 3"}, "render_template", "syntax error"),
        ({"kind": "task", "ok": False, "error_message": "task.create args invalid: missing title"},
         "task.create", "args invalid"),
    ]
    for result, source, expected_substr in cases:
        canonical = to_canonical(result, source=source)
        assert canonical["meta"].get("isError") is True, (source, canonical)
        assert canonical["text"].strip(), f"{source} error must render non-empty"
        assert expected_substr in canonical["text"], (source, canonical["text"])
        # Losslessness holds uniformly: the whole result dict is preserved in the attachment.
        assert canonical["attachments"] == [{"kind": "structured", "data": result}], source


def test_ask_user_refused_surfaces_reason_not_empty_and_is_not_error() -> None:
    """Tier 1: the THIRD in-mapper hybrid boundary — ``ask_user``'s deliberate P3-item3 refusal
    ``{status:"refused", reason, answer:""}`` is a typed NON-error outcome carrying no error-message
    field (so the seam correctly does not intercept it). It MUST render the ``reason`` as NON-EMPTY
    ``text`` and must NOT be error-framed (``meta.isError`` unset):

    - RED against the pre-fix mapper, which saw the empty ``answer`` and rendered ``(no answer)``,
      silently dropping the reason (an M1 loss + a cross-arc regression that re-introduced the very
      empty-answer the #2708 P3-item3 refusal design removed — the LLM could not tell a refusal from a
      blank answer)."""
    refused = {
        "kind": "ask_user", "question": "proceed?", "answer": "",
        "status": "refused", "reason": "detached/headless spawn — no user attached",
    }
    # A refusal is a deliberate outcome, NOT a tool error.
    assert is_error_result(refused) is False
    canonical = to_canonical(refused, source="ask_user")
    # The reason is surfaced (non-empty, NOT the silent "(no answer)").
    assert canonical["text"].strip()
    assert canonical["text"] != "(no answer)", "the refusal reason must not be silently dropped"
    assert "detached/headless spawn" in canonical["text"]
    # The text is distinguishable as a refusal (the LLM can tell refusal from a blank answer).
    assert "refused" in canonical["text"].lower()
    # NOT error-framed — a deliberate refusal is a typed non-error outcome.
    assert not canonical["meta"].get("isError")

    # A refusal with no reason string still renders a non-empty, refusal-distinguishable text.
    no_reason = to_canonical(
        {"kind": "ask_user", "answer": "", "status": "refused"}, source="ask_user"
    )
    assert no_reason["text"].strip() and "refused" in no_reason["text"].lower()
    assert not no_reason["meta"].get("isError")


def test_ask_user_legacy_empty_and_answered_paths_unchanged() -> None:
    """Tier 1: NON-REGRESSION guard — the in-mapper refused branch keys STRICTLY on ``status=="refused"``
    (the #2708 P3-item3 deliberate refusal) and must NOT alter the two neighbouring shapes the producer
    distinguishes (ask_user.py):

    - a legacy empty-string auto-refuse (``refused:False`` → ``{status:"ok", answer:""}``) still renders
      the existing ``(no answer)`` marker (NOT the refusal wording, NOT dropped);
    - a normal answered result (``{status:"ok", answer:<text>}``) still renders the answer as ``text``.

    Neither is error-classified. This pins that the refused branch does not swallow a success shape."""
    legacy_empty = to_canonical(
        {"kind": "ask_user", "question": "q", "answer": "", "status": "ok"}, source="ask_user"
    )
    assert legacy_empty["text"] == "(no answer)"
    assert not legacy_empty["meta"].get("isError")

    answered = to_canonical(
        {"kind": "ask_user", "question": "q", "answer": "yes", "status": "ok"}, source="ask_user"
    )
    assert answered["text"] == "yes"
    assert not answered["meta"].get("isError")


def test_sandboxed_exec_and_mcp_status_error_keep_in_mapper_handling() -> None:
    """Tier 1: the two producers whose ``status:"error"`` carries a NON-message payload are correctly
    NOT intercepted by the seam (their status is data-meaning) — they keep their in-mapper handling:

    - ``sandboxed_exec`` nonzero exit → ``stdout``/``stderr`` in ``text``, ``returncode`` as signal meta;
    - ``mcp`` tool-error → the description (in ``content``) as ``text`` + ``meta.isError``.

    Routing either through ``error_to_canonical`` would drop that payload on the router error path (which
    renders ``text`` only when ``meta.isError``) — the tightening-A hazard this scoping avoids."""
    se = to_canonical(
        {"kind": "sandboxed_exec", "status": "error", "returncode": 2, "stdout": "out", "stderr": "boom"},
        source="sandboxed_exec",
    )
    assert "out" in se["text"] and "boom" in se["text"], se["text"]
    assert se["meta"].get("returncode") == 2

    mc = to_canonical(
        {"kind": "mcp", "status": "error", "content": "boom: tool failed", "media_blocks": []},
        source="mcp",
    )
    assert mc["text"] == "boom: tool failed"
    assert mc["meta"].get("isError") is True


# ─────────────────────────────────────────────────────────────────────────────
# Tier 2 — the seam at the live pipeline chokepoint (real PipelineExecutor + EventLog)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recall_error_at_pipeline_chokepoint_renders_non_empty_no_degraded() -> None:
    """Tier 2: a semantic_search (FP-0057 Phase 2a; renamed from recall) missing-arg error flowing
    through a real pipeline tool step renders a non-empty canonical error view — and, because piece
    #1 guarantees an error is non-empty + error-classified, it does NOT trip the piece #2
    ``canonical_degraded`` invariant (which now unambiguously means "a SUCCESS result was lost").
    Real ``PipelineExecutor`` + real ``EventLog`` (no mocks)."""
    from reyn.core.offload.canonical import CANONICAL_DEGRADED_EVENT

    def _dispatch(name: str, args: dict) -> dict:
        return {
            "ok": False,
            "error_kind": "missing_required_arg",
            "error_message": "semantic_search requires ['query', 'sources'].",
            "missing": ["query", "sources"],
            "_canonical_source": "semantic_search",
        }

    events = EventLog()
    pipeline = Pipeline(steps=[ToolStep(name="semantic_search", args={}, output="r")])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-fp0056-p1-recall", events=events,
    )

    # The semantic_search error surfaced into the step's ctx as non-empty text (the M1 fix, end-to-end).
    ctx_value = result.named_stores["r"]
    assert ctx_value["text"].strip(), "semantic_search error must reach ctx as non-empty text"
    assert "semantic_search requires" in ctx_value["text"]
    # An error is error-classified → it does NOT masquerade as a lost success → no degraded event.
    assert not [e for e in events.all() if e.type == CANONICAL_DEGRADED_EVENT], (
        "an error result (non-empty, error-classified) must not trip canonical_degraded"
    )
