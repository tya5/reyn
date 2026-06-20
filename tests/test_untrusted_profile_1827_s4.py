"""Tier 2: context-auto untrusted-source abstraction (#1827 S4a).

The seam-agnostic pieces of S4: the untrusted-source taint marker, the built-in
secure default profile + its `_untrusted.yaml` override, and the marker-driven
tainted derivation. S4b wires these into the per-turn compose. The external
peer answer also stamps the marker on its history entry (the v1 seam); the
#1909 follow-up will stamp the SAME marker on external tool-results.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.security.permissions.capability_profile import (
    UNTRUSTED_META_KEY,
    UNTRUSTED_PROFILE_NAME,
    builtin_untrusted_profile,
    load_untrusted_profile,
    metas_have_untrusted,
    resolve_profile,
)
from reyn.security.permissions.effective import CapabilityAxis, ContextualLayer
from reyn.user_intervention import InterventionAnswer, UserIntervention

# ── the built-in secure default + override ──────────────────────────────────


def test_builtin_denies_side_effecting_surfaces():
    """Tier 2: the built-in untrusted profile denies write/delegate/exec/install."""
    prof = builtin_untrusted_profile()
    contextual, _ = resolve_profile(prof)
    layer = ContextualLayer(contextual)
    for denied in (
        "memory_operation__remember_shared", "multi_agent__delegate",
        "delegate_to_agent", "exec__sandboxed_exec", "mcp__install_registry",
    ):
        assert layer.allows(CapabilityAxis.TOOL, denied) is False, denied
    # a read/query tool is NOT denied by the built-in (read + reason is allowed)
    assert layer.allows(CapabilityAxis.TOOL, "recall") is True


def test_load_untrusted_returns_builtin_when_no_override(tmp_path: Path):
    """Tier 2: with no _untrusted.yaml the secure built-in default is used."""
    prof = load_untrusted_profile(tmp_path)
    assert prof.name == UNTRUSTED_PROFILE_NAME
    assert "multi_agent__delegate" in prof.tool_deny


def test_load_untrusted_honors_override(tmp_path: Path):
    """Tier 2: an operator _untrusted.yaml overrides the built-in (deliberate loosen)."""
    d = tmp_path / ".reyn" / "capability_profiles"
    d.mkdir(parents=True)
    (d / "_untrusted.yaml").write_text(
        "name: _untrusted\ntool_deny: [exec__sandboxed_exec]\n", encoding="utf-8",
    )
    prof = load_untrusted_profile(tmp_path)
    assert prof.tool_deny == ("exec__sandboxed_exec",)  # only the operator's choice


def test_load_untrusted_malformed_falls_back_to_builtin(tmp_path: Path):
    """Tier 2: a malformed override falls back to the built-in (floor not dropped)."""
    d = tmp_path / ".reyn" / "capability_profiles"
    d.mkdir(parents=True)
    (d / "_untrusted.yaml").write_text("name: [unclosed\n", encoding="utf-8")
    prof = load_untrusted_profile(tmp_path)
    assert "multi_agent__delegate" in prof.tool_deny  # built-in restored


# ── the seam-agnostic tainted derivation ────────────────────────────────────


def test_metas_have_untrusted_detects_marker():
    """Tier 2: the marker is detected regardless of which seam stamped it."""
    assert metas_have_untrusted([{"x": 1}, {UNTRUSTED_META_KEY: True}]) is True
    assert metas_have_untrusted([{"x": 1}, {"answered_skill": "s"}]) is False
    assert metas_have_untrusted([]) is False
    assert metas_have_untrusted(None) is False  # non-iterable is safe


# ── the v1 seam stamps the marker (external peer answer) ─────────────────────


class _Recorder:
    def __init__(self):
        self.history: list[dict] = []

    def append(self, role, text, ts, meta):
        self.history.append({"role": role, "text": text, "meta": meta})


def _build_handler(history):
    import tempfile

    from reyn.config.chat import ThreatScanConfig
    from reyn.core.events.event_store import EventStore
    from reyn.core.events.events import EventLog
    from reyn.runtime.services.intervention_handler import InterventionHandler
    from reyn.runtime.services.intervention_registry import InterventionRegistry
    from reyn.runtime.services.snapshot_journal import SnapshotJournal
    tmp = Path(tempfile.mkdtemp())
    events = EventLog(subscribers=[EventStore(tmp / "events")])
    journal = SnapshotJournal(agent_name="t", snapshot_path=tmp / "s.json", state_log=None)

    ref: list = []

    async def _on_announce(iv):
        if ref:
            await ref[0].announce(iv)

    registry = InterventionRegistry(on_announce=_on_announce)
    h = InterventionHandler(
        intervention_registry=registry, journal=journal, event_log=events,
        put_outbox=lambda m: asyncio.sleep(0), append_history=history.append,
        threat_scan=ThreatScanConfig(),
    )
    ref.append(h)
    return h, registry


@pytest.mark.asyncio
async def test_external_answer_stamps_untrusted_marker():
    """Tier 2: an external peer answer stamps UNTRUSTED_META_KEY on its history entry."""
    hist = _Recorder()
    h, registry = _build_handler(hist)
    iv = UserIntervention(kind="ask_user", prompt="?", run_id="r")
    iv.future = asyncio.get_running_loop().create_future()
    task = asyncio.ensure_future(h.dispatch(iv))
    from _async_wait import wait_until
    await wait_until(lambda: bool(registry.list_active()))

    await h.deliver_answer_to(iv, "the answer", external_source=True)
    await asyncio.gather(task, return_exceptions=True)

    assert hist.history, "expected a history entry"
    assert hist.history[-1]["meta"].get(UNTRUSTED_META_KEY) is True


@pytest.mark.asyncio
async def test_local_answer_does_not_stamp_marker():
    """Tier 2: a local (non-external) answer does NOT stamp the marker (falsify)."""
    hist = _Recorder()
    h, registry = _build_handler(hist)
    iv = UserIntervention(kind="ask_user", prompt="?", run_id="r")
    iv.future = asyncio.get_running_loop().create_future()
    task = asyncio.ensure_future(h.dispatch(iv))
    from _async_wait import wait_until
    await wait_until(lambda: bool(registry.list_active()))

    await h.deliver_answer_to(iv, "local", external_source=False)
    await asyncio.gather(task, return_exceptions=True)

    assert hist.history
    assert UNTRUSTED_META_KEY not in hist.history[-1]["meta"]
