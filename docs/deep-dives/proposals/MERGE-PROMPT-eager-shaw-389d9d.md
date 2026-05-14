# Merge prompt — branch `claude/eager-shaw-389d9d`

**Branch**: `claude/eager-shaw-389d9d`
**Rebased onto**: main `19b628e` → current main `1dac280`
**Commits ahead of main**: 6（docs のみ、FP-0022/0023/0024/0025/0026/0027/0028/0029/0030）

> ⚠️ main 進捗: FP-0021 実装着地（`a03bcfc`）、FP-0019 Wave 1 着地（`6620505`）、FP-0020 Component A 着地（`1dac280`）。これらの実装 PR はすでに main に取り込み済み。本ブランチは残る docs（FP-0022/0023/0024/0025/0026/0027/0028/0029/0030）のみ未マージ。

---

## このブランチでやったこと

God-file 削減 FP 起票 + イベントログ監査 + パーミッション設計調査 + Router SP 最適化調査 + Planner 設計調査セッション。コード変更なし（docs のみ）。

---

### f7a4496 — FP-0019: ChatSession 責務分離

`session.py`（3,836 行）から 5 つのサービスを 3 ウェーブで抽出する設計提案。
目標: ~600 行の薄いディスパッチャに縮小。

**すでに抽出済み**: 6 サービス（2,122 行）が `chat/services/` に存在。

**3 ウェーブ**:

| ウェーブ | 対象 | コスト |
|---|---|---|
| 1 | CompactionController + SkillRunner | SMALL × 2 |
| 2 | A2AHandler + InterventionHandler | MEDIUM × 2 |
| 3 | AutoResumeHandler（FP-0011 連動） | SMALL |

**Wave 1 が最優先**: FP-0012 は LANDED（commit `c9e79d6`）。SkillRunner 抽出で
`session.py` 側を着地済み非同期 OS プリミティブと整合させる。
Wave 2 の A2AHandler 抽出は FP-0013（unified-inbox-outbox-transport、ACCEPTED）と
連携が必要。Wave 3 は FP-0011 着地に連動。

**新規ファイル**:
- `docs/deep-dives/proposals/0019-chat-session-refactor.md`
- `docs/deep-dives/proposals/0019-chat-session-refactor.ja.md`

---

### 5ed5d80 — FP-0020: OSRuntime レイヤ分解

`runtime.py`（1,882 行）を垂直レイヤに分解する設計提案。
AI コーディングエージェントのコンテキストウィンドウ最適化が主目的（合計行数増加は許容）。

**設計原則**:
```
runtime.py の複雑さは「垂直方向」—— 1 つの責務（スキル実行）が深さ方向に積み重なる。
↓
RunOrchestrator (L1) → PhaseExecutor (L2) → LLMCallRecorder (L3) + RunState (共有状態)
```

**4 コンポーネント（A→B→C→D）**:

| コンポーネント | 対象 | コスト |
|---|---|---|
| A | `RunState`（ミュータブル実行状態の dataclass） | SMALL |
| B | `LLMCallRecorder`（LLM 呼び出し + WAL + バジェット） | SMALL |
| C | `PhaseExecutor`（act/decide ループ） | SMALL |
| D | `RunOrchestrator`（フェーズ順序 + ライフサイクル） | MEDIUM |

**行数変化**: 1,882 行 → 5 ファイル合計 ~1,620 行（最大ファイル ~500 行）

**注意**: FP-0017（sandboxed-execution）Component D 着地（commit `ddf2d05`）により
`exec.py` に `DeprecationWarning` 追加済み。PhaseExecutor 抽出時は `sandboxed_exec`
を使用すること。

**新規ファイル**:
- `docs/deep-dives/proposals/0020-runtime-layer-decomposition.md`
- `docs/deep-dives/proposals/0020-runtime-layer-decomposition.ja.md`

---

### 227d76f — FP-0021: イベントログ監査完全性

`workflow_started` だけが `run_id` と `skill` を持ち、同一 run の 6 イベントタイプが欠落している問題を追跡する設計提案。`permission_granted` イベントが存在しない問題（deny のみ記録）も含む。

**ギャップ一覧**:

| イベント | 不足フィールド |
|---|---|
| `workflow_finished` | `run_id`, `skill` |
| `llm_called` | `run_id`, `skill` |
| `llm_response_received` | `run_id`, `skill` |
| `permission_denied` | `run_id`, `skill`, `phase` |
| `user_intervention_requested` | `run_id`, `skill` |
| `user_intervention_received` | `run_id`, `skill`、リクエストとの相関 id |
| `permission_granted` | 存在しない（新設） |

**実装コスト**: SMALL — すべて `emit()` への kwarg 追加のみ。WAL・復元ロジック変更なし。

**背景**: WAL（復元用）と events（監査用）は独立したチャネルであり、
監査フィールドの追加は復元インフラに影響しない。`docs/concepts/events.md` の
`kind` → `type` 誤記も同コミットで修正済み。

**新規ファイル**:
- `docs/deep-dives/proposals/0021-event-log-audit-completeness.md`
- `docs/deep-dives/proposals/0021-event-log-audit-completeness.ja.md`

---

### 5ee0d01 — FP-0022: パーミッション Tier モデル正式化

パーミッションシステムの 2 軸（利用宣言 × 許諾）を明文化し、4 Tier モデルを提案。
`web_fetch` と `web_search` の非対称さを具体的な修正として定義。

**Tier モデル**:

| Tier | 代表 Op | 利用宣言 | デフォルト | config 制限 |
|---|---|---|---|---|
| 0 | run_skill, ask_user | 不要 | 無条件通過 | 不可 |
| 1 | web_search, web_fetch | 不要 | 許諾 | ✓ `deny` で制限可能 |
| 2 | mcp | 必要 | 要承認 | ✓ `allow` で事前許可 |
| 3 | shell, file（zone外） | 必要 | 要承認 | ✓ `allow` で事前許可 |

**修正内容**:
1. `web_fetch`: `get_web_fetch_allowed()` catalog ゲートを廃止 → handler-level `_approve()` に移行（初回確認 → ALWAYS で永続化）
2. `web_search`: `_is_config_denied("web.search")` チェックを追加（`web.search: deny` で制限可能に）
3. `docs/concepts/permission-model.md`: Tier モデル + 2 軸の説明を追加

**背景**: Android の Normal/Dangerous permission 区分と同構造。利用宣言 = skill.md frontmatter。許諾 = config（事前）+ interactive（動的）の 4 層。

**新規ファイル**:
- `docs/deep-dives/proposals/0022-permission-tier-model.md`
- `docs/deep-dives/proposals/0022-permission-tier-model.ja.md`

---

---

### d19dc62 — FP-0023: Router SP 速攻改善 + FP-0024: セマンティックツール選択

Router システムプロンプト最適化の 2 本立て。

#### FP-0023（SMALL）— `router_system_prompt.py` への 5 つのピンポイント修正

| 変更 | 内容 | 効果 |
|---|---|---|
| 1 | セクション並び替え（静的 → 動的） | キャッシュカバレッジ ~20% → ~60% |
| 2 | 意図軸の重複統合 | ルーティングラベル漏れリスク解消 |
| 3 | spawn-ack MUST を優先順位付きで整理 | `/tasks` 準拠率改善 |
| 4 | `delegate_to_agent` Behaviour ルール追加 | ツールスキーマだけからの推測を解消 |
| 5 | JA recall/memory 例文追加 | JA での `recall` vs `list_memory` ミスルーティング解消 |

**対象ファイル**: `src/reyn/chat/router_system_prompt.py` のみ。

#### FP-0024（MEDIUM）— セマンティックツール選択（4 コンポーネント）

| Component | 内容 | コスト |
|---|---|---|
| A | BM25 スキル事前絞り込み（`invoke_skill.name` enum を O(N)→O(K=5)） | SMALL |
| B | `search_hints` frontmatter + `reyn skill enrich` CLI（Tool2Vec 手法） | SMALL |
| C | Embedding バックエンド + ハイブリッド（BM25+embedding RRF fusion） | MEDIUM |
| D | Anthropic `tool_search_tool` + MCP deferred loading（30+ MCP ツール時） | SMALL |

依存関係: A/B/D は独立リリース可能。C は A に依存（BM25 バックエンド置き換え）。

**新規ファイル**:
- `docs/deep-dives/proposals/0023-router-sp-quick-wins.md`
- `docs/deep-dives/proposals/0023-router-sp-quick-wins.ja.md`
- `docs/deep-dives/proposals/0024-router-sp-semantic-tool-selection.md`
- `docs/deep-dives/proposals/0024-router-sp-semantic-tool-selection.ja.md`

---

### (d19dc62 に同梱) — FP-0025: Planner Router Narration + Plan Step SP 修正

plan 完了時の narration を FP-0012（skill narration）と完全に同形にする提案。

**現在の非対称性**:
- スキル完了 → `_enqueue_skill_completed` → `_handle_skill_completed` → Router LLM が narrate
- プラン完了 → terminal ステップが直接 `_put_outbox` → Router を経由しない

**4 コンポーネント（全て SMALL）**:

| Component | 内容 |
|---|---|
| A | `output_language` を `build_plan_step_system_prompt()` に引き渡す |
| B | step id（`s1`, `s2`）をプロンプトから除去 → `## Your task` に変更 |
| C | `_enqueue_plan_completed` + `_handle_plan_completed` 追加（skill と対称）; `spawn_plan_task` 変更; plan description 更新 |
| D | Router SP Behaviour に plan 使用基準ルール追加 |

Component C 実装後、各プランステップは focused 情報収集に専念し synthesis は Router LLM が担う。`_PLAN_MAX_STEPS` の 7 ステップが全て情報収集に使える。

**新規ファイル**:
- `docs/deep-dives/proposals/0025-planner-narration-and-sp-fixes.md`
- `docs/deep-dives/proposals/0025-planner-narration-and-sp-fixes.ja.md`

---

### (FP-0026 に同梱) — FP-0026: Op/Permission クロスレイヤー整合性

3 つの宣言サーフェス（phase `allowed_ops` / skill `permissions` / `reyn.yaml`）間の整合性を `reyn skill validate` CLI でチェックする提案。

**問題の核心**: 3 つの宣言が独立しており整合性チェックがない。Mode A 失敗（phase 宣言）は即座に検出されるが、Mode B 失敗（permission 宣言）は実行時まで表面化しない。

**3 コンポーネント**:
- A: `reyn skill validate` CLI — 起動時に全スキルを検査、整合性レポート出力
- B: スキルロード時の警告 — phase の `allowed_ops` が skill の `permissions` と矛盾する場合にログ出力
- C: op_catalog の Tier 説明注記 — 利用宣言が必要かどうかを各 op エントリに記述

**新規ファイル**:
- `docs/deep-dives/proposals/0026-op-permission-cross-layer-coherence.md`
- `docs/deep-dives/proposals/0026-op-permission-cross-layer-coherence.ja.md`

---

### ＜最新＞ — FP-0027〜0030: Planner 改善 4 本（Planner 深掘り後起票）

FP-0025 着地後に残る Planner UX・信頼性ギャップを 4 FP で定義。

#### FP-0027（SMALL）— プランステップ失敗の透明性向上

`step_failures` が `_handle_plan_completed` まで転送されない問題。Router LLM がデータ欠損を認識できないまま自信満々な回答を生成する。

**修正**: `_enqueue_plan_completed` に `step_failures` パラメータを追加し `spawn_plan_task` から渡す。`_handle_plan_completed` の注入メッセージに truncate（200 chars）した失敗情報を含める。

**対象**: `src/reyn/chat/session.py` 3 箇所のみ。

#### FP-0028（SMALL）— プラン進捗 UX（ステータスメッセージにステップ説明）

`"plan step 2/4 done (s3)"` がユーザーに意味をなさない問題。

**修正**: `execute_plan` の 1 行 — `f"plan step {n_done}/{n_total} done ({step.id})"` → `f"plan step {n_done}/{n_total}: {(step.description or step.id)[:60]}"`

#### FP-0029（SMALL）— `_PLAN_STEP_MAX_ITERATIONS` を 3 → 5 に引き上げ

`list_dir` + `read_file` + `read_file` + narrate のような 4 op パターンがバジェット到達でサイレントにアボートする問題。Router デフォルト `_MAX_ROUTER_ITERATIONS = 5` と一致させる。

**修正**: `planner.py` の定数 1 行。オプションで `reyn.yaml` に `plan.step_max_iterations` 設定を追加（~5 行）。

#### FP-0030（SMALL）— プランステップ結果品質（よりリッチな出力ガイダンス）

「1〜3 文で要約」というハード上限がコードスニペット・行番号・関数名を捨てさせ、Router LLM が要約の要約から合成する問題。`plan.py` の `_PLAN_DESCRIPTION` が FP-0025 C 着地後に陳腐化している問題も含む。

**修正**: `build_plan_step_system_prompt` のガイダンスを「~800 文字ソフト上限・コードスニペット OK」に変更。`_PLAN_DESCRIPTION` のターミナルステップ言及を削除。

**新規ファイル**:
- `docs/deep-dives/proposals/0027-plan-step-failure-transparency.md`
- `docs/deep-dives/proposals/0027-plan-step-failure-transparency.ja.md`
- `docs/deep-dives/proposals/0028-plan-progress-ux.md`
- `docs/deep-dives/proposals/0028-plan-progress-ux.ja.md`
- `docs/deep-dives/proposals/0029-plan-step-iteration-budget.md`
- `docs/deep-dives/proposals/0029-plan-step-iteration-budget.ja.md`
- `docs/deep-dives/proposals/0030-plan-step-result-quality.md`
- `docs/deep-dives/proposals/0030-plan-step-result-quality.ja.md`

---

## 調査で判明した「FP 不要」事項（再掲）

| 候補 | 判定 | 根拠 |
|---|---|---|
| エージェント単位コスト帰属 | 実装済み | `cost_tab.py` の `by_agent`/`by_agent_skill` |
| 永続メモリ | 実装済み | `src/reyn/memory/memory.py`（user/feedback/project/reference） |
| マルチセッション文脈継続 | 設計で解決済み | WAL + フェーズ境界復元（P5 の意図通り） |
| Docker MCP ゲートウェイ | 当面不要 | 常駐デーモン必要、Reyn の設計方針と相容れず |

## 調査で判明したアーキテクチャ知見（実装判断に有用）

| 知見 | 詳細 |
|---|---|
| WAL と events は独立チャネル | WAL = 復元専用（state_log.jsonl）。events = 監査・観測専用（events/*.jsonl）。重複する論理事象は異なるフィールド名で別記録 |
| EventLog が event bus | 同期ファンアウト。4 サブスクライバ（EventStore / ConsoleLogger / ChatEventForwarder / テストフック）。サブスキルへカスケード |
| WAL の EventLog subscriber 化は非自明 | seq 返却・async/sync 不整合・スコープ差異の 3 障壁あり |
| events/*.jsonl は recovery に使われない | クラッシュ復元は WAL + snapshot.json のみ。P6 の「events derive state recovery」は WAL を指す |
| permission 2 軸: 利用宣言 × 許諾 | `decl.*`（skill 側）vs `_approve()`（4 層: config / saved / session / prompt）。web_fetch は許諾 1 層のみ、web_search は 0 層——FP-0022 が修正提案 |
| Tier 0 と Tier 1 の違い | Tier 0（run_skill 等）は「無条件通過」で config 制限不可。Tier 1（web_fetch 等）は「デフォルト許諾だが `deny` で制限可能」|

---

## マージ後のアクション候補

> ✅ **main 着地済み**: FP-0021 実装、FP-0019 Wave 1、FP-0020 Component A。以下は未着地のもの。

**即効性あり（SMALL コスト）**:
1. **FP-0027** — `step_failures` を `_handle_plan_completed` まで転送（`session.py` 3 箇所）
2. **FP-0028** — `execute_plan` のステータスメッセージを step description に変更（1 行）
3. **FP-0029** — `_PLAN_STEP_MAX_ITERATIONS` を 3 → 5（1 行; オプションで `reyn.yaml` 設定追加）
4. **FP-0030** — `build_plan_step_system_prompt` ガイダンスを ~800 char ソフト上限に変更 + `_PLAN_DESCRIPTION` 陳腐化修正
5. **FP-0022** — `web_fetch` を handler-level `_approve()` に移行 + `web_search` に deny check 追加（4 ファイル）
6. **FP-0023** — `router_system_prompt.py` 5 変更（セクション並び替え・意図軸統合・spawn-ack 優先順位・delegate ルール・JA 例文）
7. **FP-0026** — `reyn skill validate` CLI + ロード時整合性警告 + op_catalog 説明注記（スキル作者 UX 改善）
8. **FP-0024 Component A** — BM25 事前絞り込み + `SkillSearchIndex`（スキル 20+ 時に有効、依存なし）
9. **FP-0024 Component B** — `search_hints` frontmatter + `reyn skill enrich` CLI（A/C を強化）
10. **FP-0024 Component D** — Anthropic `tool_search_tool` MCP 統合（MCP 30+ 時、独立リリース可）
11. FP-0019 Wave 1 残余 — SkillRunner 抽出（CompactionController は着地済み）
12. FP-0020 Component B — LLMCallRecorder 抽出（WAL + バジェットを独立テスト可能ユニットに）

**中期（MEDIUM コスト）**:
13. **FP-0024 Component C** — Embedding バックエンド + ハイブリッド + `.reyn/skill-index/` ライフサイクル（A + B 完了後）
14. FP-0020 Component C — PhaseExecutor 抽出（A + B 完了後）
15. FP-0013 実装 → FP-0019 Wave 2（A2AHandler 抽出は FP-0013 と連携）

**大規模（LARGE）**:
16. FP-0020 Component D — RunOrchestrator 抽出（runtime.py ~400 行化の最終段階）
17. FP-0019 Wave 2 — A2AHandler + InterventionHandler（FP-0013 着地後）

**FP-0013 着地後（ACCEPTED）**:
18. FP-0019 Wave 2 — A2AHandler 抽出を FP-0013 実装と同一 PR で

**延期**:
- FP-0019 Wave 3（FP-0011 着地待ち）
