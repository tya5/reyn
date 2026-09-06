"""Tier 2: #5845 -- MCPClient's TOOL-CALL permission hint states what reyn
granted, never a causal claim about the sandbox. Sibling of #5840 (the
init-failure hint), same fix shape, different channel.

Owner-hit sibling, deliberately deferred out of #5840's own scope (filed as
this issue): `_TOOL_CALL_WRITE_DENIAL_HINT`/`_annotate_write_denial` shared
`_looks_like_write_denial` with the init-failure hint and made the identical
causal overclaim in prose -- "this looks like reyn's MCP sandbox DENYING the
server a write ... NOT a bug in the tool". Same entailment gap: the tool-error
text ("[Errno 1] Operation not permitted: '<path>'") is IDENTICAL whether the
sandbox denied the write or a genuine non-sandbox permission failure occurred
INSIDE the granted scope (a read-only mount, a missing parent directory).

Reused, not re-derived: `looks_permission_related` -- the SAME #5832/#5840
gate -- decides whether disclosing what reyn granted is worth the sentence;
it never classifies a cause. The narrower, Seatbelt-observed
`_looks_like_write_denial` markers still gate the SPECIFIC write_paths
remedy (unchanged), offered conditionally ("if this IS the cause"), never
asserted.

`_annotate_write_denial` is called directly against a real `MCPClient`
instance (never spawned -- `_build_mcp_sandbox_policy` reads only
`self._config`, no live process needed) with a synthetic tool-call `result`
dict shaped exactly like the real, MEASURED payload
(`test_3009_mcp_tool_call_write_denial_hint.py`'s own fixture text, reused
here rather than re-typed) -- no mock, this is the real production method
under test, driven at the dict-in/dict-out seam it naturally operates on
(the channel this hint reads is already tool-result content, not a live
process's stderr, so there is no OS-level event left to make real that
`test_3009`'s own end-to-end test does not already cover).
"""
from __future__ import annotations

from typing import Any

from reyn.mcp.client import MCPClient

# The exact tool-error text the real builtin vector-store server returned
# through the real Seatbelt profile with a db_path outside its write scope
# (test_3009_mcp_tool_call_write_denial_hint.py's own measured fixture).
_DENIED_MKDIR_PAYLOAD = (
    "Error calling tool 'upsert': [Errno 1] Operation not permitted: '/tmp/x/sub'"
)
# A non-write-shaped but still permission-related failure (EACCES, not the
# Seatbelt-observed EPERM shape `_looks_like_write_denial` matches).
_NON_WRITE_PERMISSION_PAYLOAD = "Error: [Errno 13] Permission denied: '/tmp/y'"
# An ordinary, unrelated tool-call failure.
_UNRELATED_PAYLOAD = "Error calling tool 'search': no such collection 'x'"


def _error_result(text: str) -> dict[str, Any]:
    return {"isError": True, "content": [{"type": "text", "text": text}]}


def _client() -> MCPClient:
    # Never initialized/spawned -- _annotate_write_denial and the sandbox
    # policy builder it calls both read only __init__-time state.
    return MCPClient({"type": "stdio", "command": "true"}, server_name="srv")


def _texts(result: dict[str, Any]) -> str:
    return "\n".join(
        item.get("text", "") for item in result["content"]
        if isinstance(item, dict) and item.get("type") == "text"
    )


def test_write_shaped_denial_discloses_granted_range_and_offers_the_remedy() -> None:
    """Tier 2: accept -- a write-shaped permission failure gets BOTH the
    granted-range disclosure (subprocess/network/write_paths -- what reyn
    actually granted) AND the conditionally-worded write_paths remedy."""
    result = _client()._annotate_write_denial(_error_result(_DENIED_MKDIR_PAYLOAD))
    text = _texts(result)
    assert "#5845" in text
    assert "network=" in text and "write_paths=" in text and "subprocess=" in text
    assert "IS the cause" in text, f"remedy must be conditionally worded: {text!r}"
    assert "write_paths: [" in text, f"expected the concrete remedy snippet: {text!r}"


def test_hint_never_asserts_the_sandbox_caused_the_failure() -> None:
    """Tier 2: accept (the acceptance this issue exists for) -- the hint
    never states, as fact, that the sandbox denied anything; it discloses
    what reyn granted and leaves causation open."""
    result = _client()._annotate_write_denial(_error_result(_DENIED_MKDIR_PAYLOAD))
    text = _texts(result).lower()
    assert "sandbox denied" not in text and "denying the server" not in text, (
        f"the hint must never assert causation it cannot prove: {text!r}"
    )


def test_non_write_permission_failure_gets_disclosure_but_no_write_paths_remedy() -> None:
    """Tier 2: accept -- a permission-related failure that is NOT write-shaped
    (EACCES, not the Seatbelt EPERM signature) still gets the granted-range
    disclosure, but never the write-specific remedy: suggesting `write_paths`
    for a failure that was never write-shaped would be a wrong-knob remedy,
    not merely an unproven cause."""
    result = _client()._annotate_write_denial(_error_result(_NON_WRITE_PERMISSION_PAYLOAD))
    text = _texts(result)
    assert "#5845" in text and "write_paths=" in text  # the disclosure still fires
    assert "IS the cause" not in text and "write_paths: [" not in text, (
        f"the write-specific remedy must not appear for a non-write-shaped "
        f"failure: {text!r}"
    )


def test_unrelated_failure_gets_no_hint_at_all() -> None:
    """Tier 2: control arm -- an ordinary, non-permission-shaped tool-call
    failure gets neither the disclosure nor the remedy; the hint is not
    noise on every unrelated error."""
    original = _error_result(_UNRELATED_PAYLOAD)
    result = _client()._annotate_write_denial(original)
    assert result == original, "an unrelated failure must be returned unchanged"


def test_a_successful_result_is_never_annotated() -> None:
    """Tier 2: control arm -- the hint only ever attaches to isError results;
    a successful call_tool result (even one whose text happens to mention a
    permission word) passes through untouched."""
    ok_result: dict[str, Any] = {
        "isError": False,
        "content": [{"type": "text", "text": "wrote to /tmp/x with no permission issue"}],
    }
    annotated = _client()._annotate_write_denial(ok_result)
    assert annotated == ok_result


def test_a_non_stdio_server_never_gets_the_hint() -> None:
    """Tier 2: accept -- only a spawned stdio subprocess is sandboxed by
    reyn, so only it has a write_paths knob to point the operator at; a
    streamable-http server's identical-looking error must not get advice
    about a knob it does not have."""
    client = MCPClient({"type": "streamable-http", "url": "https://example.invalid/mcp"})
    result = _error_result(_DENIED_MKDIR_PAYLOAD)
    annotated = client._annotate_write_denial(result)
    assert annotated == result
