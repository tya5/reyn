---
type: concept
topic: architecture
audience: [human, agent]
---

# Permission model

reyn's permission system gates four kinds of capability: file paths, shell, MCP tool calls, and Python preprocessor steps. The defaults are conservative; anything beyond them must be declared by the workflow **and** approved by the user (or pre-approved in `reyn.yaml`).

## Three layers, in order

> **Note:** These three layers describe how a capability gets *authorized* — the grant hierarchy. A separate orthogonal model — [the conjunctive restrict layers](#effective-permission-conjunctive-restrict-model) — describes how active runtime restrictions are combined at gate time. Both use the word "layers" but answer different questions; see the end of this page for the distinction.

```
┌──────────────────────────────┐  always allowed; nothing to declare
│  defaults (read-only project)│
└──────────────────────────────┘
             ↓ if the actor needs more
┌──────────────────────────────┐  declared in reyn.yaml `permissions:` (e.g. a
│  declared capability         │  file.write path list); prompted once at the
└──────────────────────────────┘  point of actual use, not at startup
             ↓ if you trust the project broadly
┌──────────────────────────────┐  reyn.yaml: permissions.<key>: allow
│  project-wide pre-approval   │  bypasses the prompt for that capability
└──────────────────────────────┘
```

### Layer 1: defaults

Read/glob/grep anywhere under the project root. Write/edit/delete only under `.reyn/`. No shell, no MCP, no Python.

**Exception — protected write paths:** A small set of paths inside `.reyn/` are carved out from the default write grant because a direct write would bypass an authorization or audit surface. The carve-out is deliberately narrow — a path needs it only when it lacks a *downstream* gate.

| Protected path | Backs | Why carved out |
|---|---|---|
| `.reyn/approvals.yaml` | The persistent approval store — only the runtime authorization flow writes here | Permanent. It *is* the approval gate; there is no later use-gate, so a direct write would silently activate a never-approved grant on the next startup (#1199). |
| `.reyn/index/sources.yaml` | Index source registry | Transitional — carved out until the index write-gate is effective end-to-end (#1320: the postprocessor scope must carry a sandbox-policy source; the S3.4 part1 op-layer gate alone does not fire in the real index flow). |

**Protect-at-use (principle).** A config-write carve-out is *redundant* when the capability it configures is gated downstream at use time. `.reyn/config/mcp.yaml` and `.reyn/config/cron.yaml` were therefore **removed** from the carve-out:

- `.reyn/config/mcp.yaml` — writing it (installing a server) grants nothing on its own. *Using* a server still passes a per-server check at call time (`require_mcp`), so download + execute of the server package is gated regardless of who wrote the config.
- `.reyn/config/cron.yaml` — registering a job goes through the standard `require_file_write` gate against the canonical config path; fired jobs run only under a user-launched in-process scheduler, and each fired op is itself permission-gated.

An actor that legitimately needs to write a *still-protected* path must declare it explicitly (e.g. `permissions.file.write: [{path: ".reyn/index/sources.yaml"}]` in `reyn.yaml`) and obtain the corresponding approval. The intended route remains the appropriate gated op handler — not direct file writes.

**Residual risk.** With mcp/cron at protect-at-use, a safe-mode step *can* now write `.reyn/config/mcp.yaml` / `.reyn/config/cron.yaml` directly via the broad `.reyn/` zone. This is intentional and bounded: the write changes only inert configuration. The authority it appears to grant (an MCP server, a cron job) is not realized until the gated use path (`require_mcp` / scheduler + op gates) is crossed, which a config write cannot bypass. The approval store keeps its carve-out precisely because it has no such downstream gate.

### Layer 2: declared capability

An actor that needs something outside the defaults is declared in `reyn.yaml`'s `permissions:` block (`PermissionDecl`, built from `permissions.file.write` / `file.read` / `mcp` / `tool` / `http.get` / `secret.write` lists). Declaring a path doesn't itself grant access — it just makes the runtime aware the actor may need it. The prompt fires just-in-time, at the point the path is actually accessed (not at startup):

```
[approval] chat_router/file.write needs:
  /tmp/output (just_path)

  [y] allow this run only
  [j] persist for this exact path + actor
  [r] persist for the parent dir (recursive) + actor
  [N] deny
```

Persistent choices land in `.reyn/approvals.yaml` keyed by `<actor>/<op>/<path>` (e.g. `chat_router/file.write//tmp/output`). Keys are actor-scoped — one actor's approval doesn't leak to another (`security/permissions/permissions.py`: "Approval keys are actor-scoped to prevent external-actor privilege escalation"). `actor` identifies the calling subsystem (e.g. `chat_router` for the LLM-router-driven op path, or a background caller like `hooks`/`cron`), not an individual named agent.

When no intervention bus is wired for the call (`bus=None` — a non-interactive context), the JIT prompt is skipped and outside-zone access is denied outright rather than left pending.

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

A run with no intervention bus wired (CI, scripted automation, any context without an interactive TTY) proceeds without prompts. Approvals must be in place beforehand: either pre-approved in `reyn.yaml` or persisted to `.reyn/approvals.yaml` from a prior interactive run.

This is the same trust model: the automation doesn't get to decide what's safe; you do, in advance.

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

## Why actor-scoped keys

Approvals are keyed by actor, not globally. If actor A asks "can you write to `/tmp/foo`?", granting it doesn't grant actor B the same access.

The reason is composition safety: one actor's approved capability must not transitively unlock another actor's access — each actor has to ask for its own.

## `mcp_install` permission {#mcp_install-permission}

> Compat-shim form during the [Collapse arc](#collapse-arc-571). The canonical decomposition is `file.write: [.reyn/config/mcp.yaml]` + `http.get: [{host: registry.modelcontextprotocol.io}]` + `secret.write: [<env_key>]`; the bool form below is preserved through Phase 4.

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

**Axis 1 — Usage Declaration** (`reyn.yaml` `permissions:` block, parsed into a
`PermissionDecl`): the operator declares what an actor is allowed to reach
outside the defaults. An undeclared, out-of-zone op raises `PermissionError`
(analogous to Android `SecurityException` when calling an API not in the
manifest).

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
| 0 | `ask_user` | not required | unconditional pass | not possible |
| 1 | `web_search`, `web_fetch` | not required | allow | `deny` blocks |
| 2 | `mcp` | required | ask (4-layer) | `allow` pre-approves |
| 3 | `shell`, `file` (outside zone) | required | ✅ ask (JIT — `bus≠None` prompt at gate time; `bus=None` deny) | `allow` pre-approves; `deny` blocks even the default zone |

Tier 0 is "unconditional pass", not "default allow" — there is no config key
that could block these ops without breaking workflow execution semantics.

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

The permission system is part of the OS runtime, not a separate layer above it. Every side-effect performed by reyn — whether from workflow code, op handler, or any other OS-internal path — goes through the same permission resolver against the calling workflow's `PermissionDecl`. There is no inside/outside split: the OS uses the permission system as its core abstraction for all I/O.

Concretely, `op_runtime/mcp_install.py` writing `.reyn/config/mcp.yaml` routes through `reyn.api.safe.file.write` — the same gate a workflow-level safe-mode python step would use. The PermissionDecl in scope is the workflow's; the OS honors it uniformly regardless of where the call originates. The older "OS gates its callers, not itself" framing is dissolved by this: a single uniform mechanism, no cyclic concern.

## Declaration axis taxonomy

Each side-effect kind has a corresponding declarable axis. The axis vocabulary is small, and **bool axes are reserved for truly capability-shaped operations** — those not reducible to a single file / network / secret I/O scope.

### Axes

| Axis | Type | Granularity | Gate site | Notes |
|---|---|---|---|---|
| `file.read` | `list[{path, scope}]` | per-path | `require_file_read()` | scope ∈ {`just_path`, `recursive`}. Default zone = CWD. Outside zone: JIT ask (bus≠None) or deny (bus=None). `file.read: deny` blocks even CWD. Mirrors `http.get` pattern. |
| `file.write` | `list[{path, scope}]` | per-path | `require_file_write()` | covers write / edit / delete. Default zone = `.reyn/`. Outside zone: JIT ask (bus≠None) or deny (bus=None). `file.write: deny` blocks even `.reyn/`. Mirrors `http.get` pattern. |
| `http.get` | `list[{host}]` | per-host | `require_http_get()` | specific host = startup prompt + silent runtime; `"*"` wildcard = per-host runtime prompt. Covers both `reyn.api.safe.http.*` (workflow-internal, specific only) and `web_fetch` (LLM-driven, accepts wildcard) |
| `secret.write` | `list[<key>]` | per-key | `require_secret_write()` | per-key for `~/.reyn/secrets.env`; `"*"` wildcard for runtime-determined keys (= the per-value prompt is the actual gate) |
| `mcp` | `list[str]` | per-server | implicit at MCP call | per-server-name allowlist |
| `python` | `list[{module, function, mode, timeout}]` | per-step | `require_python_step()` | mode ∈ {`safe`, `unsafe`} |
| `tool` | `list[str]` | per-tool | `require_tool()` | named-tool allowlist |
| `shell` | `bool` | abstract | `require_shell()` | binary: any shell access at all |
| `allowed_mcp` | `list[str] \| None` | ACL filter | implicit at MCP call | per-agent restriction, cross-cuts `mcp` |

### A deliberately non-declarable gate: plugin git run-code trust

One gate is intentionally absent from the axis table above: `require_plugin_git_run_code_trust` (ADR 0064 §3.10, the `{kind: "git"}` branch of `plugin_install`). It has **no declarable axis, no config key, and no persisted approval** — by design. Installing a git-sourced plugin FETCHES remote code and then RUNS it (an MCP server / pipeline / skill registered to run in future sessions), an RCE trust boundary distinct from the *fetch* axis (`http.get`). If this decision were declarable or persistable, a single ALWAYS / `reyn.yaml` grant would become a standing silent-RCE authorisation for every future git plugin — and worse, an `http.get` approval (per-host, persistent, `web.fetch`-shared) could be mistaken for authority to run code from that host. So the run-code gate is a **per-install, never-persisted operator confirmation**: it consults/writes no approvals map, its choice set (`plugin_run_code_trust_choices`) offers only yes/no (structurally no ALWAYS), and it re-asks every install. Fail-closed: non-interactive callers deny. It is the one gate whose *non-declarability is the security property* — the taxonomy's declarable/persistable axes are exactly what it must not be.

### A deliberately ungated read: builtin + registered-plugin skill/pipeline bodies

`file.read`'s default zone above is CWD/`project_root`; a builtin skill/pipeline's shipped body (`reyn.builtin.registry`'s `BUILTIN_SKILLS`/`BUILTIN_PIPELINES` `path` entries) and an installed plugin's `skills/**`/`pipelines/**` content (`~/.reyn/plugins/<name>/`, ADR 0064 §3.3) both resolve OUTSIDE that zone in every deploy — the package ships outside any given project, and the plugin cache is a per-operator global directory, not project-scoped. The unmodified out-of-zone gate would hard-deny both non-interactively (there is no operator present to approve in a headless/CI run), so `reyn.builtin.docs.read_builtin_body_bytes` (#2913/#2914) and its mirror `reyn.plugins.body_read.read_plugin_body_bytes` short-circuit `require_file_read` for exactly this content, inside the `file` op handler (`reyn.core.op_runtime.file.handle`) — every other path, builtin-package or plugin-cache alike, still falls through to the unmodified `_in_default_read_zone` gate.

The plugin bypass's trust boundary is **install-registration**, deliberately NOT the presence of a `.reyn-plugin/` marker: a marker is trivially hand-plantable at `~/.reyn/plugins/<name>/.reyn-plugin/` with no install ever having run, so keying the bypass off marker presence would let anyone read (and, worse, have an agent load as instructions) unreviewed content. Instead the check is `reyn.core.op_runtime.plugin_install.is_registered_plugin_root` — true only once `plugin_install` has reached its completion step (source-resolve → manifest-validate → operator-permission-gated global-copy write → capability-register all succeeded) — the same "operator already approved this" boundary the builtin case gets for free from `importlib.resources` only ever resolving to content the wheel itself ships. Scope is least-privilege on both sides: only `skills/`/`pipelines/` content bypasses (a `.py` module inside the builtin package, or a plugin's `scripts/`/`requirements.txt`/`.mcp.json`, still hits the normal gate), and `~/.reyn/plugins/.staging/` (git-clone staging — content that predates even the `{kind: "git"}` run-code trust gate) is explicitly excluded. Enable/disable (`skills.yaml`/`pipelines.yaml`) never gates this bypass — it toggles USE of already-approved content, not a re-review of it.

### Why `shell` is the only bool

`shell` is process exec of an arbitrary command. The side-effect set is unbounded (= a shell command can read any file, write any file, network any host) and the author cannot enumerate which side effects a particular invocation will produce. There is no single I/O scope to reduce it to — process exec **is** the irreducible primitive.

Every other former bool axis (`mcp_install`, `mcp_drop_server`, `cron_register`, `index_drop`) has been re-expressed as one or more list axes, because each is actually reducible to a small set of file / network / secret operations:

| Former bool axis | Equivalent list-axis decomposition |
|---|---|
| `mcp_install: true` | `file.write: [.reyn/config/mcp.yaml]` + `http.get: [{host: registry.modelcontextprotocol.io}]` + `secret.write: [<env_key>]` |
| `mcp_drop_server: true` | `file.write: [.reyn/config/mcp.yaml]` |
| `cron_register: true` | `file.write: [.reyn/config/cron.yaml]` |
| `index_drop: true` | `file.write: [.reyn/index/sources.yaml]` + delete on `.reyn/index/<source>/index.db` |

The criterion is: **if a capability reduces to a finite I/O scope (file path / host / secret key), use a list axis; otherwise use bool**. Currently the only irreducible primitive is shell.

### What was lost in the collapse, and what wasn't

Bool axes carried a per-instance approval surface (= `mcp_install:<server_id>` keyed per server). After collapse:

- **MCP per-server granularity is preserved** at *call time* via the existing `permissions.mcp: [<server>]` axis. Installing a server (= writing `.reyn/config/mcp.yaml`) becomes a one-step grant; using a specific server still requires the call-time per-server check, so download + execute of the server's package still passes a per-server gate.
- **Cron per-job granularity is reduced** to "may write `.reyn/config/cron.yaml` at all", but cron-fired workflows still go through their own runtime permission gates when they execute. The granularity reduction does not bypass downstream protections.
- **Index per-source granularity is reduced** — there is no equivalent post-write gate. Drop is destructive and the per-source distinction was operator-UX, not security; the reduction is accepted.

### `allowed_mcp` is an ACL filter, not a capability

`allowed_mcp` doesn't grant capability — it **restricts** which subset of an already-granted `mcp` server list a specific agent may use. ACL filters cross-cut capability axes.

## Trust boundary layers

The execution surfaces that perform side-effects, ordered by enforcement strength:

```
┌──────────────────────────────────────────────────────────────────┐  ← STRONGEST
│  sandboxed_exec op (FP-0017)                                     │
│    OS-kernel enforcement (Seatbelt / Landlock / Seccomp)         │
│    argv-scoped, network-scoped, fs-scoped per-call               │
├──────────────────────────────────────────────────────────────────┤
│  python step (always safe; FP-0042)                             │
│    AST validation (= rejects `import os` at compile-time)        │
│    + reyn.api.safe.* honor-system path checks at function call   │
│    NOT kernel-sandboxed; subprocess runs with full user UID      │
├──────────────────────────────────────────────────────────────────┤
│  reyn package internal code (op handlers, registry client)       │
│    Uses the same `reyn.api.safe.*` primitives as skill code,     │
│    against the calling skill's PermissionDecl                    │
└──────────────────────────────────────────────────────────────────┘
```

- **Top (sandboxed_exec)** is the only layer with OS-kernel enforcement. argv / network / fs scope is declarative per call and enforced by the platform sandbox.
- **Internal OS code** uses the same `reyn.api.safe.*` primitives as workflow code, against the calling workflow's PermissionDecl. There is no inside/outside split — the OS exercises its own permission mechanism uniformly.
- **Python steps** are always safe-mode and honor-system: AST validation prevents `import os`, and `reyn.api.safe.*` checks declared paths / hosts / keys. A non-motivated author cannot accidentally bypass via normal coding patterns; a motivated author using metaprogramming still can, so the real boundary is the subprocess isolation + the permission gate on the `run_op` / `reyn.api.safe.*` surfaces. There is no unsandboxed mode: a `mode: unsafe` declaration is rejected at load. A step that genuinely needs raw host access splits that I/O into a `run_op`.

### Sandbox scoping model (sandboxed_exec)

The `sandboxed_exec` policy (`SandboxPolicy`) is scoped **per axis**. The axes are deliberately asymmetric — each set to the tightness that actually buys safety:

| Axis | Policy | Rationale |
|---|---|---|
| write | tight workspace-allowlist (`write_paths`) | The hard guard — bounds what a process can persist. |
| network | tight (off by default / allowlist) | The exfiltration gate — a process may read widely but cannot send anything out. |
| exec | controlled (`allow_subprocess`) | Bounds child-process spawning (enforced on Linux via seccomp, macOS via Seatbelt). |
| read | **broad-allow by default** + optional sensitive deny-list | The strict read-allowlist was abolished (#1199 realignment). |

**Why broad read is safe.** The network gate, not the read surface, is the exfiltration control. With network off by default a process may read widely but cannot send data out. A broad read surface also removes the system-path enumeration (`/usr`, `/lib`, dyld cache, …) that every binary needs just to load — enumeration that, when missing, broke the Landlock backend on Linux. This matches industry practice: Codex defaults to broad read + network-off on Linux; Claude Code treats read-restriction as secondary ("affects functionality") behind its write / network guards.

**Defense-in-depth deny-list.** `read_deny_paths` (default: OS-level credential stores — `~/.ssh`, `~/.aws`, `~/.gnupg`, …) carves sensitive locations out of the broad read surface.

**Residual risk (backend asymmetry).** The deny-list is enforceable only where the backend can express a deny-after-allow rule:

- **Seatbelt (macOS / SBPL)** — last-match-wins, so a broad `(allow file-read*)` followed by `(deny file-read* …)` enforces the deny-list.
- **Landlock (Linux)** — allowlist-only (path-beneath grants; you cannot carve a subpath out of an allowed parent), so the deny-list is **not enforceable**; broad read is a single read rule on `/`. On Linux a compromised in-sandbox process can therefore *read* the sensitive paths the deny-list names — but it stays bounded by the network gate (no exfiltration) and the write / exec guards. The deny-list is defense-in-depth, not the primary boundary; the primary boundary (write-allowlist + network-off) holds identically on both backends.

## Industry comparison

| Platform | Declaration shape | Runtime ask | Granularity | Enforcement |
|---|---|---|---|---|
| iOS (TCC + Entitlements) | `Info.plist` capability + purpose string | First-use prompt | Capability axis | OS kernel + signed entitlements |
| Android (≥ M) | `AndroidManifest.xml` `uses-permission` | First-use prompt for "dangerous" tier | Permission class + scoped storage | OS kernel + per-app UID |
| Web Permissions API | Per-feature query | Per-permission prompt | Origin-scoped (= per-domain capability) | Browser sandbox |
| Anthropic Claude Code | Tool list (Bash / Edit / Read / Write) | None at default; sandbox-mode optional | Tool name (no path scope) | Seatbelt (sandbox-mode) or trust |
| MCP servers | Server-side tool list exposed to client | Server owns its boundary | Per-tool, server-defined | Process boundary |
| **Reyn** | `permissions:` block (list-axis dominant; one bool: `shell`) | startup_guard + interactive on first use | per-path / per-host / per-server (resource scope) | AST + `reyn.api.safe.*` honor-system for safe-mode; kernel for `sandboxed_exec` |

Reyn deviates from the iOS / Android "capability + first-use prompt" pattern on two axes:

1. **Granularity is finer than industry default** — list-axis path / host / server scope is closer to Web's origin-scope than to iOS / Android's capability axis. The justification is that Reyn workflows are purpose-specific code (= author knows the inventory), whereas iOS / Android apps are general-purpose.
2. **Enforcement is honor-system for safe-mode python** — iOS / Android rely on kernel boundaries; Reyn relies on AST validation + path / host / key checks via the `reyn.api.safe.*` primitives. The trade-off is implementation simplicity (= no per-step seatbelt setup) for weaker enforcement.

## Collapse arc (#571)

The axis taxonomy above is the target state. The permissions audit identified that the prior design carried four bool axes (`mcp_install`, `mcp_drop_server`, `cron_register`, `index_drop`) which were redundant with `file.write` — the side effects all reduced to a canonical `.reyn/*.yaml` write reachable through `reyn.api.safe.file.write`, so the bool axes were duplicating coverage rather than gating new capability. The collapse arc removes them in stages:

| Phase | Scope | Status |
|---|---|---|
| 1 | This doc — articulate "permission is an OS I/O primitive" and the collapse map | this PR |
| 2 | Route `op_runtime` handlers (= `mcp_install` / `mcp_drop_server` / `cron_register` / `index_drop`) through `reyn.api.safe.file.write`; loader compat shim accepts both bool form and explicit list form | follow-up PR |
| 3 | Introduce `http.get: [{host}]` axis (= gates `reyn.api.safe.http.*` per-host) and `secret.write: [<key>]` axis (= gates `~/.reyn/secrets.env` writes per-key) | follow-up |
| 4 | Migrate stdlib workflows to explicit list-axis form | follow-up |
| 5 | Remove bool axes (`mcp_install` etc.) and `require_mcp_install` / `require_cron_register` / `require_index_drop` / `require_mcp_drop_server` from the OS surface | follow-up |

During Phases 1–4 the bool form (= `mcp_install: true`) is accepted as a compat shim that implicitly expands to the equivalent list-axis decomposition. The bool form is removed in Phase 5.

### Phase 7 — prompt-timing model unification + `safe.http`/`web_fetch` collapse

Phase 7 finishes the alignment by giving the `http.get` axis the same prompt model as `file.write`:

- **Specific declared host** (`http.get: [{host: "api.github.com"}]`) — `startup_guard` prompts the operator once per `<skill, host>` and persists the decision to approvals.yaml under `<skill>/http.get/<host>`. Runtime is then silent. Mirrors `file.write` for paths outside the default zone.
- **Wildcard** (`http.get: [{host: "*"}]` or `["*"]`) — host set is unknown at write-time (= LLM picks at runtime, e.g. `web_fetch` follow-up of `web_search` results), so the prompt fires at the actual host gate inside `require_http_get`. Same `<skill>/http.get/<host>` persistence; ALWAYS / NEVER choices apply per host.
- **No declaration** — legacy `web.fetch` compat path with a `DeprecationWarning` until the segmented-migration window closes; existing workflows that relied on Tier-1 default-allow keep working.

The `web_fetch` op handler routes through `require_http_get` instead of the legacy `require_web_fetch`; the chat router's PermissionDecl declares `http.get: [{host: "*"}]` so LLM-driven fetches go through the wildcard branch. The `reyn.api.safe.http` subprocess path strips wildcard entries at the preprocessor — sync subprocesses can't prompt, so wildcard-host fetches must go through the async `web_fetch` op route.

This unifies the two HTTP surfaces (`safe.http` workflow-internal + `web_fetch` LLM-driven) under one axis with one prompt model. It matches the browser-extension `host_permissions` (= declared, install-time prompt) + Web Permissions API (= runtime per-feature prompt) hybrid — see the [Industry comparison](#industry-comparison) section.

| Aspect | Pre-Phase-7 | Post-Phase-7 |
|---|---|---|
| `safe.http` workflow-internal | per-host decl, silent runtime, no prompt | unchanged for specific decl; wildcard rejected (= subprocess can't prompt) |
| `web_fetch` LLM-driven | Tier-1 default-allow, 4-layer per-URL prompt | routed through `http.get` axis; chat router decl carries wildcard so behaviour is preserved |
| Operator prompt granularity | per-URL (`web.fetch` key) | per-host (`<skill>/http.get/<host>` key) — ALWAYS covers all URLs on that host |
| Workflow author control over LLM fetch scope | none | declare specific `http.get` hosts to constrain (= LLM can only fetch declared hosts; wildcard absent = no fallback) |
| Legacy `web.fetch: allow` / `deny` config | direct gate | honored as backward-compat alias inside `require_http_get` during the migration window |

## `python` permission and `mode: safe` allowlist

Python steps are always sandboxed. The `python` permission has one level:

| Level | Config key | What it allows |
|-------|-----------|----------------|
| `safe` | `python.safe: allow` | Steps that import only from `PURE_STDLIB_ALLOWLIST` — clock, entropy, pure compute, and `__future__` (compiler directive). No filesystem, network, or process access. |

`PURE_STDLIB_ALLOWLIST` is defined in `src/reyn/core/kernel/_python_allowlist.py`. `__future__` is in the list as a compiler directive — it carries no runtime capability.

There is no unsandboxed level: a step declaring `mode: unsafe` is rejected at load with an actionable error. A step that needs raw host access (filesystem, network, process spawning) splits that I/O out into a `run_op` step — which carries its own permission gate and event-log entry.

**Non-interactive auto-allow**: in a non-interactive context (no intervention bus wired), safe-mode python steps are auto-allowed without a prompt. This mirrors the same non-interactive behavior already in place for other ops in CI runs.

**The formal contract for `mode: safe`** (= "ambient sources only") covers the full allowlist rationale and the refactor pattern for splitting raw I/O out into a `run_op` step.

## Credential scoping (removed trigger point)

FP-0016 Component D introduced per-invocation credential scoping: a sub-skill,
spawned via the now-removed `run_skill` op, would receive a `ScopedSecretStore`
scoped to its declared `required_credentials`, intersected with the parent's
own scope (a Confused Deputy mitigation). That trigger point is gone along
with `run_skill` (#2104), and no other call site constructs a
`ScopedSecretStore` today — `security/secrets/store.py`'s `ScopedSecretStore`
and `CredentialScopeError` classes still exist, but `OpContext.secret_store`
is unconditionally `None` in the current runtime. There is currently no
credential-scoping enforcement in effect; secret access is gated only by the
[`secret.write` declaration axis](#declaration-axis-taxonomy) and OS-level file
permissions on `~/.reyn/secrets.env`.

## Effective permission: conjunctive restrict model {#effective-permission-conjunctive-restrict-model}

The authorization layers above answer: *"has this capability been granted?"* A separate orthogonal question is: *"given all active restrictions, is this capability allowed right now?"* The conjunctive restrict model handles the second.

At gate time, a capability is permitted only if **every** active layer allows it:

```
effective = AgentLayer ∩ SandboxLayer ∩ ProfileLayer ∩ ContextualLayer
allows(axis, value) = all(layer.allows(axis, value) for layer in layers)
```

### The restrict layers

| Layer | What it models | Role |
|---|---|---|
| **AgentLayer** | Skill declaration + default zone baseline + runtime approvals | Grant layer |
| **SandboxLayer** | Runtime sandbox caps (paths, network, subprocess, env) | Restrict-only |
| **ProfileLayer** | Per-agent capability narrowing — the agent's default capability spec | Restrict-only |
| **ContextualLayer** | Per-session capability narrowing — delegation / topology / untrusted-auto | Restrict-only |

`SandboxLayer`, `ProfileLayer`, and `ContextualLayer` are **restrict-only**: they can narrow a permission, but cannot re-grant something the `AgentLayer` denied. This is a structural property of the conjunction (`all(...)`) — no layer's `False` can be overridden by any other layer.

### One spec, two binding adapters (#2074)

The two narrowing layers are **two bindings of one primitive**: both read a
`CapabilityProfile` (the single capability-narrowing spec, covering the
`mcp` / `tool` axes + catalog-`category` visibility), separating the
*spec* (what is narrowed) from the *binding* (when/how it is applied):

- **`ProfileLayer` — per-agent default binding.** Reads the agent's
  `AgentProfile.default_profile()` (a `CapabilityProfile`). The operator surface
  stays the natural `allowed_mcp` key in `.reyn/agents/<name>/profile.yaml`;
  this maps onto the spec's `mcp_allow` axis internally.
- **`ContextualLayer` — per-session dynamic binding.** Reads a `CapabilityProfile`
  resolved per-session from a delegation / topology role / untrusted-content
  auto-profile (`.reyn/capability_profiles/<name>.yaml`), composable
  (most-restrictive-wins) and subtractive-only.

Both feed the **unchanged** conjunctive ∩ above. A `None` spec, or a `None`
axis allow-list, is unrestricted (⊤) — so an agent/session with no narrowing is
byte-identical to a build without the capability spec.

### How the two "layer" concepts relate

Two distinct concepts both use the word "layers" in this document. They answer different questions:

| Concept | Question | Direction |
|---|---|---|
| Authorization 3 layers (grant hierarchy, top of page) | How does a capability get granted? | Hierarchical grant |
| Conjunctive restrict layers (this section) | Given current runtime restrictions, is the capability allowed? | Intersect — can only narrow |

They operate in sequence: authorization resolution (AgentLayer) determines whether the workflow's declaration and approvals cover a capability; then the conjunctive intersection applies any active sandbox or profile restrictions. An approved capability can still be denied by `SandboxLayer` or `ProfileLayer` — grant-back is forbidden.

## LLM spawn capability model {#llm-spawn-capability-model}

When an LLM uses `agent_spawn` or `topology_create` to build an org at runtime,
the resulting agents and topology members operate under a **⊆-parent capability
model**: every spawned agent's effective capability is capped at a subset of its
spawner's, recursively, with no path to escalate via spawn.

### How the cap is enforced

The OS, not the LLM, sets the spawn lineage. When `agent_spawn` creates a new
agent, the registry records `parent=<spawner>` from the calling context — the
LLM never supplies this link (forge-guard). At gate time, the spawned agent's
`ContextualLayer` composes the spawner's **live resolved effective capability**
as a restrict-only conjunct:

```
child_effective ⊆ parent_effective   (structural, by construction)
```

Because `ContextualLayer` is restrict-only (it feeds the `all(...)` conjunction
— see [conjunctive restrict model](#effective-permission-conjunctive-restrict-model)),
the child cannot exceed the parent on any axis. This holds recursively: a
grandchild is capped at ⊆ the child, which is itself ⊆ the parent.

The default-deny `_delegate` floor also applies to spawned agents: an unbound spawned
agent receives the least-privilege `_delegate` profile unless a `topology_create`
binding explicitly re-grants within the ⊆-parent envelope.

### No-escalation-via-spawn: the closed class

Four specific escalation avenues are closed by construction:

| Escalation avenue | Closed by |
|---|---|
| Live spawn (new agent exceeds spawner) | `ContextualLayer` parent-conjunct at gate time |
| Rewind drop (lineage lost, constraint lifted) | Lineage is WAL-tracked; rewind reconstruction restores the parent link |
| Absent parent (parent purged, constraint lifted) | Absent-parent path fails closed — gate treats missing lineage as deny |
| Name reuse (new agent reuses purged name, fresh identity) | Identity-keyed lineage: the OS key is not the name but an internal ID; a re-used name cannot inherit the prior agent's purged lineage |

### `topology_create` profiles stay inside the envelope

When `topology_create` assigns a `capability_profile` to a member, the profile
is a further **narrowing within the ⊆-parent envelope** — it can only restrict,
never re-grant. Because every member of a topology must already be in the
creator's spawn subtree (subtree-restriction gate), the profile binding is safe
by construction: it can at most reach the envelope the lineage conjunct already
established.

### Operator bounds on spawn tree size

The ⊆-parent model governs *what* a spawned agent can do. Separately,
`safety.spawn.max_depth` and `safety.spawn.max_children` govern *how many*
agents an LLM may spawn — DoS guards so an agent cannot mint an unbounded org.
See [reyn-yaml § safety.spawn](../../reference/config/reyn-yaml.md#safetyspawn-fields).

---

## What the permission system is NOT

- **Not a Linux capability sandbox.** A Python step's subprocess runs as the same user; the AST allowlist is honor-system, and reyn doesn't sandbox the kernel (that layer is `sandboxed_exec`).
- **Not a secret keeper.** Don't put credentials in approvals.yaml or rely on permissions to hide environment variables. Use [Concepts: secret handling](../runtime/secret-handling.md) for credentials.
- **Not protection against the user.** If you `permissions: shell: allow` in reyn.yaml, you've authorized shell. The system is protecting against accidental capability creep, not user intent.

## See also

- [Reference: permissions](../../reference/config/permissions.md) — full schema
- [Reference: reyn.yaml](../../reference/config/reyn-yaml.md) — `permissions:` key and `permissions.mcp_install`
- [Reference: state-dir](../../reference/config/state-dir.md) — `.reyn/approvals.yaml`
- [Concepts: secret handling](../runtime/secret-handling.md) — credential storage (`~/.reyn/secrets.env`)
- [Reference: `reyn mcp`](../../reference/cli/mcp.md) — `install` subcommand and `mcp_install` gate interaction
- [How-to: manage permissions](../../guide/for-users/manage-permissions.md)
- [Concepts: Capability profile](../runtime/capability-profile.md) — per-agent ProfileLayer spec (workflow / MCP / tool / category axes) and agent self-edit guide
- [Concepts: LLM org-design tools](../multi-agent/org-design.md) — `agent_spawn` / `session_spawn` / `topology_create` and the ⊆-parent model in practice
