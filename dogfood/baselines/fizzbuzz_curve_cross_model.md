# FizzBuzz iterate-test-fix loop — cross-model baseline (2026-05-23)

Empirical baseline for the 3-scenario FizzBuzz difficulty curve under
``dogfood/fixtures/`` , run via ``dogfood/scripts/run_dogfood_iterate.py``.

Driver: ``reyn chat --cui`` per-run on an isolated tmp workspace,
sandbox ``auto`` → Seatbelt on macOS 25 (Darwin 25.3.0). All runs
done on commit ``27ee4abe`` (= PR #541 merged) plus the 5-bug
variant from PR #544.

## Pass rate (= driver pytest verdict)

| Scenario | gemini-2.5-flash-lite | gemini-2.5-flash | Δ |
|----------|----------------------:|-----------------:|---|
| ``fizzbuzz_tdd`` (empty stub + failing tests) | 4/5 = 80% (N=5) | **5/5 = 100%** (N=5) | strong ↑ |
| ``fizzbuzz_bug_planted`` (3 independent bugs) | 5/5 = 100% (N=5) | 5/5 = 100% (N=5) | — |
| ``fizzbuzz_5bugs_interleaved`` (5 masking bugs) | 4/10 = 40% (N=10) | **0/5 = 0%** (N=5) | **strong ↓** |

## Iteration depth

| Scenario | flash-lite mean_iter | flash mean_iter |
|----------|---------------------:|----------------:|
| ``fizzbuzz_tdd``               | 1.6 | 2.0 |
| ``fizzbuzz_bug_planted``       | 2.2 | 2.2 |
| ``fizzbuzz_5bugs_interleaved`` | 1.7 | 1.8 |

The stronger model does **not** iterate materially more. On the 5-bug
case it iterates the same ~1.8 times but converges on a different
local optimum.

## Attractor (= failure-mode) distribution

| Attractor | flash-lite (5bugs, N=10) | flash (5bugs, N=5) |
|-----------|--------------------------:|--------------------:|
| Early-bail (= ``writes=0``, intent-without-action) | 4/10 = 40% | 4/5 = 80% |
| Zero-special-case preservation (= ``if n == 0: return "0"`` survives) | 2/10 = 20% | 0/5 = 0% |
| **Positive-only-guard + int-return preservation** | 0/10 = 0% | **1/5 = 20%** (= run-1 inspect) |

## What flash got wrong on the 5-bug case (= the surprise)

Inspecting the run-1 ``--keep-workspace`` artefact: the strong model
**fixed bugs 1 / 4 / 5** (= zero special-case, ``"FizzBzz"`` typo,
order-of-check) in a single confident rewrite — and **left bugs 2 / 3
in place** (= ``if n > 0:`` guard wrapping the divisibility logic,
``return n`` instead of ``return str(n)``).

```python
def fizzbuzz(n):
    if n == 0:
        return "FizzBuzz"       # bug 1 fixed
    if n > 0:                   # bug 2 STILL PRESENT
        if n % 15 == 0:         # bug 5 fixed (15 before 3)
            return "FizzBuzz"   # bug 4 fixed ("FizzBuzz" not "FizzBzz")
        elif n % 3 == 0:
            return "Fizz"
        elif n % 5 == 0:
            return "Buzz"
    return n                    # bug 3 STILL PRESENT (int, not str(n))
```

The remaining bugs cluster in **structural** properties (guard scope +
type conversion). The fixed bugs cluster in **surface** properties
(constant literal + branch order). Strong-model attractor shape, in
one line: *fix surface bugs in one confident pass, miss structural
bugs that need iterative re-discovery via failed assertions*.

## Hypothesis

Stronger models may be **more confident** in their first-attempt
rewrite, leading them to commit earlier in the iterate-test-fix loop.
flash-lite's slower convergence forces it to consult pytest output
again, occasionally surfacing the structural bug. Strong's
high-confidence one-shot rewrite skips that step.

If this hypothesis holds, the "harder" scenarios are not just *more*
bugs — they're bugs distributed across the **surface vs structural
axis**, which orthogonal-to-model-capability stresses the
iterate-vs-confident-rewrite trade-off.

## Caveats (= why not a conclusion)

- **N=5 is small**. flash-lite at 40% pass means 5 consecutive
  fails have probability ~0.6⁵ ≈ 8% — possible by chance.
- One model version, one provider proxy (= localhost:4000 LiteLLM).
- Single fixture set; "FizzBuzz" may not generalise to other
  coding tasks. CSV parser / LRU cache variants would be needed to
  test the surface-vs-structural hypothesis across domains.

## Re-test recipe

```bash
# Default fixture model (= flash-lite per the project's reyn.yaml):
python dogfood/scripts/run_dogfood_iterate.py \
    --scenario fizzbuzz_5bugs_interleaved --n 10

# Override the workspace model for cross-model comparison:
python dogfood/scripts/run_dogfood_iterate.py \
    --scenario fizzbuzz_5bugs_interleaved --n 5 \
    --model openai/gemini-2.5-flash
```

The ``--model`` flag rewrites every tier in the copied workspace
``reyn.yaml`` to the given LiteLLM-style model string. Strong-model
runs require explicit per-investigation permission — see
``feedback_strong_model_cost_gated.md`` in the e2e-coder session
memory.
