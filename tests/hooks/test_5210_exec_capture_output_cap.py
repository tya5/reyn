"""Tier 2: #5210, corrected #5244 — exec_capture's returned stdout is bounded
when a caller supplies a real, context-budget-derived cap; unbounded
(byte-identical to pre-#5210) when it does not.

Architect ruling (issue #5210): the LOGGED copy of a shell-hook's stdout has
always been capped at 200 bytes (``shell_runner.py``'s own ``[:200]`` slices);
the RETURNED copy — the ``exec_capture`` push directive, whose ``message``
field lands in an inbox and ultimately a prompt — was not.

Architect's #5244 correction (own #5210 ruling, self-corrected): "never
truncate" was about the SERIALIZED byte stream — cutting the JSON string
mid-stream produces a directive that fails to parse and is indistinguishable
from a clean, deliberate no-push run at the dispatcher (the exact "two
silences" shape #5041 already closed once). That reasoning does not forbid
bounding a single structured field's VALUE after a successful parse: the
STRUCTURE survives, the caller's own parse still succeeds, and the push
still reaches its target — only the ``message`` text itself is shortened,
with a visible elision marker appended. #5210's original form checked the
raw stdout BEFORE any parse and discarded the whole directive wholesale on
overflow; coder-brown's census (28 output-capping call sites repo-wide)
found this was the only one of the 28 that discarded rather than marking
and delivering a bounded result — #5244 brings it in line with the other 27.
Recorded via the existing ``hook_shell_executed`` event's ``denial_class``
field (now ``"exec_capture_message_bounded"``, not
``"exec_capture_output_cap_exceeded"`` — the outcome changed from "no push"
to "bounded push", so the class name changed with it). The cap itself is
never invented — it is derived from a real, live context-budget source
(``RouterHostAdapter.wrap_up_output_reserve``, the SAME token budget the
force-close wrap-up call is already hard-capped to).

A directive that isn't valid JSON, or has no usable string ``message``
field, is left untouched by this bounding step even when it's huge —
bounding only ever operates on a successfully-parsed directive's
``message`` field; a malformed directive is the dispatcher's own (separate,
unchanged) fail-safe parse's problem, not this layer's.

Level 1 (``run_shell_hook`` itself): REAL subprocesses (``python -c``
one-liners), mirroring ``test_1800_hook_shell_runner.py``'s own established
pattern — no mocks.
Level 2 (``HookDispatcher`` wiring): a real ``HookDispatcher`` with a
recording ``run_shell`` seam, mirroring ``test_hook_shell_push_2069.py``'s
own established pattern for this exact seam.
Level 3 (``Session`` wiring): a real ``Session``/``AgentRegistry`` — no
mocks, per the testing policy.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import HookDef
from reyn.security.sandbox import NoopBackend, SandboxPolicy

_PY = sys.executable


def _noop_backend() -> NoopBackend:
    return NoopBackend()


def _policy(timeout: int = 10) -> SandboxPolicy:
    return SandboxPolicy(network=False, deny_subprocess=True, timeout_seconds=timeout)


# ── Level 1 — run_shell_hook itself, real subprocesses ────────────────────


@pytest.mark.asyncio
async def test_output_within_cap_is_returned_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: falsification contrast — output that fits inside the cap is
    returned exactly as before #5210 (the cap check is a rejection, not a
    modification, of anything that passes it)."""
    from reyn.hooks.shell_runner import run_shell_hook

    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    directive = {"push_when": True, "wake": True, "message": "go"}
    script = f"import json,sys; sys.stdout.write(json.dumps({directive!r}))"

    result = await run_shell_hook(
        [_PY, "-c", script],
        event_context={"event": "turn_end"},
        timeout_seconds=10,
        sandbox_backend=_noop_backend(),
        sandbox_policy=_policy(),
        allowlist_path=tmp_path / "allowlist.json",
        capture_stdout=True,
        output_token_cap=(1000, "openai/gpt-4o-mini"),
    )

    assert result is not None
    assert json.loads(result) == directive


@pytest.mark.asyncio
async def test_message_exceeding_the_cap_is_bounded_not_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog,
) -> None:
    """Tier 2: acceptance — the #5244 witness itself. A real subprocess
    whose JSON directive's ``message`` field genuinely exceeds a small
    token cap gets that field bounded IN PLACE (structure preserved, a
    visible elision marker appended, still delivered as a normal push) —
    never a whole-directive discard (#5210's original, now-corrected form)."""
    import logging

    from reyn.hooks.shell_runner import run_shell_hook
    from reyn.services.compaction.engine import estimate_tokens

    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    # A long, repetitive message inside an otherwise-valid directive — real
    # subprocess output, not a synthetic in-process value.
    long_message = "word " * 5000
    directive = {"push_when": True, "wake": True, "message": long_message}
    script = f"import json,sys; sys.stdout.write(json.dumps({directive!r}))"

    events: "list[dict]" = []

    def _emit_event(event_type: str, **data) -> None:
        events.append({"event_type": event_type, **data})

    with caplog.at_level(logging.WARNING):
        result = await run_shell_hook(
            [_PY, "-c", script],
            event_context={"event": "turn_end"},
            timeout_seconds=10,
            sandbox_backend=_noop_backend(),
            sandbox_policy=_policy(),
            allowlist_path=tmp_path / "allowlist.json",
            capture_stdout=True,
            emit_event=_emit_event,
            output_token_cap=(5, "openai/gpt-4o-mini"),
        )

    assert result is not None, (
        "a bounded message must still be DELIVERED as a push, never turned "
        "into None the way a whole-directive discard would"
    )
    parsed = json.loads(result)
    assert parsed["push_when"] is True
    assert parsed["wake"] is True
    assert "elided" in parsed["message"]
    assert estimate_tokens(parsed["message"], "openai/gpt-4o-mini") < estimate_tokens(
        long_message, "openai/gpt-4o-mini"
    ), "the bounded message must be genuinely shorter than the original"
    assert any(
        "exceeds the context-budget-derived cap" in r.message for r in caplog.records
    ), "the bounding must be logged, not silent"
    (event,) = events  # raises if not exactly 1: the verdict must ride the
    # SAME single hook_shell_executed event the run already emits, not fire
    # a second, separate one.
    assert event["event_type"] == "hook_shell_executed"
    assert event["mode"] == "exec_capture"
    assert event["returncode"] == 0
    assert event["denial_class"] == "exec_capture_message_bounded", (
        "the verdict must be recorded on the existing hook_shell_executed "
        "event's denial_class field (architect's own prescription: reuse "
        f"denial_class, no new surface needed) — got {event!r}"
    )


@pytest.mark.asyncio
async def test_malformed_or_message_less_directive_is_left_untouched_even_if_huge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: falsification contrast — bounding only ever operates on a
    successfully-parsed directive's ``message`` field. A huge, genuinely
    invalid-JSON payload with a cap set is returned UNCHANGED (not bounded,
    not rejected) — the dispatcher's own, separate, unchanged fail-safe
    parse handles that shape the same way it always has; this layer must
    not invent a second opinion about malformed input."""
    from reyn.hooks.shell_runner import run_shell_hook

    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    payload = "x" * 20000  # not valid JSON at all

    result = await run_shell_hook(
        [_PY, "-c", f"import sys; sys.stdout.write({payload!r})"],
        event_context={"event": "turn_end"},
        timeout_seconds=10,
        sandbox_backend=_noop_backend(),
        sandbox_policy=_policy(),
        allowlist_path=tmp_path / "allowlist.json",
        capture_stdout=True,
        output_token_cap=(5, "openai/gpt-4o-mini"),
    )

    assert result == payload, (
        "a directive this layer cannot parse (or that has no usable "
        "message field) must pass through unchanged — bounding is not "
        "attempted, and the dispatcher's own fail-safe parse is untouched"
    )


@pytest.mark.asyncio
async def test_no_cap_supplied_is_unbounded_matching_pre_5210(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: non-regression — every pre-#5210 caller passes no
    ``output_token_cap`` (the default, ``None``) and must see EXACTLY the
    old behavior: a large output returned whole, no rejection. #5210 adds a
    capability; it does not change default behavior for a caller that
    doesn't opt in."""
    from reyn.hooks.shell_runner import run_shell_hook

    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    payload = "x" * 20000
    script = f"import sys; sys.stdout.write({payload!r})"

    result = await run_shell_hook(
        [_PY, "-c", script],
        event_context={"event": "turn_end"},
        timeout_seconds=10,
        sandbox_backend=_noop_backend(),
        sandbox_policy=_policy(),
        allowlist_path=tmp_path / "allowlist.json",
        capture_stdout=True,
        # output_token_cap omitted -- the default
    )

    assert result == payload, (
        "the full, unmodified payload must come back — no truncation, no "
        "rejection, exactly the pre-#5210 shape for a caller that supplies "
        "no cap"
    )


@pytest.mark.asyncio
async def test_cap_supplied_changes_the_outcome_for_the_same_oversized_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: contrast — the SAME oversized message is returned whole with
    no cap supplied, and bounded when a cap is supplied — proving the
    bounding logic in ``shell_runner.py``'s own return-point is load-bearing,
    not decorative."""
    from reyn.hooks.shell_runner import run_shell_hook

    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    long_message = "word " * 5000
    directive = {"push_when": True, "wake": True, "message": long_message}
    script = f"import json,sys; sys.stdout.write(json.dumps({directive!r}))"

    no_cap_result = await run_shell_hook(
        [_PY, "-c", script],
        event_context={"event": "turn_end"},
        timeout_seconds=10,
        sandbox_backend=_noop_backend(),
        sandbox_policy=_policy(),
        allowlist_path=tmp_path / "allowlist.json",
        capture_stdout=True,
    )
    with_cap_result = await run_shell_hook(
        [_PY, "-c", script],
        event_context={"event": "turn_end"},
        timeout_seconds=10,
        sandbox_backend=_noop_backend(),
        sandbox_policy=_policy(),
        allowlist_path=tmp_path / "allowlist.json",
        capture_stdout=True,
        output_token_cap=(5, "openai/gpt-4o-mini"),
    )

    assert json.loads(no_cap_result)["message"] == long_message
    assert json.loads(with_cap_result)["message"] != long_message, (
        "the SAME oversized message must be treated differently depending "
        "on whether a cap was supplied — proving the bounding logic "
        "actually gates the returned message text"
    )


# ── Level 2 — HookDispatcher wiring ────────────────────────────────────────


class _RecordingShell:
    """A recording async ``run_shell`` seam (real callable, not a mock) —
    mirrors ``test_hook_shell_push_2069.py``'s own ``_ReturningShell``
    pattern exactly, extended to also record the kwargs it was called
    with (needed here to verify ``output_token_cap`` was actually threaded
    through, not just that SOME call happened)."""

    def __init__(self, stdout: "str | None") -> None:
        self._stdout = stdout
        self.calls: "list[tuple[tuple, dict]]" = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self._stdout


def _dispatcher(hooks: "list[HookDef]", *, run_shell, resolve_cap=None) -> HookDispatcher:
    registry = HookRegistry(list(hooks))

    async def _noop_put_inbox(*args, **kwargs) -> None:
        return None

    async def _noop_stage(*args, **kwargs) -> None:
        return None

    return HookDispatcher(
        registry,
        put_inbox=_noop_put_inbox,
        stage_next_turn_context=_noop_stage,
        run_shell=run_shell,
        resolve_exec_capture_output_cap=resolve_cap,
    )


@pytest.mark.asyncio
async def test_dispatcher_threads_the_resolved_cap_into_run_shell() -> None:
    """Tier 2: acceptance — ``HookDispatcher`` calls its live
    ``resolve_exec_capture_output_cap`` callable at DISPATCH time (not
    construction time, matching ``hook_cwd``/``hook_process_context``'s own
    established idiom) and forwards its result to ``run_shell`` as
    ``output_token_cap``."""
    shell = _RecordingShell(json.dumps({"push_when": False, "wake": False, "message": "x"}))
    hook = HookDef(on="turn_end", exec_capture=("emit.sh",))
    disp = _dispatcher(
        [hook], run_shell=shell, resolve_cap=lambda: (42, "openai/gpt-4o-mini"),
    )

    await disp.dispatch("turn_end", {})

    (_args, kwargs), = shell.calls
    assert kwargs["output_token_cap"] == (42, "openai/gpt-4o-mini")


@pytest.mark.asyncio
async def test_dispatcher_with_no_resolver_passes_none_cap() -> None:
    """Tier 2: non-regression — ``resolve_exec_capture_output_cap=None``
    (the default, every pre-#5210 ``HookDispatcher`` construction site)
    passes ``output_token_cap=None`` through unchanged, matching #5210's own
    "does not invent a fallback number" ruling — no cap source means no cap,
    not a guessed one."""
    shell = _RecordingShell(json.dumps({"push_when": False, "wake": False, "message": "x"}))
    hook = HookDef(on="turn_end", exec_capture=("emit.sh",))
    disp = _dispatcher([hook], run_shell=shell)  # resolve_cap omitted

    await disp.dispatch("turn_end", {})

    (_args, kwargs), = shell.calls
    assert kwargs["output_token_cap"] is None


# ── Level 3 — Session wiring ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_resolves_a_real_cap_from_its_own_turn_budget_engine(
    tmp_path: Path,
) -> None:
    """Tier 2: acceptance — a real ``Session`` with a resolvable model
    derives a genuine ``(cap_tokens, model)`` pair from
    ``RouterHostAdapter.wrap_up_output_reserve`` — the SAME live budget the
    force-close wrap-up call is already hard-capped to (#1092 PR-F1), not a
    second, independent computation."""
    from tests._support.agent_session import make_session

    s = make_session(agent_name="alice", snapshot_path=tmp_path / "s" / "snapshot.json")

    resolved = s._resolve_exec_capture_output_cap()

    assert resolved is not None
    cap_tokens, model = resolved
    assert isinstance(cap_tokens, int) and cap_tokens > 0
    assert model == s.model


@pytest.mark.asyncio
async def test_session_with_no_engine_resolves_no_cap(tmp_path: Path) -> None:
    """Tier 2: falsification contrast — the None-path (B review non-blocking
    note, #5229). An unresolvable model CLASS (#4573's own degrade: the
    resolver's lazy, non-essential ``TurnBudgetEngine`` build catches the
    typed ``UnresolvableModelClassError`` and returns ``None`` rather than
    crashing the session) means ``RouterHostAdapter.wrap_up_output_reserve``
    is ``None`` — ``_resolve_exec_capture_output_cap`` must degrade to
    ``None`` too, matching pre-#5210 (unbounded) behavior, not raise."""
    from tests._support.agent_session import make_session

    s = make_session(
        agent_name="alice",
        model="gemini-2.5-flash-lite",  # #4573's own unresolvable-class fixture
        snapshot_path=tmp_path / "s" / "snapshot.json",
    )

    resolved = s._resolve_exec_capture_output_cap()

    assert resolved is None
