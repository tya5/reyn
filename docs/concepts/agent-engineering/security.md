---
type: concept
topic: architecture
audience: [human, agent]
---

# Security

Capability gating, sandbox boundaries, and trust scoping. The bar is "no skill silently gets capabilities the user didn't authorize, and a compromised skill can't escalate to other skills."

## How reyn handles it

### The three-layer permission model

```
defaults (always on)
   ↓ if a skill needs more
phase declarations → user approves at startup
   ↓ if you trust the project broadly
project-wide pre-approval (reyn.yaml)
```

Defaults are conservative — read anywhere under the project root, write only under `.reyn/` and `reyn/`, no shell, no MCP, no Python. Anything beyond requires opt-in at one of the upper layers.

### Phase-level declarations + interactive approval

A phase declares the capabilities it needs in its frontmatter; at startup the runtime shows a single approval prompt. Persistent choices land in `.reyn/approvals.yaml`, keyed by `<skill>/<op>/<path>`.

### Skill-scoped approvals

Approvals are keyed by skill, not by user. If skill A is granted `file.write:/tmp/output`, sub-skill B (invoked via `run_skill`) does not transitively inherit that grant — B has to ask for its own. This is the composition-safety property: trusting one skill doesn't trust everything it might call.

### AST sandbox for Python preprocessor steps

`python` preprocessor steps run in one of two modes:

- **`safe`** — AST-validated against an allowlist (no `open`, `eval`, `exec`, `__import__`, `compile`, `subprocess`, etc.). Imports limited to a curated allowlist (`math`, `statistics`, `json`, `re`, `random`, `time`, `datetime`, …), extensible via `reyn.yaml`. Restricted `__builtins__`. Executes in a subprocess with a wall-clock timeout for crash isolation.
- **`unsafe`** — no AST checks, full Python. Requires `--allow-unsafe-python` at runtime and a `permissions.python` entry with `mode: unsafe` in `skill.md`. Used only when `safe` blocks something genuinely needed.

Skill authors are nudged toward `safe`; reaching for `unsafe` is a deliberate choice that the linter can flag.

### Non-interactive approval (eval, CI)

`reyn eval` does not prompt. Permissions must be in place before the run — either pre-approved in `reyn.yaml` (`permissions.<key>: allow`) or persisted from a prior interactive run. The trust model doesn't change between modes; eval just inherits the decisions you've already made.

## Where it's still thin

**Prompt injection is not specifically defended.** If untrusted text reaches the LLM (a fetched web page, a user-supplied file), the LLM may follow embedded instructions. reyn's permission system bounds the *capabilities* a compromised LLM can reach (it cannot write outside approved paths, cannot run shell without `--allow-shell`, etc.) but does not pre-screen LLM input for injection. Defenses for that layer (input filtering, dual-LLM patterns, output gating) belong in the skill design, not the OS.

**`mode: unsafe` is OS-level trust, not OS-level sandbox.** An unsafe Python step runs as the same user with the same filesystem access; it is not kernel-sandboxed. The system trusts that the user authorized the specific (module, function) pair. This is the right boundary for a developer tool — but it means unsafe steps deserve code review the way a Makefile target does.

## See also

- [../runtime/permission-model.md](../runtime/permission-model.md) — concept
- [Reference: permissions](../../reference/config/permissions.md) — full schema
- [Reference: reyn.yaml](../../reference/config/reyn-yaml.md) — `permissions:` key
- [How-to: manage permissions](../../guide/for-users/manage-permissions.md)
- [reliability-engineering.md](reliability-engineering.md) — what happens when an op is denied
