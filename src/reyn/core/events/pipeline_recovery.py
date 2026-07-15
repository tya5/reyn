"""Pipeline recovery producer/reader seam — R4 step-boundary generation snapshots.

Implements the R4 recovery model of
``docs/proposals/reyn-pipeline-v0.9-design-resolutions.md``: the pipeline executor's
control-plane state (``run_id``, ``step_index``, ``named_stores``, ``pipe_data``,
``completed_step_results``) is recorded as a **full-state generation**, keyed by the
current durable WAL seq, after every step boundary. This mirrors the config-recovery
pattern in ``config_recovery.py``/``config_generations.py`` exactly: a generation is a
file (a base), not a truncatable WAL event, so it SURVIVES WAL truncation — the same
fix #2259 PR-1 applied to config registries applies here to pipeline runs. Reconstruct
is "latest generation on the active branch", no forward-replay (each generation is a
complete snapshot).

Layout: ``<.reyn>/pipeline/state/<run_id>/generations/gen-<seq>.json`` — one
generation directory per run, mirroring the per-agent
``.reyn/agents/<name>/state/generations/gen-<seq>.json`` convention (see
``docs/reference/runtime/reyn-dir-layout.md``).

Two entry points:
  - ``record_pipeline_state`` — the producer seam the executor calls after each step
    boundary. Side-effecting (``tool``/``shell``) steps use the AWAITED-durable path
    (``state_log.submit_durable``) to narrow the effect-done/snapshot-not-yet-durable
    window per R4; pure ``transform`` steps may use the fire-and-forget
    (``state_log.submit_durable_nowait``) path, mirroring the durable/non-durable
    split ``StateLog.append``/``append_nowait`` already draws for WAL entries.
  - ``latest_pipeline_state`` — the reader seam ``resume`` calls: the latest
    generation for a run ON THE ACTIVE WAL BRANCH (``is_active_seq``), mirroring
    ``ConfigGenerationStore.latest_active``'s SEMANTICS (active-branch membership,
    latest-wins, no forward-replay) but NOT its signature: that store multiplexes
    MANY rel_paths, so it takes a caller-hoisted ``is_active`` predicate to keep a
    multi-path reconcile from re-scanning the WAL per path. This store is scoped to
    ONE run (a handful of generation files, one run per caller), so there is no
    per-path loop to hoist out of and it takes the ``state_log`` directly.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reyn.core.events.config_recovery import reyn_root

if TYPE_CHECKING:
    from reyn.core.events.state_log import StateLog

_GEN_RE = re.compile(r"^gen-(?P<seq>\d+)\.json$")


def pipeline_state_dir(reyn_dir: "Path", run_id: str) -> "Path":
    """The generation-store directory for one pipeline run under a ``.reyn/`` dir."""
    return Path(reyn_dir) / "pipeline" / "state" / run_id / "generations"


class PipelineStateStore:
    """Directory of full control-plane-state generations for ONE pipeline run,
    keyed by seq. Mirrors ``ConfigGenerationStore`` (config_generations.py) but
    scoped to a single run (no per-path multiplexing — a run has exactly one
    control-plane state stream)."""

    def __init__(self, generations_dir: "Path") -> None:
        self._dir = Path(generations_dir)

    def _path_for(self, seq: int) -> "Path":
        return self._dir / f"gen-{seq}.json"

    def record(self, content: dict, seq: int) -> "Path":
        """Persist `content` as the generation at `seq` (atomic; idempotent per
        seq — re-recording overwrites, harmless since content is FULL-STATE)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path_for(seq)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return path

    def _seqs(self) -> "list[int]":
        if not self._dir.is_dir():
            return []
        out: list[int] = []
        for child in self._dir.iterdir():
            m = _GEN_RE.match(child.name)
            if m:
                out.append(int(m.group("seq")))
        out.sort()
        return out

    def latest_at_or_below(self, cut: int) -> "tuple[int, dict] | None":
        """The (seq, content) of the highest generation with seq <= cut, or
        None if no generation exists at or below cut."""
        seqs = [s for s in self._seqs() if s <= cut]
        if not seqs:
            return None
        seq = seqs[-1]
        content = json.loads(self._path_for(seq).read_text(encoding="utf-8"))
        return seq, content

    def latest_active(self, state_log: object) -> "tuple[int, dict] | None":
        """The (seq, content) of the highest generation on the ACTIVE WAL branch
        (``is_active_seq``), or None if no active generation exists.

        Mirrors ``ConfigGenerationStore.latest_active``'s SEMANTICS (active-branch
        membership, latest-wins, no forward-replay) but deliberately NOT its
        signature: that store takes a caller-hoisted ``is_active`` predicate because
        it multiplexes MANY rel_paths, so calling ``is_active_seq`` internally made a
        multi-path reconcile re-scan the whole WAL once per path (quadratic BY
        CONSTRUCTION — no caller-side hoist could fix it). This store is scoped to ONE
        run: ``_seqs()`` is that run's own handful of generation files and the sole
        caller (``latest_pipeline_state``) resolves a single run, so there is no
        per-path loop to hoist out of and taking the ``state_log`` stays correct."""
        from reyn.core.events.snapshot_generations import is_active_seq  # noqa: PLC0415
        seqs = [s for s in self._seqs() if is_active_seq(state_log, s)]  # type: ignore[arg-type]
        if not seqs:
            return None
        seq = seqs[-1]
        content = json.loads(self._path_for(seq).read_text(encoding="utf-8"))
        return seq, content


def _store(state_log: "StateLog", run_id: str) -> "PipelineStateStore | None":
    root = reyn_root(state_log.path)
    if root is None:
        return None
    return PipelineStateStore(pipeline_state_dir(root, run_id))


async def record_pipeline_state(
    state_log: "StateLog | None",
    run_id: str,
    control_plane_state: dict,
    *,
    durable: bool = True,
) -> None:
    """Record `control_plane_state` as the pipeline's full-state generation,
    keyed at the DURABLE WAL head (``state_log.last_durable_seq``) — the
    truncation-surviving recovery base for this run (R4). No-op when
    `state_log` is None (opt-in / non-persistence contract) or its path is
    not under a ``.reyn/`` dir.

    `durable=True` (the default; use for side-effecting `tool`/`shell` steps)
    routes the write through ``state_log.submit_durable`` — AWAITED, so the
    caller only proceeds to the next step once the snapshot is durable,
    narrowing the effect-done/snapshot-not-yet-durable crash window per R4.
    `durable=False` (pure `transform` steps) uses
    ``state_log.submit_durable_nowait`` — fire-and-forget, mirroring the
    `append`/`append_nowait` distinction `StateLog` already draws.
    """
    if state_log is None:
        return
    store = _store(state_log, run_id)
    if store is None:
        return
    seq = state_log.last_durable_seq
    content = dict(control_plane_state)

    async def _record() -> None:
        store.record(content, seq)

    if durable:
        await state_log.submit_durable(_record)
    else:
        state_log.submit_durable_nowait(_record)


def latest_pipeline_state(run_id: str, state_log: "StateLog") -> "dict[str, Any] | None":
    """The latest pipeline control-plane-state generation for `run_id` on the
    active WAL branch, or None if no generation was ever recorded (a fresh
    run — `resume` should treat this as run-from-scratch)."""
    store = _store(state_log, run_id)
    if store is None:
        return None
    latest = store.latest_active(state_log)
    if latest is None:
        return None
    _seq, content = latest
    return content


__all__ = [
    "PipelineStateStore",
    "pipeline_state_dir",
    "record_pipeline_state",
    "latest_pipeline_state",
]
