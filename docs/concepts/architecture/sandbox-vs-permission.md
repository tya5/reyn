---
type: concept
topic: architecture
audience: [human, agent]
---

# Sandbox and permissions: orthogonal concerns

reyn has two separate systems that both gate what a skill can do.
They are **completely orthogonal** — they answer different questions and are
configured at different levels. Conflating them is a common source of confusion.

## Permission: can the skill use this capability?

`skill.permissions` (declared in `skill.md` frontmatter) describes the **access
policy** for a specific skill:

- What file paths may it read or write?
- May it make network requests? To which hosts?
- May it call shell commands or MCP tools?

Permissions are **skill-level**: each skill declares its own, and the operator
or user approves them. The runtime enforces them through the AgentLayer of the
[conjunctive permission model](permission-model.md#effective-permission-conjunctive-restrict-model).

```yaml
# skill.md
permissions:
  file.write:
    - path: "{{workspace}}/output"
      scope: recursive
  http.get:
    - host: "api.github.com"
```

**Who sets it:** the skill author declares, the operator/user approves.
**Question answered:** "Is this op allowed for this skill?"

## Sandbox: how is the skill contained?

`sandbox` (configured in `reyn.yaml` under `sandbox:`, or via CLI flags)
describes the **containment** model for the agent:

- Which backend enforces isolation (Seatbelt / Landlock / container / none)?
- What container image is used?
- What filesystem mounts or network restrictions apply?

Sandbox is **agent-level**: a single sandbox configuration applies to the whole
agent, not per-skill or per-phase. It is part of the operator's deployment
configuration, not something skill authors declare.

```yaml
# reyn.yaml
sandbox:
  backend: auto     # auto | seatbelt | landlock | noop
```

**Who sets it:** the operator.
**Question answered:** "How is the process that runs skills contained?"

## How they combine

Permission and sandbox are applied independently and conjunctively:

```
allowed = permission_check(skill, op) AND sandbox_check(backend, op)
```

The permission system may allow an op that the sandbox still denies — for
example, a skill with `http.get: [{host: "api.github.com"}]` permission running
under a `network: false` sandbox policy will be denied at the sandbox layer. The
skill author cannot override the operator's sandbox configuration.

Conversely, the sandbox may allow something the permission system denies — for
example, a broad sandbox configuration does not grant a skill permission to call
shell ops it hasn't declared.

## Summary

| Axis | Permission | Sandbox |
|---|---|---|
| Level | Skill-level | Agent-level |
| Declared by | Skill author | Operator |
| Approved by | User / operator | Operator (config / CLI) |
| Covers | Op access policy (what may this skill do?) | Containment (how is the process isolated?) |
| Lives in | `skill.md` frontmatter `permissions:` | `reyn.yaml` `sandbox:` / CLI |

## See also

- [Permission model](../runtime/permission-model.md) — authorization layers,
  conjunctive restrict model, protected write paths
- ADR-0037 (internal) — design decision record: sandbox/permission separation
  and the migration from phase-level to agent-level sandbox policy
