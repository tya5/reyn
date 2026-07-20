# CLAUDE.md — Reyn Agent OS rules

Tier 1 hard rules for code-writing agents. Read on demand for rationale and
deep dives via the references at the bottom.

## Constitution

> **Reyn is an operating system for LLM agents** — they decide, organize, and orchestrate; the OS makes every action typed, permissioned, audited, and recoverable by construction.

Every new feature is read through **eight engineering lenses** and must stand on the **cross-cutting band**. A lens asks *"does this do X well?"*; the band asks *"does this obey the universals at all?"* — fail a band member and it does not ship.

### The eight lenses — each line is the pass-line (a gate for new work)
1. **System Design** — responsibility sits at the right layer (LLM decides / OS executes / feature owns its domain); no new cross-layer coupling.
2. **Tool Contract** — every side effect rides a typed, validated envelope (Control IR / a typed op), never an untyped string the LLM free-forms.
3. **Retrieval** — the right context is delivered deterministically at the right time (`semantic_search` + a pluggable `IndexBackend` a safe-mode Python step can call directly), not stuffed unconditionally into the prompt.
4. **Reliability** — it recovers from failure (schema-validate + re-prompt, bounded loops with graceful force-close, timeout + opt-in provider-retry); any derived state survives WAL truncation.
5. **Security** — it is permission-gated and sandbox-scoped; no capability reaches the world without passing the gatekeeper.
6. **Evaluation** — its output can be scored against a rubric in-run (an `agent` step + `schema`: the OS constrains generation and validates the parsed result; the threshold comparison is a plain `if`).
7. **Observability** — it leaves an audit-event trace sufficient to inspect and reconstruct what happened (the P6 audit log, `reyn events` replay, live audit chips).
8. **Product Think** — it is predictable, cost-disciplined, and legible to the operator (CLI/CUI affordance, cost reporting, and token-cost *reduction* such as zero-token `present`/offload).

### The cross-cutting band — the foundation every feature obeys
**permission · audit-events · workspace-SSoT · crash-recovery (WAL) · cost/budget (bounding).**

Three lenses name a *discipline* whose *universal mechanism* is a band member: **Security ↔ permission**, **Reliability ↔ crash-recovery (WAL)**, **Observability ↔ audit-events**. The band is where the still-true P5 (workspace) / P6 (events) / P7 (OS-domain-agnostic) survive, demoted from "principles" to the substrate every lens-cell stands on.

*Two honest thin areas (where new work is most valuable): **Retrieval** (`semantic_search` + a RAG framework to build on, not a fixed pipeline) and **Evaluation** (an `agent` step + `schema` is the surviving eval surface — the bespoke `judge_output` scorer op and the eval-export subsystem were both removed; scoring is ordinary agent work riding the OS's typed-schema + cost-tracking substrate, not a special-cased op).*

*"event" is three distinct things — **audit-event** (P6 `.reyn/events`, the audit trail) / **WAL-event** (`.reyn/state/wal.jsonl`, the recovery substrate) / **hook-event** (lifecycle+external reactivity triggers). Never write bare "event".*

*(The full 8×7 populated table lives in `docs/concepts/architecture/charter.md`; this skeleton is the durable core agents read before new work. Tagline: hero = the line above (T1); one-liner/meta = "An agent OS where agency is bounded by construction — decide, spawn, orchestrate, but only through typed, permissioned, auditable, rewindable ops." (T3).)*

## Hard rules

- **A doc describing a mechanism is stale the moment that mechanism's code changes — fix the doc in the SAME PR, not a follow-up.** This is broader than adding a new enum-like variant: a doc goes stale just as easily by describing a *field*, a *call path*, or a *"when wired" claim* that the PR removes or falsifies. (#2949→#2958: `control-ir.md:805` kept asserting a recording path through a field that #2958 deleted — survived because the reviewing pass grepped the doc for the one keyword it expected, not the surrounding prose describing what the PR touched.) When a PR changes something a doc describes, re-read the whole section the change touches, not just the line whose keyword you already had in mind.
- **`docs/reference/runtime/control-ir.md` must stay synced with `OP_KIND_MODEL_MAP`** in `src/reyn/schemas/models.py` (#1983: relocated there from `op_runtime/registry.py` so the `Op` union derives from the same map) — the sharpest, CI-checked instance of the rule above, not the whole of it. New op kinds get a section in the reference in the same PR.
- **Recovery-feature PR gate**: any PR adding recovery / reconstruction functionality (WAL-event-derived state, PITR, rewind/restore paths) MUST include a truncate-falsify test verifying the reconstruction source survives WAL truncation below its source events (set X → truncate past X's events → reconstruct → assert X survives). WAL-event-derived recovery state that isn't snapshot-backed is a silent data-loss vector. Same PR, not a follow-up. (Motivated by #2259/#2260.)
- **Sandbox axis-witness gate stays 2-layer**: `enforcement_self_test` (`src/reyn/security/sandbox/self_test.py`) is the PRODUCTION gate every real backend resolution calls; its blast radius is every sandboxed op on every host, so it MUST keep witnessing the deny leg only, for the write + spawn axes only. The richer per-axis 3-tuple contract (deny / exception-boundary / workload — `reyn.security.sandbox.axis_contract`) is CI-conformance-only (`tests/test_sandbox_axis_contract_2983.py`, mirroring `scripts/sandbox_landlock_deny_gate.py`'s CI-only deny arms). A PR that folds a new axis or leg into `enforcement_self_test` widens the blast radius of a probe bug to every host's sandbox — do not do this without an explicit owner-level design decision (#2983).

## Testing policy (READ BEFORE WRITING TESTS)

The testing policy is at **`docs/deep-dives/contributing/testing.ja.md`** (English:
`docs/deep-dives/contributing/testing.md`). It is normative — read it before adding
or modifying tests. For co-vet review and gate-design specifically, also read
`docs/deep-dives/contributing/verification-hazards.md` — a checklist of ways a
green result can mean less than it looks like it means, each with a real instance
and a detection technique that closed it.

Key constraints (full rationale in the doc):

- Each test belongs to exactly one Tier (1: Contract / 2: OS invariant /
  3: LLM-replay behavior). Anything that doesn't fit a Tier is **Tier 4 —
  do not write**.
- NEVER fake a collaborator — `unittest.mock.MagicMock` / `AsyncMock` /
  `patch`, or a hand-rolled stand-in class — when a real instance is cheaply
  constructible. Use real instances or the `LLMReplay` Fake. A faked callable
  bypasses signature-drift detection (raises loudly when it should); a faked
  data/state object can silently carry a field the real type doesn't have,
  which raises nothing at all (#3037: an invented `permission_resolver`
  field made a dead permission gate look tested). Same ban, two failure
  modes — see `testing.md` § Mock vs Fake.
- NEVER assert on private state (`tracker._daily_tokens == 100`,
  `mgr._timers["c1"]`, `reg._active[id]`). Use the public surface or a
  `snapshot()`-style read.
- NEVER pin algorithm-level behavior (sort order, dict iteration order,
  internal cache structure, exact whitespace / formatting).
- NEVER add snapshot / golden-file tests outside `tests/scaffold/`.
- Tests for an extracted refactor belong in `tests/scaffold/` with
  `triggered_by` / `removed_by` metadata, and are **deleted in the PR
  that lands the refactor**.
- Each test docstring's first line must declare its Tier:
  `"""Tier 3a: ..."""`.

## PR workflow (READ BEFORE OPENING / REVIEWING A PR)

This repo is touched by multiple Claude sessions (lead-coder, e2e-coder,
per-PR coders) authenticating as the same `gh` user.

**Before you open a PR, run all four CI gates locally** — `ruff check src
tests`, `python scripts/test_tier_audit.py --strict <changed test files>`,
`pytest` (from the repo root), and
`python scripts/verify_module_docstrings.py <changed src files>` are *separate*
CI jobs. A green `pytest` alone is **not** a green CI run (`pytest-green ≠
CI-green`): ruff `I001` import-sort and a Tier-4 format pin (`len(...) == N`)
both fail CI while `pytest` passes. Details + the Tier-4 → behavioral-assertion
fix idioms: `docs/deep-dives/contributing/testing.md` § "Before you push".

Three rules then keep multi-session work coherent:

1. **Finish your own Test plan before merge.** PR authors run every
   Manual / Visual item in the Test plan and tick the box, or replace
   `- [ ]` with `- [x] (skipped — <reason>)`. Reviewers do not merge
   while items are unchecked without an explicit waiver.
2. **Role-prefix every issue / PR body / PR comment.** Start the PR
   body AND each follow-up comment with `**[role-name]** — ` (e.g.
   `[lead-coder]`, `[e2e-coder]`, `[tui-coder]`, `[dogfood-coder]`,
   `[per-PR-coder]`, `[security-reviewer]`) so the recipient session
   can tell "this is feedback for me" from "I wrote this earlier
   myself". **The PR body counts** — it is the first comment a
   reviewing session reads, and without the prefix the role of the
   author can only be inferred from branch naming (= a hint, not the
   workflow contract). The `Co-Authored-By: Claude` commit trailer
   does not propagate to PR comments, so this prefix is the only
   cross-session signal.
3. **If broker MCP is connected, supplement PR comments with
   `post_message` for time-sensitive coordination.** When a session
   would otherwise wait for the peer's next manual polling to notice
   a block or revision-ready signal, send a parallel broker message
   (= `post_message(to=<peer>, ...)` with a short summary). Typical
   uses: "revision pushed, ready for re-review", "block raised on
   #N", "I'm picking up #M, please pause on it". **PR comments
   remain the authoritative audit trail** — review decisions (block /
   accept / merge), revision rationale, and review evidence all stay
   in PR body / comments / commit messages. Broker is only for
   reducing reviewer-side latency. When broker is unavailable or the
   peer is offline, fall back to PR comment alone — the workflow
   still works.

   **Broker semantic limits (= treat broker as best-effort hint):**
   - **In-memory only**: broker process restart drops all queued
     messages. If the broker maintainer announces a restart, every
     session must rewrite any in-flight coordination signal as a PR
     comment so the contract is preserved.
   - **Up to ~30s lag**: watcher polls at ~30s intervals, so a
     "block raised on #N" race with the peer's `git push` is not
     fully closed by broker alone. Truly critical pause / block /
     "do not merge" signals must land on the PR comment **and** via
     broker — never broker alone.
   - **No ack semantics**: `post_message` returns `queued` only.
     The sender cannot tell whether the recipient has read or acted
     on the message. For coordination that must be confirmed, ask
     the peer to ack via broker reply, or verify the outcome on the
     PR (= comment posted, push paused, etc.). Do not assume "I
     posted, therefore they paused".

   In one line: **broker = hint, PR = contract**.

4. **Closing-keyword caution (sub-PR arcs).** GitHub `closes/fixes/resolves #N`
   keywords match **literally** regardless of sentence context — a sub-PR body
   containing `closes #X` auto-closes `#X` on merge even if the sentence reads
   "this PR partially closes #X". For sub-PRs in a multi-PR arc, use `part of
   #X` or `toward #X`. Only the final PR that actually completes the umbrella
   issue should use `Closes #X`.
   **Reviewer recovery angle:** an unexpected issue auto-close triggered by a
   sub-PR merge is almost always a closing-keyword false positive. Reopen the
   issue and verify the arc is not half-done before assuming completion.
5. **No blanket en/ja mirror obligation.** JA docs are a curated, intentionally
   partial subset (614 EN files vs 125 JA, repo-wide) — a PR is not required to
   touch a `.ja.md` file just because an EN sibling exists or just changed. Do
   fix a `.ja.md` file's own claims if the PR falsifies something it currently
   asserts (same reasoning as any other doc-drift fix, not a mirror rule); do
   not invent a "keep en/ja pairs in sync" obligation when scoping a PR. Known
   ja-parity gaps are tracked in #2967 as backlog, not a per-PR gate.

## Pre-conclusion observation checklist (READ BEFORE WRITING ANY FINDING / 結論)

**Active trigger**: when you are about to write any of the following — **STOP**
and run the checklist below before continuing:

- 結論 / conclusion / finding / 確定 / decisive
- パターン / pattern / 一貫して / consistently
- 100% / 全件 / N/N / 0% / all / every
- proven / validated / confirmed / 決定的
- attractor / hallucination / regression (= behavioral classification)

**5-question checklist**:

1. List each specific observation that supports the claim — can you?
2. Is each observation **primary data** (= events log / metric / direct
   output) or **inference** from other observations? Inference chains
   downgrade "verified" to "hypothesised".
3. Did you actively look for data that **falsifies** the claim?
4. Is the observation infrastructure (= trace dump / events log /
   metric) actually capturing what you'd need to support the claim?
5. If you're about to write "N/N" or "100%", did you directly inspect
   each of the N items, or did you inspect 1-2 and extrapolate?

**Re-frame instead of overstating**:

- ❌ "X happens 100% in condition Y" (= when only 1-2 of N inspected)
- ✅ "Hypothesis: X may dominate in Y. Direct verification: 1/N. Remaining
  N-1 inspection pending."

**Reference**:
- `feedback_pre_conclusion_observation_checklist.md` (full 5-question detail
  + failure-mode patterns)
- `feedback_observe_before_speculate_llm.md` (passive principle this trigger
  operationalises)

## When in doubt — read these

- **Verification hazards** (why a green result can mean less than it looks
  like — co-vet review, gate design): `docs/deep-dives/contributing/verification-hazards.md`
- **Workspace** (single source of truth): `docs/concepts/runtime/workspace.md`
- **Events / replay** (audit truth): `docs/concepts/runtime/events.md`
- **`.reyn/` directory layout** (what's recovery-core vs persist/audit/cache/outside, the
  recovery-core write-gate, where new subsystem data goes):
  `docs/reference/runtime/reyn-dir-layout.md`
- **Permission model**: `docs/concepts/runtime/permission-model.md`
- **Op catalog and dispatch**: `src/reyn/core/op_runtime/`
- **LLM trace analysis**: `docs/reference/dogfood-tracing.md` — `scripts/dogfood_trace.py --mode llm-payloads` is the canonical entry point for inspecting LLM payloads; do not hand-parse JSONL.
- **Full feature inventory**: `docs/feature-map.md` — every implemented feature grouped by subsystem, each linked to its reference/concept doc (impl-extracted; impl↔doc mirror).
