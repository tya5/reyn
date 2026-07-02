"""Tier 2: #2394-followup — the CHAT tool-result offloader stores CLEAN content, not the envelope.

#2394 fixed the control_ir (dict-based) offload path, but owner's chat path uses a separate
STRING-based offloader (router_loop.feedback → cap_tool_result → cap_tool_result_content →
MediaStore.save_tool_result → .reyn/tool-results/). It serialised the whole OS dispatch envelope
`{"status":"ok","data":{...}}` to one line and stored THAT — so a large MCP result's offloaded file
was a nested single-line envelope (owner's observation).

Option 3 (#2396 Step 1): the clean-payload DECISION is now ONE shared helper (`decide_payload_field`)
called by BOTH offload paths — so they can never diverge again — and the chat cap is envelope-aware:
when `data` declares a sole-oversized payload field, it stores `data[field]` CLEAN (real newlines)
via the already-shared `offload_value(payload_field=...)`. P7-safe (OS-level keys only) → generalises
to every payload-field op (mcp/web/exec). Real MediaStore + real RouterLoop.feedback (no mocks).
Verified on the CHAT path (not op-runtime).
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.core.context_builder import decide_payload_field
from reyn.data.workspace.media_store import MediaStore
from reyn.runtime.router_loop import RouterLoop
from reyn.runtime.services.tool_result_cap import cap_tool_result_content
from reyn.tools.scheme import ExecutionResult

_MODEL = "gpt-4o"
# A multi-line payload well over the offload trigger.
_BIG = "\n".join(f"line {i}: " + "z" * 60 for i in range(400))


def _envelope(payload_field: str = "content", **data_extra) -> dict:
    """The OS dispatch envelope {status, data:<op-result>} as it reaches router_loop.feedback."""
    data = {"kind": "mcp", "status": "ok", "server": "s", "tool": "t",
            "media_blocks": [], "_offload_payload_field": payload_field}
    data.update(data_extra)
    return {"status": "ok", "data": data}


def _cap(content_str: str, store: MediaStore, **kw) -> str:
    return cap_tool_result_content(
        content_str, cap_tokens=100, model=_MODEL,
        save_fn=store.save_tool_result, use_chars4=True, **kw,
    )


def _stored_body(preview_str: str, tmp_path: Path) -> str:
    # ref is a project-relative path (MediaStore stores relative to project_root=tmp_path).
    ref = json.loads(preview_str)["_offload_ref"]
    return (tmp_path / ref).read_text(encoding="utf-8")


def test_chat_cap_stores_clean_content_not_envelope(tmp_path):
    """Tier 2: CORE — the chat offloader stores `data[field]` CLEAN (real newlines), not the
    whole `{status,data}` envelope single-line. RED on main: it stored `json.dumps(envelope)`."""
    store = MediaStore(project_root=tmp_path)
    env = _envelope(content=_BIG)
    preview = _cap(json.dumps(env), store, clean_value=env["data"], payload_field="content")

    body = _stored_body(preview, tmp_path)
    assert body == _BIG, "the offloaded file is exactly the clean payload field"
    assert not body.lstrip().startswith("{"), "not a JSON dict/envelope"
    assert "\\n" not in body and body.count("\n") == _BIG.count("\n"), "real newlines, no JSON-of-JSON"


def test_inline_preview_carries_raw_field_markers(tmp_path):
    """Tier 2: the inline preview signals raw_field format + the payload field (so the reader knows
    the ref holds the clean field, matching the control_ir offloader's markers)."""
    store = MediaStore(project_root=tmp_path)
    env = _envelope(content=_BIG)
    preview = json.loads(_cap(json.dumps(env), store, clean_value=env["data"], payload_field="content"))
    assert preview["_offload_ref_format"] == "raw_field"
    assert preview["_offload_payload_field"] == "content"


def test_generalizes_to_web_and_exec_payload_fields(tmp_path):
    """Tier 2: P7-safe generalisation — the same envelope-aware path cleans a web `results` list and
    an exec `stdout` string (no MCP/content literal anywhere)."""
    store = MediaStore(project_root=tmp_path)
    big_results = [{"url": f"http://x/{i}", "text": "w" * 200} for i in range(200)]
    env_web = {"status": "ok", "data": {"kind": "web", "results": big_results,
                                        "_offload_payload_field": "results"}}
    body_web = _stored_body(
        _cap(json.dumps(env_web), store, clean_value=env_web["data"], payload_field="results"), tmp_path
    )
    assert json.loads(body_web) == big_results, "web results stored as a clean JSON array"

    env_exec = {"status": "ok", "data": {"kind": "exec", "stdout": _BIG,
                                         "_offload_payload_field": "stdout"}}
    body_exec = _stored_body(
        _cap(json.dumps(env_exec), store, clean_value=env_exec["data"], payload_field="stdout"), tmp_path
    )
    assert body_exec == _BIG, "exec stdout stored clean"


def test_no_regression_whole_envelope_when_not_clean_payload(tmp_path):
    """Tier 2: no-regression — with no clean-payload (plain string path), the stored body is the
    whole content unchanged (the pre-fix behaviour for non-envelope / multi-large results)."""
    store = MediaStore(project_root=tmp_path)
    env = _envelope(content=_BIG)
    preview = _cap(json.dumps(env), store)  # no clean_value/payload_field
    body = _stored_body(preview, tmp_path)
    assert body.lstrip().startswith("{") and '"status"' in body, "whole envelope stored (unchanged)"


def test_decide_payload_field_shared_decision(tmp_path):
    """Tier 2: the shared decision helper — sole-oversized marker → the field; a SECOND large field
    → None (multi-large whole-dict fallback, zero data-loss). This is the SAME helper router_loop and
    the control_ir offloader call, so the two paths can never diverge on the decision."""
    assert decide_payload_field(_envelope(content=_BIG)["data"]) == "content"
    # a second oversized field → not sole → None
    two_big = _envelope(content=_BIG)["data"]
    two_big["other"] = _BIG
    assert decide_payload_field(two_big) is None
    # no marker → None
    assert decide_payload_field({"kind": "x", "content": _BIG}) is None


class _CapHost:
    """A real (not mocked) RouterLoopHost surface with only the cap wired to a real MediaStore — the
    single collaborator router_loop.feedback needs for the offload chokepoint. append_history_entry /
    scan_tool_result are absent so feedback's getattr-guards skip them."""

    def __init__(self, store: MediaStore) -> None:
        self._store = store

    def cap_tool_result(self, content_str: str, *, clean_value=None, payload_field=None) -> str:
        return cap_tool_result_content(
            content_str, cap_tokens=100, model=_MODEL,
            save_fn=self._store.save_tool_result, use_chars4=True,
            clean_value=clean_value, payload_field=payload_field,
        )


def test_live_chat_feedback_detects_envelope_and_stores_clean(tmp_path):
    """Tier 2: the LIVE chat path — RouterLoop.feedback detects the `{status,data}` envelope, applies
    the shared decision, and the tool message's offloaded file is CLEAN content. Exercises the real
    router_loop envelope-detection + cap wiring (not op-runtime). RED on main (whole envelope stored)."""
    store = MediaStore(project_root=tmp_path)
    loop = RouterLoop(host=_CapHost(store), chain_id="c1", router_model=_MODEL)
    env = _envelope(content=_BIG)
    exec_result = ExecutionResult(
        tool_results=[env],
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "mcp"}}],
        assistant_content="",
    )

    msgs = loop.feedback(exec_result)

    tool_msg = next(m for m in msgs if m["role"] == "tool")
    body = _stored_body(tool_msg["content"], tmp_path)
    assert body == _BIG, "feedback stored the clean content field (envelope detected + cleaned)"
    assert json.loads(tool_msg["content"])["_offload_ref_format"] == "raw_field"
