"""Tier 2: sandboxed_exec op + SandboxPolicy + NoopBackend invariants (FP-0017).

Verifies:
- SandboxPolicy constructs with defaults.
- NoopBackend.available() is always True.
- NoopBackend.run(["echo", "hi"], ...) returns expected output.
- sandboxed_exec op dispatches through `execute_op` and emits P6 events.
- Wall-clock timeout enforces via subprocess timeout.
- registry: OP_KIND_MODEL_MAP includes "sandboxed_exec".

No mocks of collaborators — real EventLog, Workspace, NoopBackend, dispatcher.
"""
from __future__ import annotations

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime import execute_op
from reyn.core.op_runtime.context import OpContext
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import ALL_OP_KINDS, OP_KIND_MODEL_MAP, SandboxedExecIROp
from reyn.security.permissions.permissions import PermissionDecl
from reyn.security.sandbox import (
    NoopBackend,
    SandboxBackend,
    SandboxPolicy,
    SandboxResult,
    get_default_backend,
)
from reyn.security.sandbox import noop_backend as _noop_module
from tests._support.events import collect_events, settle
from tests._support.sandbox_backend import FULLY_ENFORCING_AXES

# ─── 1. SandboxPolicy ────────────────────────────────────────────────────────


def test_policy_defaults():
    """Tier 2: SandboxPolicy() default field values.

    ``network`` defaults to True (owner decision 2026-06-05, see
    ``DEFAULT_SANDBOX_NETWORK`` in ``reyn.security.sandbox.policy``; #3905
    aligned the dataclass default with that decision after this assertion
    pinned a STALE ``network is False`` that had drifted out of sync with
    the actual resolved policy for weeks, undetected because nothing
    compared the two). #3901 PR-B ④ (owner ruling B, full compat) then
    generalised the same posture to every other axis: ``deny_subprocess``
    False, the two deny-lists (``read_deny_paths``/``write_deny_paths``)
    empty, and ``env_deny_names`` empty (nothing withheld). ``write_paths``
    is the one field that still starts closed — it is not a permission-∩
    axis (an operator cannot know it), so #3202's opt-in-restriction
    reasoning does not carry over to it; #3901 left it at its pre-existing
    empty default. ``read_paths`` was retired in the broad-read realignment
    (#1199) and is no longer a ``SandboxPolicy`` field at all.

    ``timeout_seconds`` defaults to 120 (#3903①, owner ruling 2026-08-11:
    60 -> 120, matching industry foreground precedent — see
    ``DEFAULT_EXEC_TIMEOUT_SECONDS`` in ``reyn.security.sandbox.policy``).

    ``background_timeout_seconds``/``background_max_timeout_seconds`` are
    #3903 a-2 (owner ruling 2026-08-11): background exec gets its OWN
    default + ceiling, not the single shared field the issue named as the
    problem. The ceiling defaults to ``None`` (no cap) — an explicit owner
    choice, not a large sentinel int (see the field's own docstring in
    ``policy.py`` for why)."""
    p = SandboxPolicy()
    assert p.network is True
    assert p.write_paths == []
    assert p.read_deny_paths == []
    assert p.write_deny_paths == []
    assert p.deny_subprocess is False
    assert p.env_deny_names == []
    assert p.timeout_seconds == 120
    assert p.max_timeout_seconds == 600
    assert p.background_timeout_seconds == 3600
    assert p.background_max_timeout_seconds is None


def test_policy_custom_fields():
    """Tier 2: SandboxPolicy accepts custom field values."""
    p = SandboxPolicy(
        network=False,
        write_paths=["/var/out"],
        read_deny_paths=["~/.ssh"],
        write_deny_paths=["~/.aws"],
        deny_subprocess=True,
        env_deny_names=["SECRET_TOKEN"],
        timeout_seconds=5,
        background_timeout_seconds=3600,
        background_max_timeout_seconds=7200,
    )
    assert p.network is False
    assert p.write_paths == ["/var/out"]
    assert p.read_deny_paths == ["~/.ssh"]
    assert p.write_deny_paths == ["~/.aws"]
    assert p.deny_subprocess is True
    assert p.env_deny_names == ["SECRET_TOKEN"]
    assert p.timeout_seconds == 5
    assert p.background_timeout_seconds == 3600
    assert p.background_max_timeout_seconds == 7200


# ─── 1b. SandboxedExecIROp no longer carries policy fields (#3907) ───────────


def test_op_no_longer_accepts_the_deleted_policy_fields():
    """Tier 2: #3907③ (renamed #3962 — the removed-field set grew by one,
    see below; a name pinning "5" would itself go stale) — the
    deletion-witness lead-coder asked for after architect's #3823 co-vet
    caught the twin failure mode (a field removed without any test
    witnessing the removal itself, only the surviving behavior). Asserts
    the model's OWN field set directly (`model_fields`), not a
    construction-raises probe — pydantic's v2 default is to silently
    IGNORE an unrecognized kwarg (verified: passing one does not raise), so
    a construction-time check would pass vacuously whether or not the field
    still existed. This test exists so a FUTURE accidental re-add of one of
    these fields (e.g. a merge conflict resolved the wrong way) fails LOUDLY
    here instead of silently reopening the advertised-but-ignored Tool
    Contract gap #3907 closed. `test_tool_schema_is_argv_only`
    (test_sandbox_model_completion_1339.py) witnesses the TOOL schema
    doesn't expose them; this witnesses the OP model itself doesn't carry
    them — a distinct, deeper layer the tool schema could in principle
    diverge from.

    #3962: `timeout_seconds` joined the removed set — the same defect class
    as the other 5 (LLM-advertised, silently ignored on the real path), just
    missed by #3907's own sweep since a wall-clock cap isn't a permission
    axis.

    #3903① (2026-08-11): `timeout_seconds` came BACK — a deliberate, narrow
    reversal, not a re-opening of the gap #3962 closed. The distinguishing
    fact this test's own docstring already names: THIS time the field has a
    real reader (`op_runtime/sandboxed_exec.py`'s handler checks it against
    `SandboxPolicy.max_timeout_seconds` and applies it), so it is no longer
    advertised-but-ignored. `test_sandboxed_exec_timeout_seconds_is_actually_applied`
    below is the positive witness that distinguishes "back and read" from
    "back and ignored again"."""
    fields = set(SandboxedExecIROp.model_fields)
    assert fields == {"kind", "argv", "stdin", "timeout_seconds"}
    for removed_field in (
        "network", "read_paths", "write_paths", "allow_subprocess",
        "env_passthrough",
    ):
        assert removed_field not in fields


class _RecordingBackend:
    """Real (non-mock) SandboxBackend stub that records the exact `policy`
    object `run()` was invoked with — the positive witness for #3903①: does
    an LLM-supplied `timeout_seconds` actually reach the dispatch path, or
    is it merely accepted by the model and then ignored (the #3962 shape
    this reversal must not recreate)?"""

    name = "recording-backend"
    enforced_axes = FULLY_ENFORCING_AXES

    def __init__(self) -> None:
        self.received_policy: "SandboxPolicy | None" = None
        self.run_called = False

    def available(self) -> bool:
        return True

    async def run(self, argv, policy, *, stdin=None, cwd=None, cancel_event=None, hook_process_context=None) -> SandboxResult:
        self.run_called = True
        self.received_policy = policy
        return SandboxResult(returncode=0, stdout=b"ok", stderr=b"")


@pytest.mark.asyncio
async def test_sandboxed_exec_timeout_seconds_is_actually_applied():
    """Tier 2: #3903① positive witness — an LLM-supplied timeout_seconds
    below the operator's max_timeout_seconds is threaded all the way to the
    real backend.run() call, not just accepted by the pydantic model and
    then dropped. Value-assert only (no real waiting, per CLAUDE.md's
    testing policy — this asserts what policy object WOULD have been used,
    never actually sleeps)."""
    ctx, _events = _make_ctx()
    backend = _RecordingBackend()
    ctx.sandbox_backend = backend
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "x"], timeout_seconds=45)

    await execute_op(op, ctx)

    assert backend.run_called is True
    assert backend.received_policy is not None
    assert backend.received_policy.timeout_seconds == 45, (
        "the LLM's timeout_seconds must reach the real backend.run() call, "
        f"got {backend.received_policy.timeout_seconds}"
    )


@pytest.mark.asyncio
async def test_sandboxed_exec_timeout_seconds_none_uses_the_policy_default():
    """Tier 2: #3903① — omitting timeout_seconds (the common case) uses
    SandboxPolicy.timeout_seconds unchanged (120, the empty-dict default in
    _make_ctx's default_sandbox_policy={})."""
    ctx, _events = _make_ctx()
    backend = _RecordingBackend()
    ctx.sandbox_backend = backend
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "x"])

    await execute_op(op, ctx)

    assert backend.received_policy is not None
    assert backend.received_policy.timeout_seconds == 120


@pytest.mark.asyncio
async def test_sandboxed_exec_timeout_seconds_above_max_is_rejected_not_clamped():
    """Tier 2: #3903① — a request above the operator's max_timeout_seconds
    is a typed error (status="error", naming the actual configured max),
    never a silent clamp — a silent clamp would let the LLM believe it got
    the duration it asked for, recreating #3962's advertised-but-ignored
    shape. The backend.run() must NEVER be called — rejection happens
    before dispatch, no partial/clamped exec."""
    ctx, _events = _make_ctx()  # default_sandbox_policy={} -> max_timeout_seconds=600
    backend = _RecordingBackend()
    ctx.sandbox_backend = backend
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "x"], timeout_seconds=900)

    result = await execute_op(op, ctx)

    assert result["status"] == "error"
    assert "600" in result["error"], (
        f"the error must name the ACTUAL configured max, not a vague message: {result['error']}"
    )
    assert backend.run_called is False, "rejection must happen before dispatch, no clamped exec"


@pytest.mark.asyncio
async def test_sandboxed_exec_timeout_seconds_non_positive_is_rejected():
    """Tier 2: #3903① — a non-positive timeout_seconds (0 or negative) is
    rejected before dispatch, not passed through as a meaningless-or-inverted
    duration."""
    ctx, _events = _make_ctx()
    backend = _RecordingBackend()
    ctx.sandbox_backend = backend
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "x"], timeout_seconds=0)

    result = await execute_op(op, ctx)

    assert result["status"] == "error"
    assert backend.run_called is False


@pytest.mark.asyncio
@pytest.mark.parametrize("fractional_timeout", [0.5, 1.9])
async def test_sandboxed_exec_timeout_seconds_fractional_is_rejected_not_truncated(
    fractional_timeout: float,
):
    """Tier 2: #3903① — architect + lead-coder co-vet (#4179): a fractional
    timeout_seconds must NOT be silently truncated by int() — int(0.5) == 0,
    an IMMEDIATE timeout the LLM never asked for; int(1.9) == 1, a silently
    SHORTER duration than requested. Both are the exact "silently changed
    value" shape this whole feature exists to reject. Rejected outright,
    same posture as the over-cap/non-positive cases — backend.run() must
    never be called. Parametrized on 0.5 (the immediate-timeout case) AND
    1.9 (the merely-shortened case) — the earlier 45/900/0/120 test inputs
    were all integers and could never have caught this."""
    ctx, _events = _make_ctx()
    backend = _RecordingBackend()
    ctx.sandbox_backend = backend
    op = SandboxedExecIROp(
        kind="sandboxed_exec", argv=["/bin/echo", "x"], timeout_seconds=fractional_timeout,
    )

    result = await execute_op(op, ctx)

    assert result["status"] == "error"
    assert backend.run_called is False, (
        "a fractional timeout must be rejected before dispatch, never "
        "truncated into a (possibly zero or shortened) integer and run anyway"
    )


@pytest.mark.asyncio
async def test_sandboxed_exec_ephemeral_omitted_timeout_gets_the_background_default():
    """Tier 2: #3903 a-2 ③ — an ephemeral session's exec, with no LLM-supplied
    timeout, gets policy.background_timeout_seconds (3600, the dataclass
    default), NOT policy.timeout_seconds (120, the foreground default).
    Positive witness: reyn's OWN branch on ctx.ephemeral in
    sandboxed_exec.py, verified via the ACTUAL policy handed to the
    backend — direction claimed: "an ephemeral exec gets the background
    pair" (see OpContext.ephemeral's own docstring for the one-directional
    scope of this claim)."""
    import dataclasses

    ctx, _events = _make_ctx()
    ctx = dataclasses.replace(ctx, ephemeral=True)
    backend = _RecordingBackend()
    ctx.sandbox_backend = backend
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "x"])

    await execute_op(op, ctx)

    assert backend.received_policy is not None
    assert backend.received_policy.timeout_seconds == 3600, (
        f"ephemeral exec must get the background default (3600), got "
        f"{backend.received_policy.timeout_seconds}"
    )


@pytest.mark.asyncio
async def test_sandboxed_exec_non_ephemeral_omitted_timeout_stays_foreground():
    """Tier 2: #3903 a-2 ③ — accept-side sibling: a NON-ephemeral session's
    exec (ctx.ephemeral defaults to False) keeps the foreground default
    (120) unaffected by the new branch — same op, only ctx.ephemeral
    differs from the test above, isolating the branch actually taken."""
    ctx, _events = _make_ctx()
    backend = _RecordingBackend()
    ctx.sandbox_backend = backend
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "x"])

    await execute_op(op, ctx)

    assert backend.received_policy is not None
    assert backend.received_policy.timeout_seconds == 120


@pytest.mark.asyncio
async def test_sandboxed_exec_unattended_omitted_timeout_gets_the_background_default():
    """Tier 2: #4193 ① — the gap this issue closes. A NON-ephemeral,
    UNATTENDED session's exec (the ``session_spawn`` LLM tool's
    fire-and-forget dispatch — nobody is waiting, regardless of
    ``mode``) gets the background pair, same as an ephemeral exec does —
    proving ``not ctx.attended`` alone (independent of ``ctx.ephemeral``)
    routes to the background default. Before #4193 this ctx (ephemeral
    stays at its own False default) got the foreground pair, the exact
    bug this issue opened on."""
    import dataclasses

    ctx, _events = _make_ctx()
    ctx = dataclasses.replace(ctx, attended=False)
    backend = _RecordingBackend()
    ctx.sandbox_backend = backend
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "x"])

    await execute_op(op, ctx)

    assert backend.received_policy is not None
    assert backend.received_policy.timeout_seconds == 3600, (
        f"an unattended (fire-and-forget) exec must get the background "
        f"default (3600), got {backend.received_policy.timeout_seconds}"
    )


@pytest.mark.asyncio
async def test_sandboxed_exec_ephemeral_and_attended_still_gets_the_background_default():
    """Tier 2: #4193 ① — the falsify witness architect's own review of this
    fix required (#4193 co-vet, 2026-08-11): ``ctx.ephemeral or not
    ctx.attended`` has TWO disjuncts and neither is redundant, despite how
    it looks. The real predicate the pair approximates is "is a HUMAN
    waiting", and there are three states, not two — an agent-step leaf
    worker (``spawn_ephemeral_session`` + ``run_agent_step``'s synchronous
    ``MessageBus.request``) is the one case where BOTH ``ephemeral=True``
    AND ``attended=True`` are real at once: a PROGRAM is waiting
    (attended), but that worker's own execs still need the background
    pair (ephemeral) because no human is at the other end of that wait.

    This is the ONE case a naive simplification to ``not ctx.attended``
    alone (dropping the ``ephemeral`` disjunct as "apparently redundant")
    would break — narrowing an agent-step exec from 3600s to 120s and
    failing any agent step that itself runs a long exec. A gate that does
    not pin this exact combination does not protect the fix at all."""
    import dataclasses

    ctx, _events = _make_ctx()
    ctx = dataclasses.replace(ctx, ephemeral=True, attended=True)
    backend = _RecordingBackend()
    ctx.sandbox_backend = backend
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "x"])

    await execute_op(op, ctx)

    assert backend.received_policy is not None
    assert backend.received_policy.timeout_seconds == 3600, (
        f"an ephemeral-AND-attended exec (the agent-step leaf worker shape) "
        f"must still get the background default (3600) — a 'not attended "
        f"alone' simplification would have narrowed this to 120, got "
        f"{backend.received_policy.timeout_seconds}"
    )


@pytest.mark.asyncio
async def test_sandboxed_exec_ephemeral_llm_override_checked_against_background_max():
    """Tier 2: #3903 a-2 ③ — an ephemeral exec's LLM-supplied timeout is
    checked against policy.background_max_timeout_seconds (None = no cap
    by default), NOT policy.max_timeout_seconds (600). A request that
    would be REJECTED under the foreground ceiling (900 > 600) must
    SUCCEED under the ephemeral/background ceiling (which is unset here)
    — proves the ceiling comparison itself switched, not just the
    omitted-timeout default path above."""
    import dataclasses

    ctx, _events = _make_ctx()  # default_sandbox_policy={} -> background_max_timeout_seconds=None
    ctx = dataclasses.replace(ctx, ephemeral=True)
    backend = _RecordingBackend()
    ctx.sandbox_backend = backend
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "x"], timeout_seconds=900)

    result = await execute_op(op, ctx)

    assert result["status"] == "ok", (
        f"900s must be accepted under the (unset, no-cap) background "
        f"ceiling even though it exceeds the foreground 600s ceiling: {result}"
    )
    assert backend.received_policy is not None
    assert backend.received_policy.timeout_seconds == 900


@pytest.mark.asyncio
async def test_sandboxed_exec_ephemeral_llm_override_rejected_above_explicit_background_max():
    """Tier 2: #3903 a-2 ③ — when an operator DOES configure a real
    background_max_timeout_seconds, an ephemeral exec's LLM-supplied
    timeout above it is still rejected (the "no cap by default" shape
    above is a default, not an exemption from the ceiling mechanism
    itself)."""
    import dataclasses

    events = EventLog()
    ws = Workspace(events=events)
    ctx = OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        permission_resolver=None,
        default_sandbox_policy={"background_max_timeout_seconds": 300},
    )
    ctx = dataclasses.replace(ctx, ephemeral=True)
    backend = _RecordingBackend()
    ctx.sandbox_backend = backend
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "x"], timeout_seconds=600)

    result = await execute_op(op, ctx)

    assert result["status"] == "error"
    assert "300" in result["error"], (
        f"the error must name the actual configured background max: {result['error']}"
    )
    assert backend.run_called is False


@pytest.mark.asyncio
async def test_sandboxed_exec_respects_an_operator_narrowed_max():
    """Tier 2: #3903① — architect's conditional-approval requirement,
    verified at the dispatch layer: an operator who configured a LOWER
    max_timeout_seconds than the 600 default has that ceiling actually
    enforced — the LLM cannot widen an operator's own narrower
    configuration by requesting more than the operator allows."""
    events = EventLog()
    ws = Workspace(events=events)
    ctx = OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        permission_resolver=None,
        default_sandbox_policy={"max_timeout_seconds": 90},
    )
    backend = _RecordingBackend()
    ctx.sandbox_backend = backend
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "x"], timeout_seconds=120)

    result = await execute_op(op, ctx)

    assert result["status"] == "error"
    assert "90" in result["error"]
    assert backend.run_called is False


# ─── 2. NoopBackend ──────────────────────────────────────────────────────────


def test_noop_backend_always_available():
    """Tier 2: NoopBackend.available() returns True unconditionally."""
    assert NoopBackend().available() is True


def test_noop_backend_satisfies_protocol():
    """Tier 2: NoopBackend conforms to the SandboxBackend Protocol."""
    backend = NoopBackend()
    assert isinstance(backend, SandboxBackend)
    assert backend.name == "noop"


def test_get_default_backend_returns_protocol_conformant_backend():
    """Tier 2: get_default_backend() returns a Protocol-conformant available backend.

    Since FP-0017 Components B+C landed, the default factory is platform-aware
    (= Seatbelt on Darwin, Landlock on Linux 5.13+, Noop fallback elsewhere or
    when the platform backend reports unavailable). This test pins only the
    invariants the factory contract guarantees, not the specific backend name.
    """
    backend = get_default_backend()
    assert isinstance(backend, SandboxBackend)
    assert backend.available() is True
    assert backend.name in {"noop", "seatbelt", "landlock"}


@pytest.mark.asyncio
async def test_noop_run_echo():
    """Tier 2: NoopBackend.run(['echo', 'hi']) returns expected output."""
    backend = NoopBackend()
    # #3901 PR-B ④: env is compat by default (env_deny_names empty), so PATH
    # needs no explicit passthrough declaration anymore.
    policy = SandboxPolicy()
    result = await backend.run(["echo", "hi"], policy)
    assert isinstance(result, SandboxResult)
    assert result.returncode == 0
    assert b"hi" in result.stdout
    assert result.truncated is False


@pytest.mark.asyncio
async def test_noop_run_timeout():
    """Tier 2: NoopBackend wall-clock timeout returns returncode=-1 + message."""
    backend = NoopBackend()
    policy = SandboxPolicy(timeout_seconds=1)
    result = await backend.run(["sleep", "5"], policy)
    assert result.returncode == -1
    assert b"timed out" in result.stderr.lower() or b"timeout" in result.stderr.lower()


@pytest.mark.asyncio
async def test_noop_run_nonzero_exit():
    """Tier 2: NoopBackend returns non-zero exit code for failing commands."""
    backend = NoopBackend()
    policy = SandboxPolicy()
    # `false` exits with status 1 on POSIX
    result = await backend.run(["false"], policy)
    assert result.returncode != 0


# ─── 3. Registry wiring ──────────────────────────────────────────────────────


def test_registry_includes_sandboxed_exec():
    """Tier 2: OP_KIND_MODEL_MAP and ALL_OP_KINDS include 'sandboxed_exec'."""
    assert "sandboxed_exec" in OP_KIND_MODEL_MAP
    assert OP_KIND_MODEL_MAP["sandboxed_exec"] is SandboxedExecIROp
    assert "sandboxed_exec" in ALL_OP_KINDS


# ─── 4. Op dispatch + events ──────────────────────────────────────────────────


def _make_ctx() -> tuple[OpContext, EventLog]:
    events = EventLog()
    ws = Workspace(events=events)
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        # #3907: op no longer carries policy fields — a concrete policy is
        # required (the handler asserts non-None), mirroring what a real
        # context-building path always resolves. This file's own tests are
        # about dispatch/timeout/backend-injection/cwd, not policy content.
        default_sandbox_policy={},
    )
    return ctx, events


@pytest.mark.asyncio
async def test_dispatch_emits_started_and_completed():
    """Tier 2: sandboxed_exec dispatch through execute_op emits both P6 events.

    Backend-agnostic: the factory picks per-platform (Noop / Seatbelt / Landlock);
    we assert the dispatch contract holds (status / events / stdout) and that
    the recorded backend name matches whatever the factory returned.
    """
    ctx, events = _make_ctx()
    collected = collect_events(events)
    # /bin/echo for portability — Seatbelt's deny-default profile doesn't
    # implicitly resolve bare names from PATH on first exec.
    op = SandboxedExecIROp(
        kind="sandboxed_exec",
        argv=["/bin/echo", "hello"],
    )
    result = await execute_op(op, ctx)
    assert result["status"] == "ok"
    assert result["kind"] == "sandboxed_exec"
    assert result["backend"] in {"noop", "seatbelt", "landlock"}
    assert result["returncode"] == 0
    assert "hello" in result["stdout"]

    await settle(events)
    event_types = [e.type for e in collected]
    assert "sandboxed_exec_started" in event_types
    assert "sandboxed_exec_completed" in event_types


@pytest.mark.asyncio
async def test_dispatch_timeout_status():
    """Tier 2: sandboxed_exec dispatch surfaces timeout as status='timeout'.

    #3907: the timeout cap that actually governs a run is
    ``ctx.default_sandbox_policy``'s own ``timeout_seconds`` — NOT the op's
    (this was already true before #3907② on the real, ctx.default_sandbox_policy-set
    path; #3907② only deleted the fallback branch that was the sole place the
    op field was ever read, on a `None`-policy path #3907① established is
    unreachable in production. Found while updating this exact test: setting
    it via the op silently did nothing once the fallback was gone — same
    "LLM sets it, has zero effect" defect class #3907 tracks for the other 5
    fields, reported to lead-coder as a separate finding rather than folded
    silently into this PR's scope)."""
    events = EventLog()
    ws = Workspace(events=events)
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        default_sandbox_policy={"timeout_seconds": 1},
    )
    op = SandboxedExecIROp(
        kind="sandboxed_exec",
        argv=["/bin/sleep", "5"],
    )
    result = await execute_op(op, ctx)
    # returncode -1 surfaces as either "timeout" status; the handler maps -1 -> "timeout".
    assert result["returncode"] == -1
    assert result["status"] == "timeout"


# ─── 4b. Injected backend override (FP-0008 C7 #2) ───────────────────────────


class _StubBackend:
    """Real (non-mock) SandboxBackend stub for the injection-seam test.

    Records the ``cwd`` it was invoked with so the cwd-anchoring contract can be
    asserted behaviorally.
    """

    name = "stub-injected"
    enforced_axes = FULLY_ENFORCING_AXES

    def __init__(self) -> None:
        self.received_cwd: str | None = None

    def available(self) -> bool:
        return True

    async def run(self, argv, policy, *, stdin=None, cwd=None, cancel_event=None, hook_process_context=None) -> SandboxResult:
        self.received_cwd = cwd
        return SandboxResult(returncode=0, stdout=b"from-stub", stderr=b"")


@pytest.mark.asyncio
async def test_injected_sandbox_backend_takes_precedence():
    """Tier 2: OpContext.sandbox_backend instance overrides name-based selection."""
    ctx, _events = _make_ctx()
    ctx.sandbox_backend = _StubBackend()
    op = SandboxedExecIROp(
        kind="sandboxed_exec",
        argv=["/bin/echo", "x"],
    )
    result = await execute_op(op, ctx)
    # The injected instance ran — not the platform default (e.g. seatbelt here).
    assert result["backend"] == "stub-injected"
    assert result["stdout"] == "from-stub"


# ─── 4c. cwd anchoring (parity with shell op, FP-0008 PR-I) ──────────────────


@pytest.mark.asyncio
async def test_handler_passes_workspace_base_dir_as_cwd():
    """Tier 2: the handler anchors cwd to workspace.base_dir on backend.run.

    Parity with the legacy `shell` op (FP-0008 PR-I). Asserted behaviorally via
    a recording stub so repo-relative git/pytest run in the repo root.
    """
    ctx, _events = _make_ctx()
    stub = _StubBackend()
    ctx.sandbox_backend = stub
    op = SandboxedExecIROp(
        kind="sandboxed_exec", argv=["/bin/echo", "x"],
    )
    await execute_op(op, ctx)
    assert stub.received_cwd == str(ctx.workspace.base_dir)


@pytest.mark.asyncio
async def test_default_backend_actually_runs_in_workspace_cwd(tmp_path):
    """Tier 2: the default backend's subprocess runs with cwd=workspace.base_dir.

    End-to-end proof (not just handler threading): /bin/pwd executed via the
    real platform default backend reports the workspace base_dir. Uses realpath
    on both sides to tolerate macOS /var → /private/var symlink resolution.
    """
    import os

    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext
    from reyn.data.workspace.workspace import Workspace

    events = EventLog()
    ws = Workspace(events=events, base_dir=tmp_path)
    ctx = OpContext(
        workspace=ws, events=events,
        permission_decl=PermissionDecl(), permission_resolver=None,
        # #3907: op no longer carries policy fields — a concrete policy is
        # required. Read has no allowlist concept at all (#1199 broad-read
        # realignment; this test's OLD `read_paths=` kwarg was already inert
        # before #3907 — the pre-#3907 op-fields fallback branch never read
        # it either, only network/write_paths/allow_subprocess/timeout).
        default_sandbox_policy={},
    )
    op = SandboxedExecIROp(
        kind="sandboxed_exec", argv=["/bin/pwd"],
    )
    result = await execute_op(op, ctx)
    assert result["returncode"] == 0, f"/bin/pwd failed: {result!r}"
    reported = os.path.realpath(result["stdout"].strip())
    assert reported == os.path.realpath(str(ws.base_dir)), (
        f"sandboxed_exec ran in {reported!r}, expected workspace base_dir "
        f"{ws.base_dir!r}"
    )


@pytest.mark.asyncio
async def test_no_injected_backend_falls_back_to_default():
    """Tier 2: with no injected backend, sandboxed_exec uses the platform default."""
    ctx, _events = _make_ctx()
    assert ctx.sandbox_backend is None
    op = SandboxedExecIROp(
        kind="sandboxed_exec",
        argv=["/bin/echo", "x"],
    )
    result = await execute_op(op, ctx)
    assert result["backend"] in {"noop", "seatbelt", "landlock"}


# ─── 5. Noop one-shot warning ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_noop_emits_warning_once(caplog):
    """Tier 2: NoopBackend emits the no-enforcement WARN exactly once per process."""
    _noop_module._reset_warning_for_tests()
    backend = NoopBackend()
    policy = SandboxPolicy()

    import logging
    with caplog.at_level(logging.WARNING, logger="reyn.security.sandbox.noop_backend"):
        await backend.run(["echo", "1"], policy)
        await backend.run(["echo", "2"], policy)

    warns = [r for r in caplog.records if "no isolation enforced" in r.message]
    (warn,) = warns  # exactly one warning: unpacking raises ValueError if not
