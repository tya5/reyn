---
type: concept
topic: architecture
audience: [human, agent]
---

# Permission model

reyn's permission system gates four kinds of capability: file paths, shell, MCP tool calls, and Python preprocessor steps. The defaults are conservative; anything beyond them must be declared by the skill **and** approved by the user (or pre-approved in `reyn.yaml`).

## Three layers, in order

```
┌──────────────────────────────┐  always allowed; nothing to declare
│  defaults (read-only project)│
└──────────────────────────────┘
             ↓ if skill needs more
┌──────────────────────────────┐  declare in phase frontmatter; user approves
│  phase declarations          │  approval persists to .reyn/approvals.yaml
└──────────────────────────────┘
             ↓ if you trust the project broadly
┌──────────────────────────────┐  reyn.yaml: permissions.<key>: allow
│  project-wide pre-approval   │  bypasses the prompt for that capability
└──────────────────────────────┘
```

### Layer 1: defaults

Read/glob/grep anywhere under the project root. Write/edit/delete only under `.reyn/` or `reyn/`. No shell, no MCP, no Python.

### Layer 2: phase declarations

A phase that needs something outside the defaults declares it in its frontmatter. At skill startup, the runtime shows a single approval prompt:

```
[approval] my_skill/file.write needs:
  /tmp/output (just_path)

  [y] allow this run only
  [j] persist for this exact path + skill
  [r] persist for the parent dir (recursive) + skill
  [N] deny
```

Persistent choices land in `.reyn/approvals.yaml` keyed by `<skill>/<op>/<path>`. Keys are skill-scoped — one skill's approval doesn't leak to another.

### Layer 3: project-wide pre-approval

`reyn.yaml` can pre-grant capabilities project-wide:

```yaml
permissions:
  shell: allow
  file.write: allow
  python:
    pure: allow
    trusted: allow
```

Use sparingly — `allow` removes the prompt entirely.

## Non-interactive runs

`reyn eval` runs without prompts. Approvals must be in place beforehand: either pre-approved in `reyn.yaml` or persisted to `.reyn/approvals.yaml` from a prior interactive run.

This is the same trust model: the eval doesn't get to decide what's safe; you do, in advance.

### reyn.local.yaml for operator-local pre-approval

For dogfood automation, CI runs, or any non-interactive scripted use, the natural
mechanism is `reyn.local.yaml` — a gitignored operator-personal override of `reyn.yaml`
(layer 3 project-wide pre-approval, scoped to the local machine).  Add:

```yaml
permissions:
  file:
    read: allow
  python:
    pure: allow
    trusted: allow
```

This grants project-wide pre-approval for the local environment without affecting
committed `reyn.yaml` or production users.  Interactive TTY runs elsewhere still see
startup_guard prompts as documented.

## Why skill-scoped keys

Approvals are keyed by skill, not globally. If skill A asks "can you write to `/tmp/foo`?", granting it doesn't grant skill B the same access.

The reason is composition safety. Skill A might be trusted; skill A invoking sub-skill B (via `run_skill`) doesn't transitively grant B's permissions. B has to ask for its own.

## `mcp_install` permission {#mcp_install-permission}

`mcp_install` gates **adding a new MCP server to the configuration** — it is distinct from `permissions.mcp` (which gates runtime tool calls from an already-configured server).

```yaml
permissions:
  mcp_install: ask      # deny | ask | allow (default: ask)
```

| Value | Behaviour |
|-------|-----------|
| `ask` (default) | Interactive prompt on first install per server ID. Approval persists to `.reyn/approvals.yaml` under `mcp_install:<server_id>`. |
| `allow` | Install proceeds without a prompt. |
| `deny` | All install attempts are rejected immediately. |

### Scope tiers

`mcp_install` participates in the standard three-tier merge:

```yaml
# ~/.reyn/config.yaml (user scope)
permissions:
  mcp_install: allow     # personal dev machine — no friction

# <project>/reyn.yaml (project scope — committed to git)
permissions:
  mcp_install: deny      # team-shared project — server list is centrally managed

# <project>/reyn.local.yaml (local scope — gitignored)
permissions:
  mcp_install: ask       # personal override for this project
```

### Enterprise use case: "approved servers only" policy

Combine `mcp_install: allow` with a private registry to allow installs while restricting which servers are visible:

```yaml
# enterprise reyn.yaml (project scope)
mcp:
  registries:
    - https://mcp-registry.internal.acme.com/    # private registry (approved servers only)
    - https://registry.modelcontextprotocol.io/   # public fallback (lower priority)
permissions:
  mcp_install: allow
```

With this configuration, team members can run `reyn mcp install <id>` freely — but only servers registered in the private registry are discoverable. The public registry is a fallback but any server installed from it still goes through the same audit trail (`mcp_server_installed` event). Combining `deny` on the public path via registry ordering creates a layered defence without requiring `deny` permission level.

### Audit trail

Every successful install emits a `mcp_server_installed` event with `server_id` and `scope`. Filter with:

```bash
grep '"mcp_server_installed"' .reyn/events.jsonl
```

## Permission Tier Model (FP-0022)

Reyn permissions operate on two axes:

**Axis 1 — Usage Declaration** (skill.md frontmatter `permissions:` block):
The skill author declares what ops the skill intends to use. An undeclared
op raises `PermissionError` immediately (analogous to Android `SecurityException`
when calling an API not in the manifest).

**Axis 2 — Authorization** (operator / user grants access):
Four resolution layers in `PermissionResolver._approve()`:

| Layer | Source | Persistence |
|---|---|---|
| 1 | `reyn.yaml` `permissions.<key>` | Static config |
| 2 | `.reyn/approvals.yaml` | Cross-session |
| 3 | In-memory session decision | Session only |
| 4 | Interactive prompt | → Layer 2 or 3 |

### Op tier classification

| Tier | Example ops | Declaration | Default | Config restriction |
|---|---|---|---|---|
| 0 | `run_skill`, `ask_user` | not required | unconditional pass | not possible |
| 1 | `web_search`, `web_fetch` | not required | allow | `deny` blocks |
| 2 | `mcp` | required | ask (4-layer) | `allow` pre-approves |
| 3 | `shell`, `file` (outside zone) | required | ask (4-layer) | `allow` pre-approves |

Tier 0 is "unconditional pass", not "default allow" — there is no config key
that could block these ops without breaking skill execution semantics.

### web_fetch behavior (FP-0022)

Before FP-0022: Required `web.fetch: allow` in config; otherwise the tool was
hidden from the router catalog (silently unavailable). Users who asked the agent
to look something up received a refusal with no prompt — a confusing UX.

After FP-0022: Default-allow with 4-layer approval. The tool is always in the
router catalog. First use triggers an interactive prompt (YES/NO/ALWAYS/NEVER).
`web.fetch: allow` pre-approves (existing behavior preserved). `web.fetch: deny`
blocks immediately.

### web_search config restriction (FP-0022)

`web_search` now respects `web.search: deny` in `reyn.yaml`
(raises `PermissionError` immediately). Default is allow — web search is
read-only with no side effects, so operator `deny` is the only sensible
restriction path. No interactive prompt is needed.

### SSL configuration for web_fetch and MCP registry (FP-0022 follow-up)

`reyn.yaml` supports declarative SSL settings for `web_fetch` and MCP registry
requests. This solves the corporate MITM proxy / custom PKI use case at config
level without requiring ad-hoc env-var configuration.

```yaml
web:
  fetch:
    verify_ssl: false          # bool — disable SSL verification entirely
    ca_bundle: /path/to/ca.pem # str  — custom CA bundle file path
```

Both fields are optional. Priority order (highest to lowest):

| Priority | Source | Effect |
|---|---|---|
| 1 | `web.fetch.ca_bundle` set | Pass the path to httpx `verify=<path>` (custom CA) |
| 2 | `web.fetch.verify_ssl: false` | Disable SSL verification (`verify=False`) |
| 3 | `web.fetch.verify_ssl: true` | Force SSL verification (`verify=True`) |
| 4 | Neither set (default) | `SSL_VERIFY` env var → `litellm.ssl_verify` → `SSL_CERT_FILE` → `True` |

`ca_bundle` takes precedence over `verify_ssl` when both are set. The existing
`SSL_VERIFY` / `SSL_CERT_FILE` env-var behavior is unchanged when neither field
is configured — there is no regression for environments that already use env vars.

**Common use cases:**

- **Corporate MITM proxy with internal CA**: set `ca_bundle: /etc/ssl/certs/corp-ca.pem`
- **Internal dev environment with self-signed certs**: set `verify_ssl: false`
- **Enforce verification regardless of env vars**: set `verify_ssl: true`

## `python` permission and `mode: safe` allowlist

The `python` permission has two levels:

| Level | Config key | What it allows |
|-------|-----------|----------------|
| `safe` | `python.pure: allow` (legacy key) | Steps that import only from `PURE_STDLIB_ALLOWLIST` — clock, entropy, pure compute, and `__future__` (compiler directive). No filesystem, network, or process access. |
| `unsafe` | `python.trusted: allow` | Steps that may import any module, including filesystem and network. |

`PURE_STDLIB_ALLOWLIST` is defined in `src/reyn/kernel/_python_allowlist.py`. `__future__` is in the list as a compiler directive — it carries no runtime capability.

**Non-interactive auto-allow**: when a stdlib skill is invoked via `reyn run` (non-interactive context), both `mode: safe` and `mode: unsafe` python steps are auto-allowed without a prompt. This mirrors the same non-interactive behavior already in place for other ops in eval/CI runs.

**The formal contract for `mode: safe`** (= "ambient sources only") is documented in [Python safe mode](python-safe-mode.md). That page covers the full allowlist rationale, the safe-vs-unsafe auto-allow rules by context, and the refactor pattern for converting unsafe steps to safe.

## Per-skill credential scoping (FP-0016 D)

### Threat model: Confused Deputy

When a parent skill invokes a sub-skill via `run_skill`, the sub-skill executes
with the parent's full authority if no scoping is applied. A malicious document
processed by the sub-skill could instruct it to read credentials it has no
legitimate need for and include them in its output — a classic **Confused Deputy**
attack where the OS is tricked into using its authority on behalf of an adversary.

### `required_credentials` declaration

Sub-skills declare their credential needs in `skill.md` frontmatter:

```yaml
# skill.md
name: github_pr_reviewer
required_credentials:
  - github_token
  - atlassian_token
```

The default — when `required_credentials` is omitted — is `["*"]`, which grants
full credential delegation. This preserves backward compatibility for existing
skills written before FP-0016.

To explicitly declare that a skill needs no credentials at all, use an empty list:

```yaml
required_credentials: []
```

### How `run_skill` narrows the scope

At the `run_skill` boundary, the OS constructs a `ScopedSecretStore` from the
sub-skill's `required_credentials` declaration and intersects it with the
parent's already-scoped store. A sub-skill can never gain credentials the parent
does not itself hold:

```
parent scope: {"github_token", "stripe_key", "datadog_key"}
sub-skill declares: ["github_token", "slack_token"]
effective scope: {"github_token"}  ← intersection; slack_token not in parent
```

If the parent store is unrestricted (`["*"]`), the sub-skill's declared list is
honoured as-is (no intersection needed).

### `CredentialScopeError`

Any attempt by the sub-skill to read a credential outside its effective allowed
set raises `CredentialScopeError` (a `PermissionError` subclass). Enumeration is
also blocked: `list_visible_keys()` returns only keys that are both allowed and
present — out-of-scope keys are invisible, not just unreadable.

```python
from reyn.secrets import ScopedSecretStore, CredentialScopeError

store = ScopedSecretStore(allowed_keys=["github_token"], path=secrets_path)
store.get("github_token")    # ok — returns value
store.get("stripe_key")      # raises CredentialScopeError
"stripe_key" in store        # False — no raise, no leak
store.list_visible_keys()    # ["github_token"] only
```

### Audit trail

Every `run_skill` invocation emits a `sub_skill_credential_scope` P6 event
recording the effective allowed key set for that invocation:

```bash
grep '"sub_skill_credential_scope"' .reyn/events.jsonl
```

The event payload contains `skill` (sub-skill name) and `allowed_keys` (sorted
list, or `["*"]` for unrestricted). This makes every sub-skill credential grant
auditable and replay-capable (P6).

## What the permission system is NOT

- **Not a Linux capability sandbox.** A Python step in `mode: unsafe` runs as the same user; reyn doesn't sandbox the kernel.
- **Not a secret keeper.** Don't put credentials in approvals.yaml or rely on permissions to hide environment variables. Use [Concepts: secret handling](secret-handling.md) for credentials.
- **Not protection against the user.** If you `permissions: shell: allow` in reyn.yaml, you've authorized shell. The system is protecting against accidental capability creep, not user intent.

## See also

- [Reference: permissions](../reference/config/permissions.md) — full schema
- [Reference: reyn.yaml](../reference/config/reyn-yaml.md) — `permissions:` key and `permissions.mcp_install`
- [Reference: state-dir](../reference/config/state-dir.md) — `.reyn/approvals.yaml`
- [Concepts: secret handling](secret-handling.md) — credential storage (`~/.reyn/secrets.env`)
- [Reference: `reyn mcp`](../reference/cli/mcp.md) — `install` subcommand and `mcp_install` gate interaction
- [How-to: manage permissions](../guide/for-users/manage-permissions.md)
