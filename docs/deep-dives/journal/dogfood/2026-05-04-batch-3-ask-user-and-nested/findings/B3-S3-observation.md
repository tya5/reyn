# B3-S3 [INFO]: nested skill — eval_builder が run_skill を使わない設計 (setup 問題)

> 一行で: `eval_builder` は `run_skill` を使わない 2-phase 完結 skill であり、
> nested skill (run_skill IR op の e2e) を観測する経路は存在しなかった。
> scenario は「外れ予測 = setup 問題」 として closed。

| Field | Value |
|---|---|
| Severity | INFO |
| Status | **closed** (setup 問題、 実行不能) |
| Scenario | B3-S3 (nested skill — run_skill IR op) |
| Found | 2026-05-04 |
| Prediction | 当たり期待 35%、 外れ典型 = eval_builder が run_skill を使わない設計 → setup 問題 |
| Prediction 結果 | **外れ予測命中** — 「eval_builder が run_skill を使わない」 setup 問題として記録 |

---

## 事前確認結果 (skill.md 精査)

`eval_builder` の `skill.md` を読んだ結果、 `run_skill` への言及は **ゼロ**。
phase graph は `analyze_skill → write_eval` の 2-phase で完結する。
Phase の役割は「target skill の DSL を読んで eval.md を生成する」 であり、
別 skill を呼び出す設計ではない。

### stdlib skills の run_skill 使用状況

全 stdlib skill の `skill.md` を grep した結果:

| Skill | run_skill を使うか | 用途 |
|---|---|---|
| `eval_builder` | **No** | 2-phase 完結、 eval.md を書き出すだけ |
| `eval` | **Yes** | `run_target` phase が target skill を `run_skill` で呼ぶ |
| `skill_improver` | **Yes** | `run_and_eval` phase が `eval` skill を `run_skill` で呼ぶ |
| `judge_phase` | **Yes (被呼び出し側)** | `iterate × run_skill(judge_phase)` で eval から呼ばれる |
| `skill_builder` | No (phase instruction に例示のみ) | 実際の op 発行は LLM 依存 |
| `mcp_search` | No | — |
| `direct_llm` | No | — |
| `read_local_files` | No | — |
| `chat_compactor` | No | — |
| `skill_importer` | No | — |
| `skill_narrator` | No | — |
| `word_stats_demo` | No | — |

### run_skill を実際に使う stdlib skill — 確定リスト

```
eval          (run_target phase → target skill を呼ぶ)
skill_improver (run_and_eval phase → eval を呼ぶ、さらに prepare phase → eval_builder を呼ぶ)
judge_phase   (被呼び出し側: eval の iterate preprocessor から呼ばれる)
```

---

## なぜ eval_builder を選択したか (scenario 設計の背景)

scenarios.md S3 の記述:

> `eval_builder` skill は内部で `eval` skill を `run_skill` で呼ぶ設計になっているか
> 確認 (skill.md を事前に読んで run_skill op の有無を確認)。

この記述は「確認を促す」 文脈であり、 run_skill 使用を仮定していた。
実際に確認した結果、 `eval_builder` は `eval` を呼ばず独立して動作する。
`eval_builder` が生成した eval.md を `eval` skill が別途実行するという
**疎結合な pipeline** になっており、 直接の `run_skill` 連鎖は存在しない。

---

## nested skill の観測が可能な正しい経路

`skill_improver` skill が run_skill chain の正しい観測ポイントである:

```
skill_improver (prepare) → run_skill("eval_builder") ← eval.md が未存在の場合
skill_improver (run_and_eval) → run_skill("eval") → eval が run_skill("judge_phase") × N
```

この chain は 3 階層 (skill_improver → eval → judge_phase) の nested run_skill を形成する。

---

## 観測 grep 結果 (実行なし)

scenario は実行段階に至らなかったため、 WAL events の観測値はなし。

```
sub_skill_started:    観測不能 (実行せず)
sub_skill_completed:  観測不能 (実行せず)
run_skill in events:  観測不能 (実行せず)
skill_runs/:          観測不能 (実行せず)
:cost:                観測不能 (実行せず)
```

---

## 6 軸分析

| 観点 | 観測 |
|---|---|
| **応答品質** | 実行なし |
| **意図解釈** | 実行なし |
| **待ち時間** | 実行なし |
| **見せ方** | 実行なし |
| **エラー UX** | 実行なし |
| **state 整合性** | 実行なし |

---

## 判定ポイント (summary)

| チェック項目 | 結果 |
|---|---|
| `run_skill` IR op が control_ir に出現したか | 未確認 (実行せず) |
| `sub_skill_started` / `sub_skill_completed` が出たか | 未確認 (実行せず) |
| skill_runs/ に 2 entry (parent + child) あるか | 未確認 (実行せず) |
| parent workspace に child output が届いたか | 未確認 (実行せず) |

---

## 後続アクション

B3 バッチ内での観測は断念。 nested skill (run_skill chain) の e2e 観測は
**batch 4 で `skill_improver` を使う scenario を設計する** ことを推奨。

推奨 scenario 設計:

```
"skill_improver skill を使って、 reyn/local/<existing_skill>/skill.md を 1 iteration 改善して"
```

観測できる chain:
- `skill_improver.prepare` → `run_skill("eval_builder")` (eval.md 未存在時)
- `skill_improver.run_and_eval` → `run_skill("eval")`
- `eval.run_target` → `run_skill("<target_skill>")`
- `eval.evaluate` (preprocessor) → `run_skill("judge_phase")` × N

この sequence が動けば **3-4 階層の run_skill ネスト** を一度に観測できる。

---

## Reproduction notes

```bash
# 事前確認のみ (実行は行わなかった)
find src/reyn/stdlib/skills/eval_builder -name "*.md"
grep -r "run_skill" src/reyn/stdlib/skills/eval_builder/   # → 0 件
grep -r "run_skill" src/reyn/stdlib/skills/ --include="skill.md"
# → eval, skill_improver, judge_phase のみヒット
```
