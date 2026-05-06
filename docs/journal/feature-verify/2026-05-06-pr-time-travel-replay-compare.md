# PR-TIME-TRAVEL e2e replay + compare verify

| Field | Value |
|---|---|
| Date | 2026-05-06 |
| main HEAD | `c0d24ae` |
| Verdict | **verified** ✅ |
| Classification | 🔵 拡張 (= additive、 既存 spec 無変更) |

## What was verified

PR-TIME-TRAVEL (`c0d24ae`) で導入した **debug-purpose replay walker + diff
compare** を real session trace で end-to-end 確認:

1. `--mode replay`: recorded session を walk + state inspection
2. `--mode compare`: 2 session を side-by-side diff (= headline use case)
3. Engine module (= `src/reyn/replay/`) の TUI reusability sketch

## Setup (= 制約)

- **No spec change** verify: 既存 `reyn` CLI / reyn.yaml / DSL / public Python API
  無変更を grep audit で確認
- **既存 dogfood_trace.py mode 無変更**: summary / full / chain / cost /
  llm-payloads / llm-detail / llm-tools-schema 全 7 mode は backward compat
- **TUI reusability**: engine output (= dataclasses) は render-agnostic、
  `__init__.py` docstring に Textual widget mapping sketch 記載

## Action: replay mode e2e

```bash
# WAL + LLM trace を concat (= operational pattern)
cat .reyn/state/wal.jsonl .reyn/llm_trace.jsonl > /tmp/replay.jsonl

python scripts/dogfood_trace.py --mode replay \
    --trace /tmp/replay.jsonl \
    --scope phase
```

### Output (= 抜粋)

```
=== Replay: /tmp/replay.jsonl  scope=phase  frames=1 ===

[1/1]  20260506T121409Z_skill_improver::0
  events (4):
    [3] skill_started
    [4] skill_phase_advanced
    [5] step_completed
    [6] skill_phase_advanced
  state_snapshot:
    run_id: '20260506T121409Z_skill_improver'
    last_completed_op: 'llm'
    last_completed_op_id: 'prepare.llm.0'
    last_result: {control: {next_phase: 'copy_to_work', ...}, artifact: {...}}
```

✅ events / state_snapshot / LLM result が phase scope で正しく aggregate。

## Action: compare mode e2e

```bash
# 2 session の trace を concat (= 各 session で WAL + LLM)
cat .reyn/state/wal.jsonl .reyn/llm_trace.jsonl > /tmp/before.jsonl
# (run dogfood again)
cat .reyn/state/wal.jsonl .reyn/llm_trace.jsonl > /tmp/after.jsonl

python scripts/dogfood_trace.py --mode compare \
    --before /tmp/before.jsonl \
    --after  /tmp/after.jsonl \
    --scope phase
```

### Output (= 抜粋)

```
=== Compare  scope=phase  frames=1  with_diff=1 ===

[1/1]  before=20260506T121409Z_skill_improver::0
       after=20260506T121529Z_skill_improver::0
  state_diff:
    last_result:
      reason: 'The user has provided a skill ... eval spec path is available'
            → 'The user explicitly requested to improve the direct_llm skill ...'
      case_input: 'direct_llm を 1 回 review して改善案を出して' → ''
    run_id: '...121409Z...' → '...121529Z...'
```

✅ 2 session の reasoning + artifact 差分 が side-by-side で highlight。 LLM が同
input でも異なる reasoning に到達した non-determinism が data に visible。

## Operational pattern (= 重要)

現状 `--mode replay` / `--mode compare` は **single trace file** を入力に取る。
real session の **WAL events** と **LLM trace** は別 file (= `.reyn/state/wal.jsonl`
と `REYN_LLM_TRACE_DUMP=...` の指定先) に分かれているため、 dogfood 側で:

```bash
# preserve dogfood pattern with replay-ready output
export REYN_LLM_TRACE_DUMP=$(pwd)/.reyn/llm_trace.jsonl
echo "..." | reyn chat ... && \
  cat .reyn/state/wal.jsonl .reyn/llm_trace.jsonl > /tmp/<name>.jsonl
```

= **WAL + LLM trace concat** を session 終了時に行う運用。 dogfood guide に
operational tip として記載予定。

## Verdict reasoning

| Mechanism | Verified |
|---|---|
| Engine load (= multi-source: WAL + LLM via field discrimination) | ✅ `_split_sources` 経由 |
| `walk()` step iteration | ✅ phase scope で 1 frame |
| State snapshot reconstruction | ✅ last_completed_op + last_result 取得 |
| `compare()` side-by-side diff | ✅ events / state / LLM 各 layer で diff |
| LLM-independent (= no litellm call) | ✅ engine は file read-only、 cost 0 |
| Backward compat (= 既存 mode 無変更) | ✅ summary mode 等は既存挙動 |
| Test pass count | ✅ 1123 → 1160 (+37 tests) |

## TUI reusability

`src/reyn/replay/__init__.py` の docstring に sketch:

```python
from reyn.replay import ReplayEngine, StepFrame, DiffFrame, compare
# Textual widget で StepFrame.events / state_snapshot / llm_payload を render
# DiffFrame は 2-column side-by-side panel に natural mapping
```

実装は phase 3+ で別 PR、 本 PR では engine output dataclasses が render-agnostic な
状態で完備。

## Conclusion

PR-TIME-TRAVEL は **debug-purpose replay walker + diff compare** として e2e で
動作確認。 「fix landing 前後で同じ session を side-by-side」 という headline
use case (= R1 fix の 3 fire 観察を systematic に確認できる) が CLI 経由で
operational ready。

**Operational gap (= 軽微)**: WAL + LLM trace を concat する step が必要。 future
enhancement として `--wal <path> --trace <path>` の 2-input mode 検討候補だが、
本 PR では single-file 入力で MVP 完了。

stable HEAD `c0d24ae`、 1160 passed、 既存 spec 無変更、 TUI reusability sketch
完備。
