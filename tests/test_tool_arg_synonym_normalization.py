"""Tier 2 regression tests for LLM arg-name synonym normalization (B34).

Covers:
  - file__write with ``text`` instead of ``content`` → handler accepts, no KeyError
  - Canonical key wins when both synonyms are provided simultaneously

Observed in dogfood:
  - B33 W4 S1: LLM sends {path:..., text:...} to file__write → KeyError: 'content'
  - B30 W4 S1: same mismatch (cross-batch recurrence)

FP-0066 P1b: the ``drop_source`` synonym tests (B33 W4 S6: LLM sends
{source_id:...} to drop_source → KeyError: 'source') are removed along with
the retired ``drop_source`` agent tool — see
docs/deep-dives/proposals/0066-retrieval-two-groups-two-axes.md §9.

No MagicMock — uses monkeypatch on execute_op (real handler path,
real op construction, fake op_runtime to avoid index/permission wiring).
"""
from __future__ import annotations

import pytest

from reyn.tools.file import WRITE_FILE
from reyn.tools.types import ToolContext

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_ctx() -> ToolContext:
    """Minimal ToolContext sufficient for handler invocation without wiring."""

    class _SentinelEvents:
        subscribers: list = []

    class _SentinelWorkspace:
        pass

    return ToolContext(
        events=_SentinelEvents(),
        permission_resolver=None,
        workspace=_SentinelWorkspace(),
        caller_kind="router",
    )


# ── file__write synonym: text → content ───────────────────────────────────────


@pytest.mark.asyncio
async def test_write_file_accepts_text_synonym(monkeypatch):
    """Tier 2: file__write handler accepts ``text`` as synonym for ``content``.

    Regression guard for B33 W4 S1 / B30 W4 S1: LLM sends {text:...} and
    handler previously raised KeyError: 'content' before reaching permission gate.
    """
    from reyn.schemas.models import FileIROp

    captured_ops: list = []

    async def fake_execute_op(op, ctx):
        captured_ops.append(op)
        return {"status": "ok", "path": op.path}

    import reyn.core.op_runtime as _orm
    monkeypatch.setattr(_orm, "execute_op", fake_execute_op)

    ctx = _make_ctx()
    # LLM-attractor form: {path, text} instead of {path, content}
    args = {"path": "output.txt", "text": "hello world"}
    result = await WRITE_FILE.handler(args, ctx)

    op = captured_ops[0]
    assert isinstance(op, FileIROp)
    assert op.op == "write"
    assert op.path == "output.txt"
    assert op.content == "hello world"
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_write_file_canonical_content_wins_over_text(monkeypatch):
    """Tier 2: file__write canonical ``content`` key takes priority over ``text``.

    When both are present, ``content`` is used unchanged (no synonym substitution).
    """
    from reyn.schemas.models import FileIROp

    captured_ops: list = []

    async def fake_execute_op(op, ctx):
        captured_ops.append(op)
        return {"status": "ok", "path": op.path}

    import reyn.core.op_runtime as _orm
    monkeypatch.setattr(_orm, "execute_op", fake_execute_op)

    ctx = _make_ctx()
    # Both present: content must win
    args = {"path": "out.txt", "content": "canonical", "text": "should_be_ignored"}
    await WRITE_FILE.handler(args, ctx)

    op = captured_ops[0]
    assert isinstance(op, FileIROp)
    assert op.content == "canonical"


@pytest.mark.asyncio
async def test_write_file_canonical_content_still_works(monkeypatch):
    """Tier 2: file__write canonical {path, content} call is unaffected by B34 fix."""
    from reyn.schemas.models import FileIROp

    captured_ops: list = []

    async def fake_execute_op(op, ctx):
        captured_ops.append(op)
        return {"status": "ok", "path": op.path}

    import reyn.core.op_runtime as _orm
    monkeypatch.setattr(_orm, "execute_op", fake_execute_op)

    ctx = _make_ctx()
    args = {"path": "notes.md", "content": "# Notes"}
    await WRITE_FILE.handler(args, ctx)

    op = captured_ops[0]
    assert isinstance(op, FileIROp)
    assert op.content == "# Notes"
    assert op.path == "notes.md"
