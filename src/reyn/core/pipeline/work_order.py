"""Pipeline work-order + run-lifecycle files (IS-2 async driver-session recovery core).

A pipeline driver-session (D案) is *born with its work-order*: everything
needed to run — or, after a crash, to RE-CREATE the driver-session and resume
— lives in ``.reyn/pipeline/state/<run_id>/invocation.json``, written at
spawn, BEFORE step 0 runs. It is a FILE, not a WAL event, so like the R4
``gen-<seq>.json`` step snapshots next to it (``generations/``, see
``reyn.core.events.pipeline_recovery``) it survives WAL truncation — the
CLAUDE.md recovery-gate requirement. Three files make up a run's lifecycle
under its run dir:

- ``invocation.json`` — the :class:`PipelineWorkOrder`: the full serialized
  pipeline (``reyn.core.pipeline.serde``), the seed input, the reply
  address, the (agent, sid) of the driver-session itself (so recovery can
  re-create the session from scratch when it crashed before its own session
  snapshot ever landed), and the WAL seq at spawn (the rewind guard's
  default-open predicate — see ``AgentRegistry._rewake_pipeline_runs``).
- ``attempts.json`` — the poison-pipeline cap: a monotonic resume-attempt
  counter the recovery scan bumps durably BEFORE each re-wake. A run whose
  resume crashes the process every restart increments this on every scan;
  once past the driver's cap the driver terminal-fails the run instead of
  resuming — bounded by construction (monotonic counter + finite cap),
  never a restart crash-loop amplifier.
- ``result.json`` — the TERMINAL marker, written by the driver only AFTER
  the result was posted to the reply address. Terminal therefore means
  "result delivered", NOT "all steps done": a crash between the last step
  and delivery leaves no marker, so recovery re-wakes, the driver replays
  every completed step from the R4 snapshot (exactly-once execution) and
  re-delivers. Net contract: **step execution exactly-once, result delivery
  at-least-once** (a consumer can dedup by ``run_id``). Its presence also
  lets the startup scan skip terminal run dirs with one ``stat`` — no WAL
  read per historical run.

All writes are atomic (tmp + rename), mirroring
``PipelineStateStore.record``.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from reyn.core.events.pipeline_recovery import pipeline_state_dir

_INVOCATION_FILE = "invocation.json"
_RESULT_FILE = "result.json"
_ATTEMPTS_FILE = "attempts.json"


def pipeline_run_dir(reyn_dir: "Path", run_id: str) -> "Path":
    """The run-lifecycle directory for one pipeline run — the parent of the R4
    generation store (single source: derived from ``pipeline_state_dir``)."""
    return pipeline_state_dir(reyn_dir, run_id).parent


@dataclass(frozen=True)
class PipelineWorkOrder:
    """The driver-session's birth state — see the module docstring.

    ``reply_to_agent``/``reply_to_sid`` name the invoker session the result is
    posted back to; ``driver_agent``/``driver_sid`` name the driver-session
    itself (recovery re-creates it from these when the session record is
    gone); ``spawn_seq`` is the WAL head at spawn time (``None`` in
    no-WAL/test contexts — the rewind guard then defaults open)."""

    run_id: str
    pipeline_name: str
    pipeline: "dict[str, Any]"  # serde.pipeline_to_dict shape
    input: "dict[str, Any] | None"
    reply_to_agent: str
    reply_to_sid: str
    driver_agent: str
    driver_sid: str
    spawn_seq: "int | None" = None


def _atomic_write(path: "Path", content: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(content, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def write_invocation(run_dir: "Path", work_order: PipelineWorkOrder) -> "Path":
    """Persist the work-order as ``invocation.json`` (atomic)."""
    path = Path(run_dir) / _INVOCATION_FILE
    _atomic_write(path, asdict(work_order))
    return path


def load_invocation(run_dir: "Path") -> "PipelineWorkOrder | None":
    """Read the run dir's work-order, or ``None`` when absent/corrupt (a
    corrupt invocation cannot be resumed — the caller logs and skips)."""
    path = Path(run_dir) / _INVOCATION_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PipelineWorkOrder(**data)
    except (ValueError, TypeError):
        return None


def write_result(
    run_dir: "Path", *, status: str, delivered: bool,
    output: Any = None, error: "str | None" = None,
    named_stores: "dict | None" = None,
) -> "Path":
    """Write the terminal marker (atomic). ``delivered=False`` records a
    permanently-undeliverable result (reply target gone) — still terminal, so
    a vanished consumer can never turn the run into an infinite re-wake.

    ``named_stores`` (IS-6) carries the run's final named-store map so a SYNC
    attached caller can reconstruct the full IS-1 tool result
    (``{run_id, output, named_stores}``) from the marker alone, in-band, without
    the reply inbox. ``None`` on non-ok terminals (a failed/cancelled run has no
    meaningful store snapshot to surface). The marker is a TERMINAL stop-signal,
    NOT a recovery source (the R4 generations are), so extending it does not
    touch the truncation-survival contract."""
    path = Path(run_dir) / _RESULT_FILE
    _atomic_write(path, {
        "status": status, "delivered": delivered, "output": output,
        "error": error, "named_stores": named_stores,
    })
    return path


def has_result(run_dir: "Path") -> bool:
    """True when the run reached terminal (result delivered / terminal-failed /
    cancelled — any terminal marker halts the recovery re-wake scan)."""
    return (Path(run_dir) / _RESULT_FILE).is_file()


def read_result(run_dir: "Path") -> "dict | None":
    """Read the terminal marker written by ``write_result``, or ``None`` when the
    run has not reached terminal (no marker yet) or the marker is unreadable.

    Symmetric with :func:`write_result` — the read seam a SYNC attached caller
    (IS-6 ``run_pipeline``) uses to collect its run's outcome after it has pumped
    the driver-session to quiescence: the driver delivers the value in-band via
    this file (``{status, delivered, output, error}``), not through the reply
    inbox (that path is suppressed on the attached happy path so the attached
    caller does not also get a redundant ``pipeline_result`` turn). A corrupt /
    partially-written marker reads as ``None`` (the caller treats it as
    not-yet-terminal, same defensive shape as ``load_invocation``)."""
    path = Path(run_dir) / _RESULT_FILE
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, TypeError):
        return None


def read_resume_attempts(run_dir: "Path") -> int:
    """The persisted recovery re-wake count for this run (0 when never re-woken)."""
    path = Path(run_dir) / _ATTEMPTS_FILE
    if not path.is_file():
        return 0
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("attempts", 0))
    except (ValueError, TypeError):
        return 0


def bump_resume_attempts(run_dir: "Path") -> int:
    """Durably increment the resume-attempt counter BEFORE a recovery re-wake
    (so a resume that crashes the process still advanced the counter) and
    return the new count."""
    attempts = read_resume_attempts(run_dir) + 1
    _atomic_write(Path(run_dir) / _ATTEMPTS_FILE, {"attempts": attempts})
    return attempts


__all__ = [
    "PipelineWorkOrder",
    "pipeline_run_dir",
    "write_invocation",
    "load_invocation",
    "write_result",
    "has_result",
    "read_result",
    "read_resume_attempts",
    "bump_resume_attempts",
]
