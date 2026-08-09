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

from pathlib import Path

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
from tests._support.events import collect_events

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
    (#1199) and is no longer a ``SandboxPolicy`` field at all."""
    p = SandboxPolicy()
    assert p.network is True
    assert p.write_paths == []
    assert p.read_deny_paths == []
    assert p.write_deny_paths == []
    assert p.deny_subprocess is False
    assert p.env_deny_names == []
    assert p.timeout_seconds == 60


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
    )
    assert p.network is False
    assert p.write_paths == ["/var/out"]
    assert p.read_deny_paths == ["~/.ssh"]
    assert p.write_deny_paths == ["~/.aws"]
    assert p.deny_subprocess is True
    assert p.env_deny_names == ["SECRET_TOKEN"]
    assert p.timeout_seconds == 5


# ─── 1b. SandboxedExecIROp no longer carries policy fields (#3907) ───────────


def test_op_no_longer_accepts_the_5_deleted_policy_fields():
    """Tier 2: #3907③ — the deletion-witness lead-coder asked for after
    architect's #3823 co-vet caught the twin failure mode (a field removed
    without any test witnessing the removal itself, only the surviving
    behavior). Asserts the model's OWN field set directly (`model_fields`),
    not a construction-raises probe — pydantic's v2 default is to silently
    IGNORE an unrecognized kwarg (verified: passing one does not raise), so
    a construction-time check would pass vacuously whether or not the field
    still existed. This test exists so a FUTURE accidental re-add of one of
    these fields (e.g. a merge conflict resolved the wrong way) fails LOUDLY
    here instead of silently reopening the advertised-but-ignored Tool
    Contract gap #3907 closed. `test_tool_schema_is_argv_only`
    (test_sandbox_model_completion_1339.py) witnesses the TOOL schema
    doesn't expose them; this witnesses the OP model itself doesn't carry
    them — a distinct, deeper layer the tool schema could in principle
    diverge from."""
    fields = set(SandboxedExecIROp.model_fields)
    assert fields == {"kind", "argv", "timeout_seconds", "stdin"}
    for removed_field in (
        "network", "read_paths", "write_paths", "allow_subprocess", "env_passthrough",
    ):
        assert removed_field not in fields


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
        timeout_seconds=10,
    )
    result = await execute_op(op, ctx)
    assert result["status"] == "ok"
    assert result["kind"] == "sandboxed_exec"
    assert result["backend"] in {"noop", "seatbelt", "landlock"}
    assert result["returncode"] == 0
    assert "hello" in result["stdout"]

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

    def __init__(self) -> None:
        self.received_cwd: str | None = None

    def available(self) -> bool:
        return True

    async def run(self, argv, policy, *, stdin=None, cwd=None, cancel_event=None) -> SandboxResult:
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
        timeout_seconds=10,
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
        timeout_seconds=10,
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
        timeout_seconds=10,
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
        timeout_seconds=10,
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
