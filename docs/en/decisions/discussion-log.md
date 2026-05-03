# Skill resume — design discussion log

A chronological narrative of the iterative refinements that produced
the formal decisions in this directory's ADRs. Captures the
"discarded paths" so future readers understand what was tried and why
it was rejected.

ADRs are the verdicts; this log is the trial transcript.

## Phase 1: framing the problem (pre-D-track)

**Starting state**: PR21 added crash recovery for inbox + pending
chains, but skill execution itself was ephemeral. A skill mid-run +
crash → all in-flight work lost. For a tool whose value is multi-phase
orchestration, this was unacceptable.

Initial design cut considered three points on the spectrum:

1. Re-run skill from scratch after crash
2. Capture exact mid-step state and resume verbatim
3. Per-phase fast-forward + op memoization within the in-flight phase

(1) loses too much value. (2) requires rich per-step state capture
and is operationally complex. (3) — the "transactional event-sourced
replay" pattern — was selected. See **ADR-0002** for the formal
record.

## Phase 2: WAL or per-skill files? (state model)

Tension: per-skill state files are simpler but lose cross-skill /
cross-agent ordering. A global WAL preserves ordering but adds the
"single global file" footgun if not designed carefully.

Discarded ideas along the way:

- Per-agent WAL with periodic merge — adds a merge layer with its
  own bugs.
- SQLite — overkill, hurts operator visibility (binary file).
- "Snapshot is the truth, WAL is auxiliary log" — inverted from the
  end design. Rejected because corrupt snapshot → lost data.

Landed on: WAL = global single file = single source of truth;
snapshots = derivable cache. **ADR-0001**.

The "ballast problem" came up here as a generic warning about
log-hierarchy splits — the lower layer's truncation can drop entries
that the higher layer still needs. Drove the "WAL is global, period"
hard rule.

## Phase 3: which ops emit events?

If every op emits `step_started` + `step_completed`, WAL volume
balloons (50%+ of events are noise). Not all ops have side effects;
not all need ambiguity detection.

Iterated through:

1. Emit everything — too noisy.
2. Per-op opt-in via op definition flag — places burden on op authors;
   easy to forget.
3. **Op purity classification** — framework decides emission policy
   from the op's purity class. Adopted.

The classification (pure / world / side_effect / external / llm) maps
cleanly to "what events does this op need". See **ADR-0003**.

`python` op was tricky — runtime can't know if user code has side
effects. Defaulted to `side_effect` (pessimistic), with `pure: true`
opt-in. Static analysis was discussed and rejected as out-of-scope.

## Phase 4: memoization key

When resume re-enters the in-flight phase, ops need to match against
recorded results. The key must:

- Identify a specific call uniquely
- Survive crash (= reconstructable from WAL)
- Detect drift if inputs changed
- Be cheap

Iterations:

1. `step_seq` alone — global seq doesn't survive resume cleanly.
2. `args_hash` alone — can't distinguish identical retries.
3. `(op_kind, args_hash)` — same problem as 2.
4. `(op_invocation_id, phase, args_hash)` — adopted. **ADR-0004**.

`op_invocation_id` = `{phase}.{op_idx}` for ops, `{phase}.llm.{idx}`
for LLM. Phase-local sequential counter, deterministic on re-entry
because `_enter_phase` resets it.

## Phase 5: LLM memoization (R-D2)

Discovered late that LLM calls don't go through `dispatch_tool` —
they're direct `call_llm` invocations. So R-D2 had to add a second
memoization path in `_call_llm_and_record`.

While auditing the LLM call's effective input, two volatile fields
surfaced:

- `current_datetime` — set by `datetime.now()` each call.
- `execution.path` — derived from `OSRuntime._history` which uses
  transition strings ("draft → review") in normal operation but is
  restored from snapshot's phase-name list ("draft") on resume.

Two design choices for handling these:

- **B (record-and-replay)**: store original datetime in the recorded
  step, inject into resume's frame. Bit-perfect prompt re-creation.
- **C (strip from hash)**: simpler — exclude these fields from the
  hash. LLM still sees fresh datetime when actually called fresh.

Chose C for narrow scope. **ADR-0005**. B is the natural follow-up
if bit-perfect replay becomes a hard requirement.

The `execution.path` issue exposed a deeper schema mismatch
(`rt._history` format vs `snap.history` format). Full fix tracked as
R-D11; for now `execution.path` is excluded from memo hash and
acknowledged as "informational, not memo-critical".

## Phase 6: visit_count off-by-one (latent bug surfaced by R-D2)

LLM memo lookup never hit during R-D2 e2e. Tracing showed:

- Original run: phase entered once → `visit_counts = {X: 1}` → LLM
  call frame has `current_visit = 1`.
- Resume run: snapshot restores `visit_counts = {X: 1}` →
  `_enter_phase(X)` increments to 2 → LLM call frame has
  `current_visit = 2`.
- Frames differ → hashes differ → memo miss.

Considered:

- Strip `current_visit` from hash — loses drift detection.
- Skip `_enter_phase` on resume — too much side effect (timer, events).
- **Pre-decrement visit_count for the resumed phase** — `_enter_phase`'s
  increment lands on the recorded value. Adopted. **ADR-0009**.

## Phase 7: intervention persistence (PR-intervention-link)

When `ask_user` is mid-await and process crashes, what survives? The
intervention itself can be persisted via WAL (added
`intervention_dispatched` / `intervention_resolved` events), but the
USER ANSWER is the harder question.

Three states need handling:

1. User answers BEFORE skill resumes (= answer must be cached for
   later)
2. User answers AFTER skill resumes (= dispatch normally)
3. User answers, process crashes BEFORE skill consumes (= durability
   gap)

Discussed:

- Full WAL durability for the answer — handles all three but
  significant scope (new event kinds, new field on snapshot,
  semantics shift on `intervention_resolved`).
- In-memory buffer keyed by run_id — handles 1 and 2; loses 3.

Chose in-memory buffer for PR-intervention-link L6. Tracked durability
as **R-D12**. **ADR-0008** records the trade-off.

## Phase 8: resume UX (PR-resume-ux)

The hardest design phase, mostly because user experience is subjective.

### Iteration 8.1: 4-choice prompt with all options

```
[R]etry  [S]kip  [D]iscard  [I]nspect
```

Rejected: too much for non-experts. "Inspect" isn't an action; it's
a separate diagnostic flow.

### Iteration 8.2: 3-choice with structured "不利益 / 後でやること"

Tried showing each choice with its disadvantage and follow-up action.
Rejected: the structure leaks ("不利益:" labels). User asks questions
the structure can't answer ("blog_writer って何?", "どうやって確認?").
Adding more text makes it worse.

### Iteration 8.3: 2-choice bulk view (adopted)

```
3 件の skill が前回の中断から復元できます:

  alpha / blog_writer — Notion にブログ記事を投稿する
  alpha / image_picker — 画像を選択する
  beta / eval_runner — テスト評価を実行する

  [すべて続ける]  [すべてやめる]
```

- 2 choices in the prompt
- Description from `Skill.description` (skill author writes it)
- Bulk view to handle multi-skill restarts in one shot
- `retry` removed from interactive UX (yaml policy only)
- `inspect` removed (separate slash if needed)

**ADR-0007** records this. The journey was: more options → less
options. Clear UX wins from being aggressive about cutting choices.

### Iteration 8.4: scope (α / β / γ)

Originally planned full bulk-prompt UX in one PR. After designing the
prompt, realized the bulk prompt requires `UserIntervention` model
extension (it currently assumes 1-prompt = 1-choice). That's a real
design risk.

Three scope options:

- **α**: full prompt + everything (4-4.5 days, risky)
- **β**: skip + discard runtime + slash + CLI flags + schema version
  (3-3.5 days, no prompt UX risk)
- **γ**: CLI flags only (1 day)

Chose β for landing reliability. Bulk prompt deferred to
PR-resume-prompt as separate PR.

## Phase 9: schema upgrade policy

Late in PR-resume-ux β, the question came up: "what happens when
schema bumps?" Three failure modes:

1. Silent corruption (load anyway, drop unknown fields)
2. Hard crash with stack trace
3. Refuse + clear error + remediation command

Chose 3 for pre-1.0. The user-driven realization: trading user data
preservation for system integrity is the right move during pre-1.0
when schema is changing fast. The `--reset` command becomes the
documented escape hatch. Migration framework deferred to post-1.0
(R-D15). **ADR-0006**.

## Phase 10: discard side effects (current PR's polish)

Late thought: "is discard 100% safe?" — surfaced four risks:

1. **Zombie task**: mid-session discard leaves running asyncio.Task.
   Solved: `task.cancel() + await` in `/skill discard`.
2. **Caller blocking (foreground)**: caller dies with the process. Not
   a real problem.
3. **Caller blocking (multi-agent chain)**: agent A waiting on agent
   B's discarded skill. PR18 watchdog timeout handles this naturally.
   Tracked as **R-D14** for immediate-notification optimization.
4. **Pending interventions**: handled by existing
   `_drop_interventions_for_run`.

This audit was the user's contribution — the framing "ゾンビになったり要求元
をブロッキングするリスクはある？" forced explicit consideration of edge
cases that would have been bugs in production.

## Cross-cutting: test policy

Not a single decision but a thread through every PR:

- Tier 1/2/3 distinction (contract / OS invariant / LLM-replay)
- No `unittest.mock.MagicMock` (forbidden by `docs/contributing/testing.md`)
- TDD red→green for every layer
- Real `LLMReplay` Fake instead of mocks for LLM
- Scaffold pattern for tests tied to upcoming refactors

Documented in `CLAUDE.md` and `docs/en/contributing/testing.md`. The
discipline produced a regression-free sequence of 26+ commits across
5 PRs (490 passed / 2 xfailed).

## What this log is not

- Not a complete history. Many discussion threads happened in the
  conversation flow that aren't worth preserving. This log captures
  the structurally important pivots.
- Not the place for new design discussions. Those go in commit
  messages, plan file, or new ADRs.
