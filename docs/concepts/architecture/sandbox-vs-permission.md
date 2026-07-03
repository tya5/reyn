---
type: concept
topic: architecture
audience: [human, agent]
---

# Sandbox and permissions: orthogonal concerns

reyn has two separate systems that both gate what a workflow can do.
They are **completely orthogonal** — they answer different questions and are
configured at different levels. Conflating them is a common source of confusion.

## Permission: can the workflow use this capability?

`skill.permissions` (declared in `skill.md` frontmatter) describes the **access
policy** for a specific workflow:

- What file paths may it read or write?
- May it make network requests? To which hosts?
- May it call shell commands or MCP tools?

Permissions are **workflow-level**: each workflow declares its own, and the operator
or user approves them. The runtime enforces them through the AgentLayer of the
[conjunctive permission model](../runtime/permission-model.md#effective-permission-conjunctive-restrict-model).

```yaml
# skill.md
permissions:
  file.write:
    - path: "{{workspace}}/output"
      scope: recursive
  http.get:
    - host: "api.github.com"
```

**Who sets it:** the workflow author declares, the operator/user approves.
**Question answered:** "Is this op allowed for this workflow?"

## Sandbox: how is the workflow contained?

`sandbox` (configured in `reyn.yaml` under `sandbox:`, or via CLI flags)
describes the **containment** model for the agent:

- Which backend enforces isolation (Seatbelt / Landlock / container / none)?
- What container image is used?
- What filesystem mounts or network restrictions apply?

Sandbox is **agent-level**: a single sandbox configuration applies to the whole
agent, not per-workflow or per-phase. It is part of the operator's deployment
configuration, not something workflow authors declare.

```yaml
# reyn.yaml
sandbox:
  backend: auto     # auto | seatbelt | landlock | noop
```

**Who sets it:** the operator.
**Question answered:** "How is the process that runs workflows contained?"

## How they combine

Permission and sandbox are applied independently and conjunctively:

```
allowed = permission_check(skill, op) AND sandbox_check(backend, op)
```

The permission system may allow an op that the sandbox still denies — for
example, a workflow with `http.get: [{host: "api.github.com"}]` permission running
under a `network: false` sandbox policy will be denied at the sandbox layer. The
workflow author cannot override the operator's sandbox configuration.

Conversely, the sandbox may allow something the permission system denies — for
example, a broad sandbox configuration does not grant a workflow permission to call
shell ops it hasn't declared.

## Summary

| Axis | Permission | Sandbox |
|---|---|---|
| Level | Workflow-level | Agent-level |
| Declared by | Workflow author | Operator |
| Approved by | User / operator | Operator (config / CLI) |
| Covers | Op access policy (what may this workflow do?) | Containment (how is the process isolated?) |
| Lives in | `skill.md` frontmatter `permissions:` | `reyn.yaml` `sandbox:` / CLI |

## See also

- [Permission model](../runtime/permission-model.md) — authorization layers,
  conjunctive restrict model, protected write paths
- ADR-0037 (internal) — design decision record: sandbox/permission separation
  and the migration from phase-level to agent-level sandbox policy
