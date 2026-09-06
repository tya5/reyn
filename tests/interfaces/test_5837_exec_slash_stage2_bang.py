"""Tier 2: #5837 stage 2 — ``!``/``!!`` normalize to ``/exec-attach``/``/exec``
inside ``maybe_dispatch_slash``, one CLIENT-side seam, before the server (or
even the rest of this module's own dispatch) ever sees a bang.

Real ``Session``/``SandboxConfig`` throughout (``tests._support.agent_
session.make_session``, ``tests._support.slash.slash_ctx``) — no mocks. This
file's own subject is the NORMALIZATION step in
``interfaces/slash/dispatch.py``; stage 1's own command behavior (sandboxing,
audit trail, attach queueing) is already covered by
``test_5837_exec_slash_stage1.py`` and is not re-proven here.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.slash.dispatch import maybe_dispatch_slash
from tests._support.agent_session import make_session
from tests._support.paths import REPO_ROOT
from tests._support.slash import slash_ctx


def _session(tmp_path: Path):
    return make_session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        workspace_state_dir=tmp_path,
    )


def _texts(ctx) -> list[str]:
    return [getattr(m, "text", "") for m in ctx.transport.displayed]


# ---------------------------------------------------------------------------
# ⑦ — the 4 spellings land on the same handler, with the same observable
#     effect (exit code, stdout, and — for the attach pair — the queue).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bang_bang_and_slash_exec_produce_the_same_reply(tmp_path):
    """Tier 2: accept — ``!!<cmdline>`` and ``/exec <cmdline>`` both run the
    SAME sandboxed exec op and produce the same screen-only reply (exit
    code + stdout), never touching history or the attach queue."""
    cmdline = 'python3 -c "print(2+2)"'

    bang_session = _session(tmp_path / "bang")
    bang_ctx = slash_ctx(bang_session)
    consumed = await maybe_dispatch_slash(bang_ctx.transport, f"!!{cmdline}")
    assert consumed is True

    slash_session = _session(tmp_path / "slash")
    slash_ctx_ = slash_ctx(slash_session)
    await maybe_dispatch_slash(slash_ctx_.transport, f"/exec {cmdline}")

    bang_texts = _texts(bang_ctx)
    slash_texts = _texts(slash_ctx_)
    assert any("exit 0" in t for t in bang_texts), bang_texts
    assert any("4" in t for t in bang_texts), bang_texts
    # Same command, same sandbox, same handler -> the same reply shape.
    reply_kind_bang = [m.kind for m in bang_ctx.transport.displayed if m.kind != "user"]
    reply_kind_slash = [m.kind for m in slash_ctx_.transport.displayed if m.kind != "user"]
    assert reply_kind_bang == reply_kind_slash
    assert any("exit 0" in t and "4" in t for t in slash_texts), slash_texts

    bang_queue = bang_session._pending_user_attachments
    slash_queue = slash_session._pending_user_attachments
    assert not bang_queue
    assert not slash_queue


@pytest.mark.asyncio
async def test_bang_and_slash_exec_attach_both_queue_the_same_block(tmp_path):
    """Tier 2: accept — ``!<cmdline>`` and ``/exec-attach <cmdline>`` both
    queue the same ``shlex.join(argv)`` + result text block for the next
    user message."""
    cmdline = 'python3 -c "print(1+1)"'

    bang_session = _session(tmp_path / "bang")
    bang_ctx = slash_ctx(bang_session)
    await maybe_dispatch_slash(bang_ctx.transport, f"!{cmdline}")

    slash_session = _session(tmp_path / "slash")
    slash_ctx_ = slash_ctx(slash_session)
    await maybe_dispatch_slash(slash_ctx_.transport, f"/exec-attach {cmdline}")

    (bang_block,) = bang_session._pending_user_attachments
    (slash_block,) = slash_session._pending_user_attachments
    assert bang_block["type"] == slash_block["type"] == "text"
    assert "2" in bang_block["text"] and "exit 0" in bang_block["text"]
    assert bang_block["text"] == slash_block["text"], (
        "the same cmdline through either spelling must queue an identical block"
    )


# ---------------------------------------------------------------------------
# ordering — !! must be recognized before the single-! check would fire.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_bang_is_not_misrouted_to_exec_attach(tmp_path):
    """Tier 2: accept — ``!!<cmdline>`` runs ``/exec`` (screen only), never
    ``/exec-attach``. ``!!cmd`` also satisfies ``str.startswith("!")``, so a
    single-bang-first check would wrongly queue it for attachment instead."""
    session = _session(tmp_path)
    ctx = slash_ctx(session)
    await maybe_dispatch_slash(ctx.transport, '!!python3 -c "print(3+3)"')

    queue = session._pending_user_attachments
    assert not queue, "!! must never queue an attachment -- that is /exec-attach's job"
    texts = _texts(ctx)
    assert any("6" in t for t in texts), texts


# ---------------------------------------------------------------------------
# bare ! / !! — explicit error, never a submitted message.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bare", ["!", "!!"])
async def test_bare_bang_is_an_explicit_error_not_a_submitted_message(tmp_path, bare):
    """Tier 2: accept — a bang with no command text is a mistake reported
    on screen, consumed (``True``) so it is never sent as an ordinary turn."""
    session = _session(tmp_path)
    ctx = slash_ctx(session)
    consumed = await maybe_dispatch_slash(ctx.transport, bare)

    assert consumed is True, f"{bare!r} alone must be consumed, not forwarded as a turn"
    kinds = [m.kind for m in ctx.transport.displayed]
    assert "error" in kinds, ctx.transport.displayed
    queue = session._pending_user_attachments
    assert not queue


# ---------------------------------------------------------------------------
# structural — the server never sees a bang; exactly one client-side site
# recognizes the single-! form.
# ---------------------------------------------------------------------------


def _startswith_bang_literal_hits(tree: "ast.AST") -> "list[int]":
    """Every syntactic ``x.startswith("!")`` call in ``tree`` (the literal
    single-bang form — NOT ``"!!"``, which is a distinct string and a
    distinct call). Written from the value side (the literal itself), the
    same shape ``test_3595_s5_session_does_not_interpret_text.py`` already
    uses for its own ``"/"`` census, so a second single-bang recognizer
    introduced under a different spelling (``x[0] == "!"``, a regex) is a
    disclosed blind spot of this walk, not a silent pass — see that file's
    own docstring for the general shape of this caveat."""
    hits: "list[int]" = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "!"
        ):
            hits.append(node.lineno)
    return hits


# ---------------------------------------------------------------------------
# ⑧ — /help discovery of the shorthand.
# ---------------------------------------------------------------------------


def test_help_lists_the_bang_shorthand_for_exec_and_exec_attach():
    """Tier 2: accept — the top-level ``/help`` listing (which reads
    ``cmd.summary`` off the live registry, ``interfaces/slash/help.py``)
    surfaces the ``!``/``!!`` shorthand for a user who never knew to look
    for ``/exec``."""
    from reyn.interfaces.slash import REGISTRY

    exec_cmd = REGISTRY.get("exec")
    attach_cmd = REGISTRY.get("exec-attach")
    assert exec_cmd is not None and attach_cmd is not None
    assert "!!<cmdline>" in exec_cmd.summary
    assert "!<cmdline>" in attach_cmd.summary


def test_single_bang_recognition_has_exactly_one_site_in_src():
    """Tier 2: accept — ``startswith("!")`` (the single-bang form) appears
    at exactly ONE call site under ``src/`` — ``maybe_dispatch_slash``
    itself. A second site would mean two places decide what a bang means,
    the same "2 source" shape the owner ruling on this issue named and
    rejected for the call-site-level placement question."""
    hits: "dict[str, list[int]]" = {}
    for path in (REPO_ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = _startswith_bang_literal_hits(tree)
        if found:
            hits[str(path.relative_to(REPO_ROOT))] = found

    assert set(hits) == {"src/reyn/interfaces/slash/dispatch.py"}, (
        f"expected the single-bang check only in dispatch.py, found: {hits!r}"
    )
    # Unpacking to a single-element tuple IS the "exactly one site" check —
    # it raises (rather than silently passing) on zero or on two-or-more
    # hits, without a magic-number size comparison.
    (_only_site,) = hits["src/reyn/interfaces/slash/dispatch.py"]
