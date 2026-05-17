# H3 (answered) race ablation

Generated: 2026-05-17
HEAD at run time: 2785de0 (feat/fp-0034-phase1-universal-catalog)
Baseline: B32 findings (`docs/deep-dives/journal/dogfood/2026-05-17-batch-32-b30-fix-verify/`)

---

## Patch summary

**File**: `src/reyn/chat/router_loop.py`

**Location**: after the `routing_decided` P6 event block (~line 901), before message
accumulation.

**Mechanism**: After `invoke_skill` or `invoke_action` returns a spawn-ack
(`{status: "spawned", ...}` in the inner `data` field of dispatch_tool's envelope),
exit the router loop immediately instead of continuing to the next LLM call. The
`skill_completed` inbox message (emitted by `_run_one_skill` when the skill finishes)
re-engages the router via `_handle_skill_completed`, which injects a `[task_completed]`
user message with the actual skill output. This prevents the `(answered)` workaround
in `llm.py` from triggering a premature LLM reply based only on the spawn-ack.

**Key discovery during patch development**: Three iterations were needed:
1. v1: checked `tc["function"]["name"] == "invoke_skill"` — wrong; FP-0034 routes via
   `invoke_action`, not `invoke_skill` directly.
2. v2: checked `r.get("status") == "spawned"` — wrong; `dispatch_tool` wraps the
   invoker result in `{"status": "ok", "data": <inner>}`. The spawn-ack is nested at
   `r["data"]["status"] == "spawned"`.
3. v3 (final): checks both tool names and both envelope shapes; positioned AFTER
   `routing_decided` so P6 audit fires on the early-exit path.

**Diff**: `/tmp/reyn-ablation/H3-async-race/patch.diff` (56 lines total, 47 added)

---

## Architecture clarification

The `(answered)` string in `src/reyn/llm/llm.py` (line 821) is appended when the
LAST message in the LLM conversation is `role=tool`. This fires on the NEXT router
loop iteration after a tool result is accumulated via `messages.append({"role":
"tool", ...})`. The race is:

1. `invoke_action(skill__read_local_files)` → spawn-ack (`status: "spawned"`)
2. Router loop accumulates: assistant tool_call message + tool result message
3. Loop continues → next LLM call sees `role=tool` last → `(answered)` appended
4. LLM generates a reply using only the spawn-ack context (skill not yet done)
5. Premature reply sent to user before skill output is available

The patch exits at step 2 (before step 3), preventing step 4.

---

## Pre-conclusion 5Q checklist

1. **Observations**: (a) B32 §4.2 W3 S1 `file_read_via_chat` directly attributes the
   failure to "race condition in router quiescence detection" — primary data from B32
   findings. (b) Patch fires `invoke_skill_spawn_ack_exit` in 3/3 runs where LLM chose
   skill spawn path (events log). (c) Patched runs: router waits for
   `skill_completion_injected` before producing narration. (d) W2 S4/S7/S9-T2 and W6
   s-fp12-completion-2 are different failure classes (stdin-close CancelledError and
   spawn-ack hallucination respectively) — not affected by this patch.

2. **Primary vs inference**: Patch behavior = primary data (events log verified).
   B32 baseline verdict = primary (B32 findings doc). "Patched version would flip S1
   to verified" = inference — depends on (a) LLM choosing skill spawn path and
   (b) underlying infrastructure (MCP) being available.

3. **Falsification**: In 4/8 patched S1 runs, LLM chose direct `file__read` instead
   of skill spawn. The patch correctly did NOT fire on those runs. When LLM chose skill
   spawn, patch fired and skill failed due to MCP infrastructure gap (not the race).
   This means the patch is correct but S1 verdict under patch depends on infrastructure.

4. **Observation infra**: Events log captures `invoke_skill_spawn_ack_exit`,
   `routing_decided`, `skill_run_spawned/completed/failed`, `skill_completion_injected`.
   The absence of a premature `agent>` reply before `skill_run_completed` is the
   behavioral signal.

5. **N/N**: Directly inspected 8 patched runs. 4/8 showed spawn path (patch fired);
   4/8 showed direct file read (patch not needed). No extrapolation from 1-2.

---

## Per-scenario before/after

| Scenario | B32 verdict | Race symptom observed? | Patched behavior | Patch verdict |
|---|---|---|---|---|
| W3 S1 `file_read_via_chat` | REFUTED | YES — §4.2 "replied before skill output; hallucinated generic content" | Patch fires on spawn path; router waits for skill_completed narration | INCONCLUSIVE (MCP infra gap blocks full verification; race itself eliminated) |
| W2 S4 `skill_builder_web_summariser` | INCONCLUSIVE | NO — stdin-close CancelledError (issue #52) | Not applicable | N/A |
| W2 S7 `eval_run_direct_llm` | INCONCLUSIVE | NO — stdin-close CancelledError | Not applicable | N/A |
| W2 S9 `chained_find_then_index` T2 | REFUTED | NO — stdin-close CancelledError | Not applicable | N/A |
| W6 `s-fp12-completion-2-error-narrate` | REFUTED | NO — §4.3 LLM hallucinated spawn-ack language with zero tool calls | Not applicable | N/A |

---

## Quantitative

- N scenarios attributed to `(answered)` race in B32 primary data: **1** (W3 S1)
- N scenarios with other async failure classes (stdin-close #52, §4.3 hallucination): **4**
- N patched runs directly observing `invoke_skill_spawn_ack_exit` fired: **3/3** when spawn path taken
- N flipped to verified: **0** (infrastructure gap prevents full verification)
- N flipped to inconclusive: **hypothesized 1** (W3 S1 — race eliminated; full flip blocked by MCP)
- **Conclusion: not-race-bound at batch scale** (K/N = hypothesized 1/1 for the one
  directly attributed scenario, but K/total_refuted = 1/22 = 0.05 = << 0.2)

The H3 hypothesis is **partially confirmed** for the one scenario where the race was
directly attributed (W3 S1), but the "non-trivial fraction of refuted scenarios" claim
is **not supported** — only 1/22 refuted scenarios in B32 are attributable to this race.
The dominant refusal causes are: routing misses (file__grep absence), infrastructure gaps
(MCP/unsafe-python), and unrelated LLM behavior (double-dispatch, plan trigger misses).

---

## Patch artifact

- Full diff: `/tmp/reyn-ablation/H3-async-race/patch.diff` (56 lines)
- Modified file: `/tmp/reyn-ablation/H3-async-race/src/reyn/chat/router_loop.py`
- The patch is ONLY in the ablation worktree — not committed, not pushed to main.

---

## Event evidence

Patched run showing correct quiescence-wait behavior:

```
chat_started
user_message_received
tool_called                          ← invoke_action(skill__read_local_files)
skill_run_spawned                    ← skill starts async
tool_returned                        ← spawn-ack {status: "spawned"}
routing_decided action=skill__read_local_files  ← P6 audit fires
invoke_skill_spawn_ack_exit          ← H3 patch fires; loop exits
compaction_check
skill_run_failed/completed           ← skill reaches terminal state
skill_completion_injected            ← router re-engages with real result
chat_turn_completed_inline           ← narration turn completes
chat_stopped
```

Unpatched (reconstructed from B32 W3 S1 events): the `invoke_skill_spawn_ack_exit`
event is absent; `tool_returned` is followed by a subsequent LLM call (with `(answered)`
injected), producing a premature reply before `skill_run_completed`.
