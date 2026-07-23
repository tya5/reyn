"""Tier 1/2: #3005 — the operator's expressed sandbox will is applied or refused,
never silently ignored, on the hook-shell path.

The measured gap: an operator writing ``sandbox.policy: {network: true}`` got a
hook shell with ``network=False`` and NO signal of any kind. ``resolve_sandbox_policy``
is reached only from the op path (``runtime/router_op_context.py``); the hook-shell
path never calls it, so ``allow_subprocess`` was reachable per-hook (#3003) while
``network`` / ``write_paths`` were hardcoded and the agent-level declaration was
dropped in silence.

The scoping itself is deliberate and is NOT changed here: a hook shell's floor
should not move because a run's *ops* are unsandboxed, so the agent-level policy
stays op-scoped. What this module pins is the invariant that scoping must obey —
**an operator's declaration is applied (per-hook keys) or refused out loud
(``sandbox_policy_not_applied``), never ignored** — plus its corollary, that an
explicit per-hook value is a decision and therefore silences the refusal.

Real ``load_config`` / ``load_hooks`` / ``HookDispatcher`` / ``run_shell_hook``, and
a real (non-mock) recording SandboxBackend implementing the actual backend protocol
— the ``_RecordingBackend`` pattern of tests/test_hook_subprocess_per_site_2827.py.
The backend is the enforcement boundary (its own enforcement is #1914/#2820/#2983);
what is pinned here is the CONTRACT of what reaches it and what the operator is told.
"""
from __future__ import annotations

import pytest

from reyn.config.infra import SandboxConfig
from reyn.hooks.loader import HookConfigError, load_hooks
from reyn.hooks.sandbox_scope import unapplied_policy_fields
from reyn.security.sandbox.backend import SandboxResult


class _RecordingBackend:
    """Real (non-mock) SandboxBackend recording each policy it is handed."""

    name = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], object]] = []

    def available(self) -> bool:
        return True

    async def run(self, argv, policy, *, stdin=None, cwd=None, cancel_event=None):
        self.calls.append((list(argv), policy))
        return SandboxResult(returncode=0, stdout=b"", stderr=b"")


def _policy_for(backend: "_RecordingBackend", cmd_tail: str):
    """The policy the backend was handed for the hook whose command ends in
    *cmd_tail*. Raises if that hook never reached the backend, so an assertion on
    it can never pass vacuously."""
    for argv, policy in backend.calls:
        if argv and argv[-1] == cmd_tail:
            return policy
    raise AssertionError(
        f"hook command ending {cmd_tail!r} never reached the sandbox backend "
        f"(saw: {[a for a, _ in backend.calls]})"
    )


def _load_one(raw_hook: dict):
    return load_hooks([raw_hook]).hooks_for("turn_end")[0]


async def _dispatch(raw_hooks, backend, *, config_policy=None, events=None):
    """Drive the REAL HookDispatcher over real loaded hooks, with the operator's
    agent-level policy threaded exactly as Session threads it (``sandbox_config``
    = the real ``SandboxConfig``, whose ``.policy`` is the operator's mapping)."""
    from reyn.hooks.dispatcher import HookDispatcher

    dispatcher = HookDispatcher(
        load_hooks(raw_hooks),
        put_inbox=lambda *a, **k: None,
        stage_next_turn_context=lambda *a, **k: None,
        sandbox_backend=backend,
        sandbox_config=SandboxConfig(backend="noop", policy=config_policy),
        emit_event=(lambda et, **d: events.append((et, d))) if events is not None else None,
    )
    await dispatcher.dispatch("turn_end", {})


def _not_applied(events) -> dict:
    """``{policy_field: event_payload}`` for every ``sandbox_policy_not_applied``
    audit-event — the operator-visible refusals, keyed by the axis refused."""
    return {d["policy_field"]: d for et, d in events if et == "sandbox_policy_not_applied"}


# ── loader contract (Tier 1) ─────────────────────────────────────────────────


def test_network_parses_as_operator_will():
    """Tier 1: ``network: true`` on a shell hook parses to True."""
    assert _load_one({"on": "turn_end", "exec": ["echo", "hi"], "network": True}).network is True


def test_network_omitted_is_none_not_false():
    """Tier 1: omitting ``network:`` yields None (= keep the floor), NOT False —
    "the operator said nothing" must stay distinguishable from "the operator said
    no", which is the #2964 principle the knob hinges on."""
    assert _load_one({"on": "turn_end", "exec": ["echo", "hi"]}).network is None


def test_network_false_parses_as_explicit_not_omitted():
    """Tier 1: ``network: false`` parses to False — DISTINCT from omitted (None).
    The distinction is load-bearing beyond the policy value: an explicit False is
    the operator deciding, so it also silences the not-applied refusal."""
    assert _load_one({"on": "turn_end", "exec": ["echo", "hi"], "network": False}).network is False


def test_write_paths_parses_and_explicit_empty_is_distinct_from_omitted():
    """Tier 1: ``write_paths: []`` is an explicit (empty) grant, not an omission —
    the same explicit-vs-omitted distinction, on the field where it is easiest to
    lose (an empty list is falsy)."""
    granted = _load_one({"on": "turn_end", "exec": ["echo", "hi"], "write_paths": ["/tmp/x"]})
    explicit_empty = _load_one({"on": "turn_end", "exec": ["echo", "hi"], "write_paths": []})
    omitted = _load_one({"on": "turn_end", "exec": ["echo", "hi"]})

    assert granted.write_paths == ("/tmp/x",)
    assert explicit_empty.write_paths == ()
    assert omitted.write_paths is None


@pytest.mark.parametrize("key,value", [("network", True), ("write_paths", ["/tmp/x"])])
def test_sandbox_knob_rejected_on_non_shell_hook(key, value):
    """Tier 1: eager-rejection — a per-hook sandbox key on a template_push hook is
    a config ERROR, not a silent ignore (the #2976/#3003 model). Rejecting it here
    is the same invariant this issue is about, applied one level up: the operator
    is told, rather than left reading a restriction that was never applied."""
    with pytest.raises(HookConfigError, match=key):
        load_hooks([{"on": "turn_end", "template_push": {"message": "hi", "wake": False}, key: value}])


@pytest.mark.parametrize(
    "key,value",
    [("network", "true"), ("write_paths", "/tmp/x"), ("write_paths", [""])],
)
def test_sandbox_knob_rejected_when_ill_typed(key, value):
    """Tier 1: an ill-typed knob is a config error — a truthy string like "true"
    must never be silently coerced into granting network, and a bare string must
    not be silently iterated into per-character write paths."""
    with pytest.raises(HookConfigError, match=key):
        load_hooks([{"on": "turn_end", "exec": ["echo", "hi"], key: value}])


# ── the scope map (Tier 1: pure function) ────────────────────────────────────


def test_unapplied_fields_reports_only_what_the_operator_left_silent():
    """Tier 1: the pure boundary function — an agent-level field is reported iff
    the operator wrote it AND the hook did not re-declare that axis. Key PRESENCE
    is the test, so an explicitly-declared floor value still counts as written."""
    config_policy = {"network": True, "allow_subprocess": True, "write_paths": ["/tmp/x"]}

    assert unapplied_policy_fields(config_policy, {}) == [
        ("network", "network"),
        ("allow_subprocess", "subprocess"),
        ("write_paths", "write_paths"),
    ]
    assert unapplied_policy_fields(config_policy, {"network": True, "subprocess": False}) == [
        ("write_paths", "write_paths"),
    ]
    assert unapplied_policy_fields(None, {}) == []


def test_unapplied_fields_ignores_axes_the_operator_never_wrote():
    """Tier 1: a field the operator omitted from sandbox.policy is not a refusal —
    they expressed no will about it, so there is nothing to apply or refuse. Only
    an actual declaration can be ignored."""
    assert unapplied_policy_fields({"read_deny_paths": ["~/x"]}, {}) == []


# ── per-hook reach, driven through the REAL dispatcher (Tier 2) ──────────────


@pytest.mark.asyncio
async def test_operator_network_and_write_paths_reach_the_sandbox_policy(monkeypatch):
    """Tier 2: the axes #3005 names as unreachable are now reachable — an operator's
    per-hook declaration actually reaches the policy the backend enforces, via the
    path production uses (the dispatcher never passes ``sandbox_policy``, so the
    default built in ``run_shell_hook`` IS the effective policy)."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    backend = _RecordingBackend()

    await _dispatch(
        [{
            "on": "turn_end",
            "exec": ["echo", "reach"],
            "network": True,
            "write_paths": ["/tmp/hook-said-this"],
        }],
        backend,
    )

    policy = _policy_for(backend, "reach")
    assert policy.network is True
    assert policy.write_paths == ["/tmp/hook-said-this"]


@pytest.mark.asyncio
async def test_omitted_knobs_keep_the_floor(monkeypatch):
    """Tier 2: omitting the knobs keeps the pre-#3005 floor for every existing hook
    — no silent loosening rides in with the new reach."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    backend = _RecordingBackend()

    await _dispatch([{"on": "turn_end", "exec": ["echo", "floor"]}], backend)

    policy = _policy_for(backend, "floor")
    assert policy.network is False
    assert policy.allow_subprocess is False
    assert policy.write_paths == []
    assert policy.read_deny_paths  # the sensitive-file deny-list still applies


@pytest.mark.asyncio
async def test_network_is_per_hook_not_a_global_flip(monkeypatch):
    """Tier 2: ★ the PER-SITE boundary — hooks with DIFFERENT declarations in ONE
    registry each get their OWN policy. A single-hook test passes identically if
    the knob were a global flip, so only differing siblings in one dispatch can
    witness "per-site"."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    backend = _RecordingBackend()

    await _dispatch(
        [
            {"on": "turn_end", "exec": ["echo", "online"], "network": True},
            {"on": "turn_end", "exec": ["echo", "mute"]},                     # omitted → floor
            {"on": "turn_end", "exec": ["echo", "isolated"], "network": False},
        ],
        backend,
    )

    by_cmd = {argv[-1]: policy.network for argv, policy in backend.calls}
    assert by_cmd == {"online": True, "mute": False, "isolated": False}


@pytest.mark.asyncio
async def test_shell_push_sibling_honours_the_same_knobs(monkeypatch):
    """Tier 2: the fix-class sibling — exec_capture gets the knobs too. What a
    command needs from the sandbox is a property of the command, not of which
    scheme consumes its stdout; wiring only exec would leave half the class
    unfixed."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    backend = _RecordingBackend()

    await _dispatch(
        [{"on": "turn_end", "exec_capture": ["echo", "{}"], "network": True, "write_paths": ["/tmp/p"]}],
        backend,
    )

    policy = _policy_for(backend, "{}")
    assert policy.network is True
    assert policy.write_paths == ["/tmp/p"]


# ── ★ the invariant: applied, or refused — never ignored (Tier 2) ────────────


@pytest.mark.asyncio
async def test_ignored_agent_policy_is_refused_out_loud_not_dropped(monkeypatch):
    """Tier 2: ★ THE invariant. An operator's agent-level ``sandbox.policy`` does
    not reach a hook shell (deliberate scoping, unchanged) — so every axis they
    declared and the hook did not re-declare surfaces as a ``sandbox_policy_not_applied``
    audit-event naming what they wrote, what actually applied, and the per-hook key
    that reaches it. Without this the policy was neither applied nor refused; it was
    dropped, and no signal existed from which the operator could learn either fact.
    """
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    backend, events = _RecordingBackend(), []

    await _dispatch(
        [{"on": "turn_end", "name": "silent-hook", "exec": ["echo", "silent"]}],
        backend,
        config_policy={"network": True, "allow_subprocess": True, "write_paths": ["/tmp/op"]},
        events=events,
    )

    refused = _not_applied(events)
    # the scoping is NOT changed — the hook still ran at its floor ...
    assert _policy_for(backend, "silent").network is False
    # ... but the operator can now learn every axis of it, and where to say it.
    assert {f: (d["hook_key"], d["configured"], d["effective"]) for f, d in refused.items()} == {
        "network": ("network", True, False),
        "allow_subprocess": ("subprocess", True, False),
        "write_paths": ("write_paths", ["/tmp/op"], []),
    }
    assert all(d["hook"] == "silent-hook" for d in refused.values())


@pytest.mark.asyncio
async def test_explicit_per_hook_value_is_a_decision_and_silences_the_refusal(monkeypatch):
    """Tier 2: the corollary — a hook that declares an axis has had the operator's
    will applied AT the site that consumes it, so there is nothing silent left to
    report, even when the per-hook value CONTRADICTS the agent-level one. That is
    the point of the invariant: contradiction is a decision; only silence is a
    defect. It is also the operator's way to acknowledge the boundary and mute the
    warning without changing what the hook may do."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    backend, events = _RecordingBackend(), []

    await _dispatch(
        [{
            "on": "turn_end",
            "name": "decided",
            "exec": ["echo", "decided"],
            "network": False,        # contradicts the agent-level network: true
            "subprocess": True,
            "write_paths": [],       # explicit empty grant, contradicting the agent-level one
        }],
        backend,
        config_policy={"network": True, "allow_subprocess": True, "write_paths": ["/tmp/op"]},
        events=events,
    )

    assert _not_applied(events) == {}
    policy = _policy_for(backend, "decided")
    assert policy.network is False        # the HOOK's word wins at the hook site
    assert policy.allow_subprocess is True


@pytest.mark.asyncio
async def test_no_agent_policy_means_no_refusal_noise(monkeypatch):
    """Tier 2: an operator who declared no agent-level policy expressed no will, so
    they get no refusal — the event fires on a real dropped declaration, never as
    unconditional chatter about the boundary."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    backend, events = _RecordingBackend(), []

    await _dispatch(
        [{"on": "turn_end", "exec": ["echo", "quiet"]}], backend, config_policy=None, events=events
    )

    assert _not_applied(events) == {}
    assert [et for et, _ in events] == ["hook_shell_executed"]
