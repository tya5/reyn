# Merge prompt — branch `claude/eager-shaw-389d9d`

**Branch**: `claude/eager-shaw-389d9d`
**Rebased onto**: main `19b628e`
**Commits ahead of main**: 6（docs のみ）

---

## このブランチでやったこと

God-file 削減 FP 起票 + イベントログ監査 + パーミッション設計調査セッション。コード変更なし。

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

**即効性あり（SMALL コスト）**:
1. **FP-0021** — `emit()` に `run_id`/`skill` を追加 + `permission_granted` 新設（6 ファイル、kwarg 追加のみ）
2. **FP-0022** — `web_fetch` を handler-level `_approve()` に移行 + `web_search` に deny check 追加（4 ファイル）
3. FP-0019 Wave 1 — CompactionController + SkillRunner 抽出（session.py を非同期 OS と整合）
4. FP-0020 Component A — RunState 抽出（LLMCallRecorder の前提、独立して SMALL）

**中期（MEDIUM コスト）**:
4. FP-0020 Component B — LLMCallRecorder 抽出（WAL + バジェットを独立テスト可能ユニットに）
5. FP-0020 Component C — PhaseExecutor 抽出（A + B 完了後）
6. FP-0013 実装 → FP-0019 Wave 2（A2AHandler 抽出は FP-0013 と連携）

**大規模（LARGE）**:
7. FP-0020 Component D — RunOrchestrator 抽出（runtime.py ~400 行化の最終段階）
8. FP-0019 Wave 2 — A2AHandler + InterventionHandler（FP-0013 着地後）

**FP-0013 着地後（ACCEPTED）**:
9. FP-0019 Wave 2 — A2AHandler 抽出を FP-0013 実装と同一 PR で

**延期**:
- FP-0019 Wave 3（FP-0011 着地待ち）
