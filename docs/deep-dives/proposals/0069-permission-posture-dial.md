# FP-0069: A permission posture dial — aligning reyn with the industry shape

**Status**: proposed (not ruled on)
**Proposed**: 2026-09-06
**Author**: architect session
**Track**: owner request 2026-09-06 —「reyn のパーミッションシステム仕様を業界標準に寄せたい」／「できるだけ寄せる案を作成できる？」
**Measurement this rests on**: [research/competitive/permission-modes.md](../research/competitive/permission-modes.md) (Claude Code / Codex / OpenClaw / Hermes, primary docs, 2026-09-06)

---

## Summary

Adopt the one shape all four competitors share and reyn lacks: **a single named posture dial**, ordered strict-to-permissive, switchable at runtime, sitting *over* the machinery reyn already has. Keep every place reyn is stricter than the standard — none of them costs UX. Drop the one place reyn is genuinely heavier than the standard: **the mandatory declaration**.

**This proposal is maximal alignment, as asked.** Its one hard dependency is stated in §6 and it is not free: the mode that produces most of the UX win cannot be built on reyn's current sandbox defaults.

## 1. What "aligned" means, concretely

From the measurement, four convergent properties. This proposal adopts all four.

| # | Property | reyn today |
|---|---|---|
| 1 | One named posture dial, 3-6 values, runtime-switchable | **absent** — `grep -rniE "yolo\|dangerously\|skip.permission\|permission_mode" src/` returns 0 |
| 2 | Posture is separate from the enforcement boundary | partially — the conjunctive restrict layers *are* this separation, but nothing names a posture |
| 3 | A floor no mode can cross | partially — protected write paths exist, but they are not framed as a floor and no mode language exists to be exempt from |
| 4 | Deny composes by intersection; nothing re-grants | **already stronger than the standard** — `all(layer.allows(...))` is structural, not evaluation-order |

## 2. The dial

`permissions.mode` in `reyn.yaml` (overridable in `reyn.local.yaml`, and at runtime). Named by **disposition — what you may do**, not by rank.

| Mode | Meaning | Built from |
|---|---|---|
| `read_only` | Read and search. No write, no exec, no MCP call, no network. | AgentLayer denies the write / exec / mcp / http axes wholesale. No new mechanism. |
| `ask` | Today's behaviour: just-in-time prompt at first use, decision persisted to the ledger with its actor / agent / inode scope. | Exactly the current layer-2 + ledger path, minus the mandatory declaration (§5). |
| `bounded` | **The UX mode.** Inside the boundary, act without asking. Ask only for an action that would *leave* it. | Requires §6. `sandboxed_exec` with a mode-owned restrictive policy; the boundary replaces the prompt. |
| `unbounded` | No prompts, no boundary. | Must be removable by config, the way Claude Code's `disableBypassPermissionsMode` removes `bypassPermissions`. |

Ordering is total and strict-to-permissive, so "at least as strict as X" is expressible.

**Optional fifth, deliberately separable:** `reviewed` — allowlist misses go to an automated reviewer before a human (Claude Code's classifier, OpenClaw's auto-reviewer, Hermes's `smart`; 3 of 4 have one). ⚠️ It puts **an LLM in the gate path**, which adds a cost axis and a new question ("on what basis is the reviewer trusted?"). Held out of the core so it can be dropped without redesigning the dial.

## 3. The floor

Make explicit what all four competitors have: **actions no mode grants.** reyn already has the material — the protected write paths (`.reyn/approvals.jsonl`, `.reyn/index/sources.yaml`) — but they are documented as a carve-out from a default grant, not as a floor mode language must respect.

Restated as a floor: **`unbounded` does not reach them either.** Hermes's hardline blocklist and OpenClaw's host floor are the precedent; Claude Code's is weaker here (`bypassPermissions` *does* write `.git`/`.claude`), so this is a place to follow the stricter two.

## 4. What reyn keeps — every one of these is stricter than the standard and costs no UX

- **The ledger's scope semantics** (actor / op / path + `agent:<name>` + inode identity re-checked per match, #5042 / #5052). No competitor has identity re-binding; Hermes has no per-path scope at all. This is what makes `ask` cheap — a correctly-scoped answer is given once.
- **The conjunctive restrict layers.** Claude Code achieves "deny always wins" by evaluation order across settings scopes; reyn achieves it structurally. Keep as is.
- **Fail-closed when unattended.** Already reyn's behaviour (no bus → denied, not pending). Hermes reaches the same place with three config keys.

## 5. What reyn drops — the mandatory declaration

Layer 2 (`permissions:` in `reyn.yaml`) has **no analogue in any of the four**; all gate at the moment of use. This is the main source of "reyn is heavy", and it is **ceremony-by-default, not security-by-default**: removing it leaves the just-in-time gate untouched, so almost no enforcement is lost.

**Proposal:** the declaration becomes **required only in `read_only` and `ask`-with-`strict_declaration`**, and optional elsewhere. What it buys — a permission inventory readable before anything runs — is preserved as a posture you can choose rather than the only path.

⚠️ **This is a behaviour change and it is UX-visible.** It needs the owner's ruling, not this document's.

## 6. 🔴 The dependency this proposal cannot hide

**`bounded` cannot be built on reyn's current sandbox defaults.**

Owner ruling #3901 (2026-06-05) set every non-`write` axis of `SandboxPolicy` to **open by default** — network, subprocess, env, read — with the stated rationale that "the sandbox's job is bounding what happens *behind* a permitted action, not re-deciding what the launching shell could already do."

Competitors can delete the prompt precisely because their boundary is restrictive: Codex defaults network **off** and confines writes to the workspace; Claude Code merges `sandbox.filesystem` settings with Read/Edit deny rules and domain lists into one boundary, and only then does `autoAllowBashIfSandboxed` drop the prompt. **Deleting reyn's prompt against a compat-open boundary deletes the gate, not the friction.**

**This is not a request to overturn #3901.** That ruling answers "what should a sandbox do behind an action the user already permitted." `bounded` asks a different question — "what may *replace* the permission question" — and the ruling's own rationale does not cover it. So `bounded` carries its **own** policy preset (write: workspace; network: deny unless declared; env/read: unchanged), leaving #3901's default in force wherever `sandboxed_exec` is launched outside the mode.

**Consequence for sequencing:** `read_only`, `ask` and `unbounded` are naming + wiring over machinery that exists. `bounded` is the only one that needs enforcement work, and it is also the one that produces most of the UX gain.

## 7. Migration

Today's behaviour is exactly `ask` **with** a mandatory declaration. So:

- Existing projects keep working with `mode: ask`; nothing about the JIT prompt or the ledger changes.
- Dropping the declaration requirement *removes* prompts (the startup-guard surface), never adds one.
- `reyn.local.yaml` stays the operator-local override, and gains `mode:` like any other key.

## 8. Acceptance

- [ ] `permissions.mode` is a required key with no silent default, or has a default stated in one place that a test reads. (Every mode-less caller drifting to the most permissive value is the failure this whole arc kept closing.)
- [ ] The order is total and testable: for every pair, "strictly more permissive than" has one answer.
- [ ] A capability denied by a restrict layer stays denied in **every** mode, `unbounded` included — the conjunction is not mode-aware.
- [ ] The floor (§3) is unreachable from `unbounded`, with a test that goes red if a mode is added that reaches it.
- [ ] `mode: ask` on an existing project is byte-identical to today's behaviour apart from the declaration requirement.
- [ ] `bounded` never runs an action outside its boundary without a prompt — the strip-falsifier is removing the boundary and watching the acceptance go red, not watching the prompt disappear.
- [ ] Removing `unbounded` by config is possible, and a session cannot re-enable it.

## 9. Open questions — owner's, not this document's

1. **Adopt the dial at all?** §2 is the whole alignment; everything else is detail.
2. **Make the declaration optional (§5)?** The single biggest UX change, and the one real deviation from the standard.
3. **`bounded`'s own sandbox preset (§6)?** Needed for the UX win; a new default in a place a prior ruling deliberately left open.
4. **`reviewed` (§2, optional)?** Independent of 1-3. Puts an LLM in the gate path.
