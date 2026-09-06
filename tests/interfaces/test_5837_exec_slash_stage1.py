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
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.slash import slash_ctx


def _session(tmp_path: Path, *, sandbox_config: "SandboxConfig | None" = None) -> Session:
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
async def test_exec_is_denied_end_to_end_when_contextually_narrowed(tmp_path):
    """Tier 2: #5841 accept ② -- "同じ条件で /exec も同じ seam で拒否される".
    A real Session whose CapabilityScope.contextual_permission denies
    "exec" makes `/exec` (the REAL registered command, not a
    reproduction) refuse to run at all -- through the SAME dispatch_tool
    seam #5843 wired it into, no separate check this module built.

    Control arm in the SAME test (not a separate one) -- a session with
    NO narrowing runs the identical command successfully, so this isn't
    "denies everything now"."""
    from reyn.runtime.session_params import CapabilityScope
    from reyn.security.permissions.effective import ContextualPermission

    denied_session = make_session(
        agent_name="alpha-denied",
        state_log=StateLog(tmp_path / "denied.wal"),
        snapshot_path=tmp_path / "denied-snap.json",
        workspace_state_dir=tmp_path / "denied",
        capability_scope=CapabilityScope(
            contextual_permission=ContextualPermission(tool_deny=frozenset({"exec"})),
        ),
    )
    outbox: list = []
    ctx = slash_ctx(denied_session, recorder=outbox)
    await REGISTRY.get("exec").handler(ctx, 'python3 -c "print(1)"')
    texts = [getattr(m, "text", "") for m in outbox]
    assert any("exec denied" in t.lower() for t in texts), (
        f"expected a denial reply for the contextually-narrowed session, got {texts!r}"
    )
    assert not any("exit 0" in t for t in texts), texts

    # Control arm: an UNnarrowed session runs the identical command fine.
    allowed_session = _session(tmp_path / "allowed")
    outbox2: list = []
    ctx2 = slash_ctx(allowed_session, recorder=outbox2)
    await REGISTRY.get("exec").handler(ctx2, 'python3 -c "print(1)"')
    texts2 = [getattr(m, "text", "") for m in outbox2]
    assert any("exit 0" in t for t in texts2), texts2


@pytest.mark.asyncio
async def test_exec_emits_tool_called_with_operator_caller_kind(tmp_path):
    """Tier 2: accept (#5843 BLOCKING ①) — caller_kind="operator" must
    reach a REAL audit-event, not just sit on an object nothing reads.
    dispatch_tool (core/dispatch/dispatcher.py) is the ONE place that
    emits tool_called/tool_returned carrying caller_kind — a direct call
    to handle_sandboxed_exec (the earlier shape of this module) bypassed
    it entirely, leaving zero audit-events with caller_kind at all.

    Falsify note (verified in-file, Edit-only, then reverted): reverting
    _run_exec's dispatch_tool call back to a direct handle_sandboxed_exec
    call makes this RED -- no tool_called event is emitted at all."""
    from tests._support.events import collect_events, settle

    session = _session(tmp_path)
    events = collect_events(session._audit_events)

    cmd = REGISTRY.get("exec")
    ctx = slash_ctx(session)
    await cmd.handler(ctx, 'python3 -c "print(1)"')
    await settle(session._audit_events)

    tool_called = [e for e in events if e.type == "tool_called" and e.data.get("tool") == "exec"]
    assert tool_called, f"no tool_called event for exec -- got types {[e.type for e in events]!r}"
    assert tool_called[0].data.get("caller_kind") == "operator", (
        f"/exec's own audit trail must attribute caller_kind='operator', "
        f"got {tool_called[0].data!r}"
    )

    tool_returned = [e for e in events if e.type == "tool_returned" and e.data.get("tool") == "exec"]
    assert tool_returned, "no tool_returned event for a successful exec run"
    assert tool_returned[0].data.get("caller_kind") == "operator"


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
    queue_before = session.pending_user_attachments
    await cmd.handler(ctx, 'python3 -c "print(1+1)"')

    queue = session.pending_user_attachments
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

    queue = session.pending_user_attachments
    assert all(b["type"] == "text" for b in queue)
    for i in range(3):
        assert any(f"print({i})" in b["text"] and f"\n{i}" in b["text"] for b in queue), (
            f"block for call {i} not found (or not distinguishable) in {queue!r}"
        )


@pytest.mark.asyncio
async def test_exec_attach_three_times_then_submit_drains_to_one_message_one_completion(
    tmp_path,
):
    """Tier 2: accept (architect's own closing prescription, #5843 BLOCKING
    ②) — "queued" is not a witness for "drained". A plain ``{"type":
    "text", ...}`` block is a NEW shape in `_pending_user_attachments`
    (existing producers, `/attachment`/`/image`, only ever queue a
    path-ref `file`/`image` block) — this drives a REAL Session end to
    end to confirm it is not silently filtered out anywhere between the
    queue and the actual outbound litellm call.

    Real Session, real inbox processing (`run_one_iteration`), a real
    (stubbed-at-the-litellm-boundary, #5103's own established
    fixture-less pattern) completion — captures the EXACT `messages`
    litellm.acompletion is called with, one layer beneath `LLMStub`'s own
    fixed response so the call itself still returns a normal, harmless
    completion (no gating/tool-call machinery needed for this witness).

    Asserts BOTH halves architect named: (1) the durable history entry
    (one `role="user"` turn) carries the "go" text AND all 3 `$ ...`
    markers, and (2) the actual message litellm receives ALSO carries
    them — proving the block survives history persistence AND the wire-
    format conversion, not just the first of the two.
    """
    import litellm

    from reyn.dev.testing.llm_stub import LLMStub

    session = _session(tmp_path)
    cmd = REGISTRY.get("exec-attach")
    ctx = slash_ctx(session)

    for i in range(3):
        await cmd.handler(ctx, f'python3 -c "print({i})"')

    stub = LLMStub()
    stub.install()
    captured_messages: "list[list[dict]]" = []
    original_acompletion = litellm.acompletion

    async def _capturing_acompletion(*, model, messages, **kwargs):
        captured_messages.append(messages)
        return await original_acompletion(model=model, messages=messages, **kwargs)

    litellm.acompletion = _capturing_acompletion
    try:
        await session.submit_user_text("go")
        await session.run_one_iteration()
    finally:
        stub.restore()
        litellm.acompletion = original_acompletion

    # (1) the durable history entry.
    user_entries = [m for m in session.history if m.role == "user"]
    (entry,) = [m for m in user_entries if _content_text(m.content) and "go" in _content_text(m.content)]
    entry_text = _content_text(entry.content)
    assert "go" in entry_text
    for i in range(3):
        assert f"print({i})" in entry_text, (
            f"call {i}'s own $ ... marker missing from the persisted history "
            f"entry: {entry_text!r}"
        )

    # (2) the ACTUAL message the model call received — ONE completion,
    # carrying all 3 (architect's own "3回 -> 1通 -> completion 1回").
    assert captured_messages, "litellm.acompletion was never called"
    (sent_messages,) = captured_messages  # exactly one completion
    sent_texts = " ".join(_content_text(m.get("content")) for m in sent_messages)
    assert "go" in sent_texts
    for i in range(3):
        assert f"print({i})" in sent_texts, (
            f"call {i}'s own $ ... marker missing from the message actually "
            f"sent to the model: {sent_texts!r}"
        )


def _content_text(content: "str | list[dict] | None") -> str:
    """Flatten a ChatMessage/litellm-message ``content`` (a plain string,
    or a list of ``{"type": "text", "text": ...}``-shaped blocks) into one
    searchable string — mirrors how both the history entry and the
    litellm-wire message represent multi-block content."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


