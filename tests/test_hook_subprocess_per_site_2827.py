"""Tier 1/2: #2827 part 2 — the operator's per-hook ``subprocess:`` sandbox knob.

The gap: ``hooks/shell_runner.py`` hardcoded ``allow_subprocess=False`` in the
default policy it builds, and NO production caller ever passed ``sandbox_policy``
(the dispatcher doesn't), so the floor was unconditional and **unconfigurable** —
an operator whose hook command forks (``git``/``npm``/a pyenv-shimmed bare
command) had no knob at all. ``HookDef`` had no sandbox field, and the hook path
never reaches ``resolve_sandbox_policy``, so ``reyn.yaml sandbox.policy`` does not
apply to it either.

The knob is deliberately NOT defaulted to True (contrast an MCP stdio server's
``subprocess: true`` default, #2820 part C, where the server *forks to exist* so
False hardened nothing): a hook shell's fork need is a property of the operator's
own command, so the judgment is theirs per hook (#2827) — omitting the key keeps
the False floor, byte-identical to pre-knob behaviour.

Real ``load_hooks`` / ``HookDispatcher`` / ``run_shell_hook`` and a real (non-mock)
recording SandboxBackend implementing the actual backend protocol — the same
``_StubBackend`` pattern as tests/test_op_sandboxed_exec.py. The backend is the
enforcement boundary (its own enforcement is covered by #1914/#2820); what this
module pins is the CONTRACT that the operator's per-hook declaration reaches it.
"""
from __future__ import annotations

import pytest

from reyn.hooks.loader import HookConfigError, load_hooks
from reyn.security.sandbox.backend import SandboxResult


class _RecordingBackend:
    """Real (non-mock) SandboxBackend that records each policy it is handed.

    Records per-invocation so a multi-hook run can be inspected site-by-site —
    what the per-site contract needs to be witnessed at all.
    """

    name = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], object]] = []

    def available(self) -> bool:
        return True

    async def run(self, argv, policy, *, stdin=None, cwd=None, cancel_event=None):
        self.calls.append((list(argv), policy))
        return SandboxResult(returncode=0, stdout=b"", stderr=b"")


def _policy_for(backend: "_RecordingBackend", cmd_tail: str):
    """The policy the sandbox backend was handed for the hook whose command ends
    in ``cmd_tail``. Raises if that hook never reached the backend — so a test
    asserting on it can never pass vacuously (the reason this is a lookup rather
    than a call-count pin)."""
    for argv, policy in backend.calls:
        if argv and argv[-1] == cmd_tail:
            return policy
    raise AssertionError(
        f"hook command ending {cmd_tail!r} never reached the sandbox backend "
        f"(saw: {[a for a, _ in backend.calls]})"
    )


def _load_one(raw_hook: dict):
    reg = load_hooks([raw_hook])
    hooks = reg.hooks_for("turn_end")
    assert len(hooks) == 1
    return hooks[0]


# ── loader contract (Tier 1) ─────────────────────────────────────────────────


def test_subprocess_true_parses_as_operator_will():
    """Tier 1: ``subprocess: true`` on a shell hook parses to True."""
    hook = _load_one({"on": "turn_end", "shell_exec": "echo hi", "subprocess": True})
    assert hook.subprocess is True


def test_subprocess_false_parses_as_explicit_not_omitted():
    """Tier 1: ``subprocess: false`` parses to False — DISTINCT from omitted
    (None). The explicit-vs-omitted distinction is the #2964 principle the knob
    hinges on: a bare bool could not represent it."""
    hook = _load_one({"on": "turn_end", "shell_exec": "echo hi", "subprocess": False})
    assert hook.subprocess is False


def test_subprocess_omitted_is_none_not_false():
    """Tier 1: omitting the key yields None (= keep the floor), NOT False — so
    "operator said nothing" stays distinguishable from "operator said no"."""
    hook = _load_one({"on": "turn_end", "shell_exec": "echo hi"})
    assert hook.subprocess is None


def test_subprocess_rejected_on_non_shell_hook():
    """Tier 1: eager-rejection — ``subprocess:`` on a template_push hook is a
    config ERROR, not a silent ignore. A silently-ignored security field reads as
    an applied restriction that was never applied (the #2976 model)."""
    with pytest.raises(HookConfigError, match="subprocess"):
        load_hooks([{
            "on": "turn_end",
            "template_push": {"message": "hi", "wake": False},
            "subprocess": True,
        }])


def test_subprocess_rejected_when_not_a_boolean():
    """Tier 1: a non-bool ``subprocess:`` is a config error — a truthy string
    like "false" must never be silently coerced into permitting fork."""
    with pytest.raises(HookConfigError, match="subprocess"):
        load_hooks([{"on": "turn_end", "shell_exec": "echo hi", "subprocess": "false"}])


# ── policy wiring, driven through the REAL dispatcher (Tier 2) ───────────────


async def _dispatch(raw_hooks: list[dict], backend: _RecordingBackend) -> None:
    """Drive the REAL HookDispatcher over real loaded hooks."""
    from reyn.hooks.dispatcher import HookDispatcher

    reg = load_hooks(raw_hooks)
    dispatcher = HookDispatcher(
        reg,
        put_inbox=lambda *a, **k: None,
        stage_next_turn_context=lambda *a, **k: None,
        sandbox_backend=backend,
    )
    await dispatcher.dispatch("turn_end", {})


@pytest.mark.asyncio
async def test_operator_subprocess_true_reaches_the_sandbox_policy(monkeypatch):
    """Tier 2: an operator's ``subprocess: true`` actually reaches the policy the
    sandbox backend enforces — driven through the real dispatcher → run_shell_hook
    default-policy path (the path production uses: the dispatcher never passes
    sandbox_policy, so the built default IS the effective policy)."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    backend = _RecordingBackend()

    await _dispatch(
        [{"on": "turn_end", "shell_exec": "echo hi", "subprocess": True}], backend
    )

    assert _policy_for(backend, "hi").allow_subprocess is True


@pytest.mark.asyncio
async def test_omitted_subprocess_keeps_the_false_floor(monkeypatch):
    """Tier 2: omitting the knob keeps allow_subprocess False — the pre-#2827
    behaviour is preserved for every existing hook (no silent loosening)."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    backend = _RecordingBackend()

    await _dispatch([{"on": "turn_end", "shell_exec": "echo hi"}], backend)

    policy = _policy_for(backend, "hi")
    assert policy.allow_subprocess is False
    # the floor's OTHER bounds are untouched by the knob
    assert policy.network is False
    assert policy.read_deny_paths  # the sensitive-file deny-list still applies


@pytest.mark.asyncio
async def test_subprocess_is_per_hook_not_a_global_flip(monkeypatch):
    """Tier 2: ★ the PER-SITE boundary — two hooks in ONE registry with DIFFERENT
    declarations each get their OWN policy.

    A single-hook test cannot witness "per-site": it passes identically if the knob
    were a global/process-wide flip. Putting two differing hooks in one dispatch is
    the input outside that boundary — it fails unless the value is carried per hook.
    """
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    backend = _RecordingBackend()

    await _dispatch(
        [
            {"on": "turn_end", "shell_exec": "echo forker", "subprocess": True},
            {"on": "turn_end", "shell_exec": "echo pure"},           # omitted → floor
            {"on": "turn_end", "shell_exec": "echo hardened", "subprocess": False},
        ],
        backend,
    )

    # the mapping IS the assertion: every hook's own declaration, side by side.
    by_cmd = {argv[-1]: policy.allow_subprocess for argv, policy in backend.calls}
    assert by_cmd == {"forker": True, "pure": False, "hardened": False}


@pytest.mark.asyncio
async def test_shell_push_sibling_honours_the_same_knob(monkeypatch):
    """Tier 2: the fix-class sibling — shell_push gets the knob too. The fork need
    is a property of the operator's command, not of which scheme consumes its
    stdout; wiring only shell_exec would leave half the class unfixed."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    backend = _RecordingBackend()

    await _dispatch(
        [{"on": "turn_end", "shell_push": "echo {}", "subprocess": True}], backend
    )

    assert _policy_for(backend, "{}").allow_subprocess is True
