"""Tier 2: #2993 — ConfigGenerationStore path encoding must be injective for ANY rel_path,
independent of ``_AGENT_NAME_RE`` or any other caller-side allow-list.

``_encode`` used to map ``/`` → ``__`` and RAISE when the rel_path already contained ``__``
(#2352 guard). Every real caller passed a fixed literal path (e.g. ``config/mcp.yaml``), so the
guard was never exercised — until an agent-scoped path like ``agents/<name>/hooks.yaml`` carries
an operator-chosen ``<name>`` that ``_AGENT_NAME_RE`` legally permits to contain ``__`` (e.g.
``my__agent``). The guard then raised AFTER the ``.yaml`` had already been persisted, leaving the
config mutated with NO recovery generation recorded — a silent recovery hole (#2088 would have
been the first caller to hit it).

The fix (escape ``%`` → ``%25``, then ``_`` → ``%5F``, THEN map ``/`` → ``__``; decode reverses
in the opposite order) is injective over any string, with no assumption about the input alphabet.

Real ``StateLog`` + real ``ConfigGenerationStore`` throughout (no mocks/fakes).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.events.config_generations import ConfigGenerationStore, _encode
from reyn.core.events.state_log import StateLog

# The 7 real caller rel_paths in production (grepped from every `record_config_generation` /
# `record_config_change` call site: hooks.py, cron.py, mcp_install.py/mcp_verbs.py/
# mcp_drop_server.py/plugin_install.py (all resolve to config/mcp.yaml), skill_install.py,
# presentation_install.py, pipeline_install.py, index_drop.py). None contain '_' or '%', so the
# new escape step is a no-op and every encoding stays byte-identical to the pre-#2993 output.
_REAL_CALLER_PATHS_TO_LEGACY_ENCODING = {
    "config/hooks.yaml": "config__hooks.yaml",
    "config/cron.yaml": "config__cron.yaml",
    "config/mcp.yaml": "config__mcp.yaml",
    "config/skills.yaml": "config__skills.yaml",
    "config/presentations.yaml": "config__presentations.yaml",
    "config/pipelines.yaml": "config__pipelines.yaml",
    "config/index/sources.yaml": "config__index__sources.yaml",
    # _scope_to_path returns a scope-independent ".reyn/mcp.yaml" (since #470), so the rel-path
    # seen by _encode on that call path is the bare "mcp.yaml" (no "config/" prefix) — a
    # distinct real caller path from "config/mcp.yaml" above and worth pinning on its own.
    "mcp.yaml": "mcp.yaml",
}


def test_real_caller_paths_encode_unchanged_filenames() -> None:
    """Tier 2: #2993 — for every real production rel_path (the 7 literal paths grepped from
    call sites), the new escape-then-map encoding produces the EXACT SAME filename as the old
    direct '/' → '__' map (no '_' or '%' in any real path → the escape step is a no-op). This
    pins backward compatibility: existing on-disk generation files remain readable with no
    rename / migration."""
    for rel_path, expected_encoded in _REAL_CALLER_PATHS_TO_LEGACY_ENCODING.items():
        assert _encode(rel_path) == expected_encoded, (
            f"{rel_path!r} must encode to the same filename as before #2993: "
            f"{expected_encoded!r} (no migration)"
        )


def _make_log_and_store(tmp_path: Path) -> "tuple[StateLog, ConfigGenerationStore]":
    log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    store = ConfigGenerationStore(tmp_path / ".reyn" / "config" / "generations")
    return log, store


def test_truncate_falsify_underscore_collision_pair_survives_and_stays_distinct(
    tmp_path: Path,
) -> None:
    """Tier 2: CLAUDE.md recovery-feature truncate-falsify gate — record a generation for an
    agent-scoped rel_path containing '__' (``agents/my__agent/hooks.yaml``-shaped), truncate the
    WAL below its recording seq, and confirm reconstruct still returns the correct config — the
    generation is a FILE (a base), not a truncatable WAL event, so it survives.

    Non-vacuous witness (architect requirement): the former collision pair
    ``agents/my__agent/hooks.yaml`` and ``agents/my/agent/hooks.yaml`` both flattened to the SAME
    safe-rel filename under the OLD direct '/' → '__' map. Both are recorded here, at DIFFERENT
    seqs with DIFFERENT content, survive the same truncation, and must round-trip to their OWN
    distinct content — neither overwrites nor is shadowed by the other.

    RED under the pre-#2993 code (verified by hand, Edit-only, no git checkout/stash): reverting
    ``_encode``/``_decode`` to the direct '/' → '__' map + '__' guard raises ``ValueError`` the
    moment ``path_a`` (which contains '__') is recorded — this test fails immediately instead of
    reaching the truncate/reconstruct assertions.
    """
    path_a = "agents/my__agent/hooks.yaml"
    path_b = "agents/my/agent/hooks.yaml"
    content_a = {"hooks": [{"name": "a-hook"}]}
    content_b = {"hooks": [{"name": "b-hook"}]}

    # Self-naming guard, BEFORE any record() call: under the OLD direct '/' -> '__' map these
    # two paths collide to the same safe-rel filename (both flatten to
    # "config__agents__my__agent__hooks.yaml") — that collision, not WAL truncation, is the
    # actual failure mode this test protects against. If this assert ever fails, the real bug
    # is an encoding collision, not a truncation regression.
    assert _encode(path_a) != _encode(path_b), (
        f"{path_a!r} and {path_b!r} must encode to distinct safe-rel filenames; under the "
        "old direct '/' -> '__' map they collided to the same filename — that is exactly the "
        "property this test exists to guard"
    )

    async def scenario() -> "tuple[int, int]":
        log, store = _make_log_and_store(tmp_path)

        await log.append("inbox_put", target="x", payload={"i": 1})
        await log.flush()
        seq_a = log.last_durable_seq
        store.record(path_a, content_a, seq_a)  # generation for the '__'-bearing path

        await log.append("inbox_put", target="x", payload={"i": 2})
        await log.flush()
        seq_b = log.last_durable_seq
        store.record(path_b, content_b, seq_b)  # generation for the collision-partner path

        # Advance the WAL well past both generation seqs, then truncate everything below a
        # floor ABOVE both — the WAL events at seq_a/seq_b are dropped, but the generations
        # (files) are not WAL entries and must survive.
        for i in range(3, 8):
            await log.append("inbox_put", target="x", payload={"i": i})
        await log.flush()
        await log.truncate_below(6)
        await log.flush()
        return seq_a, seq_b

    seq_a, seq_b = asyncio.run(scenario())

    # Reconstruct: a fresh store handle over the same directory (simulates process restart —
    # nothing but the on-disk generation files is consulted).
    _log2, store2 = _make_log_and_store(tmp_path)
    result_a = store2.latest_at_or_below(path_a, cut=seq_b + 100)
    result_b = store2.latest_at_or_below(path_b, cut=seq_b + 100)

    assert result_a is not None, "path_a's generation must survive WAL truncation"
    assert result_b is not None, "path_b's generation must survive WAL truncation"
    assert result_a == (seq_a, content_a), (
        f"path_a must reconstruct its OWN content, not path_b's (collision witness); "
        f"got {result_a}"
    )
    assert result_b == (seq_b, content_b), (
        f"path_b must reconstruct its OWN content, not path_a's (collision witness); "
        f"got {result_b}"
    )
