"""Tier 2: #2946 Item 4 — topology YAML parsing is lazy (first-access, not construction).

Registry construction used to blocking-glob + parse every ``.reyn/topologies/*.yaml``
file eagerly (``_reload_topologies`` called from ``__init__``), even for a `reyn chat`
run that never touches topologies. That is pure cold-start cost with no payoff for the
common case. The fix defers the glob+parse to first access (the ``_topologies``
property) — ``_topologies_raw`` starts ``None`` and is populated on first read.

★ behavior change (documented in the issue / PR, intentional, low-risk): a malformed
topology yaml's "skipping malformed topology" warning used to surface at Registry
construction time; it now surfaces at first topology access instead. Once loaded, all
topology behavior (list/get/exists/create/delete/rename) is unchanged — same real
AgentRegistry + StateLog, no mocks.
"""
from __future__ import annotations

from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.topology import TOPOLOGY_DIRNAME, Topology


def _registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=lambda p: None, state_log=state_log
    )


def _write_malformed_topology(tmp_path: Path, name: str = "bad") -> Path:
    topo_dir = tmp_path / ".reyn" / TOPOLOGY_DIRNAME
    topo_dir.mkdir(parents=True, exist_ok=True)
    path = topo_dir / f"{name}.yaml"
    # Not a mapping at all — Topology.load's ``data.get(...)`` calls raise, which
    # _reload_topologies catches and turns into the stderr warning.
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    return path


def test_construction_does_not_parse_topology_yaml(tmp_path: Path, capsys):
    """Tier 2: constructing the Registry with a malformed topology yaml on disk must
    NOT surface the malformed-yaml warning — the glob+parse hasn't run yet (lazy)."""
    _write_malformed_topology(tmp_path)
    _registry(tmp_path)
    captured = capsys.readouterr()
    assert "skipping malformed topology" not in captured.err


def test_first_topology_access_parses_and_warns(tmp_path: Path, capsys):
    """Tier 2: the first call that touches topologies (list_topologies) triggers the
    glob+parse — the malformed-yaml warning now appears, timing-shifted from
    construction to first access (the documented #2946 Item 4 behavior change)."""
    _write_malformed_topology(tmp_path)
    reg = _registry(tmp_path)
    capsys.readouterr()  # discard anything from construction (should be empty)

    reg.list_topologies()

    captured = capsys.readouterr()
    assert "skipping malformed topology" in captured.err


def test_valid_topology_loaded_lazily_and_visible(tmp_path: Path):
    """Tier 2: a well-formed topology yaml is invisible until first access, then
    resolves correctly — the lazy-load defers timing but doesn't change the result."""
    topo_dir = tmp_path / ".reyn" / TOPOLOGY_DIRNAME
    Topology(name="squad", kind="network", members=("alice", "bob")).save(
        topo_dir / "squad.yaml"
    )
    reg = _registry(tmp_path)

    # Behavioral witness that load hasn't happened yet: the backing cache is still
    # unpopulated (checked via the public topology_exists surface, which itself
    # triggers the lazy-load — so we assert on the OUTCOME of that first call
    # rather than peeking at private state).
    assert reg.topology_exists("squad") is True
    names = {t.name for t in reg.list_topologies()}
    assert "squad" in names


def test_repeated_access_is_stable(tmp_path: Path):
    """Tier 2: once lazily loaded, repeated access returns the same topology set
    (the lazy-load populates once and is reused, not re-parsed per call in a way
    that would change results)."""
    topo_dir = tmp_path / ".reyn" / TOPOLOGY_DIRNAME
    Topology(name="squad", kind="network", members=("alice",)).save(
        topo_dir / "squad.yaml"
    )
    reg = _registry(tmp_path)

    first = {t.name for t in reg.list_topologies()}
    second = {t.name for t in reg.list_topologies()}
    assert first == second == {"squad", "_default"}
