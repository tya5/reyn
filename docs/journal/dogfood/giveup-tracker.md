# Trade-off & Deferred-fix Tracker

> 2 つの目的を「両立できなかった」 と判明した案件、 もしくは「真の解は明確だが
> 着手順序として後回し」 にした案件を management する index。 dogfood batch /
> 設計議論 / 運用観測 で discover した時点で記録、 状態と着手 trigger を明示。
>
> Reyn は **production-grade 開発フェーズ** (memory: `project_reyn_vision.md`) なので、
> 各案件は「真の解への道筋」 と「いつ着手するか」 を accountable に記録する。
> 「MVP だから後回し」 という defer は採用しない。 着手順序の理由 (= dependency
> / blocker / 並走 wave 等) を明示。

## なぜ tracker が必要か

scenario 別 fix を積み重ねると、 後から「あのとき何を諦めて、 何を受容したか」
が log の中に埋もれる。 結果 (a) 同じ妥協を別人が再発見する、 (b) 真の解への
復帰タイミングを逃す、 (c) cross-cutting trade-off が cumulative に増える、
の 3 リスクがある。 tracker で trade-off を可視化し、 着手 trigger を明示。

## カテゴリ定義

各案件は以下のいずれか (複数該当可) に分類:

| Cat | 説明 |
|---|---|
| **C1: model-capability-tradeoff** | weak LLM (gemini-2.5-flash-lite) では完全 honor しきれず、 強モデル併用 / 切替で本質解消する案件。 vision 整合のため weak LLM 路線で押さえ込む |
| **C2: cost-vs-reliability-policy** | Reyn vision (predictability + constrained reasoning) として恒久的に受容している policy 系 trade-off。 再評価は vision pivot 時のみ |
| **C3: architectural-complexity** | code-side で完全 fix できるが OS layer 越境 / cross-cutting 影響大、 影響範囲設計に時間が必要。 設計済 PR plan で landing 待ち |
| **C4: surfacing-pending** | 真の解は明確だが、 user-impact / production 観測がまだ surface していないため、 wave 順序として後着手。 trigger 監視中 |
| **C5: design-choice-explicit** | 機能 / granularity を明示的に捨てる選択 (= ADR で formalize)。 customer 要件で revoke 可能 |
| **C6: planned-followup-with-trigger** | 真の解 (= preprocessor 化 / refactor 等) が plan file に tracked、 別 wave で確実に landing 予定 (= 着手順序待ち) |
| **C7: prompt-vs-bloat-tradeoff** | prompt rule 追加で fix 可能だが bloat / cross-scenario interference が overhead を超える。 code-side or 強モデル路線で解消 |
| **C8: context-verbosity-trigger** | LLM への context (tool_response / system prompt) の verbosity が attractor / 誤動作の直接 trigger。 structural environment 整備 (= description truncation 等) で fix 可能な care boundary integral 領域 |

複数カテゴリ該当時は **主因 → 副因** 順で記載。

## Status 定義

- **active**: 現在の compromise 適用中、 真の解への wave 順序待ち or trigger 監視中
- **revisiting**: 着手 trigger 発火、 解消 wave 進行中
- **resolved**: 真の解 landing で給付解消
- **policy-accepted**: vision / ADR で恒久的に受容、 再評価は方針 pivot 時のみ

---

## 案件一覧

| ID | Title | Cat | Status | Discovered | Next action |
|---|---|---|---|---|---|
| [G1](#g1) | prompt size vs signal strength | C1 / C7 | revisiting (re-balance fix 進行中) | batch 5 | sonnet a39f 完了待ち |
| [G2](#g2) | `copy_to_work` LLM-driven vs deterministic | C3 / C1 | **resolved** at `763c86c` | batch 4 → 5 再 surface | — (= preprocessor 化、 LLM call 完全削除) |
| [G3](#g3) | router parallel invocation 制御 | C3 / C1 | **resolved** at `9798372` | batch 5 | — (= dedupe fix landing、 batch 6 S3 で pre-fix 再現確認) |
| [G4](#g4) | weak LLM 路線 (predictability vision) | C2 | policy-accepted | project 設計時 | 強モデル併用 trigger 監視 (G5 等の累積) |
| [G5](#g5) | `ask_user` IR op e2e 観測 | C1 / C4 | active | batch 2-4 連続未達 | scenario 設計再考 + 強モデル trial を batch 6 で |
| [G6](#g6) | router intent confidence gating (R2-R7) | C4 / C7 | active | post-PR35 dogfood | dogfood 観測で urgency 上昇 trigger |
| [G7](#g7) | phase-level permissions granularity | C5 | policy-accepted | postprocessor design | ADR-0020 で formalize 予定 |
| [G8](#g8) | nested skill `parent_run_id` 表示 | C6 | active | batch 4 | R-D13 として plan、 PR-skill-resume follow-up wave で land |
| [G9](#g9) | LLMReplay fixture rekey 自動化 | C3 / C6 | **resolved** at `1b8e82a` | 全 batch | — (= `scripts/rekey_fixtures.py` で 1 コマンド化) |
| [G10](#g10) | `tool_failed` 後 fallback の英語 (B2-M2) | C1 / C7 | **resolved** at `af16228` | batch 2 | — (= deterministic i18n table 経由、 LLM call 完全削除) |
| [G11](#g11) | MCP teardown anyio cancel scope (B2-M3) | C3 | active | batch 2-4 で再現 | R-Dx として plan 追加、 long-session 系 R-D wave で land |
| [G12](#g12) | attractor variant family (= weak LLM の MUST rule 確率的不honor) | C8 / C1 / C7 / C2 | 真因確定 (2026-05-04、 description verbosity)、 Option F 維持 + truncation fix wave 中 | batch 5 retest 2 で 3 度目発生時に確定 (4 連続は batch 6 S2、 真因は batch 7 後半 N-shot で判明) | 短期: Option G (truncation fix) + Option F 維持、 中期: proxy 整備後 G4 spike → Option C 評価 |
| [G13](#g13) | `reyn chat` trusted python gap | C2 / C5 | **resolved** at `07ee851` | B6-S1-M1 dogfood retest (2026-05-04) | — (= `--allow-untrusted-python` flag 追加で `reyn run` と symmetry 確保) |
| [G14](#g14) | `Workspace.glob_files()` stdlib boundary reject | C5 | **resolved** at `f666acb` | B6-S1-M1 dogfood retest (2026-05-04) | — (= PermissionResolver consultation 追加、 stdlib path への explicit perm で opt-in) |
| [G15](#g15) | eval_builder の stdlib path read permission gap | C5 | **resolved** | B8-S1 (2026-05-05) | B9 fix: startup_guard 非 interactive auto-approve + invoke_sub_skill resolver 伝播 (2 root cause: Hyp A + B) |
| [G16](#g16) | router intent misrouting (semantic ambiguity, post-enum-fix) | C4 / C7 | **resolved** | B8-S5a (2026-05-05) | B9 fix: description distinctive verb ('Build') + when_not_to_use create/run contrast + symmetric eval fix、 9 Tier 1 tests |
| [G17](#g17) | `_extract_skill_name` の unknown artifact_type 非対応 | C5 | **resolved** | B8-S5b (2026-05-05) | B9 fix: field-presence-first inversion、 5 Tier 2 tests |
| [G18](#g18) | router tool function description 非 truncate (Pattern A 保険) | C7 | active (low priority) | B8-S4 (2026-05-05) | Pattern A 復活時の保険、 0 empty stop で urgency 低、 G18 として monitor |

---

## 各案件詳細

### G1: prompt size vs signal strength
**Categories**: C1 (model-capability-tradeoff) / C7 (prompt-vs-bloat-tradeoff)
**Status**: revisiting (re-balance fix 進行中、 sonnet a39f)
**Discovered**: 2026-05-04 batch 5 (B5-H1)
**Related findings**: B5-H1, [feedback_prompt_design memory](../../../../.claude/projects/-Users-yasudatetsuya-Workspace-junk-claude-sandbox-sandbox-2/memory/feedback_prompt_design.md)

#### 試行
1. 旧 4 rule (F3+F9 / B2-H1 / B3-H1+M3) — 個別 bullet × 各 MUST、 wording 重複
2. `e90c0f2` で 2 段落に consolidation (= bloat 解消狙い)
3. batch 5 で specialist が再び list_skills 後空 reply (B5-H1 regression)

#### 現 compromise (re-balance fix で確定)
個別 bullet 維持 + wording 内 dedup のみ。 4-5 bullet × 各 1 MUST × jargon 削除。

#### 真の解
weak LLM 前提では現方針 (= 個別 bullet × MUST) が optimal。 強モデル併用が始まれば paragraph 形式 consolidation も成立、 prompt サイズ最小化が可能。

#### 着手 trigger / 監視
- weak LLM と強モデル並走運用が始まる時 → consolidation 可能性 re-evaluate
- prompt rule 追加が累計 7 件超えた時 → 構造的 refactor 検討 (= classifier model 切り出し等)

---

### G2: `copy_to_work` LLM-driven vs deterministic
**Categories**: C3 (architectural-complexity) / C1 (model-capability-tradeoff)
**Status**: **resolved** at `763c86c` (= Phase Preprocessor 化)
**Discovered**: 2026-05-04 batch 4 (B4-H2) → batch 5 で再 surface
**Related findings**: B4-H2, B4-L1, B5-M3 候補

#### 試行
1. 初期: `max_act_turns: 3` で LLM が glob + read で budget 使い切り、 write skip
2. `d9787cb`: 3→6 拡大 + glob scope を `<original_dsl_root>` に強制
3. batch 5: LLM が再び glob constraint 違反、 全 stdlib glob、 write skip → eval cascade FileNotFoundError

#### 現 compromise (一時的)
prompt instruction 強化と budget 拡大で **partial 動作** (1 試行で workspace 作成成功)、 ただし安定性に欠ける。

#### 真の解
`copy_to_work` は決定論的 file copy。 **Phase Preprocessor** (`run_op` ベース) で書き換え、 LLM 不要にする。 instruction の wording で再現性を出すことは不可能と判明。

#### 着手 trigger / Next action (resolved)
- ✅ Phase Preprocessor 化 landed at `763c86c`
- `max_act_turns: 6 → 0` (= LLM act loop 完全廃止)
- 8 step deterministic chain (python ×4 + run_op glob ×2 + iterate ×2)
- LLM call 削減: 旧 3-6 turns → 新 0 (cost ≈ 0 for copy phase)
- 6 新規 Tier 2 test、 sibling skill 非汚染 invariant も pin (B4-L1 と同等)

---

### G3: router parallel invocation 制御
**Categories**: C3 (architectural-complexity) / C1 (model-capability-tradeoff)
**Status**: **resolved** at `9798372`
**Discovered**: 2026-05-04 batch 5 (B5-M1)
**Related findings**: B5-M1, B6-S3-observation.md

#### 観測
batch 5 で 1 review request に対し `skill_improver` が 3 並列 invoke、 333k tokens / 51 LLM calls に達した。 cost 暴発リスク。

#### 真の解
code 側で **同 skill の同 args 重複 invoke を dedupe する rate limiter**。 既存 F5 dedupe (async tool dedupe) の sync 拡張。 dedupe scope (chain_id / run_id / phase) を設計。

prompt rule で「並列 invoke 禁止」 を入れる路線は G1 教訓で却下 (bloat 復活)。

#### 着手 trigger / Next action (resolved)
- ✅ dedupe fix landed at `9798372` (= batch 6 A3 並走中)
- **batch 6 S3 pre-fix 再現**: `skill_improver` 3 並列が 155ms 以内に発行される
  ことが fix 前 HEAD で決定論的に確認 (Run 1 / Run 2 ともに再現、 S3 / S1 でも
  同パターン)。 G3 fix の必要性が定量的に裏付けられた
- post-fix retest は batch 7 で実施予定

---

### G4: weak LLM 路線 (predictability vision)
**Categories**: C2 (cost-vs-reliability-policy)
**Status**: **policy-accepted** (Reyn vision 整合)
**Discovered**: project 設計時 (memory: `project_reyn_vision.md`)
**Related findings**: 全 batch の attractor / hallucination 系

#### 背景
Reyn は「日本企業向け predictability + constrained reasoning」 が differentiator。 強モデルの autonomy 任せでなく、 weak LLM + 強い constraint で動かす設計。

#### Trade-off
- weak LLM: cost 安、 但し attractor / hallucination が頻発 → prompt rule + code-side gate で押さえ込む必要
- 強モデル: attractor 自然消失、 但し cost 高 + autonomy 寄りで「予測可能性」 vision に逆行

#### 受容内容 (policy)
- prompt rule の継続的 maintenance を受容
- dogfood batch 毎に new attractor を発見・対処する運用コスト
- model 切替の柔軟性は LiteLLM proxy 設定で確保 (= 必要時に強モデル切替可能、 default は flash-lite)

#### 再評価 trigger
- production deployment で specific 顧客が強モデル選好を表明した時
- weak LLM での attractor 押さえ込み coverage が 80% を超え、 強モデル併用 ROI が出た時
- vision pivot

---

### G5: `ask_user` IR op e2e 観測
**Categories**: C1 (model-capability-tradeoff) / C4 (surfacing-pending)
**Status**: active (scenario 設計続行)
**Discovered**: 2026-05-04 batch 2 (B2-INFO) → batch 3 / 4 で連続未観測
**Related findings**: B2-INFO, B3-S2, B4-S2

#### 試行
- batch 2: router が pre-skill clarification で skill 未起動 → IR op 未発火
- batch 3: skill 名明示 + path 曖昧 → router が tool 呼ばず direct reply
- batch 4: B3-H1 fix 後 retry → trial A は list_skills (catalog 不在で空) / trial B は direct reply、 IR op 未到達

#### 現 compromise
未到達のまま、 batch 6 で scenario 再設計 + 強モデル trial で観測を狙う。

#### 真の解
- skill 内 instruction で「path missing → ask_user 必須」 を明記 (= prompt 強化路線、 G1 と緊張関係)
- 強モデル trial で self-driven IR op 選択を観測、 weak LLM の wording 補強の方向性を決定 (= G4 連動)

#### 着手 trigger / Next action
- batch 6 で強モデル trial を組み込み、 IR op 経路観測を最重点化
- ask_user e2e を含む customer feature request 出現時に優先度上げ

---

### G6: router intent confidence gating (R2-R7)
**Categories**: C4 (surfacing-pending) / C7 (prompt-vs-bloat-tradeoff)
**Status**: active (plan file tracked、 着手順序待ち)
**Discovered**: post-PR35 dogfood
**Related findings**: project plan file `R2-R7` 系

#### 試行
未着手。 設計時に検討した confidence gating / threshold based routing 系列。

#### 真の解
intent classification を LLM 内部判断から外して構造化 (= classifier model + threshold)。

#### 着手 trigger
- intent mis-classification の cumulative count が user impact を出した時
- 強モデル併用 (G4) で intent 判断品質が天井に達した時 → 構造化 classifier の ROI 顕在化
- batch 6+ で intent 系 finding が再発した時

---

### G7: phase-level permissions granularity
**Categories**: C5 (design-choice-explicit)
**Status**: **policy-accepted** (ADR-0020 で formalize 予定)
**Discovered**: postprocessor design 議論 2026-05-04
**Related findings**: PR-perm-skill-migrate 設計、 plan file Wave 2 (option C)

#### 背景
postprocessor 設計議論で permission 体系の選択肢が出た:
- 案 1: phase 単位で permission 細分化 (granularity あり)
- 案 2: skill 単位で permission 統一 (granularity なし)

#### Trade-off
- 案 1: 「phase A read-only / phase B write」 のような細かい制御可能、 ただし複雑
- 案 2: シンプル、 ただし「phase A read-only」 構造を捨てる

#### 受容内容
案 2 採用 = 美しさ / complexity 削減を優先、 phase-level granularity を **完全削除**。 必要時に「skill perm + phase override」 で復権可能 (= 別 PR で扱う)。

#### 再評価 trigger
- 「phase A read / phase B write」 構造が必要な user / skill request 出現時
- enterprise customer の細粒度 permission 要件発生時

---

### G8: nested skill `parent_run_id` 表示
**Categories**: C6 (planned-followup-with-trigger)
**Status**: active (R-D13 として plan 化済)
**Discovered**: 2026-05-04 batch 4 (B4-INFO-B)
**Related findings**: B4-INFO-B, plan file R-D13

#### 背景
batch 4 nested skill_improver chain 観測で `run_skill_started` event に
`parent_run_id` 欠如、 階層関係は co-location でしか復元できないと判明。

#### 真の解
- `SkillSnapshot` に `parent_run_id: str | None = None` field 追加
- `run_skill` op handler が parent's run_id を child に渡す
- `/skill list` で `agent / parent / child` 形式に表示

#### 着手 trigger / Next action
- R-D13 が次の skill-resume / nested wave に組み込まれる
- `/skill discard` cascade 再設計と連動して land
- batch 6 で nested chain 系 finding 出現時に優先度上げ

---

### G9: LLMReplay fixture rekey 自動化
**Categories**: C3 (architectural-complexity) / C6 (planned-followup-with-trigger)
**Status**: **resolved** at `1b8e82a` (= `scripts/rekey_fixtures.py`)
**Discovered**: 全 batch で system prompt 変更ごとに発生
**Related findings**: 51ba3e8, 30fdc33, ca116f3 (rekey commits、 全 7 entry × 3 round = 21 entry の手動 rekey 履歴)

#### 背景
system prompt を変更するたび LLMReplay fixture の SHA-256 key が変わり、 既存 cassette が cache miss → MissingFixture。 rekey 専用 sonnet を毎回 dispatch する運用負荷あり。

#### 真の解 (=即着手レベル)
- 案 A (採用): `scripts/rekey_fixtures.py` で 1 コマンド rekey、 LLMReplay の `_replay` を一時 patch して missing key を capture → 既存 response を新 key で append
- 案 B (将来): fixture key を prompt-stable subset に変更 (= user message + tools のみ hash)
- 案 C (長期): snapshot test framework 導入で fixture 不要化

#### 着手 trigger / Next action (resolved)
- ✅ 案 A landed at `1b8e82a` (211 行、 stdlib only、 4 Tier 2 test)
- 25-30 min/round の手作業 → 30 秒 1 コマンドに短縮
- 案 B / C は将来の operational efficiency 改善として open (= 必要時に R-Dx 化)

---

### G10: `tool_failed` 後 fallback の英語 (B2-M2)
**Categories**: C1 (model-capability-tradeoff) / C7 (prompt-vs-bloat-tradeoff)
**Status**: **resolved** at `af16228`
**Discovered**: 2026-05-04 batch 2 (B2-M2)
**Related findings**: B2-M2, B6-S4-observation.md

#### 背景
F11 fix で正常経路の日本語 reply を確保したが、 `tool_failed` 後の error fallback path が英語のまま。

#### 真の解
- error fallback path に `output_language` context を渡し、 LLM に指示 (= F11 拡張)
- code-side で error message を i18n table 経由で生成 (= prompt 触らず deterministic)

#### 着手 trigger / Next action (resolved)
- ✅ deterministic i18n table 経由の fix landed at `af16228` (= batch 6 A3 並走中)
  - `tool_failed` event 後の fallback reply 生成を LLM call から code-side i18n
    table 経由に切替、 LLM call 完全削除
  - memory `feedback_deterministic_split.md` の決定論分離思想を適用
- **batch 6 S4 観測が示した effective scope の注記**: S4 では LLM が
  `invoke_skill` を呼ばずに text reply を直接返したため、 `tool_failed` 経路
  が発火しなかった。 G10 fix は `tool_failed` が発火した場合に確実に日本語
  reply を出す修正で、 方向は正しい。 ただし LLM が tool call 自体を選ばない
  経路 (= G12 family) では fix の効果が届かない — これは G12 問題の一側面
  として記録

---

### G11: MCP teardown anyio cancel scope (B2-M3)
**Categories**: C3 (architectural-complexity)
**Status**: active (再現確認済、 機能影響なし)
**Discovered**: 2026-05-04 batch 2 (B2-M3) → batch 3 (B3-L3) → batch 4 (B4 でも再現)
**Related findings**: B2-M3, B3-L3, B4-S4

#### 背景
MCP server teardown 時に anyio cancel scope の RuntimeError が stderr に残る。 機能影響は無く、 long session でのリーク懸念のみ。

#### 真の解
MCP client lifecycle を asyncio task group で wrap、 cancel scope を厳格管理。 `src/reyn/mcp/client.py` の shutdown path を refactor。

#### 着手 trigger / Next action
- R-Dx として plan file に追加、 long-session 系 R-D wave (= R-D2 / R-D8 系の周辺) で land
- production env で stderr noise が user-visible になる前に予防的 landing

---

### G12: attractor variant family (= weak LLM の MUST rule 確率的不honor)
**Categories**: C8 (context-verbosity-trigger、 真因) / C1 (model-capability、 加担因) / C7 (prompt-vs-bloat、 既往) / C2
**Status**: 真因確定 (2026-05-04、 description verbosity)、 Option F 維持 + truncation fix wave 中
**Discovered**: 2026-05-04 batch 5 retest 2 で attractor 3 度目発生時に確定
**Related findings**: B2-H1 / B3-H1 / B5-H1 / B5R2-H1 / B6-S2-observation.md (= 同 family の variant 系譜)
**Spike record**: `docs/journal/dogfood/g4-trigger-evaluation-spike.md`
**Design doc**: [ADR-0021](../../en/decisions/0021-g12-attractor-structural-fix-design.md) (2026-05-04)

#### 背景

weak LLM (gemini-2.5-flash-lite) が `router_system_prompt.py` の MUST rule を
確率的に honor しない事象が attractor variant として複数 batch で発生:

| Stage | Attractor | 当時の対処 | 結果 |
|---|---|---|---|
| batch 2 (B2-H1) | `describe → 停止` | `83bad83` MUST rule 追加 | 一時解消 |
| batch 3 (B3-H1) | `list → 停止` | `48676ad` MUST rule 追加 | 一時解消 |
| batch 5 fix-verify (B5-H1) | consolidation `e90c0f2` で破壊 | `ca116f3` で revert | 一時解消 |
| **batch 5 retest 2 (B5R2-H1)** | **`describe → 停止` 再発** | open | **prompt rule 路線の限界確定** |
| **batch 6 S2** | **`describe → 停止` 4 連続再現** | 記録のみ (G12 policy) | **Wave 3 G4 spike 優先度を即上げ** |

`83bad83` の MUST rule が今も prompt にあるにも関わらず、 weak LLM が honor
しないケースが出る。 「prompt rule に依存する戦略では完封できない」 が確定。

#### 試行と判断

OS 層 state machine による gate (= 当初検討) を **却下**:

- OS が weak LLM の不確実性を吸収する装置になり、 P3 (= OS = runtime engine、
  not LLM behavior controller) 違反気味
- attractor 別 variant 出現ごとに OS gate 増設が必要 = bloat の linear growth
- G4 trigger 判断材料 (= 「weak LLM 単独ではここまで」 の上限観測) を曇らせる
- prompt rule (memory `feedback_prompt_design.md`) と同型の「OS bloat trap」
  を生む

代わりに **G12 化 + G4 trigger spike** で resource を真の解 (= 強モデル併用
ROI 評価) に集中。

#### 真の解

- **structural fix (短期)**: list_skills + system prompt 両経路で skill description を ≤80 chars に truncate
  (= a6127a46 wave で実装、 batch 8 で verify 予定)
- **G4 spike (中期、 user-side)**: 強モデル (claude-sonnet / gemini-2.5-pro) で
  attractor scenario を 5 回回し、 発生率 + cost 上昇を定量化。 truncation fix 後も
  attractor が残る場合に G4 切替検討
- ROI 判定:
  - attractor 0/5 + cost ≤ 10x → G4 切替候補 (= G12 resolved)
  - 改善するが cost > 10x → 案件別 model selection
  - 変わらない → 強モデルでも限界、 別 attribute (= prompt structure) を疑う
- Option F 維持: structural fix 後も empty-stop が 0% にならない場合に observability 経路として残す
- 中間策: attractor 受容 + runtime retry / chain timeout で recover (= 既存
  PR21 + R-D14 が一定範囲で吸収)

#### 着手 trigger / Next action

設計は [ADR-0021](../../en/decisions/0021-g12-attractor-structural-fix-design.md) で formalize 済。

**短期 (2026-05-04 採用済) — Option F**:
- ~~Option B~~ → **却下** (user 原則: LLM glitch を Reyn が auto-rescue しない)
- **Option F 採用**: `RouterLoop.run()` で empty-stop 検出 → audit event emit
  (`router_empty_response_detected`) + user-visible failure message → exit (no retry)
- 実装: `_is_empty_router_response()` + `_EMPTY_RESPONSE_MSG` dict in `router_loop.py`
- Tier 2 tests: `tests/test_router_empty_response.py` (16 tests)
- 履歴: ADR-0021 Option B 却下、 Option F 採用 (2026-05-04)

**短期 (2026-05-04 実装中) — Option G**:
- **Option G 採用**: list_skills + system prompt 両経路で skill description を ≤80 chars に truncate
  (詳細は describe_skill 経由で取得する summary/detail パターン)
- 実装: `router_loop.py` list_skills tool_response builder + `router_system_prompt.py` inline skill list
- 効果: H-b1 実験 (skill_improver desc のみ 218→<80 chars) で empty-stop 0/5 (0%)
- 担当 wave: a6127a46 (別 sonnet 並走中)

**中期 (proxy 整備後)**:
- **prerequisite**: `/Users/yasudatetsuya/Workspace/junk/litellm/config.yaml` に
  `claude-sonnet` または `gemini-2.5-pro` を追加し proxy reload
  (設定手順: `docs/journal/dogfood/g4-trigger-evaluation-spike.md` 参照)
- G4 spike (Option A 計測) を実施 → attractor rate + cost ratio を測定
  - spike が 0/5 attractor + cost ≤ 10x → Option C (user opt-in) 採用判断へ
  - spike が 5/5 attractor → 強モデルでも限界、 prompt structure 調査が別途必要
- Option C は user-configurable opt-in として評価 (default 変更は不採用)

**defer**:
- Option D (tool_choice=required): G4 spike 実施時に `llm_replay.py --patch` で副作用計測
- Option E (per-session auto-resume): 必要性が surfaced した場合のみ再評価
- Option A (flat strong-model 固定): Reyn vision 整合のため default 変更は不採用

#### 真因観測 (2026-05-04、 batch 7 後半)

`B7-G12-context-root-cause.md` (a62a9dad) と `B7-G12-cross-attractor-pattern.md`
(a947255e) の 2 件 N-shot 観測で、 G12 attractor の真因が:

> **skill description verbosity (specifically `skill_improver` の 218-char
> description) が両経路 (= list_skills tool_response / system prompt inline)
> で trigger**

と確定。 H-b 実験で description を 1342→285 chars に縮小すると empty-stop は
**100% → 0%** に落ちる。

過去仮説の paradigm shift:
- MUST rule non-honor (= RETRO-H4) → H-a で effect 0、 関係なかった
- weak LLM の内部限界 → structural fix で 100% rescue 可能
- ✅ context verbosity が真因、 これは Reyn の structural environment 整備で fix 可能領域 (= care boundary integral)

| 仮説 | 実験 | 結果 |
|---|---|---|
| H-a: MUST rule 削除 → 無関係か | MUST rule を prompt から除去して replay | effect 0 (= MUST rule は attractor に無関係) |
| **H-b: description 縮小** | list_skills response を 1342→285 chars に縮小 | **100% → 0%** (= decisive) |
| H-b1: skill_improver desc のみ縮小 | skill_improver の 218-char description のみ <80 chars に | 0/5 (0%) empty-stop (= 218-char が trigger 確定) |
| H-c / H-d | 他仮説 | effect 0 |

Cross-attractor pattern (B7-G12-cross-attractor-pattern.md):
- 全 5 attractor で共通 = skill catalogue 可視性
- Trigger 経路 2 種:
  - Pattern A: list_skills tool_response (= 3 件)
  - Pattern C: system prompt inline skill list embedding (= 2 件)
- 両経路で同 verbose context が trigger → 両経路 fix が必要

#### batch 6 S2 observation (4 連続再現の記録)

| 観測項目 | 値 |
|---|---|
| Batch | batch 6 S2 |
| Variant | `list_skills → describe_skill → stop` |
| LLM model | gemini-2.5-flash-lite |
| Tool sequence | `list_skills("read_local_files")` → `describe_skill("read_local_files")` → empty reply |
| `invoke_skill` 発行 | no |
| LLM calls (router) | 3 |
| Tokens | 5,987 |
| MUST rule 適用状態 | `83bad83` / `48676ad` / `ca116f3` が全て適用済みの prompt で発生 |
| 累積再現 batch | B2-H1 → B3-H1 → B5R2-H1 → **B6-S2** (= 4 連続) |

#### Out-of-scope (= **やらない** 事項)

- OS 層 state machine による gate 実装 (= 撤回済)
- prompt rule の追加積み増し (= bloat trap)

#### 教訓

- **「fix できる」 と「fix すべき」 は別問題**: 技術的に code 側で gate を
  作ることは可能だが、 vision 整合 / OS complexity / G4 trigger 判断材料を
  考慮すると、 attractor 系は受容 + G4 評価が optimal
- **giveup tracker は「諦め」 でなく「正しい解への judgment 整理」**: G12 化
  により attractor 系を「監視 + 評価」 の status に明示移動、 OS 投資を
  非 attractor 系に集中させる
- **prompt rule の bloat trap と OS layer の bloat trap は同型**: bullet 単位
  で対症療法を続ける戦略は、 prompt でも code でも同じ pitfall

#### 教訓: paradigm shift via 観測 evidence

batch 5 retest 2 → batch 6 → batch 7 retro と 3 batch にわたり「MUST rule
non-honor」 を G12 真因と仮定して prompt rule 累積 / 撤回 / 整理に focus した
が、 batch 7 後半の N-shot 観測 (= a62a9dad H-a で MUST 削除 → effect 0、 H-b
で description 縮小 → 100% rescue) で **MUST 系仮説は 4 batch 全部 hallucinated
の推測スタック** だったと判明。

memory `feedback_minimize_speculation.md` の 「観測道具なしで推測しない」
原則の威力を実証した case。 G12 を 4 batch 引きずったのは観測 infra 不在期、
infra 整備 (= REYN_LLM_TRACE_DUMP + llm_replay --patch + detect_attractor)
完了直後に真因が 1 batch で判明した。

---

### G13: `reyn chat` trusted python gap
**Categories**: C2 (cost-vs-reliability-policy) / C5 (CLI surface inconsistency)
**Status**: **resolved** at `07ee851`
**Discovered**: 2026-05-04 B6-S1-M1 dogfood retest
**Related findings**: [B6-S1-M1-hypothesis-a-retest.md](2026-05-04-batch-6-non-attractor/findings/B6-S1-M1-hypothesis-a-retest.md)

#### 背景

`skill_improver` の `copy_to_work` preprocessor は `mode='trusted'` の python step
を含む。 `reyn run --allow-untrusted-python` フラグが存在するが、 `reyn chat` には
対応するフラグがなかった。

`reyn.yaml` に `python.trusted: allow` を設定しても、 `PermissionResolver` は
`trusted_python_allowed=False` 固定で生成されるため、 runtime の hard-fail を
bypass できない — config 設定が効果を持たないという設計 gap。

#### 観測

B6-S1-M1 dogfood retest の chat run で `preprocessor_step_failed` が step 0 で発生:

```
python step ./copy_to_work_resolver.py:compute_paths declares mode='trusted'
but --allow-untrusted-python was not provided.
```

`reyn chat` context では trusted python step を含む skill が **設定に依存せず到達不能**
という状態だった。

#### 試行

特になし (= 発見即修正)。

#### 真の解

`reyn chat` に `--allow-untrusted-python` フラグを追加し、 `PermissionResolver` の
`trusted_python_allowed` フラグを配線。 `reyn run` との CLI 対称性を確保。

#### 着手 trigger / Next action (resolved)

- ✅ `reyn chat --allow-untrusted-python` flag landed at `07ee851` (2026-05-04)
  - `+4 test`、 0 regression
  - `reyn run` と同 flag surface で symmetry 確保
- Out-of-scope: config bypass による完全自動化 (= `python.trusted: allow` で
  `--allow-untrusted-python` と同等になる設計) — security trade-off のある変更で
  別 PR での議論が必要

#### 教訓

- **dogfood は意図しない infra bug を炙り出す機構として有効**: 仮説 (a) の
  観測を試みた dogfood retest が、 CLI surface の非対称という別の設計 gap を
  発見した。 「観測失敗 = 無益」 でなく「インフラ gap 発見の起点」として価値がある

---

### G14: `Workspace.glob_files()` stdlib boundary reject
**Categories**: C5 (workspace abstraction inconsistency)
**Status**: **resolved** at `f666acb`
**Discovered**: 2026-05-04 B6-S1-M1 dogfood retest
**Related findings**: [B6-S1-M1-hypothesis-a-retest.md](2026-05-04-batch-6-non-attractor/findings/B6-S1-M1-hypothesis-a-retest.md)

#### 背景

`copy_to_work_resolver.py` の `compute_paths()` は stdlib skill の path を
**absolute path** で返す (stdlib は worktree 外の `src/reyn/stdlib/skills/` 以下)。

`Workspace.glob_files()` は `base_dir` (= worktree root) および `state_dir` の
下かどうかを boundary check し、 外れていれば `PermissionError` を raise。

`file.read: allow` を `reyn.yaml` に設定しても、 この boundary check は
`PermissionResolver.require_file_read()` を経由しない別レイヤー実装のため
bypass されない — **permission system と workspace boundary の二重 gate 構造**
が引き起こした gap。

#### 観測

B6-S1-M1 dogfood retest の run mode で step 1 が失敗:

```
"glob not permitted: '/Users/yasudatetsuya/.../sandbox_2/src/reyn/stdlib/skills/
direct_llm/skill.md' (outside project)"
```

`file.read: allow` config が設定されているにも関わらず、 別レイヤーの boundary
check が拒否した。

#### 試行

特になし (= 発見即修正)。

#### 真の解

`Workspace.glob_files()` に `PermissionResolver` consultation を追加。 boundary
外の path については `PermissionResolver.is_read_allowed()` を呼び出し、
明示的な permission (= `file.read: allow` 相当) があれば通過させる設計。

stdlib path への glob は explicit perm で opt-in する形で、 security は保ちつつ
legitimate なユースケース (= stdlib skill source を読む) を通過させる。

#### 着手 trigger / Next action (resolved)

- ✅ `Workspace.glob_files()` perm consultation landed at `f666acb` (2026-05-04)
  - `+4 test`、 0 regression
- Out-of-scope: boundary check を完全廃止する設計変更 — workspace isolation の
  意図 (= worktree 外へのアクセスを明示的 perm でのみ許可) を保ちたい

#### 教訓

- **permission system と workspace boundary の二重 gate は gap を生む**: 同じ
  semantics (= アクセス許可) を 2 つの独立したレイヤーが実装すると、 片方を
  bypass した時に他方が拒否する gap が生まれる
- **設計 review で「同じ semantics の gate は 1 箇所に集約」 という invariant を
  pin する候補**: boundary check が permission system の一部として実装されるべきか、
  または独立した boundary enforcement として残すべきかの判断を ADR 化する

---

### G15: eval_builder の stdlib path read permission gap
**Categories**: C5 (skill-design-vs-runtime-gap)
**Status**: **resolved** at B9 fix wave
**Discovered**: 2026-05-05 batch 8 (B8-S1)
**Related findings**: [B8-S1](2026-05-04-batch-8-cumulative-verify/findings/B8-S1-chain-completion.md), [B9-G15-diagnosis](2026-05-05-batch-9-fix-wave/findings/B9-G15-diagnosis.md), [B9-G15-fix-verify](2026-05-05-batch-9-fix-wave/findings/B9-G15-fix-verify.md)

#### 状況 (resolved)

診断で 2 つの root cause が判明:

**Hypothesis A (寄与因子)**: `startup_guard` の `_prompt_file_access` が non-interactive
モード (= piped stdin) で即 `return False` し、 `_session` に何も記録しない。
宣言された path が存在しても approval が保存されず、 runtime が deny する。

**Hypothesis B (主因)**: `invoke_sub_skill` が `permission_resolver` を `Agent` に
渡さない。 sub-skill の workspace は `self._perm = None` → CWD 外の全 path を
`_resolve_read` が PermissionError。

`f229f6c` (B8-NEW-1) で eval_builder/skill.md に declarations を追加済みだったが、
上記 2 因子で declarations が機能していなかった。

#### 試行
1. B9 fix: `startup_guard` 非 interactive auto-approve (file.read のみ) +
   `invoke_sub_skill` に `permission_resolver` parameter 追加 +
   `run_skill.py` handler が `ctx.permission_resolver` を伝播。
   7 Tier 2 tests added。 986 passed (baseline 979)。

---

### G16: router intent misrouting (semantic ambiguity, post-enum-fix)
**Categories**: C4 (cost-vs-reliability-policy) / C7 (prompt-vs-bloat-tradeoff)
**Status**: **resolved** at B9 fix wave
**Discovered**: 2026-05-05 batch 8 (B8-S5a)
**Related findings**: [B8-S5a](2026-05-04-batch-8-cumulative-verify/findings/B8-S5a-eval-builder-natural.md), [B9-G16-fix-verify](2026-05-05-batch-9-fix-wave/findings/B9-G16-fix-verify.md)

#### 状況

router enum fix (`9ee6ae1`) は B7 の dot-notation hallucinate (= 存在しない
`eval_builder.eval_md`) を排除した。 確定的に有効。

ただし B8 で **新 hallucinate variant** が出現: `direct_llm の eval を作って` という
input に対し router が `eval_builder` でなく `eval` skill (= run/evaluate 担当) を
選択。 enum 制約で名前空間は守られるが、 **semantic ambiguity (= 「eval」 keyword で
両 skill 競合)** は守られない。

`eval` skill は実際に起動・完走 (= judge_phase + narrator まで通った) ため、 表面上は
silently wrong 動作。 user 意図と完全乖離。

#### 試行
1. ✅ B9 fix: eval_builder description を `Build an eval spec...` に変更 (76 chars、 G12 safe)
   + when_not_to_use に create/run 対比 rule 追加
   + examples に `direct_llm の eval を作って` (positive) + `direct_llm を eval して` (negative) 追加
   + eval skill に対称的 when_not_to_use / examples 追加
   + 9 Tier 1 contract tests

#### 真の解
1. eval_builder の `when_to_use.negative` に「eval を『実行』する intent は eval skill を
   使う、 eval_builder は『eval spec を作成』する」 を追加
2. eval_builder の `description` を「Auto-generate an eval spec (eval.md) for a skill」
   から「**Build** an eval spec... (use `eval` skill to **run** evaluations)」 等に
   distinctive な動詞を含める
3. routing example phrase を「`<skill_name> の eval を作って`」 形式で eval_builder
   側に明示

#### 真の解 (中期、 構造的)
weak LLM の semantic disambiguation 限界に依存する問題、 強モデル併用 (= G4 trigger)
で大半解消する見込み。 短期は wording fix で patching、 G4 spike 評価で ROI 計測。

#### 着手 trigger
- batch 9 で B8-NEW-5 として fix dispatch
- skill.md edit + Tier 3 LLMReplay で同 input 5-shot で eval_builder invoke 確認

---

### G17: `_extract_skill_name` の unknown artifact_type 非対応
**Categories**: C5 (skill-design-vs-runtime-gap)
**Status**: **resolved** at B9 fix wave
**Discovered**: 2026-05-05 batch 8 (B8-S5b)
**Related findings**: [B8-S5b](2026-05-04-batch-8-cumulative-verify/findings/B8-S5b-eval-builder-structured.md), [B9-G17-fix-verify](2026-05-05-batch-9-fix-wave/findings/B9-G17-fix-verify.md)

#### 状況 (resolved)

batch 8 で preprocessor anyOf fix (`3cbe983`) は確認済 (= eval_builder が compile
成功、 analyze_skill phase 到達)。 ただし runtime で blocker:

invoke_skill の input に `type` field なし (= `{"target_skill": "direct_llm"}` のみ) で
渡された場合、 OS が `artifact_type="unknown"` に分類。 `analyze_skill_resolver.py:
_extract_skill_name` は `type == "eval_builder_request"` 分岐で `data.target_skill` を
取得する設計だが、 `unknown` 経路だと user_message regex fallback に落ちて、 `data` に
`text` field が無く ValueError。

#### 試行
1. B9 fix: `_extract_skill_name` を inverted structure に変更 (artifact_type で分岐
   → `"target_skill"` key 存在チェックを FIRST に)。 P7 保持のため OS 側変更なし。
   5 Tier 2 tests added。 991 passed (baseline 986)。

#### 解決内容

`data` に `"target_skill"` キーが存在する場合はそれを直接返す (artifact_type 不問)。
型チェックから field-presence チェックへの inversion。OS 側 logic に skill 固有 field
名を埋め込む案 (= P7 violation) は却下、skill-side fix で完結。

---

### G18: router tool function description 非 truncate (Pattern A 保険)
**Categories**: C7 (prompt-vs-bloat-tradeoff)
**Status**: active (low priority、 monitor)
**Discovered**: 2026-05-05 batch 8 (B8-S4)
**Related findings**: [B8-S4](2026-05-04-batch-8-cumulative-verify/findings/B8-S4-truncation-effect.md)

#### 状況

G12 truncation fix (`cdbd853`) は system prompt の `Available skills` section の
inline skill description を ≤80 chars に truncate する。 ただし **router tool function
descriptions** (= JSON tools schema の `function.description` field) は対象外。

batch 8 観測: `invoke_skill` = 349 chars、 `list_memory` = 187 chars、 `list_skills` =
206 chars。 verbose のまま。

batch 8 では 0/9 empty stop (= attractor 不在) で urgency 低い。 ただし Pattern A
(= 「verbose desc が attractor trigger」) が tool schema 経由でも発動する可能性は残る。

#### 試行
1. (なし、 monitor 中)

#### 真の解
- short term: 不要 (= 0 empty stop で trigger 不在)
- mid term: tool function descriptions も `MAX_DESC_LEN_FOR_LISTING` の対象に含める
  fix。 ただし tool schema は LLM の OpenAI tool call API で意味があるので、 truncate
  すると「LLM に何ができるか」 が伝わらず副作用ある。 慎重に評価
- long term: G4 trigger (= 強モデル併用) で empty stop 自体が消える + tool schema
  verbose による副作用が解消されれば不要

#### 着手 trigger
- Pattern A empty stop が再発した時 (= N=10 replay で >10% empty stop ratio)
- batch 9-N で empty stop frequency monitor、 trigger 発火しなければ低優先のまま

---

## メンテナンス

新規案件 discovery 時:
1. ID を採番 (G[N])
2. 表に行追加 + Next action 記入
3. 詳細 section を本 file に追記
4. 関連 finding doc に「→ giveup-tracker.md G[N]」 link を貼る

case が resolved した時:
1. status を resolved に変更
2. resolved 時の commit hash + 経緯を section に追記
3. 表は維持 (= history として)

各案件は **月 1 回** trigger 状況を review、 着手順序を再評価。
