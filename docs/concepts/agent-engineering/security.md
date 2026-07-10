---
type: concept
topic: architecture
audience: [human, agent]
---

# Security

Capability gating, sandbox boundaries, and trust scoping. The bar is "no agent silently gets capabilities the operator didn't authorize, and a compromised call can't escalate to other actors."

## How reyn handles it

### Three-layer permission model

```
defaults (always on)
   ↓ if an actor needs more
declared capability → JIT prompt at point of use (not at startup)
   ↓ if you trust the project broadly
project-wide pre-approval (reyn.yaml)
```

Defaults are conservative — read/glob/grep anywhere under the project root, write/edit/delete only under `.reyn/` (with a narrow carve-out even there for `.reyn/approvals.yaml` and `.reyn/index/sources.yaml`, since those paths have no downstream use-time gate to catch a direct write). No shell, no MCP, no Python beyond that. Anything more requires a declared capability, which prompts just-in-time at the point of actual use — not a single startup-time blanket prompt.

**This 3-layer split and the charter's "4-layer JIT approval" are two different axes, not a contradiction.** These three layers are the *grant hierarchy* — how broad an actor's authorization is (defaults / declared / project-wide). Charter's 4-layer description is the *approval-source resolution order* the JIT prompt itself checks before it actually has to ask: config pre-approval → saved approvals (`.reyn/approvals.yaml`) → session approvals (in-memory, current invocation) → interactive prompt (the last resort). The 4-layer resolution lives entirely inside this section's middle layer ("declared capability → JIT prompt").

### Actor-scoped approvals

Persistent approval choices land in `.reyn/approvals.yaml`, keyed by `<actor>/<op>/<path>`. Keys are actor-scoped, not skill- or user-scoped: one actor's approval doesn't leak to another. This is the composition-safety property — an approval granted to the chat router's own dispatch path doesn't transitively extend to, say, a background hook or cron caller acting through a different actor identity.

### `sandboxed_exec` — a typed, per-axis `SandboxPolicy`

Subprocess execution is gated by a `SandboxPolicy` with deliberately asymmetric axes, each set to the tightness that actually buys safety: `write_paths` is a tight allowlist (the hard guard on what a process can persist), `network` is off by default (the exfiltration gate), `allow_subprocess` bounds child-process spawning, and `read` is broad-allow by default plus an optional sensitive-path deny-list (`read_deny_paths`) — the strict read-allowlist model was abolished, since the network gate, not the read surface, is what actually stops exfiltration. Enforcement is backend-selected per platform (Seatbelt on macOS, Landlock + seccomp-BPF on Linux, a `NoopBackend` audit-only fallback when neither is available).

### Non-interactive approval (run-once, CI)

`reyn run-once` does not prompt. Permissions must be in place before the run — either pre-approved in `reyn.yaml` (`permissions.<key>: allow`) or persisted from a prior interactive run. The trust model doesn't change between modes; a non-interactive run just inherits the decisions you've already made.

### Content-layer defense

Untrusted content is scanned and fenced at the OS seams where it enters the LLM prompt. Two primitives:

- **Pattern scan** (`security/threat_patterns.py`) — regex-based detection of injection / exfiltration / role-hijack / exec-scope threats. Matches emit threat events; blocked patterns abort the operation.
- **Structural fence** (`security/content_fence.py`) — explicit delimiters wrap untrusted content so the model sees it as data, not instructions.

These primitives apply at the OS seams below — each seam uses the mechanism that fits its trust direction (read seams scan and/or fence; write seams block):
- **Tool results** — scanned (all results) and structurally fenced (external-content results only) via `security/content_guard.py` before reaching the prompt
- **Memory writes** — writes matching threat patterns are blocked at the router level
- **Context files** (REYN.md/AGENTS.md) — fenced on load
- **A2A inbound messages** — fenced + scanned on arrival
- **Pre-exec commands** — `sandboxed_exec` scans the full joined argv for exec-scope threats before the subprocess is launched
- **Compaction input** — secret-looking content is stripped before summaries persist (`security/secret_redaction.py`)

#### What gets structurally fenced

Scanning is broad (it runs on all content at read seams for detection telemetry), but the **structural fence** is applied selectively — only content from an *untrusted source* is wrapped, and only when fencing is enabled. Two gates decide:

1. **Config gate** — `safety.threat_scan.enabled` *and* `safety.threat_scan.fence_enabled` must both be on (both default `true`). Either off → content passes through unfenced.
2. **Source-trust gate** — applied per seam. Trusted-internal content (the OS's own framing, operator-typed input) is never fenced; only untrusted-source content is.

With both gates open, these are the content targets fenced today:

| Fenced target | What it is | Source-trust rule |
|---|---|---|
| **External-content tool results** | Results from tools that return outside content — web fetch / web search, MCP calls and server-authored tool descriptions, **recalled memory / RAG results**, and **memory-entry reads** | Only tools flagged as returning external content are fenced; every other (trusted-internal) tool result is **scan-only**, not fenced |
| **Project context file** | `REYN.md` / `AGENTS.md` / `project_context_path` text threaded into the system prompt | Always fenced — an operator-editable file is treated as data |
| **A2A inbound peer messages** | Message text from a remote peer agent, before it enters history | Always fenced — a remote peer is outside the trust boundary |
| **External intervention answers** | An answer delivered from an external peer (A2A POST / webhook) | Only the history-bound (context) copy is fenced; the buffered / choice-matched answer and the audit record stay raw |
| **Task query results** | The free-text `description` / `name` / `result` fields of a task returned by the task read / list ops | Always fenced; the structural fields (id / status / dependencies / dates) are OS-generated and left unfenced |
| **Delegated-task wake descriptions** | A delegated task's description carried inside the wake message that tells an assignee to execute it | Always fenced as data; the OS "you are the assignee — execute this" framing is the trusted instruction |

Memory is therefore covered on **both** directions: a memory **read** (recall or a memory-tool result) is fenced as external content on the way in, while a memory **write** is pattern-**blocked** at the write seam — a different mechanism (see the seam list above). Content that is deliberately *not* fenced: trusted-internal tool results, direct operator input (`ask_user`, chat messages — trusted by definition), pre-exec command argv (scanned, not fenced), and compaction input (secret-redacted, not fenced).

## Where it's still thin

**Content-layer defense is seam-based regex detection, not a prompt-injection guarantee.** Pattern scans catch known attack shapes at OS seams; novel or obfuscated payloads that don't match a regex pass through. Once untrusted content is fenced and in the prompt, the LLM may still follow embedded instructions that read as natural language rather than a recognisable attack pattern. The OS does not gate the LLM's *response* for injection residue — capability damage is bounded by the permission system (no writes outside approved paths, no `sandboxed_exec` outside its declared `SandboxPolicy`) but response-level interception is not implemented.

**The Landlock backend's read-deny-list is not enforceable.** On macOS, Seatbelt's last-match-wins semantics let a broad read-allow be narrowed by a deny-list for sensitive paths (`~/.ssh`, `~/.aws`, …). Landlock (Linux) is allowlist-only — you cannot carve a sensitive subpath back out of a broader allowed parent — so on Linux a compromised in-sandbox process can read those paths. The primary boundary (write-allowlist + network-off) holds identically on both backends; the deny-list is defense-in-depth on Linux, not the primary guarantee there.

## See also

- [`docs/concepts/architecture/charter.md`](../architecture/charter.md) — the Security row, grounded across all 7 feature families
- [../runtime/permission-model.md](../runtime/permission-model.md) — the full permission model, including the JIT prompt UX and the audit trail
- [../runtime/sandbox.md](../runtime/sandbox.md) — the full `SandboxPolicy` field reference and backend selection table
- [Reference: permissions](../../reference/config/permissions.md) — full schema
- [How-to: manage permissions](../../guide/for-users/manage-permissions.md)
- [reliability-engineering.md](reliability-engineering.md) — what happens when an op is denied
- [Feature map — Content-layer defense](../../feature-map.md#content-layer-defense) — full mechanism inventory
