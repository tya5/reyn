"""Topology — first-class abstraction for agent-to-agent communication structure.

PR12 introduces three kinds (`network` / `team` / `pipeline`) declared in
`.reyn/topologies/<name>.yaml`. Each topology lists its members and a
`can_send(from, to)` rule derived from its kind. The registry consults all
topologies a sender shares with its receiver to decide whether the edge is
allowed.

PR13 replaces the previous "permissive fallback" with an auto-managed
`_default` network topology synthesized by AgentRegistry: it contains every
agent that does not belong to any user-declared topology. With `_default`
in the picture the permit rule collapses to a single line — "edge allowed
iff some shared topology's `can_send` is True". The empty-state bootstrap
still works (all agents are in `_default`, so they freely communicate) and
declaring even one user topology immediately removes its members from
`_default`, so any restriction is enforced the moment it's declared.

This composition means a hierarchical organization tree is expressible as
overlapping `team` topologies (one per parent-team relationship) without
a dedicated `tree` kind. `meeting` / `pair` / `broadcast` kinds remain
deferred until there's concrete demand.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml


TOPOLOGY_DIRNAME = "topologies"
KINDS = ("network", "team", "pipeline")

# Same charset as agent names — keeps the on-disk layout uniform.
_TOPOLOGY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_RESERVED_NAMES = {"default"}


def _validate_topology_name(name: str) -> None:
    if name in _RESERVED_NAMES:
        raise ValueError(f"topology name {name!r} is reserved")
    if not _TOPOLOGY_NAME_RE.match(name):
        raise ValueError(
            f"invalid topology name {name!r}: must be 1-32 chars of "
            "[a-z0-9_-] starting with [a-z0-9]"
        )


@dataclass(frozen=True)
class Topology:
    name: str
    kind: str
    members: tuple[str, ...] = field(default_factory=tuple)
    leader: str | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(
                f"invalid topology kind {self.kind!r}: expected one of {KINDS}"
            )
        if len(set(self.members)) != len(self.members):
            raise ValueError(f"topology {self.name!r}: duplicate members in {self.members}")
        if self.kind == "team":
            if self.leader is None:
                raise ValueError(f"topology {self.name!r}: kind=team requires a leader")
            if self.leader not in self.members:
                raise ValueError(
                    f"topology {self.name!r}: leader {self.leader!r} not in members"
                )
        elif self.leader is not None:
            raise ValueError(
                f"topology {self.name!r}: leader is only valid for kind=team"
            )

    # ── permission rule ────────────────────────────────────────────────────────

    def can_send(self, from_agent: str, to_agent: str) -> bool:
        if from_agent == to_agent:
            return False
        if from_agent not in self.members or to_agent not in self.members:
            return False
        if self.kind == "network":
            return True
        if self.kind == "team":
            # Star around the leader: leader ↔ each member, but member ↔ member
            # is forbidden.
            return self.leader in (from_agent, to_agent)
        if self.kind == "pipeline":
            try:
                i = self.members.index(from_agent)
                j = self.members.index(to_agent)
            except ValueError:
                return False
            return j == i + 1
        return False

    def edges(self) -> list[tuple[str, str]]:
        """All directed edges this topology permits — used by `topology show`."""
        out: list[tuple[str, str]] = []
        for a in self.members:
            for b in self.members:
                if self.can_send(a, b):
                    out.append((a, b))
        return out

    # ── persistence ────────────────────────────────────────────────────────────

    @classmethod
    def new(
        cls,
        name: str,
        *,
        kind: str,
        members: list[str],
        leader: str | None = None,
    ) -> "Topology":
        _validate_topology_name(name)
        return cls(
            name=name,
            kind=kind,
            members=tuple(members),
            leader=leader,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    @classmethod
    def load(cls, path: Path) -> "Topology":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        members = data.get("members", []) or []
        return cls(
            name=str(data.get("name", path.stem)),
            kind=str(data.get("kind", "network")),
            members=tuple(str(m) for m in members),
            leader=(str(data["leader"]) if data.get("leader") else None),
            created_at=str(data.get("created_at", "") or ""),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict = {
            "name": self.name,
            "kind": self.kind,
            "members": list(self.members),
        }
        if self.leader is not None:
            payload["leader"] = self.leader
        if self.created_at:
            payload["created_at"] = self.created_at
        path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    # ── builder helpers used by mutation API ───────────────────────────────────

    def with_member_added(self, agent: str) -> "Topology":
        if agent in self.members:
            raise ValueError(f"topology {self.name!r}: {agent!r} already a member")
        return Topology(
            name=self.name,
            kind=self.kind,
            members=self.members + (agent,),
            leader=self.leader,
            created_at=self.created_at,
        )

    def with_member_removed(self, agent: str) -> "Topology":
        if agent not in self.members:
            raise ValueError(f"topology {self.name!r}: {agent!r} is not a member")
        if self.kind == "team" and agent == self.leader:
            raise ValueError(
                f"topology {self.name!r}: cannot remove leader; remove the topology instead"
            )
        return Topology(
            name=self.name,
            kind=self.kind,
            members=tuple(m for m in self.members if m != agent),
            leader=self.leader,
            created_at=self.created_at,
        )


__all__ = [
    "Topology",
    "TOPOLOGY_DIRNAME",
    "KINDS",
    "_validate_topology_name",
]
