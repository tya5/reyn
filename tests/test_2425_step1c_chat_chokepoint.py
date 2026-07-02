"""Tier 3a: #2425 案B step1c — the chat chokepoint canonicalizes MCP results (whole-envelope FIXED).

router_loop.feedback (the chat tool-result chokepoint) now normalizes an MCP result via to_canonical
+ the offload seam: ``text`` (content) is the sole offload payload; ``structured``/media are
attachments held OUT of the offload decision. So the owner chat-MCP whole-envelope (a large content
AND a large structuredContent → the whole ``{status,data}`` dict stored as one JSON line) can no
longer occur. Non-MCP ops stay byte-identical (the current decide_payload_field path). Real MediaStore
+ real RouterLoop.feedback (no mocks); the host provides the media_store the seam stores through.
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.data.workspace.media_store import MediaStore
from reyn.runtime.router_loop import RouterLoop
from reyn.runtime.services.tool_result_cap import cap_tool_result_content
from reyn.tools.scheme import ExecutionResult

_MODEL = "gpt-4o"
_BIG = "\n".join(f"line {i}: " + "z" * 60 for i in range(400))  # well over the offload trigger


class _CanonicalHost:
    """RouterLoopHost surface with the cap AND the media_store the #2425 MCP path stores through."""

    def __init__(self, store: MediaStore) -> None:
        self.media_store = store

    def cap_tool_result(self, content_str: str, *, clean_value=None, payload_field=None) -> str:
        return cap_tool_result_content(
            content_str, cap_tokens=100, model=_MODEL, save_fn=self.media_store.save_tool_result,
            use_chars4=True, clean_value=clean_value, payload_field=payload_field,
        )

    def media_followup_budget(self, _content_str: str) -> int:
        return 500


def _mcp_env(**data_extra) -> dict:
    data = {"kind": "mcp", "status": "ok", "server": "s", "tool": "t", "content": "", "media_blocks": []}
    data.update(data_extra)
    return {"status": "ok", "data": data}


def _feedback(env: dict, tmp_path: Path):
    store = MediaStore(project_root=tmp_path)
    loop = RouterLoop(host=_CanonicalHost(store), chain_id="c1", router_model=_MODEL)
    result = ExecutionResult(
        tool_results=[env],
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "mcp"}}],
        assistant_content="",
    )
    return loop.feedback(result), store


def _stored_body(tool_content: str, tmp_path: Path) -> str:
    return (tmp_path / json.loads(tool_content)["_offload_ref"]).read_text(encoding="utf-8")


def test_a_single_payload_mcp_offloads_content_clean(tmp_path):
    """Tier 3a: (a) a single-payload MCP result (only content large) offloads the content CLEAN — the
    LLM-reachable body is exactly the content (fit-equivalent to the current path)."""
    msgs, _store = _feedback(_mcp_env(content=_BIG), tmp_path)
    tool_msg = next(m for m in msgs if m["role"] == "tool")
    assert _stored_body(tool_msg["content"], tmp_path) == _BIG, "content offloaded clean (text = sole payload)"
    assert json.loads(tool_msg["content"]).get("_offload_payload_field") == "text"


def test_b_multi_field_whole_envelope_is_fixed(tmp_path):
    """Tier 3a: (b) CORE — an MCP result with a large content AND a large structuredContent no longer
    whole-envelopes. The offloaded body is the content CLEAN (not a JSON dict of the envelope); the
    structured is a separate attachment, never a second oversized field. RED-verify: reverting the MCP
    branch stores the whole {status,data} envelope (body starts with '{', structured JSON-escaped)."""
    env = _mcp_env(content=_BIG, structured={"rows": ["x" * 4000]})
    msgs, _store = _feedback(env, tmp_path)
    tool_msg = next(m for m in msgs if m["role"] == "tool")
    body = _stored_body(tool_msg["content"], tmp_path)
    assert body == _BIG, "the offloaded body is the CLEAN content — the whole-envelope collapse is gone"
    assert not body.lstrip().startswith("{"), "not a whole-dict JSON envelope"
    assert "\\n" not in body, "real newlines, no JSON-of-JSON (structured did not force a whole-dict fallback)"


def test_c_media_blocks_inside_data_are_forwarded(tmp_path):
    """Tier 3a: (c) media blocks nested INSIDE data (which the top-level strip missed) are lifted and
    forwarded as a multimodal follow-up — the canonical path fixes the previously-missed MCP media."""
    env = _mcp_env(content="small", media_blocks=[{"type": "image", "data": "aGVsbG8="}])
    msgs, _store = _feedback(env, tmp_path)
    # a follow-up user message beyond the assistant + tool messages carries the media
    followups = [m for m in msgs if m.get("role") == "user"]
    assert followups, "a media follow-up message is produced (MCP image lifted from data + forwarded)"


def test_e_non_mcp_op_stays_on_current_payload_path(tmp_path):
    """Tier 3a: (e) a non-MCP op (exec) stays on the current decide_payload_field path — its declared
    stdout is offloaded clean via its own marker, unchanged by the MCP branch (a permanent invariant:
    canonicalization is MCP-only, so web/exec keep their field offload)."""
    env = {"status": "ok", "data": {"kind": "sandboxed_exec", "status": "ok", "stdout": _BIG,
                                     "_offload_payload_field": "stdout"}}
    msgs, _store = _feedback(env, tmp_path)
    tool_msg = next(m for m in msgs if m["role"] == "tool")
    assert _stored_body(tool_msg["content"], tmp_path) == _BIG, "exec stdout offloaded clean (unchanged path)"
    assert json.loads(tool_msg["content"]).get("_offload_payload_field") == "stdout", "current marker, not text"
