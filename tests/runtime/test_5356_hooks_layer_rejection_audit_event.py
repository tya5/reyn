"""Tier 2: #5356 — a rejected per-agent/per-session hooks.yaml layer (a
write_paths self-grant, or any other HookConfigError) emits exactly one
hooks_layer_rejected audit-event, and a valid layer emits none.

A log line alone is invisible with the shipped config (band gate 2). This
site (Session._build_hook_registry's per-layer try/except) is shared with
#4501's own unknown-key rejection, which had no audit-event of its own
before this — verified directly the site had none (only logger.warning).

No mocks: a real Session, a real per-agent hooks.yaml on disk, observed
via _audit_events.add_subscriber (the same pattern test_5296 established)."""
from __future__ import annotations

from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

_AGENT = "wp-agent"


def _write_per_agent_hooks(tmp_path: Path, body: str) -> Path:
    agent_dir = tmp_path / ".reyn" / "agents" / _AGENT
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "hooks.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _make_session(tmp_path: Path) -> Session:
    return make_session(
        agent_name=_AGENT,
        state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
    )


def test_write_paths_self_grant_drops_the_layer_and_emits_one_event(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: acceptance — a per-agent hooks.yaml declaring write_paths
    (the #5356 self-grant shape) is rejected wholesale (the hook does not
    reach the registry at all) and exactly one hooks_layer_rejected event
    fires, naming the per-agent layer."""
    monkeypatch.chdir(tmp_path)
    _write_per_agent_hooks(
        tmp_path,
        "hooks:\n  - on: turn_end\n    exec: [/usr/bin/true]\n"
        "    write_paths: ['/tmp/somewhere']\n",
    )
    session = _make_session(tmp_path)

    events: list = []
    session._audit_events.add_subscriber(lambda e: events.append(e))
    registry = session._build_hook_registry({})

    assert registry.hooks_for("turn_end") == [], (
        "the rejected per-agent layer must not contribute ANY hook to the "
        "registry — the layer is dropped wholesale, not the single field"
    )
    (event,) = [e for e in events if e.type == "hooks_layer_rejected"]  # raises if not exactly 1
    assert event.data["layer"] == "per-agent"
    assert "write_paths" in event.data["reason"]


def test_a_valid_per_agent_layer_emits_no_rejection_event(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: falsification contrast — a per-agent hooks.yaml with no
    write_paths (and otherwise well-formed) loads clean and emits ZERO
    hooks_layer_rejected events. Without this, "an event fired" could mean
    nothing more than "the boot code ran," not "a real rejection happened.\""""
    monkeypatch.chdir(tmp_path)
    _write_per_agent_hooks(
        tmp_path,
        "hooks:\n  - on: turn_end\n    template_push:\n      message: ok\n      wake: true\n",
    )
    session = _make_session(tmp_path)

    events: list = []
    session._audit_events.add_subscriber(lambda e: events.append(e))
    registry = session._build_hook_registry({})

    assert len(registry.hooks_for("turn_end")) == 1, "the valid hook must load"
    rejected = [e for e in events if e.type == "hooks_layer_rejected"]
    assert rejected == []
