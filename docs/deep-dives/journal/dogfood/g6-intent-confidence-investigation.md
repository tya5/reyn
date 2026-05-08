# G6 Intent Confidence Gating — Investigation

| Field | Value |
|---|---|
| Status | investigation complete (read-only) |
| Source | router_loop.py + router_system_prompt.py + batch 1-6 findings |
| Date | 2026-05-04 |
| Scope | design spike only — no file changes |

---

## 現状の intent decision 構造

PR35 以降、 chat router は LLM native tool_use loop (`RouterLoop`) で動作する。
`build_system_prompt` が "What you can do (intent axis)" セクションで
`Action / Recall / Save / Forget / Reply` の 5 intent を列挙し、
"Behaviour" セクションに「まず intent を決定してから対応 tool 群を選べ」 という
指示を置く。 LLM はこれを読んで自律的に intent を "決定" する — 決定の
信頼性を測る仕組み (confidence score / threshold / fallback) は一切存在しない。

RouterLoop のループ条件は `result.tool_calls` の有無のみ:
- tool_calls あり → 並列 dispatch → 次 iteration
- tool_calls なし → `result.content` を text reply として outbox emit → 終了

つまり **LLM が text reply を選んだ時点で RouterLoop は無条件に終了する**。
「LLM が正しい intent を選んだか」 を OS 側で判定するロジックは皆無。
`tool_choice="auto"` で LLM に intent decision を全権委任している状態。

---

## intent mis-classification 事例 (batch 1-6 から)

| Batch | Finding | Intent 誤分類の中身 |
|---|---|---|
| Batch 1 | F3 | 「要約して」→ LLM が Reply を選択、 Action (invoke_skill) を選ばず直接 text reply |
| Batch 1 | F9 | skill 名を明示しても Reply を選択、 Action path に入らず hallucinated clarifying question を返す |
| Batch 2 | B2-H1 | specialist: list_skills → describe_skill → **Reply (空)** へ遷移、 Action の invoke_skill まで到達せず |
| Batch 2 | B2-M1 | invoke_skill を選んだが skill 名を hallucinate (`general.summarize`)、 list_skills を経由せず名前を推測 |
| Batch 3 | B3-H1 | specialist: list_skills → **Reply (空)** へ遷移、 describe_skill すら呼ばず停止 |
| Batch 3 | B3-M3 | default agent: list_skills 後に invoke_skill を bypass して Reply |
| Batch 5 | B5-H1 | consolidation revert で B3-H1 variant 再発: list_skills → Reply (空) |
| Batch 5 retest 2 | B5R2-H1 | describe_skill → **Reply (空)**: B2-H1 と同位置で 3 度目発生 |
| Batch 6 | B6-S2 | list_skills → describe_skill → **Reply (空)**: G12 の 4 連続再現確定 |

**共通パターン**: いずれも "Action" intent を途中まで実行しながら、
最後の commit ステップ (`invoke_skill`) を選ばず Reply に落ちる。
これは confidence 問題というよりも **attractor 問題** — G12 family として分類済。
B2-M1 は intent 自体は Action だが artifact 選択 (skill 名) が hallucination。

---

## 実装候補 list

### 候補 A: LLM self-report confidence (prompt 内 "how confident are you?")

LLM に intent 選択後「自信度を 0.0-1.0 で返せ」と要求し、 threshold 以下なら
Reply fallback を抑止する。

- **実装コスト**: 低 (prompt 追記 + RouterLoop 内 confidence 抽出ロジック)
- **cost 増加**: なし (同一 LLM call 内)
- **prompt 影響**: Behaviour section に 1-2 行追加。 ただし memory
  `feedback_p7_strictness.md` が示す通り、 信頼性は極めて低い。
  weak LLM (gemini-2.5-flash-lite) は self-report が不正確で、
  "confidence: 0.9" と返しながら MUST rule を無視する事例が batch 6 で観測済。
- **confidence 信頼性**: **低**。 G12 attractor の本質は "rule を知っているが
  honor しない" 確率的挙動。 self-report も同様に確率的に誤る。

### 候補 B: LLM logit / log probability からの confidence 取得

provider API が token log prob を返す場合 (= OpenAI `logprobs: true`)、
intent 選択 token の確率を取得して threshold gating する。

- **実装コスト**: 中 (litellm proxy の `logprobs` サポート確認 + RouterLoop 拡張)
- **cost 増加**: なし (log prob は inference 中に計算済)
- **prompt 影響**: なし
- **confidence 信頼性**: 中。 token 確率は self-report より客観的だが、
  gemini-2.5-flash-lite が `logprobs` を expose するか未確認。
  expose 可能でも "intent を何 token で表現するか" が不定 (tool_use path では
  tool 選択が tool_calls JSON に現れ、 intent decision の token は見えない)。
  **tool_use response の confidence は logprob では取れない可能性が高い**
  — LLM が tool_calls を選ぶ際、 tool 選択 token の log prob が API から
  返るとは限らない。 現状の litellm `call_llm_tools` ラッパーは log prob を
  扱っていない。

### 候補 C: 構造化 classifier (別 model で intent 分類後 routing)

軽量 classifier model を router の前段に置き、 user utterance を
`Action / Recall / Save / Forget / Reply` に分類して confidence score を付与。
threshold 以下なら Reply fallback を OS 側で override する。

- **実装コスト**: 高 (classifier 選定 / fine-tune or few-shot prompt / RouterLoop 前段追加 / G6 giveup tracker の "R2-R7" として当初想定された方向性)
- **cost 増加**: 高 (LLM call が毎ターン +1 追加)
- **prompt 影響**: なし (RouterLoop 前段 standalone)
- **confidence 信頼性**: 高。 分類専用 model は汎用 LLM より精度安定。 ただし
  "intent" と "attractor" は別問題 — classifier が Action と判定しても
  RouterLoop 内の invoke_skill MUST rule を weak LLM が honor するかは別。
  classifier は "入口" を gate するが、 "出口" (describe→stop) は gate できない。

### 候補 D: OS 層 state machine (discovery state track + inline prompt injection)

RouterLoop が `list_skills` / `describe_skill` 呼び出しを検出したら
`_router_state.discovered_skills` に記録し、 次 LLM call の messages に
「discovered: [X] — invoke or explain required」 を inline append する。

- **実装コスト**: 中 (RouterLoop 内 state field + messages injection logic)
- **cost 増加**: なし
- **prompt 影響**: 動的 context injection (Behaviour section 外)。 P8 境界は
  Phase instructions に対する制約で RouterLoop には直接適用されないが、
  OS に skill-specific state ("discovered_skills") を持たせる点で P7 に違反する
  可能性がある。 giveup tracker / plan file で **明示的に却下済み**
  (OS bloat + P3 違反気味 + G4 trigger 判断材料を消す)。

---

## 着手 priority 判定

### G6 の本質は attractor の subset

batch 1-6 の mis-classification 事例を分析すると、 **大半は G12 attractor
(= weak LLM の MUST rule 確率的不 honor) の manifestation** であり、
"intent classification" 段階の問題ではない。

- F3 / F9 (batch 1) は prompt rule 追加 (`e59cead`) で一時解消
- B2-H1 / B3-H1 / B5-H1 / B5R2-H1 / B6-S2 は同一 attractor variant の繰り返し
- いずれも「LLM が intent (Action) を認識した上で最終 commit を skip する」 挙動
- これは confidence が低いのでなく、 **rule を知っているが高確率で honor しない**

intent classification の "信頼性" 問題として独立に存在するのは B2-M1
(skill 名 hallucination) 程度で、 これも list_skills を呼ばせる prompt rule で
対処済み (`e59cead`)。

### G4 spike 結果待ちが最適

candidates B (logprob) と C (classifier) は実装コストと cost 増加が大きく、
かつ attractor の root cause (= weak LLM の MUST rule 不 honor) には届かない。
**候補 A (self-report)** は実装コスト最小だが信頼性が低く、 bloat trap
(`feedback_prompt_design.md`) に接近する。

結論: **G6 単独の "confidence gating" 実装で attractor を解消する経路は存在しない**。
attractor の真の解は G4 (weak LLM vs 強モデル ROI 評価) であり、 G4 spike 結果を
見てから "強モデル切替" vs "候補 C 構造化 classifier" を判断するのが正しい順序。

**今着手しない理由**:

1. G4 spike (強モデル trial) が blocked (proxy 未整備、 G12 status 参照)
2. G12 attractor が解消されれば G6 の motivation の大半が消える
3. G12 attractor が強モデルでも残るなら classifier (候補 C) の ROI が浮上するが、
   その判断は spike data なしには不可能

**G4 spike 完了後に再評価**。 spike で attractor が 0/5 なら G6 は不要。
残るなら候補 C (classifier) を R2 として plan file に追加する。

---

## Out of scope

- production model selection / classifier model 選定 (= G4 spike 結果後の判断)
- logprob availability の litellm proxy 確認 (= 候補 B の前提調査、 G4 後)
- OS 層 state machine (= 候補 D、 計画ファイルで明示却下済み)
- prompt rule の追加積み重ね (= bloat trap 確定)
