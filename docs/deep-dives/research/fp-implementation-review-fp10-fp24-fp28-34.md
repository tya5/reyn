---
title: FP-0010 / FP-0024 / FP-0028〜0034 実装レビュー
status: draft
last_updated: 2026-05-16
author: researcher
sources:
  - "gh issue view 11,25,29,30,31,32,34,35,36"
  - "src/reyn/chat/planner.py"
  - "src/reyn/chat/session.py"
  - "src/reyn/chat/router_loop.py"
  - "src/reyn/chat/router_tools.py"
  - "src/reyn/chat/router_system_prompt.py"
  - "src/reyn/tools/mcp.py"
  - "src/reyn/tools/universal_catalog.py"
  - "src/reyn/tools/universal_dispatch.py"
  - "src/reyn/tools/action_index.py"
  - "src/reyn/tools/action_usage_tracker.py"
  - "src/reyn/tools/mcp_drop.py"
  - "src/reyn/tools/plan.py"
  - "src/reyn/config.py"
---

# FP-0010 / FP-0024 / FP-0028〜0034 実装レビュー

## TL;DR

| FP | タイトル | 実装状態 | 主な問題 |
|---|---|---|---|
| FP-0028 | プラン進捗 UX | ✅ 正常 | なし |
| FP-0029 | イテレーション予算 | ✅ 正常 | なし |
| FP-0030 | ステップ結果品質 | ⚠️ 部分的不整合 | `plan.py` 記述が intent と乖離 |
| FP-0031 | Planner UX 改善 | ✅ 正常 | なし |
| FP-0032 | MCP カタログ対称化 | ⚠️ デッドコード | `mcp_section` 変数が未使用 |
| FP-0033 | Hot-list アーキテクチャ | ✅ (FP-0034 に統合) | — |
| FP-0034 | 統一 tool retrieval | ✅ Phase 1〜3 実装済み | コメント陳腐化、plan.py ツール名参照陳腐化 |
| FP-0010 | RAG ルーティング | ✅ (FP-0034 に統合) | — |
| FP-0024 | セマンティックツール選択 | ✅ (FP-0034 に統合) | — |

---

## 1. FP-0028: プラン進捗 UX

**Issue 意図**: `"plan step 2/4 done (s3)"` → `"plan step 2/4: auth.py を読む"` に変更。

**実装確認** (`src/reyn/chat/planner.py`):
```python
# L864
desc_preview = (step.description or step.id)[:60]

# L1014 (成功時)
text=f"plan step {n_done}/{n_total}: {desc_preview}"

# L974 (失敗時, FP-0031-B と統合)
text=f"plan step {n_done + 1}/{n_total}: {desc_preview} → 失敗 ({err_summary})"
```

**判定**: ✅ Issue 意図と完全一致。フォールバック (`step.id`) も実装済み。

---

## 2. FP-0029: プランステップのイテレーション予算

**Issue 意図**: `_PLAN_STEP_MAX_ITERATIONS = 3` → `5` に引き上げ。`reyn.yaml` での上書き対応。

**実装確認**:
- `planner.py` L74: `_PLAN_STEP_MAX_ITERATIONS = 5` ✅
- `config.py` L843: `PlanConfig.step_max_iterations: int = 5` ✅
- `config.py` L1580: `step_max_raw = raw.get("step_max_iterations")` → `reyn.yaml` の `plan.step_max_iterations:` で上書き可能 ✅

**判定**: ✅ 定数変更 + Config 統合の両方が実装済み。

---

## 3. FP-0030: プランステップ結果品質

**Issue 意図 (2 ファイル)**:
1. `planner.py` `build_plan_step_system_prompt` のガイダンスを "1–3 sentences" → "Report what this step found. Include code snippets, ~800 chars soft limit" に変更
2. `plan.py` `_PLAN_DESCRIPTION` の陳腐化テキスト「ターミナルステップが合成」→「ルーターが合成」に更新

**実装確認**:

### planner.py (L370–374) ✅
```python
"Report what this step found. Include concrete details: code snippets, "
"function signatures, specific line numbers, exact values, structured data. "
"Aim for ~800 characters as a soft target; exceed if the content requires "
"it (e.g. multi-line code blocks). Be factual — a separate synthesis step "
"will produce the user reply."
```
→ Issue 意図と一致。

### plan.py ⚠️ 部分的不整合

**`_PLAN_DESCRIPTION` (L54–56)**:
```python
"Each step summarises what it found; the router "
"synthesises the final reply after all steps "
"complete."
```
Issue 提案テキスト:
> "After all steps complete, the router synthesises step results into a final reply. Design each step to gather specific evidence (code, facts, data); a dedicated synthesis turn handles the final reply."

現在の実装は「ターミナルステップ」の言及を除去できているが、**"summarises"** という動詞が残っている。Issue が求める **"gather specific evidence (code, facts, data)"** という指示が欠落。

**`steps_json` パラメータ説明 (L87–88)**:
```python
"Each step should "
"summarise what it found; the router synthesises "
"the final reply after all steps complete."
```
同様に "summarise" が残っている。

**`steps_json` の `tools` フィールド説明 (L80–82)**: [→ FP-0034 との交差問題、§5 参照]

**影響**: `plan` ツールの説明を読んだ LLM（= 計画作成時）は、ステップが「要約」を返すものと理解する。一方で実際のステップ実行時には「コードスニペット・行番号・具体的証拠」を返すよう促される。計画設計と実行ガイダンスの意図がズレており、合成品質に影響する可能性がある。

**判定**: ⚠️ `build_plan_step_system_prompt` は正しく更新済み。`plan.py` の記述は不完全更新。

---

## 4. FP-0031: Planner 実行 UX 改善

**Issue 意図**: A=計画報告 / B=失敗ステータス / C=自動リトライ / D=上限確認。

**実装確認**:

| Component | 実装場所 | 状態 |
|---|---|---|
| A. 計画事前報告 | `session.py` L3007–3023 (`spawn_plan_task`) | ✅ |
| B. 失敗ステータス通知 | `planner.py` L974 | ✅ |
| C. 自動リトライ (`_PLAN_STEP_RETRY_LIMIT = 3`) | `planner.py` L856–920 | ✅ |
| D. 上限到達時ユーザー確認 | `planner.py` L921–945 (`handle_limit_exceeded`) | ✅ |

```python
# session.py L3019
text=f"以下の計画で実行します:\n{plan_summary}"

# planner.py L893
text=f"  リトライ {attempt}/{step_retry_limit}: {desc_preview}"
```

**判定**: ✅ 全 4 コンポーネント実装済み。

---

## 5. FP-0032: MCP tool catalog の skill/agent 対称化

**Issue 意図**: 語彙統一 (`tool` → `mcp_tool_name`)、schema enum 注入、`describe_mcp_tool` 追加、SP flat list 追加、`MCP_SEARCH_THRESHOLD = 0`。

**実装確認**:

| 変更 | 実装場所 | 状態 |
|---|---|---|
| 語彙統一 (`mcp_tool_name`) | `tools/mcp.py` L96–130 | ✅ |
| `_enrich_router_schema` (enum 注入) | `tools/mcp.py` L316–358 | ✅ |
| `describe_mcp_tool` + handler | `tools/mcp.py` L362–445 | ✅ |
| SP flat list `_render_mcp()` | `router_system_prompt.py` L423–456 | ✅ 実装あり |
| `MCP_SEARCH_THRESHOLD = 0` | `router_tools.py` L57 | ✅ |

### ⚠️ デッドコード問題

`router_system_prompt.py` L66:
```python
mcp_section = _render_mcp(mcp_servers)
```

この変数 `mcp_section` は **`parts` に一度も `append` されない**。FP-0034 の wrapper-only モード移行に伴い、MCP 節の SP 掲載は意図的に廃止されたが（L301–303 のコメントが確認）、`_render_mcp()` の呼び出し自体が削除されていない。

影響:
- 毎ターン無駄に `_render_mcp(mcp_servers)` が実行される（MCP サーバー・ツールが多い環境で無視できないコスト）
- 将来の読み手が「MCP 節は SP に含まれている」と誤解するリスク

**判定**: ⚠️ FP-0032 本体は正常実装。`mcp_section` のデッドコードが FP-0034 との統合時に残存。

---

## 6. FP-0033 / FP-0010 / FP-0024

いずれも FP-0034 に supersede されてクローズ。実装は §7 (FP-0034) にまとめて反映。

---

## 7. FP-0034: 統一 tool retrieval アーキテクチャ

FP-0034 は 6 Phase 構成。実装状態を Phase ごとに確認。

### Phase 0: FP-0032 landing ✅

### Phase 1: Universal catalog wrappers ✅

`tools/universal_catalog.py`:
- `LIST_ACTIONS`, `DESCRIBE_ACTION`, `INVOKE_ACTION` — real handlers 実装済み
- `SEARCH_ACTIONS` — Phase 2 の ActionEmbeddingIndex に依存するが、handler は stub でなく実装済み（`rs.action_embedding_index` 参照、embedding 未設定なら空リスト返却で graceful degrade）
- `split_qualified_name`, `build_qualified_name`, `is_valid_qualified_name` — qualified name ユーティリティ ✅
- D14 visibility gating (`is_search_available`, `is_exec_available`, `visible_categories`) ✅

`tools/universal_dispatch.py`:
- PR-2 routing layer — `resolve_invoke_action`, `resolve_describe_action`, `suggest_similar_names` ✅
- `mcp.operation__drop_server` rule (PR-4) ✅

`config.py`:
- `ActionRetrievalConfig.universal_wrappers_enabled = True` (デフォルト ON) ✅
- `hot_list_n = 10` (デフォルト) ✅

`router_tools.py`:
- Section I: universal wrappers を tools= に追加 (L844–866) ✅
- Section J: legacy tool 除外 (L874–890) ✅
- Section K: hot list direct aliases (L892–904) ✅

### Phase 2: Foundation Layer (ActionEmbeddingIndex + ActionUsageTracker) ✅

- `tools/action_index.py` (373 行): SQLite-WAL 永続化付き `ActionEmbeddingIndex` ✅
- `tools/action_usage_tracker.py` (180 行): freq + recency ベースの hot list 選出 ✅
- `tools/mcp_drop.py`: `mcp.operation__drop_server` op (FP-0034 §D23 PR-4) ✅

### Phase 3: Self-improvement Loop (routing_decided event) ✅

`router_loop.py` L828–866:
- `invoke_action` 呼出し時に `routing_decided` P6 event を emit
- `hot_list_alias` 経由の呼出し時も source="hot_list_alias" で emit
- outcome: `error` / `success` の 2 値 ✅

### Phase 4: SP refactor ✅

`router_system_prompt.py`:
- `universal_wrappers_enabled=True` 時: "## Action categories" 節 (13 カテゴリ、static) ✅
- legacy flat list 節 (Skills / Agents / MCP / Indexed sources / Files) は wrapper-only パスで省略 ✅

### Phase 5: 既存 wrapper 統合 / Phase 6: cleanup

`router_tools.py` Section J で legacy tool を tools= から除外済み ✅  
ただし `build_tools()` 内での legacy tool 構築コード (A1–D4) は まだ削除されていない（= universal_wrappers_enabled=True 時は無駄に構築して直後に除外）。これは Phase 5/6 cleanup の残タスク。

---

## 8. 横断的問題: plan.py と universal catalog のミスマッチ

`plan.py` の `steps_json` 説明 (L80–82):
```python
"tools: list of TOP-LEVEL tool names this step "
"calls (e.g. \"reyn_src_read\", \"web_search\", "
"\"invoke_skill\")."
```

FP-0034 universal mode では:
- `reyn_src_read` → `invoke_action(action_name="reyn.source__read", ...)`
- `web_search` → `invoke_action(action_name="web__search", ...)`
- `invoke_skill` → `invoke_action(action_name="skill__...", ...)`

計画作成時に LLM が `tools` フィールドに `["reyn_src_read"]` と入力しても、Plan Step の実行環境では `reyn_src_read` は tools= に存在しない（Section J で除外済み）。

FP-0034 Phase 5 が完了した時点では、`plan.py` の `steps_json` ツール名例を universal 形式に更新する必要がある。

---

## 9. 問題サマリと推奨アクション

### 🐛 Bug (軽微)

**B1: `mcp_section` デッドコード** (`router_system_prompt.py` L66)  
```python
# L66 — 削除すべき
mcp_section = _render_mcp(mcp_servers)
```
毎ターン実行されるが結果が使われない。`build_system_prompt()` の引数 `mcp_servers` は `router_system_prompt.py` で今後も使われないなら削除候補。

### ⚠️ Inconsistency (機能影響あり)

**I1: `plan.py` の "summarise" 残存** (FP-0030 不完全更新)  
- `_PLAN_DESCRIPTION` (L54): "summarises" → "gathers specific evidence (code, facts, data)"
- `steps_json` 説明 (L88): "summarise what it found" → "report concrete evidence"
- `steps_json` 例示 step.description (L93): "summarise findings" → "report evidence"

計画作成 LLM が "summarise" 意図でステップを設計し、実行 LLM が "report concrete evidence" で実行するギャップが生じる。

**I2: `plan.py` の古いツール名例** (FP-0034 追従漏れ)  
`steps_json` (L80–82) のツール名例 (`reyn_src_read`, `web_search`, `invoke_skill`) が universal catalog 後の名前と不一致。`invoke_action` を使うよう更新が必要。

### 📝 Stale comment

**S1: `router_tools.py` L840–843 のコメント**  
> "Phase 1 keeps it OFF unconditionally because (a) the handler is a NotImplementedError stub awaiting Phase 2's ActionEmbeddingIndex"

実際には `search_actions` handler は NotImplementedError stub ではなく実装済み。正しくは「`search_actions_visible=False` (embedding 未設定) によって非公開」。

### 📋 Phase 5/6 残タスク

`build_tools()` が `universal_wrappers_enabled=True` 時に legacy tool (A1〜D4) を構築してから Section J で除外するパターンは、段階的移行の痕跡。Phase 5/6 で legacy tool 構築コードごと削除すればレイテンシと可読性が改善する。

---

## 10. 推奨 GitHub Issue

以下を新規 issue として発行予定:

1. **FP-0030 followup: `plan.py` の "summarise" → "gather evidence" テキスト更新** (SMALL)
   - `_PLAN_DESCRIPTION`, `steps_json` 説明, 例示 step の記述を FP-0030 提案テキストに合わせる
   - `steps_json` のツール名例も universal catalog 形式に更新 (FP-0034 追従)

2. **cleanup: `mcp_section` デッドコード除去** (SMALL)
   - `router_system_prompt.py` L66 の `mcp_section = _render_mcp(mcp_servers)` 削除
   - 引数 `mcp_servers` が完全不要なら関数シグネチャからも除去

3. **comment fix: `router_tools.py` の `search_actions` に関するコメント陳腐化** (SMALL)
   - Phase 1 コメントを「handler は stub」→「embedding 未設定により非公開」に修正
