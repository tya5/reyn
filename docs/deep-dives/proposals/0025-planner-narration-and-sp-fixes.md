# FP-0025: Planner — Router Narration + Plan Step SP Fixes

**Status**: proposed
**Proposed**: 2026-05-14
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

Align plan completion narration with the skill narration pattern established
by FP-0012: instead of the terminal plan step emitting text directly to the
user, enqueue a `plan_completed` inbox message and run one router LLM turn
for narration. Also fixes three independent issues in `build_plan_step_system_prompt`:
`output_language` not forwarded, step id leaked into the prompt, and a
missing plan-usage Behaviour rule in the router SP.

---

## Motivation

### Current plan completion path

```
plan tool called
  → dispatch_plan_tool → {"status": "spawned"}
  → router LLM produces spawn-ack (1 sentence)

[background]
  → PlanRuntime.run() → execute_plan()
  → terminal step LLM produces user-facing text   ← synthesis burden on step LLM
  → spawn_plan_task: _put_outbox(kind="agent", text=result_text)  ← directly to user
```

The terminal step LLM carries the full synthesis responsibility and its output
reaches the user **without any router review**. The router never sees the plan
result, so it cannot verify coherence with the original query or apply
output-language preferences.

### Skill narration (FP-0012, already landed)

```
invoke_skill called
  → {"status": "spawned"}
  → router LLM produces spawn-ack

[background]
  → OSRuntime.run()
  → _enqueue_skill_completed → inbox "skill_completed"
  → session.run() loop: _handle_skill_completed
      injects [task_completed] user-role message into history
      runs one router LLM turn
  → router LLM narrates → user sees narration
```

The plan path is asymmetric with skill. Aligning them gives the router the
same synthesis and language-correction opportunity for both.

### Additional issues in `build_plan_step_system_prompt`

**Issue 1 — `output_language` not forwarded**
`_PlanStepHost.output_language` inherits from the parent host but
`build_plan_step_system_prompt` has no `output_language` parameter.
JA users get EN step replies.

**Issue 2 — step id leaked into prompt**
```python
parts.append(f"## This step (id={step.id})\n{step.description}")
```
Internal step ids (`"s1"`, `"s2"`) appear in the LLM context and can
surface in step replies, which then propagate into `prior_results` and
potentially into the narration turn.

**Issue 3 — plan usage guidance absent from router SP Behaviour**
The `plan` tool relies solely on its schema description to guide when the
LLM should or should not decompose. No Behaviour rule in the router SP
reinforces or constrains this, unlike `invoke_skill` and `delegate_to_agent`.

---

## Proposed implementation

### Component A — `output_language` forwarding (SMALL)

**`src/reyn/chat/planner.py`** — `build_plan_step_system_prompt` signature:

```python
def build_plan_step_system_prompt(
    plan: Plan,
    step: PlanStep,
    prior_results: dict[str, str],
    *,
    output_language: str | None = None,   # NEW
) -> str:
    parts: list[str] = []
    if output_language:
        parts.append(f"Respond in {output_language}.")
        parts.append("")
    parts.append(
        "You are a Reyn agent executing one step of a multi-step plan. ..."
    )
    ...
```

Caller site (inside `execute_plan`):

```python
sys_prompt = build_plan_step_system_prompt(
    plan, step, step_results,
    output_language=narrow_host.output_language,
)
```

### Component B — step id removal (SMALL)

**`src/reyn/chat/planner.py`** — change the step header:

```python
# Before
parts.append(f"## This step (id={step.id})\n{step.description}")

# After
parts.append(f"## Your task\n{step.description}")
```

The step id is visible in events for audit (P6) and in plan validation
errors; it does not need to appear in the LLM context.

Also simplify the output guidance — with router narration (Component C)
the step LLM no longer needs to produce user-quality prose:

```python
# Before
"emit a concise text reply (100-400 chars) summarising what "
"this step contributes to the plan goal."

# After
"Summarise what this step found in 1–3 sentences. "
"Be factual; a separate synthesis step will produce the user reply."
```

### Component C — Router narration (= same form as skill, SMALL)

This mirrors the FP-0012 pattern exactly.

#### C.1 — `_enqueue_plan_completed` (new, session.py)

```python
async def _enqueue_plan_completed(
    self,
    *,
    plan_id: str,
    chain_id: str,
    goal: str,
    step_results: dict[str, str],
    n_steps: int,
) -> None:
    """FP-0025: enqueue plan_completed inbox message for router narration."""
    try:
        await self._put_inbox(
            "plan_completed",
            {
                "plan_id": plan_id,
                "chain_id": chain_id,
                "goal": goal,
                "step_results": step_results,
                "n_steps": n_steps,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("_enqueue_plan_completed failed for %s: %r", plan_id, exc)
```

#### C.2 — `_handle_plan_completed` (new, session.py)

```python
async def _handle_plan_completed(self, payload: dict) -> None:
    """FP-0025: narrate plan completion via one router LLM turn.

    Symmetric with _handle_skill_completed (FP-0012). Injects a
    [plan_completed] user-role message into history so the router
    LLM sees the step_results and synthesises a user reply.
    """
    plan_id = payload.get("plan_id", "")
    chain_id = payload.get("chain_id") or _new_chain_id()
    goal = payload.get("goal", "")
    step_results = payload.get("step_results") or {}
    try:
        results_str = json.dumps(step_results, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        results_str = repr(step_results)
    injected_text = (
        f"[plan_completed] plan_id={plan_id}\n"
        f"goal: {goal}\n"
        f"step_results:\n{results_str}\n\n"
        "Please synthesize the step results into a complete response for the user."
    )
    self._append_history(ChatMessage(
        role="user", text=injected_text, ts=_now_iso(),
        meta={
            "source": "plan_completion",
            "plan_id": plan_id,
            "chain_id": chain_id,
        },
    ))
    self._chat_events.emit(
        "plan_completion_injected",
        plan_id=plan_id, chain_id=chain_id,
    )
    # Run one router LLM turn — same pattern as _handle_skill_completed.
    await self._run_router_turn(chain_id=chain_id)
```

#### C.3 — `spawn_plan_task` change (session.py)

```python
# Before
if clean_exit and result_text:
    await self._put_outbox(OutboxMessage(
        kind="agent", text=result_text,
        meta={"plan_id": plan_id, "source": "plan", ...},
    ))

# After
if clean_exit and result is not None:
    await self._enqueue_plan_completed(
        plan_id=plan_id, chain_id=chain_id,
        goal=result.plan_goal,      # expose plan.goal via PlanExecutionResult
        step_results=result.step_results,
        n_steps=result.n_steps,
    )
```

`PlanExecutionResult` gains `plan_goal: str` and `n_steps: int` fields
(both available at `execute_plan` call site).

#### C.4 — Session loop registration

```python
# session.py run() main loop — add alongside skill_completed
elif kind == "plan_completed":
    await self._handle_plan_completed(payload)
```

#### C.5 — Plan tool description update

Remove "design the last step to synthesise" guidance from `tools/plan.py`
and the fallback literal in `router_tools.py`:

```python
# Before
"The terminal step's text reply becomes the user-facing answer; "
"design the last step to synthesise."

# After
"Each step summarises what it found; the router synthesises the "
"final reply after all steps complete."
```

Also update the `steps_json` description: remove the "Use [] for steps
that just synthesise" guidance — every step now does focused work.

### Component D — Router SP plan usage Behaviour rule (SMALL)

**`src/reyn/chat/router_system_prompt.py`** — add to Behaviour section
after the `invoke_skill` / `delegate_to_agent` rules:

```markdown
## Plan decomposition

Use the `plan` tool when the query requires combining information from
multiple independent sources (e.g. "compare A and B from two docs",
"explain X with code references from N files", "summarise across these
sources"). Each step should gather one piece of information; the OS
synthesises the final reply.

Do NOT use `plan` for:
  - Single-tool retrievals or single-source narrations
  - Chitchat or conversational replies
  - Queries that invoke_skill handles end-to-end
  - Queries answerable in one router reply without tools
```

---

## Target files

| File | Change |
|---|---|
| `src/reyn/chat/planner.py` | A: `output_language` param; B: step id → "Your task", output guidance |
| `src/reyn/chat/session.py` | C: `_enqueue_plan_completed`, `_handle_plan_completed`, `spawn_plan_task` update, run() loop |
| `src/reyn/chat/router_system_prompt.py` | D: plan usage Behaviour rule |
| `src/reyn/tools/plan.py` | C: description — remove terminal-step-as-synthesiser guidance |
| `src/reyn/chat/router_tools.py` | C: fallback literal description update |

---

## Dependencies

- Component C depends on FP-0012 (already landed): `_run_router_turn`,
  `_put_inbox`, `_append_history` patterns are all in place.
- Components A, B, D are independent; can ship in any order.
- Component C can ship before or after A/B; each improves a different axis.

---

## Cost estimate

| Component | Task | Cost |
|---|---|---|
| A | `output_language` param + caller site | SMALL |
| B | step id removal + output guidance update | SMALL |
| C | `_enqueue_plan_completed` + `_handle_plan_completed` + `spawn_plan_task` + loop + description | SMALL |
| D | Plan usage Behaviour rule in router SP | SMALL |
| **Total** | | **SMALL** |

Component C is SMALL (not MEDIUM) because the FP-0012 pattern is already
established and battle-tested — this is a structural copy, not a design
invention.

---

## Verification

1. **Component A**: Set `output_language: Japanese`. Run a plan-mode query.
   Confirm each step reply is in JA (not EN).
2. **Component B**: In `dogfood_trace --mode plan-trace`, confirm step ids
   (`s1`, `s2`) do not appear in step captured text.
3. **Component C**: Run a 3-step plan query. Confirm:
   - `plan_completion_injected` event appears in events log
   - Router produces a synthesised reply (not the raw terminal step text)
   - `plan_completed` inbox message carries `step_results` dict
   - History contains `[plan_completed]` user-role message
4. **Component D**: Run a single-tool query — confirm router does NOT call
   `plan`. Run a multi-source synthesis query — confirm router calls `plan`.

---

## Related

- FP-0012 (`0012-async-skill-execution.md`) — skill narration pattern this
  mirrors (already landed, commit `c9e79d6`)
- FP-0011 (`0011-remove-narrator.md`) — router narration direction
- FP-0023 (`0023-router-sp-quick-wins.md`) — companion router SP fixes
- `src/reyn/chat/session.py` — `_handle_skill_completed`, `_enqueue_skill_completed`
- `src/reyn/chat/planner.py` — `build_plan_step_system_prompt`, `execute_plan`
