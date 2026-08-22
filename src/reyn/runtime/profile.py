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

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from reyn.security.permissions.capability_profile import CapabilityProfile

PROFILE_FILENAME = "profile.yaml"


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

    @classmethod
    def new(
        cls, name: str, role: str = "", *, base_dir: "str | None" = None,
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
        the same way."""
        from reyn.runtime.bounding import validate_bounding
        from reyn.runtime.preferences import validate_preferences

        path = agent_dir / PROFILE_FILENAME
        if not path.is_file():
            raise FileNotFoundError(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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
        return cls(
            name=name,
            role=str(data.get("role", "") or ""),
            created_at=str(data.get("created_at", "") or ""),
            allowed_mcp=allowed_mcp,
            preferences=preferences,
            bounding=bounding,
            base_dir=base_dir,
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
