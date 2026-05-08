# Batch 6 (non-attractor focus) — Scenarios

> 5 scenario。 attractor 系 (= G12) は触らず、 G2 post-fix verify + G5
> ask_user 観測 + MED 3 件の現状 data 収集に focus。 prediction は internal
> metric / user metric 分離記録。

## 共通前提

- LLM: LiteLLM proxy at `localhost:4000`、 model `openai/gemini-2.5-flash-lite`
- 実行 dir: `~/Workspace/junk/claude_sandbox/sandbox_2`
- CUI mode (`--cui --no-restore`)、 観測は `dogfood_trace.py` 必須使用
- batch 開始前 `rm -rf .reyn/` で完全 state flush
- main HEAD: `6c8542c` (= G12 追加後)

## 観測の枠組み (6 軸 + dogfood_trace 出力)

batch 1-5 と同じ 6 軸 (応答品質 / 意図解釈 / 待ち時間 / 見せ方 / エラー UX /
state 整合性) + 追加で:

- **internal metric**: `dogfood_trace --mode summary` の events / WAL 観測
- **user metric**: CUI に映る最終 reply の内容、 user 体感 OK / NG
- **G12 attractor 発生時**: variant 種別 + 観測 data を G12 section に追記

## 構成

| ID | 種別 | カバー領域 | 期待 |
|---|---|---|---|
| S1 | G2 post-fix retest | skill_improver nested chain | preprocessor 化で eval cascade 完走、 改善案 user 到達 |
| S2 | G5 trigger trial | ask_user e2e | weak LLM で初観測、 失敗時は強モデル trial 検討材料 |
| S3 | MED 観測 (B5-M1) | router parallel skill_improver invocation | 333k tokens / 51 calls 再現観測、 G3 dedupe 設計用 data |
| S4 | MED 観測 (B2-M2) | tool_failed 後 fallback path 英語 reply | error fallback path で英語 reply を意図的に踏ませる |
| S5 | MED 観測 (B4-M1) | eval.md path mismatch | skill_improver chain で `prepare` phase の path search 観測 |

S1 と S5 は重複領域 (= skill_improver chain) なので 1 セッションで両方観測も
可。 ただし観測ポイントが別 (= S1 は e2e flow、 S5 は path search の中身) で、
prediction も別なので scenario として分離。

---

## Scenario 1 (G2 post-fix retest): skill_improver chain が改善案を出すか

### 目的

G2 (= `copy_to_work` Phase Preprocessor 化、 commit `763c86c`) の e2e
effectiveness 確認。 batch 5 retest 2 では G2 fix landing 前に走ったため
未検証。 本 scenario で:

- preprocessor 化で `0-byte file` write attractor が消えたか
- eval cascade が score を出すか
- 最終的に skill_improver が user に改善案を届けるか

### Setup

```bash
rm -rf .reyn/
export OPENAI_API_KEY=dummy
```

### Action

```bash
reyn chat default --cui --no-restore
```
input: `skill_improver で direct_llm を 1 回 review して改善案を出して`

### 期待結果

- skill_improver chain が起動: `prepare → copy_to_work → run_and_eval →
  plan_improvements → apply_improvements → finalize`
- `copy_to_work` phase が **0 turn / 0 LLM call** で完走 (= preprocessor 化)、
  workspace dir に skill.md + phases/*.md がコピー
- eval cascade が score を出す (= 0.0 でない、 batch 5 では全部 0.0)
- 改善案が narrator 経由で user に届く

### 観測ポイント

```bash
# G2 fix の効果
python scripts/dogfood_trace.py --mode chain  # preprocessor 経由を確認
ls .reyn/skill_improver_work/direct_llm/      # workspace dir 内容
cat .reyn/skill_improver_work/direct_llm/skill.md  # 0-byte でないこと

# eval cascade
python scripts/dogfood_trace.py --mode summary | grep -A3 eval

# 最終 reply
python scripts/dogfood_trace.py --mode summary | grep agent_message_sent
```

### 事前 prediction

- **internal metric**: 90% で workspace dir 作成 ✅ (= G2 preprocessor 化の効果)
- **user metric**: 70% で改善案が user に届く (= eval が score を出す前提)

**外れ予測**:
- (a) eval cascade 段階で別 attractor (= G12 family、 LLM 判断ばらつき) が
  発生し chain 途中で止まる → fix dispatch せず G12 monitoring data として記録
- (b) 改善案は出るが内容が一般論で具体性に欠ける (= weak LLM 限界、 G4
  spike 候補)

---

## Scenario 2 (G5 trigger trial): ask_user IR op 観測再挑戦

### 目的

G5 (= ask_user IR op e2e 観測) は batch 2 / 3 / 4 で連続未達。 batch 6 では
**weak LLM で観測** を狙い、 失敗時は強モデル trial (= Wave 3 G4 spike と連動)
の検討材料にする。

### Setup

```bash
rm -rf .reyn/
export OPENAI_API_KEY=dummy
```

### Action

```bash
reyn chat default --cui --no-restore --config examples/configs/with-mcp.yaml
```

input 1 (= path 曖昧誘発):
```
read_local_files skill を使って /tmp/nonexistent_report.md を読んで要約して
```

skill 内で `ask_user` IR op が発火し CUI に prompt が出れば input 2:
```
README.md を読んで
```

### 期待結果

- router が `read_local_files` を invoke (= B3-M2 fix の効果で list_skills
  name lookup が機能)
- skill phase の LLM が path 不在を検出して `ask_user` IR op を発行
- CUI に clarifying question が表示される
- user input 2 で skill が resume、 README.md を read → 要約

### 観測ポイント

```bash
# IR op 発火確認
python scripts/dogfood_trace.py --mode summary | grep -E "intervention_dispatched|intervention_resolved"

# skill 起動確認 (= 過去 batch では skill 起動段階で止まった)
python scripts/dogfood_trace.py --mode summary | grep skill_started

# 順序確認: skill_started → intervention_dispatched → intervention_resolved → skill_completed
python scripts/dogfood_trace.py --mode chain
```

### 事前 prediction

- **internal metric**: 30% で `intervention_dispatched` 発火 (= weak LLM では
  ask_user IR op を選択しにくい attractor あり)
- **user metric**: 20% で user に clarifying question が届く (= IR op 発火 +
  display パイプラインの両方が必要)

**外れ予測**:
- (a) router が pre-skill clarification を挟んで skill 起動せず (= B2-INFO 再発)
- (b) skill 起動するが LLM が `ask_user` でなく `abort` で finish
- (c) G12 attractor で list_skills 後 invoke skip → skill 起動せず
- (d) MCP catalog timing 等の別 issue (= B3-M2 root cause investigation で
  partially 解消済だが完全ではない可能性)

外れた場合は強モデル trial (= Wave 3) の動機材料になる。

---

## Scenario 3 (B5-M1 観測): router の parallel skill_improver invocation

### 目的

batch 5 で B5-M1 (= 1 review request に対し `skill_improver` 3 並列 invoke、
333k tokens / 51 LLM calls) が観測された。 batch 6 で再現観測し、 G3 dedupe
fix の設計 evidence にする。

### Setup

```bash
rm -rf .reyn/
export OPENAI_API_KEY=dummy
```

### Action

```bash
reyn chat default --cui --no-restore
```
input: `skill_improver を使って direct_llm を review して`

(= batch 5 と同じ wording)

### 観測ポイント

```bash
# 並列 invoke 検出
python scripts/dogfood_trace.py --mode chain | grep skill_improver
ls .reyn/state/skill_runs/ | wc -l  # 同 skill の run_id 数

# tokens / LLM call 数
python scripts/dogfood_trace.py --mode cost
python scripts/dogfood_trace.py --mode summary | grep -c "llm_call"
```

### 事前 prediction

- **internal metric**: 50% で 並列 invoke 再現 (= LLM judgment ばらつきで
  並列起動しないケースもあり)
- **user metric**: n/a (= 観測のみ、 fix は dispatch しない)

**外れ予測**:
- LLM が単一 invoke で済ませる場合あり、 その場合は別の input wording で
  並列を誘発する scenario を追記

### 後続

観測 data を G3 (router parallel invocation 制御) の fix design 用 evidence
として記録、 別 PR で code-side dedupe (= F5 dedupe の sync 拡張) を実装。

---

## Scenario 4 (B2-M2 観測): tool_failed 後 fallback の英語 reply

### 目的

B2-M2 (= tool_failed 後の error fallback path で reply が英語) が batch 2 で
観測された。 F11 fix は正常経路のみカバーで、 error path は english fallback の
まま。 batch 6 で意図的に tool_failed を踏ませて挙動観測、 fix 設計用 data
収集。

### Setup

```bash
rm -rf .reyn/
export OPENAI_API_KEY=dummy
```

### Action

```bash
reyn chat default --cui --no-restore
```

input (= 不存在 tool 名を明示):
```
nonexistent_skill_xyz123 を使ってこのテキストを要約して: hello world
```

(= LLM が `invoke_skill(name="nonexistent_skill_xyz123")` を試みて
`tool_failed` event が発火、 router が fallback reply を返す scenario 誘発)

### 観測ポイント

```bash
# tool_failed 発火確認
python scripts/dogfood_trace.py --mode summary | grep -E "tool_failed|tool_called.*nonexistent"

# fallback reply 言語確認
python scripts/dogfood_trace.py --mode summary | grep agent_message_sent
# CUI 出力の reply text を直接確認
```

### 事前 prediction

- **internal metric**: 70% で tool_failed event 発火 + fallback reply 経路通過
  (= LLM が「skill 存在しない」 と判断して text reply に逃げるパターン含む)
- **user metric**: 50% で fallback reply が **英語** で出る (B2-M2 再現)

**外れ予測**:
- LLM が tool_failed 経路を通らず最初から text reply (= G12 attractor 系)
- LLM が日本語で error reply を生成 (= weak LLM の判断ばらつき範囲)

### 後続

観測 data から G10 (B2-M2 fix) の方向性決定:
- option A: error fallback path に `output_language` context を渡す (F11 拡張)
- option B: code-side で deterministic な i18n table 経由に切替

option B が memory `feedback_deterministic_split.md` 整合 (= 決定論で書ける
処理は LLM 経由しない)、 推奨。

---

## Scenario 5 (B4-M1 観測): eval.md path mismatch

### 目的

B4-M1 (= `eval_builder` write と `prepare` read の path 不整合、 4 回 failed
read) が batch 4 で観測された。 batch 6 で再現観測 + 詳細 path search 観測、
fix 設計用 data 収集。

### Setup

```bash
rm -rf .reyn/
export OPENAI_API_KEY=dummy
```

### Action

S1 (= skill_improver) と同じ chain を起動するが、 観測対象は **`prepare` phase
内の `file/read` events**。 S1 の WAL を流用するか、 別途 fresh state で
回す。

```bash
reyn chat default --cui --no-restore
```
input: `skill_improver で direct_llm を 1 回 review して改善案を出して`

(= S1 と同じ input、 別 session で fresh 観測)

### 観測ポイント

```bash
# eval.md path search の trace
python scripts/dogfood_trace.py --mode full --filter file_read | grep eval.md

# prepare phase 内の試行順序
python scripts/dogfood_trace.py --mode chain | grep -B1 -A5 prepare
```

具体的に観測したいこと:
- `<target_dsl_root>/eval.md` を最初に試すか
- `reyn/local/<slug>/eval.md` まで何回 failed read したか
- 試行順序が deterministic か LLM ばらつきか

### 事前 prediction

- **internal metric**: 80% で 4 回 failed read 観測 (= batch 4 と同じ pattern)
- **user metric**: n/a (= 観測のみ)

**外れ予測**:
- skill_improver chain 自体が attractor で途中停止 (= G12 family) する場合、
  prepare phase まで到達せず観測不能 → S1 と同じく G12 monitoring data として記録

### 後続

観測 data から B4-M1 fix の方向性決定:
- option A: ADR で「skill 間 artifact path convention」 を formalize、
  `eval_builder` write と `prepare` read を一致 (= `reyn/local/<slug>/eval.md`)
- option B: `prepare` の path search 順序を逆転 (= `reyn/local/<slug>/eval.md`
  を最初の候補に)
- option C: skill_improver 側が input artifact field として `eval_md_path`
  を渡す (= LLM が探さなくて済む)

option A + B が healthy (= 規約 + 順序の両方を整える)。

---

## バッチ完了基準

- 5 scenario 全実行完了 (= attractor で途中停止した場合も「未達」 として記録、
  fix dispatch しない)
- 各 scenario について 6 軸 + dogfood_trace 出力を記録
- 各 scenario について internal metric / user metric を分離して prediction
  hit/miss 評価
- attractor 発生時は G12 monitoring data として `giveup-tracker.md` に追記
- findings.md + per-finding 5 要素 file (batch 1 quality 維持)
- A4 で user 感覚 review、 process 継続可否を確認
- retrospective.md で batch 6 完走 narrative + 教訓

---

## A2 review request

- S2 (G5 ask_user) の wording: `/tmp/nonexistent_report.md` のような絶対 path で
  曖昧さを除いた方が良いか? それとも `report.md` (相対 path) のように曖昧さを
  残した方が良いか?
- S3 (B5-M1) の input wording: batch 5 と同じ `skill_improver で direct_llm
  を review して` で並列が誘発されるか不明、 別 wording 案あれば
- S5 が S1 と input 重複している件: 1 セッションで両方観測する形 (= scenario
  4 件にまとめる) も可能、 user の好みは?
- 順序: 重複領域 (S1 / S5) を連続させるか分散させるか
- 5 件で多すぎないか / 少ないか

---

## A3 step の制約 (= batch 5 retest 2 教訓を反映)

- piped input で `reyn chat` を回す場合、 `/quit` 前に **sleep 必須** (=
  非同期 peer agent 完了待ち)
- pexpect timeout: 60s/turn を default、 G5 (peer delay 大) は 90s
- 各 scenario 1 回のみ (= cost 抑制)、 attractor で停止した場合 1 retry まで
- **dogfood_trace tool 必須**、 grep 直接禁止
- worktree 隔離で sonnet 並列実行可
