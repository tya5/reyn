"""Tier 3a: #5364 §1.2 — the write-time cap chokepoint (RouterLoop.feedback,
mirroring test_2425_step1c_chat_chokepoint.py's own real harness) stamps
``SPILLED_META_KEY``/``CONTENT_REF_META_KEY`` on the persisted history entry
exactly when an offload actually happened — never guessed from the
rendered content string's own shape (this repo's typed-over-form-sniffed
convention). This is the resolver's own write-side counterpart: without
this wiring, ``reyn.core.offload.history_content_resolve.resolve()`` has
no real signal to read (#5364, lead-coder: "resolver は呼び口と同じ PR で").
"""
from __future__ import annotations

from reyn.data.workspace.media_store import MediaStore
from reyn.runtime.chat_message import CONTENT_REF_META_KEY, SPILLED_META_KEY
from reyn.runtime.router_loop import RouterLoop
from reyn.runtime.services.tool_result_cap import cap_tool_result_content
from reyn.tools.scheme import ExecutionResult

_MODEL = "gpt-4o"
_BIG = "\n".join(f"line {i}: " + "z" * 60 for i in range(400))  # well over the offload trigger
_SMALL = "tiny result, stays inline"


class _RecordingHost:
    """Mirrors test_2425_step1c_chat_chokepoint.py's own ``_CapHost`` +
    captures every ``append_history_entry`` call so this test can assert
    on the persisted ``meta`` dict — the ONE thing ``RouterLoop.feedback``
    actually hands a real Session in production."""

    offload_enabled = True

    def __init__(self, store: "MediaStore | None") -> None:
        self.media_store = store
        self.appended: list[dict] = []

    def cap_tool_result(
        self, content_str: str, *, content_type: "str | None" = None, on_offload=None,
    ) -> str:
        if self.media_store is None:
            return content_str
        return cap_tool_result_content(
            content_str, cap_tokens=100, model=_MODEL,
            save_fn=self.media_store.save_tool_result,
            use_chars4=True, content_type=content_type, on_offload=on_offload,
        )

    def media_followup_budget(self, _content_str: str) -> int:
        return 500

    def append_history_entry(self, **kwargs) -> None:
        self.appended.append(kwargs)


def _mcp_env(**data_extra) -> dict:
    data = {"kind": "mcp", "status": "ok", "server": "s", "tool": "t", "content": "", "media_blocks": []}
    data.update(data_extra)
    return {"status": "ok", "data": data, "_canonical_source": "mcp"}


def _feedback(env: dict, host: "_RecordingHost"):
    loop = RouterLoop(host=host, chain_id="c1", router_model=_MODEL)
    result = ExecutionResult(
        tool_results=[env],
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "mcp"}}],
        assistant_content="",
    )
    return loop.feedback(result)


def test_offloaded_tool_result_is_stamped_spilled_with_a_content_ref(tmp_path) -> None:
    """Tier 3a: content over the cap → the persisted entry's meta carries
    SPILLED_META_KEY=True and CONTENT_REF_META_KEY naming a file that
    actually exists and holds the ORIGINAL (pre-offload) content."""
    store = MediaStore(project_root=tmp_path, session_id="test-session")
    host = _RecordingHost(store)

    _feedback(_mcp_env(content=_BIG), host)

    (tool_entry,) = [e for e in host.appended if e["role"] == "tool"]
    meta = tool_entry["meta"]
    assert meta.get(SPILLED_META_KEY) is True
    ref = meta.get(CONTENT_REF_META_KEY)
    assert ref, f"expected a content ref in meta, got: {meta!r}"
    full = tmp_path / ref
    assert full.is_file(), f"the ref must name a file that actually exists: {full}"
    assert full.read_text(encoding="utf-8") == _BIG


def test_unoffloaded_tool_result_is_never_stamped_spilled(tmp_path) -> None:
    """Tier 3a: content under the cap → no offload happens → meta carries
    neither key at all (never False/None as a placeholder — see
    SPILLED_META_KEY's own docstring: absence means "never spilled")."""
    store = MediaStore(project_root=tmp_path, session_id="test-session")
    host = _RecordingHost(store)

    _feedback(_mcp_env(content=_SMALL), host)

    (tool_entry,) = [e for e in host.appended if e["role"] == "tool"]
    meta = tool_entry["meta"]
    assert SPILLED_META_KEY not in meta
    assert CONTENT_REF_META_KEY not in meta


def test_no_media_store_never_stamps_spilled(tmp_path) -> None:
    """Tier 3a: accept-side — a host with no media_store configured (cap
    is identity, per _RecordingHost.cap_tool_result's own no-op branch)
    never stamps SPILLED_META_KEY even for content that WOULD have been
    offloaded had a store existed — proves the stamp is tied to an actual
    offload, not just "content is large"."""
    host = _RecordingHost(store=None)

    _feedback(_mcp_env(content=_BIG), host)

    (tool_entry,) = [e for e in host.appended if e["role"] == "tool"]
    meta = tool_entry["meta"]
    assert SPILLED_META_KEY not in meta
    assert CONTENT_REF_META_KEY not in meta
