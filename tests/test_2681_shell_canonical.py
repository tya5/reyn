"""Tier 1: #2681 Bucket A — the ``shell`` tool ships a real text canonical mapper.

The scout enumeration for FP-0056's CANONICAL_TODO burn-down (#2681) flagged that PR-F1's triage
missed ``shell`` (``reyn.tools.shell._handle``) as a TEXT-content producer: it returns bare subprocess
STDOUT (``json.loads(stdout)`` when it parses, else the raw text), which under the provisional
``CANONICAL_TODO`` whole-dict fallback the LLM saw as an opaque ``structured`` blob instead of clean
text — the same file-class latent bug the ``file``/``reyn_repo`` 2026-07-09 dogfood incident produced.

``shell_to_canonical`` mirrors ``sandboxed_exec_to_canonical``'s "stdout IS the text" treatment.
``shell``'s own ``_handle`` (locked #2593 design) surfaces ONLY stdout — ``stderr``/``returncode``
never reach this canonical seam (dropped one layer up), so unlike ``sandboxed_exec`` this mapper's
``meta`` is always empty; that gap is a structural, documented consequence of the #2593 contract, not
an oversight here.

Mirrors the tool-result arc (#2425) / FP-0056 PR-H mapper-contract test style: real result shapes (the
two envelope shapes ``to_canonical(source="shell")`` actually receives — see
``reyn.core.offload.canonical.shell_to_canonical``'s docstring), no mocks, presence/absence +
substring assertions (no Tier-4 formatting pins).
"""
from __future__ import annotations

from reyn.core.offload.canonical import CANONICAL_TODO, canonical_declaration, to_canonical
from reyn.tools import get_default_registry
from reyn.tools.shell import SHELL


def test_shell_declares_a_real_mapper_not_canonical_todo():
    """Tier 1: the burn-down itself — ``shell``'s ToolDefinition no longer declares the provisional
    ``CANONICAL_TODO`` marker; it is registered as a real callable mapper."""
    assert SHELL.canonical is not CANONICAL_TODO
    assert callable(SHELL.canonical)
    registered = get_default_registry().lookup("shell")
    assert registered is not None
    assert registered.canonical is not CANONICAL_TODO
    assert canonical_declaration("shell") is SHELL.canonical


def test_plain_text_stdout_is_clean_text_not_a_whole_dict_blob():
    """Tier 1: INCIDENT — a plain (non-JSON) shell command's STDOUT, reaching this seam wrapped in the
    dispatch envelope ``{"status": ..., "data": <stdout>}`` (the shape ``to_canonical`` sees for the
    router/chat tool-call path — ``unwrap_dispatch_envelope`` cannot peel a non-dict ``data``), renders
    as the readable ``text`` verbatim. RED pre-fix: with ``CANONICAL_TODO`` the whole envelope dict
    (``{"status": "ok", "data": "hello world"}``) became a ``structured`` attachment and ``text`` was
    empty — exactly the "readable output shown as an opaque blob" bug the #2681 scout flagged."""
    result = {"status": "ok", "data": "hello world"}
    canonical = to_canonical(result, source="shell")
    assert canonical["text"] == "hello world"
    assert not any(a.get("kind") == "structured" for a in canonical["attachments"]), (
        "no whole-dict structured attachment — the incident's blob is gone"
    )


def test_json_dict_stdout_renders_as_readable_json_text_not_structured():
    """Tier 1: a JSON-emitting shell command's STDOUT decodes to a ``dict`` — the pipeline ``tool:``
    step path (``core.pipeline.executor._run_tool_step``) already peels the dispatch envelope one
    layer for a ``dict`` ``data`` (``unwrap_dispatch_envelope``), so this mapper receives the parsed
    dict DIRECTLY (no ``status``/``data`` wrapper). It still renders as legible ``text`` (json-dumped),
    never a ``structured`` attachment — mirroring sandboxed_exec's stdout-is-text treatment even though
    the stdout happens to parse as JSON."""
    result = {"n": 3, "msg": "hi"}
    canonical = to_canonical(result, source="shell")
    assert canonical["text"] == '{"n": 3, "msg": "hi"}'
    assert canonical["attachments"] == []


def test_empty_output_renders_an_explicit_marker_not_blank_text():
    """Tier 1: a no-output command (empty stdout) renders an explicit ``(no output)`` marker rather
    than a blank ``text`` — the same M2 (``canonical_degraded``) guard every other mapper applies to a
    legitimately-empty success."""
    canonical = to_canonical({"status": "ok", "data": ""}, source="shell")
    assert canonical["text"] == "(no output)"


def test_shell_removed_from_the_canonical_todo_grandfathered_ledger():
    """Tier 1: the ratchet — ``shell`` no longer takes the ``CANONICAL_TODO`` fallback, so it must not
    appear in the gate's grandfathered ledger (``tests/test_fp0056_canonical_coverage_gate.py``); the
    full ``live == ledger`` equality is asserted there, this just pins the direct declaration check."""
    assert canonical_declaration("shell") is not CANONICAL_TODO
