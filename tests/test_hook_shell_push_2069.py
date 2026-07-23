"""Tier 2: #2069 exec_capture (renamed from ``shell_push`` in #3226 Phase 4 —
naming honesty only; the argv-run mechanism is unchanged) — an argv whose
stdout is a JSON push-directive.

exec_capture is the third hook scheme (alongside template_push / exec). Its
argv is run with stdout CAPTURED (vs exec's ignored output), the stdout
is parsed fail-safe into a ResolvedPush, and — if it pushes — it travels the SAME
C/E dispatch path as template_push. The only difference from template_push is the
SOURCE of the ResolvedPush: captured stdout JSON here vs a Jinja2 render there.

Two levels, no mocks:
- The fail-safe PARSE contract (``_parse_exec_push``): the pure stdout→ResolvedPush
  function, including the missing/wrong-type/invalid-JSON skip matrix and the
  forward-compat ``session`` carry (only observable at the parse boundary — the
  dispatcher drops it at routing today, uniform with template_push).
- The DISPATCH behavior: a real HookDispatcher with a recording run_shell seam
  returning canned stdout → assert the E (inbox) / C (staging) routing, the
  capture_stdout=True flag, and the run-failure / parse-failure skips.
"""
from __future__ import annotations

import json

import pytest

from reyn.hooks.dispatcher import HookDispatcher, _parse_exec_push
from reyn.hooks.registry import HookRegistry
from reyn.hooks.render import ResolvedPush, render_push
from reyn.hooks.schema import HookDef, PushBlock

# ---------------------------------------------------------------------------
# Recording async seams — real callables (not mocks); run_shell returns canned stdout
# ---------------------------------------------------------------------------


class _Recorder:
    """A recording async callable (records (args, kwargs); returns None)."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    @property
    def kinds(self) -> list:
        return [a[0] for (a, _k) in self.calls]


class _ReturningShell:
    """A recording run_shell seam that returns a preset stdout value (str | None)."""

    def __init__(self, stdout: str | None) -> None:
        self._stdout = stdout
        self.calls: list[tuple[tuple, dict]] = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self._stdout


def _dispatcher(hooks, *, run_shell) -> tuple[HookDispatcher, dict]:
    seams = {
        "put_inbox": _Recorder(),
        "stage_next_turn_context": _Recorder(),
        "run_shell": run_shell,
    }
    disp = HookDispatcher(
        HookRegistry(hooks),
        put_inbox=seams["put_inbox"],
        stage_next_turn_context=seams["stage_next_turn_context"],
        run_shell=run_shell,
    )
    return disp, seams


# ===========================================================================
# _parse_exec_push — the fail-safe stdout→ResolvedPush parse contract
# ===========================================================================


def test_parse_valid_directive_yields_resolvedpush() -> None:
    """Tier 2: a valid JSON directive parses to a ResolvedPush with all fields."""
    stdout = json.dumps({"push_when": True, "wake": False, "message": "hi", "session": "s2"})
    rp = _parse_exec_push(stdout)
    assert rp == ResolvedPush(message="hi", wake=False, push_when=True, session="s2")


def test_parse_session_optional_defaults_none() -> None:
    """Tier 2: session is optional — absent → None (carried forward-compat)."""
    rp = _parse_exec_push(json.dumps({"push_when": True, "wake": True, "message": "hi"}))
    assert rp is not None and rp.session is None


@pytest.mark.parametrize(
    "stdout",
    [
        None,                                   # run-failure (no stdout)
        "",                                     # empty
        "   ",                                  # whitespace-only
        "not json",                             # invalid JSON
        "[1, 2, 3]",                            # JSON but not an object
        json.dumps({"wake": True, "push_when": True}),               # missing message
        json.dumps({"message": "x", "push_when": True}),             # missing wake
        json.dumps({"message": "x", "wake": True}),                  # missing push_when
        json.dumps({"message": "", "wake": True, "push_when": True}),   # empty message
        json.dumps({"message": "x", "wake": 1, "push_when": True}),     # wake not a bool
        json.dumps({"message": "x", "wake": True, "push_when": "yes"}),  # push_when not a bool
        json.dumps({"message": "x", "wake": True, "push_when": True, "session": 9}),  # session wrong type
    ],
)
def test_parse_failsafe_returns_none(stdout) -> None:
    """Tier 2: any parse failure (empty / invalid JSON / non-object / missing or
    wrong-typed required field / bad session) returns None → the dispatcher skips
    the push and the run proceeds. Never raises."""
    assert _parse_exec_push(stdout) is None


# ===========================================================================
# Dispatch behavior — exec_capture routes via the shared C/E path
# ===========================================================================


@pytest.mark.asyncio
async def test_exec_capture_wake_true_routes_to_inbox_E() -> None:
    """Tier 2: exec_capture stdout with wake=true → the same E (inbox trigger) path
    as template_push, carrying the [hook:name] attribution."""
    stdout = json.dumps({"push_when": True, "wake": True, "message": "go"})
    hook = HookDef(on="turn_end", name="cont", exec_capture=("emit.sh",))
    disp, seams = _dispatcher([hook], run_shell=_ReturningShell(stdout))

    await disp.dispatch("turn_end", {})

    assert seams["put_inbox"].kinds == ["hook"]
    (args, _k), = seams["put_inbox"].calls
    _kind, payload = args
    assert payload["wake"] is True
    assert payload["name"] == "cont"
    assert payload["text"] == "go"
    assert seams["stage_next_turn_context"].calls == []


@pytest.mark.asyncio
async def test_exec_capture_wake_false_routes_to_staging_C() -> None:
    """Tier 2: exec_capture stdout with wake=false → the C (next-turn staging) path."""
    stdout = json.dumps({"push_when": True, "wake": False, "message": "note"})
    hook = HookDef(on="turn_start", exec_capture=("emit.sh",))
    disp, seams = _dispatcher([hook], run_shell=_ReturningShell(stdout))

    await disp.dispatch("turn_start", {})

    assert seams["stage_next_turn_context"].kinds == ["hook"]
    (args, _k), = seams["stage_next_turn_context"].calls
    _kind, payload = args
    assert payload["text"] == "note"
    assert payload["name"] == "turn_start"      # attribution defaults to the point
    assert seams["put_inbox"].calls == []


@pytest.mark.asyncio
async def test_exec_capture_captures_stdout() -> None:
    """Tier 2: the exec_capture command is run with capture_stdout=True (vs exec
    which ignores output) — the observable difference at the run_shell seam."""
    shell = _ReturningShell(json.dumps({"push_when": True, "wake": True, "message": "x"}))
    hook = HookDef(on="turn_end", exec_capture=("emit.sh",))
    disp, _seams = _dispatcher([hook], run_shell=shell)

    await disp.dispatch("turn_end", {})

    (args, kwargs), = shell.calls
    assert args[0] == ("emit.sh",)                # the argv
    assert kwargs.get("capture_stdout") is True


@pytest.mark.asyncio
async def test_exec_capture_run_failure_skips_push() -> None:
    """Tier 2: a run-failure (run_shell returns None → exit-non-zero / timeout)
    skips the push — neither inbox nor staging is touched (fail-safe)."""
    hook = HookDef(on="turn_end", exec_capture=("emit.sh",))
    disp, seams = _dispatcher([hook], run_shell=_ReturningShell(None))

    await disp.dispatch("turn_end", {})

    assert seams["put_inbox"].calls == []
    assert seams["stage_next_turn_context"].calls == []


@pytest.mark.asyncio
async def test_exec_capture_parse_failure_skips_push() -> None:
    """Tier 2: stdout that is not a valid directive (invalid JSON) skips the push."""
    hook = HookDef(on="turn_end", exec_capture=("emit.sh",))
    disp, seams = _dispatcher([hook], run_shell=_ReturningShell("garbage not json"))

    await disp.dispatch("turn_end", {})

    assert seams["put_inbox"].calls == []
    assert seams["stage_next_turn_context"].calls == []


@pytest.mark.asyncio
async def test_exec_capture_push_when_false_skips_push() -> None:
    """Tier 2: a directive with push_when=false is the conditional-push guard —
    skipped via the shared _push_resolved gate (same as template_push)."""
    stdout = json.dumps({"push_when": False, "wake": True, "message": "x"})
    hook = HookDef(on="turn_end", exec_capture=("emit.sh",))
    disp, seams = _dispatcher([hook], run_shell=_ReturningShell(stdout))

    await disp.dispatch("turn_end", {})

    assert seams["put_inbox"].calls == []
    assert seams["stage_next_turn_context"].calls == []


@pytest.mark.asyncio
async def test_template_push_and_exec_capture_share_identical_push_path() -> None:
    """Tier 2: uniformity — a template_push and a exec_capture that resolve to the
    SAME ResolvedPush produce a BYTE-IDENTICAL inbox payload, proving the C/E path
    is shared and the scheme differs only in the ResolvedPush source."""
    # The directive both schemes resolve to.
    msg, wake = "unify", True
    # template_push: a static PushBlock that renders to it.
    tmpl_hook = HookDef(on="turn_end", name="u", template_push=PushBlock(message=msg, wake=wake))
    # exec_capture: stdout JSON that parses to the same ResolvedPush.
    sp_stdout = json.dumps({"push_when": True, "wake": wake, "message": msg})
    sp_hook = HookDef(on="turn_end", name="u", exec_capture=("emit.sh",))

    # sanity: the two sources yield equal ResolvedPush objects.
    assert render_push(tmpl_hook.template_push, {}) == _parse_exec_push(sp_stdout)

    t_disp, t_seams = _dispatcher([tmpl_hook], run_shell=_Recorder())
    s_disp, s_seams = _dispatcher([sp_hook], run_shell=_ReturningShell(sp_stdout))
    await t_disp.dispatch("turn_end", {})
    await s_disp.dispatch("turn_end", {})

    assert t_seams["put_inbox"].calls[0][0] == s_seams["put_inbox"].calls[0][0]
