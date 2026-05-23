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

## Declaration axis taxonomy (= when to use a bool flag vs a resource list)

The `permissions:` block in `skill.md` frontmatter mixes axes of three
shapes. This section names each axis explicitly and gives the criterion
for picking a shape when a new axis is added.

### Current axes

| Axis | Type | Granularity | Gate site | Notes |
|---|---|---|---|---|
| `file.read` | `list[{path, scope}]` | resource (per-path) | `require_file_read()` | scope ∈ {`just_path`, `recursive`} |
| `file.write` | `list[{path, scope}]` | resource (per-path) | `require_file_write()` | covers write / edit / delete |
| `python` | `list[{module, function, mode, timeout}]` | resource (per-step) | `require_python_step()` | mode ∈ {`safe`, `unsafe`} |
| `mcp` | `list[str]` | resource (per-server) | implicit at MCP call | per-server-name allowlist |
| `tool` | `list[str]` | resource (per-tool) | `require_tool()` | named-tool allowlist |
| `shell` | `bool` | abstract | `require_shell()` | binary: any shell access at all |
| `mcp_install` | `bool` (declaration) + per-server approval key | hybrid | `require_mcp_install()` | declaration is bool; approval persists per `<server_id>` |
| `mcp_drop_server` | `bool` (= same shape as `mcp_install`) | hybrid | `require_mcp_drop_server()` | counter-op to `mcp_install` |
| `index_drop` | `bool` (= same shape) | hybrid | `require_index_drop()` | RAG corpus / source drop |
| `cron_register` | `bool` (= same shape; per-job approval key) | hybrid | `require_cron_register()` | covers register / unregister / enable / disable |
| `allowed_mcp` | `list[str]` or `None` | ACL filter | implicit at MCP call | per-agent restriction, cross-cuts `mcp` |

### Criterion — `bool` axis vs `list` axis

A capability lives on a **bool axis** when **all** of the following hold:

1. The capability fires a **side-effect set** that no single resource scope can describe — e.g. config write + chain-notify peers + state-change emit (the `mcp_install` triad).
2. The skill author has no natural way to enumerate which instances will be touched at write-time (= "which server I'll install" is determined by the user / LLM at runtime, not by the skill author).
3. The user wants to see a **per-instance** approval surface at runtime even though the **declaration** is intent-shaped (= the hybrid shape: declaration bool, approval key resource-keyed).

A capability lives on a **list axis** when **all** of the following hold:

1. The capability is reducible to a **single I/O scope** (= one path / one host / one server).
2. The skill author knows the inventory at write-time (= "I'll read these specific paths").
3. Per-instance runtime approval is not needed beyond the declared list (= the list **is** the scope; no further per-call prompt unless config tier asks).

The **`allowed_mcp` ACL axis** is a third shape — it doesn't grant capability; it **restricts** which subset of an already-granted resource list a specific agent may use. ACL filters cross-cut both bool and list axes.

Worked examples:

| Capability | Shape | Why |
|---|---|---|
| `file.write` | list (resource) | single I/O scope (= one path); author knows the inventory; no chain effects |
| `mcp_install` | bool (hybrid) | side-effect set (= config write + emit + notify); author doesn't know server name at write time; user wants per-server prompt |
| `shell` | bool | side-effect set is unbounded (= shell is arbitrary process execution); author can't list "these specific commands" |
| `cron_register` | bool (hybrid) | side-effect set (= cron.yaml write + emit); job name determined at runtime; per-job approval key |

### Hidden axis — the cross-cutting "intent ↔ raw I/O" correlation

A bool axis declares **intent**; the actual side effects (= file writes / HTTP calls / process spawns) still happen through underlying primitives. When the **same raw I/O** is reachable directly (= via `file.write` in a default-zone path), the bool axis's intent is **bypassable**.

Concretely: `mcp_install: true` declares "this skill installs MCP servers", but the side effect ("write `.reyn/mcp.yaml`") is also reachable via `reyn.safe.file.write(".reyn/mcp.yaml", ...)` because `.reyn/` is in the default-zone write paths. The two paths don't reconcile — see [Known gaps](#known-gaps) below.

The correlation rule (= proposed canonical, not enforced today) is:

> When a bool axis B describes a side-effect set that includes raw I/O R, then performing R by any path (declared list OR default zone OR internal-OS code) MUST also pass B's gate.

Today's implementation does not enforce this rule for any of the four bool axes (`mcp_install`, `mcp_drop_server`, `index_drop`, `cron_register`).

## Trust boundary layers (= where enforcement actually happens)

The four execution surfaces that perform side-effects, ordered by enforcement strength:

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
│  reyn package internal code (OS handlers, registry client)   │  ← WEAKEST
│    permission_resolver not in the call path                  │
│    Trust boundary: this code IS the OS, gates its callers    │
│    not itself                                                │
└──────────────────────────────────────────────────────────────┘
```

The asymmetry is intentional for the top and bottom layers and a current limitation for the middle two:

- **Top (sandboxed_exec)** is the only layer with OS-kernel enforcement. argv / network / fs scope is declarative per call and enforced by the platform sandbox.
- **Bottom (reyn package internal)** is trusted by construction — the OS gates its callers, not itself. Adding gates here would require either a separate inside-OS permission model (= cyclic) or moving the I/O out to the `op_runtime` layer (= where gates already exist).
- **Safe-mode python** is honor-system: AST validation prevents `import os`, and `reyn.safe.file` checks declared paths. A motivated user with `mode: unsafe` access can bypass; a non-motivated `mode: safe` author cannot accidentally bypass via normal coding patterns.
- **Unsafe-mode python** is trust-by-declaration: the operator approves `--allow-unsafe-python` at runtime and accepts that the step has full host access.

## Industry comparison (= reference for design choices)

| Platform | Declaration shape | Runtime ask | Granularity | Enforcement |
|---|---|---|---|---|
| iOS (TCC + Entitlements) | `Info.plist` capability + purpose string | First-use prompt | Capability axis | OS kernel + signed entitlements |
| Android (≥ M) | `AndroidManifest.xml` `uses-permission` | First-use prompt for "dangerous" tier | Permission class + scoped storage | OS kernel + per-app UID |
| Web Permissions API | Per-feature query | Per-permission prompt | **Origin-scoped** (= per-domain capability) | Browser sandbox |
| Anthropic Claude Code | Tool list (Bash / Edit / Read / Write) | None at default; sandbox-mode optional | Tool name (no path scope) | Seatbelt (sandbox-mode) or trust |
| MCP servers | Server-side tool list exposed to client | Server owns its boundary | Per-tool, server-defined | Process boundary |
| **Reyn** | `permissions:` block (this doc) | startup_guard + interactive on first use | **Hybrid** (= per-path list + abstract bool) | AST + honor-system for safe-mode; kernel for `sandboxed_exec` only |

The industry pattern is **abstract capability declaration + runtime per-instance prompt + OS-kernel enforcement**. Reyn deviates on two axes:

1. **Granularity is finer than industry default** — path-list `file.read` / `file.write` is closer to Web's origin-scope than to iOS / Android's capability axis. The justification is that Reyn skills are workflow code (= author knows the file inventory), whereas iOS / Android apps are general-purpose (= author cannot enumerate at write-time).
2. **Enforcement is honor-system for safe-mode python** — iOS / Android rely on kernel boundaries; Reyn relies on AST validation + path-list checks. The trade-off is implementation simplicity (= no per-step seatbelt setup) for weaker enforcement.

These deviations are explicit design choices, but they constrain the safe-mode-python honor-system contract: anything that bypasses `reyn.safe.*` (= via an AST hole, or via a direct I/O path the AST doesn't catch) silently escapes the declared-path check. The bool-axis cross-cutting correlation rule from the criterion section above is the surface that this honor-system breakdown is most visible on.

## Known gaps (= 2026-05-23 audit, follow-up tracked)

Three architectural inconsistencies identified during the 2026-05-23 axis-taxonomy audit, captured here so the gap is explicit while individual remediation PRs are scoped separately.

### Gap A — bool intent vs raw I/O default-zone bypass

The four bool axes (`mcp_install`, `mcp_drop_server`, `index_drop`, `cron_register`) declare **intent** but their side-effect set (= config writes under `.reyn/`) is also reachable through `reyn.safe.file.write()` because `.reyn/` is a default-zone write path (= `src/reyn/kernel/preprocessor_executor.py:493-499`).

Concrete bypass: a safe-mode python step can write `.reyn/mcp.yaml` directly, mutating the MCP server registry, without declaring `mcp_install: true` and without going through `require_mcp_install()`'s approval prompt.

Remediation candidates (= future PR):

- Cross-axis correlation gate: `_check_write(path)` consults a "raw I/O → bool axis" registry (= `.reyn/mcp.yaml` → `mcp_install`, `.reyn/cron.yaml` → `cron_register`, etc.) and additionally enforces the bool axis when the path matches.
- OR move the canonical MCP-registry mutation out of `safe.file` reach (= keep it inside `op_runtime/mcp_install.py` only, refuse direct write at the safe.file layer).

The cross-cutting rule is articulated in the [Declaration axis taxonomy](#declaration-axis-taxonomy--when-to-use-a-bool-flag-vs-a-resource-list) section above; no implementation enforces it today.

### Gap B — reyn package internal code bypasses the permission resolver

OS-internal code (= `src/reyn/registry/client.py` MCP registry HTTP, `src/reyn/op_runtime/mcp_install.py` config writes) performs I/O without consulting `PermissionResolver`. This is a deliberate trust boundary — the OS gates its callers, not itself — but the boundary is undocumented.

This gap is **not necessarily a bug**: any tight gate around internal OS code would either need a separate inside-OS permission model (= cyclic) or push the I/O into the `op_runtime` layer (= where gates already exist). Either is a significant architectural commitment.

Disposition: documented as a known trust-boundary choice. Re-open if a specific OS-internal I/O path becomes user-visible and operators want to gate it (= e.g. logging a `web.fetch` event for the MCP registry call).

### Gap C — `safe.http` exists but is un-gated

`src/reyn/safe/http.py` (= landed during FP-0042 Phase 3 drift-fix) ships `get` / `post` / `put` / `delete` — urllib-backed, callable from safe-mode steps via the AST allowlist. However it has **no per-call permission gate**: the module's docstring is explicit that the "safe" label here matches the namespace (= AST-allowlisted) rather than the stronger per-call permission-resolver pattern that `reyn.safe.file` enforces, and the docstring references this issue as the design question to resolve.

Stdlib usage today:

- `mcp_search` / `mcp_install` use the domain-specific `reyn.safe.mcp.registry` (= hardcoded registry URL, no host parameter exposed to the skill).
- `skill_search` / `skill_importer` use bare `reyn.safe.http.get` (= arbitrary URL, no host check).

The shape question (= what gate to add when one is added) remains:

- **Per-host allowlist** (= `http.get: [{host: "registry.modelcontextprotocol.io"}]`) — matches the resource-list criterion (single I/O scope, author knows the inventory).
- **Bool flag** (= `http: true`) — matches the bool criterion only if HTTP carries side effects beyond the fetch itself (= it doesn't, by definition; HTTP GET is read-only).
- **Origin-scope with method** (= Web Permissions API style) — most aligned with industry pattern, granular enough for Reyn's workflow-code use case.

Per the criterion section above: HTTP read is a single I/O scope, author knows the hosts at write-time, no side effects beyond the fetch → resource list, **not** bool. The current un-gated state is the gap; adding a per-host allowlist on `safe.http` is the consistent remediation.

## `python` permission and `mode: safe` allowlist

The `python` permission has two levels:

| Level | Config key | What it allows |
|-------|-----------|----------------|
| `safe` | `python.safe: allow` | Steps that import only from `PURE_STDLIB_ALLOWLIST` — clock, entropy, pure compute, and `__future__` (compiler directive). No filesystem, network, or process access. |
| `unsafe` | `python.unsafe: allow` | Steps that may import any module, including filesystem and network. |

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
