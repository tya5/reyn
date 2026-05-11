# Merge prompt — branch `claude/eager-shaw-389d9d`

**Branch**: `claude/eager-shaw-389d9d`
**Rebased onto**: main `19b628e`
**Commits ahead of main**: 2（docs のみ）

---

## このブランチでやったこと

God-file 削減 FP 起票セッション。コード変更なし。

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

## 調査で判明した「FP 不要」事項（再掲）

| 候補 | 判定 | 根拠 |
|---|---|---|
| エージェント単位コスト帰属 | 実装済み | `cost_tab.py` の `by_agent`/`by_agent_skill` |
| 永続メモリ | 実装済み | `src/reyn/memory/memory.py`（user/feedback/project/reference） |
| マルチセッション文脈継続 | 設計で解決済み | WAL + フェーズ境界復元（P5 の意図通り） |
| Docker MCP ゲートウェイ | 当面不要 | 常駐デーモン必要、Reyn の設計方針と相容れず |

---

## マージ後のアクション候補

**即効性あり（SMALL コスト）**:
1. FP-0019 Wave 1 — CompactionController + SkillRunner 抽出（session.py を非同期 OS と整合）
2. FP-0020 Component A — RunState 抽出（LLMCallRecorder の前提、独立して SMALL）

**中期（MEDIUM コスト）**:
3. FP-0020 Component B — LLMCallRecorder 抽出（WAL + バジェットを独立テスト可能ユニットに）
4. FP-0020 Component C — PhaseExecutor 抽出（A + B 完了後）
5. FP-0013 実装 → FP-0019 Wave 2（A2AHandler 抽出は FP-0013 と連携）

**大規模（LARGE）**:
6. FP-0020 Component D — RunOrchestrator 抽出（runtime.py ~400 行化の最終段階）
7. FP-0019 Wave 2 — A2AHandler + InterventionHandler（FP-0013 着地後）

**FP-0013 着地後（ACCEPTED）**:
8. FP-0019 Wave 2 — A2AHandler 抽出を FP-0013 実装と同一 PR で

**延期**:
- FP-0019 Wave 3（FP-0011 着地待ち）
