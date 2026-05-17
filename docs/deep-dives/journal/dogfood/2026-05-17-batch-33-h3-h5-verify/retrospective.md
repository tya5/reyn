# Batch 33 — Retrospective

> Fifth dogfood batch. First batch where the OS-layer fixes (H3 + #53)
> verified cleanly while the aggregate verified count moved 0 net.
> The reading is "OS got better; harness needs to catch up." This
> retrospective records the missed step in the ablation phase that
> let a downstream contract gap land alongside the H3 patch.

---

## 1. What this batch verified, what it didn't

### Verified

- **H3 race fix** structurally landed: `invoke_skill_spawn_ack_exit` fires across 8 different workers' applicable scenarios. The B32 §4.3 "I will notify you" hallucination is gone. W6's `s-fp12-completion-2-error-narrate` now reports the real `workflow_aborted` reason.
- **#53 web.fetch enforcement** fix (commit `b5d81e4` from another session): W4 S8 emits `permission_denied` and the LLM produces a clear explanation. Previous silent bypass eliminated.
- **C1 / Q2** stability holds across the 4th batch in a row.
- **H5 refit** calibration paying off: mean Brier ≈ 0.26 across reporting workers (= B27's pre-refit was 0.913).
- **Complete wipe recipe** (= include `wal.jsonl` + `history.jsonl` + `reyn/local/`): zero `session_restored` events. The B32-NEW-FINDING gap is closed.

### Not verified

- **W2 F2 driver harness reply capture gap**: post-H3, `send_to_agent_impl` returns empty reply on spawn-ack turns because the driver's quiescence boundary is the spawn turn, not the subsequent inbox-driven re-engage. This is the missed step from the ablation phase.
- **W6 NEW-1 `phase_no_progress` abort path**: `skill_completion_injected` is skipped on `workflow_aborted` via `phase_no_progress`, but fires on LLM-initiated abort. Different code paths in the inbox-injection logic.
- **W5 F2 peer-agent silent hallucination**: when an agent.peer__ target doesn't exist, the dispatch returns success-shaped and the LLM fabricates content. Trust-breaking.

---

## 2. The miss I owe to the user's discipline correction

In B30 the user reminded me to remember the dogfood principle (= memory `feedback_pre_conclusion_observation_checklist.md`). The B32 ablation wave was the first time I applied it end-to-end. The H3 land + H5 refit shipped cleanly under that discipline at the OS layer.

B33 exposes a gap one layer above: the **downstream consumer contract** for H3. Specifically:

- The H3 ablation captured a B28 trace, applied `--patch` to undo the router-internal change, and verified the LLM's tool choice did NOT shift. K/N=1/22 at batch scale. Correct.
- The cross-confirmation by H1 (strong model) showed the `(answered)` hallucination is OS-layer, not LLM-layer. Correct.
- **What was not analysed**: the contract between `_handle_skill_completed`'s inbox-driven re-engage and `send_to_agent_impl`'s quiescence detection. The dogfood driver's stdin-pipe pattern uses `send_to_agent_impl` as the boundary; post-H3 that boundary needs to wait until the inbox re-engage completes.

The miss is recorded in §6 of `findings.md`. The pattern correction for future fix waves:
- Context analysis must include "what downstream consumers does this fix change the contract for, and do they adapt automatically or need a co-patch?"
- Ablation must exercise the full driver→OS→back-to-driver path for any patch that changes router event flow or quiescence semantics.

---

## 3. The honest aggregate read

| Layer | ΔV from B32 |
|---|---|
| OS-layer wins (H3 + #53) | +3 |
| Driver harness gap (W2 F2) | -1 |
| LLM probabilistic variance (W1) | -2 |
| **Net** | **0** |

Reading the trajectory B28 12 → B30 10 → B32 11 → B33 12 as "the system has plateaued at ~11 V" is the right frame. The 4-batch mean is ~11.25 V (~19%); single-batch swings of ±2 V are within probabilistic noise per the H5 ablation finding.

The B33 framing — *"verified rate stable"* — is technically true. But the more useful framing is *"OS got better, harness gap surfaced, net cancelled."*

---

## 4. Process reflection

### What worked

- **Honest worker reporting**: W2 F2 explicitly tagged the harness gap as a structural finding, not as an LLM regression. The aggregation flowed from primary data.
- **Discipline held**: no "fix X caused Y" inference paragraphs in mid-batch reports. The user's discipline correction from B30 has stuck.
- **W3 / W7 workers applied the 5Q check inline**: when S1 in W3 flipped R→V, the worker explicitly flagged "primary data shows file__read direct path, but H3 causation vs LLM variance is N=1." That's the discipline operating correctly.

### What needs adjustment

- **Ablation scope must include downstream consumers**. Future ablations for any patch that changes:
  - router event flow
  - quiescence semantics
  - inbox / completion injection paths
  ...must run the driver→agent→inbox→driver loop at least once before merge. Not just `--patch` LLM-replay.
- **Driver-layer regression tests** would have caught W2 F2 immediately. Adding a `test_send_to_agent_impl_waits_for_inbox_re_engage` or similar to `tests/` is a natural follow-up.

### What surprised us

- **C1's continued boring stability** (4 batches × ~10 workers each, zero duplicates): the right kind of boring.
- **H3 fix delivered structurally but the aggregate verified-rate didn't move**: in earlier batches, OS-layer fixes (= C1) showed up as +N V immediately. H3 also delivered, but the driver gap absorbed the gain. The lesson: OS fixes that change *contracts* need a different kind of verification than OS fixes that simply *fix bugs at one site*.

---

## 5. Cross-reference to memory

- `feedback_pre_conclusion_observation_checklist.md` — held in mid-batch reports; the discipline transferred from B30 correction.
- `feedback_iterative_replay_patch_disambiguation.md` — applied for H3/H4/H6/H7; the lesson learned is that LLM-replay ablation does not exercise driver contracts.
- `feedback_envelope_layer_fix.md` — H3 was an envelope-layer fix at the router. The driver layer below the envelope still needed adaptation.
- `feedback_observe_before_speculate_llm.md` — held; no LLM-side speculation paragraphs.

---

## 6. Fix wave priorities for B34

In priority order, with ablation pre-conditions for each:

1. **W2 F2 driver harness gap** (= my missed step from H3 ablation phase). Pre-ablate: capture the W2 S4/S5/S7 trace; patch `send_to_agent_impl` quiescence; re-run; measure K/N flip. Then land. **High leverage** (= probably restores 1-3 V across multiple workers).
2. **W6 NEW-1 phase_no_progress inject gap** (= `skill_completion_injected` skipped on phase-loop rollback abort). Small patch; needs trace-side confirmation first.
3. **W5 F2 peer-agent silent hallucination**. Handler-layer error envelope; small patch + Tier-2 test.
4. **LLM arg-name normalization** (= `file__write` `text` vs `content`, `drop_source` `source_id` vs `source`). Envelope-layer defensive normalization.
5. **task #93 framework verifier integration**. W4 confirms it's still required: manual rubric 4V vs framework-reported 0V on the same 8 scenarios.

Deferred:
- PLAN-STEP-PATH (= partial, scenario-design heavy).
- file__grep attractor (= choose between implement or envelope hint).
- #52 (B27-H4 acompletion never awaited) — not retested this batch.

---

## 7. Goal restated

Five batches in: **OS layer continues to land cleanly; the new bottleneck is the driver / harness contract layer one level up**. The disciplines that took us from B27's 0/58 to B33's 12/58 (= primary-data findings, ablation before fix, observation-first phrasing, calibration honesty) hold. The next discipline addition is *downstream consumer contract analysis* — make sure every OS fix that changes a contract is mirrored in the consumers that depend on it.

Target for B34: W2 F2 driver gap landed cleanly with ablation pre-check. Verified rate should move +1 to +3 V if the gap absorbs the expected fraction of W2/W6 refused-by-empty-reply scenarios.
