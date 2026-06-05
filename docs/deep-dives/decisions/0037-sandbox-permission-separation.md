# ADR-0037: Sandbox / permission separation — agent-level containment unification

**Status**: **Accepted (2026-06-05) — agent-level re-level in progress (#1326).**
Sandbox policy unifies at agent level; per-phase `default_sandbox_policy` is retired.

## Context

reyn has two systems that both constrain what a skill can do. They answered
different questions but were historically described as if they were related
layers of the same mechanism:

1. **Permission** (`skill.permissions`) — skill-level access policy: what file
   paths, network hosts, shell ops, MCP tools may this skill use? Declared by
   the skill author; approved by the operator/user. Enforced at dispatch time.
2. **Sandbox** (`SandboxPolicy`, `sandboxed_exec` op) — containment: how is
   the subprocess isolated at the kernel level? backend selection, read/write
   surface, network gate, deny-list. Configuration belongs to the operator, not
   the skill author.

### The FP-0017 divergence

FP-0017 (sandboxed-execution, landed 2026-05) introduced `SandboxPolicy` as
a per-phase / per-op declaration. This followed an intuition that sandbox
policy — like permissions — is skill-scope: skill authors should declare the
subprocess isolation their phases need, just as they declare file.write paths.

This model was implemented and shipped:
- `default_sandbox_policy` field in `phase.md` frontmatter (phase-scoped)
- `SandboxPolicy` kwargs on `sandboxed_exec` Control IR op (per-op)
- Used by swe_bench phases specifically

### Why it diverged from the design intent

The operator/user, not the skill author, controls the trust environment.
Sandbox is containment infrastructure: whether to use Seatbelt, Landlock, or a
container; what image; what mount points; what network policy. These are
deployment decisions — the same skill might run in a dev environment with
`noop` backend and a production environment with Seatbelt + container.

Allowing a skill author to declare their sandbox policy means a skill can
specify its own containment — which is not the right authority model. A skill
that declares `network: true` in its `default_sandbox_policy` would be
self-granting network access at the sandbox layer, which defeats the
operator's ability to enforce containment. This is the same class of issue
as a skill self-approving its own permissions.

The phase-scoped model also created a conceptual conflation with permissions:
both lived in frontmatter, both used similar vocabulary (`read_paths`,
`network`), and both were described as "what the skill needs." In practice they
are orthogonal and answer different questions.

## Decision

**Sandbox policy is agent-level operator configuration, not skill-level
declaration.**

- `reyn.yaml sandbox:` is the canonical location for sandbox backend and
  scoping configuration.
- CLI flags (`--sandbox`, `--image`, `--mount`) are the runtime override.
- `default_sandbox_policy` in `phase.md` frontmatter is **retired** (#1326): the
  key is no longer parsed (a phase that still declares it is silently ignored).
  Sandbox policy is set at the agent level instead.
- The `SandboxPolicy` on `sandboxed_exec` op fields still applies (per-op
  policy at execution time), but this is operator/OS context, not skill author
  declaration.

**Permission and sandbox are completely orthogonal:**

| | Permission | Sandbox |
|---|---|---|
| Level | Skill-level | Agent-level |
| Who declares | Skill author | Operator |
| Who approves | User/operator | Operator (config) |
| Covers | Op access policy | Process containment |
| Enforced at | Dispatch time | Subprocess kernel level |

## Implementation

- `#1326` — agent-level sandbox-policy re-level: retire `default_sandbox_policy`
  from the frontmatter; wire `reyn.yaml sandbox:` as the single configuration
  surface; add `--image` / `--mount` CLI flags for container mode.
- `docs/concepts/architecture/sandbox-vs-permission.md` — concept doc for
  operator and agent audiences (no history refs; published to site).
- `docs/concepts/runtime/sandbox.md` — updated to agent-level framing;
  retired `default_sandbox_policy`; updated SandboxPolicy field table.
- `docs/reference/dsl/phase-md.md` — removal note on `default_sandbox_policy`.

## Consequences

1. Skill authors no longer need to (or should) declare sandbox policy in
   `skill.md` or `phase.md`. This simplifies skill authoring.
2. Operators have a single, clear location for containment configuration.
3. The permission/sandbox orthogonality is explicitly documented; the two
   systems can evolve independently.
4. The swe_bench phases' `default_sandbox_policy` was migrated to the agent-level
   policy the eval harness injects (`reyn.yaml sandbox.policy`) — #1326.
