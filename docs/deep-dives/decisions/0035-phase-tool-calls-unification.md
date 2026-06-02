# ADR-0035: Phase op-execution via native tool_calls (Phase ↔ chat/planner unification)

**Status**: Proposed (2026-06-02) — design seed for issue #1212, PoC-validated.
Implementation wave is **user-gated** (separate GO); this ADR + PoC + the PR-split
plan are for review only.
**Track**: #1212 — unify the op-invocation format across Phase (skill side) and
chat/planner (plan side) so a working plan promotes to a skill 1:1.
**Input**: the canonical design comments D1–D8 on #1212 (user + lead-coder,
2026-06-02). This ADR formalizes those decisions; the issue comments remain the
discussion record.

## Context

Two op-invocation surfaces have diverged since reyn's early design:

| axis | Phase (skill side) | chat / planner (plan side) |
|------|--------------------|----------------------------|
| call | `call_llm` with `tools=None, tool_choice=None`, `response_format={json_object}` (`llm/llm.py:940-941,958`) — **json-mode** | `call_llm_tools` — **native function-calling** |
| envelope | one structured JSON `{control, artifact, control_ir:[]}`; ops are a JSON field | OpenAI `tool_calls:[{name, arguments(JSON string)}]` |
| op shape | `ControlIROp` (also used by preprocessor/postprocessor `RunOpStep.op`, `schemas/models.py:67,87-88`) | `{name, arguments}` |
| allowed_ops | **kind** granularity (`file`) | tool-name granularity (`file__read`) |

The **goal** is not uniformity for its own sake: it is to make *"a plan that worked
→ a skill"* a subset-copy, not a rework (`plan steps → phase instructions +
allowed_ops`). The op-invocation format is the load-bearing precondition.

Key enabling insight (primary evidence): the op **executor is already shared** —
LLM-emitted control_ir, preprocessor, and postprocessor all bottom out at the same
`control_ir_executor` dispatch, with permission enforcement and per-op events
already there. So unification is an *emission/offer* change, not an executor rewrite.

## Decision (D1–D8)

**D1 — Scope: op execution only.** Make the **op-execution** part native tools.
The **transition mechanism (`control` + `artifact`) stays structured output** — it
is the load-bearing skill/phase contract (P1–P8). Provider-agnosticism is dropped
for this surface (chat/planner already dropped it; it is not a system invariant —
explicit user call).

**D2 — Two mechanisms, temporally separated** (resolves finish_reason exclusivity):
```
op-loop call (tools=):  stop_reason=tool_use  → execute op → feed result → loop
                        stop_reason=end_turn   → ops done
then transition call (response_format, no tools) → {control, artifact}  (structured)
```

**D2-impl — op results are FRAME-fed, not native tool-role-threaded** (intentional,
PR2 `kernel/phase_executor.py:_run_op_loop`). Each op turn rebuilds the phase frame
with the accumulated `control_ir_results` and issues an **independent** `call_tools`
([system, user(frame)] + tools) — exactly like the json-mode `_run_act_loop` rebuilds
the frame each turn. Op results are NOT appended back as native
`{role:assistant, tool_calls}` + `{role:tool, ...}` messages. Trade-off (deliberate):
- **(+)** reuses the json-mode frame builder (no drift) / each call is self-contained
  (no dangling-tool_call API hazard) / **simplifies PR5 replay** — no provider-specific
  native tool-message is persisted, so the op-loop replays exactly like json-mode frame
  replay (see Open items: the D8b provider-id-normalization concern is **moot**).
- **(−)** the model loses *native* tool-call continuity across turns; it sees prior
  results as frame context rather than its own tool-message history. Mitigated by the
  `control_ir_results` in every frame; the residual behavioral risk (real model redoes
  an op / stalls instead of progressing op-by-op) is settled by **動作確認** (real-model
  op-by-op progression), not the scripted Tier-2/3 plumbing tests.

**D3 — Trigger = `stop_reason`** (weak/strong, provider-common). reyn's llm layer
already normalizes tool-extraction vs content (`llm/llm.py:741`); the transition
call's schema enforcement suppresses the empty-stop attractor (`planner.py:101`).

**D4 / D5 — SUPERSEDED by D2 (separate-decide). Pruned (#1226, user GO (b)).**
These two decisions assumed a *combined* op-loop call — passing `tools=` and
`response_format=` together so one call could emit a tool_call OR a structured
decide — which on a combine-incapable model (flash-lite) 400s and needs a degrade
(D4: drop `response_format`, retry tools-only) plus a per-model capability cache
(D5: remember the rejection, skip the doomed first attempt).

**But the implemented op-loop is D2 separate-decide**: op-turns are **tools-only**
(no `response_format`) and a **separate** json-mode call does the transition. It
therefore **never combines** `tools`+`response_format`, so the D4 combined-400 never
fires and the D5 cache is never populated. 動作確認 on real flash-lite (#1226) +
a code/grep audit confirmed: the sole `call_llm_tools` caller (`call_tools`) passes
no `response_format`; **no caller anywhere combines.** D4's degrade machinery + D5's
cache were thus built-but-unreachable and an internal contradiction with D2 — so they
were **pruned**: `capability_cache.py` removed, the `recorded_acompletion` cache
wiring removed (the pre-#1212 json-mode `response_format` fallback is **retained** —
it predates D5 and is still used by `call_llm`), and the `response_format` param
dropped from `call_llm_tools` / `call_tools`.

The combine investigation was still valuable: it *established* that flash-lite cannot
combine, which is exactly why D2 chose the robust separate-decide shape. The original
D4/D5 PoC + cache (PR1 #1219 / PR2) are retained only in git history.

**D6 — op shape unification.** Align LLM-emitted ops *and* the preprocessor/
postprocessor **literal real-op** to the `{name, arguments}` tool_call shape via a
deterministic `universal_dispatch` codemod (the 11 skill files).
**Correction (user, 2026-06-02):** only **real ops** are in scope — the
preprocessor/postprocessor non-tool DSL steps (`iterate`/`validate`/`python`/
`lint_plan`) are **out of scope and unchanged** (they are OS-deterministic, never
LLM-emitted).

**D7 — allowed_ops kind→tool-name granularity** (matches planner/chat). e.g.
`file` → `file__read, file__write, file__edit, file__delete, file__glob, file__grep`.
- `compiler/linter.py:_lint_allowed_ops` validates against the **universal-catalog
  tool names** instead of `ALL_OP_KINDS`.
- 36 phases migrate; **default migration = kind → sub-tool expansion**
  (behavior-preserving).
- Per-phase tightening (only the tools a phase actually uses = the P4-precision win,
  e.g. "read but not delete") is a **follow-up**, kept out of the behavior-preserving
  wave.
- Benefits: promotion becomes a subset copy (granularity matches across surfaces);
  P4 precision (per-tool, not per-kind).

**D8 — blast radius: permission + WAL/event = extension of existing mechanisms, not
from-scratch** (recon, primary evidence):
- **Permission (enforcement unchanged).** The check is at the **shared executor's
  op-execution time** (`kernel/control_ir_executor.py:331` passes `permission_resolver=self._perm`
  to dispatch) — execution layer, not emission. `_build_phase_tool_catalog(allowed_ops)`
  (`:42`) already builds a phase tool catalog from allowed_ops = a native-tools
  precursor. File permission is read/write-class (`permissions.py:9-10`), and D7's
  tool-name allowed_ops map cleanly (`file__read`→`file.read`, `file__edit`→`file.write`).
  *ADR work = wire the **offer layer** (candidates passed as `tools=`) to filter on
  `allowed_ops ∩ permission-granted`; the enforce layer is untouched.*
- **WAL / event (P6 invariant).** op-execution events are already **per-op**
  (`kernel/control_ir_executor.py:418` `tool_executed`); WAL/resume is already **per-step**
  (`:128` `dispatch_tool` memoizes `committed_steps`, `ResumePlan`, `:503`
  `op_invocation_id` scopes WAL steps phase-relative). The native-tools loop maps onto
  these same primitives (today: one response → control_ir batch → per-op exec; new:
  multiple tool_use turns → per-op exec → transition call). *ADR work = (a) adapt
  resume from "control_ir batch unit" to "tool_call unit" (per-step memoize already
  exists); (b) extend LLMReplay/Tier-3 fixtures to the native tool_call structure
  (provider ids); (c) carry the chat-side round concept (`events/event_schema.py:49`
  `tool_calls_attempted`) to the phase side. The transition stays structured output,
  so core transition replay is unchanged. P6 holds — an event still fires per op.*

## PoC results (de-risk, flash-lite only — approved weak model)

Live calls via the litellm proxy (:4000), `gemini-2.5-flash-lite`:

- **(a) Does the existing fallback catch the combined-mode 400?** YES.
  `tools` + `response_format={json_object}` → `litellm.BadRequestError`:
  *"Function calling with a response mime type: 'application/json' is unsupported"*
  (Gemini 400, wrapped as a Python `Exception`). reyn's broad `except Exception`
  catches it → retry without `response_format` → **the tools-only retry succeeds
  (no error)**. D4 confirmed. The load-bearing fact is precisely that the retry
  does not error — **whether the model emits a `tool_call` vs plain `content` on
  that retry is model-choice and non-deterministic on a weak model** (one run here
  returned `finish_reason=tool_calls`; an independent re-run returned `content`
  with `finish_reason=stop`). Both are fine: the op-loop simply continues on a
  `tool_use` stop and ends on `end_turn`/content. (Per the pre-conclusion
  observation discipline — the flaky tool_call-vs-content outcome is not stated as
  a deterministic criterion.)
- **(b) Does the transition come out valid after degrade?** YES. The transition call
  (json-mode, no tools, control schema) → `finish_reason=stop`, valid
  `{control:{type:transition, decision:continue, next_phase:report, …}, artifact:…}`.
  (This is the existing flash-lite Phase json-mode path — empirically proven by every
  C7 run; the PoC re-confirms it.)

**No design premise broke.** Capable-model combined-mode is confirmed by provider
docs (no PoC needed).

> **Outcome (#1226): combined-mode was NOT shipped.** This PoC established that
> flash-lite *cannot* combine `tools`+`response_format` — which is precisely why the
> implemented op-loop chose **D2 separate-decide** (tools-only op-turns + a separate
> json transition) over a combined call. Since the shipped design never combines, the
> D4 degrade + D5 cache were superseded and **pruned** (see D4/D5 above). The
> investigation remains valuable as the evidence that motivated separate-decide.

## Invariants preserved

- **P1/P4/P8**: transitions remain externally-determined structured output; the LLM
  still picks only from OS-offered candidates (op tools = `allowed_ops ∩ permission`;
  transition = candidate schema). Phase still declares no next phase.
- **P6**: per-op events + per-step WAL preserved (D8).
- **P7**: the executor/permission/event layers stay skill-agnostic; the change is the
  emission/offer format + the catalog granularity.

## Migration — proposed PR split (dependency order; each behavior-preserving)

1. **PR1 — per-model capability cache (D5).** Landed (#1219), then **pruned (#1226)**
   — superseded by D2 separate-decide (the op-loop never combines, so the cache was
   never populated). See D4/D5 above.
2. **PR2 — native-tools op-loop Phase mechanism (D1–D4).** The `stop_reason` loop +
   the transition call; the op-turns are **tools-only** (D2 separate-decide), so the
   D4 combined-fallback wiring it shipped was **pruned (#1226)** along with PR1's
   cache. Coexists with the json-mode path (incremental, gated, not big-bang).
3. **PR3 — op-shape codemod (D6).** Deterministic `universal_dispatch` rewrite of the
   11 skill files' real ops to `{name, arguments}`; preprocessor/postprocessor DSL
   untouched. Mechanical — Sonnet-suitable, with an AST/round-trip guard test.
4. **PR4 — allowed_ops op-native file verb granularity (D7).** Shipped scope =
   the **mechanism only**: `file` (the lone op kind with a real tool-verb axis)
   gains `file__<verb>` granularity — gating (`is_op_instance_allowed`, op-aware),
   catalog (`file__verb` drops the implied `op`), conversion, and the linter
   target swap (`ALL_TOOL_NAMES`). Coarse `file` stays behavior-preserving (all
   verbs); all other kinds are single-verb (no-op); the chat-router taxonomy is
   NOT adopted (decision A). **Deferred follow-ups** (#1212 rationale, tracked
   pre-close): (a) **offer-layer `allowed_ops ∩ permission` filter (D8)** — there
   is no clean kind-level permission-granted set at offer time (grants are
   per-key, arg-dependent, interactive), and the enforce layer already gates, so
   the offer-filter is marginal; (b) **per-phase frontmatter expansion** — under
   the D7 mechanism a coarse `file` is identical to the all-verbs expansion
   (cosmetic), and the only meaningful change is narrowing, which is
   non-behavior-preserving = the deliberate per-phase tightening (= the
   P4-precision win) phases opt into later.
5. **PR5 + enablement — op-loop resume semantics: decision (A), deterministic
   replay (#1225).** `call_tools` is memoized parallel to the json-mode `call`
   (per-phase `op_invocation_id` + `args_hash` + per-step WAL), so on crash-resume
   the act turn **replays deterministically**: `call_tools` memo-hits (not
   re-decided) → the recorded tool_calls → the same op → `dispatch_tool` also
   memo-hits → no side-effecting op re-executes = **json-mode-equal crash recovery**.
   No new replay fixtures: the op-loop is **frame-fed** (D2-impl — provider
   tool_call-id normalization is moot, replays like json-mode frame replay). Pinned
   by `tests/test_op_loop_resume_memo_1212.py`. (PR5 originally shipped the weaker
   (B) re-decide-with-caveat under a HARD GATE; the enablement work then landed (A),
   resolving the gate — see Open items.)

## Open items / risks

- **D5 cache shape**: per-process vs persisted; invalidation on provider/model change.
- **D6 codemod**: the `universal_dispatch` map must be total over the 11 skills' real
  ops; pin with a coverage test before the rewrite.
- **D7 follow-up tightening**: where to draw "actually-used tools" per phase (needs a
  usage scan), explicitly deferred so the behavior-preserving wave stays mechanical.
- **Replay (D8b) — MOOT under the frame-fed op-loop (D2-impl).** This concern
  assumed native tool-role messages would be threaded back into the conversation (so
  their provider-specific `tool_call` ids would need normalizing at the replay
  boundary). PR2 feeds op results via the rebuilt frame's `control_ir_results`
  instead, so **no native tool-message is persisted** — the op-loop replays exactly
  like json-mode frame replay, with no provider-id normalization needed. Retained
  here only as the rationale trail; PR5 replay fixtures track the json-mode frame
  shape, not a tool_call structure.
- **Op-loop act-turn memo / resume — RESOLVED (A), enablement (#1225).** `call_tools`
  is memoized parallel to the json-mode `call` (per-phase `op_invocation_id` +
  `args_hash` + per-step WAL, in `LLMCallRecorder.call_tools`). On crash-resume the act
  turn **replays deterministically**: `call_tools` memo-HITS (not re-decided) → the
  recorded tool_calls → the same op → `dispatch_tool`'s op+args memo
  (`control_ir_executor.py:128`) also HITS → no side-effecting op re-executes =
  **json-mode-equal crash recovery**. Pinned by `tests/test_op_loop_resume_memo_1212.py`.

  History: PR5 originally shipped the weaker **(B)** (accept re-decide-on-resume +
  document the divergent-re-decide caveat) under a **HARD GATE** — because the op-loop
  was then not production-reachable, so (A) was YAGNI. The production-enablement work
  (user GO, no-deferral) then threaded the gate AND landed (A), so the HARD GATE is
  **resolved**: a divergent re-decide can no longer re-run a side-effecting op (the act
  turn is replayed, not re-decided). Un-opted skills are unaffected (json-mode
  unchanged).
