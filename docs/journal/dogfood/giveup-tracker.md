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
| [G12](#g12) | attractor variant family (= weak LLM の MUST rule 確率的不honor) | C8 / C1 / C7 / C2 | **Pattern A/C/D 全解決** (B7 + B11-R2)、 **Pattern E (post-tool empty-stop) workaround landed at `aab6be2`** (2026-05-07)。 G4 spike 中期評価残。 | batch 5 retest 2 で 3 度目発生時に確定 (Pattern E は 2026-05-07 Wave A revert wave で判明) | Pattern E は envelope-layer `(answered)` inject で押さえ込み済 (= workaround、 真の fix は provider 改善待ち)。 中期: proxy 整備後 G4 spike → Option C 評価 |
| [G13](#g13) | `reyn chat` trusted python gap | C2 / C5 | **resolved** at `07ee851` | B6-S1-M1 dogfood retest (2026-05-04) | — (= `--allow-untrusted-python` flag 追加で `reyn run` と symmetry 確保) |
| [G14](#g14) | `Workspace.glob_files()` stdlib boundary reject | C5 | **resolved** at `f666acb` | B6-S1-M1 dogfood retest (2026-05-04) | — (= PermissionResolver consultation 追加、 stdlib path への explicit perm で opt-in) |
| [G15](#g15) | eval_builder の stdlib path read permission gap | C5 | **resolved** via documented design + dogfood pre-approval (B13、 2026-05-06) | B8-S1 (2026-05-05) | B9 fix `651a053` was REVERTED at B13 `1408f42` (= doc 違反)。 真の resolution は `reyn.local.yaml` layer 3 pre-approval pattern |
| [G16](#g16) | router intent misrouting (semantic ambiguity, post-enum-fix) | C4 / C7 | **resolved** via V3 wording (B13 `2bd9cbf`、 2026-05-06) | B8-S5a (2026-05-05) | B9 R3 fix (`330dd2a`) inconclusive (= 60% rate)、 B12-R2 N-shot diagnose で V3 (= ABSOLUTE rule + JA examples) を確定、 B13 で landing。 N=5 で 0% routing-fail verified |
| [G17](#g17) | `_extract_skill_name` の unknown artifact_type 非対応 | C5 | **resolved** via top-level priority check (B12 R1 `8f3bccf`、 2026-05-06) | B8-S5b (2026-05-05) | B9 R2 fix (`d1f2d30`) was wrong-layer trap (test pass + e2e 失敗)、 B12 R1 で OS runtime shape に合わせ top-level check を priority 1 に |
| [G18](#g18) | router tool function description 非 truncate (Pattern A 保険) | C7 | active (low priority) | B8-S4 (2026-05-05) | Pattern A 復活時の保険、 0 empty stop で urgency 低、 G18 として monitor |
| [G19](#g19) | write_eval artifact validation failure (B9-NEW-1) | C5 | **resolved-indirectly** | B9-S1 retest (2026-05-05) | B9-NEW-2 (G17) + G15 で indirect 解消。 B10-G19 diagnosis で確認 |
| [G20](#g20) | router invoke_skill duplication after run_skill failure (B9-NEW-3) | C3 | **resolved-indirectly** | B9-S1 retest (2026-05-05) | B9-NEW-2 (G17) + G15 で indirect 解消。 B10-G20 diagnosis で確認 |
| [G21](#g21) | copy_to_work preprocessor run_op permission_denied — stdlib CWD mismatch (B11-NEW-1) | C5 | **resolved** via documented design + dogfood pre-approval (B13、 2026-05-06) | B11-S2 (2026-05-06) | B12-R1 fix (`2219b20`) was REVERTED at B13 `b92a22c` (= doc 違反、 default zone overreach)。 真の resolution は `reyn.local.yaml` layer 3 pre-approval pattern |
| [G22](#g22) | `eval.run_target` literal model string bypasses proxy (B13-NEW-1) | C5 | **resolved** at B14-R1 fix (`a10553c`、 2026-05-06) | B13-S4 retest (2026-05-06) | B14-R1: `run_skill` で `ModelResolver.is_known_class` check + `ctx.model` fallback。 B14 N=5 で 3 fire 観察、 真に effective verified |
| [G23](#g23) | intent-axis section is load-bearing routing scaffold (Wave A revert evidence) | C7 / C1 | **resolved-by-revert + category-only retry SUCCEEDED** at `f4c5df2` (2026-05-07) | Wave A dogfood verify (2026-05-07) | Wave A 削除は cause を SP に誤認、 真因は G12 Pattern E (= post-tool empty-stop)。 envelope fix `aab6be2` 後に category-only retry (= 1 行 pointer + schema enum 信頼) を `f4c5df2` で landing、 N=10 で zero regression、 真の O(1) SP scaling 達成 |

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
**Status**: Pattern A/C/D 全解決 (2026-05-05 B11-R2)。 **Pattern E (post-tool empty-stop) workaround landed at `aab6be2` (2026-05-07)**。 G4 spike 中期評価。
**Discovered**: 2026-05-04 batch 5 retest 2 で attractor 3 度目発生時に確定
**Related findings**: B2-H1 / B3-H1 / B5-H1 / B5R2-H1 / B6-S2-observation.md (= 同 family の variant 系譜)
**Spike record**: `docs/journal/dogfood/g4-trigger-evaluation-spike.md`
**Design doc**: [ADR-0021](../../en/decisions/0021-g12-attractor-structural-fix-design.md) (2026-05-04)
**Insight (Pattern E)**: [envelope-layer attractor fix + mutation isolation methodology](../insights/2026-05-07-envelope-layer-attractor-fix.md) (2026-05-07)

#### Pattern E workaround (= post-tool empty-stop、 envelope-layer fix、 commit `aab6be2`)

**問題**: gemini-2.5-flash-lite via LiteLLM proxy が OpenAI tool_use compat path で
`role=tool` 受信後の turn で 0 completion tokens / finish_reason=stop の empty-stop
attractor を高頻度で発火。 N=10 measurement で 30-100% range (= 確率的、 deterministic-leaning)。
remember_shared / list_skills 等、 全 tool 経路で発生。

**仮説検証 (= V0-V11 mutation testing、 `/tmp/wave_a_trailing_user.py` evidence)**:
- V0 baseline (no trailing message): empty_stop 30-60%
- V1 `"(continue)"`: empty_stop 0%、 ただし **70-100% で同 tool 再 invoke** (= duplicate save 副作用)
- V4 元 user query repeat: empty_stop 0%、 100% text reply、 0% duplicate (clean だが cost 大)
- V6 `"Reply in text now."`: 0% / 100% / 0%、 ただし「reply 強制」 = multi-tool chain blocker
- **V7 `"(answered)"`**: empty_stop 0%、 100% text reply、 0% duplicate (= **neutral state signal、 chain freedom 保持**)
- V8 `"Now confirm."`: 100% duplicate tool (= 命令系 NG)
- V9 `"thanks"`: 100% replied だが reply が "You're welcome!" (= UX ずれ)

**選定 = V7 `"(answered)"`**: parenthesised neutral signal (= "前 turn の question 応答済"、
imperative ではない)、 LLM は次 turn で text reply / next tool の選択自由。

**実装 (`src/reyn/llm/llm.py:call_llm_tools`)**:

```python
if messages and isinstance(messages[-1], dict) and messages[-1].get("role") == "tool":
    messages = messages + [{"role": "user", "content": "(answered)"}]
```

= envelope-layer 介入のみ、 SP 一切触らず、 chat history は inject 前のまま (= clean)。

**Dogfood verify (N=10 each、 real `reyn chat` subprocess)**:

| scenario | baseline | post-workaround | Δ |
|---|---|---|---|
| W1 invoke V3 pattern | ~80% | **90%** | +10pp ✓ |
| W2 web_search | ~90% | **100%** | +10pp ✓ |
| W3 chitchat | 100% | 100% | 0 |
| W4 capabilities | 100% | 100% | 0 |
| **W5 remember (Pattern E 直撃)** | 30-50% | **100%** | **+50pp** ✓✓ |
| W6 recall query | n/a | 100% | new ✓ |
| **W7 unnamed task post-list_skills (Pattern E)** | 10% | **70%** | **+60pp** ✓✓ |

= W5 / W7 で Pattern E 完全消滅、 W1 baseline 死守、 W2 副次効果 +10pp。

**workaround 認識 (= 真の fix ではない)**:
- 原因は provider quirk (= Gemini OpenAI compat path) で Reyn 制御外
- model 改善 / 別 provider 移行で obsolete 候補
- multilingual 副作用: 英語 directive `"(answered)"` が reply 言語を英語に bias する可能性 (=
  「workaround を多言語 engineering しない」 割り切り、 user 同意済)
- code に「workaround」 明示 comment 必須 (= ✓ commit に含む)

**学び (= multi-perspective review が funnel の核)**:
- Wave A 削除アプローチ + trigger phrase richer 仮説、 両方とも N≥10 で refuted (= -40〜-50pp regression)
- 「prompt 構造変化で attractor が re-shuffle」 という非線形性、 SP 弄りでは常に regression risk
- envelope-layer fix は **care boundary 整合** (= P3 + pre-call structural environment = OS responsibility)
- 業界 practice (= Anthropic Skills L1 metadata / OpenAI Tool Search namespaces / MCP-Zero hierarchical) も「LLM への注文」 路線では解決していない、 envelope / RAG 層が真の fix layer
- N=10 measurement の noise window は ±10-20pp 大きい、 V0 baseline 30%〜100% range で振れた → 数字 1 経路だけで結論せず multi-path 検証必須

#### Pattern E 観測 (post-OSS HN dogfood、 2026-05-07)

post-OSS HN first-touch dogfood (`hn_demo` agent on v7 trace `c98a685a`)
で Q4 「list available skills」 / Q7 「explain chat router」 / Q8
「Reyn vs LangGraph」 の empty-stop が 11 query 中 3-4 件観測された。

私の最初の解析プロセスは **streaky N=5 sampling を deterministic な
quirk と誤解** して以下のような複雑な仮説 stack を積み上げた:

- 仮説 1: gemini-2.5-flash-lite の tool_use → narration transition の
  wire-format quirk (= role=tool last + tools array で empty stop)
- 仮説 2: LiteLLM の OpenAI → Vertex 翻訳 multi-turn bug
- 提案: synthetic trailing user message inject (= ADR-0021 violation 候補)

user の Occam's razor 反論で正解判明。 **真の状況**:

- N=20 measurement で確認: gemini-2.5-flash-lite multi-turn tool result
  narrate rate = **17/20 ≒ 85%** (= 普通の weak-LLM noise)
- つまり empty stop は ~15% の base rate で stochastic に発火する G12
  の subset、 deterministic bug ではない
- 私の N=5 で 0/5 連続観測は streaky luck (= 11 hypothesis 全て偶然 streak
  に hit) を不適切に「 deterministic 」 と結論付けた **methodology
  artifact**
- LiteLLM / gemini / Reyn いずれにも構造的 bug は無い、 messages array
  は OpenAI tool_use 契約 satisfied、 wire body は curl と一致

**得られた discipline 教訓**:

- **N≥10 で attractor rate を測定**、 N=5 では 70-90% rate でも 0/5 が
  普通に出る (= 5 連続 0 の確率は 0.15^5 = 7.6e-5、 11 hypothesis 連続
  も 11 streak しか観測してないなら統計的に十分有り得る)
- **「 deterministic 」 を結論付ける前に N≥10 + 複数試行 が必須**
- **streaky data の上に構造仮説を積まない**、 まず rate を pin する
- 「公開未報告の重大欠陥」 を仮説に含める時は Occam's razor で simpler
  hypothesis (= 自分の testing methodology の artifact) を優先で verify

**実装変更**: なし (= G12 受容、 default model 据え置きで財布優先)。
README に honest note 追加 (= weak default model で時々 reply が空になる、
operator が reyn.yaml で stronger model に切り替え可)。

cross-ref: `feedback_minimize_speculation.md` /
`feedback_observe_before_speculate_llm.md` /
`feedback_verify_reproduce_first.md` の原則を **N-shot measurement**
discipline で補強。

#### Pattern D 解決 (B11-R2、 2026-05-05)

B10-S5b で `describe_skill("eval_builder")` → empty-stop が 25% per session で観測。 B11-R2 で:

- **Pattern D 確定**: `_describe_skill()` が routing block (780-1400 chars) を含む full catalogue entry を返却 → last tool_response が P-b threshold を超過
- **N-shot measurement**: N=10 replay on B10-S5b trace → 50% G12 rate (5/10) at HEAD `4898ef9`
- **Hypothesis B confirmed**: routing strip patch (1381→187 chars) → 0/10 (0%) empty-stop
- **Fix**: `_describe_skill()` に `_DESCRIBE_SKILL_STRIP_FIELDS = frozenset({"routing", "category"})` フィルタ追加
- **Tests**: `test_router_skill_description_truncation.py` (+2 new)、 `test_router_describe_skill_routing_strip.py` (+3 new)
- **Full suite**: 1015 passed、 2 xfailed (B11-R2 post-fix)

参照: `B11-R2-g12-diagnosis.md` / `B11-R2-g12-fix-verify.md`

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
**Status**: **resolved** via documented design + dogfood pre-approval pattern (B13、 2026-05-06)
**Discovered**: 2026-05-05 batch 8 (B8-S1)
**Related findings**: [B8-S1](2026-05-04-batch-8-cumulative-verify/findings/B8-S1-chain-completion.md), [B9-G15-diagnosis](2026-05-05-batch-9-fix-wave/findings/B9-G15-diagnosis.md), [B13-R1-revert-g15](2026-05-06-batch-13-revert-and-real-milestone/findings/B13-R1-revert-g15.md)

#### 経緯

**B8-S1 (2026-05-05)**: dogfood で eval_builder analyze_skill が stdlib path read で
permission_denied 多発、 chain 進行 blocker と判明。

**B9 fix wave (`651a053`、 後に revert)**: 2 changes を導入:
- (1) `startup_guard._prompt_file_access` 非 interactive で auto-approve (file.read のみ)
- (2) `invoke_sub_skill` に `permission_resolver` parameter 追加 + `run_skill.py`
  handler が `ctx.permission_resolver` を伝播

(1) は dogfood で機能したが、 後に **documented permission model 違反** と判明
(= `docs/en/concepts/permission-model.md` に non-interactive auto-approve は記載なし、
documented design は「approvals must be in place beforehand」 = pre-approval 必須)。

**B13 revert (`1408f42`、 2026-05-06)**: change (1) を **revert**、 change (2) は
documented design で sub-skill が permission resolver を持つことが necessary なので
**keep**。 7 Tier 2 tests removed (= G15-specific test file)。

#### 真の resolution (= documented design 内)

dogfood 自動化 (= sonnet 並列 + piped stdin) は documented layer 3 mechanism
(= `reyn.local.yaml` の `permissions: file.read: allow` 等) で pre-approval を
入れて運用。 production user (= interactive TTY) は startup_guard prompt が
documented 通り機能。

詳細: `docs/en/concepts/permission-model.md` の "reyn.local.yaml for operator-local
pre-approval" section (= B14 で doc 化)。

#### 教訓

- **「fix が dogfood で機能した」 ≠ 「documented design 整合」**: 既存 design に
  記載のない behavior を fix で導入すると complexity が累積、 user 視点の simplicity
  smell test (B13) で発見されるまで気づきにくい
- **fix dispatch 前の documented design 整合性 audit が必要** (= B13 で確立した新原則)
- B9-NEW-1 (= 当時の問題) の真因は documented design の operational gap (=
  dogfood のような non-interactive workflow への適用方法が doc 不足) で、
  fix で workaround するのでなく doc + 運用 pattern で対応すべきだった

---

### G16: router intent misrouting (semantic ambiguity, post-enum-fix)
**Categories**: C4 (cost-vs-reliability-policy) / C7 (prompt-vs-bloat-tradeoff)
**Status**: **resolved** via V3 wording fix (B13、 2026-05-06)
**Discovered**: 2026-05-05 batch 8 (B8-S5a)
**Related findings**: [B8-S5a](2026-05-04-batch-8-cumulative-verify/findings/B8-S5a-eval-builder-natural.md), [B9-G16-fix-verify](2026-05-05-batch-9-fix-wave/findings/B9-G16-fix-verify.md), [B12-R2-diagnosis](2026-05-06-batch-12-real-milestone/findings/B12-R2-diagnosis.md), [B13-R3-v3-wording-fix](2026-05-06-batch-13-revert-and-real-milestone/findings/B13-R3-v3-wording-fix.md)

#### 経緯

**B8-S5a (2026-05-05)**: router enum fix (`9ee6ae1`) で B7 の dot-notation hallucinate
(= 存在しない `eval_builder.eval_md`) は排除済。 ただし `direct_llm の eval を作って`
input で router が `eval_builder` でなく `eval` skill (= run/evaluate 担当) を選択する
**新 hallucinate variant** 出現。 enum 制約で名前空間は守られるが semantic ambiguity
は守られず、 silently wrong 動作。

**B9 fix attempt (`330dd2a` の R3 wording fix、 後に effective でない判定)**:
eval_builder description を `Build an eval spec...` に変更 + when_not_to_use に
create/run 対比 + examples 追加 + 9 Tier 1 tests。 ただし B11 N=5 で 60% rate
text-reply 残存、 batch 11 retro で「inconclusive」 判定。

**B12-R2 diagnose (2026-05-06)**: N=10 N-shot で hypothesis A/B/C/D 比較:
- baseline 40-50% text-reply rate
- V3 (= ABSOLUTE rule + Japanese routing examples) で **5% rate** 達成
- structurally-fixable と確定 (= G4 trigger 不要)

**B13 V3 wording fix landing (`2bd9cbf`、 2026-05-06)**: 🟡 仕様変更 (= router
routing semantics 強化)。 `router_system_prompt.py` Behaviour section に:
```
ROUTING RULE (ABSOLUTE): When ANY Available skill name appears in the
user message, call invoke_skill with that skill name immediately.
NO clarifying questions. NO text replies. Examples:
  「<skill_name> で <target> を review して」 → invoke_skill(name=<skill_name>)
  「<skill_name> で <X> を作って」 → invoke_skill(name=<skill_name>)
```
P7 compliant (= `<skill_name>` placeholder で skill 固有名なし)。 1 Tier 2 contract
+ 4 fixture rekey。

**B14 N=5 verification (2026-05-06)**: 5/5 sessions で 0% routing-fail (= V3 真に
verified)。 batch 11 で「inconclusive」 と判定した B9 R3 fix も retroactive verify
で「真に effective」 と確定 (= V3 wording 強化と組み合わせで完全解消)。

#### 真の resolution

V3 wording fix (= ABSOLUTE rule + Japanese routing examples) で 40-50% → 5% (N-shot
measured) → 0% (N=5 e2e) 達成。 G4 trigger 不要、 weak LLM 環境での wording fix の
limit 内で structural fixable と data 化。

#### 教訓

- **wording fix の effective 確率 hierarchy**: B9 R3 fix は inconclusive、 B13 V3
  fix は verified — 同じ wording layer での fix でも framing 強度で大きく変わる
  (= 「If A then B」 vs 「ROUTING RULE (ABSOLUTE) ... NO X. NO Y.」)
- **N-shot diagnose で fix を ROI evaluate**: B12-R2 で 4 wording variant を N=10
  比較したことで V3 を確定的に選択可能、 fix 投入の hit 確率を上げた
- **「inconclusive」 → 「retroactive verified」 への格上げ**: 別 layer fix が
  landing してから prior fix の真の effect が測定可能になる場合あり、 calibration
  も updates 必要

---

### G17: `_extract_skill_name` の unknown artifact_type 非対応
**Categories**: C5 (skill-design-vs-runtime-gap)
**Status**: **resolved** at top-level fix (B12 R1 / B9-NEW-2、 `8f3bccf`、 2026-05-06)
**Discovered**: 2026-05-05 batch 8 (B8-S5b)
**Related findings**: [B8-S5b](2026-05-04-batch-8-cumulative-verify/findings/B8-S5b-eval-builder-structured.md), [B9-G17-fix-verify](2026-05-05-batch-9-fix-wave/findings/B9-G17-fix-verify.md), [B9-S5b-retest](2026-05-05-batch-9-fix-wave/findings/B9-S5b-retest.md)

#### 経緯

**B8-S5b (2026-05-05)**: `invoke_skill(input={"target_skill": "direct_llm"})` で
`type` field なしで渡された場合、 OS が `artifact_type="unknown"` に分類、
`analyze_skill_resolver.py:_extract_skill_name` が user_message regex fallback に
落ちて ValueError。

**B9 R2 fix attempt (`d1f2d30`、 後に wrong-layer trap と判明)**: `_extract_skill_name`
を field-presence-first inversion に変更、 5 Tier 2 tests pass。 ただし test fixture が
`{"type": "unknown", "data": {"target_skill": "..."}}` (= wrapped form) を assume、
実際の OS runtime は `{"target_skill": "...", "eval_spec": {...}}` (= top-level、
no `data` wrapper) を生成。 fix は `data["target_skill"]` を check、 runtime では空 dict
に fall through で同じ ValueError 継続。

**B9 retest (B9-S5b、 2026-05-05)**: e2e で同 ValueError 確認、 **「test pass + e2e
失敗」 wrong-layer trap** として確定。 B9 retro で「fix verify は per-fix Tier 3 e2e
cross-check 必須」 という discipline 確立。

**B12 R1 fix (`8f3bccf`、 2026-05-06)**: `_extract_skill_name` に **3-priority check**:
1. `artifact["target_skill"]` (= top-level、 OS runtime shape)
2. `artifact["data"]["target_skill"]` (= wrapped/legacy form)
3. text regex fallback (= top-level OR data の text field 経由)

5 Tier 2 tests 追加 (= top-level shape を test、 既存 wrapped-form test も legacy
guard として残存)。 B12 Step 1 e2e で **真の verified** 確認。

#### 真の resolution

`8f3bccf` の top-level priority check で OS runtime shape に対応。 wrapped form
(= legacy / typed eval_builder_request) も 2nd priority で残存、 backward compat 維持。
P7 compliant (= OS 側に skill 固有 field name 埋め込まず、 skill-side fix のみ)。

#### 教訓

- **wrong-layer trap は test pass + e2e 失敗 の最 dangerous な fix pattern**:
  test fixture が runtime artifact 構造と乖離している場合、 test は self-consistent
  に pass、 ただし e2e で同じ error 継続
- **Tier 2 OS invariant test の fixture は runtime data で cross-check が必要**:
  Tier 3 LLMReplay test を併用、 もしくは fixture 設計時に「runtime で OS が
  生成する artifact 構造を grep で確認」 を ritual 化
- **「fix verify は per-fix Tier 3 e2e cross-check 必須」** discipline (= B9 retro
  で確立) の motivation 起源、 batch 12 M2 audit で systematic に他 fix を check
  する trigger となった

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

---

### G19: write_eval artifact validation failure (B9-NEW-1)
**Categories**: C5 (design-choice-explicit)
**Status**: **resolved-indirectly**
**Discovered**: B9-S1 retest (2026-05-05)
**Related findings**: [B9-S1-retest.md](2026-05-05-batch-9-fix-wave/findings/B9-S1-retest.md), [B10-G19-diagnosis.md](2026-05-05-batch-10-residual-fix-wave/findings/B10-G19-diagnosis.md)

#### 観測

B9-S1 retest で `write_eval` phase が 3 回 artifact validation failure:
`"Artifact data validation failed for 'eval_spec_result'"`. `case_count: 0` が
schema constraint `minimum: 1` に違反。

#### 真因 (downstream symptom)

因果連鎖:
1. B9-NEW-2 (`compute_paths ValueError`) → `run_skill(eval_builder)` が 3 回 fail
2. 3 回目の試行では analyze_skill が permission_denied を経て degenerate completion
3. LLM が `test_cases: []` (empty) の skill_analysis を emit → write_eval で
   `case_count=0` → schema violation

B9-NEW-2 fix (`8f3bccf`) + G15 (startup_guard auto-approve) で根本原因が解消。

#### 解消確認

B10-G19 diagnosis にて `reyn run skill_improver` で full chain 完走 (write_eval
passed, case_count=3). B9-NEW-1 は単独 bug でなく B9-NEW-2 の downstream symptom
として confirmed。

---

### G20: router invoke_skill duplication after run_skill failure (B9-NEW-3)
**Categories**: C3 (architectural-complexity)
**Status**: **resolved-indirectly**
**Discovered**: B9-S1 retest (2026-05-05)
**Related findings**: [B9-S1-retest.md](2026-05-05-batch-9-fix-wave/findings/B9-S1-retest.md), [B10-G20-diagnosis.md](2026-05-05-batch-10-residual-fix-wave/findings/B10-G20-diagnosis.md)

#### 観測

B9-S1 retest (HEAD `330dd2a`) で `invoke_skill(skill_improver)` が T+141s / T+147s /
T+157s に重複発行。 `run_skill(eval_builder)` が 3 回 fail した後 `copy_to_work`
phase 完了時にルーターが再 invoke。

#### 真因 (downstream symptom)

B9-NEW-2 (`compute_paths ValueError`) が `run_skill(eval_builder)` の連続失敗
(~25s × 3 = ~75s failure cascade) を引き起こし、 chain が prolonged 状態に。
この状態で `copy_to_work` 完了 → router 再呼び出し → context なしで
`skill_improver` を再 invoke するパターンが発生。

B9-NEW-2 fix (`8f3bccf`) + G15 で failure cascade が解消 → chain が 18s 以内で
完走するため、 prolonged execution window が消滅。

#### 既存 safeguard (B9-NEW-3 が独立 bug でない理由)

- **G10 fix** (router_loop.py L267-301): `invoke_skill` が `status=error` を返した
  時点で router loop が即 return。 LLM に error を見せず retry させない
- **dispatcher.py 正規化**: `dispatch_tool` は全例外を `{"status": "error", ...}`
  に正規化。 例外が raw で router_loop に届くことはない
- **G3 fix** (`_dedupe_tool_calls_round`): 同一 round 内の重複 `invoke_skill` を
  dedupe (same-LLM-call scope)

これら safeguard で normal operation における failure 伝播は完全に cover 済み。

#### 解消確認

B10-G19 diagnosis で `reyn run skill_improver` にて full chain 完走 + 重複なし
を確認。 B10-G20 diagnosis で structural analysis + no-reproduce verdict。

#### 将来注意点

cross-session chain tracking (= long-running job で router が複数セッションにわたる
場合) は PR21 (crash recovery) の scope で対処すべき課題。 B9-NEW-3 として
dedupe patch を追加する必要なし。

---

### G21: copy_to_work preprocessor run_op permission_denied — stdlib CWD mismatch (B11-NEW-1)
**Categories**: C5 (workspace abstraction inconsistency)
**Status**: **resolved** via documented design + dogfood pre-approval (B13、 2026-05-06)
**Discovered**: 2026-05-06 batch 11 (B11-S2、 runs 1 and 4)
**Related findings**: [B12-R1-diagnosis.md](2026-05-06-batch-12-real-milestone/findings/B12-R1-diagnosis.md), [B12-R1-fix-verify.md](2026-05-06-batch-12-real-milestone/findings/B12-R1-fix-verify.md), [B13-R2-revert-stdlib-default-zone.md](2026-05-06-batch-13-revert-and-real-milestone/findings/B13-R2-revert-stdlib-default-zone.md)

#### 観測

B11-S2 2 partial runs (runs 1 and 4) が `copy_to_work` preprocessor step[1] で
停止:

```
Phase 'copy_to_work' preprocessor step[1] run_op (file): read from
'<main_repo>/src/reyn/stdlib/skills/direct_llm/skill.md' was not approved.
```

step[0] (`python compute_paths`) は成功、 step[1] (`run_op file glob`) で PermissionError。

#### 根本原因

**editable install + git worktree CWD mismatch**:

1. `reyn chat` は git worktree から起動: CWD = `.../sandbox_2/.claude/worktrees/<id>/`
2. `startup_guard` は宣言 path `"src/reyn/stdlib/skills"` を worktree-relative に
   解決 → CWD 内に収まる → `session_approve_path` をスキップ
3. `compute_paths` が `stdlib_root()` を呼ぶ:
   `Path(__file__).parent.parent / "stdlib"` → インストール済パッケージの絶対 path
   (main repo `.../sandbox_2/src/reyn/stdlib`)
4. `run_op (file.read)` が該当絶対 path に対して `_in_default_read_zone()` を呼ぶ
5. `_in_default_read_zone` は `Path.cwd()` (= worktree) に対して `relative_to` を試みる
   → ValueError → False を返す
6. session_approve_path にも entry なし → PermissionError

batch 10 Run 2 が偶発的に成功した理由: その run では CWD が main repo だったため
stdlib path が CWD 内に収まった (N=1 lucky case)。 batch 11 5-shot で 0/5 complete
rate という data が systematic な blocker であることを確認。

#### 試行 (なし)

即 diagnosis → 1 仮説 1 修正 で landing。

#### B12 試行 → B13 で revert

**B12-R1 fix attempt (`2219b20`、 後に revert)**: `_in_default_read_zone` に
`stdlib_root()` を second default zone として追加。 lazy initialization で
循環 import 回避。 6 Tier 2 tests + 8 updated。 dogfood で fire しなくなり
batch 12 で「resolved」 と判定。

**B13 revert (`b92a22c`、 2026-05-06)**: user の simplicity smell test 介入で
**documented permission model 違反** と判明 (`docs/en/concepts/permission-model.md`):

> **Layer 1: defaults** — Read/glob/grep anywhere under the project root.

= **default zone は project root のみ**、 stdlib path は declared (= layer 2) で
対応する設計。 layer 1 を expand すると documented design の trust model
(= 「user が事前に判断」) を密かに緩める effect があり、 production user に
影響。

revert: `_in_default_read_zone` を CWD ancestry のみに復帰。 6 R1-specific tests
削除 + 2 updated。

#### 真の resolution (= documented design 内)

dogfood 自動化 (= sonnet 並列 + piped stdin、 worktree-based dev) は
**`reyn.local.yaml` の layer 3 pre-approval mechanism** で対応:

```yaml
# reyn.local.yaml (= operator-personal、 git 管理外)
permissions:
  file:
    read: allow
  python:
    pure: allow
    trusted: allow
```

production user (= interactive TTY) は startup_guard prompt が documented 通り
機能、 dogfood は config 経由で pre-approval。 layer 1 (default zone) を変えず、
documented design 整合性維持。

詳細: `docs/en/concepts/permission-model.md` の "reyn.local.yaml for operator-local
pre-approval" section (= B14 で doc 化)。

#### 教訓

- **「fix が dogfood で機能した」 ≠ 「documented design 整合」**: B12 R1 fix も
  G15 fix (`651a053`) と同じ pattern、 documented behavior にない special case を
  introduction することで本来の design simplicity を密かに崩していた
- **「default zone を expand する fix」 は trust model に影響大**: layer 1 の
  expand は user の事前判断 (= layer 2 declaration approve) を skip する path
  を作る、 文字通り project-wide な silently broadening
- **「真因」 は実装 bug でなく **operational gap 用 doc 不備** だった**: dogfood
  のような non-interactive workflow への運用が doc に書かれていなかった、
  fix で workaround するのでなく doc + 運用 pattern (= reyn.local.yaml) で
  対応すべきだった
- batch 13 で確立した「documented design 整合性 audit を fix dispatch 前に
  必須」 原則の motivation 起源
- **「N=1 で動いた」 は stability 保証にならない**: batch 10 Run 2 が偶発的に main
  repo CWD で起動したため pass したが、 これは B11-NEW-1 の存在を隠蔽した
- **editable install + git worktree の組み合わせは path resolution で hidden assumption
  を生む**: `Path(__file__)` 系の path は常に installed package の absolute path を
  返す → CWD に依存する permission check と mismatch する可能性がある

---

### G22: `eval.run_target` literal model string bypasses proxy (B13-NEW-1)
**Categories**: C5 (skill-side intent vs LLM hallucinate)
**Status**: **resolved** at B14-R1 fix (`a10553c`、 2026-05-06)
**Discovered**: 2026-05-06 batch 13 (B13-S4 retest、 1/5 partial cause)
**Related findings**: [B13-S4-stability-5shot.md](2026-05-06-batch-13-revert-and-real-milestone/findings/B13-S4-stability-5shot.md), [B14-R1-diagnosis.md](2026-05-06-batch-14-stability-extension/findings/B14-R1-diagnosis.md), [B14-R1-fix-verify.md](2026-05-06-batch-14-stability-extension/findings/B14-R1-fix-verify.md)

#### 経緯

**B13-S4 (2026-05-06、 N=5 で 4/5 complete)**: 1 partial の真因が `eval.run_target`
phase の `run_skill` op で **LLM hallucinate した literal model string**
(`"openai/gpt-3.5-turbo"`) を proxy が直接 reject。 `ModelResolver.resolve()` は
unknown string を passthrough する documented behavior、 ただし proxy は該当 model
を持っていないので reject。 chain abort で 1 partial。

**B14-R1 fix (`a10553c`、 2026-05-06)**: 🔵 不具合修正 (= 意図と実装の乖離訂正)。
documented intent は「skill は model class (= `light` / `standard` / `strong`) を
使い、 proxy で実 model に解決」。 実装は `op.model` を passthrough、 unknown class
が proxy を bypass する path が空いていた。

Fix design:
- `ModelResolver.is_known_class(name)` 追加 (= boolean check)
- `run_skill.py` で `op.model` が known class でなければ warning log + `ctx.model`
  fallback
- 4 Tier 1 (ModelResolver) + 6 Tier 2b (run_skill model selection) tests 追加

P3 + P7 compliant: OS layer で intercept、 skill 固有 string なし、 LLM への
instruction 変更なし、 warning log で operator visibility 確保。

#### 真の resolution

B14 N=5 retest で 3 fire 観察 (= Run 2 で `gpt-3.5-turbo` を 2 回 + Run 3 で
`gemini-2.5-flash-lite` を 1 回)、 全 fire で正常 fallback して chain 完走。
batch 13 で 1 partial だった原因が **真に effective** に解消、 N=5 で **5/5 (100%)
complete** = production-grade phase 1 完了 milestone へ contribute。

#### 教訓

- **「LLM hallucinate を OS で transparent 救済」 は P3 + P7 compliant fallback の
  template pattern**: skill 固有名 / instruction 変更なしで OS boundary intercept、
  warning log で visibility、 documented intent と整合
- **fix の operational evidence は real LLM dogfood で fire するか で確定**:
  test-only verification では effective か未確定、 N-shot で fire を観察すると
  fix の structural 価値が確認される
- **literal model string passthrough は historical convenience だった**: 元々の
  `ModelResolver.resolve()` の passthrough は unknown class 時の graceful
  degradation を意図したが、 proxy 環境では「unknown = reject される」 という
  reality と不整合、 fail-fast 寄り (= warning + fallback) が安全

---

### G23: intent-axis section is load-bearing routing scaffold → category-only retry SUCCEEDED
**Categories**: C7 (prompt-vs-bloat-tradeoff、 主因) / C1 (model-capability-tradeoff)
**Status**: **resolved-by-revert + category-only retry SUCCEEDED** at `f4c5df2` (2026-05-07)
**Discovered**: 2026-05-07 Wave A dogfood verify (post-`dc8296f`)
**Related**: [Wave A commit `dc8296f`](https://github.com/tya5/reyn/commit/dc8296f) (= reverted)、 [revert commit `589e50f`](https://github.com/tya5/reyn/commit/589e50f)、 [G12 envelope workaround `aab6be2`](https://github.com/tya5/reyn/commit/aab6be2)、 [**category-only retry success `f4c5df2`**](https://github.com/tya5/reyn/commit/f4c5df2)
**Insights**: [envelope-layer attractor fix + mutation isolation methodology](../insights/2026-05-07-envelope-layer-attractor-fix.md) / [industry tool discovery patterns survey](../insights/2026-05-07-industry-tool-discovery-survey.md) (2026-05-07)

#### 経緯

Wave A `dc8296f` (= 2026-05-07): router system prompt から 58 行の
`## What you can do (intent axis)` section を削除し、 同等の grouped catalog を
on-demand で返す `discover_tools` tool を追加。 prompt size を 5162 → 4526 chars
(-12.3%) に圧縮、 1271→1275 tests pass、 LLMReplay fixture 4 件 re-record で
green landing。

Dogfood verify (= N=10 / scenario × 5 scenario):

| scenario | PRE Wave A (`6b41fd0`) | POST Wave A (`dc8296f`) | Δ |
|---|---|---|---|
| W1 invoke V3 pattern | **80% invoke** (8/10、 leak 2) | **40% invoke** (4/10、 leak 6) | **-40pp** |
| W2 web_search | (assumed healthy) | 90% verified | ~ |
| W3 chitchat | 100% reply | 100% reply | 0 |
| W4 capabilities | 100% reply | 100% reply | 0 |
| W5 remember (G12) | 30% replied (= 70% empty-stop) | 50% replied (= 50% empty-stop) | +20pp 改善 |

W1 の Gemini `<ctrl42>` format leak (= LLM が tool_use OpenAI format ではなく
Gemini native function-call syntax を text に流出) 率が 20% → 60% に倍増。

Cross-condition isolation:

| config | invoke success | leak rate |
|---|---|---|
| A0 PRE baseline | 8/10 | 2 |
| A1 POST Wave A (full) | 4/10 | 6 |
| A2 post-SP + pre-tools (no `discover_tools` tool) | 2/10 | 8 |
| A3 pre-SP + post-tools (with `discover_tools` tool) | 7/10 | 3 |
| A4 post-SP + post-tools-without-`discover_tools` schema | 4/10 | 6 |

A2 (post-SP only) が最悪 = **system prompt 変更が leak の cause**、 `discover_tools`
tool 追加自体は無害 (= A3 baseline 近い)。

#### 真の resolution

Wave A revert (`589e50f`) で baseline 復帰。 1271 passed maintained。

#### 教訓

- **「intent-axis section は load-bearing routing scaffold」**: tool 名を group
  ごとに enumerate する section は cosmetic な categorization ではなく、 weak
  LLM (= gemini-2.5-flash-lite) が OpenAI tool_use format を choose するための
  structural cue。 削除で LLM が Gemini native syntax 経路に逃げる
- **「prompt size 圧縮 = 純粋な利得」 は誤り**: -12.3% reduction を取りに行ったら
  -40pp routing reliability を失った。 prompt 内容は signal density で評価すべき、
  char count だけ見るのは care boundary 違反 (= structural environment を
  カットしすぎ)
- **dogfood verify discipline (= N≥10、 LLM context + resp 観測、 最小パス探索)
  が production-grade phase 1 milestone 後も valid**: test green + LLMReplay
  re-record では「routing が効いてる」 と保証できない、 real LLM で N≥10 必要
- **discover_tools tool 単独は無害だが intent-axis を replace できない**: tool
  description 経由の lazy catalog は routing decision の structural cue にならない、
  prompt-level enumeration が必要

#### post-revert 追跡実験: trigger phrase richer 仮説も refuted (2026-05-07)

revert 後 「intent-axis 維持 + Behaviour に category routing trigger 追加」 路線を
試した (= +958 chars、 SP 6120 chars に膨張)。 N=10 dogfood:

| scenario | baseline | trigger 追加 | Δ |
|---|---|---|---|
| W1 invoke V3 | ~80% | 30% | -50pp ✗✗ |
| W7 unnamed task | n/a | **10%** (= G12 Pattern E 暴露) | new ✗ |

→ trigger 追加でも W1 が -50pp regression、 さらに W7 で post-list_skills G12 が
80% empty-stop で発火。 「prompt 構造変化 (削除 / 追加問わず) で attractor が
re-shuffle」 という非線形性が確定。

**真の root cause = G12 Pattern E** (= post-tool empty-stop attractor、 routing layer
ではない)。 SP 内介入は G12 を放置する限り masked、 真の effect が測れない。

#### 解決経路 (= envelope-layer fix が前提整備)

`aab6be2` で G12 Pattern E に envelope-layer workaround (= post-tool に `(answered)`
inject) が landing。 W5 / W7 の G12 暴露が解消、 W1 baseline 維持。 → **SP 圧縮系
(= category-only catalog) の retry は この fix の上で再評価可能** な状態に。

業界 practice (= MCP-Zero hierarchical / Anthropic Skills L1 metadata / OpenAI Tool
Search namespaces) は category-level lazy loading が確立しており、 G12 fix が
下にあれば Reyn でも retry の path が開ける。 ただし retry も N≥10 dogfood + LLM
context 直接観測 + 最小パス探索 の discipline は変わらず必要。

#### category-only retry SUCCEEDED (= `f4c5df2`、 2026-05-07 同 session)

G12 envelope fix が下にある状態で category-only catalog を改めて試行:

実装:
- `## Available skills (N)` の inline name + description list を **`## Skills (N
  available) — call list_skills(path) to browse...` の 1 行 pointer に置換**
- Behaviour rule で `Available skills list` 参照を `invoke_skill enum` 参照に書き換え
- ROUTING RULE (ABSOLUTE) も同じく enum 参照、 example 文言保持
- Memory section は inline 維持 (= 小 N stable、 user 設計判断)

Dogfood verify (N=10 each、 real `reyn chat` subprocess):

| scenario | G12 envelope only baseline | + category-only retry | Δ |
|---|---|---|---|
| W1 invoke V3 pattern | 90% | **90%** | 0 (= baseline 死守) |
| W2 web_search | 100% | **100%** | 0 |
| W3 chitchat | 100% | **100%** | 0 |
| W4 capabilities | 100% | **100%** | 0 |
| W5 remember (G12 W5) | 100% | **100%** | 0 (= envelope fix 維持) |
| W6 recall query | 100% | **100%** | 0 |
| W7 unnamed task (G12) | 70% | **70%** | 0 (= G12 list_skills fix 維持) |

= **zero regression、 全 scenario healthy**。

SP size scaling:
- 10 skills: 5151 chars
- 50 skills: 5151 chars (= **真の O(1)**)
- 1000 skills (extrapolated): 5151 chars (vs ~50000 char with old design)

= Reyn skill catalog が成長しても SP は不変。 業界 standard pattern (= Anthropic
Tool Search Tool / OpenAI namespaces / MCP-Zero hierarchical) に整合。

#### 教訓 update (= retry 成功で得られた追加学び)

- **「load-bearing scaffold」 仮説の精緻化**: G23 当初の判断 (= intent-axis は
  load-bearing) は **正しかったが因果関係が逆**。 intent-axis section は category
  catalog として機能していたが、 W1 -40pp regression の真因は G12 Pattern E の
  暴露であり、 intent-axis 自体ではなかった
- **介入順序の重要性**: G12 Pattern E (= envelope-layer attractor) を解決せずに
  SP 削減を試すと、 list_* 経由の attractor 暴露 (= W7 80% empty-stop) が表面化
  して「削除すると壊れる」 と誤認させる。 attractor → routing layer の **下から
  順に解決** が正しい順序
- **業界 practice の Reyn applicability 確認済**: Anthropic Tool Search Tool /
  OpenAI namespaces / MCP-Zero hierarchical の core idea (= category 層 SP 残し、
  item 層 lazy fetch) は Reyn vision (= P3 + predictability) と整合し、 weak LLM
  下でも N≥10 で zero regression を達成

---

各案件は **月 1 回** trigger 状況を review、 着手順序を再評価。

---

### G24: plan tool router-LLM attractor (= R1 family、 batch 16 で 100% materialized)

**Categories**: C1 (model-capability-tradeoff、 主因) / C7 (prompt-vs-bloat-tradeoff、 副因)
**Status**: active (= R1 base rate 解明 + 介入 trial 候補)
**Discovered**: batch 16 (2026-05-08) — 5 scenario × N=5 = 25 runs で plan tool 0/25 invoked
**History**: 30+ commit の plan-mode 実装 (ADR-0022/0023/0024/0025) は Tier 2 で全 verified、 ただし real LLM (gemini-2.5-flash-lite via LiteLLM proxy) で plan tool は **一度も invoke されず**

#### 観測

| scenario | N | invoked? | reply 種別 |
|---|---|---|---|
| S1 multi-source synthesis | 5 | 0/5 | direct text-reply (=training data 由来 synthesis) |
| S2 concurrent (= 2 prompts back-to-back) | 5 | 0/5 | direct text-reply × 2 (= concurrent race も発生、 別途 G25) |
| S3 crash + resume | 5 | 0/5 | direct text-reply (= crash sim ineffective、 plan が無いので resume なし) |
| S4 operator commands | 5 | 0/5 | direct text-reply (= identical responses 5 runs across、 warm cache pattern) |
| S5 32KB+ spill | 5 | 0/5 | direct text-reply (~4-5KB output、 ADR-0024 threshold 未到達) |

**Brier**: 0.96 average (= prelude predicted refuted 10-30% range、 actual 100%)。 batches 13-14 の Brier 0.20 と比較で systemic miscalibration 確定。

#### 真因 (= 推測 3 候補、 batch 17 で verify-first)

1. **Plan tool description language mismatch**: prompt は Japanese、 description は English
2. **「rare tool」 attractor**: plan tool は導入が新しく、 LLM の使い慣れた response pattern (= direct reply) が dominant。 G1 family の plan-mode 版
3. **Prompt design 不適合**: batch 16 の prompts は「3 段落で書いて」 等の short-output 指示を含む → LLM が「one-shot で十分」 判断、 multi-step decomposition motivation 欠落

#### 介入 trial 候補 (= batch 17 着手案)

- **T1**: LLM context dump (= verify-first、 description が tools= に正しく載っているか) — $0.01 / risk 低
- **T2**: Description rewrite (= JP-aware wording) — $0.05 / risk 低
- **T3**: Prompt redesign (= "use plan tool", "use multiple steps") — risk: artificial、 real-user pattern との乖離
- **T4**: `force_plan` reyn.yaml flag — 0.5 day / risk: production code に dogfood 機能混入
- **T5**: Strong model (claude-3.7-sonnet) trial — $0.10 / G4 spike connection

**着手 trigger**: batch 17 で T1 → T2 順、 T1 結果次第で T3-T5 優先度確定。 batch 17 着手は G25 (= concurrent A2A race) fix 後。

#### Out-of-scope

- T4 force_plan flag を production default 化 (= P3 / vision 違反)
- prompt rule 累積で plan invoke 強要 (= G23 教訓に反する SP bloat trap)

#### 教訓

- **plan tool は新規 router LLM tool、 base rate 観測なしで Tier 2 検証だけで 進めると 30+ commit が無効化される risk**
- **R1 attractor は G1 family の generalized form**: 個別 tool ごとの calibration が必要
- **dogfood prediction prior は base rate 観測後に再 calibrate**: batch 16 prelude の 10-30% prior は過去 G1 経験の overfit、 plan-mode は new tool として独自 base rate (≥80%) を baked in すべきだった

---

### G25: 並行 A2A request 同 agent で `ChatSession.history` race + cross-talk

**Categories**: C5 (consistency-vs-availability-tradeoff、 主因)
**Status**: **resolved (2026-05-08, batch 16 同 wave、 mcp_server.py で fix landed)**
**Discovered**: batch 16 S2 sonnet (2026-05-08) — `ThreadPoolExecutor` 並行 dispatch で reply cross-talk 観測
**Severity**: HIGH (= production-impact、 user が wrong answer を receive)

#### 観測

S2 で 2 prompt を `ThreadPoolExecutor(max_workers=2)` で同時 send_message → 5 runs 中 3 (1, 2, 5) で cross-talk、 run 5 では reply の **完全 swap** 観測 (= P1 caller が P2 reply、 P2 caller が P1 reply 受信)。

#### 真因 (= source code 解析確定)

| layer | site | 問題 |
|---|---|---|
| 1 | `mcp_server.py:128 send_to_agent_impl` | per-session asyncio.Lock なし、 並行 call 同 session で race |
| 2 | `mcp_server.py:80 _new_agent_history_entries` | `session.history[baseline:]` を chain_id filter なし、 baseline 同値で開始した 2 caller が両方の reply を取得 |
| 3 | `session.py ChatMessage` | `chain_id` field 不在 (= meta dict はあるが history append 時に chain_id tag されない) |

#### 真の解 (= 2 layer fix)

**Layer A (= correctness, immediate)**: `send_to_agent_impl` に **per-session asyncio.Lock** 導入、 同 agent の concurrent A2A request を serialize。 concurrent UX 喪失と引き換えに correctness 確保。

**Layer B (= concurrency restoration, follow-up)**: `ChatMessage.chain_id` field 追加 (= meta から昇格)、 history append 全 site で chain_id tag、 `_new_agent_history_entries` を chain_id filter 化。 Layer A の lock を removable に。

#### 着手 trigger

- ✅ **Layer A + Layer B が batch 16 同 wave で landing**: `_AGENT_LOCKS` per-agent asyncio.Lock + `_new_agent_history_entries` の chain_id filter parameter 追加。 regression test も `tests/test_mcp_server.py::test_concurrent_send_to_same_agent_does_not_cross_talk` で landing
- 残: ChatMessage.chain_id を meta から first-class field に昇格は別 wave (= 既存 history.jsonl schema 互換維持で defer 可)

#### 教訓

- **A2A は LLM-to-LLM protocol、 同 agent への concurrent peer 想定** (= multi-agent topology + delegate_to_agent で頻発)、 per-session lock は **basic correctness primitive**
- **dogfood driver 自体が integration test の役**: B16-S2-1 は Tier 2 test 範囲外、 ThreadPoolExecutor 並行 dispatch という dogfood 固有 path で初観測

---

### G27: A2A async-dispatch return mismatch (= plan terminal text 配送 gap)

**Categories**: C5 (consistency-vs-availability tradeoff)
**Status**: **resolved (2026-05-08, batch 17 G24 investigation で発見 + 同 wave fix landed)**
**Discovered**: batch 16 finding 再 audit (= observe-first phase) 中、 LLM trace dump で plan IS invoked と確定後、 reply が空である mismatch から特定

#### 観測

batch 16 では「plan tool 0/25 invoked」 と判定したが、 phase 1 observe で trace dump 取った probe で:

- request [0] response: `tool_calls=['plan']` (= plan **was** invoked)
- subsequent step LLM calls fired (= read_README, read_CLAUDE, compare 3 step 完走)
- plan_summary: `2 plans started + 2 completed + 6 steps completed`
- ただし A2A reply: **0 chars** (= caller 受信は空)

= plan は走り完走したが terminal text が A2A caller に届かず。 batch 16 の "5/5 refuted" 判定は **本当は plan が invoke されていたが reply 配送経路 missing で empty に見えていた** 部分が混在していた疑い。 R1 attractor (= G24) は real だが rate は再測定必要。

#### 真因 (= 3 layer)

1. **`spawn_plan_task` が history append しない**: `put_outbox(kind="agent", ...)` のみ呼び、 `_append_history` を呼ばない。 通常の agent reply は両方呼ぶ pattern (= session.py 2181-2187)
2. **`send_to_agent_impl` が plan task を await しない**: RouterLoop は async dispatch で即 exit、 `_handle_user_message` も即 return。 plan task は背景で続くが A2A は既に harvest 段階
3. **chain_id 不整合**: spawn_plan_task が received `chain_id` は per-plan (= `plan_{plan_id}`)、 A2A caller の chain_id とは異なる。 仮に history append しても `_new_agent_history_entries(chain_id=A2A_chain)` filter で除外される

#### 真の解 (= 3 layer fix、 同 wave landing)

- **A**: `spawn_plan_task` で `_append_history` 呼び出し追加 (`session.py`)
- **B**: `send_to_agent_impl` で `running_plans` を残り timeout で gather await (`mcp_server.py`)
- **C**: `dispatch_plan_tool` から `parent_chain_id` を thread、 `spawn_plan_task` が history meta は parent chain で write、 plan_chain_id は `meta.plan_chain_id` field に保持 (= forensic continuity)

#### 検証

probe web (= port 8081、 trace dump 付き) で fix 後 retry:

| metric | before fix | after fix |
|---|---|---|
| A2A reply length | 0 chars | **1010 chars** ✅ |
| Plan invocation observed | yes (= trace dump 確認) | yes |
| Plan completed cleanly | yes (= 3 steps) | yes |
| Terminal text reaches caller | no | **yes** |

regression test: `tests/test_mcp_server.py::test_send_to_agent_waits_for_plan_terminal_text` (= 偽 plan task が history append、 A2A caller が text 受信)。

#### 教訓

- **batch 16 の Brier 0.96 は misclassification 含み**: A2A path で plan 不可視だった分が refuted 計上、 真の R1 attractor rate は再測定 (= G24 の T1-T5 trial で更新)
- **observe-first 原則の真価**: phase 1 LLM context dump なしには「plan invoked but invisible」 を分離できなかった。 batch 16 retro 段階では mixed signal 解釈不能、 trace 観測で初めて 2 因子分解可能になった
- **architectural plumbing は dogfood で初めて exercised**: Tier 2 test は ChatSession ↔ A2A の async/sync 境界を直接 cover しない。 dogfood driver (= A2A path) が production user の代わりに gap を露呈

---

### G26: slash command が A2A 越しに reply 戻らない (= architectural design boundary)

**Categories**: C7 (protocol-surface-tradeoff)
**Status**: tracker (= production user-impact なし、 dogfood methodology 上は HIGH)
**Discovered**: batch 16 S4 sonnet (2026-05-08)

#### 観測

`/plan list` を A2A 越しに send → empty reply。 source 解析で **double barrier** 確定:

1. `ChatSession._put_outbox` (session.py:1178) が `kind="status"` を `is_attached=False` で drop
2. A2A path は `is_attached=True` を set しない (= TUI のみ set)
3. `_new_agent_history_entries` (mcp_server.py:80) は `role="agent"` history のみ参照、 slash output は outbox 経由で history append しない

#### Design boundary 解釈

- slash commands は **operator-TUI concept** (= /list, /cancel, /skill, /plan)
- A2A は **LLM-to-LLM protocol** (= no operator UI attached)

→ **bug ではなく design choice**。 ただし dogfood driver が A2A を採用するなら、 operator command path は別 channel が必要。

#### 候補 fix (= mutually exclusive)

- **A1**: A2A response に slash output を attach — A2A protocol pollution
- **A2**: 別 endpoint `GET /observe/agents/<name>/state` — clean separation
- **A3**: slash 専用 dedicated CLI driver (= subprocess + stdin pipe) — driver 二重化の運用負荷
- **A4**: doc 化 + dogfood_trace.py 経由 fallback — minimal effort、 現状はこれで十分

**Recommendation**: A4 (= 即対応 doc) + A2 (= long-term)

#### 着手 trigger

- A4 doc 追記は batch 16 retro と同 commit で landing
- A2 着手は dogfood batch が複雑化して direct trace で間に合わなくなった時点

#### 教訓

- **driver 選定は scenario 設計の前**: A2A driver で operator command scenario を planning した時点で gap 露呈
- **architectural gap は bug でないが doc が必要**: 「動かない」 を「動かすべきでない」 と説明する doc がないと future contributor は bug と誤認

---

### G28: dogfood-induced empty A2A reply (= G12 Pattern E manifestation)

**Categories**: C7 (driver-vs-production-tradeoff)、 C1 (model-capability LLM context bloat)
**Status**: tracked (= production user-impact ゼロ、 dogfood methodology メモ)
**Discovered**: batch 16 retest 後 G28 hunt (2026-05-08、 main HEAD `18274b6`)

#### 観測

batch 16 retest で 25 runs 中 2 件 (= S1-r3, S4-r1、 8%) が plan invoke + step 完走後に reply_len=0。 別 web (port 8082) で REYN_LLM_TRACE_DUMP 有効化 + N=10 cold loop で **再現率 1/10** 確定。

trace 観測で diverge point pinpoint:

- empty run の router LLM request が **trace 上で orphan** (= request logged、 response 行が全 trace 中で唯一欠落)
- WAL に当該 run の agent event ゼロ
- A2A endpoint は `200 OK` で empty body を返却
- LLM context = **system + (user + assistant) × 7 重複構造** (= 25 messages、 5 prior runs の蓄積)
- empty run の elapsed 2.0s (= early fail-fast、 cold start でも slow phase でもない)

#### Bug chain (= 観測事実から)

1. dogfood driver の `clean_state` が disk side (`history.jsonl`) を unlink するが、 reyn web process の **ChatSession in-memory `_history`** は invalidate されない
2. 5 round 以上蓄積で session memory に "user prompt + agent reply" pair 重複
3. LLM context bloat → **G12 Pattern E (= empty completion attractor)** 発火
4. RouterLoop が empty 受領 → no-op exit (= history append なし、 plan invoke なし、 outbox emit なし)
5. A2A endpoint は session._history 差分なしを harvest → 200 OK empty body 返却

#### 真因 + Severity

- **真因 1 (= G12 Pattern E manifestation)**: weak LLM (= gemini-2.5-flash-lite) は 25 messages 重複 context で empty completion を確率的に発火。 G12 の envelope `(answered)` workaround は role=tool 受信 turn 限定で、 context bloat triggered empty には効かない
- **真因 2 (= dogfood driver design)**: `clean_state` が disk vs memory の semantics 不整合を作る。 production user は agent 背中で `rm` しないので invariant holds
- **production user impact: ゼロ** — 多 turn 会話で context 蓄積は normal、 G12 envelope fix が role=tool turn の主要 case を吸収済、 empty stop の base rate は dogfood data から分離不能

#### 候補 fix (= mutually exclusive)

- **α**: G28 を tracker entry のみ、 production fix なし (= 推奨)
- **β**: A2A endpoint に `force_fresh` flag 追加 (= per-call session memory reset)、 dogfood-only utility、 0.5 day
- **γ**: session 自動 detect (= disk history 不在で in-memory invalidate)、 1+ day、 state semantics 複雑

#### Recommendation: α

- production user 影響ゼロ
- batch 16 retest の 8% empty rate は **driver-induced**、 真の production rate ではないと明示
- 真の production base rate を測りたい場合は long-lived session で N=20+ shot、 disk reset しない pattern が必要 (= future dogfood methodology 改善)

#### 着手 trigger

- enterprise long-session で empty completion による UX issue が報告された時点で **真の base rate 測定** wave (= driver pattern 改善 + N≥20)
- production rate ≥ 5% なら β / γ を検討、 < 5% なら G12 受容 + 強モデル併用 (= G4 spike)

#### 教訓

- **dogfood driver は production behavior の代理ではない**: clean_state per-run pattern は test isolation 価値はあるが session lifecycle 不変条件を破壊。 driver が exposed する rate は production rate と必ずしも一致しない
- **observe-first で 8% を 0% に分解できた**: trace dump なしには 「2/25 empty は G27 fix の不完全」 と誤推測したまま batch 17 description rewrite を着手していた可能性。 実際は driver-induced + G12 manifestation で description rewrite では解消しない
- **真の rate 測定には driver 設計の見直しが必要**: 既存 dogfood pattern (= per-run clean_state) は R1 type attractor (= LLM 拒否) の測定に最適化、 G12 type (= context bloat empty) は long-session pattern が必要

---
