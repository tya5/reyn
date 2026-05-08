# Batch 7 (post-infra-fix verification + MED residuals) — Scenarios v1

> 5 scenario。 batch 6 wave で landing した 6 commit (e6de782 / 0fd6d0b /
> 9763ecf / 07e16ca / 07ee851 / f666acb) の累積効果を chat 経路 e2e で
> 検証する primary 目的。 副次的に MED 残件 (B5-M1 / B4-M1 / B6-S1-M1
> 実 LLM 確認) を観測。 attractor 系 (= G12) は触らず継続。

## 共通前提

- LLM: LiteLLM proxy at `localhost:4000`、 model `openai/gemini-2.5-flash-lite`
- 実行 dir: `~/Workspace/junk/claude_sandbox/sandbox_2`
- CUI mode (`--cui --no-restore`)、 観測は `dogfood_trace.py` 必須使用
- batch 開始前 `rm -rf .reyn/` で完全 state flush
- main HEAD: `578bb03` (= batch 6 narrative integration 後)
- **`--allow-untrusted-python` flag を chat 起動時に明示** (= `07ee851` で追加された flag、 skill_improver 系 scenario で必須)
- **`reyn.yaml` に `python.trusted: allow` 追記** (= preprocessor の startup_guard prompt skip、 scenario 内では一時 config、 commit 対象外)

## 観測の枠組み

batch 6 と同じ 6 軸 (応答品質 / 意図解釈 / 待ち時間 / 見せ方 / エラー UX /
state 整合性) + 追加で:

- **internal metric**: `dogfood_trace --mode summary` の events / WAL 観測
- **user metric**: CUI に映る最終 reply の内容、 user 体感 OK / NG
- **prediction 形式の更新**: 「点 prediction」 (= 確率 1 値) でなく **分布形式
  prediction** (= verified / inconclusive / refuted の 3 区分の確率分布、 合計 100%)
  を試行。 weak LLM 挙動の不確実性を distributional に記録することで、 batch
  間の prediction calibration が改善される見込み

例:
```
- internal metric: 70% verified / 20% inconclusive / 10% refuted
- user metric: 50% verified / 30% inconclusive / 20% refuted
```

## 構成

| ID | 種別 | カバー領域 | 期待 |
|---|---|---|---|
| S1 | 6 commit 統合効果 verify (primary) | chat 経由で skill_improver が direct_llm を完走 | copy_to_work 到達 + workspace dir 作成 + eval cascade + improvement plan が user に届く |
| S2 | B5-M1 (G3 dedupe) post-fix retest | router parallel skill_improver invocation 制御 | G3 dedupe (= F5 sync 拡張) で重複 invoke が deduped × N、 cost 大幅削減 |
| S3 | B4-M1 (eval.md path) post-fix retest | eval_builder OS path resolution の e2e 効果 | eval_builder が stdlib path 解決成功、 eval.md 生成、 prepare の path search が 0 failed read |
| S4 | B6-S1-M1 仮説 (a) 実 LLM final verify | copy_to_work LLM が `data.validation` を transparent に参照 | Tier 3 で behavioral pin 済の挙動が実 weak LLM でも再現 |
| S5 | eval_builder 単独直接 invoke | chat router → eval_builder e2e (skill_improver 経由なし) | eval_builder fix の独立効果検証、 union input (構造 vs 自然言語) の両形式観測 |

S1 / S2 / S3 / S4 は input wording が同じ (= `skill_improver で direct_llm
を 1 回 review して改善案を出して`) なので、 **1 chat session で 4 scenario の観測を同時取得可**
(= cost 抑制)。 ただし scenario 4 件分の prediction / 観測 / verdict は別個に評価。

S5 は別 chat session (= 別 input)。

---

## Scenario 1 (6 commit 統合効果 verify): chat 経由 skill_improver 完走

### 目的

batch 6 wave で landing した 6 commit の **累積 e2e 効果** を chat 経由で
verify。 batch 6 終盤の dogfood retest (`07e16ca`) で発見された 2 件の
infra bug (= chat trusted python gap / workspace glob boundary) が
fix されて以降、 chat 経路で skill_improver が完走するかを未検証のまま。

primary 観測:
- chat 起動時に `--allow-untrusted-python` flag が effective か (= `07ee851`)
- copy_to_work preprocessor が compute_paths 経由で `<skill_dir>` を解決し、 absolute path glob が perm 経由で許可される (= `f666acb`)
- eval_builder が stdlib `direct_llm` の skill.md を OS-resolved path で読める (= `e6de782`)
- skill_improver chain (prepare → copy_to_work → run_and_eval → plan_improvements → apply_improvements → finalize) が完走する
- B5-M2 fix (= `0fd6d0b`) で plan/apply_improvements の retry が削減される

### Setup

```bash
rm -rf .reyn/
export OPENAI_API_KEY=dummy
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
# 6 commit の合流効果 = preprocessor 経路が動くこと
python scripts/dogfood_trace.py --mode chain | grep -E "phase_started|preprocessor"

# workspace dir 内容
ls .reyn/skill_improver_work/direct_llm/
cat .reyn/skill_improver_work/direct_llm/skill.md  # 0-byte でないこと

# eval cascade
python scripts/dogfood_trace.py --mode summary | grep -A3 eval

# 最終 reply
python scripts/dogfood_trace.py --mode summary | grep agent_message_sent

# B5-M2 retry 削減効果
python scripts/dogfood_trace.py --mode chain | grep phase_retry  # 0 件 ideal
```

### 事前 prediction (分布形式)

- **internal metric** (= chain 完走 + workspace dir 作成):
  - 60% verified / 30% inconclusive (= 途中 G12 attractor 等で停止) / 10% refuted (= infra fix が想定外の挙動)
- **user metric** (= 改善案が user に届く):
  - 35% verified / 45% inconclusive / 20% refuted (= weak LLM の plan_improvements 等で空 reply / 一般論等)

**外れ予測**:
- (a) chain が plan_improvements で attractor (= G12 再発、 control block 省略残存)
- (b) 改善案は出るが内容が一般論で具体性に欠ける (= weak LLM 限界、 G4 spike 動機強化)
- (c) eval cascade が score=0 を出す (= judgment の閾値問題)

### 後続

verified なら 6 commit 統合効果が e2e 確認。 inconclusive / refuted の場合、
attractor 種別 + WAL 観測から fix dispatch (= sequential、 並列禁止) または
G12 monitoring data 追記。

---

## Scenario 2 (B5-M1 post-fix retest): G3 dedupe 効果検証

### 目的

batch 5 で B5-M1 (= `skill_improver` 3 並列 invoke、 333k tokens / 51 LLM calls)
を観測。 G3 dedupe (= F5 sync 拡張、 `9798372`) が landing 済。 batch 6 で
完全再現確認したが G3 が effective かは未 e2e verify。 batch 7 で同 wording の
input を流して並列起動 + dedupe 動作を観測。

S1 と同 chat session 内で観測 (= 1 session で 1 input、 dedupe は LLM 自身の
parallel tool call 内で発火するので S1 観測と同時取得可)。

### 観測ポイント

```bash
# 並列 invoke 検出
python scripts/dogfood_trace.py --mode chain | grep -E "invoke_skill|tool_call_deduped"
ls .reyn/state/skill_runs/ | wc -l  # 同 skill の run_id 数

# tokens / LLM call 数
python scripts/dogfood_trace.py --mode cost
python scripts/dogfood_trace.py --mode summary | grep -c "llm_call"

# dedupe event の caller 確認
grep '"event": "tool_call_deduped"' .reyn/wal.jsonl
```

### 事前 prediction (分布形式)

- **internal metric** (= 並列 invoke 発生 + dedupe 効く):
  - 50% verified (= 並列 + dedupe 成功) / 30% inconclusive (= 並列起きず単一 invoke) / 20% refuted (= 並列起きるが dedupe 失効)
- **user metric**: n/a (= 観測のみ)

### 後続

dedupe 効果確認なら G3 を完全 resolved 化。 失効が観測されたら fix scope を
拡大 (= 例えば caller-aware dedupe key の見直し)。

---

## Scenario 3 (B4-M1 post-fix retest): eval.md path mismatch 解消確認

### 目的

batch 4 で B4-M1 (= `eval_builder` write と `prepare` read の path 不整合、
4 回 failed read) を観測。 Wave 1 で `eval_md_path_for(name)` helper 追加
(`0a92db0`) + Wave 2 で eval_builder 自身の OS path resolution 化 (`e6de782`)
が landing 済。 batch 7 で path search trace を観測し、 prepare の failed
read が 0 件に落ちたかを verify。

S1 と同 session 内で観測 (= 同 chain の prepare phase の WAL を分析)。

### 観測ポイント

```bash
# eval.md path search の trace
python scripts/dogfood_trace.py --mode chain | grep -E "file/read.*eval.md|file_read.*eval.md"

# prepare phase 内の試行回数
grep '"phase": "prepare"' .reyn/wal.jsonl | grep -c eval.md
grep '"phase": "prepare"' .reyn/wal.jsonl | grep '"status": "error"' | grep eval.md
```

具体的に観測したいこと:
- prepare が `<target_dsl_root>/eval.md` 系の path で確実に hit するか
- failed read が **0 件** に落ちたか (= batch 4 では 4 件)
- eval_builder が write した path と prepare の read path が一致するか

### 事前 prediction (分布形式)

- **internal metric** (= failed read 0 件):
  - 70% verified / 20% inconclusive (= 別 attractor で prepare 未到達) / 10% refuted (= path 不整合残存)
- **user metric**: n/a

### 後続

verified なら B4-M1 + G9 系を resolved 化。 refuted なら eval_md_path_for の
caller 整合性を再 audit。

---

## Scenario 4 (B6-S1-M1 仮説 (a) 実 LLM final verify)

### 目的

B6-S1-M1 仮説 (a): copy_to_work preprocessor の validation 結果フィールド
名が `data._validation` (underscore prefix) だと LLM が internal field と
解釈して judgment context として無視する。 → `3cf7412` で `data.validation` に
rename 済、 Tier 3 LLMReplay (`9763ecf`) で behavioral pin 済 (= verified
間接的)。 batch 6 dogfood retest は infra bug で inconclusive。

batch 7 では infra fix 2 件 (`07ee851` / `f666acb`) landing 後に copy_to_work
phase に到達できる前提なので、 **実 weak LLM が `data.validation.ok` を
transparent に参照するか** を直接 verify。

S1 と同 session 内で観測 (= copy_to_work phase の LLM response を WAL から抽出)。

### 観測ポイント

```bash
# copy_to_work phase の LLM response 抽出
grep '"phase": "copy_to_work"' .reyn/wal.jsonl | grep llm_response_received

# response 内に validation.ok の参照があるか (= reason.summary / control_ir 内)
grep '"phase": "copy_to_work"' .reyn/wal.jsonl | grep -E "validation\.ok|validation_ok|validation:"
```

### 事前 prediction (分布形式)

- **internal metric** (= LLM が validation.ok を response 内で参照):
  - 60% verified / 30% inconclusive (= 参照せず黙々と transition、 ただし validation 結果が読まれた証拠は外形上不在) / 10% refuted (= validation 無視で誤った transition)
- **user metric**: n/a (= LLM 内部挙動)

### 後続

verified なら仮説 (a) 完全確定 (= Tier 3 + 実 LLM 両方で confirmed)。
inconclusive なら追加 prompt 工夫 (= LLM に validation 結果を明示参照させる
instruction add) を検討、 refuted なら別仮説 (b)(c) 系の調査に切替。

---

## Scenario 5 (eval_builder 単独直接 invoke): union input 両形式観測

### 目的

eval_builder fix (`e6de782`) で union input 受け付け (`user_message |
eval_builder_request`) を導入したが、 chat 経由の自然言語経路と CLI 経由の
構造データ経路が e2e で動くか未 verify。 batch 7 で両形式を試して観測。

skill_improver 経由 (= S1) でなく、 router → eval_builder 直接 invoke 経路を
使う。

### Setup

```bash
rm -rf .reyn/
export OPENAI_API_KEY=dummy
```

### Action (5a: 自然言語経由)

```bash
reyn chat default --cui --no-restore --allow-untrusted-python
```
input: `eval_builder で direct_llm の eval.md を作って`

### Action (5b: 構造データ経由、 CLI 直接)

```bash
reyn run eval_builder '{"type":"eval_builder_request","data":{"target_skill":"direct_llm"}}' --allow-untrusted-python
```

### 期待結果

両形式で:
- eval_builder が起動、 analyze_skill phase の preprocessor で path 解決成功
- skill.md / phases/*.md / artifacts/*.yaml の read 成功
- eval.md が `reyn/local/direct_llm/eval.md` に生成される (= stdlib redirect)
- `eval_spec_result` artifact が emit、 `eval_md_path` が出力される

### 観測ポイント

```bash
# 5a (chat 経由): router → eval_builder invoke 確認
python scripts/dogfood_trace.py --mode chain | grep -E "invoke_skill.*eval_builder|skill_started.*eval_builder"

# preprocessor 結果
grep '"phase": "analyze_skill"' .reyn/wal.jsonl | grep -E "_resolved|_prep"

# eval.md 生成確認
ls reyn/local/direct_llm/eval.md && head -10 reyn/local/direct_llm/eval.md

# 5b (run 経由): 同上 + cost log 確認
```

### 事前 prediction (分布形式)

- **internal metric (5a)** (= chat 経由で eval.md 生成):
  - 55% verified / 30% inconclusive (= router が eval_builder を選ばない / 別 skill 起動) / 15% refuted
- **internal metric (5b)** (= CLI run 経由で eval.md 生成):
  - 80% verified / 15% inconclusive / 5% refuted (= 構造データ直送なので route 段階の不確実性なし)
- **user metric (5a)**: 40% verified (= 生成完了が user に届く) / 40% inconclusive / 20% refuted
- **user metric (5b)**: n/a (= CLI 直接、 user 体験 metric は薄い)

### 後続

両形式 verified なら eval_builder fix の独立効果確認。 5a inconclusive なら
router の eval_builder 認知度 (= description / when_to_use 文言) を再 audit。

---

## バッチ完了基準

- 5 scenario 全実行完了 (= attractor で途中停止した場合も「未達」 として記録、 fix dispatch しない)
- 各 scenario について 6 軸 + dogfood_trace 出力を記録
- 各 scenario について **分布形式 prediction** を hit/miss 評価 (= top probability category と実 verdict が一致したか)
- attractor 発生時は G12 monitoring data として `giveup-tracker.md` に追記
- findings.md + per-finding 5 要素 file (batch 1 quality 維持)
- A4 で user 感覚 review、 process 継続可否を確認
- retrospective.md で batch 7 完走 narrative + 教訓 (= 特に 6 commit 統合効果 + 分布形式 prediction の calibration 評価)

---

## A2 review request (= user に確認したい点)

1. **input wording**: S1-S4 で同じ input を流す案 (= 1 session で 4 観測同時取得)、 これで効率良いか分散させた方が良いか
2. **scenario 数**: 5 件 + 5b (= CLI run 経由) で多すぎないか / 少ないか
3. **分布形式 prediction**: 試行価値ありそうか、 点 prediction に戻した方が良いか
4. **G12 attractor 発生時の方針**: 既に「monitoring data として記録、 fix dispatch しない」 で決定済だが、 batch 7 で再確認したい (= prediction 外れ予測として記録するか、 別 column で「attractor 発生」 を separate 記録するか)
5. **infra fix の verify 範囲**: chat trusted python flag は `--allow-untrusted-python` 明示渡しで動作期待だが、 「flag 渡さない場合の error message が user-friendly か」 等を別 scenario で観測すべきか

---

## A3 step の制約 (= batch 5/6 教訓を反映)

- piped input で `reyn chat` を回す場合、 `/quit` 前に **sleep 必須** (= 非同期 peer agent 完了待ち)
- pexpect timeout: 60s/turn を default、 S1 (= chain 完走 5+ phase) は 180s 以上
- 各 scenario 1 回のみ (= cost 抑制)、 attractor で停止した場合 1 retry まで
- **dogfood_trace tool 必須**、 grep 直接禁止
- worktree 隔離で sonnet 並列実行可 (= S1 と S5 は別 session なので並列実行候補)
- **`reyn.yaml` `python.trusted: allow` の一時追加は dogfood 専用**、 commit 対象外、 cleanup 必須

---

## 想定 commit / fix 範囲 (= 観測のみ、 fix dispatch せず)

batch 7 は **観測 batch**。 fix dispatch は基本しない (= attractor 系は monitoring、
新 finding は separate wave で fix)。 例外:

- 致命的 bug (= chat 起動できない / WAL corrupt 等) → 即時 fix
- prediction 外れの mass (= 全 scenario inconclusive 等) → process 自体の見直し (= A1 step に巻き戻し)

---

## 想定工数

- A1 (この plan 起こし): 完了済 (= この doc)
- A2 (user review): 30 分以内
- A3 (実行): 5 scenario で sonnet 1-2 体並列、 ~1-1.5 hour
- A4 (user review): 30 分
- A5 (分類 + finding doc 起こし): sonnet 1 体、 1-2 hour
- retrospective: 私 (sequential) で ~1 hour

合計 ~4-5 hour / batch。
