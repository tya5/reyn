"""
PostprocessorExecutor: OS-owned deterministic execution of Skill postprocessor chains.

Runs once at skill finish, after the LLM's finish artifact has been captured
and before the caller receives the result. Transforms the LLM-contract artifact
(conforming to skill.final_output_schema) into the caller-contract artifact
(conforming to skill.postprocessor.output_schema).

Step semantics are identical to PreprocessorExecutor — the step set, on_error
policy, op dispatch, and permission gate are shared via delegation. The only
differences are the fire position (skill finish, not phase entry) and the
schema sources (skill-level, not phase-level).

Resume / crash-recovery (mid-postprocessor resume):

When ``resume_plan`` is supplied to ``run()``, each step checks for a matching
``CommittedStep`` in ``resume_plan.committed_steps`` before executing. On a
memo hit, the recorded result is used directly (no re-execution). WAL
``step_started`` / ``step_completed`` / ``step_failed`` events are emitted
when ``state_log`` + ``skill_run_id`` are wired — these feed the
``SkillResumeAnalyzer`` on the next resume cycle.

The ``op_invocation_id`` format is ``"__post__.{step_idx}"`` — the reserved
``__post__`` prefix separates postprocessor steps from phase steps in the
analyzer's pairing logic.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import jsonschema

from reyn.kernel.preprocessor_executor import PreprocessorError, PreprocessorExecutor
from reyn.llm.pricing import TokenUsage

if TYPE_CHECKING:
    from reyn.events.events import EventLog
    from reyn.llm.model_resolver import ModelResolver
    from reyn.permissions.permissions import PermissionResolver
    from reyn.python_runner import PythonRunner
    from reyn.schemas.models import PreprocessorStep, Skill
    from reyn.user_intervention import InterventionBus
    from reyn.workspace.workspace import Workspace

logger = logging.getLogger(__name__)


class PostprocessorError(RuntimeError):
    pass


# ── Synthetic phase-like scope ────────────────────────────────────────────────

@dataclass
class _PostprocessorScope:
    """Minimal Phase-like object satisfying PreprocessorExecutor's phase parameter.

    PreprocessorExecutor._build_op_ctx pulls:
      - phase.name           → used as `preprocessor_phase_name` and in event fields
      - phase.preprocessor   → used to check non-empty and iterate steps

    _apply_step also uses phase.name (via phase_name = phase.name local) for
    error messages. We expose exactly those fields.
    """
    name: str
    preprocessor: "list[PreprocessorStep]"


_POST_PHASE_NAME = "__post__"


# ── Module-level helpers ──────────────────────────────────────────────────────


def _compute_step_hash(step_index: int, artifact: dict) -> str:
    """Stable hash over (step_index, artifact) for postprocessor step memoization.

    Mirrors dispatch._compute_args_hash but includes step_index so that
    two consecutive steps that happen to receive the same artifact data
    still produce distinct memo keys.
    """
    try:
        canonical = json.dumps(
            {"step_index": step_index, "artifact": artifact},
            sort_keys=True, default=str,
        )
    except Exception:  # noqa: BLE001
        canonical = repr((step_index, artifact))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _lookup_committed_step(resume_plan: Any, op_invocation_id: str, args_hash: str) -> Any:
    """Find a CommittedStep matching the current postprocessor step, or None.

    Matches on (op_invocation_id, phase="__post__", args_hash).  Returns
    the *most recent* matching CommittedStep (highest seq) so a botched
    truncation that left two completions doesn't poison the replay.
    """
    if resume_plan is None:
        return None
    committed = getattr(resume_plan, "committed_steps", None)
    if not committed:
        return None
    best = None
    for step in committed:
        if (step.op_invocation_id == op_invocation_id
                and step.phase == _POST_PHASE_NAME
                and step.args_hash == args_hash):
            if best is None or step.seq > best.seq:
                best = step
    return best


class PostprocessorExecutor:
    """Runs a Skill.postprocessor block at skill finish.

    Mirrors PreprocessorExecutor but at skill scope: takes the LLM's finish
    artifact (conforming to skill.final_output_schema) and produces the
    caller-facing artifact (conforming to skill.postprocessor.output_schema).

    The step-application logic is shared with PreprocessorExecutor via
    delegation — step semantics (validate/run_op/iterate/lint_plan/python),
    on_error policy, and op dispatch context are identical.

    Events emitted:
      postprocessor_step_started   — before each step
      postprocessor_step_completed — after each successful step
      postprocessor_step_failed    — on step failure
    """

    def __init__(
        self,
        *,
        skill: "Skill",
        workspace: "Workspace",
        events: "EventLog",
        model: str,
        resolver: "ModelResolver",
        subscribers: list,
        permission_resolver: "PermissionResolver | None" = None,
        intervention_bus: "InterventionBus | None" = None,
        python_runner: "PythonRunner | None" = None,
        python_allowed_modules: list[str] | None = None,
        max_phase_visits: int = 0,
        caller: str = "direct",
        # Resume / WAL wiring — optional; None means fresh run (no memoization).
        state_log: Any = None,
        skill_run_id: str | None = None,
    ) -> None:
        self._skill = skill
        self._events = events
        self._state_log = state_log
        self._skill_run_id = skill_run_id
        # Delegate all step execution to a PreprocessorExecutor instance.
        # It is constructed identically to the runtime's preprocessor — the
        # synthetic scope below adapts the call surface.
        self._delegate = PreprocessorExecutor(
            skill=skill,
            workspace=workspace,
            model=model,
            events=events,
            subscribers=subscribers,
            resolver=resolver,
            max_phase_visits=max_phase_visits,
            permission_resolver=permission_resolver,
            intervention_bus=intervention_bus,
            python_runner=python_runner,
            python_allowed_modules=python_allowed_modules,
            caller=caller,
        )

    async def run(
        self,
        finish_artifact: dict,
        output_language: str | None,
        resume_plan: Any = None,
    ) -> tuple[dict, TokenUsage]:
        """Apply skill.postprocessor.steps; return (caller_artifact, usage).

        If skill.postprocessor is None, returns finish_artifact unchanged.

        On success the returned artifact conforms to
        skill.postprocessor.output_schema; if the final validation fails a
        PostprocessorError is raised.

        ``resume_plan``: when set (mid-postprocessor resume), each step checks
        for a committed result in ``resume_plan.committed_steps`` before
        executing. On memo hit the recorded result is replayed; on miss the
        step executes normally and the result is emitted to the WAL.
        """
        postprocessor = self._skill.postprocessor
        if postprocessor is None:
            return finish_artifact, TokenUsage()

        # Build a synthetic Phase-like scope so the PreprocessorExecutor
        # delegate can execute steps without knowing it's in postprocessor
        # context. The reserved name "__post__" appears in events / error
        # messages and is documented as a pseudo-phase sentinel.
        scope = _PostprocessorScope(
            name=_POST_PHASE_NAME,
            preprocessor=postprocessor.steps,
        )

        result = copy.deepcopy(finish_artifact)
        total_usage = TokenUsage()

        emit_wal = (
            self._state_log is not None
            and self._skill_run_id is not None
        )

        for i, step in enumerate(postprocessor.steps):
            op_invocation_id = f"{_POST_PHASE_NAME}.{i}"

            self._events.emit(
                "postprocessor_step_started",
                step_index=i, step_type=step.type,
            )

            # ── Resume memoization ────────────────────────────────────────
            # Compute a stable args_hash over the step's serializable state
            # (step type + current result data) so memo lookup can detect
            # drift (e.g. different artifact entering the step on resume vs
            # the original run).
            args_hash = _compute_step_hash(i, result)
            memo = _lookup_committed_step(resume_plan, op_invocation_id, args_hash)

            if memo is not None:
                # Replay the recorded outcome without re-executing.
                self._events.emit(
                    "postprocessor_step_memoized",
                    step_index=i, step_type=step.type,
                    op_invocation_id=op_invocation_id,
                    recorded_seq=memo.seq,
                )
                if memo.error_kind is not None:
                    # The original run failed this step; re-raise on resume.
                    raise PostprocessorError(
                        f"Postprocessor step[{i}] ({step.type}) replay: "
                        f"{memo.error_kind}: {memo.error_message or ''}"
                    )
                # memo.result is the serialized post-step artifact from the WAL.
                if isinstance(memo.result, dict):
                    result = memo.result
                self._events.emit(
                    "postprocessor_step_completed",
                    step_index=i, step_type=step.type,
                )
                continue

            # ── Fresh execution ───────────────────────────────────────────
            if emit_wal:
                await self._wal_step_started(op_invocation_id, step.type, args_hash)

            try:
                result, step_usage = await self._delegate._apply_step(
                    step, result, i, scope, output_language,  # type: ignore[arg-type]
                )
                total_usage += step_usage
            except (PreprocessorError, Exception) as exc:
                self._events.emit(
                    "postprocessor_step_failed",
                    step_index=i, step_type=step.type, error=str(exc),
                )
                if emit_wal:
                    await self._wal_step_failed(
                        op_invocation_id, step.type, args_hash,
                        "exception", f"{type(exc).__name__}: {exc}",
                    )
                raise PostprocessorError(
                    f"Postprocessor step[{i}] ({step.type}): {exc}"
                ) from exc

            self._events.emit(
                "postprocessor_step_completed",
                step_index=i, step_type=step.type,
            )
            if emit_wal:
                await self._wal_step_completed(op_invocation_id, step.type, args_hash, result)

        # Final output validation against postprocessor.output_schema.
        data = result.get("data", {})
        validator = jsonschema.Draft7Validator(postprocessor.output_schema)
        errors = sorted(validator.iter_errors(data), key=str)
        if errors:
            messages = [e.message for e in errors[:5]]
            raise PostprocessorError(
                f"Postprocessor output failed schema validation "
                f"(output_schema): {'; '.join(messages)}"
            )

        return result, total_usage

    # ── WAL helpers ───────────────────────────────────────────────────────────

    async def _wal_step_started(
        self, op_invocation_id: str, op_kind: str, args_hash: str,
    ) -> None:
        """Append step_started to WAL. Defensive — never raises."""
        try:
            await self._state_log.append(
                "step_started",
                run_id=self._skill_run_id,
                phase=_POST_PHASE_NAME,
                op_invocation_id=op_invocation_id,
                op_kind=op_kind,
                args={},
                args_hash=args_hash,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "WAL step_started failed (run=%s op=%s): %s",
                self._skill_run_id, op_invocation_id, e,
            )

    async def _wal_step_completed(
        self, op_invocation_id: str, op_kind: str, args_hash: str, result: dict,
    ) -> None:
        """Append step_completed to WAL with the post-step artifact. Defensive."""
        try:
            await self._state_log.append(
                "step_completed",
                run_id=self._skill_run_id,
                phase=_POST_PHASE_NAME,
                op_invocation_id=op_invocation_id,
                op_kind=op_kind,
                args_hash=args_hash,
                result=result,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "WAL step_completed failed (run=%s op=%s): %s",
                self._skill_run_id, op_invocation_id, e,
            )

    async def _wal_step_failed(
        self,
        op_invocation_id: str,
        op_kind: str,
        args_hash: str,
        error_kind: str,
        message: str,
    ) -> None:
        """Append step_failed to WAL. Defensive."""
        try:
            await self._state_log.append(
                "step_failed",
                run_id=self._skill_run_id,
                phase=_POST_PHASE_NAME,
                op_invocation_id=op_invocation_id,
                op_kind=op_kind,
                args_hash=args_hash,
                error_kind=error_kind,
                message=message,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "WAL step_failed failed (run=%s op=%s): %s",
                self._skill_run_id, op_invocation_id, e,
            )
