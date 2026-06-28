"""reyn.config.execution — execution config: Plan/SkillResume/SelfImprovement/TimeTravel/ToolUse. (#1682 #3 split)."""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from reyn.config.chat import (  # #1682 #3: phase compaction config lives in chat
    PhaseActResultsCompactionConfig,
)
from reyn.runtime.budget.budget import CostConfig, CostLimitConfig

SKILL_RESUME_POLICIES = ("prompt", "retry", "skip", "discard_skill")


@dataclass
class SelfImprovementConfig:
    """`self_improvement:` — skill_improver behavior knobs (FP-0006).

    Fields:
        on_propose:
            What skill_improver does when it is about to apply improvements
            back to the original skill directory:

            - ``ask_user`` (default): pause and prompt the user via the
              InterventionBus (summarise score + changes, wait for approval
              before writing). Safe default — the user is in the loop.
            - ``auto``: skip the prompt and apply directly. Intended for CI /
              unattended runs where the operator trusts the eval gate.
            - ``disabled``: do NOT apply the changes. Log a
              ``skill_improvement_dry_run`` event noting what would have been
              applied. Useful for "what would improve this skill?" exploration
              without modifying the source.

        max_versions:
            Maximum number of v<N>.md snapshot files kept in
            ``.reyn/skill-versions/<name>/``.  When the cap is exceeded the
            OLDEST version is deleted (the version pointed to by ``current``
            is never deleted).  Default 10.  Set 0 to disable pruning.
    """

    on_propose: Literal["ask_user", "auto", "disabled"] = "ask_user"
    max_versions: int = 10

    def __post_init__(self) -> None:
        _VALID_ON_PROPOSE = {"ask_user", "auto", "disabled"}
        if self.on_propose not in _VALID_ON_PROPOSE:
            raise ValueError(
                f"self_improvement.on_propose {self.on_propose!r} is not one of "
                f"{sorted(_VALID_ON_PROPOSE)}"
            )
        if self.max_versions < 0:
            raise ValueError(
                f"self_improvement.max_versions must be >= 0, got {self.max_versions}"
            )


def _build_self_improvement_config(raw: object) -> "SelfImprovementConfig":
    """Parse the ``self_improvement:`` section. Empty / missing returns defaults."""
    defaults = SelfImprovementConfig()
    if not isinstance(raw, dict):
        return defaults
    on_propose_raw = raw.get("on_propose", defaults.on_propose)
    on_propose = str(on_propose_raw) if on_propose_raw is not None else defaults.on_propose
    max_versions_raw = raw.get("max_versions", defaults.max_versions)
    try:
        max_versions = int(max_versions_raw)
    except (TypeError, ValueError):
        max_versions = defaults.max_versions
    # Validation is delegated to __post_init__ — raises ValueError with clear message.
    return SelfImprovementConfig(on_propose=on_propose, max_versions=max_versions)


@dataclass
class SkillResumeConfig:
    """`skill_resume:` — policy for handling ambiguous steps on resume.

    An *ambiguous step* is a ``step_started`` WAL event with no matching
    ``step_completed`` / ``step_failed``. The op may have committed
    externally (canonical intermediate-state); only the operator
    can decide what to do.

    Policies (one of ``SKILL_RESUME_POLICIES``):
      - ``retry``         — re-execute the step (default). Safe for
                            read-only ops and for skills the operator
                            trusts to be idempotent. Risk: duplicate
                            side effect.
      - ``skip``          — synthesize an empty / default completion.
                            The skill continues as if the op succeeded
                            without actually running it. Risk: missing
                            data downstream.
      - ``discard_skill`` — abort the entire skill run, drop the
                            checkpoint, surface a failure to the
                            originating chain.
      - ``prompt``        — legacy/no-op under PR-resume-auto. Retained
                            for config compatibility. Treated as
                            ``retry`` by the auto-resume runtime
                            (no interactive prompt is shown — see the
                            R-D3 廃案 note in the active plan).

    ``per_skill`` overrides the default for specific skill names —
    operator declares which skills are safe to retry vs which require
    careful inspection.

    Default changed from ``prompt`` to ``retry`` in PR-resume-auto: the
    auto-resume design never blocks on interactive prompt; ``retry`` is
    the safest non-blocking choice (correct for the common
    flaky-read-API case after PR-memo-purity-fix invalidates world op
    memos on resume).
    """

    default: str = "retry"
    per_skill: dict[str, str] = field(default_factory=dict)

    def policy_for(self, skill_name: str) -> str:
        """Return the resume policy for a given skill name.

        Falls back to ``default`` when no per_skill override exists.
        Caller may further inspect / validate the value (already
        validated to be in ``SKILL_RESUME_POLICIES`` at config-load
        time).
        """
        return self.per_skill.get(skill_name, self.default)


@dataclass
class ToolUseConfig:
    """``tool_use:`` — the tool-use scheme per layer (#1593).

    Each layer (chat / step / phase) selects a registered ``ToolUseScheme`` by name,
    generalizing the binary ``action_retrieval.universal_wrappers_enabled`` toggle
    into a pluggable, per-layer scheme selector. #1657: the ``chat`` default is
    ``enumerate-all`` (the owner H1 fix — flat-listing actions stops
    invoke_action name-hallucination, 30%→100% non-hot-list tool-use). ``step`` /
    ``phase`` keep ``universal-category`` (unchanged — the H1 evidence is the chat
    path). Any layer can be set to another scheme name via reyn.yaml.
    """

    chat: str = "enumerate-all"
    step: str = "universal-category"
    phase: str = "universal-category"


def _build_tool_use_config(raw: object) -> ToolUseConfig:
    """Parse ``tool_use:`` from reyn.yaml. None / missing / empty → defaults
    (chat=enumerate-all #1657; step/phase=universal-category).

    Each layer key accepts a scheme name (string); a missing key keeps the default.
    A non-mapping block or non-string value is a config error (fail loud)."""
    if raw is None:
        return ToolUseConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"tool_use must be a mapping, got {type(raw).__name__}")

    def _name(key: str, default: str) -> str:
        if key not in raw:
            return default
        val = raw[key]
        if not isinstance(val, str) or not val:
            raise ValueError(
                f"tool_use.{key} must be a non-empty scheme name, got {val!r}"
            )
        return val

    return ToolUseConfig(
        chat=_name("chat", "enumerate-all"),  # #1657: owner default switch (H1 fix)
        step=_name("step", "universal-category"),
        phase=_name("phase", "universal-category"),
    )


def _build_skill_resume_config(raw: object) -> SkillResumeConfig:
    """Parse `skill_resume:` block; reject unknown policy values up front."""
    defaults = SkillResumeConfig()
    if not isinstance(raw, dict):
        return defaults
    default = str(raw.get("default", defaults.default))
    if default not in SKILL_RESUME_POLICIES:
        # Unknown policy → fall back to default (safe). Don't raise — config
        # parse failures should never block startup; logger.warning is the
        # convention used elsewhere for "bad config keys".
        import logging
        logging.getLogger(__name__).warning(
            "skill_resume.default %r is not one of %s; using %r",
            default, SKILL_RESUME_POLICIES, defaults.default,
        )
        default = defaults.default
    per_skill_raw = raw.get("per_skill") or {}
    per_skill: dict[str, str] = {}
    if isinstance(per_skill_raw, dict):
        for k, v in per_skill_raw.items():
            v_str = str(v)
            if v_str not in SKILL_RESUME_POLICIES:
                import logging
                logging.getLogger(__name__).warning(
                    "skill_resume.per_skill[%r] = %r is not one of %s; "
                    "skipping", k, v_str, SKILL_RESUME_POLICIES,
                )
                continue
            per_skill[str(k)] = v_str
    return SkillResumeConfig(default=default, per_skill=per_skill)


