# FP-0009: Operational Intelligence — イベントログの RAG インデックス化

**Status**: **Components A + C + D 着地** 2026-05-15; B proposed (= FP-0001 待ち)
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)

---

## Landing notes (2026-05-15)

Component A — `index_events` stdlib スキル着地。P6 events を run 単位でチャンク化し RAG インデックスに書き込む。任意フェーズから `recall(sources=["events"], ...)` クエリが使用可能になった。インクリメンタル indexing は `.reyn/index/events_cursor` で管理。

Component C — recall usage パターンのドキュメント着地。`docs/concepts/data-retrieval/operational-intelligence.md` に `recall(sources=["events"])` の利用ガイドを追加。

Component D — `ops_report` stdlib スキル着地。events index が存在すれば preferred path として使用し、`index_events` 未実行の場合は raw events log walk にフォールバック。

Component B（FP-0001 経由の cron 定期実行）は proposed のまま — FP-0001 A2A task lifecycle 着地待ち。

---

## Landing notes — 2026-05-15 post-dogfood fixes

初回着地後のコードレビューと dogfood リテストで 6 件の問題が発覚し、同日中にすべて修正された。

**R-1: `collect_aggregate` パブリック名の欠落（commit `5f56005`）**
`ops_report` の `skill.md` が `_collect_aggregate`（プライベート関数）を参照していたが、
実装に該当する公開 API が存在しなかった（スキーマ見落とし）。`aggregate.py` に
`collect_aggregate(artifact: dict) -> dict` をパブリック関数として追加 — recall path を優先し、
index 未実行時は raw events walk にフォールバックする設計。

**R-2: `event_chunker.py` ステータス導出バグ（commit `5f56005`）**
削除予定コード内の条件式が `"failed" if "fail" in ... else "failed"` と両分岐同一になっており、
`aborted` ステータスに到達不能だった（コードロジック誤り）。条件式を修正し、
`aborted` が正しくキャプチャされるように変更。

**BUG-1: `index_events` scan フェーズの artifact_ref 漏洩（commit `982bc2a`）**
scan フェーズの前処理器が 67KB の `event_files` リストをアーティファクトに格納したため、
`ARTIFACT_REF_THRESHOLD`（8KB）を超えて `artifact_ref` に昇格。scan フェーズは
`allowed_ops: []` のためアーティファクト参照を逆引きできず、LLM が存在しないパスを
生成 → 0 チャンクインデックスという連鎖障害（P5/P8 違反: LLM にファイルシステム列挙を
キャリーさせるべきでない）。前処理器の出力を `event_files_count` + `oldest_timestamp` +
`newest_timestamp`（~300 バイト）に最小化。postprocessor がランタイムで再 glob して解決。

**BUG-3: `ops_report` の `phases/collect.md` が存在しない（commit `5589906`）**
`skill.md` のインラインフェーズ宣言（`phases.collect`）は DSL コンパイラに無視されており、
ファイルベースのフェーズが存在しないためコンパイルエラーとなっていた（`skill.md` インライン
フェーズは DSL 仕様外）。`phases/collect.md` をファイルとして作成し、同じ前処理器を持つ
フェーズとして実体化。`skill.md` の misleading インラインブロックを削除。
コンパイル + フェーズファイル存在を検証する regression test 2 件を追加。

**python step の permission mode 宣言（commit `a41d52a` → `9025e1e` + `a0cb630`）**
`aggregate.py` がモジュールレベルで `glob` を import しているため、`mode: safe` では
非インタラクティブ実行コンテキストで自動拒否されていた。`aggregate.py` はオペレーター
管理のファイルシステム状態を参照する意図的な設計（`docs/concepts/python-safe-mode.md`
の「operator state ingress」区分に相当）であり、`mode: unsafe` の正直な宣言が適切。
その後のアーキテクチャ修正（`9025e1e` + `a0cb630`）で `mode: safe` の stdlib 自動許可
起動ガードのホールを閉じ、真に安全な stdlib python の safe mode 動作を整合させた。

**Y-1: chunker 死コード統合（commit `ce23bdc`）（コードレビュー起点）**
`event_chunker.py`（669 LoC）が `chunkers.py` と重複していた。有用な部分を
`chunkers.py` に統合し `event_chunker.py` を削除。約 400 LoC の死コード除去。

---

Post-fix dogfood確認: `reyn run index_events` で 38 events indexed → SQLite 5 unique chunks、 `reyn run ops_report` で recall path 経由 ($0.0016)、 全 E2E pipeline 動作確認済。

---

## Summary

P6 イベントログ（`.reyn/events/*.jsonl`）を `index_docs` + `recall` op の RAG インフラ上で
インデックス化することで、Reyn が自分の実行履歴を知識ベースとして活用できるようにする。
「監査用記録」だったイベントログが「operational intelligence」になる。

FP-0006（スキル自己改善）・FP-0007（評価インフラ）・FP-0008（SWE-bench）の
`collect_traces` / レポート生成 / 過去事例参照が、この基盤の上に自然に乗る。

---

## Motivation

### P6 + RAG の組み合わせが生む構造

```
通常の RAG:         外部ドキュメント → index → recall → 回答生成
                              ↓
Operational Intelligence:  自分の実行履歴 → index → recall → 自分の改善・分析
```

P6 は append-only で全実行履歴を持つ。RAG Phase 1（ADR-0033）が landed したことで、
この履歴をセマンティック検索可能にする条件が整った。

### 線形スキャンとの違い

現状の `read_file(events/*.jsonl)` は全イベントを読む必要がある。

```
イベントが 10,000 件蓄積した場合:
  read_file: 全件スキャン → コンテキスト溢れ・コスト増大
  recall op: "my_skill の phase2 失敗パターン" → 関連 20 件を semantic 取得
```

運用が長くなるほど線形スキャンは非現実的になり、セマンティック検索の優位が増す。

### 活用ユースケース

| ユースケース | クエリ例 | 活用先 |
|---|---|---|
| スキル自己改善 | 「my_skill の verify フェーズでの失敗パターン」 | FP-0006 collect_traces |
| 評価レポート | 「先週のコスト上位スキルと失敗理由」 | FP-0007 |
| 過去事例参照 | 「django リポジトリへの過去の修正でうまくいったアプローチ」 | FP-0008 SWE-bench |
| デバッグ | 「PermissionError が最後に起きたのはいつ・どう解決したか」 | 一般用途 |
| コスト分析 | 「月間コストが急増した日のスキル実行履歴」 | 運用 |

---

## 設計の核心：チャンク単位は「1 run」

イベントは 1 行 = 1 イベントのJSONL だが、意味的なまとまりは **1 run**（start → complete）。

```jsonl
{"type": "run_skill_started",   "data": {"skill": "my_skill", "skill_version_hash": "abc"}}
{"type": "skill_node_started",  "data": {"node": "explore"}}
{"type": "tool_executed",       "data": {"op": "grep", "status": "ok"}}
{"type": "skill_node_completed","data": {"node": "explore"}}
...
{"type": "run_skill_completed", "data": {"skill": "my_skill", "status": "success"}}
```

これを 1 チャンクに変換:

```
[run chunk]
skill: my_skill
version_hash: abc123
timestamp: 2026-05-10T09:15:00
status: success
duration_seconds: 43
phases: explore → plan → apply → verify → report
errors: []
tool_calls: grep(×3), read_file(×5), edit_file(×2), shell(×1)
cost_usd: 0.18
```

この形式なら「失敗した run」「特定フェーズでエラーが出た run」「コストが高い run」を
セマンティック検索で効率的に取得できる。

---

## Proposed implementation

### Component A — `index_events` stdlib スキル（MEDIUM）

イベント JSONL を run 単位でチャンク化し、RAG インデックスに書き込むスキル。

```
src/reyn/stdlib/skills/index_events/
  skill.md
  phases/
    scan.md          ← 新規イベントの範囲を特定（incremental）
    chunk.md         ← run 単位でチャンク化
    index.md         ← embed + index_write op でインデックス化
```

**incremental indexing の仕組み**:

カーソルファイル `.reyn/index/events_cursor` に最終インデックス済みタイムスタンプを保存。
次回実行時はそれ以降のイベントのみ処理する。

```
scan フェーズ:
  read_file(.reyn/index/events_cursor) → last_indexed_at
  glob_files(events/*.jsonl) → 対象ファイル一覧
  新規イベント（last_indexed_at 以降）を特定

chunk フェーズ:
  各 run（run_skill_started → run_skill_completed）を 1 チャンクに変換
  失敗 run は error detail を追加フィールドとして保持

index フェーズ:
  embed op → run チャンクのベクトル化
  index_write op → SqliteIndexBackend に書き込み
  write_file(.reyn/index/events_cursor) → カーソル更新
```

**skill.md frontmatter の骨格**:

```yaml
---
name: index_events
description: P6 イベントログを run 単位でインデックス化する — operational intelligence の基盤
entry_phase: scan
graph:
  scan:  [chunk]
  chunk: [index]
  index: []
final_output_schema: index_events_summary
input_schema:
  since: string | null    # ISO timestamp。null = カーソルから自動取得
  skills: list[str] | null  # 特定スキルのみ対象。null = 全スキル
permissions:
  file:
    read: [".reyn/events/", ".reyn/index/"]
    write: [".reyn/index/"]   # デフォルトゾーン内なので宣言不要だが明示
---
```

### Component B — 定期インデックス更新（SMALL）

FP-0001（A2A task lifecycle）の cron 機構と接続し、
`index_events` を定期実行する設定を追加。

```yaml
# reyn.yaml
operational_intelligence:
  index_events:
    enabled: true
    schedule: "0 */6 * * *"   # 6 時間ごと（デフォルト）
    skills: null               # null = 全スキル
```

手動実行:
```
reyn run index_events
reyn run index_events --input '{"since": "2026-05-01T00:00:00"}'
```

### Component C — recall op からの利用パターン（SMALL）

`index_events` でインデックス化されたイベントは、
既存の `recall` op でそのまま検索できる（新規実装不要）。

```yaml
# スキルの任意フェーズから
- op: recall
  query: "{{ skill_name }} の verify フェーズでの失敗パターン"
  sources: ["events"]   # index_events が登録したソース名
  top_k: 10
```

FP-0006 `collect_traces` フェーズの実装:

```markdown
# collect_traces（FP-0006 Component C の実装）

recall op で対象スキルの失敗パターンを取得:
  query: "{{ input.skill_name }} failure error phase"
  sources: ["events"]
  top_k: 20

結果を traces_summary.md として workspace に保存。
index_events が未実行の場合は read_file(events/*.jsonl) にフォールバック。
```

### Component D — 組み込みクエリパターン（SMALL）

よく使うクエリをスキルとして提供。ユーザーが `reyn run` で即使える。

```
src/reyn/stdlib/skills/ops_report/
  skill.md    ← "先週の実行サマリーを出力する" レポートスキル
```

レポートスキルの出力例:

```
[週次 ops レポート 2026-W19]
実行スキル: 5 種類、合計 127 回
成功率: 91.3% (116/127)
平均コスト: $0.21 / run
失敗頻度が高いスキル: swe_bench (3/10 失敗)
  → 主な原因: verify フェーズでのテスト実行タイムアウト (shell op 60s 上限)
  → 推奨: FP-0004 の safety.timeout.phase_seconds を延長
```

---

## RAG Phase 1 との関係

`index_events` は `index_docs` の「イベントログ特化バリアント」として設計する。

| | `index_docs` | `index_events` |
|---|---|---|
| 入力ソース | ドキュメントファイル（.md / .txt / etc.）| P6 イベント JSONL |
| チャンク単位 | LLM が戦略決定（文書構造に依存）| run 単位（固定）|
| チャンク内容 | ドキュメントの一節 | run サマリー（構造化）|
| incremental | ファイルの hash 変化で判断 | タイムスタンプカーソル |
| バックエンド | SqliteIndexBackend（共通）| SqliteIndexBackend（共通）|

OS レイヤーの変更は不要。スキルとして実装するため P7 遵守。

---

## Dependencies

- ADR-0033 RAG Phase 1（landed、commit 1e6f153）— `embed` / `index_write` / `recall` op が前提
- `src/reyn/stdlib/skills/index_docs/` — 実装パターンの参考（chunkers.py のアプローチ）
- FP-0001（A2A task lifecycle）— Component B の cron 定期実行
- FP-0006（スキル自己改善）— `collect_traces` がこの基盤を使用
- FP-0007（評価インフラ）— evaluation report がこの基盤を使用
- FP-0008（SWE-bench）— 過去事例参照がこの基盤を使用

前提 PR: ADR-0033 Phase 1（✅ 完了）。FP-0001 は Component B のみの依存で、
Component A / C / D は独立実装可能。

---

## Cost estimate

**合計: MEDIUM**

| タスク | コスト | 備考 |
|---|---|---|
| Component A: `index_events` スキル（3 フェーズ）| MEDIUM | run 単位チャンク変換ロジックが主 |
| Component B: 定期実行設定（reyn.yaml + cron）| SMALL | FP-0001 が前提 |
| Component C: recall op からの利用パターン文書化 | SMALL | 実装不要。スキル設計ガイドの追記のみ |
| Component D: `ops_report` スキル | SMALL | レポート出力スキル |

ボトルネックは **Component A の chunk フェーズ**（run 境界の検出と
失敗情報の適切な要約フォーマット）。

---

## Related

- `src/reyn/events/events.py` — P6 イベント基盤
- `src/reyn/index/` — IndexBackend + SourceManifest（ADR-0033 landed）
- `src/reyn/op_runtime/recall.py` — recall macro op（ADR-0033 landed）
- `src/reyn/stdlib/skills/index_docs/` — 実装参考
- ADR-0033 (`docs/deep-dives/decisions/0033-rag-extensible-os.md`) — RAG 設計
- FP-0006 (`0006-skill-self-improvement.md`) — collect_traces の利用元
- FP-0007 (`0007-evaluation-infrastructure.md`) — 評価レポートの利用元
- FP-0008 (`0008-swe-bench-integration.md`) — 過去事例参照の利用元
