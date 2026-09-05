# ADR-0046 (#5747) — reyn bounds resources, not control flow

**Status**: **PROPOSED** (owner discussion 2026-09-05; owner called this line 有力 — "a leading candidate" — and explicitly did **not** ratify it: 「今のは議論だから設計・実装への反映はまだしないでね。どういう結論になるか次第だから」). **Nothing in this ADR authorises an implementation.**

**Extends**: the #5561 owner ruling that abolished the hook loop valve, by stating the general form that ruling was a specific case of.

**Track**: #5747 (hook-originated mid-turn injection) → this.

## Context

[#5747](https://github.com/tya5/reyn/issues/5747) admits `TurnOrigin.HOOK` to `MID_TURN_INJECTABLE`, so an operator-declared hook's message can reach a session **during** a running turn rather than only at the next turn boundary. The driving incident: a stop order to a peer session did not arrive until the wrong first step had already run.

That raised a question the issue could not answer from inside itself: **does mid-turn injection need a recursion guard?** A hook declared on `builtin:external:file_changed` fires when files change; a turn that writes files therefore fires its own hook, whose injection may prompt more writes.

Three control-flow guards were considered. All three are rejected below, and two of them had already been rejected before this issue existed.

## The decision (candidate)

**reyn bounds resources. It does not bound control flow.**

- Each charter band member bounds **its own resource, cause-independently**: `CostConfig` bounds compute, the media store's project-wide cross-session cap bounds storage, the permission gate bounds outward side effects.
- **Termination of an operator's own workflow is the operator's.** A recursive workflow that converges is legitimate — build systems and file watchers are exactly this shape — and the OS must not forbid the shape in order to prevent the non-converging case.
- The OS retains two duties, **neither of which is a threshold**: a running loop must be **visible**, and it must be **stoppable**. "User responsibility" is only coherent when the user can see the thing they are responsible for.
- **The OS bounds resources; it does not discard the user's own data.** A cache is the OS's to reclaim; a conversation is not. The distinction is not size or growth rate but authorship — `cache/` is derived, `events/` is a forensic record the OS writes, and `history.jsonl` **is the conversation itself**. Reclaiming the first two is resource bounding; deleting the third is taking something from the user, which this decision does not authorise. (Raised while drafting the #5759 countermeasures; see that issue and the Falsification section below for the actual eligibility conditions the #5759 GC applies — not restated here, since a shortened form risks being read as the complete predicate when it is not.)

## Alternatives considered

| Alternative | Why not |
|---|---|
| **A raw count of hook-driven turns** (`safety.loop.max_hook_driven_turns`, default 25) | Already abolished, #5561, owner ruling 2026-08-30. Owner verbatim: 「hook 起動を回数で制限なんて誰も設定できないでしょ。どんな回数が妥当か誰も判断できない」. The default was, in the concept doc's own words, 「意図的な決定を装った事実上の未検討回答」. |
| **A predicate detecting a true self-continuation cycle** | Considered and rejected in the same #5561 ruling: no such cycle has ever been observed. |
| **A static partition of hook points by "can a running turn cause this point to fire"** (architect, #5747) | **Withdrawn by its author.** It is a variant of the already-rejected cycle predicate, and strictly worse: cycle detection costs no expressiveness, while a static partition forbids legitimate watch/build workflows outright. Owner verbatim: 「あなたの考えた機構によってユーザによる決定論的ワークフローのできることを狭めるという弊害が許容できるのかという問題に差し代わるだけ」／「これは、再帰無限を単純に禁止すれば良いという問題ではなさそうに思える」. |

The common defect in all three: they bound **how the workflow is shaped**, and no operator can state the correct shape or count in advance. A resource cap asks a question the operator *can* answer ("how much am I willing to spend").

## Consequences

**Desirable**

- No new mechanism. The #5561 line extends to the injection path unchanged.
- The `fold` batching (#5516) already sits **upstream** of the injection decision — it folds *launches*, and `template_push`'s own inbox push is what injection later consumes — so injection inherits burst-amplification bounding for free. Measured: `drain_folded`'s call sites are `hooks/composed_consumer.py:139`, `hooks/external_fire.py:263`, `hooks/ingress.py:263`, all on the external-event ingress path feeding the dispatcher.
- Per-push size caps (#5210/#5244, `Spillability` / `spillability_max_chars`) are likewise upstream.

**Undesirable, and stated rather than hidden**

- `fold` collapses a **burst** of simultaneously-queued events into one launch. It does **not** collapse a steady loop that fires once per turn. For that shape the only remaining bound is a resource cap, so the terminal is **"a cap is exhausted, loudly"** rather than "the loop was prevented".
- Whether that terminal is acceptable is a values question this ADR does not settle. It is the question owner is holding open.

**Open obligation created by this decision**

- The "visible and stoppable" duty above is **unmeasured**. Whether a spinning hook loop can be identified from `reyn doctor` or the TUI, and whether an operator has a way to stop it, has not been checked. If the duty is unmet, this decision is not yet safe to rely on.

## Falsification

This decision is falsified by **a runaway that consumes a resource no band member bounds**.

**Two candidates were found after drafting** (2026-09-05, [#5759](https://github.com/tya5/reyn/issues/5759)), by walking `docs/reference/runtime/reyn-dir-layout.md`'s own tree rather than asking "what does a loop cost". **One of the two was a misdetection, retracted on further measurement — not a second closed candidate.** The remaining, genuine candidate is `history.jsonl`; its own bound is narrowed but not (yet) fully resolved:

- `agents/*/history.jsonl` — append-only, and (before the #5759 GC below) had no floor-truncation of any kind. **The #5759 GC (`reyn.runtime.history_tail_reader.rewrite_history_dropping`) is not a floor mechanism**; it drops a bounded, caller-identified middle range (content already folded into a compaction summary AND already outside both the WAL's own retention floor and the startup-hydration margin), never a `seq < floor` prefix — see `reyn.core.events.retention` for the retention-floor reasoning this GC sits alongside without changing. **What the GC changes is not that description but the conclusion drawn from it**: this resource is no longer *unbounded* in the sense this ADR's own "does not discard the user's own data" line cares about, because the only content it removes is content no surviving `/rewind` point or hydration read could show the user anyway. Whether that is a sufficient bound, or whether the remainder (everything not yet folded, or folded but still within the WAL floor / hydration margin) still falsifies the decision, is not settled here.
- `events/` — **retracted as a candidate (measured, not merely reasoned).** `audit_events:` already has a purge mechanism (#4479, owner ruling 2026-08-13) on 2 axes — `cleanup_period_days` (default 30) and `max_disk_usage_percent` (default 10), either one sufficient to fire — and both are measured live: the oldest file on disk is 23 days old (inside the 30-day window; older files are genuinely gone) and 108M against ~194Gi free is 0.05% (correctly below the 10% disk-usage trigger, so that axis is correctly silent, not inactive). The drafting-time claim ("both rotation axes disabled, deletes nothing") missed this separate purge path entirely — this ADR's own Falsification section was itself falsified by a closer look, before ever reaching PROPOSED→accepted.

The project-wide storage cap (#4478) covers `media/` + `memory/history-content/` together and neither of these — `events/`'s own bound comes from #4479, a different mechanism, not #4478. `CostConfig` bounds compute, not a loop that writes without calling an LLM.

**This does not settle the decision.** Whether the #5759 GC's own narrower bound on `history.jsonl` is itself sufficient to satisfy this ADR's "does not discard the user's own data" line remains open and **is the sole precondition for raising this ADR out of PROPOSED** — not decided here.

## References

- [#5747](https://github.com/tya5/reyn/issues/5747) — the issue this came from; the recursion discussion there is **materials, not decisions**.
- #5561 — the abolished loop valve. `docs/concepts/runtime/hooks.ja.md` § ループバルブ carries the owner quote and the successor mechanisms.
- #5516 — N-into-one push folding. #5210/#5244 — per-push size caps.
- [ADR-0044](0044-overflow-recovery-ladder.md) — the in-tree precedent for terminating on a **decreasing measure** rather than an iteration cap; the same preference, one layer down.
- `docs/concepts/architecture/charter.md` — the band whose members do the resource bounding.
