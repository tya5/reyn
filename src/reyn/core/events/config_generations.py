"""Config-as-snapshot generations — full-state config registries, keyed by boundary seq.

#2259 PR-1. Config recovery used to ride `config_changed` WAL events, but the WAL is
truncated below floor = min(agent applied_seq) and config was in no snapshot — so a registry
whose latest change fell below the floor was silently LOST on reconstruct (a real data-loss
bug). The fix mirrors `SnapshotGenerationStore`: config is already FULL-STATE (the whole
registry `.yaml`), so it IS a snapshot. Each config mutation records a full-state generation
keyed by the WAL head at that point; generations are files (a base), NOT truncatable WAL
events, so they SURVIVE truncation. Reconstruct as-of-cut = the latest generation ≤ cut (no
forward-replay — each generation is complete).

Layout: `<generations_dir>/<safe-rel>@<seq>.yaml`, where `<safe-rel>` encodes the registry's
`.reyn`-relative path (e.g. `config/mcp.yaml` → `config__mcp.yaml`).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_GEN_RE = re.compile(r"^(?P<rel>.+)@(?P<seq>\d+)\.yaml$")


def _encode(rel_path: str) -> str:
    """`config/mcp.yaml` → `config__mcp.yaml` (path-segment-safe single filename).

    #2352: the encoding maps ``/`` → ``__`` and is injective ONLY for paths with NO ``__``
    in any segment — ``_decode`` reverses EVERY ``__`` to ``/``, so ``_decode(_encode("a__b/c"))``
    would yield ``"a/b/c"`` ≠ the original. Config-registry rel_paths are ``__``-free by
    construction (``.reyn``-relative config file names like ``config/mcp.yaml``), so this
    enforces that invariant LOUD rather than silently colliding two distinct paths onto one
    ``<safe-rel>@<seq>.yaml`` generation file — which would return the wrong generation on
    config-rewind / time-travel (a durability keying corruption). Raising (not asserting) so
    ``-O`` cannot strip the guard. Existing generation file names are unchanged (no migration):
    every real path stays ``__``-free, so its encoding is byte-identical to before.
    """
    if "__" in rel_path:
        raise ValueError(
            f"config-generation rel_path must not contain '__' (it collides with the '/' → '__' "
            f"encoding and would round-trip wrong): {rel_path!r}"
        )
    return rel_path.replace("/", "__")


def _decode(safe_rel: str) -> str:
    return safe_rel.replace("__", "/")


class ConfigGenerationStore:
    """Directory of full config-registry generations keyed by (`.reyn`-relative path, seq).

    Each generation is the COMPLETE registry state (the whole `.yaml` content) at the WAL
    head when it was recorded, so reconstruct is "latest generation ≤ cut" with no
    forward-replay, and a generation survives WAL truncation (it is a base, not an event).
    """

    def __init__(self, generations_dir: Path) -> None:
        self._dir = Path(generations_dir)

    def _path_for(self, rel_path: str, seq: int) -> Path:
        return self._dir / f"{_encode(rel_path)}@{seq}.yaml"

    def record(self, rel_path: str, content: dict, seq: int) -> Path:
        """Persist `content` as the generation for `rel_path` at `seq` (atomic; idempotent
        per (rel_path, seq) — re-recording overwrites). The recovery TRUTH for this registry."""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path_for(rel_path, seq)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            yaml.dump(content, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    def _entries(self) -> "dict[str, list[int]]":
        """Per `.reyn`-relative path → sorted generation seqs present on disk."""
        out: dict[str, list[int]] = {}
        if not self._dir.is_dir():
            return out
        for child in self._dir.iterdir():
            m = _GEN_RE.match(child.name)
            if not m:
                continue
            out.setdefault(_decode(m.group("rel")), []).append(int(m.group("seq")))
        for seqs in out.values():
            seqs.sort()
        return out

    def paths(self) -> "list[str]":
        """All config relative-paths that have at least one generation."""
        return list(self._entries().keys())

    def latest_at_or_below(self, rel_path: str, cut: int) -> "tuple[int, dict] | None":
        """The (seq, content) of the highest generation for `rel_path` with seq ≤ cut, or
        None when the registry did not exist as-of-cut (its first generation is after cut)."""
        seqs = [s for s in self._entries().get(rel_path, ()) if s <= cut]
        if not seqs:
            return None
        seq = seqs[-1]
        content = yaml.safe_load(
            self._path_for(rel_path, seq).read_text(encoding="utf-8")
        )
        return seq, content if isinstance(content, dict) else {}

    def latest_active(self, rel_path: str, state_log: object) -> "tuple[int, dict] | None":
        """The (seq, content) of the highest generation for `rel_path` on the ACTIVE WAL
        branch (``is_active_seq``), or None when no active generation exists.

        #2405: ``latest_at_or_below(cut=N)`` has the symmetric gap — post-rewind active
        generations (seq > R > N) are excluded, reverting config to as-of-N on crash
        recovery. ``is_active_seq`` covers all three regions correctly:
        • Pre-target (seq ≤ N): ``is_active_seq=True`` → applied.
        • Abandoned branch (N < seq < R): ``is_active_seq=False`` → skipped.
        • Post-rewind active (seq > R): ``is_active_seq=True`` → applied."""
        from reyn.core.events.snapshot_generations import is_active_seq  # noqa: PLC0415
        seqs = [s for s in self._entries().get(rel_path, ()) if is_active_seq(state_log, s)]
        if not seqs:
            return None
        seq = seqs[-1]
        content = yaml.safe_load(
            self._path_for(rel_path, seq).read_text(encoding="utf-8")
        )
        return seq, content if isinstance(content, dict) else {}

    def prune_below(self, min_keep_seq: int) -> int:
        """Drop generations with seq < `min_keep_seq` — EXCEPT, per registry, the single
        highest generation < `min_keep_seq` (the truncation-surviving BASE: a rewind target
        is always ≥ the WAL floor, and config-as-of-floor may be the last change from BEFORE
        the floor, so that base must survive). This is the difference from
        `SnapshotGenerationStore.prune_below`, which can drop everything < floor because the
        floor itself always carries an agent snapshot. Returns the count dropped."""
        dropped = 0
        for rel_path, seqs in self._entries().items():
            below = [s for s in seqs if s < min_keep_seq]
            if len(below) <= 1:
                continue  # nothing to drop, or only the base (keep it)
            # keep the highest below-floor seq (the base); drop the rest below it.
            for s in below[:-1]:
                p = self._path_for(rel_path, s)
                try:
                    p.unlink()
                    dropped += 1
                except OSError:
                    pass
        return dropped


__all__ = ["ConfigGenerationStore"]
