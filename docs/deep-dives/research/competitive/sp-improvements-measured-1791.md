# SP improvements from the OpenClaw/Hermes comparison — measured verdict (#1791)

**Status:** measured, closed. **Verdict: adopt 0 of 6 → no SP change.**

Follow-up to the OpenClaw/Hermes SP comparison (`comparison.md`, #1838), which
proposed six additions to Reyn's `router_system_prompt.py`. Each was treated as
an additive SP signal carrying the burden of proof and measured by dogfood A/B on
the **production-default chat scheme** (`enumerate-all`, #1657) on weak-tier
`gemini-2.5-flash-lite` via the live proxy. None cleared the bar.

This is a measure-first **success**: it empirically establishes that 5+
competitor-derived SP additions have no demonstrable benefit for Reyn, preventing
a blind-add that would bloat the cache-prefix Behaviour block and (per the
weak-tier additive-signal prior) risk diluting existing signals.

## Per-proposal verdicts (primary-evidence grounded)

| # | Proposal | Verdict | Primary evidence |
|---|----------|---------|------------------|
| 1 | TASK_COMPLETION (stub-stop / no-fabrication) | **REJECT — no demonstrable +V** (caveat: no measurable window, *not* proven-inert) | Short tasks: baseline 5/5 ok = no headroom. Long task (CHAIN): 10/10 A/B runs empty-response-confounded (5–16 empties/run — provider flakiness, not completion-behavior #1 can't fix). Fabrication clause: tool-failure bait → baseline 3/3 honest, 0 fabrication. |
| 2 | PARALLEL_TOOL_CALL | **REJECT — no headroom** | Under `enumerate-all`, baseline already batches 3 independent reads 3/3 via native parallel tool_calls. (The "serial" signal seen under a stale `codeact` override was a scheme artifact — falsified.) |
| 3 | Memory-quality guidance | **DEFER** | Hygiene emerges over many turns (not bounded-A/B-able); and the inline `## Memory` section is dropped in the universal scheme (`router_system_prompt.py:256-258`). Low ROI. |
| 4 | Prompt-injection scan | **SECURITY TRACK** | Input sanitization, not an SP signal-efficacy item. Folded into #1822 (security-reviewer/e2e). |
| 5a | abs-path (Gemini) | **REJECT — moot (mechanism)** | `file__` tools resolve paths cwd-relative through the workspace layer (`file.py:291` "list files here intent resolves to cwd"; `:344-348` glob default "." + cwd-relative combine). The LLM never constructs absolute paths. Baseline PATH 3/3. |
| 5b | non-interactive flags | **REJECT — moot (mechanism)** | Exec runs `stdin=subprocess.DEVNULL` (no TTY) when no stdin supplied (`noop_backend.py:147`) → interactive prompts get EOF, can't hang; `-y` is redundant. |
| 5c | dep-checks | **REJECT — no headroom** | Absent-lib bait → flash-lite reports honestly ("no module") or computes the answer itself; 0 fabrication. |
| 5d | keep-going | **FOLD → #1** | Semantically identical to #1's "don't stop with a plan, execute it" → #1 verdict applies. |
| 6 | date granularity (cache) | **NO CHANGE** | Premise false — `get_environment_info` already returns `datetime.date.today().isoformat()` = day-granularity (`router_host_adapter.py:582`); the SP renders a day-only date, so there is no sub-day prefix-cache invalidation. |

## Meta-finding (the load-bearing result)

**Most of the competitor SP guidance is moot for Reyn because Reyn's
ARCHITECTURE already structurally enforces what those system prompts instruct via
prose:**

- abs-path → the workspace layer resolves relative paths (`file.py`)
- non-interactive → exec runs with `stdin=DEVNULL` (`noop_backend.py:147`)
- parallel batching → the native tool-call scheme already batches independent calls

The comparison's "SP gap" is, for these, **already closed by Reyn's tool-layer
design**. This is **P3/P5-aligned**: the OS *enforces* behavior structurally; the
system prompt need not re-*instruct* what the OS already guarantees. Adding SP
prose to request what the tool layer already does is the wrong layer — it adds
prompt surface (and weak-tier dilution risk) for zero behavioral change. The
genuinely behavioral proposal (#1 completion) showed no demonstrable weak-tier
benefit: weak-tier failures are largely **capability-limited**, and
completion-*steering* cannot close a capability gap.

This is a positive validation of Reyn's design: competitor agents need these SP
directives because they drive raw shell/file ops; Reyn's tool abstraction makes
them unnecessary.

## Measurement-limitation note (for future weak-tier dogfood)

`gemini-2.5-flash-lite` via the proxy is **empty-response-prone on multi-turn
tasks** (CHAIN A/B: 10/10 runs, 5–16 empty events each — same root as the
codeact-empty investigation). This swamps behavioral signals on long-task
weak-tier A/Bs. **Future weak-tier dogfood A/Bs should use short-task or
empty-absorbing designs** (bounded turns / higher retry / capable-tier when the
*behavior* — not the provider — is the variable of interest).

## Methodology — 3 confounds excluded before the verdict

Each was caught by cross-checking before publishing a verdict
(metric-validity-before-verdict):

1. **ws state-pollution** — a run mis-wrote the input file (37→10); without a
   per-run reset the corruption persisted → a false 0/5. Caught via an
   ROI-baseline (2/3) vs A/B-baseline (0/5) inconsistency. Fix: per-run ws reset.
2. **scheme-override** — a stale `tool_use.chat: codeact` masked the production
   default `enumerate-all` (`scheme.py:320`), which **inverted** #2 and flipped
   #1's failure mode (codeact code-bug arithmetic vs enumerate-all
   clarification-stops). Fix: the harness pins the production scheme explicitly.
3. **empty-flakiness** — provider empty-responses swamp long-task multi-turn
   signals (above).

The scheme-override confound is notable: it would have **inverted** the #1 verdict
(wrongly rejecting on a capability-failure that does not exist in production) had
it not been caught — measuring under production config is load-bearing, not
cosmetic.
