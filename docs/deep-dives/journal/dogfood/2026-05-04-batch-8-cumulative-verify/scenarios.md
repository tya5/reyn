# Batch 8 (cumulative fix verification) — Scenarios v1 (A1)

> 5 scenario。 batch 7 wave で landing した 8 件の fix + 観測 infra 4 道具 の
> **累積 e2e 効果** を chat 経路で verify する観測 batch。 fix dispatch しない。
> attractor 系は G12 truncation fix (= `cdbd853`) が landing 済なので Pattern A
> 経路は改善されているはず、 これも観測対象。

## 共通前提

- LLM: LiteLLM proxy at `localhost:4000`、 model `openai/gemini-2.5-flash-lite`
- 実行 dir: `~/Workspace/junk/claude_sandbox/sandbox_2`
- CUI mode (`--cui --no-restore`)、 観測は `dogfood_trace.py` + `REYN_LLM_TRACE_DUMP` 必須使用
- batch 開始前 `rm -rf .reyn/` で完全 state flush
- main HEAD: `519e096` (= batch 7 narrative + G12 truncation fix landing 後)
- **`--allow-untrusted-python` flag** chat 起動時に明示 (= skill_improver 系 scenario で必須)
- **`reyn.yaml` に `python.trusted: allow`** 一時追加 (= preprocessor の startup_guard prompt skip、 commit 対象外)
- **`REYN_LLM_TRACE_DUMP=/tmp/batch8_<scenario>.jsonl`** export し、 全 LLM payload を JSONL dump (= 全 scenario で観測 infra full-on)

## 観測の枠組み

batch 7 と同じ 6 軸 (応答品質 / 意図解釈 / 待ち時間 / 見せ方 / エラー UX /
state 整合性) + 追加で:

- **internal metric**: `dogfood_trace --mode summary` の events / WAL 観測
- **user metric**: CUI に映る最終 reply の内容、 user 体感 OK / NG
- **prediction 形式の更新 (batch 7 retro 反映)**: **4 区分** (verified /
  inconclusive / refuted / **blocked**) の確率分布、 合計 100%。 batch 7 では
  `blocked` カテゴリ漏れで mass miss したのが教訓 (= S1 S5a S5b 全て top
  prediction 外して blocked)

例:
```
- internal metric: 50% verified / 20% inconclusive / 10% refuted / 20% blocked
- user metric: 40% verified / 25% inconclusive / 15% refuted / 20% blocked
```

`blocked` の base rate (= batch 7 calibration data 反映):
- chain 完走系 (= 前段 stability 低): 20-30%
- 単独 skill 経由 (= 前段 stability 中): 10-20%
- Option F 等 infra 系 (= 前段 stability 高): 5-10%

## 構成

| ID | 種別 | カバー領域 | 期待 |
|---|---|---|---|
| S1 | 8 commit 統合効果 verify (primary) | chat 経由で skill_improver が direct_llm を 6 phase 完走 | copy_to_work 到達 → run_and_eval → finalize 完走 + improvement plan が user に届く |
| S2 | Option F 実 LLM 観測 | G12 attractor で empty stop 発生時の clean failure UX | empty stop event emit + clean failure message + retry なし |
| S3 | B7-RETRO-H3 unblock final verify | preprocessor anyOf fix + B8-NEW-1/2 fix 後の copy_to_work で `data.validation` 参照 | Tier 3 で pin 済の挙動が実 weak LLM でも再現 |
| S4 | G12 truncation fix 効果 | list_skills + system prompt の skill description が ≤80 chars | payload trace で全 skill description ≤80 + Pattern A 経路 (verbose desc) の empty stop 削減 |
| S5 | eval_builder 単独直接 invoke | chat router → eval_builder e2e (B7-S5a 自然言語 + B7-S5b 構造) 両形式 | hallucinate 消失 (router enum fix) + preprocessor anyOf で union input 受理 |

S1 / S2 / S3 / S4 は input wording が同じ (= `skill_improver で direct_llm
を 1 回 review して改善案を出して`) なので、 **1 chat session で 4 scenario
の観測を同時取得可** (= cost 抑制)。 ただし scenario 4 件分の prediction /
観測 / verdict は別個に評価。

S5 は別 chat session × 2 入力 (自然言語 / 構造)、 計 2 session。

---

## Scenario 1 (8 commit 統合効果 verify): chat 経由 skill_improver chain 完走

### 目的

batch 7 wave で landing した 8 件の fix の **累積 e2e 効果** を chat 経由で
verify。 batch 7 fresh retest (`B7-S1-fresh-retest.md`) で:
- router 通過 ✅ (router enum fix の効果)
- prepare 完走 ✅
- copy_to_work で permission_denied 停止 ❌ (= B8-NEW-1)

→ B8-NEW-1 fix (= `f229f6c`) + B8-NEW-2 fix (= `ed9de6c`) 後の chain 完走を
未検証のまま batch 7 を retrospective 化した。 S1 はその直接 follow-up。

primary 観測:
- router enum fix → invoke_skill が hallucinate なく skill_improver 起動
- prepare 完走 (= `e6de782` の OS path resolution 効果)
- copy_to_work preprocessor が trusted python step で実行される (= `ed9de6c` + chat trusted python flag `07ee851`)
- copy_to_work が absolute path glob を perm 経由で許可 (= `f666acb`)
- run_and_eval cascade が score 出力
- plan_improvements / apply_improvements が retry 削減で完走 (= `0fd6d0b`)
- finalize で `improvement_result` artifact emit、 narrator 経由 user 通知

### Setup

```bash
rm -rf .reyn/
export OPENAI_API_KEY=dummy
export REYN_LLM_TRACE_DUMP=/tmp/batch8_s1.jsonl
# reyn.yaml に python.trusted: allow を一時追加 (worktree-only)
```

### Action

```bash
reyn chat default --cui --no-restore --allow-untrusted-python
```

input: `skill_improver で direct_llm を 1 回 review して改善案を出して`

### 期待結果

- skill_improver chain が **完走** (= 6 phase 全て通過、 終端 finalize phase で `improvement_result` artifact emit)
- `copy_to_work` phase が **0 turn / 0 LLM call** で完走 (= preprocessor 化 G2 効果)
- workspace dir に skill.md + phases/*.md がコピー、 0-byte file 無し
- eval cascade が score を出す (= 0.0 でない、 weak LLM 限界で 0.5 程度の見込み)
- 改善案 (= `improvement_result.next_steps`) が narrator 経由で user に届く

### 観測ポイント

```bash
# 8 commit の合流効果 = preprocessor 経路 + chain 完走
python scripts/dogfood_trace.py --mode chain | grep -E "phase_started|skill_run_completed"

# workspace dir 内容 (0-byte 無し確認)
find .reyn/skill_improver_work/direct_llm/ -type f -size 0

# eval cascade
python scripts/dogfood_trace.py --mode events | grep -E "eval|score"

# LLM trace で全 phase の payload 確認
python scripts/dogfood_trace.py llm-payloads --trace /tmp/batch8_s1.jsonl
```

### Prediction (4 区分)

- internal metric: **45% verified / 20% inconclusive / 15% refuted / 20% blocked**
- user metric: **35% verified / 25% inconclusive / 20% refuted / 20% blocked**

`blocked` base rate 高 (chain 完走系 = 前段 stability 低)、 batch 7 で発見
されなかった次層 blocker 露呈の可能性あり。

---

## Scenario 2 (Option F 実 LLM 観測): empty stop の clean failure UX

### 目的

batch 7 で確認された G12 attractor (= 50% probabilistic empty stop) に対する
Option F 実装 (= `48125ab` + `0a274fd`) の **実 LLM 経由動作確認**。 batch 7
では `--patch` replay で behavioral pin したが、 chat 経由の実 e2e は未達。

primary 観測:
- empty stop 発生時に router loop が **retry なし** で exit する
- `router_empty_response` event が emit される
- user に clean failure message (= explicit な「LLM が応答を返しませんでした」 等) が届く
- 黙って何もせず終わる UX bad な挙動が無いこと

### Setup

S1 と同 chat session で観測 (= input wording 同じなので S1 実行中に G12
発生すれば同時観測)。 もし S1 全 turn で G12 発生しなければ、 retry input
(= `じゃあ task_decomposer も同じく review して`) で 2 turn 目を回す。

### Action

S1 と同 session、 G12 発生 turn を観測対象に。 S1 で empty stop 発生しなければ
追加 input でもう 1-2 turn 回す。

### 期待結果

- 50% probabilistic で empty stop 発生 (= batch 7 N=10 測定通り)
- 発生時、 router loop が cleanly exit
- `router_empty_response` event が WAL に emit
- user に「応答が空です」 系の explicit message が届く (= 沈黙でなく)
- retry なし (= ADR 0021 Option F の核心)

### 観測ポイント

```bash
# router_empty_response event の emit 確認
python scripts/dogfood_trace.py --mode events | grep router_empty_response

# 同 turn の LLM payload trace で finish_reason=stop / content empty を確認
python scripts/dogfood_trace.py llm-payloads --trace /tmp/batch8_s1.jsonl | grep stop

# attractor 自動検出 (= G12 cross-check)
python scripts/detect_attractor.py /tmp/batch8_s1.jsonl
```

### Prediction (4 区分)

- internal metric: **40% verified / 30% inconclusive / 5% refuted / 25% blocked**
- user metric: **35% verified / 30% inconclusive / 10% refuted / 25% blocked**

inconclusive 比率高: G12 が S1 chain で発生しないと観測機会が無く、 retry
input 経由も入るので「観測できたが clean failure UX が user 体感で OK だったか」
の判断が weak LLM 出力次第で曖昧になる。

`blocked`: G12 truncation fix (= `cdbd853`) で empty stop rate が下がった
場合、 観測機会自体が減って blocked。 これは「fix が効きすぎ」 で観測不能の
ケース、 batch 8 の特殊事情として明示。

---

## Scenario 3 (B7-RETRO-H3 unblock final verify): copy_to_work で `data.validation` 参照

### 目的

B6-S1-M1 仮説 (a) (= copy_to_work LLM が input artifact 内の `data.validation`
を transparent に参照) を **実 weak LLM で final verify**。 batch 7 では
B8-NEW-1 / B8-NEW-2 で copy_to_work 未到達、 観測 blocked。 batch 8 で chain
完走すれば自然に観測経路が通る。

primary 観測:
- copy_to_work phase に到達 (= S1 と前提共通)
- preprocessor 経由 (= 0 turn / 0 LLM call) で完走
- ただし preprocessor 化前の挙動を verify したいので、 もし preprocessor が
  fail back して LLM 経路に落ちるケースがあれば観測対象に
- input artifact の `data.validation` field が transparent に participant
  field として処理される (= preprocessor が anyOf union input を正しく解釈)

### Setup

S1 と同 chat session で同時観測 (= S1 で copy_to_work 到達すれば自動的に観測)。

### Action

S1 と同 input。 観測対象は copy_to_work phase の preprocessor execution log。

### 期待結果

- copy_to_work が preprocessor で 0 turn 完走 (= G2 deterministic split 効果)
- もし preprocessor が fallback で LLM 経路に落ちた場合、 LLM が
  `data.validation` を artifact result に含めるか観測
- preprocessor 経路では「validation field を参照する LLM 判断」 自体が起きない
  (= deterministic compute_paths) ので、 真の H3 verify は LLM fallback path
  でないと観測できない可能性あり

### 観測ポイント

```bash
# copy_to_work phase の execution mode (preprocessor vs LLM)
python scripts/dogfood_trace.py --mode chain | grep -A5 copy_to_work

# preprocessor step events
python scripts/dogfood_trace.py --mode events | grep -E "preprocessor_step|python_step"

# fallback で LLM 経路に落ちた場合の payload (input artifact の dump 確認)
python scripts/dogfood_trace.py llm-detail <copy_to_work_request_id> --trace /tmp/batch8_s1.jsonl
```

### Prediction (4 区分)

- internal metric: **30% verified / 20% inconclusive / 10% refuted / 40% blocked**
- user metric: **25% verified / 25% inconclusive / 15% refuted / 35% blocked**

`blocked` base rate 高: H3 の本質は「LLM が validation field を参照するか」
だが preprocessor 化で LLM 経路自体が消えた。 fallback 観測機会が無ければ
blocked。 inconclusive 高: preprocessor 完走時に「H3 と無関係に成功した」 と
解釈すべきか「H3 が暗黙に成立した」 と解釈すべきかが曖昧。

---

## Scenario 4 (G12 truncation fix 効果 verify): description ≤80 chars + Pattern A 改善

### 目的

G12 truncation fix (= `cdbd853`) が landing 後、 list_skills + system prompt
の skill description が `MAX_DESC_LEN_FOR_LISTING = 80` で truncate されて
いるかを **payload trace で直接確認**。 副次的に Pattern A (= verbose desc が
trigger する empty stop) の発生頻度が改善されたかを cross-check。

primary 観測:
- LLM payload (= system prompt + tools schema) 内の全 skill description が ≤80 chars
- batch 7 で 218 chars (skill_improver) などの verbose desc が `...` で
  truncate されている
- empty stop frequency が batch 7 の 50% から低下しているか (= S1+retry の
  N turn で empty stop / total turn の比率)

### Setup

S1 と同 chat session で同時観測 (= LLM payload trace は全 turn 自動収集)。

### Action

S1 + S2 で発生した N turn の payload trace を batch 8 完了後に集計。

### 期待結果

- 全 skill description が ≤80 chars + 末尾 `...` (元 desc が長い場合)
- system prompt の inline skill list の description も ≤80 chars
- empty stop frequency が batch 7 (50%) より低下 (= 期待 20-30% 程度)
- batch 7 の B7-G12-context-root-cause で観測した「skill_improver desc 218
  chars → 100% empty stop」 経路が「短縮 desc → 0% empty stop」 になっているか

### 観測ポイント

```bash
# tools schema の description 長さ確認
python scripts/dogfood_trace.py llm-tools-schema <any_request_id> --trace /tmp/batch8_s1.jsonl | jq '.functions[].description | length'

# system prompt 内の skill list 行ごとの長さ
python scripts/dogfood_trace.py llm-detail <any_request_id> --trace /tmp/batch8_s1.jsonl | grep -A100 "Available skills" | awk '{print length, $0}'

# empty stop ratio (S1 + retry の N turn 中)
python scripts/dogfood_trace.py --mode events | grep -E "router_started|router_empty_response" | sort | uniq -c

# Pattern A 経路の cross-check
# 同 payload を --patch でlong-desc 化して N=10 → empty stop rate 比較
python scripts/llm_replay.py <request_id> --trace /tmp/batch8_s1.jsonl --n 10
python scripts/llm_replay.py <request_id> --trace /tmp/batch8_s1.jsonl --patch 'tools[0].function.description=<218 char verbose desc>' --n 10
```

### Prediction (4 区分)

- internal metric: **70% verified / 15% inconclusive / 5% refuted / 10% blocked**
- user metric: **60% verified / 20% inconclusive / 10% refuted / 10% blocked**

verified base rate 高: 構造的 fix なので payload trace で直接確認可能、
description 長さ ≤80 は deterministic に観測。 Pattern A 改善は確率的なので
inconclusive 比率がやや上がる。

---

## Scenario 5 (eval_builder 単独直接 invoke): B7-S5a + B7-S5b retest

### 目的

batch 7 で B7-S5a (自然言語直接 invoke = router 経由 hallucinate) +
B7-S5b (構造データ直接 invoke = preprocessor anyOf 非対応) が両方 refuted
された。 router enum fix (= `9ee6ae1`) + preprocessor anyOf fix (= `3cbe983`)
後の **eval_builder fix の独立効果** を verify。

primary 観測:
- 自然言語 input: router が `eval_builder.eval_md` 等を hallucinate せず、
  正規の `eval_builder` skill 名で invoke
- 構造データ input: union input (構造 vs 自然言語) が preprocessor で受理
- どちらの形式でも eval_builder が prepare 到達 + eval.md 生成

### Setup

S1 と別 chat session × 2 input。

```bash
rm -rf .reyn/  # batch state を S1 と分離する場合のみ。 同 session でも観測可
export REYN_LLM_TRACE_DUMP=/tmp/batch8_s5.jsonl
```

### Action

```bash
reyn chat default --cui --no-restore --allow-untrusted-python
```

input 1 (B7-S5a retest): `direct_llm の eval を作って`
input 2 (B7-S5b retest): `eval_builder で direct_llm を analyze して、 target_skill=direct_llm`

### 期待結果

- input 1: router が `eval_builder` 名で invoke (= hallucinate 消失、 enum
  制約効果)、 prepare 到達 + eval.md 生成
- input 2: union input (構造 + 自然言語混在) を preprocessor が受理、
  prepare 到達 + eval.md 生成
- 両方で eval_builder の analyze_skill phase が完走

### 観測ポイント

```bash
# router invoke の skill 名確認 (hallucinate していないか)
python scripts/dogfood_trace.py --mode events | grep -E "skill_run_started|invoke_skill"

# preprocessor の union input 解釈
python scripts/dogfood_trace.py --mode events | grep -E "preprocessor_step"

# eval.md 生成確認
ls -la .reyn/eval_builder_work/direct_llm/eval.md

# router enum fix の hallucinate 消失 cross-check
python scripts/detect_attractor.py /tmp/batch8_s5.jsonl
```

### Prediction (4 区分)

- internal metric (input 1): **65% verified / 15% inconclusive / 10% refuted / 10% blocked**
- internal metric (input 2): **55% verified / 20% inconclusive / 15% refuted / 10% blocked**
- user metric (input 1): **55% verified / 20% inconclusive / 15% refuted / 10% blocked**
- user metric (input 2): **45% verified / 25% inconclusive / 20% refuted / 10% blocked**

input 1 verified 高: router enum fix は payload trace + B7 path 1 verify で
57%→0% 確定済、 e2e でも再現性高い。 input 2 はやや低: preprocessor anyOf
fix は landing 済だが LLM の構造データ理解は確率的、 union resolution の
weak LLM 挙動が安定するかが open。

---

## Out-of-scope (= batch 8 では触らない)

- `describe_skill` 強制 (= H2 第 2 層 input field hallucinate) — 別 wave で
  実装中、 e2e effect は batch 9 以降で
- G12 attractor の **context 要因診断** (= `--patch` で system prompt /
  messages 改変 → rate 探り) — long-term root cause investigation として
  継続
- G4 trigger 評価 spike (= 強モデル比較) — proxy 強モデル追加 (user-side)
  が前提条件、 別 batch
- 新 fix dispatch (= 観測 batch なので fix 連鎖は batch 9 へ deferred)

## Calibration target

batch 7 retro 教訓を反映した 4 区分 prediction で、 batch 8 retro 時の
Brier score を **batch 7 baseline (≈ 0.45) より低下** させる。 特に
`blocked` カテゴリの正しい配分が key。

---

## A2 review pending

このファイルは A1 草案 (= assistant 提案)。 user review (= A2) を経て
A3 (= sonnet 並列実行) に移行。 review 観点:

- scenario カバー領域に偏り無いか (= chain 完走 / Option F / H3 / G12 fix /
  eval_builder の 5 軸で十分か)
- prediction の base rate (= verified / blocked 比率) が batch 7 calibration
  data と整合しているか
- 「観測できたら何が verified か」 の criteria が ambiguous でないか
- S1+S2+S3+S4 の同 session 同時観測が現実的か (= turn 数が膨らみすぎないか)
- S5 の input 2 つ目 (= 構造データ) の wording が実 user 操作として自然か
