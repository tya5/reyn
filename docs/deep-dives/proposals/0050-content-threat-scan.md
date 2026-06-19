# FP-0050 — Content-layer threat scan (prompt-injection / pre-exec command)

**Issue:** #1822 (umbrella) — aggregates #1820 (tool-output strip) + #1821 (memory injection scan).
**Author:** e2e-coder. **Status:** DRAFT — flow-trace + design for lead #311-rigor review **before** impl.
**Scope of THIS FP:** #1822 **Part 1 (prompt-injection scan, highest priority)** + the integration seam for Part 2 (pre-exec command scan). Part 3 (`reyn audit` static audit) is a separate later FP.

---

## 1. Problem (grounded in the #1822 cross-system comparison)

Reyn's **execution layer** (Docker + OS-native syscall: Landlock/Seatbelt/Seccomp; permission model: skill/path-scoped approval; secret scoping; IV persistence) is at-or-above the Hermes/OpenClaw comparison. The single clear gap is the **content layer**: nothing inspects untrusted content for prompt-injection before it is baked into the system prompt / context, and nothing inspects command **strings** before exec.

The execution layer restricts *what the agent can do*; it does not stop *poison entering the LLM context* or *a dangerous command string being run within the sandbox*. These are orthogonal concerns — the content scan **complements**, and must not **duplicate**, the existing layers (see §4).

## 2. Untrusted-content entry points (flow-trace — file:line, verified on main)

Two seam classes (the Hermes design insight: **block where the agent can intervene, detect-only where it cannot**):

### Detect seams (read/render — content already exists; flag/quarantine, cannot un-fetch)
| # | Path | Seam (file:line) | Source |
|---|---|---|---|
| EP1 | memory → SP | `router_system_prompt._render_memory` (router_system_prompt.py:451), via `build_system_prompt(memory_index=)` | `host.get_memory_index()` (router_loop.py:1819; router_history_buffer.py:480) |
| EP2 | tool result → compaction | `_turn_to_compactor_input` (compaction_controller.py:45, called :264) | candidate ChatMessages (tool turns) |
| EP3 | context file (REYN.md) → SP | `build_system_prompt` §6 "Project context" (router_system_prompt.py:227) | `self._project_context` (session.py:1100, injected at construction) |
| EP4 | command string → exec | `op_runtime/sandboxed_exec.handle` (sandboxed_exec.py:21) + bash exec | LLM-emitted op.command |

### Block seams (writes the agent controls — block on detection to prevent persistent poisoning)
| # | Path | Seam (file:line) |
|---|---|---|
| BP1 | memory write | `runtime/services/memory_service.py` write path (`.reyn/memory/<slug>.md`, layer shared/agent — memory_service.py:79) |
| BP2 | skill / MCP install | `op_runtime/mcp_install.py` (+ any skill-install path) |

EP3's REYN.md is loaded *upstream* of Session (passed to the constructor at session.py:1100), so the OS-level scan seam is the §6 render, not the file read.

## 3. Proposed design

### 3.1 Pattern library — `src/reyn/security/threat_patterns.py` (new)
A pure `(regex, pattern_id, scope)` library + a `scan(text, scope) -> list[ThreatMatch]` function. No I/O, no skill knowledge.

**P7 note:** the patterns are security-domain regexes (injection / exfiltration / role-hijack phrases), **not** skill-specific phase/artifact/field strings — so this lives correctly in `security/` (a domain module), and the OS-core decision logic stays free of skill strings. The scan is applied at content boundaries the same way secret interpolation already is — a security transform at a seam, not OS decision vocabulary.

### 3.2 Scope → enforcement mapping
- `all` — classic injection / exfiltration; scanned everywhere.
- `context` — promptware / C2 / role-hijack; scanned at EP1–EP3 (broad detection).
- `strict` — scanned at BP1/BP2 (memory write / skill install) → **block**.
- `exec` — homograph / pipe-to-interpreter / terminal-escape; scanned at EP4 (Part 2).

**Policy per seam:** detect seams → emit a P6 threat event + annotate/quarantine (non-blocking, since the content already exists); block seams → raise a deny (reuse the existing permission-deny channel, see §4) so the write never persists.

### 3.3 Config (no uncustomizable hardcoding — [[feedback_no_uncustomizable_hardcoded_choices]])
A `SecurityConfig` (or a `safety.threat_scan` sub-config): `enabled` (default vs opt-in — **open question, see §5**), per-scope on/off, and a custom-pattern extension point. Default posture is an explicit lead/owner decision (§5).

## 4. Reconcile with existing layers (non-duplication — the key review axis)

| Existing layer | What it gates | Overlap with scan? |
|---|---|---|
| `permissions/` (skill/path-scoped approval) | file/network/exec **access** | None — scan inspects **content**, not access. Block seams **reuse** the permission-deny channel (deny message = decision-enabling, [[feedback_deny_message_decision_enabling]]) rather than inventing a parallel refusal type. |
| `sandbox/` (Landlock/Seatbelt/Seccomp/Docker) | what an exec **can do** | None — EP4 inspects the command **string** before exec; sandbox confines its **effects**. Complementary, both fire. |
| `secrets/` (interpolation, oauth, store scoping) | which secrets a skill can **access** | None for injection-scan. **#1820 tool-output strip** wants redaction of secrets *out of* summaries — there is **no existing redaction fn to reuse** (secrets/ is interpolation+oauth, confirmed), so the strip is genuinely new (a redaction pass, possibly pattern-driven by the same library). |

No existing content-scan / threat / sanitize mechanism exists in `src/reyn/` (confirmed by grep).

## 5. Open design questions — **competitor-impl research needed (do NOT assume)**

Per owner's path: these depend on Hermes/OpenClaw implementation details I have not seen; flagging rather than fabricating. Targeted asks for lead to dispatch:

1. **Hermes `tools/threat_patterns.py` — exact shape:** the real `(regex, pattern_id, scope)` catalog and how `scope` (all/context/strict) maps to *enforcement* (block vs warn vs log) at each call site. I have the 3-tier concept + the multi-word-bypass guard `(?:\w+\s+)*` from the issue, but not the pattern set or the enforcement wiring.
2. **Hermes `tools/tirith_security.py` (Part 2 reference):** the *detection approach* for the pre-exec scan (homograph / pipe-to-interpreter / terminal-escape) — we're avoiding the external binary, so we need the **pattern/heuristic ideas**, not the binary protocol.
3. **False-positive suppression:** what techniques beyond the multi-word guard do Hermes/OpenClaw use to keep FP rates tolerable (whitelist contexts? severity thresholds? scoped disable)? Drives the §3.3 config + the detect-seam annotate-vs-quarantine choice.
4. **OpenClaw `external-content.ts`:** how it marks/fences untrusted external content entering context (delimiter fencing vs scan vs both) — informs whether EP1–EP3 should *fence* (structural) in addition to *scan* (pattern).

**Plus two reyn-side steer points (not research — your call):**
- **A. Default posture:** injection-scan `enabled` by default, or opt-in? Security-correctness argues default-on at detect seams (log-only is low-risk) + default-on block at BP1/BP2; but FP rate (Q3) gates that.
- **B. #1820 strip mechanism:** pattern-driven redaction in the same library, vs a separate secrets-shaped redactor. Leaning same-library (one threat source), pending Q1's pattern shape.

## 6. Staging (clean-break stages, #1794 discipline — each gate-verified)

- **S1:** `threat_patterns.py` library + `scan()` + config skeleton (no integration). Pure unit tests (pattern hit/miss, scope filter, multi-word bypass). *Blocked on Q1 pattern catalog.*
- **S2:** EP1 memory-render scan (detect) — unifies #1821.
- **S3:** EP2 compaction tool-result scan + #1820 strip (detect/redact) — unifies #1820.
- **S4:** EP3 context-file scan (detect) + BP1/BP2 write/install scan (**block**, via permission-deny channel).
- **S5 (Part 2):** EP4 pre-exec command scan (`exec` scope). *Blocked on Q2.*
- *(Part 3 `reyn audit` = separate FP, OSS-publication phase.)*

Gate per stage: scan fires at the seam (positive + negative content), no duplication with permission/sandbox (existing tests green), P7 (no skill strings in OS), config round-trip (non-default value).

## 7. Decision requested
Lead #311-rigor review of: (a) entry-point completeness (EP1–4 + BP1–2), (b) non-duplication map (§4), (c) P7 placement (§3.1), (d) dispatch of the §5 research questions, (e) steer A/B. Then staged impl.
