# PR-MODEL-SPEC-EXTENDS e2e deep merge verify

| Field | Value |
|---|---|
| Date | 2026-05-06 |
| main HEAD | (= PR-MODEL-SPEC-EXTENDS landed) |
| Verdict | **verified** ✅ |
| Classification | 🟡 仕様変更 (= additive backward compat) |

## What was verified

PR-MODEL-SPEC-EXTENDS で導入した 3 mechanism を real LLM dogfood で end-to-end 確認:

1. **Built-in catalog pre-load**: `strong: gemini-flash-lite` (= str shorthand) で built-in が namespace lookup される
2. **`extends` resolution**: user-defined `base` を `standard` が `extends`
3. **Deep merge override**: nested dict (= `extra_body.reyn_marker`) で sibling field carry

## Setup (= reyn.local.yaml temporary、 verify 後に restore)

```yaml
models:
  light: openai/gemini-2.5-flash-lite          # str literal (backward compat)

  base:                                         # user-defined
    model: openai/gemini-2.5-flash-lite
    extra_body:
      reyn_marker:
        scope: parent
        from_base: yes

  standard:                                     # extends + deep merge override
    extends: base
    extra_body:
      reyn_marker:
        scope: child                            # override scalar
        from_child: yes                         # add sibling
        # from_base: yes は base から carry されるはず (= deep merge)

  strong: gemini-flash-lite                     # str shorthand → built-in
```

## Pre-dogfood resolve check (= ModelResolver invariant)

```
light: kwargs={}                                      # backward compat
base:  kwargs={extra_body: {reyn_marker: {scope: parent, from_base: True}}}
standard: kwargs={extra_body: {reyn_marker: {
            scope: child,           # ← override
            from_base: True,        # ← carry from base ✅
            from_child: True,       # ← own sibling
        }}}
strong: model=openai/gemini-2.5-flash-lite             # ← built-in resolve ✅
        kwargs={}
```

= deep merge ロジックが 設計通り 動作。

## Action

1 dogfood session: `skill_improver で direct_llm を 1 回 review して改善案を出して`
(= batch 14 と同 input)。 `REYN_LLM_TRACE_DUMP` で全 LLM call payload を JSONL dump。

## Observation

### Trace dump per-phase reyn_marker

```
Frames with reyn_marker by caller:
  phase:prepare:           1
  phase:copy_to_work:      1
  phase:run_and_eval:      3
  phase:run_target:        2
  phase:evaluate:          1
  phase:apply_improvements: 2
  phase:finalize:          1
  phase:narrate:           1
  (= 全 12 phase LLM call で carry)

Distinct reyn_marker dicts:
  {"from_base": true, "from_child": true, "scope": "child"}
```

= **全 12 phase で同一 deep-merged dict が carry**、 base + child の sibling field
全て揃っている。

### Chain completion

```
skill_improver (entry=prepare) status=finished
  phases: prepare → copy_to_work → run_and_eval → plan_improvements
        → apply_improvements → finalize
```

cost $0.0082 (= base run より cheap、 phase coverage 違いに依存)。

## Verdict reasoning

| Mechanism | Verified |
|---|---|
| Built-in catalog pre-load (= `strong: gemini-flash-lite` shorthand) | ✅ ModelResolver で `model=openai/gemini-2.5-flash-lite` resolve |
| `extends` resolution (= `standard extends base`) | ✅ base の extra_body を inherit |
| Deep merge sibling carry (= `from_base: yes` 維持) | ✅ trace dump で全 phase に存在 |
| Override semantics (= `scope: parent` → `scope: child`) | ✅ trace dump で `scope: child` |
| End-to-end passthrough (= litellm に kwargs 届く) | ✅ 12 phase 全 frames |
| Backward compat (= `light: openai/...` 既存 str form) | ✅ chain 完走 |
| Chain 完走 (= 仕様変更が既存 skill 動作に影響なし) | ✅ finalize 到達 |

## Conclusion

PR-MODEL-SPEC-EXTENDS の 3 mechanism (= built-in pre-load / extends resolution /
deep merge) は **e2e で documented 設計通り動作**。 cost variant 派生 pattern
(= `reasoning-light extends base`、 budget だけ override) や 一般 user shorthand
(= `standard: claude-sonnet-thinking` 1 行) が production-ready。

G4 spike trial (= 強モデル `gemini-3.1-flash-preview` を built-in shorthand 経由で
試行) の前提条件も整備完了、 任意 timing で着手可能。
