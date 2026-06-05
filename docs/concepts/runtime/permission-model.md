---
type: concept
topic: architecture
audience: [human, agent]
---

# Permission model

reyn's permission system gates four kinds of capability: file paths, shell, MCP tool calls, and Python preprocessor steps. The defaults are conservative; anything beyond them must be declared by the skill **and** approved by the user (or pre-approved in `reyn.yaml`).

## Three layers, in order

> **Note:** These three layers describe how a capability gets *authorized* — the grant hierarchy. A separate orthogonal model — [the conjunctive restrict layers](#effective-permission-conjunctive-restrict-model) — describes how active runtime restrictions are combined at gate time. Both use the word "layers" but answer different questions; see the end of this page for the distinction.

```
┌──────────────────────────────┐  always allowed; nothing to declare
│  defaults (read-only project)│
└──────────────────────────────┘
             ↓ if skill needs more
┌──────────────────────────────┐  declare in skill.md frontmatter; user approves
│  skill declarations          │  approval persists to .reyn/approvals.yaml
└──────────────────────────────┘
             ↓ if you trust the project broadly
┌──────────────────────────────┐  reyn.yaml: permissions.<key>: allow
│  project-wide pre-approval   │  bypasses the prompt for that capability
└──────────────────────────────┘
```

### Layer 1: defaults

Read/glob/grep anywhere under the project root. Write/edit/delete only under `.reyn/` or `reyn/`. No shell, no MCP, no Python.

**Exception — protected write paths:** A small set of paths inside `.reyn/` are carved out from the default write grant because a direct write would bypass an authorization or audit surface. The carve-out is deliberately narrow — a path needs it only when it lacks a *downstream* gate.

| Protected path | Backs | Why carved out |
|---|---|---|
| `.reyn/approvals.yaml` | The persistent approval store — only the runtime authorization flow writes here | Permanent. It *is* the approval gate; there is no later use-gate, so a direct write would silently activate a never-approved grant on the next startup (#1199). |
| `.reyn/index/sources.yaml` | Index source registry | Transitional — carved out until the S3.4 part1 op-layer gate lands. |

**Protect-at-use (principle).** A config-write carve-out is *redundant* when the capability it configures is gated downstream at use time. `.reyn/mcp.yaml` and `.reyn/cron.yaml` were therefore **removed** from the carve-out:

- `.reyn/mcp.yaml` — writing it (installing a server) grants nothing on its own. *Using* a server still passes a per-server check at call time (`require_mcp`), so download + execute of the server package is gated regardless of who wrote the config.
- `.reyn/cron.yaml` — registering a job goes through the standard tool gate (`require_cron_register` / `require_file_write`); fired jobs run only under a user-launched in-process scheduler, and each fired op is itself permission-gated.

A skill that legitimately needs to write a *still-protected* path must declare it explicitly (e.g. `file.write: [{path: ".reyn/index/sources.yaml"}]` in `skill.md` frontmatter) and obtain the corresponding approval. The intended route remains the appropriate gated op handler — not direct file writes.

**Residual risk.** With mcp/cron at protect-at-use, a safe-mode step *can* now write `.reyn/mcp.yaml` / `.reyn/cron.yaml` directly via the broad `.reyn/` zone. This is intentional and bounded: the write changes only inert configuration. The authority it appears to grant (an MCP server, a cron job) is not realized until the gated use path (`require_mcp` / scheduler + op gates) is crossed, which a config write cannot bypass. The approval store keeps its carve-out precisely because it has no such downstream gate.

### Layer 2: skill declarations

A skill that needs something outside the defaults declares it in its `skill.md` frontmatter. At skill startup, the runtime shows a single approval prompt:

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
    safe: allow
    unsafe: allow
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
    safe: allow
    unsafe: allow
```

This grants project-wide pre-approval for the local environment without affecting
committed `reyn.yaml` or production users.  Interactive TTY runs elsewhere still see
startup_guard prompts as documented.

## Why skill-scoped keys

Approvals are keyed by skill, not globally. If skill A asks "can you write to `/tmp/foo`?", granting it doesn't grant skill B the same access.

The reason is composition safety. Skill A might be trusted; skill A invoking sub-skill B (via `run_skill`) doesn't transitively grant B's permissions. B has to ask for its own.

## `mcp_install` permission {#mcp_install-permission}

> Compat-shim form during the [Collapse arc](#collapse-arc-571). The canonical decomposition is `file.write: [.reyn/mcp.yaml]` + `http.get: [{host: registry.modelcontextprotocol.io}]` + `secret.write: [<env_key>]`; the bool form below is preserved through Phase 4.

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

## Permission is an OS I/O primitive

The permission system is part of the OS runtime, not a separate layer above it. Every side-effect performed by reyn — whether from skill code, op handler, or any other OS-internal path — goes through the same permission resolver against the calling skill's `PermissionDecl`. There is no inside/outside split: the OS uses the permission system as its core abstraction for all I/O.

Concretely, `op_runtime/mcp_install.py` writing `.reyn/mcp.yaml` routes through `reyn.safe.file.write` — the same gate a skill-level safe-mode python step would use. The PermissionDecl in scope is the skill's; the OS honors it uniformly regardless of where the call originates. The older "OS gates its callers, not itself" framing is dissolved by this: a single uniform mechanism, no cyclic concern.

## Declaration axis taxonomy

Each side-effect kind has a corresponding declarable axis. The axis vocabulary is small, and **bool axes are reserved for truly capability-shaped operations** — those not reducible to a single file / network / secret I/O scope.

### Axes

| Axis | Type | Granularity | Gate site | Notes |
|---|---|---|---|---|
| `file.read` | `list[{path, scope}]` | per-path | `require_file_read()` | scope ∈ {`just_path`, `recursive`} |
| `file.write` | `list[{path, scope}]` | per-path | `require_file_write()` | covers write / edit / delete |
| `http.get` | `list[{host}]` | per-host | `require_http_get()` | specific host = startup prompt + silent runtime; `"*"` wildcard = per-host runtime prompt. Covers both `reyn.safe.http.*` (skill-internal, specific only) and `web_fetch` (LLM-driven, accepts wildcard) |
| `secret.write` | `list[<key>]` | per-key | `require_secret_write()` | per-key for `~/.reyn/secrets.env`; `"*"` wildcard for runtime-determined keys (= the per-value prompt is the actual gate) |
| `mcp` | `list[str]` | per-server | implicit at MCP call | per-server-name allowlist |
| `python` | `list[{module, function, mode, timeout}]` | per-step | `require_python_step()` | mode ∈ {`safe`, `unsafe`} |
| `tool` | `list[str]` | per-tool | `require_tool()` | named-tool allowlist |
| `shell` | `bool` | abstract | `require_shell()` | binary: any shell access at all |
| `allowed_mcp` | `list[str] \| None` | ACL filter | implicit at MCP call | per-agent restriction, cross-cuts `mcp` |

### Why `shell` is the only bool

`shell` is process exec of an arbitrary command. The side-effect set is unbounded (= a shell command can read any file, write any file, network any host) and the author cannot enumerate which side effects a particular invocation will produce. There is no single I/O scope to reduce it to — process exec **is** the irreducible primitive.

Every other former bool axis (`mcp_install`, `mcp_drop_server`, `cron_register`, `index_drop`) has been re-expressed as one or more list axes, because each is actually reducible to a small set of file / network / secret operations:

| Former bool axis | Equivalent list-axis decomposition |
|---|---|
| `mcp_install: true` | `file.write: [.reyn/mcp.yaml]` + `http.get: [{host: registry.modelcontextprotocol.io}]` + `secret.write: [<env_key>]` |
| `mcp_drop_server: true` | `file.write: [.reyn/mcp.yaml]` |
| `cron_register: true` | `file.write: [.reyn/cron.yaml]` |
| `index_drop: true` | `file.write: [.reyn/index/sources.yaml]` + delete on `.reyn/index/<source>/index.db` |

The criterion is: **if a capability reduces to a finite I/O scope (file path / host / secret key), use a list axis; otherwise use bool**. Currently the only irreducible primitive is shell.

### What was lost in the collapse, and what wasn't

Bool axes carried a per-instance approval surface (= `mcp_install:<server_id>` keyed per server). After collapse:

- **MCP per-server granularity is preserved** at *call time* via the existing `permissions.mcp: [<server>]` axis. Installing a server (= writing `.reyn/mcp.yaml`) becomes a one-step grant; using a specific server still requires the call-time per-server check, so download + execute of the server's package still passes a per-server gate.
- **Cron per-job granularity is reduced** to "may write `.reyn/cron.yaml` at all", but cron-fired skills still go through their own runtime permission gates when they execute. The granularity reduction does not bypass downstream protections.
- **Index per-source granularity is reduced** — there is no equivalent post-write gate. Drop is destructive and the per-source distinction was operator-UX, not security; the reduction is accepted.

### `allowed_mcp` is an ACL filter, not a capability

`allowed_mcp` doesn't grant capability — it **restricts** which subset of an already-granted `mcp` server list a specific agent may use. ACL filters cross-cut capability axes.

## Trust boundary layers

The execution surfaces that perform side-effects, ordered by enforcement strength:

```
┌──────────────────────────────────────────────────────────────┐  ← STRONGEST
│  sandboxed_exec op (FP-0017)                                 │
│    OS-kernel enforcement (Seatbelt / Landlock / Seccomp)     │
│    argv-scoped, network-scoped, fs-scoped per-call           │
├──────────────────────────────────────────────────────────────┤
│  safe-mode python step (FP-0042)                             │
│    AST validation (= rejects `import os` at compile-time)    │
│    + reyn.safe.* honor-system path checks at function call   │
│    NOT kernel-sandboxed; subprocess runs with full user UID  │
├──────────────────────────────────────────────────────────────┤
│  unsafe-mode python step                                     │
│    No gate after the `--allow-unsafe-python` opt-in          │
│    Trusted-by-declaration: author asserts the step is safe   │
├──────────────────────────────────────────────────────────────┤
│  reyn package internal code (op handlers, registry client)   │
│    Uses the same `reyn.safe.*` primitives as skill code,     │
│    against the calling skill's PermissionDecl                │
└──────────────────────────────────────────────────────────────┘
```

- **Top (sandboxed_exec)** is the only layer with OS-kernel enforcement. argv / network / fs scope is declarative per call and enforced by the platform sandbox.
- **Internal OS code** uses the same `reyn.safe.*` primitives as skill code, against the calling skill's PermissionDecl. There is no inside/outside split — the OS exercises its own permission mechanism uniformly.
- **Safe-mode python** is honor-system: AST validation prevents `import os`, and `reyn.safe.*` checks declared paths / hosts / keys. A motivated user with `mode: unsafe` access can bypass; a non-motivated `mode: safe` author cannot accidentally bypass via normal coding patterns.
- **Unsafe-mode python** is trust-by-declaration: the operator approves `--allow-unsafe-python` at runtime and accepts that the step has full host access.

## Industry comparison

| Platform | Declaration shape | Runtime ask | Granularity | Enforcement |
|---|---|---|---|---|
| iOS (TCC + Entitlements) | `Info.plist` capability + purpose string | First-use prompt | Capability axis | OS kernel + signed entitlements |
| Android (≥ M) | `AndroidManifest.xml` `uses-permission` | First-use prompt for "dangerous" tier | Permission class + scoped storage | OS kernel + per-app UID |
| Web Permissions API | Per-feature query | Per-permission prompt | Origin-scoped (= per-domain capability) | Browser sandbox |
| Anthropic Claude Code | Tool list (Bash / Edit / Read / Write) | None at default; sandbox-mode optional | Tool name (no path scope) | Seatbelt (sandbox-mode) or trust |
| MCP servers | Server-side tool list exposed to client | Server owns its boundary | Per-tool, server-defined | Process boundary |
| **Reyn** | `permissions:` block (list-axis dominant; one bool: `shell`) | startup_guard + interactive on first use | per-path / per-host / per-server (resource scope) | AST + `reyn.safe.*` honor-system for safe-mode; kernel for `sandboxed_exec` |

Reyn deviates from the iOS / Android "capability + first-use prompt" pattern on two axes:

1. **Granularity is finer than industry default** — list-axis path / host / server scope is closer to Web's origin-scope than to iOS / Android's capability axis. The justification is that Reyn skills are workflow code (= author knows the inventory), whereas iOS / Android apps are general-purpose.
2. **Enforcement is honor-system for safe-mode python** — iOS / Android rely on kernel boundaries; Reyn relies on AST validation + path / host / key checks via the `reyn.safe.*` primitives. The trade-off is implementation simplicity (= no per-step seatbelt setup) for weaker enforcement.

## Collapse arc (#571)

The axis taxonomy above is the target state. The permissions audit identified that the prior design carried four bool axes (`mcp_install`, `mcp_drop_server`, `cron_register`, `index_drop`) which were redundant with `file.write` — the side effects all reduced to a canonical `.reyn/*.yaml` write reachable through `reyn.safe.file.write`, so the bool axes were duplicating coverage rather than gating new capability. The collapse arc removes them in stages:

| Phase | Scope | Status |
|---|---|---|
| 1 | This doc — articulate "permission is an OS I/O primitive" and the collapse map | this PR |
| 2 | Route `op_runtime` handlers (= `mcp_install` / `mcp_drop_server` / `cron_register` / `index_drop`) through `reyn.safe.file.write`; loader compat shim accepts both bool form and explicit list form | follow-up PR |
| 3 | Introduce `http.get: [{host}]` axis (= gates `reyn.safe.http.*` per-host) and `secret.write: [<key>]` axis (= gates `~/.reyn/secrets.env` writes per-key) | follow-up |
| 4 | Migrate stdlib skills to explicit list-axis form | follow-up |
| 5 | Remove bool axes (`mcp_install` etc.) and `require_mcp_install` / `require_cron_register` / `require_index_drop` / `require_mcp_drop_server` from the OS surface | follow-up |

During Phases 1–4 the bool form (= `mcp_install: true`) is accepted as a compat shim that implicitly expands to the equivalent list-axis decomposition. The bool form is removed in Phase 5.

### Phase 7 — prompt-timing model unification + `safe.http`/`web_fetch` collapse

Phase 7 finishes the alignment by giving the `http.get` axis the same prompt model as `file.write`:

- **Specific declared host** (`http.get: [{host: "api.github.com"}]`) — `startup_guard` prompts the operator once per `<skill, host>` and persists the decision to approvals.yaml under `<skill>/http.get/<host>`. Runtime is then silent. Mirrors `file.write` for paths outside the default zone.
- **Wildcard** (`http.get: [{host: "*"}]` or `["*"]`) — host set is unknown at write-time (= LLM picks at runtime, e.g. `web_fetch` follow-up of `web_search` results), so the prompt fires at the actual host gate inside `require_http_get`. Same `<skill>/http.get/<host>` persistence; ALWAYS / NEVER choices apply per host.
- **No declaration** — legacy `web.fetch` compat path with a `DeprecationWarning` until the segmented-migration window closes; existing skills that relied on Tier-1 default-allow keep working.

The `web_fetch` op handler routes through `require_http_get` instead of the legacy `require_web_fetch`; the chat router's PermissionDecl declares `http.get: [{host: "*"}]` so LLM-driven fetches go through the wildcard branch. The `reyn.safe.http` subprocess path strips wildcard entries at the preprocessor — sync subprocesses can't prompt, so wildcard-host fetches must go through the async `web_fetch` op route.

This unifies the two HTTP surfaces (`safe.http` skill-internal + `web_fetch` LLM-driven) under one axis with one prompt model. It matches the browser-extension `host_permissions` (= declared, install-time prompt) + Web Permissions API (= runtime per-feature prompt) hybrid — see the [Industry comparison](#industry-comparison) section.

| Aspect | Pre-Phase-7 | Post-Phase-7 |
|---|---|---|
| `safe.http` skill-internal | per-host decl, silent runtime, no prompt | unchanged for specific decl; wildcard rejected (= subprocess can't prompt) |
| `web_fetch` LLM-driven | Tier-1 default-allow, 4-layer per-URL prompt | routed through `http.get` axis; chat router decl carries wildcard so behaviour is preserved |
| Operator prompt granularity | per-URL (`web.fetch` key) | per-host (`<skill>/http.get/<host>` key) — ALWAYS covers all URLs on that host |
| Skill author control over LLM fetch scope | none | declare specific `http.get` hosts to constrain (= LLM can only fetch declared hosts; wildcard absent = no fallback) |
| Legacy `web.fetch: allow` / `deny` config | direct gate | honored as backward-compat alias inside `require_http_get` during the migration window |

## `python` permission and `mode: safe` allowlist

The `python` permission has two levels:

| Level | Config key | What it allows |
|-------|-----------|----------------|
| `safe` | `python.safe: allow` | Steps that import only from `PURE_STDLIB_ALLOWLIST` — clock, entropy, pure compute, and `__future__` (compiler directive). No filesystem, network, or process access. |
| `unsafe` | `python.unsafe: allow` | Steps that may import any module, including filesystem and network. |

`PURE_STDLIB_ALLOWLIST` is defined in `src/reyn/kernel/_python_allowlist.py`. `__future__` is in the list as a compiler directive — it carries no runtime capability.

**Non-interactive auto-allow**: when a stdlib skill is invoked via `reyn run` (non-interactive context), both `mode: safe` and `mode: unsafe` python steps are auto-allowed without a prompt. This mirrors the same non-interactive behavior already in place for other ops in eval/CI runs.

**The formal contract for `mode: safe`** (= "ambient sources only") is documented in [Python safe mode](../skills/python-safe-mode.md). That page covers the full allowlist rationale, the safe-vs-unsafe auto-allow rules by context, and the refactor pattern for converting unsafe steps to safe.

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

## Effective permission: conjunctive restrict model {#effective-permission-conjunctive-restrict-model}

The authorization layers above answer: *"has this capability been granted?"* A separate orthogonal question is: *"given all active restrictions, is this capability allowed right now?"* The conjunctive restrict model handles the second.

At gate time, a capability is permitted only if **every** active layer allows it:

```
effective = AgentLayer ∩ SandboxLayer ∩ ProfileLayer
allows(axis, value) = all(layer.allows(axis, value) for layer in layers)
```

### The three restrict layers

| Layer | What it models | Role |
|---|---|---|
| **AgentLayer** | Skill declaration + default zone baseline + runtime approvals | Grant layer |
| **SandboxLayer** | Runtime sandbox caps (paths, network, subprocess, env) | Restrict-only |
| **ProfileLayer** | Agent-level allowlists (skills, MCP servers) | Restrict-only |

`SandboxLayer` and `ProfileLayer` are **restrict-only**: they can narrow a permission, but cannot re-grant something the `AgentLayer` denied. This is a structural property of the conjunction (`all(...)`) — no layer's `False` can be overridden by any other layer.

### How the two "layer" concepts relate

Two distinct concepts both use the word "layers" in this document. They answer different questions:

| Concept | Question | Direction |
|---|---|---|
| Authorization 3 layers (grant hierarchy, top of page) | How does a capability get granted? | Hierarchical grant |
| Conjunctive restrict layers (this section) | Given current runtime restrictions, is the capability allowed? | Intersect — can only narrow |

They operate in sequence: authorization resolution (AgentLayer) determines whether the skill's declaration and approvals cover a capability; then the conjunctive intersection applies any active sandbox or profile restrictions. An approved capability can still be denied by `SandboxLayer` or `ProfileLayer` — grant-back is forbidden.

## What the permission system is NOT

- **Not a Linux capability sandbox.** A Python step in `mode: unsafe` runs as the same user; reyn doesn't sandbox the kernel.
- **Not a secret keeper.** Don't put credentials in approvals.yaml or rely on permissions to hide environment variables. Use [Concepts: secret handling](../runtime/secret-handling.md) for credentials.
- **Not protection against the user.** If you `permissions: shell: allow` in reyn.yaml, you've authorized shell. The system is protecting against accidental capability creep, not user intent.

## See also

- [Reference: permissions](../../reference/config/permissions.md) — full schema
- [Reference: reyn.yaml](../../reference/config/reyn-yaml.md) — `permissions:` key and `permissions.mcp_install`
- [Reference: state-dir](../../reference/config/state-dir.md) — `.reyn/approvals.yaml`
- [Concepts: secret handling](../runtime/secret-handling.md) — credential storage (`~/.reyn/secrets.env`)
- [Reference: `reyn mcp`](../../reference/cli/mcp.md) — `install` subcommand and `mcp_install` gate interaction
- [How-to: manage permissions](../../guide/for-users/manage-permissions.md)
