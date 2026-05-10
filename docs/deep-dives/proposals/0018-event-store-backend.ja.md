# FP-0018: Event Store バックエンド抽象化 — JSONL / SQLite / DuckDB

**Status**: proposed
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)
**Priority**: LOW

---

## Summary

現在の `EventStore`（`src/reyn/events/event_store.py`）は append-only JSONL ファイルにイベントを書き込み、ローテーションで管理している。現在の規模（1 セッションあたり数十〜数百件のイベント）では正確かつ十分な実装である。Reyn が OSS 採用・高ボリュームワークロード（FP-0007 eval export、FP-0012 非同期長時間スキル）に向けて成長するにつれ、JSONL 単独の実装は読み取りパスでパフォーマンスのボトルネックになる。本提案は FP-0017 が確立した`SandboxBackend` Protocol と同じバックエンド抽象化パターンで `EventStoreBackend` Protocol を導入する。具体的なバックエンドとして、既存の JSONL ロジックをリファクタリングした `JSONLBackend`（デフォルト、移行不要）、インデックス付き再開読み取りのための `SQLiteBackend`、分析的な eval-export ワークロード向けの `DuckDBBackend` の 3 種類を定義する。

---

## Motivation

### 現在の実装 — パフォーマンス特性

**書き込みパス**（`write()`）:
- 毎回の呼び出しでファイルをオープン、追記、クローズする同期処理 — バッファリングもバッチ処理もなし。
- ローテーション閾値の確認に毎回の書き込みで `stat()` syscall が発生する。
- 現在の規模では許容範囲内；1 セッションあたりせいぜい数百件のイベントが生成される。

**読み取りパス**（`iter_all()`）:
- 毎回の呼び出しで全 JSONL ファイルを完全に順次スキャンする。
- インデックスなし：特定の `run_id` や `event_type` のイベントを見つけるには全行を読み込む必要がある。
- 現在の呼び出し元：スキル再開（WAL リプレイ）、FP-0007 eval export、`skill_resume_analyzer`。

### なぜ JSONL 単独ではインデックス付き読み取りにスケールしないか

DuckDB を JSONL 上で使用すれば Python の順次ループより高速なスキャンが可能だが（ベクトル化実行、マルチファイル並列処理、列プロジェクション）、**依然として完全スキャンを実行する** — 先頭から読まずに特定位置にスキップすることはできない。「`run_id` X のイベントを全て取得する」という操作では、JSONL+Python も JSONL+DuckDB も O(n) の処理を回避できない。

真の O(log n) 読み取りには適切なインデックスが必要となる:

- **SQLite + `(run_id, timestamp, event_type)` インデックス** — ポイントルックアップが B-tree 走査になる。
- **DuckDB ネイティブ形式または Parquet** — 列ごとの min/max 統計によるチャンクスキップが可能；`run_id` ポイントルックアップは依然 O(log n) にはならないが、全イベントの分析的集計では桁違いに高速になる。

### ユースケースのマッピング

| ユースケース | 現在のボトルネック | 最適なバックエンド |
|---|---|---|
| 再開（run_id のイベント取得） | 完全順次スキャン | SQLite（インデックス付き） |
| Eval export（特定 type のイベント集計） | 完全順次スキャン | DuckDB または SQLite |
| 監査証跡（append-only、人間が読める） | 書き込みごとの open/close | JSONL（現状維持） |
| コストタブ集計 | すでにメモリ内 | 変更不要 |

### なぜ今すぐ SQLite に切り替えないのか

JSONL には保持する価値のある実際のメリットがある:

- **人間が読める**：`tail -f events/.../*.jsonl` は現在のワークフローで最速のデバッグツールである。
- **トランザクションなしでクラッシュセーフ**：部分的な書き込みは最終行が切り捨てられるだけ；`iter_all()` はすでに不完全な行をスキップする。SQLite WAL は同等の安全性を提供するが透明性が低い。
- **依存関係ゼロ**：新たなパッケージが不要。
- **シンプルさ**：現在の 160 行の実装は数分で監査できる。

抽象化レイヤーにより、今日の移行にコミットせずにバックエンドを切り替えるオプションが維持される。JSONL はデフォルトのまま；SQLite と DuckDB は `reyn.yaml` 経由でオプトインする。

### 将来的な圧力

- **FP-0007**（eval export）は回帰分析のため複数ランにわたる大量のイベント履歴を集計する。完全スキャンのコストは履歴の深さに対して線形に増大する。
- **FP-0012**（非同期長時間スキル）は数時間にわたって高頻度のイベントを生成し、ファイル数と再開操作あたりにスキャンする総バイト数を増大させる。
- **OSS 採用**により、現在の dogfood 環境より桁違いに大きなイベント履歴を持つユーザーが現れる。

### 設計のインスピレーション — FP-0017 と同じパターン

FP-0017 は実行分離のための `SandboxBackend` Protocol を確立した：スキルがポリシーを宣言し、OS が実施バックエンドを選択する。FP-0018 は同一のパターンをイベントストレージに適用する：呼び出し元は統一された API を通じてイベントを書き込み・読み取る；OS が `reyn.yaml` からストレージバックエンドを選択する。スキルコードおよび OS のフェーズ実行コードはバックエンド型を参照しない。

---

## Proposed implementation

### 抽象化レイヤー

```
EventStoreBackend（イベントの保存方法）   ← reyn.yaml から OS が選択
    ↓
EventFilter（何を読み取るか）             ← OS 呼び出し元が渡す；LLM からは渡さない
```

### バックエンド Protocol と EventFilter

**`src/reyn/events/backend.py`**:

```python
class EventStoreBackend(Protocol):
    def write(self, event: Event) -> None: ...
    def iter_events(self, filter: EventFilter | None = None) -> Iterator[Event]: ...
    def iter_files(self) -> list[Path]: ...  # 既存呼び出し元への後方互換性
    def close(self) -> None: ...
```

**`src/reyn/events/filter.py`**:

```python
@dataclass
class EventFilter:
    run_id: str | None = None
    event_types: list[str] | None = None
    since: datetime | None = None
    until: datetime | None = None
```

`iter_files()` はファイルパスを直接検査する呼び出し元（例：`reyn events` CLI サブコマンド）への後方互換性のために残す。新しい呼び出し元はすべて `iter_events()` を使用すること。

### Component A — Protocol + JSONLBackend リファクタリング（SMALL）

`EventStoreBackend` Protocol と `EventFilter` を定義する。現在の `EventStore` を Protocol を実装する `JSONLBackend` にリファクタリングする。公開クラス `EventStore` は設定済みバックエンドをインスタンス化する薄いラッパーになる — 既存の呼び出し元はすべて変更なし。

`JSONLBackend` の振る舞いは現在の `EventStore` と同一：ファイルローテーション、時系列順序、不正行のスキップ。動作変更なし、移行不要。

**対象ファイル**:
- `src/reyn/events/backend.py` — `EventStoreBackend` Protocol
- `src/reyn/events/filter.py` — `EventFilter` dataclass
- `src/reyn/events/backends/jsonl.py` — `JSONLBackend`（現在の `EventStore` から抽出）
- `src/reyn/events/event_store.py` — バックエンドへの委譲にリファクタリング

### Component B — SQLiteBackend（SMALL）

`sqlite3` stdlib のみ；新たな依存関係なし。

```python
class SQLiteBackend(EventStoreBackend):
    # スキーマ: events(id INTEGER PRIMARY KEY, run_id TEXT, event_type TEXT,
    #                   timestamp TEXT, payload TEXT)
    # インデックス: (run_id)、(event_type)、(timestamp)
    # 書き込みバッファリング: flush_interval_seconds（デフォルト: 1.0）
    ...
```

書き込みバッファリングにより open/close オーバーヘッドを削減する：イベントをメモリ内に蓄積し、設定可能な間隔（デフォルト 1 秒）または `close()` 時に SQLite へフラッシュする。フラッシュはトランザクション内の単一 `executemany()` — イベントごとのファイルオープンよりはるかに安価である。

`iter_events(filter)` は `EventFilter` をパラメータ化 SQL `WHERE` 句に変換する。`run_id` ポイントルックアップは B-tree インデックススキャンになる：O(log n + k)（k は結果件数）。

`iter_files()` は後方互換性のため SQLite データベースパスを単一要素リストで返す。

**対象ファイル**:
- `src/reyn/events/backends/sqlite.py` — `SQLiteBackend`

### Component C — DuckDBBackend（MEDIUM）

`duckdb` PyPI パッケージが必要（追加依存関係、オプトインのみ）。

```python
class DuckDBBackend(EventStoreBackend):
    # 主要書き込み先: DuckDB ネイティブ形式
    # read_json_auto で既存 JSONL もクエリ可能 —
    # データをコピーせずに既存セッションを移行する際に有用。
    ...
```

`DuckDBBackend` は FP-0007 eval-export ワークロードに最適：列指向ストレージとベクトル化実行により、スケール時の `GROUP BY event_type` / `WHERE timestamp BETWEEN ...` クエリが JSONL+Python と比較して桁違いに高速になる。また `read_json_auto('<dir>/**/*.jsonl')` で既存 JSONL ファイルをデータ移行なしにクエリでき、人間が読める監査証跡を維持しながら分析クエリを実現する。

**対象ファイル**:
- `src/reyn/events/backends/duckdb.py` — `DuckDBBackend`

### Component D — 自動選択 + reyn.yaml 設定（SMALL）

**`reyn.yaml`**:

```yaml
events:
  backend: jsonl    # jsonl | sqlite | duckdb（デフォルト: jsonl）
  sqlite:
    flush_interval_seconds: 1.0   # 書き込みバッファフラッシュ間隔
  duckdb:
    also_query_jsonl: false       # true にすると DuckDB ファイルと並行してレガシー JSONL もクエリ
```

`src/reyn/events/event_store.py` の自動選択ロジック：`ReynConfig` から `events.backend` を読み取り、対応するバックエンドをインスタンス化する。`duckdb` が選択されているがパッケージがインストールされていない場合、明確なメッセージ付きの `ConfigError` を発生させる。

**対象ファイル**:
- `src/reyn/events/event_store.py` — バックエンドファクトリ + 設定配線
- `src/reyn/config.py` — `EventsConfig` dataclass（`backend`、`sqlite`、`duckdb` サブ設定）

---

## Priority ordering

**A → D → B → C**

Component A（Protocol 定義）は SMALL のコストでいつでも着手できる — 動作変更のない純粋なリファクタリングであり、他のすべてが構築される基盤となる。Component D（設定配線）が次に来て抽象化を設定可能にする。Component B と C は実際のパフォーマンス回帰が観察されるまで延期する。

---

## Alignment with Reyn principles

| 原則 | この FP との整合 |
|---|---|
| P3 | OS が `reyn.yaml` からバックエンドを選択する；スキルと LLM はストレージレイヤーに触れない。 |
| P5 | JSONL ファイルのルートとしてワークスペースパスが維持される；SQLite と DuckDB データベースもワークスペース配下に配置される。すべてのバックエンドが OS 管理のパスに書き込む。 |
| P6 | append-only セマンティクスとイベントスキーマはすべてのバックエンドにわたって変更されない。「すべての状態変更がイベントを発行する」という監査証跡の保証は `EventStore` の呼び出し元の性質であり、バックエンドの性質ではない。 |
| P7 | `EventStore` 呼び出し元（OS フェーズ実行、スキル再開）はバックエンド型名を参照しない。バックエンド選択は設定駆動の OS の関心事である。 |
| P8 | フェーズの instruction はストレージレイヤーの選択を記述しない；これは LLM から見えない。 |

---

## Dependencies

- **Component A**：なし — `event_store.py` の純粋な内部リファクタリング。
- **Component B**：なし — `sqlite3` は標準ライブラリ。
- **Component C**：`duckdb` PyPI パッケージ。FP-0007（eval export）がこのバックエンドの恩恵を受ける主要な消費者。強い順序依存なし — FP-0007 は JSONL バックエンドに対して進行でき、後から移行可能。
- **Component D**：Component A が先に着地している必要あり（バックエンド Protocol が存在してからファクトリがインスタンス化できる）。

---

## Cost estimate

| Component | コスト | 備考 |
|---|---|---|
| A: Protocol + JSONLBackend リファクタリング | SMALL | 純粋な抽出とリネーム；動作変更なし |
| B: `SQLiteBackend` | SMALL | `sqlite3` stdlib；インデックススキーマはシンプル |
| C: `DuckDBBackend` | MEDIUM | 追加依存関係；`read_json_auto` ブリッジが複雑さを加える |
| D: 設定配線 + 自動選択 | SMALL | Config dataclass + ファクトリメソッド |
| テスト | SMALL | Tier 1: `EventStoreBackend` 契約（write + iter_events）；Tier 2: バックエンド自動選択不変条件 |

**総作業量: MEDIUM**（ただし優先度 LOW — 実際のパフォーマンス回帰が観察されるまで B/C を延期）

---

## Related

- `src/reyn/events/event_store.py` — 現在の実装（Component A、D: リファクタリング）
- `src/reyn/events/backends/jsonl.py` — 新規ファイル（Component A）
- `src/reyn/events/backends/sqlite.py` — 新規ファイル（Component B）
- `src/reyn/events/backends/duckdb.py` — 新規ファイル（Component C）
- `src/reyn/events/backend.py` — 新規ファイル（Component A: Protocol）
- `src/reyn/events/filter.py` — 新規ファイル（Component A: EventFilter）
- `src/reyn/config.py` — `EventsConfig`（Component D）
- FP-0007（`0007-evaluation-infrastructure.md`）— Component C の主要消費者
- FP-0012（`0012-async-skill-execution.md`）— 書き込みパスに負荷をかける高頻度イベントソース；Component B の書き込みバッファリングがこれに直接対応
- FP-0017（`0017-sandboxed-execution.md`）— この FP が踏襲する `SandboxBackend` Protocol パターンを確立
