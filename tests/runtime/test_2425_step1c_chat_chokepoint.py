"""Tier 2: #2425 案B — the chat chokepoint renders frontmatter+text (whole-envelope blob FIXED).

router_loop.feedback (the chat tool-result chokepoint) normalizes EVERY result via to_canonical + the
offload seam and renders the LLM-visible frontmatter+text format — no JSON envelope. ``text`` is the
sole text-offload payload; a large ``structured`` is offloaded to its OWN ref, so the owner chat-MCP
whole-envelope (a large content AND a large structuredContent collapsing to one single-line JSON blob)
can no longer occur. Real MediaStore + real RouterLoop.feedback (no mocks); ``media_store=None`` proves
the format is store-independent.
"""
from __future__ import annotations

import json
import re

import yaml

from reyn.data.workspace.media_store import MediaStore
from reyn.runtime.router_loop import RouterLoop
from reyn.runtime.services.tool_result_cap import TRIGGER_CAP, cap_tool_result_content
from reyn.tools.scheme import ExecutionResult

_MODEL = "gpt-4o"
_BIG = "\n".join(f"line {i}: " + "z" * 60 for i in range(400))  # well over the offload trigger


class _CapHost:
    """RouterLoopHost surface with the cap + the media_store the canonical path stores through.

    ``offload_enabled = True`` — this test exercises the offload mechanism itself
    (structured/text offloading, no whole-dict blob collapse), independent of the
    config default (opt-in flip: ``offload.enabled`` defaults False in reyn.yaml)."""

    offload_enabled = True

    def __init__(self, store: "MediaStore | None") -> None:
        self.media_store = store

    def cap_tool_result(
        self,
        content_str: str,
        *,
        content_type: "str | None" = None,
        on_offload=None,
        on_write_unavailable=None,
    ) -> str:
        if self.media_store is None:
            return content_str
        return cap_tool_result_content(
            content_str, cap_tokens=100, model=_MODEL, save_fn=self.media_store.save_tool_result,
            trigger=TRIGGER_CAP, use_chars4=True, content_type=content_type, on_offload=on_offload,
            on_write_unavailable=on_write_unavailable,
        )

    def media_followup_budget(self, _content_str: str) -> int:
        return 500


def _feedback(env: dict, store: "MediaStore | None"):
    loop = RouterLoop(host=_CapHost(store), chain_id="c1", router_model=_MODEL)
    result = ExecutionResult(
        tool_results=[env],
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "mcp"}}],
        assistant_content="",
    )
    return loop.feedback(result)


def _tool_content(msgs) -> str:
    return next(m for m in msgs if m["role"] == "tool")["content"]


def _mcp_env(**data_extra) -> dict:
    data = {"kind": "mcp", "status": "ok", "server": "s", "tool": "t", "content": "", "media_blocks": []}
    data.update(data_extra)
    # FP-0056 PR-F1: the invoked-identity tag dispatch() sets — canonicalization now resolves by
    # ``source`` (the effective tool name), not result["kind"]. The direct-feedback unit test supplies
    # what dispatch() would tag.
    return {"status": "ok", "data": data, "_canonical_source": "mcp"}


def _text_ref(content: str) -> str:
    m = re.search(r'read_file\(path="([^"]+)"\)', content)
    assert m, f"no read_file read-back path in {content[:200]!r}"
    return m.group(1)


def test_single_payload_mcp_offloads_text_clean(tmp_path):
    """Tier 2: a single-payload MCP result (only content large) offloads the content CLEAN as a
    plain-text preview — the read-back file is exactly the content (real newlines, no JSON stub)."""
    store = MediaStore(project_root=tmp_path, session_id="test-session")
    content = _tool_content(_feedback(_mcp_env(content=_BIG), store))
    assert not content.lstrip().startswith("{"), "plain-text preview, not a JSON stub"
    assert "read_file(path=" in content, "the preview names the read_file read-back path"
    body = (tmp_path / _text_ref(content)).read_text(encoding="utf-8")
    assert body == _BIG, "the offloaded body is the CLEAN content (real newlines)"


def test_falsify_both_streams_offload_cleanly_no_whole_dict_blob(tmp_path):
    """Tier 2: FALSIFY (bug #2) — a result with BOTH an oversized ``content`` AND an oversized
    ``structured`` produces TWO clean offload files (one per stream) and NO single-line whole-dict
    JSON blob. The independent-stream seam is genuinely exercised: text via the token cap, structured
    via the seam's own gate."""
    store = MediaStore(project_root=tmp_path, session_id="test-session")
    big_structured = {"rows": ["x" * 4000]}
    content = _tool_content(_feedback(_mcp_env(content=_BIG, structured=big_structured), store))

    # No whole-dict blob: the content is frontmatter + a plain-text preview, never one JSON line
    # carrying BOTH content and structured.
    assert "\\n" not in content, "no JSON-of-JSON escaped newlines (the whole-dict-blob symptom)"
    assert not any(
        line.lstrip().startswith("{") and '"content"' in line and '"structured"' in line
        for line in content.splitlines()
    ), "no single-line whole-dict JSON blob carrying both streams"

    # Two clean offload files: unpacking asserts EXACTLY two independent files (one per stream) —
    # the whole point of the independent-stream seam, and the falsify against a single-blob fallback.
    file_a, file_b = sorted(store.history_content_dir.iterdir())
    bodies = [file_a.read_text(encoding="utf-8"), file_b.read_text(encoding="utf-8")]
    assert any(b == _BIG for b in bodies), "the text stream is stored CLEAN in its own file"
    assert any(json.loads(b) == big_structured for b in bodies if b.lstrip().startswith(("{", "["))), \
        "the structured stream is stored in its OWN file (not merged into text)"

    # The structured ref is surfaced in the frontmatter, not dropped.
    assert "structured_ref:" in content and "structured: offloaded" in content


def test_format_is_store_independent_media_store_none():
    """Tier 2: format ⊥ store — with ``media_store=None`` the frontmatter format STILL applies (no
    JSON envelope), proving the format does not depend on store presence. Only offloading needs a
    store; here structured stays inline, uncapped."""
    content = _tool_content(_feedback(_mcp_env(content="body", structured={"n": 1}), None))
    assert content.startswith("---\n"), "frontmatter still emitted with no store"
    head, _, body = content[4:].partition("\n---\n")
    assert yaml.safe_load(head).get("structured") == {"n": 1}, "structured inline in the frontmatter"
    assert body == "body", "the text body follows the frontmatter"


def test_plain_text_only_result_has_no_wrapper():
    """Tier 2: a text-only MCP result (no structured/signal-meta) renders as the plain text — no
    frontmatter, no JSON."""
    content = _tool_content(_feedback(_mcp_env(content="hello world"), None))
    assert content == "hello world"


def test_media_blocks_inside_data_are_forwarded(tmp_path):
    """Tier 2: media nested INSIDE data (which the top-level strip misses) is lifted and forwarded as
    a multimodal follow-up user message — the canonical path fixes the previously-missed MCP media."""
    store = MediaStore(project_root=tmp_path, session_id="test-session")
    msgs = _feedback(_mcp_env(content="small", media_blocks=[{"type": "image", "data": "aGVsbG8="}]), store)
    assert [m for m in msgs if m.get("role") == "user"], "a media follow-up message is produced"


def test_dispatch_error_renders_error_kind_message():
    """Tier 2: a dispatch-envelope error renders the plain ``Error (<kind>): <message>`` string
    (``kind`` retained because permission_denied vs not_found imply different recovery), never JSON."""
    env = {"status": "error", "error": {"kind": "permission_denied", "message": "denied: web.fetch"}}
    content = _tool_content(_feedback(env, None))
    assert content == "Error (permission_denied): denied: web.fetch"


def test_mcp_iserror_renders_error_from_content():
    """Tier 2: an MCP ``isError`` result (carried as ``status: error``) renders ``Error: <content>`` —
    MCP puts the error description in the content text."""
    content = _tool_content(_feedback(_mcp_env(status="error", content="tool blew up"), None))
    assert content == "Error: tool blew up"
