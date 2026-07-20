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
from typing import Callable

import yaml

_GEN_RE = re.compile(r"^(?P<rel>.+)@(?P<seq>\d+)\.yaml$")


def _encode(rel_path: str) -> str:
    """`config/mcp.yaml` → `config__mcp.yaml` (path-segment-safe single filename).

    #2993: the previous encoding mapped ``/`` → ``__`` directly and RAISED when the
    rel_path already contained ``__`` (#2352 guard) — a defense that assumed every real
    caller passes a fixed literal path with no ``__`` in it. That assumption broke for
    agent-scoped config paths like ``agents/<name>/hooks.yaml`` where ``<name>`` is
    LLM/operator-chosen and validated only by ``_AGENT_NAME_RE`` (which permits ``__``,
    e.g. ``my__agent``) — the guard then raised AFTER the ``.yaml`` write had already
    landed, leaving the config mutated with no recovery generation recorded (a silent
    recovery hole, not merely a rejected write).

    The fix is a proper escape-then-map scheme, injective over ANY string with no
    assumption about the input alphabet (no regex, no allow-list): first ``%`` → ``%25``,
    then ``_`` → ``%5F`` (escaping ``%`` first so the escape sequences themselves can never
    be misread as user content — standard percent-escaping order), and only THEN
    ``/`` → ``__`` (now safe: no lone ``_`` survives to be confused with the separator).
    ``_decode`` reverses in the exact opposite order. Existing generation file names are
    unchanged (no migration): every real caller path is ``_``/``%``-free, so the escape
    step is a no-op and the encoding stays byte-identical to before.
    """
    escaped = rel_path.replace("%", "%25").replace("_", "%5F")
    return escaped.replace("/", "__")


def _decode(safe_rel: str) -> str:
    """Exact inverse of ``_encode`` — reverse the three substitutions in reverse order:
    ``__`` → ``/``, then ``%5F`` → ``_``, then ``%25`` → ``%``. This order is load-bearing:
    reversing ``__`` first can only ever re-form a genuine ``/`` (no lone ``_`` character
    survives after encode, since every literal ``_`` was escaped to ``%5F`` before the
    ``/`` → ``__`` step ran), and un-escaping ``%5F``/``%25`` last means those literal
    substrings from the ORIGINAL path (if any) are restored only after all structural
    markers are gone — so a decode never mistakes original content for a marker."""
    unslashed = safe_rel.replace("__", "/")
    unescaped_underscore = unslashed.replace("%5F", "_")
    return unescaped_underscore.replace("%25", "%")


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

    def latest_active(
        self, rel_path: str, is_active: "Callable[[int], bool]",
    ) -> "tuple[int, dict] | None":
        """The (seq, content) of the highest generation for `rel_path` on the ACTIVE WAL
        branch, or None when no active generation exists.

        ``is_active`` is the caller-supplied membership predicate (``is_active_seq``'s
        derivation is seq-independent — see ``build_active_predicate``). A caller
        reconciling MANY rel_paths in one pass (e.g.
        ``AgentRegistry._reconcile_config_as_of_cut``) MUST hoist ONE
        ``build_active_predicate(state_log)`` and reuse it here per rel_path — passing
        ``is_active_seq`` re-bound per call would re-scan the whole WAL once per
        rel_path (the #2941 sibling quadratic-cold-start shape this signature exists to
        prevent). A single-path caller may pass
        ``lambda s: is_active_seq(state_log, s)`` directly.

        #2405: ``latest_at_or_below(cut=N)`` has the symmetric gap — post-rewind active
        generations (seq > R > N) are excluded, reverting config to as-of-N on crash
        recovery. The active-branch predicate covers all three regions correctly:
        • Pre-target (seq ≤ N): active=True → applied.
        • Abandoned branch (N < seq < R): active=False → skipped.
        • Post-rewind active (seq > R): active=True → applied."""
        seqs = [s for s in self._entries().get(rel_path, ()) if is_active(s)]
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
