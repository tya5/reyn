# FP-0013: Agent Authentication — OAuth Delegation, Token Lifecycle, and MCP Auth Headers

**Status**: proposed
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

Reyn currently supports only static API keys via `secrets.store`. This blocks connection to
the growing HTTP-mode MCP ecosystem, causes 401 errors during long-running tasks when OAuth
tokens expire, and provides no audit trail for per-agent credential usage. This proposal
introduces five focused components — MCP Bearer headers, OAuth token refresh, Device
Authorization Grant login, per-skill credential scoping, and agent identity propagation —
so that Reyn treats authentication as a first-class OS concern rather than a user-configured
environment variable.

---

## Motivation

Practitioner voice surveys (Reddit, HN, Zenn, Qiita) surface authentication as a recurring
gap across communities, but the failure modes differ by engineering context.

### Scenario 1 — MCP HTTP Bearer headers (immediate blocker)

HTTP-transport MCP servers (GitHub MCP, Atlassian MCP, Slack MCP, internal enterprise MCP
servers) require `Authorization: Bearer <token>` on each request. Reyn's `mcp.servers.<name>`
config has no `headers:` field. Static API keys in `secrets.store` are decoupled from the
MCP connection layer — there is no mechanism to inject them at connection time.

Effect: every HTTP-mode MCP server is currently unreachable unless the operator manually
patches the HTTP client. The `mcp_install` permission gate (ADR-0029) is fully functional,
but the connection itself fails at the Bearer header step.

### Scenario 2 — Token auto-refresh during long tasks

OAuth access tokens expire in approximately one hour (RFC 6749 §4.1). Long-running skills
(especially FP-0012 async execution, which may run for 30–90 minutes) will encounter 401
errors mid-task. `secrets.store` stores static values only; there is no refresh token
lifecycle. The error surfaces as an opaque tool failure deep inside a running skill, with no
recovery path.

### Scenario 3 — OAuth Device Authorization Grant (RFC 8628)

Many enterprise GitHub/GitLab/Azure DevOps environments forbid Personal Access Tokens by
policy, requiring OAuth flows. The standard browser-redirect flow (Authorization Code Grant)
does not work for headless or autonomous agents — there is no browser to redirect. Device
Authorization Grant (RFC 8628) solves this: the agent prints a URL and user code, the
operator approves on any device, and the agent polls for the token. Reyn has no `reyn auth`
CLI entry point and no first-class flow for this grant type.

### Scenario 4 — Scoped credential delegation to sub-skills

When a parent skill spawns sub-skills via `run_skill`, the sub-skill currently inherits the
full `secrets.store`. A prompt injection attack in a processed document could instruct a
sub-skill to exfiltrate all stored credentials (Confused Deputy problem). The attack surface
grows with each credential added to `secrets.store`. This is a structural vulnerability in
the current delegation model, not a configuration issue.

### Scenario 5 — Enterprise Agent Identity (Entra Agent ID pattern)

Japanese enterprise deployments require per-agent identity for RBAC and audit trails.
SOC2/ISO27001 compliance mandates "who (which agent) accessed what." Current P6 events do
not carry an `agent_id` field. API calls made by sub-skills are indistinguishable from calls
made by the parent session. METI AI Governance v1.1 requires auditability of automated
system actions at the actor level.

### Reyn's differentiator

Most agent frameworks treat authentication as "user's problem — configure your env vars."
Reyn's permission model and P6 audit trail make agent authentication a first-class OS
concern: credentials are scoped to the skills that need them, their lifecycle is managed
by the runtime, and every use is recorded in the append-only event log. This directly
addresses enterprise compliance requirements that are currently impossible to satisfy with
ad-hoc environment variable patterns.

---

## Proposed implementation

### Component A — `mcp.servers.<name>.headers` config field (SMALL)

Add an optional `headers: dict[str, str]` field to `MCPServerConfig` in `src/reyn/config.py`.
The MCP HTTP client in `src/reyn/mcp/client.py` reads this dict and passes it at connection
time. Header values may reference `secrets.store` keys via `${secret:my_token}` interpolation
(same pattern as existing env var injection).

```yaml
# reyn.yaml
mcp:
  servers:
    github:
      transport: http
      url: https://api.githubcopilot.com/mcp/
      headers:
        Authorization: "Bearer ${secret:github_token}"
```

No OS-level policy change is needed — the permission system (ADR-0029) already gates MCP
server connections. This component makes the already-gated connection actually work.

Target files:
- `src/reyn/config.py` — `MCPServerConfig.headers: dict[str, str] = {}`
- `src/reyn/mcp/client.py` — pass `headers` dict at HTTP session creation

### Component B — OAuth token type in `secrets.store` + refresh lifecycle (MEDIUM)

Add an `OAuthToken` credential type to `secrets.store`:

```python
@dataclass
class OAuthToken:
    access_token: str
    refresh_token: str
    token_uri: str
    client_id: str
    client_secret: str   # or PKCE — no client secret needed
    expires_at: datetime
    scopes: list[str]
```

The store exposes a `get_valid_token(key: str) -> str` method that:
1. Returns `access_token` if `expires_at` is more than 60 seconds away
2. Otherwise calls the token endpoint with `grant_type=refresh_token`
3. Updates the stored token and emits a `token_refreshed` event (P6)
4. Returns the new `access_token`

Component A's `${secret:key}` interpolation calls `get_valid_token` when the secret type
is `OAuthToken`, making refresh transparent to MCP connections.

Target files:
- `src/reyn/secrets/store.py` — `OAuthToken` dataclass + `get_valid_token` method
- `src/reyn/events/events.py` — `token_refreshed` event payload

### Component C — `reyn auth login <service>` CLI (Device Authorization Grant) (MEDIUM)

New `reyn auth` command group implementing RFC 8628 Device Authorization Grant:

```
reyn auth login github        → start Device Grant flow for GitHub
reyn auth login <custom_url>  → generic OIDC endpoint
reyn auth list                → show stored OAuth tokens (name, scopes, expiry)
reyn auth revoke <service>    → delete token from secrets.store
```

Flow:
1. POST to device authorization endpoint → receive `device_code`, `user_code`, `verification_uri`
2. Print `verification_uri` and `user_code` to terminal
3. Poll token endpoint with exponential backoff (respecting `interval` from step 1)
4. On success: store `OAuthToken` in `secrets.store` under `<service>_token`

This makes `reyn auth login github` the standard onboarding path for GitHub MCP, replacing
the current manual PAT copy-paste workflow.

Target files:
- `src/reyn/cli/auth.py` — new command group
- `src/reyn/cli/main.py` — register `auth` group

### Component D — Per-skill credential scoping at `run_skill` spawn (LARGE)

Skills declare their required credentials in `skill.md` frontmatter:

```yaml
required_credentials:
  - github_token
  - atlassian_token
```

When the OS spawns a sub-skill via `run_skill` Control IR op, it builds a
`ScopedSecretStore` containing only the declared credentials and passes it to the
sub-skill runtime context. The sub-skill cannot access credentials outside this scoped
store, regardless of what the parent session holds.

This aligns with P4 (OS provides only the candidates the LLM/skill needs) and P5
(workspace is the single source of truth — the scoped store IS the sub-skill's credential
workspace).

The parent skill may declare `required_credentials: [*]` to explicitly opt in to full
delegation (only for trusted internal skills; this is auditable via P6 events).

Target files:
- `src/reyn/op_runtime/run_skill.py` — build `ScopedSecretStore` at spawn
- `src/reyn/secrets/store.py` — `ScopedSecretStore` wrapper
- `src/stdlib/skills/*/skill.md` — add `required_credentials` to stdlib skills

### Component E — `agent_id` identity propagation in P6 events (SMALL)

Add `agent_id: str` to `reyn.yaml` (optional; defaults to `reyn/<hostname>`). This value
is included in every P6 event payload and injected as a client header
(`X-Reyn-Agent-Id: <agent_id>`) on outbound HTTP calls (MCP, A2A, external APIs).

```yaml
# reyn.yaml
agent:
  id: "reyn/acme-corp/code-review-agent"
```

This makes every action auditable at the agent level — satisfying SOC2/ISO27001 and METI
v1.1 audit requirements.

Target files:
- `src/reyn/config.py` — `AgentConfig.id` field
- `src/reyn/events/events.py` — `agent_id` in base event payload
- `src/reyn/mcp/client.py` — inject `X-Reyn-Agent-Id` header

---

## Priority ordering

**A → B → C → E → D**

Component A is the immediate unlocker: it makes the entire HTTP-mode MCP ecosystem
accessible. Component B enables long-running tasks (FP-0012) to survive token expiry.
Component C provides the enterprise-friendly onboarding flow that Component B depends on
for initial token acquisition. Component E is small and high-value for compliance. Component
D (credential scoping) is the largest and most disruptive but also the most important for
security — it should land after A–C are stable.

---

## Dependencies

- **FP-0012** (async skill execution): Component B (token refresh) is most critical for
  long-running async tasks that span multiple token expiry windows
- **ADR-0029** (mcp_install permission gate): Component A uses the same permission
  enforcement pattern; the gate is already in place
- RFC 8628 (Device Authorization Grant) — Component C; no Reyn dependencies
- RFC 6749 §6 (token refresh) — Component B; no Reyn dependencies

---

## Cost estimate

**Total: LARGE**

| Component | Cost | Notes |
|---|---|---|
| A: `mcp.servers.headers` config field | SMALL | Config struct + HTTP client; ~50 lines |
| B: `OAuthToken` + refresh lifecycle | MEDIUM | New credential type + P6 event |
| C: `reyn auth login` CLI (Device Grant) | MEDIUM | New CLI command group; RFC 8628 polling |
| D: Per-skill credential scoping | LARGE | Scoped store + all stdlib `skill.md` updates |
| E: `agent_id` in events + headers | SMALL | Config field + base event payload |
| Tests | MEDIUM | Tier 1: token refresh contract; Tier 2: scoped store isolation |

---

## Related

- `src/reyn/config.py` — `MCPServerConfig`, `AgentConfig`
- `src/reyn/mcp/client.py` — HTTP session creation
- `src/reyn/secrets/store.py` — credential store
- `src/reyn/cli/auth.py` — new file (Component C)
- `src/reyn/op_runtime/run_skill.py` — credential scoping at spawn (Component D)
- `src/reyn/events/events.py` — `token_refreshed` event, `agent_id` base payload
- ADR-0029 — MCP install permission gate
- FP-0012 (`0012-async-skill-execution.md`) — async execution; Component B is a dependency
- RFC 8628 — Device Authorization Grant
- RFC 6749 §6 — OAuth 2.0 token refresh
