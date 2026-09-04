"""AgentProfile — per-agent metadata persisted to .reyn/agents/<name>/profile.yaml.

PR10 introduced the file with the minimal schema (`name`, `role`,
`created_at`). PR37 adds `allowed_mcp`: an optional allowlist of MCP server names this
agent may access, layered on top of the project-wide `permissions.mcp`
config. Semantics:
- absent / null  → no restriction (inherits project config)
- `"all"`        → same as null but explicit in YAML for audit clarity
- `[a, b]`       → intersect with project allow-list (per-agent narrowing)

The `role` text is injected into the LLM's system prompt so each agent
gets a distinct persona without changing the OS layer.

#4206 slice 1 adds `preferences`: an agent-layer ③ (free-override, see
`reyn.runtime.preferences`) mapping — dotted config key -> value, keys
restricted to `reyn.runtime.preferences.PREFERENCE_KEYS`. Absent/empty is
the common case (most agents set no preference override at all); a
malformed key raises at `load()` time rather than being silently ignored
(the `preferences` module's own `validate_preferences`).

#4206 ② adds `bounding`: an agent-layer ceiling mapping (see
`reyn.runtime.bounding`) — SAME flat dotted-key shape as `preferences`
above, but a DIFFERENT composition (narrowest wins, never widens) and a
DIFFERENT, disjoint key vocabulary (`BOUNDING_KEYS`, currently `{"model"}`
only). Kept as a separate field/key rather than folded into `preferences`
because the two axes' composition functions differ — see
`reyn.runtime.bounding`'s module docstring for the full reasoning.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from reyn.security.permissions.capability_profile import CapabilityProfile

PROFILE_FILENAME = "profile.yaml"


#: #5742 PR2 (architect ruling, issue #5742): every profile.yaml top-level
#: key that has been RETIRED, mapped to its replacement — the population
#: :meth:`AgentProfile.load` raises :class:`RetiredProfileKeyError` for.
#: A map, not a set, so the error can NAME the replacement (a bare "unknown
#: key" WARN — :func:`unknown_profile_keys`'s own generic disclosure — does
#: not tell an operator their agent's instructed text stopped being read;
#: this dict is what lets the raised message say "use X instead" instead of
#: just "Y is gone"). Adding the NEXT retirement is one entry here, never a
#: new hand-written check — see :meth:`AgentProfile.load`'s own retired-key
#: block.
_RETIRED_PROFILE_KEYS: "dict[str, str]" = {
    "project_context_path": "context_path",
}


class RetiredProfileKeyError(ValueError):
    """A ``profile.yaml`` names a top-level key in :data:`_RETIRED_PROFILE_
    KEYS` — raised, never merely warned, unlike every other unrecognized
    top-level key (:func:`unknown_profile_keys`'s own generic WARN).

    #5742 PR2 (architect, verbatim): this asymmetry with ``reyn.yaml``'s own
    retired top-level keys (WARN-only, ``config/chat.py``'s
    ``max_shrink_iterations`` for the current instance) is DELIBERATE, not
    an inconsistency to reconcile: "この key は model に渡る指示文そのもの
    を選ぶ。誤った timeout で動くことと、誤った指示で動くことは別の
    class." A stale ``reyn.yaml`` tuning knob degrades gracefully (the
    orphaned value is read, does nothing, the feature it drove already has
    a replacement path); a stale ``project_context_path`` silently starts
    an agent on WRONG instructed text — the harm is invisible until a
    human notices the agent is behaving as if it never read what an
    operator thinks it did. Not visible with the shipped config (an
    operator who never customized ``project_context_path`` never sees it);
    the bound is "the operator immediately, at their own next `reyn chat`,
    a load, not a silent runtime drift" — a hard fail here trades a broken
    session for a self-correcting one.

    Scope note: retiring ``reyn.yaml``'s OWN retired-key severity to match
    is explicitly OUT of this PR's scope (architect, #5742) — that
    population and its actual incident rate are unmeasured; re-open trigger
    is a real incident from a stale ``reyn.yaml`` key, not this asymmetry
    alone."""


def unknown_profile_keys(data: "dict") -> "frozenset[str]":
    """#5455 ①: every top-level key in a raw ``profile.yaml`` dict that is
    not a real :class:`AgentProfile` field — the same class of gap #4501/
    #4515 closed for ``reyn.yaml`` (an unknown key is silently dropped by
    ``.get(...)``, never surfaced), now closed for this file too.

    The registry is ``dataclasses.fields(AgentProfile)`` itself — the SAME
    "the live dataclass is the complete population" idiom #5416 already
    established (no separate hand-maintained key list to drift from the
    real fields). A DEDICATED function, not a new entry into
    :func:`reyn.config.config_schema.unknown_config_keys` — that function
    walks ``ReynConfig``'s OWN, unrelated vocabulary; folding profile.yaml
    into it would make ONE function know TWO closed vocabularies (the
    #5057 "same guard, second copy" shape architect's #5455 review
    explicitly rejected), not close anything.

    Called from BOTH :meth:`AgentProfile.load` (so every real caller of
    it — chat startup, ``AgentRegistry.spawn_session`` — gets the SAME
    disclosure, a parse-time WARN; ``reyn doctor`` does NOT call
    ``AgentProfile.load`` — it reads ``profile.yaml`` directly for its
    own hook-env section, so it needs the SEPARATE walk below, not this
    call site, to see this) and ``reyn config validate`` (a dedicated
    CLI section, walking ``.reyn/agents/*/profile.yaml`` itself) —
    mirroring how ``unknown_config_keys``
    itself already serves 3 callers from one implementation.

    #5742 PR2: :data:`_RETIRED_PROFILE_KEYS` is excluded from the returned
    population — a retired key is not "unrecognized, no further signal"
    (this function's own generic disclosure); it has a NAMED replacement
    and, at :meth:`AgentProfile.load`, a hard raise. Folding it back into
    this bucket would understate it to a bare WARN at any OTHER caller
    (e.g. ``reyn config validate``) that reads only this function's
    result — see :func:`retired_profile_keys_present` for that population."""
    known = {f.name for f in dataclasses.fields(AgentProfile)}
    return frozenset(data.keys()) - known - set(_RETIRED_PROFILE_KEYS)


def retired_profile_keys_present(data: "dict") -> "dict[str, str]":
    """#5742 PR2: the subset of :data:`_RETIRED_PROFILE_KEYS` actually
    present as top-level keys in *data* (a raw, just-``yaml.safe_load``ed
    ``profile.yaml`` dict), mapped old -> replacement. Shared by
    :meth:`AgentProfile.load` (raises :class:`RetiredProfileKeyError` when
    non-empty) and ``reyn config validate``'s own profile-scanning section
    (reports the SAME finding without constructing a live
    ``AgentProfile`` — read-only diagnostic, no raise)."""
    return {k: v for k, v in _RETIRED_PROFILE_KEYS.items() if k in data}


@dataclass(frozen=True)
class AgentProfile:
    name: str
    role: str = ""
    created_at: str = ""
    # PR37: optional MCP server allowlist. None = no per-agent restriction
    # (inherits project config). "all" in YAML normalizes to None here.
    # list[str] = intersect with project allow-list.
    allowed_mcp: list[str] | None = None
    # #4206 slice 1: ③ preference overrides, dotted key -> value. Empty
    # dict (not None) is the "nothing set" case, matching the flat-dict
    # shape `reyn.runtime.preferences.resolve_preference` expects directly
    # (no None-check needed at every call site).
    preferences: "dict[str, object]" = field(default_factory=dict)
    # #4206 ②: bounding-axis ceilings, dotted key -> value, keys restricted
    # to `reyn.runtime.bounding.BOUNDING_KEYS`. Same "empty dict, not None"
    # shape as `preferences` above.
    bounding: "dict[str, object]" = field(default_factory=dict)
    # #5080/#5081: #4206's axis ① (capability, restrict-only) applied to a
    # "file zone" -- a working-directory override for this agent. Lives
    # here (keyed by AGENT identity, this file's own directory) rather
    # than in `.reyn/capability_profiles/<X>.yaml` (architect BLOCK,
    # #5081): that directory's `<X>` is keyed by PROFILE name -- a
    # topology's `profiles: {member: profile_name}` binding is a free
    # string with no uniqueness constraint against agent names
    # (`_validate_agent_name`/`_validate_topology_name` validate names,
    # not profile-name uniqueness), and `profiles: {alice: alice}` (an
    # agent bound to a same-named narrowing template) is idiomatic, not
    # exceptional -- writing base_dir there would silently collide with
    # an unrelated narrowing template. None = no override (falls through
    # to the project's own base_dir, same convention as `allowed_mcp`'s
    # None).
    base_dir: "str | None" = None
    # #5742 PR2 (architect ruling, issue #5742): project_context_path
    # (#5084/#5111 — an agent-layer override of WHICH file is read as this
    # agent's project context, REPLACING the project-wide file for this
    # one agent's own session) is RETIRED — no longer a field. A
    # profile.yaml naming it raises RetiredProfileKeyError at load() (see
    # _RETIRED_PROFILE_KEYS); the write side (AgentRegistry.create's own
    # former project_context_path kwarg, `reyn agent new
    # --project-context-path`) is removed in the same PR (a live creation
    # seam that keeps writing a key load() then hard-rejects would be a
    # landmine, not a deprecation). context_path (below) is the
    # replacement — a DIFFERENT shape, not a rename: a bare filename
    # resolved against THIS agent's own workspace_dir, never an arbitrary
    # absolute/${REYN_PROJECT_DIR}-relative path anywhere in the
    # workspace.
    #
    # #5742 (owner ruling, chat 2026-09-04): context_path's own default-
    # name-order search (REYN.md else AGENTS.md, first EXISTING wins,
    # resolve_context_candidate/DEFAULT_PROJECT_CONTEXT_FILES, config/
    # loader.py) is what project_context_path's arbitrary-path shape had
    # no room for — the reason #5742 generalizes THIS mechanism, not that
    # one. None = auto-resolve (same convention as base_dir's own None).
    # Read FRESH every turn (router_host_adapter.py's own
    # _read_agent_instructions) — unlike project_context_path's own
    # CONSTRUCTION_ONCE (owner ruling B/#3787), this is the agent-layer
    # field, so it follows #3787's OTHER half of that same ruling: "hot
    # reload — する（agent 側のみ）". PR1 (#5742) landed this field
    # alongside the still-accepted, deprecated project_context_path; PR2
    # (this PR) retires that field, as described above.
    context_path: "str | None" = None
    # #5352: this agent's OWN declared sandbox-policy narrowing — the
    # config-facing vocabulary dict (``network`` / ``subprocess`` /
    # ``allow_write_paths`` / ``deny_write_paths`` / ``deny_read_paths`` /
    # ``allow_env_names`` / ``deny_env_names`` / ...) that
    # ``reyn.security.sandbox.policy._translate_sandbox_policy_config`` and
    # ``reyn.config.infra.SandboxConfig.policy`` already use — SAME shape,
    # a DIFFERENT source (this agent's own profile.yaml rather than the
    # process-wide reyn.yaml `sandbox.policy`). None (absent) = no per-agent
    # declaration; the agent's ``Session._sandbox_config`` then falls
    # through to the process-wide ``SandboxConfig`` unmodified (#5352's own
    # disclosure in ``runtime/agent.py`` — this field is what answers the
    # "per-agent narrowing" question that disclosure named as open).
    # Composition at spawn time (same-agent inherits the spawner's live
    # effective value; cross-agent inherits THIS field when the target
    # declares it, else falls back to the spawner's value) lives in
    # ``AgentRegistry.resolved_sandbox_for`` / the spawn call sites — this
    # field only carries the agent's OWN raw declaration, same "raw value,
    # resolved/bounded at USE time" split ``base_dir``/``context_path``
    # already use above.
    sandbox: "dict[str, object] | None" = None

    @classmethod
    def new(
        cls, name: str, role: str = "", *,
        base_dir: "str | None" = None,
    ) -> "AgentProfile":
        return cls(
            name=name,
            role=role,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            base_dir=base_dir,
        )

    @classmethod
    def load(cls, agent_dir: Path) -> "AgentProfile":
        """Load profile.yaml from `agent_dir`. Raises FileNotFoundError if missing.

        #4206 slice 1: a `preferences:` mapping with a key outside
        `reyn.runtime.preferences.PREFERENCE_KEYS` raises
        `UnknownPreferenceKeyError` — a typo'd/renamed preference key must
        not silently do nothing, the same discipline #4655 established for
        config-schema dict-leaves.

        #4206 ②: a `bounding:` mapping with a key outside
        `reyn.runtime.bounding.BOUNDING_KEYS` raises `UnknownBoundingKeyError`
        the same way.

        #5455 ①: an unrecognized TOP-LEVEL key (e.g. a field removed from
        this dataclass, like #5095's `broker_identity`) WARNs — never
        raises, matching every other "not applied" disclosure in this
        codebase (an operator's file stays loadable; the log is where the
        mismatch surfaces) — see :func:`unknown_profile_keys`.

        #5742 PR2: a top-level key in :data:`_RETIRED_PROFILE_KEYS` (e.g.
        `project_context_path`, retired in favor of `context_path`) is
        DIFFERENT from the WARN above — it raises
        :class:`RetiredProfileKeyError`, naming the replacement, before any
        other parsing happens. See that error class's own docstring for
        why this severity is deliberately asymmetric with `reyn.yaml`'s own
        retired keys (WARN-only there)."""
        from reyn.runtime.bounding import validate_bounding
        from reyn.runtime.preferences import validate_preferences

        path = agent_dir / PROFILE_FILENAME
        if not path.is_file():
            raise FileNotFoundError(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        retired_present = retired_profile_keys_present(data)
        if retired_present:
            lines = ", ".join(
                f"{old!r} -> use {new!r} instead"
                for old, new in sorted(retired_present.items())
            )
            raise RetiredProfileKeyError(
                f"agent {data.get('name', agent_dir.name)!r} profile.yaml "
                f"uses retired key(s): {lines}. This key selects the "
                "instructed text delivered to the model — a stale value "
                "here means the agent silently would not run on what its "
                "operator wrote, so this is a hard failure, not a "
                f"warning. Edit {path} and rewrite the key(s) named above "
                "before this agent can start."
            )
        unknown_top_level = unknown_profile_keys(data)
        if unknown_top_level:
            import logging

            logging.getLogger(__name__).warning(
                "agent %r profile.yaml has unrecognized key(s) (not "
                "applied): %s",
                data.get("name", agent_dir.name), ", ".join(sorted(unknown_top_level)),
            )
        # PR37: parse allowed_mcp — "all" sentinel normalizes to None.
        raw_allowed_mcp = data.get("allowed_mcp", None)
        if raw_allowed_mcp is None or raw_allowed_mcp == "all":
            allowed_mcp: list[str] | None = None
        else:
            allowed_mcp = [str(s) for s in raw_allowed_mcp]
        name = str(data.get("name", agent_dir.name))
        raw_preferences = data.get("preferences") or {}
        preferences = dict(raw_preferences) if isinstance(raw_preferences, dict) else {}
        validate_preferences(preferences, source=f"agent {name!r} profile.yaml")
        raw_bounding = data.get("bounding") or {}
        bounding = dict(raw_bounding) if isinstance(raw_bounding, dict) else {}
        validate_bounding(bounding, source=f"agent {name!r} profile.yaml")
        raw_base_dir = data.get("base_dir")
        base_dir = str(raw_base_dir) if raw_base_dir else None
        raw_context_path = data.get("context_path")
        context_path = str(raw_context_path) if raw_context_path else None
        # #5352: `sandbox:` — a dict (the config-facing sandbox-policy
        # vocabulary) or absent/None. Not routed through any further
        # validation here (same "no hard-fail, warn elsewhere" posture
        # `SandboxConfig.__post_init__` already settled for this exact
        # vocabulary, #4174 T0) — an unknown key inside it is a USE-time
        # concern for whatever resolves it, not a load-time crash.
        raw_sandbox = data.get("sandbox")
        sandbox = dict(raw_sandbox) if isinstance(raw_sandbox, dict) else None
        return cls(
            name=name,
            role=str(data.get("role", "") or ""),
            created_at=str(data.get("created_at", "") or ""),
            allowed_mcp=allowed_mcp,
            preferences=preferences,
            bounding=bounding,
            base_dir=base_dir,
            context_path=context_path,
            sandbox=sandbox,
        )

    def save(self, agent_dir: Path) -> None:
        agent_dir.mkdir(parents=True, exist_ok=True)
        path = agent_dir / PROFILE_FILENAME
        # Hand-roll the dict so absent allowed_mcp (None) / empty
        # preferences don't appear in the yaml as `null`/`{}` — keep the
        # on-disk shape minimal.
        payload: dict = {
            "name": self.name,
            "role": self.role,
            "created_at": self.created_at,
        }
        if self.allowed_mcp is not None:
            payload["allowed_mcp"] = list(self.allowed_mcp)
        if self.preferences:
            payload["preferences"] = dict(self.preferences)
        if self.bounding:
            payload["bounding"] = dict(self.bounding)
        if self.base_dir is not None:
            payload["base_dir"] = self.base_dir
        if self.context_path is not None:
            payload["context_path"] = self.context_path
        if self.sandbox is not None:
            payload["sandbox"] = dict(self.sandbox)
        path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def default_profile(self) -> "CapabilityProfile":
        """The agent's default capability spec (#2074 S4a) — the canonical unified
        representation of this agent's per-agent baseline narrowing on the MCP axis.

        The profile.yaml user key ``allowed_mcp`` maps onto the unified spec's
        ``mcp_allow`` axis (the INTERNAL representation). ``None`` passes through
        as ``None`` (= ⊤, unrestricted). #2074 S4b repoints the per-agent ∩ layer
        to read this spec object so one primitive feeds the MCP binding adapter."""
        from reyn.security.permissions.capability_profile import CapabilityProfile

        return CapabilityProfile(
            name=self.name,
            mcp_allow=tuple(self.allowed_mcp) if self.allowed_mcp is not None else None,
        )


__all__ = ["AgentProfile", "PROFILE_FILENAME"]
