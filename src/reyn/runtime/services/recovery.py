"""#3082 Family 2 (issue link: recovery-bundle-out-of-Session): the
WAL-event/recovery construction Session used to build inline in
``__init__`` (via the now-deleted ``Session._build_recovery_bundle`` /
``_RecoveryBundle``). ``Session`` now RECEIVES the pair
(``generation_store``, ``journal``) as required constructor params instead
of building it — this module is where every construction site (production
factory + test helpers) builds them, and where the
``.reyn/agents/<agent_name>/state/snapshot.json`` default-path convention
lives exactly once so ``Session`` and its callers can share it without
duplicating the literal.
"""
from __future__ import annotations

from pathlib import Path

from reyn.core.events.snapshot_generations import SnapshotGenerationStore
from reyn.core.events.state_log import StateLog
from reyn.runtime.services.snapshot_journal import SnapshotJournal


def default_snapshot_path(agent_name: str, root: "Path | None" = None) -> Path:
    """The per-agent snapshot-file convention (PR21 / PR-refactor-session-1):
    ``<root>/agents/<agent_name>/state/snapshot.json``. Single source —
    ``Session.__init__`` and every recovery-object construction site call
    this instead of re-deriving the path literal.

    ``root`` (#3705): the caller's already-resolved state root (e.g.
    ``workspace_state_dir``, which already ends in ``.reyn`` —
    ``env_backend.py`` / ``registry_bootstrap.py`` both set it to
    ``project_root / ".reyn"``). ``None`` (every caller that has no root to
    supply) falls back to ``Path.cwd() / ".reyn"`` — the exact previous
    behavior, preserved for callers that never had a root available."""
    base = root if root is not None else Path.cwd() / ".reyn"
    return base / "agents" / agent_name / "state" / "snapshot.json"


def build_recovery(
    agent_name: str,
    snapshot_path: Path,
    state_log: "StateLog | None",
    session_id: str,
) -> tuple[SnapshotGenerationStore, SnapshotJournal]:
    """Build the WAL-event/recovery pair — ``generation_store`` (ADR-0038
    Stage 1a PITR generation store) -> ``journal`` (``SnapshotJournal``,
    wired to this same generation_store instance), constructed in that
    order because the journal is wired to this same generation_store
    instance.

    Byte-identical extraction of the sequence that used to run inline in
    ``Session.__init__`` (then in ``Session._build_recovery_bundle``, #3082
    Family 2) — same objects, same construction order, same args.
    ``snapshot_path`` is the CALLER's already-resolved path (see
    :func:`default_snapshot_path` for the shared default derivation), and
    ``state_log`` must be the caller's LOCAL value — not a `self._state_log`
    read back off an already-constructed ``Session`` — since this now runs
    strictly BEFORE ``Session`` exists.

    ADR-0038 Stage 1a: PITR generation store, kept beside snapshot.json.

    PR21: WAL + per-agent snapshot for crash recovery. state_log is
    process-shared (owned by AgentRegistry); when None, persistence is
    disabled (tests / non-chat invocation). PR-refactor-session-1 wave 2:
    persistence now flows through SnapshotJournal (extracted service).

    FP-0043 Stage 5: session_id is the conversation session id, threaded
    to the journal so every WAL append carries it."""
    generation_store = SnapshotGenerationStore(
        agent_name, snapshot_path.parent / "generations",
    )
    journal = SnapshotJournal(
        agent_name=agent_name,
        snapshot_path=snapshot_path,
        state_log=state_log,
        generation_store=generation_store,
        session_id=session_id,  # FP-0043 S5: per-session WAL routing
    )
    return generation_store, journal
