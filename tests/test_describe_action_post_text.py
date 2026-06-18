"""Tier 2: describe_action `_post_text` convention (B41-NF-W7-1 fix).

Pinned invariants:

- ``_handle_describe_action`` returns a ``_post_text`` field whose value is
  the directive appended after the JSON body by the router-loop
  message-construction layer.
- The router-loop tool-result serialisation appends ``_post_text`` outside
  the JSON content (= a textual instruction location the LLM reads as
  guidance, not as part of the structured metadata) and strips the field
  before JSON serialisation so the JSON body itself does not carry it.

Reference: B41-NF-W7-1 retrospective + W7-S2 patch-isolation Variant F
(= 10/10 → 1/10 empty-stop reduction with directive appended outside JSON).
"""
from __future__ import annotations

import asyncio
import json

from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import _handle_describe_action


class _FakeEvents:
    def emit(self, *args, **kwargs) -> None:
        pass


class _FakeHost:
    def __init__(self, skills):
        self._skills = skills

    def list_available_skills(self):
        return list(self._skills)


def _make_ctx(skills=None):
    rs = RouterCallerState(host=_FakeHost(skills or []))
    return ToolContext(
        events=_FakeEvents(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=rs,
    )


def _describe(qualified_name: str, ctx: ToolContext) -> dict:
    return asyncio.run(_handle_describe_action(
        {"action_name": qualified_name}, ctx,
    ))


def test_describe_action_returns_post_text_field():
    """Tier 2: describe_action result carries a non-empty ``_post_text`` string."""
    ctx = _make_ctx()
    out = _describe("file__read", ctx)
    assert "_post_text" in out
    assert isinstance(out["_post_text"], str)
    assert out["_post_text"].strip()


def test_describe_action_post_text_references_reply_directive():
    """Tier 2: post_text content directs the LLM to write a reply.

    Pins the directive semantic (= "write the reply now") without locking
    the exact wording — the substring "reply" is enough to confirm intent.
    """
    ctx = _make_ctx()
    out = _describe("web__search", ctx)
    assert "reply" in out["_post_text"].lower()


def test_describe_action_other_fields_unchanged_by_post_text():
    """Tier 2: adding ``_post_text`` does not displace the existing contract.

    The pre-B41 fields (``qualified_name``, ``description``,
    ``input_schema``, ``metadata``) must all remain present so callers that
    consume describe_action programmatically keep working.
    """
    ctx = _make_ctx()
    out = _describe("file__write", ctx)
    for key in ("qualified_name", "description", "input_schema", "metadata"):
        assert key in out, f"missing field: {key}"


# ── router_loop tool-result serialisation: _post_text appended outside JSON ──


def _construct_tool_message_content(r: dict) -> str:
    """Inline duplication of the router_loop tool-result serialisation step.

    Mirrors the production code in src/reyn/runtime/router_loop.py around the
    ``messages.append({"role": "tool", ...})`` block: strip ``_post_text``
    from the dict, JSON-serialise the remainder, then append the directive
    outside the JSON body separated by ``\\n\\n---\\n``.
    """
    post_text: str | None = None
    if isinstance(r, dict) and isinstance(r.get("_post_text"), str):
        post_text = r["_post_text"]
        r = {k: v for k, v in r.items() if k != "_post_text"}
    content_str = json.dumps(r, default=str)
    if post_text:
        content_str = f"{content_str}\n\n---\n{post_text}"
    return content_str


def test_serialisation_strips_post_text_from_json_body():
    """Tier 2: ``_post_text`` does not leak into the JSON body."""
    r = {"qualified_name": "x", "_post_text": "directive"}
    content = _construct_tool_message_content(r)
    json_part, _, _ = content.partition("\n\n---\n")
    body = json.loads(json_part)
    assert "_post_text" not in body
    assert body["qualified_name"] == "x"


def test_serialisation_appends_post_text_outside_json():
    """Tier 2: directive is appended after the JSON body, separated by ``---``."""
    r = {"qualified_name": "x", "_post_text": "the directive"}
    content = _construct_tool_message_content(r)
    assert content.endswith("\n\n---\nthe directive")


def test_serialisation_noop_when_post_text_absent():
    """Tier 2: dict without ``_post_text`` produces pure-JSON content (no suffix)."""
    r = {"qualified_name": "x", "description": "d"}
    content = _construct_tool_message_content(r)
    parsed = json.loads(content)
    assert parsed == r
    assert "---" not in content


def test_serialisation_noop_when_post_text_non_string():
    """Tier 2: non-string ``_post_text`` values are ignored (defensive).

    Defensive against accidental dict / list / None values from future
    handlers; the field type contract is ``str``, anything else falls
    through to the no-op path.
    """
    r = {"qualified_name": "x", "_post_text": 12345}
    content = _construct_tool_message_content(r)
    parsed = json.loads(content)
    assert parsed.get("_post_text") == 12345  # Kept inside JSON (no strip)
    assert "---" not in content
