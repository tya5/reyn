"""Tier 2: #5837 stage 1 — ``/exec`` and ``/exec-attach``.

Real ``Session``/``SandboxConfig`` throughout (``tests._support.agent_
session.make_session``, ``tests._support.slash.slash_ctx``) — no mocks. The
LLM ``exec`` tool path is untouched by this file's own subject; only the
NEW operator-facing slash commands (``interfaces/slash/exec.py``) are
under test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config import SandboxConfig
from reyn.core.events.state_log import StateLog
from reyn.interfaces.slash import REGISTRY
from reyn.interfaces.slash.exec import (
    ExecCmdlineRejected,
    tokenize_exec_cmdline,
)
from tests._support.agent_session import make_session
from tests._support.slash import slash_ctx


def _session(tmp_path: Path, *, sandbox_config: "SandboxConfig | None" = None):
    return make_session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        sandbox_config=sandbox_config,
        workspace_state_dir=tmp_path,
    )


# ---------------------------------------------------------------------------
# ④ tokenize_exec_cmdline — the one isolated shlex function
# ---------------------------------------------------------------------------


def test_tokenize_splits_quoted_argument_as_one_token():
    """Tier 2: accept — ``/exec ls "my dir"`` -> argv ``["ls", "my dir"]``."""
    assert tokenize_exec_cmdline('ls "my dir"') == ["ls", "my dir"]


@pytest.mark.parametrize(
    "cmdline",
    [
        "ls | wc -l",
        "'ls | wc -l'",  # quoting a pipe does not rescue it (whole quoted token)
        "ls > out.txt",
        "ls < in.txt",
        "true && false",
        "ls; rm -rf /",
        "echo $(whoami)",
        "echo `whoami`",
    ],
)
def test_tokenize_rejects_every_shell_metacharacter_form(cmdline: str):
    """Tier 2: accept — ``/exec 'ls | wc -l'`` is NOT executed, explicit
    error (#5837 ④'s own named case, plus the sibling metacharacters)."""
    with pytest.raises(ExecCmdlineRejected):
        tokenize_exec_cmdline(cmdline)


def test_tokenize_rejects_empty_command():
    """Tier 2: an empty/whitespace-only command line is rejected, never
    silently executed as an empty argv."""
    with pytest.raises(ExecCmdlineRejected):
        tokenize_exec_cmdline("   ")


def test_tokenize_rejects_unbalanced_quotes():
    """Tier 2: shlex's own ValueError (unbalanced quoting) surfaces as
    ExecCmdlineRejected, not an uncaught exception from the slash handler."""
    with pytest.raises(ExecCmdlineRejected):
        tokenize_exec_cmdline("ls 'unterminated")


# ---------------------------------------------------------------------------
# ② operator actor + ① sandbox seam (op-context construction, no subprocess)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_op_context_uses_a_dedicated_actor_not_chat_router(tmp_path):
    """Tier 2: accept — the audit trail's actor is NOT "chat_router" (the
    approval ledger is actor-scoped; reusing that name would mix an
    operator's own /exec calls into the chat router's record)."""
    from reyn.interfaces.slash.exec import (
        _OPERATOR_EXEC_ACTOR,
        _build_exec_tool_context,
        _build_operator_op_context,
    )

    session = _session(tmp_path)
    ctx = slash_ctx(session)
    tool_ctx = await _build_exec_tool_context(ctx)
    assert tool_ctx.caller_kind == "operator"

    op_ctx = await _build_operator_op_context(tool_ctx)
    assert op_ctx.actor == _OPERATOR_EXEC_ACTOR
    assert op_ctx.actor != "chat_router"


@pytest.mark.asyncio
async def test_operator_op_context_threads_strict_sandbox_mode(tmp_path):
    """Tier 2: accept — sandbox.mode: strict reaches /exec's own OpContext
    through the SAME op_context_factory() seam the LLM's own exec tool
    call uses (reyn.tools.exec.op_context_from_tool_context) — mirrors
    test_5818_router_op_context_strict_mode_wiring.py's own assertion
    shape, applied to this NEW bridge instead of the chat-router one.

    Falsify note (verified in-file, Edit-only): temporarily replacing
    ``_build_operator_op_context``'s call to ``op_context_from_tool_
    context`` with a bare, mode="compat"-hardcoded OpContext construction
    (the "minimal synthesis" shape that path already has as its OWN
    fallback) turns this RED — ``network`` reads the compat default
    (``True``) instead of ``False``. Confirmed directly, then reverted —
    see this PR's own commit history / falsify report, never left in the
    diff.
    """
    from reyn.interfaces.slash.exec import _build_exec_tool_context, _build_operator_op_context

    session = _session(tmp_path, sandbox_config=SandboxConfig(mode="strict"))
    ctx = slash_ctx(session)
    tool_ctx = await _build_exec_tool_context(ctx)
    op_ctx = await _build_operator_op_context(tool_ctx)

    pol = op_ctx.default_sandbox_policy
    assert pol is not None
    assert pol["network"] is False, (
        "strict mode must deny network on /exec's own OpContext — "
        f"got default_sandbox_policy={pol!r}"
    )
    assert pol["deny_subprocess"] is True


@pytest.mark.asyncio
async def test_operator_op_context_compat_mode_unchanged(tmp_path):
    """Tier 2: control arm — compat (no sandbox_config) is untouched,
    same posture as test_5818's own compat-mode sibling."""
    from reyn.interfaces.slash.exec import _build_exec_tool_context, _build_operator_op_context
    from reyn.security.sandbox.policy import DEFAULT_SANDBOX_NETWORK

    session = _session(tmp_path, sandbox_config=None)
    ctx = slash_ctx(session)
    tool_ctx = await _build_exec_tool_context(ctx)
    op_ctx = await _build_operator_op_context(tool_ctx)

    pol = op_ctx.default_sandbox_policy
    assert pol is not None
    assert pol["network"] is DEFAULT_SANDBOX_NETWORK


# ---------------------------------------------------------------------------
# The real slash commands themselves — /exec and /exec-attach end to end.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_registered_and_shows_up_in_help_discovery():
    """Tier 2: accept — /exec and /exec-attach are real, discoverable
    commands (the `!`/`!!` prefixes are stage 2 — this only pins the
    named forms this stage actually ships)."""
    exec_cmd = REGISTRY.get("exec")
    attach_cmd = REGISTRY.get("exec-attach")
    assert exec_cmd is not None
    assert attach_cmd is not None
    assert "sandboxed exec" in exec_cmd.summary.lower()


@pytest.mark.asyncio
async def test_exec_runs_and_shows_result_on_screen_only_no_history_growth(tmp_path):
    """Tier 2: accept — `/exec` shows exit code + stdout on screen, and
    history.jsonl gains ZERO lines (slash replies are put_display only,
    never appended to history — this command's whole point)."""
    session = _session(tmp_path)
    history_before = (
        session.history_path.read_text() if session.history_path.exists() else ""
    )

    cmd = REGISTRY.get("exec")
    outbox: list = []
    ctx = slash_ctx(session, recorder=outbox)
    await cmd.handler(ctx, 'python3 -c "print(2+2)"')

    texts = [getattr(m, "text", "") for m in outbox]
    assert any("exit 0" in t for t in texts), f"expected an exit-0 reply, got {texts!r}"
    assert any("4" in t for t in texts), f"expected the command's own stdout, got {texts!r}"

    history_after = (
        session.history_path.read_text() if session.history_path.exists() else ""
    )
    assert history_after == history_before, (
        "/exec must never grow history.jsonl -- it is a display-only slash reply"
    )


@pytest.mark.asyncio
async def test_exec_rejects_shell_metacharacters_with_no_execution(tmp_path):
    """Tier 2: accept — `/exec 'ls | wc -l'` is not executed, explicit
    error on screen."""
    session = _session(tmp_path)
    cmd = REGISTRY.get("exec")
    outbox: list = []
    ctx = slash_ctx(session, recorder=outbox)
    await cmd.handler(ctx, "ls | wc -l")

    texts = [getattr(m, "text", "") for m in outbox]
    assert any("shell interpretation" in t.lower() for t in texts), texts


@pytest.mark.asyncio
async def test_exec_attach_queues_argv_and_result_for_the_next_message(tmp_path):
    """Tier 2: accept — the queued block carries shlex.join(argv), not the
    raw typed text, ahead of the result."""
    session = _session(tmp_path)
    cmd = REGISTRY.get("exec-attach")
    outbox: list = []
    ctx = slash_ctx(session, recorder=outbox)
    queue_before = list(session._pending_user_attachments)
    await cmd.handler(ctx, 'python3 -c "print(1+1)"')

    queue = session._pending_user_attachments
    added = [b for b in queue if b not in queue_before]
    assert added, "the command must queue at least one new block"
    block = added[-1]
    assert block["type"] == "text"
    assert "python3 -c" in block["text"]
    assert "exit 0" in block["text"]
    assert "2" in block["text"]

    texts = [getattr(m, "text", "") for m in outbox]
    assert any("Send your next message to include it" in t for t in texts), texts


@pytest.mark.asyncio
async def test_exec_attach_three_times_each_adds_its_own_distinguishable_block(tmp_path):
    """Tier 2: accept — /exec-attach x3 -> the next user turn drains all of
    them into ONE user message (the SAME `_pending_user_attachments` queue
    `/attachment`/`/image` already share the drain contract for — this
    test proves /exec-attach feeds that queue, not a re-derivation of the
    drain itself, which session.py's own tests already cover). Each call's
    OWN output (0, 1, 2) must be independently findable — a shared-mutable-
    state bug would collapse them into copies of the same block."""
    session = _session(tmp_path)
    cmd = REGISTRY.get("exec-attach")
    ctx = slash_ctx(session)

    for i in range(3):
        await cmd.handler(ctx, f'python3 -c "print({i})"')

    queue = session._pending_user_attachments
    assert all(b["type"] == "text" for b in queue)
    for i in range(3):
        assert any(f"print({i})" in b["text"] and f"\n{i}" in b["text"] for b in queue), (
            f"block for call {i} not found (or not distinguishable) in {queue!r}"
        )


