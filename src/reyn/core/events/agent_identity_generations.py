"""Agent identity/lineage generations — per-agent full-state identity, keyed by create seq.

#2259 PR-1b. Rewind used to rebuild ``_agent_create_seq`` + ``_spawn_lineage`` from the
``agent_created`` WAL events, but the WAL is truncated below floor = min(agent applied_seq) and
``agent_created`` is NOT exempt — so a long-lived agent whose ``agent_created`` fell below the
floor lost its identity + lineage edge on rewind. A dropped lineage edge makes
``resolved_profile_for`` skip the ⊆-parent conjunct → the child runs UN-capped = capability
escalation-on-rewind (a security bug, not just data-loss).

The fix mirrors ``ConfigGenerationStore`` (config-as-snapshot, #2259 PR-1): each agent's
identity + lineage is FULL-STATE (``create_seq`` + the frozen spawn edge), so it IS a snapshot.
Each ``create_agent`` records a generation keyed by the agent's create seq; generations are
files (a base), NOT truncatable WAL events, so they SURVIVE truncation. Reconstruct as-of-cut =
the latest generation ≤ cut per agent (no forward-replay — each generation is complete).

Layout: ``<generations_dir>/<agent_name>@<seq>.json`` (agent names are already filesystem-safe
— they key the ``.reyn/agents/<name>/`` dirs).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_GEN_RE = re.compile(r"^(?P<name>.+)@(?P<seq>\d+)\.json$")


class AgentIdentityGenerationStore:
    """Directory of per-agent identity/lineage generations keyed by (agent name, create seq).

    Each generation is the COMPLETE identity state for one agent — ``create_seq`` (its stable
    identity) plus the frozen spawn edge (``spawn_parent`` + ``spawn_parent_seq``) — at the WAL
    head when it was recorded, so reconstruct is "latest generation ≤ cut" with no
    forward-replay, and a generation survives WAL truncation (it is a base, not an event).
    """

    def __init__(self, generations_dir: Path) -> None:
        self._dir = Path(generations_dir)

    def _path_for(self, name: str, seq: int) -> Path:
        return self._dir / f"{name}@{seq}.json"

    def record(
        self, name: str, *, create_seq: int, spawn_parent: "str | None",
        spawn_parent_seq: "int | None", seq: int,
    ) -> Path:
        """Persist ``name``'s identity + frozen lineage as the generation at ``seq`` (atomic;
        idempotent per (name, seq) — re-recording overwrites). The recovery TRUTH for this
        agent's identity (it survives WAL truncation, unlike the ``agent_created`` event)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path_for(name, seq)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({
                "create_seq": create_seq,
                "spawn_parent": spawn_parent,
                "spawn_parent_seq": spawn_parent_seq,
            }),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    def _entries(self) -> "dict[str, list[int]]":
        """Per agent name → sorted generation seqs present on disk."""
        out: dict[str, list[int]] = {}
        if not self._dir.is_dir():
            return out
        for child in self._dir.iterdir():
            m = _GEN_RE.match(child.name)
            if not m:
                continue
            out.setdefault(m.group("name"), []).append(int(m.group("seq")))
        for seqs in out.values():
            seqs.sort()
        return out

    def names(self) -> "list[str]":
        """All agent names that have at least one identity generation."""
        return list(self._entries().keys())

    def latest_at_or_below(self, name: str, cut: int) -> "tuple[int, dict] | None":
        """The (seq, identity-dict) of the highest generation for ``name`` with seq ≤ cut, or
        None when the agent did not exist as-of-cut (its first generation is after cut)."""
        seqs = [s for s in self._entries().get(name, ()) if s <= cut]
        if not seqs:
            return None
        seq = seqs[-1]
        data = json.loads(self._path_for(name, seq).read_text(encoding="utf-8"))
        return seq, data if isinstance(data, dict) else {}

    def prune_below(self, min_keep_seq: int) -> int:
        """Drop generations with seq < ``min_keep_seq`` — EXCEPT, per agent, the single highest
        generation < ``min_keep_seq`` (the truncation-surviving BASE: a rewind target is always
        ≥ the WAL floor, and identity-as-of-floor is the create generation from BEFORE the
        floor, so that base must survive). This is the crux carried over from
        ``ConfigGenerationStore.prune_below`` — and the difference from
        ``SnapshotGenerationStore.prune_below``, which can drop everything < floor because the
        floor itself always carries an agent snapshot. Returns the count dropped."""
        dropped = 0
        for name, seqs in self._entries().items():
            below = [s for s in seqs if s < min_keep_seq]
            if len(below) <= 1:
                continue  # nothing to drop, or only the base (keep it)
            # keep the highest below-floor seq (the base); drop the rest below it.
            for s in below[:-1]:
                p = self._path_for(name, s)
                try:
                    p.unlink()
                    dropped += 1
                except OSError:
                    pass
        return dropped


__all__ = ["AgentIdentityGenerationStore"]
