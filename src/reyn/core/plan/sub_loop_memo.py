"""Sub-loop LLM call memoization for plan-mode (ADR-0025).

Mirrors R-D2's skill-side memoization (= ``runtime._call_llm_and_record``)
but at the ``call_llm_tools`` boundary inside ``RouterLoop`` rather than
the dispatcher boundary. Plan steps' sub-loops invoke LLM up to
``_PLAN_STEP_MAX_ITERATIONS`` times; on crash mid-step, resume previously
re-paid every LLM call. With this module, results from prior turns are
recorded on the per-plan snapshot and replayed on resume.

Persistence is snapshot-only — no new WAL event kinds. Each LLM call
in a step appends a record to ``PlanSnapshot.step_llm_calls[step_id]``
and triggers a snapshot save. Atomic save (~1 ms) is negligible
relative to the LLM call itself (1–30 s).

Spill: ADR-0024 mirror — records >32 KB serialised spill to
``state/plans/<plan_id>/step_llm_calls/<step_id>/<turn_idx>.json``.
``delete_plan_workspace`` already reclaims the entire per-plan
directory on completion / discard.

Drift: ``args_hash`` mismatch falls through to fresh execution (=
provider versioning, prompt construction shifts, etc.). Records the
fresh result, overwriting nothing — the previous record stays in the
list but is unreachable by hash.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reyn.llm.pricing import TokenUsage

if TYPE_CHECKING:
    from reyn.core.plan.plan_registry import PlanRegistry
    from reyn.llm.llm import LLMToolCallResult

logger = logging.getLogger(__name__)


# ADR-0025 §2: same threshold as ADR-0024 step result spill so the
# overall persistence pattern is consistent.
_LLM_CALL_SPILL_THRESHOLD_CHARS = 32_768


# ── args_hash ─────────────────────────────────────────────────────────────


def compute_sub_loop_args_hash(
    *,
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    tool_choice: str | dict | None,
    sampling: dict | None = None,
) -> str:
    """Stable hash for sub-loop ``call_llm_tools`` invocations.

    Hashes over the inputs that drive deterministic output. Mirrors
    ``dispatcher._compute_llm_args_hash`` shape (SHA-256 truncated to
    16 hex) but uses RouterLoop-shaped inputs (``messages`` list of
    role/content/tool_calls/tool_call_id dicts) rather than
    ContextFrame.

    No volatile-field stripping is applied: chat-router messages don't
    typically embed datetime fields. If the caller injects a volatile
    string into ``messages``, the resume-side memo will miss; the
    fresh-call path records the new hash and proceeds correctly
    (= same drift handling as R-D2).
    """
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools or [],
        "tool_choice": tool_choice,
        "sampling": sampling or {},
    }
    try:
        canonical = json.dumps(
            payload, sort_keys=True, default=str, ensure_ascii=False,
        )
    except Exception:  # noqa: BLE001 — fallback for unhashable values
        canonical = repr(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ── record / restore helpers ──────────────────────────────────────────────


def _serialise_result(result: "LLMToolCallResult") -> dict:
    """Convert an LLMToolCallResult into a JSON-safe dict for persistence.

    Drops ``raw_message`` (= debugging only). Keeps content / tool_calls
    / finish_reason / usage. ``tool_calls`` is already a list of plain
    dicts (litellm shape), so JSON serialisation is direct.
    """
    usage_dict = None
    if result.usage is not None:
        usage_dict = {
            "prompt_tokens": getattr(result.usage, "prompt_tokens", 0),
            "completion_tokens": getattr(result.usage, "completion_tokens", 0),
        }
    return {
        "content": result.content,
        "tool_calls": list(result.tool_calls or []),
        "finish_reason": result.finish_reason,
        "usage": usage_dict,
    }


def _deserialise_result(record: dict) -> "LLMToolCallResult":
    """Reconstruct an LLMToolCallResult from a recorded dict."""
    from reyn.llm.llm import LLMToolCallResult
    usage_dict = record.get("usage")
    usage = TokenUsage()
    if isinstance(usage_dict, dict):
        usage.prompt_tokens = int(usage_dict.get("prompt_tokens", 0))
        usage.completion_tokens = int(usage_dict.get("completion_tokens", 0))
    return LLMToolCallResult(
        content=record.get("content"),
        tool_calls=list(record.get("tool_calls") or []),
        finish_reason=record.get("finish_reason"),
        usage=usage,
        raw_message=None,
    )


# ── memo provider ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _MemoLookupRecord:
    """Internal helper — a single recorded LLM call addressable by hash."""
    args_hash: str
    inline: dict | None      # full result dict (≤ threshold) OR None if spilled
    ref: str | None          # relative path under per-plan dir (> threshold) OR None


class SubLoopMemoProvider:
    """Per-step LLM memoization for a plan sub-loop.

    Constructed by ``execute_plan`` for each step. On miss: invoke
    proceeds normally, ``record`` persists the result. On hit:
    ``get_recorded_result`` returns the deserialised LLMToolCallResult.

    Read-side (``get_recorded_result``) is sync — it consults the seed
    log (provided at construction) plus the in-memory record list
    (populated as the sub-loop runs). On reload-from-disk paths the
    seed log carries the surviving records.

    Write-side (``record``) is async — calls into PlanRegistry which
    persists the snapshot atomically and may spill to file.
    """

    def __init__(
        self,
        *,
        plan_registry: "PlanRegistry",
        plan_id: str,
        step_id: str,
        seed_records: list[_MemoLookupRecord] | None = None,
    ) -> None:
        self._registry = plan_registry
        self._plan_id = plan_id
        self._step_id = step_id
        # seed_records: from PlanResumePlan.step_llm_call_log on resume.
        # Fresh runs start empty.
        self._records: list[_MemoLookupRecord] = list(seed_records or [])
        # Track turn index so spilled paths are unique per call.
        self._turn_idx = len(self._records)

    @property
    def plan_id(self) -> str:
        return self._plan_id

    @property
    def step_id(self) -> str:
        return self._step_id

    def get_recorded_result(self, args_hash: str) -> "LLMToolCallResult | None":
        """Return the recorded LLMToolCallResult for ``args_hash``, or None.

        Walks the seed + in-memory record list. Inline records
        deserialise immediately; spilled records read from the
        per-plan workspace file. File unreadable → None (= caller
        treats as miss; fresh execution will run + record).
        """
        for rec in self._records:
            if rec.args_hash != args_hash:
                continue
            if rec.inline is not None:
                return _deserialise_result(rec.inline)
            if rec.ref is not None:
                full = self._spill_path_from_ref(rec.ref)
                try:
                    data = json.loads(full.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    logger.warning(
                        "spilled LLM record unreadable at %s: %r — falling "
                        "through to fresh call", full, exc,
                    )
                    return None
                return _deserialise_result(data)
            return None
        return None

    async def record(
        self,
        *,
        args_hash: str,
        result: "LLMToolCallResult",
    ) -> None:
        """Persist a fresh LLM call result for future resume hit."""
        record_dict = _serialise_result(result)
        try:
            serialised = json.dumps(record_dict, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            serialised = ""
        # Branch on size: inline ≤ threshold, spill to file > threshold.
        if len(serialised) <= _LLM_CALL_SPILL_THRESHOLD_CHARS:
            inline = record_dict
            ref = None
        else:
            ref_rel = self._allocate_spill_path()
            full = self._spill_path_from_ref(ref_rel)
            full.parent.mkdir(parents=True, exist_ok=True)
            tmp = full.with_suffix(full.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(record_dict, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(full)
            inline = None
            ref = ref_rel
        # Update in-memory state.
        new_rec = _MemoLookupRecord(args_hash=args_hash, inline=inline, ref=ref)
        self._records.append(new_rec)
        self._turn_idx += 1
        # Persist to snapshot.
        await self._registry.record_step_llm_call(
            plan_id=self._plan_id,
            step_id=self._step_id,
            args_hash=args_hash,
            inline=inline,
            ref=ref,
            usage=record_dict.get("usage") or {},
        )

    # ── helpers ──────────────────────────────────────────────────────────

    def _allocate_spill_path(self) -> str:
        """Per-call relative path under per-plan dir.

        ``step_llm_calls/<step_id>/<turn_idx>.json`` — turn_idx increments
        per recorded call, so collisions across re-runs of the same
        step (= rare; would happen only if reset_from_step then re-runs
        and produces a >32KB result that needs distinct path).
        """
        return f"step_llm_calls/{self._step_id}/{self._turn_idx}.json"

    def _spill_path_from_ref(self, ref_rel: str) -> Path:
        return self._registry.state_dir / "plans" / self._plan_id / ref_rel


# ── analyzer extraction helper ────────────────────────────────────────────


def extract_step_llm_call_records(
    snapshot_step_llm_calls: dict[str, list[dict]],
    step_id: str,
) -> list[_MemoLookupRecord]:
    """Build seed memo records for one step from PlanSnapshot data.

    Used by execute_plan when resume_plan is set: extracts the recorded
    LLM call records for ``step_id`` from the snapshot's
    ``step_llm_calls`` dict and returns them in the
    ``_MemoLookupRecord`` form the provider expects.
    """
    records = snapshot_step_llm_calls.get(step_id) or []
    out: list[_MemoLookupRecord] = []
    for entry in records:
        if not isinstance(entry, dict):
            continue
        h = entry.get("args_hash")
        if not isinstance(h, str):
            continue
        out.append(_MemoLookupRecord(
            args_hash=h,
            inline=entry.get("inline"),
            ref=entry.get("ref"),
        ))
    return out


__all__ = [
    "SubLoopMemoProvider",
    "compute_sub_loop_args_hash",
    "extract_step_llm_call_records",
]
