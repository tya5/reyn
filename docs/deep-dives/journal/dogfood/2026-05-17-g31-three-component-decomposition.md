# G31 three-component decomposition deep dive

| Field | Value |
|---|---|
| Date | 2026-05-17 |
| Session | e2e-coder |
| Triggering | giveup-tracker [G31](./giveup-tracker.md#g31) follow-up — "is there a non-overfit structural fix?" |
| Models | gemini-2.5-flash-lite (weak default), gemini-2.5-flash (strong, cost-gated permission granted for this session) |
| Method | trace-driven `--patch` replay, [`scripts/llm_replay.py`](../../../scripts/llm_replay.py), N=10 per cell, classification by regex heuristics |
| Total cost | ~430 weak calls + ~40 strong calls |

## TL;DR

The original G31 entry collapsed three separable defect components into one umbrella `cap-enum` metric. Decomposing into A / B / C and re-evaluating ~10 candidate fixes shows:

- **C (cap-enum umbrella)** = "I can help you with various tasks..." natural-language capability summary. **NOT a defect** — expected agent behaviour, matches what Claude Code / ChatGPT do. **10/10 invariant** across all SP and tool-array changes tested. C is question + model-prior bound and cannot be suppressed without overfitting to question shapes.
- **A (prefix-leak)** = `skill__X`, `file__Y`, `web__Z`, `memory.*` etc. verbatim in user-facing reply. Actual defect.
- **B (router-meta-leak)** = `list_actions`, `describe_action`, `invoke_action` etc. in user-facing reply. Actual defect.

No deployable single-PR structural fix reduces both A and B without offsetting harm. The original G31 policy-accepted decision (no SP rule) is reaffirmed; this deep dive records the negative results so future sessions don't redo the same experiments.

## Background

The 2026-05-17 e2e-coder session investigated 10 candidate fixes for G31. The investigation started from a misframed target (`cap-enum 10/10` as a single defect) and arrived at the three-component decomposition only after exhausting payload-level ablations. The methodology lesson (= decompose attractor signals before A/B) is captured in user memory `[[separate-leak-types-before-measuring]]`.

## Decomposition

| Component | Example output | Defect? | Heuristic |
|---|---|---|---|
| **A. prefix-leak** | "Use `skill__code_review` to review code" | **YES** — exposes implementation-detail vocab to user | `re.search(r"skill__\|file__\|web__\|memory\.\|reyn\.source__\|rag\.\|exec__\|mcp\.", content)` |
| **B. router-meta-leak** | "I can `list_actions`, `describe_action`, then `invoke_action`" | **YES** — catalog wrapper names are router internals, not user concepts | `re.search(r"list[_ ]actions\|invoke[_ ]action\|describe[_ ]action\|...", content)` |
| **C. cap-enum (umbrella)** | "I can help you with: file operations, web access, memory management" | **NO — expected product behaviour** | `re.search(r"i can:\|i can help\|file operations\|...", content)` |

C is what every agent product does when asked "what can you do?" — including Claude Code and ChatGPT. The original G31 framing implicitly treated C as defective, which led to chasing an unfixable target.

## Methodology

### Captured traces (4 scenarios)

| Scenario | User message | Purpose | Trace |
|---|---|---|---|
| **B** | "Summarize this repo" | Regression check (= must keep calling tools) | `/tmp/reg/B.trace.jsonl` req `1b7bb836` |
| **C-en** | "What can you do?" | Primary capability-question target (English) | `/tmp/reg/C-en.trace.jsonl` req `88f1c27c` |
| **G-ja** | "教えて、reyn で何ができる?" | Capability-question target (Japanese, weak-model JA leak) | `/tmp/reg/G-ja.trace.jsonl` req `8d482660` |
| **L** | post-search chain to `web_fetch` | Regression check (= tool-chain still works) | `/tmp/reg/L-fetch.trace.jsonl` req `5d1d956d` |

Traces captured via `REYN_LLM_TRACE_DUMP=/tmp/reg/<scenario>.trace.jsonl reyn chat --cui` (see [`reference_trace_tools.md`](./giveup-tracker.md) and `scripts/dogfood_trace.py`).

### Replay harness

`scripts/llm_replay.py` extended in this session with sed-style substitution `~=s/pat/repl/[gi]` so SP-text or tool-description patches can be expressed without rewriting the whole payload. See `--help` for the new operator.

Each cell: 10 parallel `litellm.acompletion` calls via the local proxy at `localhost:4000`, model override `openai/gemini-2.5-flash-lite` (weak) or `openai/gemini-2.5-flash` (strong). Classification regexes count tool_calls / router-meta / prefix-leak / cap-enum / cyrillic per reply.

### Candidates tested

10 candidates split across two layers:

**SP / tool-array ablations (payload-level)**

| ID | Description |
|---|---|
| baseline | unmodified payload |
| **β** | SP: replace `skill__code_review` → `skill__<entry>` |
| **δ** | SP: append "Always reply in user's last message language; do not switch language mid-reply" |
| **α** | SP: rewrite `For chitchat or self-questions, reply without tools.` to forbid Action-category enumeration |
| **γ** | SP: delete `## Action categories` block |
| **γ'** | γ + delete wrapper enumeration in `## Capabilities (routing guide)` |
| **ε1** | All `tools[i].function.description` set to `""` (diagnostic: descriptions as A+B seed?) |
| **ε2** | Replace `invoke_action` description (2096 chars) with 1-line stub |
| **ε3** | Replace `skill__code_review` in `list_actions` description |
| **ε4** | Production-ready trim: shorten 4 wrapper descriptions + replace `skill__code_review` everywhere |
| **H1** | Rename all tools to verb-form (`file__read` → `read_file`, etc.) |
| **H2** | Delete the 4 universal wrappers from tools[] |
| **H3** | Keep only the 4 wrappers; delete 15 specific tools |

**Chat-layer intercept (= overfit-via-regex, ultimately rejected)**

| ID | Description |
|---|---|
| **ζ1** | Detect capability question by regex; rewrite user message with 2-sentence response constraint |
| **ζ3** | Stricter ζ1 — single-sentence constrained reply |

## Results

### A — prefix-leak (out of 10 on C-en weak)

| Candidate | C-en px | Δ vs baseline | Note |
|---|---|---|---|
| baseline | 7 | — | |
| β (SP identifier replace) | **9** | ↑2 | regression |
| δ (lang anchor) | **5** | ↓2 | minor |
| α (SP no-enum rule) | **0** | ↓7 | but B routing 10→8 (= overfit, rejected) |
| γ | 7 | = | no effect |
| γ' | 6 | ↓1 | minor |
| ε1 (descriptions = "") | **3** | ↓4 | diagnostic extreme, not deployable |
| ε2 | 9 | ↑2 | regression |
| ε3 | 7 | = | no effect |
| ε4 (realistic trim) | 7 | = | **no improvement; B regressed (see below)** |
| H1 (verb-form rename) | **3** | ↓4 | works, but huge blast radius |
| H2 (no wrappers) | **9** | ↑2 | regression |
| H3 (only wrappers) | **2** | ↓5 | but B regressed 4→9 |
| ζ1 | 0 | ↓7 | overfit via regex (rejected) |

### B — router-meta-leak (out of 10 on C-en weak)

| Candidate | C-en rt | Δ vs baseline | Note |
|---|---|---|---|
| baseline | 4 | — | |
| β | 7 | ↑3 | regression |
| δ | 6 | ↑2 | regression |
| α | 0 | ↓4 | routing-cost (rejected) |
| γ | 7 | ↑3 | regression |
| γ' | 6 | ↑2 | regression |
| ε1 | **3** | ↓1 | diagnostic extreme |
| ε2 (invoke_action stub) | **10** | ↑6 | major regression |
| ε3 | 8 | ↑4 | regression |
| **ε4** | **7** | **↑3** | **regression** |
| H1 | 5 | ↑1 | flat |
| H2 (no wrappers) | **1** | ↓3 | works on B but A regressed 7→9 |
| H3 (only wrappers) | 9 | ↑5 | regression |
| ζ1 | 0 | ↓4 | overfit via regex (rejected) |

### C — cap-enum umbrella (out of 10 on C-en weak)

**All 10 hypotheses preserve cap-enum at 10/10.** This is the decisive invariant. The seed is the question + model prior, not the architecture or SP text.

| Candidate | C-en cap |
|---|---|
| baseline | 10 |
| β / δ / γ / γ' / ε1 / ε2 / ε3 / ε4 / H1 / H2 / H3 | 10 |
| α (SP rule) | 7 — but routing cost (= overfit) |
| ζ1 (chat-layer regex) | 0 — but regex itself hardcodes question shapes (= overfit relocated) |

### Regression checks (B / L weak)

All payload-level ablations preserved 10/10 tool_calls on B (first-call routing) and L (web_fetch chain), with one exception:

- **α** on B: 8/10 tool_calls (= ↓2, SP rule leaks to non-capability questions; **disqualifier for α**)

### Weak vs strong comparison (post-D2-min, same captured traces)

| Scenario | Metric | weak baseline | strong baseline (gemini-2.5-flash) |
|---|---|---|---|
| C-en | px | 7 | 5 |
| C-en | rt | 4 | 7 |
| C-en | cap | 10 | 7 |
| G-ja | px | 4 | 0 |
| G-ja | rt | 5 | 0 |
| G-ja | cap | 1 | 0 |
| G-ja | cyrillic | 4 | 0 |
| G-ja | tool_calls | 2 | 6 |
| L | web_fetch chain | 10/10 | 10/10 (with sharper URL — direct to LICENSE) |

Strong dramatically improves G-ja (= no cyrillic attractor, 6/10 proper tool routing), reduces A on C-en (px 7→5), but does **not** eliminate C on C-en (cap 10→7). C is partially a model-prior issue that strong attenuates but does not remove.

## Key findings

1. **C is not a defect.** All structural changes preserve C at 10/10. Trying to eliminate C requires overfitting (α in SP, ζ1 in chat code) and both options were rejected on thesis grounds. C is expected behaviour for "what can you do?" questions.

2. **No deployable single-PR fix reduces both A and B.** Each candidate that improves one regresses the other:
   - H2 (no wrappers): rt 4→1 ✅ but px 7→9 ❌
   - H3 (only wrappers): px 7→2 ✅ but rt 4→9 ❌
   - ε4 (realistic description trim): **px 7→7 (=), rt 4→7 (regression)** ❌
   - ε1 (descriptions = ""): both reduced (rt 3, px 3) but not deployable
   - H1 (verb-form rename): px 7→3 ✅ but blast radius too large (every action's qualified_name changes; fixture re-record + memory updates across sessions)

3. **The "remove specifics → LLM enumerates more generically" pattern recurs.** Observed in β, ε2, ε3, ε4, γ' on C-en. The LLM substitutes whatever vocabulary remains in scope; removing one specific channel does not reduce total leak. Surface area is partly fungible.

4. **Strong tier is the only durable mitigation for G-ja** (cyrillic attractor 4/10 → 0/10, tool_calls 2/10 → 6/10). Strong is cost-gated per [[strong-model-cost-gated]] and was used here only with explicit permission for this session.

5. **Chat-layer regex intercept (ζ) works empirically but is overfit by construction.** Moving the question-shape detection from SP (= α) to chat code (= ζ1's regex) does not eliminate overfit; it relocates it. Both rejected on thesis (= structural-first, prompt-level workaround needs numbers + non-overfit shape).

## Decision

**G31 policy-accepted decision unchanged.** No SP rule, no tool-description PR, no chat-layer intercept. README weak-model warning + giveup-tracker entry remain the user-facing surface.

## Updates to existing artifacts

- **`docs/deep-dives/journal/dogfood/giveup-tracker.md` G31** — addendum below pointing at this journal entry
- **User memory** `project_g31_capability_leak_policy.md` — rewritten to reflect three-component decomposition (`[[g31-capability-leak-policy]]`)
- **User memory** `feedback_separate_leak_types_before_measuring.md` — new methodology entry (`[[separate-leak-types-before-measuring]]`)

## Do not redo these experiments

This is the explicit "do not redo" list for future sessions. The negative results below are reproducible with the captured traces + `scripts/llm_replay.py` if anyone wants to re-verify, but the conclusion is firm:

| Hypothesis | Why dropped |
|---|---|
| **α** (SP no-enum rule) | C-en cap 10→7 but B routing 10→8 (= regression). Overfit. |
| **β** (SP `skill__code_review` placeholder) | C-en rt 4→7, px 7→9 (= regression). Removing specific identifier causes LLM to generalise. |
| **δ** (language anchor) | G-ja cyrillic 4→0 (= works on that target), but G-ja routing 2→0 and px 4→7 (= regression on other axes). |
| **γ** (delete `## Action categories`) | No effect on cap-enum (10→10). Slight rt regression (4→7). |
| **γ'** (γ + wrapper-enum strip in SP) | Same as γ. cap-enum 10→10. |
| **ε1** (descriptions = "") | Reduces both A and B (rt 4→3, px 7→3) but **not deployable** — descriptions are needed for routing decisions. Useful only as a diagnostic. |
| **ε2** (invoke_action stub) | C-en rt 4→10 (= saturation). Removing the giant wrapper description makes LLM generate more wrapper-name text. |
| **ε3** (replace `skill__code_review` in list_actions desc) | C-en rt 4→8 (regression). Same pattern as β. |
| **ε4** (realistic trim + placeholder, the "production-ready" candidate) | C-en rt 4→7 (regression). px 7→7 (no improvement). **The mid-point between baseline and ε1 actively worsens metrics.** |
| **H1** (verb-form rename, e.g. file__read → read_file) | Works on A (px 7→3) but blast radius is unacceptably large — every action's qualified_name changes, all fixtures re-record, all session memories invalidated, `category` design concept weakens. Cost/benefit fails for "just A" target. |
| **H2** (delete 4 universal wrappers) | rt 4→1 ✅ but px 7→9 (= equivalent leak migrates to specific tool names). Net A+B unchanged. |
| **H3** (keep only 4 wrappers) | px 7→2 ✅ but rt 4→9 (= equivalent leak migrates to wrapper names). Net A+B unchanged. Also loses ability to use specific tools as hot-list aliases. |
| **ζ1** (chat-layer regex intercept) | C-en cap 10→0 ✅ but the regex itself hardcodes capability-question phrasings (= overfit relocated from SP to code; same kind of rule, different layer). Rejected on thesis. |

## Reproduction notes

```bash
# Tool extension (this session)
git checkout <this-PR>
# scripts/llm_replay.py now supports ~=s/pat/repl/[gi] sed-style substitution

# Capture a trace (if you have a candidate scenario)
REYN_LLM_TRACE_DUMP=/tmp/<scen>.trace.jsonl reyn chat --cui --model gemini-2.5-flash-lite
# Type your scenario message, then exit

# Replay with a patch
OPENAI_API_KEY=dummy LITELLM_API_BASE=http://localhost:4000 \
  .venv/bin/python scripts/llm_replay.py <request_id> \
    --trace /tmp/<scen>.trace.jsonl \
    --model openai/gemini-2.5-flash-lite \
    --patch 'messages[0].content~=s/<pat>/<repl>/g' \
    --output-format json --full
```

The full batch scripts used here (`/tmp/sp_*.py`) are throwaway — recreate from the candidate descriptions above if needed.

## Related

- giveup-tracker [G31](./giveup-tracker.md#g31) — original entry, framing pre-decomposition
- giveup-tracker [G12](./giveup-tracker.md#g12) — adjacent affordance-bias family
- FP-0034 issue #36 — universal catalog architecture (= the layer where descriptions live)
- PR #110 — README weak-model warning + giveup-tracker G31 entry
- PR #117 / #119 / `a1a5093a` — D2-min / D2-full / cherry-pick (= hot-list alias schema cleanse; surface-related but independent fix)
