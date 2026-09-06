# FP-0069: A permission posture dial — aligning reyn with the industry shape

**Status**: ruled (owner 2026-09-06, three verbatim rulings in §10) — §2 the dial, §5 opt-in declaration, §6.1 `bounded`'s boundary (network outside it: declared → silent, undeclared → **ask**), and `reviewed` (not now) are all decided. What remains is implementation, tracked on [#5825](https://github.com/tya5/reyn/issues/5825) (the network-ask seam design) and the FP's own acceptance (§8).
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

**Floor vocabulary (architect ruling 2026-09-06, #5825 ③ / #5849 ②):** a per-axis `permissions.<axis>: deny` is floor language **only for an axis that has no other declaration** — `permissions.network: deny` qualifies (nothing else declares a subprocess's network; see §6.1). `permissions.exec: deny` does **not**: a tool is already floored by `CapabilityProfile.tool_deny: [exec]`, and a second declaration of the same axis is exactly the shape #5848 deleted. So `exec: deny` stays an unrecognized key (#5849 ③), not a floor entry.

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

**This is not a request to overturn #3901.** That ruling answers "what should a sandbox do behind an action the user already permitted." `bounded` asks a different question — "what may *replace* the permission question" — and the ruling's own rationale does not cover it. So `bounded` carries its **own** policy preset (write: workspace; network: **closed**, with leaving it a declaration-or-ask act — §6.1; env/read: unchanged), leaving #3901's default in force wherever `sandboxed_exec` is launched outside the mode.

### 6.1 What the preset actually has to close — the axis that decides it is **network**

`bounded` runs commands **nobody was asked about**. So the question the preset answers is not "what could the launching shell do" but "what can an action nobody approved reach". Per axis:

| Axis | In `bounded` | Why |
|---|---|---|
| **write** | workspace only (already the #3901 default) | unchanged |
| 🔴 **network** | **closed by the preset; crossing it is declared → silent, undeclared → ask** (owner ruling 2026-09-06, §10) | The exfiltration path. If an unprompted command can reach the internet, the box has a hole and `bounded` is not a boundary — it is `unbounded` with a smaller write scope. This is exactly why Codex defaults network off. The owner's ruling keeps the boundary and makes the crossing an **ask**, not a silent deny — §2's own `bounded` definition ("ask only for an action that would *leave* it"), applied to network. |
| **subprocess** | open | A child inherits the same boundary (seccomp / Seatbelt bound the process tree), so it adds no reach. |
| **env** | open | Safe **only because** network is closed: a command can read a secret but cannot send it. This is #1199's original argument, which `permission-model.md` records as having died when #3901 opened network — **under `bounded` it is restored**, because `bounded` closes network again. |
| **read** | broad-allow | Unchanged; system paths are needed just to load a binary. |

⭕ **The preset already exists and is already correct.** `sandbox.mode: strict` (#3823) resolves exactly these defaults — `_SANDBOX_STRICT_MODE_DEFAULTS = {"network": False, "deny_subprocess": True, "allow_env_names": []}`, with `write` deliberately excluded (zeroing it would block the op's own workspace, per #3823's co-vet correction). **`bounded` does not need a new preset; it needs to select this one.**

~~🔴 But it is not wired~~ — **wired 2026-09-06** (#5818 → #5821: `resolve_sandbox_policy(..., mode=)` is now a required argument every production caller passes, and `sandbox.mode: strict` reaches the resolver). With that done, §6's cost fell from "new enforcement work" to "the mode selects an existing key" plus the ask seam below.

**Where the declaration lands — corrected 2026-09-06:** an earlier draft of this paragraph pointed at `permissions.http.get: [{host: …}]`. That axis stays what it is — the per-host declaration + prompt for the LLM's own `web_fetch` tool (`require_http_get`), where the gate genuinely sees the host. It is **not** the declaration for a subprocess: no sandbox backend can scope a child process's network by host (Seatbelt is on/off for outbound, Linux carries the deny in the seccomp allowlist — Landlock has no network API — Docker is `--network none`), so a host list there would claim more than the boundary enforces. For `sandboxed_exec` the honest granularity is **boolean**: `permissions.network: allow` (config pre-approval) or a persisted ledger grant, and otherwise the **ask**. The seam — an explicit per-call `network` request on the exec op, one `require_network` check before spawn, ledger key `<actor>/network/*`, operator-declared surfaces (hooks, MCP servers, `skill_install`) unchanged — is the architect ruling on [#5825](https://github.com/tya5/reyn/issues/5825) (2026-09-06). §5's now-optional declaration still earns its keep here: declaring network is what buys a prompt-free `bounded` workspace for commands that need it.

**The alternative, stated so it is a choice and not an omission (and not chosen — §10):** leaving network open in `bounded` is cheaper and needs no preset — but then `bounded` deletes prompts without adding a boundary, which is the one thing the measurement says the industry did **not** do.

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
- [ ] In `bounded`, an exec that requests network with no declaration asks once; with `permissions.network: allow` or a ledger grant it passes silently; with `permissions.network: deny` it is refused without asking; an exec that does not request network is never asked (#5825's seven witnesses).
- [ ] When the configured sandbox backend cannot enforce the network deny (`sandbox_policy_not_applied`), `bounded` is shown as degraded in the posture surface — a boundary that is not enforced is not silently called one.
- [ ] Removing `unbounded` by config is possible, and a session cannot re-enable it.

## 9. `plan` is a phase, not a posture

Claude Code has `plan` on its mode dial. **This proposal deliberately does not**, and the reason is not that reyn shouldn't have planning.

`plan` conflates two independent axes. "Read-only" is a **permission posture**. "Explore, propose, then act on approval" is a **workflow phase**. Claude Code can merge them because it has one agent in one loop, so "the planning phase" and "the read-only posture" are the same interval. reyn is a multi-agent OS: a phase belongs to a turn, a posture belongs to a session, and a pipeline can want a planning phase inside any posture. Putting `plan` on the dial makes it answer "is planning stricter or looser than `bounded`?" — a question with no meaning.

**Measured:** reyn has no plan machinery today (`git grep -niE "plan_mode|exit_plan|propose.*approve" -- src/` returns zero), and it does have the approval seam a plan phase would need (`ask_user`, the intervention bus).

**If planning is wanted, the reyn-shaped form is:** posture stays `read_only` for the duration, and the plan→execute transition is an intervention event on the existing bus — the same "spec vs binding" separation `CapabilityProfile` already uses for its two adapters. That is a separate proposal, not a value on this dial.

## 10. Owner rulings so far

**2026-09-06, verbatim** 「**ダイヤル入れる。宣言はoptin。3,4 解説して。あと plan モードは？**」

- **§2 the dial — ACCEPTED.**
- **§5 the declaration becomes opt-in — ACCEPTED.**
- §6 (`bounded`'s boundary) and the `reviewed` tier — explanation requested at that point; ruled later the same day (below).
- `plan` — answered in §9.

**2026-09-06 (later), verbatim** 「**network は境界の外で良いよ。宣言 or ask で許可だよね？**」

- **§6.1 `bounded`'s boundary — ACCEPTED with the crossing as an ask.** Network is outside the boundary; a declaration lets an exec cross it silently, and with no declaration the crossing is asked about — never a silent deny. The architect's earlier reading on #5838/#5825 ("undeclared → deny") was the wrong side of that choice and is corrected on #5825; the seam design lives there.

**2026-09-06 (later), verbatim** 「**reviewed は今はいらない。今後入れる可能性はあるけどね。**」

- **`reviewed` — NOT NOW.** The dial ships as `read_only` / `ask` / `bounded` / `unbounded`; the `reviewed` paragraph in §2 stays as the separable design so a later decision does not need a redesign.

## 9. Open questions — owner's, not this document's

1. ~~Adopt the dial at all?~~ **Ruled: yes** (§10).
2. ~~Make the declaration optional (§5)?~~ **Ruled: opt-in** (§10).
3. ~~`bounded`'s boundary (§6.1)?~~ **Ruled: network is outside the boundary; crossing it is declared → silent, undeclared → ask** (§10, #5825). Prerequisite #5818 landed (#5821).
4. ~~`reviewed` (§2)?~~ **Ruled: not now; the door stays open** (§10). It remains the only element of this proposal that **adds** a trust assumption rather than removing one, which is why it stays separable from the dial.
