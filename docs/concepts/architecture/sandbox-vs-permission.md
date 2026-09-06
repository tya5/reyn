---
type: concept
topic: architecture
audience: [human, agent]
---

# Sandbox and permissions: orthogonal concerns

reyn has two separate systems that both gate what a workflow can do.
They are **completely orthogonal** — they answer different questions and are
configured at different levels. Conflating them is a common source of confusion.

## Permission: can this agent/session use this capability?

`permissions:` (declared in `reyn.yaml`'s `permissions:` block —
[reference](../../reference/config/permissions.md)) describes the **access
policy** in force:

- What file paths may it read or write?
- May it make network requests? To which hosts?
- May it call shell commands or MCP tools?

Permissions are **project-level**, not workflow-level: there is no `skill.md`
frontmatter `permissions:` key — an earlier version of this page (and of
`reference/config/permissions.md`) described one, and a parser for it was
never built (#5863). The operator declares the project's `reyn.yaml`, and a
just-in-time prompt covers an access that declaration does not. The runtime
enforces the declared set through the AgentLayer of the
[conjunctive permission model](../runtime/permission-model.md#effective-permission-conjunctive-restrict-model).

```yaml
# reyn.yaml
permissions:
  file.write:
    - path: "{{workspace}}/output"
      scope: recursive
  http.get:
    - host: "api.github.com"
```

**Who sets it:** the operator declares in `reyn.yaml`; the operator/user approves any JIT prompt.
**Question answered:** "Is this op allowed?"

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
allowed = permission_check(op) AND sandbox_check(backend, op)
```

The permission system may allow an op that the sandbox still denies — for
example, `http.get: [{host: "api.github.com"}]` permission granted in
`reyn.yaml`, running under a `network: false` sandbox policy, is still denied
at the sandbox layer: a `permissions:` grant cannot override the sandbox
configuration.

Conversely, the sandbox may allow something the permission system denies — for
example, a broad sandbox configuration does not itself grant permission to
call a shell op `reyn.yaml`'s `permissions:` block has not declared.

## Summary

| Axis | Permission | Sandbox |
|---|---|---|
| Level | Project-level (per `reyn.yaml`) | Agent-level |
| Declared by | Operator | Operator |
| Approved by | User / operator (JIT prompt) | Operator (config / CLI) |
| Covers | Op access policy (what may this project's agents do?) | Containment (how is the process isolated?) |
| Lives in | `reyn.yaml` `permissions:` | `reyn.yaml` `sandbox:` / CLI |

## See also

- [Permission model](../runtime/permission-model.md) — authorization layers,
  conjunctive restrict model, protected write paths
- ADR-0037 (internal) — design decision record: sandbox/permission separation
  and the migration from phase-level to agent-level sandbox policy
