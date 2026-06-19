# FP-0045: Session C6 — first method-cluster seam (owner-gated)

**Status**: proposed (design-review only — no cut)
**Proposed**: 2026-06-19
**Author**: e2e-coder session (#1792 / FP-0044 C-series)
**Gate**: owner-gated. This doc exists to choose the first `Session`
method-cluster cut **before** any extraction. Lead + owner review → then a
PR. No code is cut by this doc.

## Context

FP-0044 Stage-1 (C1–C5, all merged) extracted the 6 non-`Session` classes
out of `session.py` (5214 → 4825 LOC). The remaining work — FP-0044 §(d) /
FP-0043 continuation — is slimming the `Session` **god-class** (149 methods,
~4060 LOC) into a **coordinator** that holds collaborators, by extracting its
method clusters. FP-0044 named the candidate clusters: history/context
assembly, intervention coordination, persistence/journal, turn dispatch, with
lifecycle staying as the coordinator core.

This is the highest-care stage (FP-0044: "where wrong cuts cost the most"),
so we pick the **first** cut deliberately.

## Primary-data finding: a forwarding-residue layer already exists

> **Line numbers below are as-of `origin/main` @ `0dcf1823` (session.py 4825
> LOC).** The stable anchors are the **method / section names** (all verified
> present by a docs-maintainer drift-audit). `session.py` moves actively across
> the C-series, so the bare line numbers WILL drift once the C6 cut lands —
> read them as "where it was when this doc was written"; the names are
> authoritative.

Surveying `Session`'s 149 methods (body sizes + targets, then reading the
block directly), the contiguous block **L4688–4825** is almost entirely
**thin-forward shims to collaborators that already exist** — each docstring
names its target and the FP-0043 PR that created it:

| Session forwarder (LOC) | Forwards to (existing collaborator) | FP-0043 PR |
|---|---|---|
| `_build_history_for_router` (4) | `RouterHistoryBuffer.build_history` | PR-2 |
| `_decompose_history_for_retry` (6) | `RouterHistoryBuffer.decompose_history_for_retry` | PR-2 |
| `_build_router_system_prompt` (4) | `RouterHistoryBuffer.build_system_prompt` | PR-2 |
| `_cap_tool_result` (4) | `ContextBudgetAdvisor.cap_tool_result` | PR-1 |
| `_per_turn_cap_tokens` (4) | `ContextBudgetAdvisor.per_turn_cap_tokens` | PR-1 |
| `_media_followup_budget` (4) | `ContextBudgetAdvisor.media_followup_budget` | PR-1 |
| `_free_window_now` (7) | `ContextBudgetAdvisor._free_window_now` | PR-1 |
| `_context_window_status` (4) | `ContextBudgetAdvisor.context_window_status` | PR-1 |
| `_maybe_force_compact_for_router` (5) | `ContextBudgetAdvisor.maybe_force_compact` | PR-1 |
| `_router_run_with_shrink` (4) | `RouterLoopDriver._run_with_shrink` | PR-3 |
| `_force_close_handoff` (4) | `RouterLoopDriver._force_close_handoff` | PR-3 |
| `_force_close_wrap_up` (4) | `RouterLoopDriver._force_close_wrap_up` | PR-3 |

The collaborators (`self._budget_advisor`, `self._history_buffer`,
`self._loop_driver`) already hold the logic. FP-0043 PR-1/2/3 slimmed
`Session` by moving the bodies out, **but left the forwarding methods on
`Session`** — and, critically, kept `Session` as the **wiring middleman**:
several forwarders are injected as **callbacks** into other collaborators
rather than the collaborators being wired to each other directly. Confirmed
at the `router_host_adapter` construction site (`session.py:1492–1507`):

```python
cap_tool_result=self._cap_tool_result,            # → ContextBudgetAdvisor
media_followup_budget=self._media_followup_budget, # → ContextBudgetAdvisor
context_window_status=self._context_window_status, # → ContextBudgetAdvisor
compact_now=self._compact_now_for_op,              # real method (see below)
reasoning_continuity_section_fn=self.reasoning_continuity_section,
```

`router_host_adapter` stores each and calls it back (e.g. `:416`,
`:1024`) — so the call path is `host_adapter → Session forwarder →
collaborator`, where `host_adapter → collaborator` would do.

### Not all of the block is a pure forward (exclusions)

Reading the bodies, three methods in the same block are **not** pure
forwards and are **out of scope** for the first cut:

- `_compact_now_for_op` (L4725, 56 LOC) — real chat-axis compression-metric
  logic (#191); it *uses* `_free_window_now`/`_latest_summary`, not a shim.
- `_run_router_loop` (L4801, 14 LOC) — forwards to `RouterLoopDriver.run_turn`
  **and then** does `self._journal.cut_generation(...)` (a turn-boundary
  side-effect). Collapsing it would drop the journal cut — so it stays or is
  split deliberately, not in the mechanical first cut.
- `reasoning_continuity_section` (L4788) — a **retired stub** (always `""`,
  #1652/②); a separate trivial cleanup, not part of this seam.

## Implementation finding (flow-trace before the cut): 9 → 6

A pre-implementation flow-trace refined the count. Of the candidate forwarders,
**6 are genuine residue and collapse cleanly; 3 are not residue and are
retained** (see the construction-cycle note below). The first cut is **6**:

- **4 pure-residue forwarders → rewire callers to the collaborator directly,
  delete the `Session` method**: `_build_history_for_router`,
  `_decompose_history_for_retry`, `_build_router_system_prompt`
  (→ `RouterHistoryBuffer`), `_free_window_now` (→ `ContextBudgetAdvisor`; its
  only caller is the retained `_compact_now_for_op`, rewired to
  `self._budget_advisor._free_window_now()`).
- **2 dead forwarders → delete** (zero live callers, verified):
  `_per_turn_cap_tokens`, `_maybe_force_compact_for_router`.

### Retained: 3 cycle-bound late-binding shims (NOT residue)

`_cap_tool_result`, `_media_followup_budget`, `_context_window_status` are
injected as **callbacks into `RouterHostAdapter` at its construction**
(`session.py:1403`), which runs **before** `ContextBudgetAdvisor` is built
(`:1658`). And `RouterHistoryBuffer` (`:1557`, which the advisor's `history_fn`
depends on) takes `router_host=self._router_host` — so there is a real
construction cycle: `host_adapter → (callback) budget_advisor →
history_buffer → host_adapter`. Injecting `self._budget_advisor.<m>` at
`:1498/1501/1507` would `AttributeError` (advisor not built yet). These three
are therefore **legitimate late-binding wiring, not forwarding residue** — they
exist precisely to bridge the construction-order gap, and are kept.

- A lambda-wrap at the injection site was **rejected**: it removes the named
  method but keeps the same runtime hop (cosmetic surface change, worse
  readability).
- Truly collapsing these three requires **breaking the construction cycle**
  (e.g. two-phase init: build `host_adapter` with placeholder callbacks, build
  the collaborators, then set the callbacks). That is a construction-order
  refactor — a *different, higher-care* change than residue-collapse, and is
  **owner-gated**; deferred to a possible follow-up cut, not done here.

## Recommended first cut

**Collapse the 6 ContextBudgetAdvisor + RouterHistoryBuffer forwarders that are
genuine residue/dead by rewiring their callers — internal calls and the
`system_prompt_provider` injection — to the collaborator directly, then
deleting the `Session` forwarders. The 3 cycle-bound shims are retained.**

Rationale (why this is the cleanest possible *first* cut):

1. **The collaborator already exists** — no new module, no behavior change,
   no new tests to author for moved logic. This is the lowest-risk way to
   establish the C6 "rewire-callers-off-Session" mechanic before the harder,
   genuinely-inline clusters (intervention coordination, the router file/MCP
   op handlers) where logic actually moves.
2. **Dependency direction is already correct** — `Session → advisor/buffer`;
   collapsing only *removes* an indirection hop, it cannot introduce a cycle.
3. **It directly advances the FP-0043/0044 goal** — every removed forwarder
   makes `Session` measurably thinner (≈ -45 LOC + 6 methods off the surface)
   and removes `Session` from a call path it has no reason to be on.
4. **Smallest blast radius** — the callers are the `system_prompt_provider`
   injection into `CompactionEngine`, the retained `_compact_now_for_op`'s
   internal `_free_window_now` call, and test call-sites. No cross-process /
   wire-format surface.

`Session` ends this cut still holding the collaborators (it constructs them),
but no longer *forwarding* to them — it wires `host_adapter` to them directly.

## Dependency direction & "thin forward" after the cut

- Today: `host_adapter ──callback──▶ Session._cap_tool_result ──▶ advisor`
- After: `host_adapter ──callback──▶ advisor.cap_tool_result` (Session drops out)
- `Session` keeps: constructing `advisor`/`buffer`/`loop_driver` (lifecycle —
  the coordinator core that stays per FP-0044).

This matches the existing `services/` delegation pattern: collaborators wired
to each other, `Session` as the lifecycle owner that assembles them — not a
pass-through hub.

## Open questions for lead + owner (the genuine decisions)

1. **Is "collapse the FP-0043 forwarding residue" the intended C6 first cut,
   or do you want the first cut to be a *new-collaborator extraction*** (e.g.
   the still-inline **router file/MCP op handlers** `_file_*` / `_mcp_*`,
   L4509–4688, ~9 methods / ~190 LOC, wired via `_make_router_op_context`)?
   The forwarding-collapse is objectively cleaner/lower-risk and de-risks the
   pattern; the op-handler extraction better exercises the "move real logic to
   a new collaborator" mechanic the *harder* clusters need. I recommend
   forwarding-collapse **first**, op-handlers **second** — but the sequencing
   intent is yours.
2. **Test-surface**: a few forwarders are pinned by tests against the
   `Session` surface (`session._build_history_for_router()` in
   `test_session_router_history_slicing.py`; `session._force_close_wrap_up()`
   in `test_force_close_chat_handoff_1092.py`). On collapse these tests
   repoint to the collaborator. OK to migrate them in the same PR?
3. **`_run_router_loop`'s journal side-effect** — leave the method on Session
   (it's a coordinator concern: drive turn + cut generation), or split the
   journal cut into the driver? I lean: leave it on Session (it is genuinely
   coordinator glue, not a forward).

## Scope guard

This doc proposes **one** seam. It does not touch intervention coordination,
persistence/journal, or `router_loop.py`. Each subsequent cluster gets its own
seam doc + review, per FP-0044 §(d).

## Related

- FP-0044 §(d) (god-file decomposition seams; C-series plan)
- FP-0043 (Session-slim; the PR-1/2/3 that created the collaborators this cut
  finishes wiring)
- #1792 (FP-0044 C-series tracking issue)
