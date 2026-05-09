---
adr: 0031
title: "3-layer config cascade — deprecate <project>/.reyn/config.yaml"
status: Accepted
date: 2026-05-09
---

# ADR-0031 — 3-layer config cascade

## Status

Accepted

## Context

Reyn previously used a 4-layer config cascade (lowest → highest priority):

```
1. ~/.reyn/config.yaml         user global
2. <project>/reyn.yaml          project committed
3. <project>/reyn.local.yaml    project local (gitignored)
4. <project>/.reyn/config.yaml  project local (gitignored) ← tool-managed
```

The 4th layer (`<project>/.reyn/config.yaml`) was introduced to give
`reyn config set` and `reyn mcp install --scope local` a write target that
was separate from human-edited `reyn.local.yaml`. The motivation was to keep
tool writes and human edits in distinct files.

In practice this created friction:

1. **Onboarding confusion**: new users encountered two gitignored local
   config files (`reyn.local.yaml` and `.reyn/config.yaml`) with no clear
   separation rule. Claude Code and similar tools use a single local file.

2. **`.reyn/` opacity violated**: industry practice (git, Docker, npm) treats
   hidden directories as opaque runtime state managed exclusively by tooling.
   Placing a user-facing config file inside `.reyn/` contradicted this
   expectation and surfaced `.reyn/` in `reyn init` output and docs as a
   human-edited location.

3. **`reyn.local.yaml` already existed**: the project already had a well-named
   gitignored file for local overrides. Tool writes can go there too — the
   YAML merge is additive and idempotent, so human and tool edits coexist
   safely.

## Decision

Remove `<project>/.reyn/config.yaml` from the config cascade.

**After (3-layer)**:

```
1. ~/.reyn/config.yaml         user global
2. <project>/reyn.yaml          project committed
3. <project>/reyn.local.yaml    project local (gitignored — human + tool)
4. CLI flags                   per-invocation
```

The write target for `reyn config set` and `reyn mcp install --scope local`
changes from `<project>/.reyn/config.yaml` to `<project>/reyn.local.yaml`.

`.reyn/` is redefined as **opaque runtime state** only:
`events/`, `state/`, `chats/`, `memory/`, `approvals.yaml`, etc.
No user-facing config belongs there.

`~/.reyn/config.yaml` (user-global) is **not** affected — it remains layer 1.

## Migration

Phase 1 (this ADR): **warning + ignore**.

- If `<project>/.reyn/config.yaml` exists at startup, Reyn emits a warning
  to stderr and does **not** load the file.
- No automatic migration — the user manually runs:
  ```bash
  cat .reyn/config.yaml >> reyn.local.yaml   # merge manually
  rm .reyn/config.yaml
  ```

Phase 2 (future): automatic migration tool or removal of the warning code
once adoption is confirmed.

## Consequences

**Positive:**
- Single gitignored local file — matches industry convention (Claude Code,
  aider, Cursor all use one local config).
- `.reyn/` is truly opaque; gitignore entry `/.reyn/` is enough.
- Onboarding cost reduced: `reyn init` creates `reyn.yaml` +
  `reyn.local.yaml.example` only.
- `reyn mcp install --scope local` output (`Scope: local`) now unambiguously
  refers to `reyn.local.yaml`.

**Negative / risks:**
- Existing users with `.reyn/config.yaml` see a warning until they migrate.
  Warning message includes migration command.
- Tool writes (e.g. `reyn config set`) now go to `reyn.local.yaml` alongside
  human edits — potential for minor YAML comment loss on tool re-writes.
  Mitigated by the fact that `yaml.dump` preserves structure; comments are
  the only loss.

## See also

- ADR-0029: MCP install permission model (introduced `mcp_install` scope concept)
- ADR-0030: Universal secret handling (introduced `~/.reyn/secrets.env`)
- `docs/reference/config/reyn-yaml.md` — resolution order
- `docs/reference/config/state-dir.md` — `.reyn/` layout
