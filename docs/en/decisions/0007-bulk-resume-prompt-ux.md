# ADR-0007: Bulk 2-choice resume prompt UX

**Status**: Accepted (2026-05-03), pending implementation in PR-resume-prompt
**Track**: R-D3 (deferred from PR-resume-ux β to separate PR)

## Context

When `SkillResumeCoordinator` returns `prompt_required` for one or
more skill_runs (= ambiguous step exists, policy says ask), the
chat session needs to show this to the user and consume the answer.

Designing this UX surfaced significant trade-offs:

- How many choices? Too few = lost flexibility. Too many = decision
  paralysis.
- What wording? Avoid implementation jargon while staying actionable.
- Per-skill prompts vs. bulk view? With multi-agent skills, restart
  may have N>1 ambiguous runs.
- How much context? Skill name only? Op kind? Op-specific args?

The design went through three iterations before landing.

## Considered alternatives

### Iteration 1: 4-choice with structured info

```
Skill blog_writer (run_id=A001) interrupted with ambiguous step:
  - Started: mcp/call_tool(notion, create_page, ...) at step_id=...
  - No completion event recorded.
  - May have committed externally.

[R]etry — re-execute (may duplicate side effect)
[S]kip — assume completed, use empty result, continue
[D]iscard — abort skill, drop checkpoint
[I]nspect — show full event log + workspace state
```

Rejected: too many choices for non-expert users; each option carries
implications they're unlikely to weigh correctly. "Inspect" isn't an
action — it's a separate diagnostic flow.

### Iteration 2: 3-choice with structured "不利益 / 後でやること"

```
[1] 続ける — 完了したものとして次へ進む
    不利益: 実際は失敗していた場合、結果に反映されません
    後でやること: 送信先 (Notion) で実際に作成されたか確認
...
```

Rejected: structure leaks through ("不利益:" labels). User has to ask
clarifying questions ("blog_writer って何?", "どうやって確認するの?").
Wording is too long; the structure encourages over-explanation.

### Iteration 3 (adopted): 2-choice bulk view with description

```
3 件の skill が前回の中断から復元できます:

  alpha / blog_writer — Notion にブログ記事を投稿する
  alpha / image_picker — 画像を選択する
  beta / eval_runner — テスト評価を実行する

  [すべて続ける]  [すべてやめる]
```

## Decision

**Adopt iteration 3: bulk view, 2 choices, skill description-driven
context.**

Design rules:

- **2 choices in the prompt**: `[すべて続ける]` (= internal `skip` for
  ambiguous) and `[すべてやめる]` (= internal `discard`). `retry` is
  removed from interactive prompts; available only via `reyn.yaml`
  policy for power users.
- **Bulk view**: list all interrupted skill_runs in one prompt, not N
  separate prompts. Less friction.
- **Path format**: `<agent_name> / <skill_name> — <description>`.
  - `agent_name` for multi-agent disambiguation.
  - `skill_name` only — nested path (parent/child via run_skill) is
    deferred to R-D13 follow-up.
  - `description` from `Skill.description` field (skill author writes
    it, framework displays as-is). Empty → omit.
- **No op-specific text**: avoid trying to auto-generate "Notion へ
  の書き込み" from op args. Skill description carries the context.
- **No special-case for N=1**: same wording "N 件の skill が..."
  regardless of count. Avoids edge-case branching.
- **Selective scenarios**: handled via `/skill discard <id>` slash
  before answering the prompt, not in the prompt itself.

## Consequences

**Positive:**

- Low cognitive load: 2 verbs, the user picks "yes/no equivalent".
- Description-driven context = framework-agnostic; works for any
  skill without per-op manifest mapping.
- Bulk view scales: 5 ambiguous skills → still one prompt.
- Power users have escape hatches (slash commands, yaml policy).

**Negative:**

- Lost granularity: can't say "continue these 3 but stop those 2" in
  one shot. Workaround: discard unwanted ones via slash, then "all
  continue" the rest. Acceptable for the rare selective case.
- `retry` is hidden from interactive UX. Some legitimate retry
  scenarios (e.g. "I want to redo this even though it might
  duplicate") require yaml config, which is friction. Acceptable
  trade-off — retry's twin-execution risk is hard for non-experts to
  evaluate.
- Description quality is the skill author's responsibility. Empty or
  unclear descriptions degrade the UX. Mitigated by docs urging good
  descriptions.

**Precluded:**

- Per-skill granular prompts. Documented as out-of-scope; revisit if
  user feedback demands it.
- Op-specific prompt text generation (e.g. "外部 API への書き込み
  中"). Tried in iteration 2; rejected as fragile and noisy.

## References

- R-D3 in plan file (defered to PR-resume-prompt)
- ADR-0008 (intervention answer buffering — what carries the user's
  choice)
- ADR-0010 (CLI flags — alternative to interactive prompt)
- R-D13 (nested skill path enhancement)
