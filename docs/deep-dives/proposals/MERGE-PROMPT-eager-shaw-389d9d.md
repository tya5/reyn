# Merge prompt — branch `claude/eager-shaw-389d9d`

**Branch**: `claude/eager-shaw-389d9d`
**Rebased onto**: main `ba4c5fe`
**Commits ahead of main**: 11（コード変更 1 件 + docs 10 件）

---

## このブランチでやったこと

Research / FP 起票セッション。コード変更は `reyn mcp install --args` 対応のみ。

---

### f2f4811 — feat(mcp): `reyn mcp install --args` フラグ追加

`reyn mcp install <server> --args "..."` で、インストール後のサーバーコマンドに
追加引数を渡せるようにした（例：`--args "--server pyright"`）。

**変更ファイル**:
- `src/reyn/cli/commands/mcp.py` — `--args` 引数追加、`shlex.split` でパース
- `src/reyn/schemas/models.py` — `MCPInstallIROp.extra_args: list[str] | None`
- `src/reyn/op_runtime/mcp_install.py` — `server_entry["args"]` に追記
- `src/reyn/stdlib/skills/mcp_install/phases/discover.md` — `data.extra_args` を op に転送するガイダンス

---

### 38f95e9 — research: Zenn 実践者の声分析 2026-05

18 記事調査。主要知見：
- RPA デジャヴュフレーム・組織レディネスの欠如・ユーザー単位コスト語彙
- オンプレ正当性・名指し規制当局（FSA/MHLW）
- Reyn の OS =「エージェントハーネス」として VectorTech Lab 記事が独立提案

**新規ファイル**:
- `docs/deep-dives/research/landscape/zenn-practitioner-voice-2026.md`
- `docs/deep-dives/research/landscape/zenn-practitioner-voice-2026.ja.md`

---

### f9b6167 — research: Qiita 実践者の声分析 2026-05

20 記事調査。主要知見：
- **ハーネスエンジニアリング**（@miruky、99 LGTM）= マルチセッション安定性・文脈喪失・技術的負債増幅
- **PlanGate パターン**（@s977043）= コード生成前の計画承認必須 → Reyn の `ask_user` + フェーズ境界が構造実装
- サプライチェーンリスク（22,511 スキル監査で 34% に問題）
- 最多 LGTM: @ksonoda（Oracle Japan、439 LGTM）
- **結論**: Qiita の実践者は「harness engineering」に独立到達。Reyn OS がそれを構造的に実装している

**新規ファイル**:
- `docs/deep-dives/research/landscape/qiita-practitioner-voice-2026.md`
- `docs/deep-dives/research/landscape/qiita-practitioner-voice-2026.ja.md`

---

### f6d9a6e — FP-0013: エージェント認証

AI エージェントが直面する認証パターンの設計提案。

**5 コンポーネント（優先順 A→B→C→E→D）**:

| Component | 内容 | コスト |
|---|---|---|
| A | `mcp.servers.<name>.headers` フィールド（MCP HTTP Bearer ヘッダー） | SMALL |
| B | `secrets.store` に OAuthToken 型 + 自動リフレッシュ | MEDIUM |
| C | `reyn auth login <service>` CLI（Device Authorization Grant、RFC 8628） | MEDIUM |
| D | 子スキルへのスコープ限定認証情報委任（Confused Deputy 対策） | LARGE |
| E | `agent_id` を P6 イベントに伝播（METI/SOC2 監査証跡） | SMALL |

**Component A が最優先**：HTTP 型 MCP サーバー（GitHub MCP・Atlassian MCP・社内 MCP）への接続に即効。

**新規ファイル**:
- `docs/deep-dives/proposals/0013-agent-authentication.md`
- `docs/deep-dives/proposals/0013-agent-authentication.ja.md`

---

### 95a8ad4 / f69b96a / 7c2980f — FP-0014: サンドボックス実行

`exec` op の無制限 shell 実行を廃止し、ポリシー宣言＋バックエンド抽象に置き換える提案。

**設計原則（FP-0014 の核心）**:
```
SandboxPolicy（何を許可するか）← skill.md で宣言
    ↓
SandboxBackend（どう強制するか）← OS がプラットフォームに応じて選択
```
OpenBSD `pledge`/`unveil` + systemd declarative policy と同じ思想。

**5 コンポーネント（優先順 A→D→C→B→E）**:

| Component | 内容 | コスト |
|---|---|---|
| A | `SandboxPolicy` + `SandboxBackend` Protocol + `sandboxed_exec` op | SMALL |
| B | `LandlockBackend`（Linux 5.13+、seccomp 重ね）**コントリビュータ向け** | MEDIUM |
| C | `SeatbeltBackend`（macOS、sandbox-exec）+ `NoopBackend` | SMALL |
| D | `exec` op を deprecated マーク（移行コストゼロ：stdlib 利用ゼロ確認済み） | TINY |
| E | `AppleContainerBackend`（macOS 26+、延期） | LARGE |

**重要な制約**:
- 主要メンテナーの開発環境は **macOS のみ**。Component B（Landlock）は Linux 環境なしで検証不可
- Component B は明示的に**コントリビュータ向け**とマーク済み
- macOS 優先のため C → B の順

**新規ファイル**:
- `docs/deep-dives/proposals/0014-sandboxed-execution.md`
- `docs/deep-dives/proposals/0014-sandboxed-execution.ja.md`

---

### a7d31dd — landscape docs 更新（2026-05-10 調査反映）

**`reyn-strategic-priorities.md` 更新**:
- `code_exec` ギャップ → FP-0014 設計済み（コスト LARGE→MEDIUM に修正）
- Docker MCP エコシステム注記追加
- ギャップ 3.5 として FP-0013（エージェント認証）追加
- 「実装確認済み（FP 不要と判明）」セクション追加:
  - エージェント単位コスト帰属 → `cost_tab.py` に `by_agent`/`by_agent_skill` 実装済み
  - 永続メモリ → `src/reyn/memory/memory.py` 実装済み（4 タイプ）
  - マルチセッション文脈継続 → WAL + フェーズ境界復元で解決済み（P5 の設計意図通り）

**`emerging-players.md` 更新**:
- Docker MCP Catalog & Toolkit エントリ追加（Desktop 4.42、hub.docker.com/mcp、100+ サーバー）

---

### (new) — FP-0017: OSRuntime レイヤ分解

`runtime.py`（1,882 行）を垂直レイヤに分解する設計提案。
AI コーディングエージェントのコンテキストウィンドウ最適化が主目的（合計行数増加は許容）。

**4 コンポーネント（A→B→C→D）**:

| コンポーネント | 対象 | コスト |
|---|---|---|
| A | `RunState`（ミュータブル実行状態の dataclass） | SMALL |
| B | `LLMCallRecorder`（LLM 呼び出し + WAL + バジェット） | SMALL |
| C | `PhaseExecutor`（act/decide ループ） | SMALL |
| D | `RunOrchestrator`（フェーズ順序 + ライフサイクル） | MEDIUM |

**行数変化**: 1,882 行 → 5 ファイル合計 ~1,620 行（最大ファイル ~500 行）

**新規ファイル**:
- `docs/deep-dives/proposals/0017-runtime-layer-decomposition.md`
- `docs/deep-dives/proposals/0017-runtime-layer-decomposition.ja.md`

---

### (new) — FP-0016: ChatSession 責務分離

`session.py`（3,689 行）から 5 つのサービスを 3 ウェーブで抽出する設計提案。
目標: ~600 行の薄いディスパッチャに縮小。

**すでに抽出済み**: 6 サービス（2,122 行）が `chat/services/` に存在。

**3 ウェーブ**:

| ウェーブ | 対象 | コスト |
|---|---|---|
| 1 | CompactionController + SkillRunner | SMALL × 2 |
| 2 | A2AHandler + InterventionHandler | MEDIUM × 2 |
| 3 | AutoResumeHandler（FP-0011 連動） | SMALL |

**Wave 1 が最優先**: FP-0012（非同期実行）は SkillRunner が独立ユニットでないと
クリーンに実装できない。Wave 2 は Wave 1 完了後。Wave 3 は FP-0011 着地に連動。

**新規ファイル**:
- `docs/deep-dives/proposals/0016-chat-session-refactor.md`
- `docs/deep-dives/proposals/0016-chat-session-refactor.ja.md`

---

### 8817f9e — FP-0015: Event Store バックエンド抽象（優先度 LOW）

**現状の `EventStore` のパフォーマンス特性**:
- 書き込み：毎イベントで `open()`/`close()`（バッファリングなし）
- 読み込み：`iter_all()` は全 JSONL ファイルをフルスキャン（インデックスなし）
- DuckDB on JSONL はスキャンを省かない（ベクトル化・並列化で速くなるだけ）
- 真の O(log n) 読み込みには SQLite インデックスか DuckDB ネイティブ形式が必要

**設計（FP-0014 の SandboxBackend と同じパターン）**:

```python
class EventStoreBackend(Protocol):
    def write(self, event: Event) -> None: ...
    def iter_events(self, filter: EventFilter | None = None) -> Iterator[Event]: ...
```

**4 コンポーネント（優先順 A→D→B→C）**:

| Component | 内容 | コスト |
|---|---|---|
| A | `EventStoreBackend` Protocol + `JSONLBackend` 抽出（動作変更なし） | SMALL |
| D | `reyn.yaml` の `events.backend` 設定 + バックエンド選択 | SMALL |
| B | `SQLiteBackend`（sqlite3 stdlib、インデックスで O(log n) reads） | SMALL |
| C | `DuckDBBackend`（`read_json_auto` で既存 JSONL を移行なしで読む） | MEDIUM |

**優先度 LOW**：現状 JSONL は正しく動作中。OSS 後にスケール問題が実際に出てから B/C を実装。

**新規ファイル**:
- `docs/deep-dives/proposals/0015-event-store-backend.md`
- `docs/deep-dives/proposals/0015-event-store-backend.ja.md`

---

## 調査で判明した「FP 不要」事項

| 候補 | 判定 | 根拠 |
|---|---|---|
| エージェント単位コスト帰属 | 実装済み | `cost_tab.py` の `by_agent`/`by_agent_skill` |
| 永続メモリ | 実装済み | `src/reyn/memory/memory.py`（user/feedback/project/reference） |
| マルチセッション文脈継続 | 設計で解決済み | WAL + フェーズ境界復元（P5 の意図通り） |
| Docker MCP ゲートウェイ | 当面不要 | 常駐デーモン必要、Reyn の設計方針と相容れず |

---

## マージ後のアクション候補

**即効性あり（SMALL コスト）**:
1. FP-0013 Component A — `mcp.servers.<name>.headers` 追加（HTTP 型 MCP サーバー即接続）
2. FP-0014 Component A+D — `SandboxBackend` Protocol 定義 + `exec` op deprecated マーク

**中期（MEDIUM コスト）**:
3. FP-0014 Component C — `SeatbeltBackend`（macOS、dogfood 可能）
4. FP-0013 Component B — OAuth トークン自動リフレッシュ（FP-0012 非同期実行の前提）

**コントリビュータ向け**:
5. FP-0014 Component B — `LandlockBackend`（Linux 環境が必要）

**アーキテクチャ整備（Wave 1 のみ SMALL）**:
6. FP-0016 Wave 1 — CompactionController + SkillRunner 抽出（FP-0012 の前提）
7. FP-0017 Component A — RunState 抽出（LLMCallRecorder の前提、独立して SMALL）
