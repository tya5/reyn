---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn dogfood]
---

# `reyn dogfood`

チャットルーターのシナリオベース回帰テスト。YAML シナリオセットで期待動作を宣言し、実システムに対して実行し、リリースをまたいで合否を追跡します。

## 概要

```
reyn dogfood run <SET_YAML> [OPTIONS]
reyn dogfood coverage [--feature-map FILE] [--json] [<SET_YAML>...]
reyn dogfood report <RUN_ID> [--json]
reyn dogfood compare <BASELINE> <CANDIDATE> [--threshold FLOAT] [--json]
reyn dogfood baseline <RUN_ID> [--label NAME]
reyn dogfood publish <RUN_ID> [--repo OWNER/REPO] [--category SLUG] [--dry-run] [--template PATH] [--batch-id N] [--topic TOPIC]
```

## 説明

`reyn dogfood` は構造化されたシナリオセットでチャットルーターを動かします。各シナリオは入力プロンプトと 3 つの観測面での期待動作を宣言します。

- **reply** — 自然言語出力（judge / substring / regex）
- **events** — P6 イベントログ（must_emit / must_not_emit）
- **artifacts** — 実行で生成されるワークスペースアーティファクト

各シナリオは 4 段階のアウトカムを返します：`verified | inconclusive | refuted | blocked`。アウトカムはラン間で追跡され、`reyn dogfood compare` で自動的に回帰を検出します。

## ストレージレイアウト

```
.reyn/dogfood/
  runs/<run_id>/
    scenarios/<scenario_id>/
      output.json       # reply + 検証結果
      events.jsonl      # P6 イベントログ
      artifacts/        # ワークスペーススナップショット
    summary.json        # 4 段階集計 + Brier スコア
  baselines/<label>/    # 名前付きベースラインへのシンボリックリンク
```

## アウトカムスケール

| アウトカム | 意味 |
|-----------|------|
| `verified` | 全 verifier が合格。 |
| `inconclusive` | 合否を判定できなかった（例：judge の不確かさ）。 |
| `refuted` | 少なくとも 1 つの verifier が失敗。 |
| `blocked` | シナリオを実行できなかった（例：パーミッション拒否、エージェントエラー）。 |

アウトカムの順序（悪→良）：`blocked < refuted < inconclusive < verified`

---

## `reyn dogfood run` — シナリオセットの実行

YAML ファイルの全シナリオをチャットルーター経由で実行し、結果を記録します。

### 概要

```
reyn dogfood run <SET_YAML> [OPTIONS]
```

### 位置引数

| 名前 | 説明 |
|------|------|
| `SET_YAML` | シナリオセット YAML ファイルのパス（例：`dogfood/scenarios/chat_router_smoke.yaml`）。 |

### オプション

| フラグ | デフォルト | 説明 |
|--------|-----------|------|
| `--n N` | `1` | 繰り返し回数。安定性バンドには N ≥ 3 を推奨。繰り返し間の最悪ケースアウトカムが採用されます。 |
| `--replay FIXTURE_DIR` | — | 録音済み LLM フィクスチャを使ってリプレイモードで実行。ライブ LLM コールは発生しません。 |
| `--agent NAME` | `default` | チャットルーターエージェント名。 |
| `--storage DIR` | `.reyn/dogfood/runs/<run_id>` | ラン出力ディレクトリのオーバーライド。 |
| `--run-id RUN_ID` | *(自動 UUID)* | 明示的なラン ID。 |

### 終了コード

| コード | 意味 |
|--------|------|
| `0` | ラン完了（アウトカム分布を問わず）。 |
| `2` | エラー：シナリオファイルが見つからない、依存関係が未インストール。 |

### 出力例

```
dogfood run: chat_router_smoke  (3 シナリオ, n=1)

  run_id      : a1b2c3d4-...
  verified    : 2
  inconclusive: 1
  refuted     : 0
  blocked     : 0
  total       : 3
  verified %  : 66.7%
  Brier       : 0.1200

  results → .reyn/dogfood/runs/a1b2c3d4-.../summary.json
```

### 使用例

```bash
# 1 回実行
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml

# 安定性のため 5 回繰り返し
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml --n 5

# 決定論的リプレイ（LLM コストゼロ）
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml --replay dogfood/fixtures/chat_router_smoke/
```

---

## `reyn dogfood coverage` — フィーチャーマップカバレッジ

シナリオセットでカバーされているフィーチャーマップのフィーチャーを表示します。

### 概要

```
reyn dogfood coverage [--feature-map FILE] [--json] [<SET_YAML>...]
```

### 位置引数

| 名前 | 説明 |
|------|------|
| `SET_YAML...` | 0 個以上のシナリオセット YAML ファイル。省略時は `dogfood/scenarios/*.yaml` がデフォルト。 |

### オプション

| フラグ | デフォルト | 説明 |
|--------|-----------|------|
| `--feature-map FILE` | `docs/feature-map.md` | フィーチャーマップ Markdown ファイルのパス。 |
| `--json` | — | カバレッジを JSON で出力。 |

### 使用例

```bash
# デフォルト：全シナリオセット、デフォルトフィーチャーマップ
reyn dogfood coverage

# 特定ファイル + JSON 出力
reyn dogfood coverage dogfood/scenarios/chat_router_smoke.yaml --json
```

---

## `reyn dogfood report` — 保存済みランの結果表示

過去のランの 4 段階内訳と Brier スコアを表示します。

### 概要

```
reyn dogfood report <RUN_ID> [--json]
```

### 位置引数

| 名前 | 説明 |
|------|------|
| `RUN_ID` | ラン ID（UUID）またはランディレクトリのパス。 |

### オプション

| フラグ | 説明 |
|--------|------|
| `--json` | レポートを JSON で出力。 |

### 終了コード

| コード | 意味 |
|--------|------|
| `0` | レポート表示完了。 |
| `2` | ランディレクトリが見つからない、または summary.json が欠損。 |

### 出力例

```
Run: a1b2c3d4-...
Set: chat_router_smoke
Started: 2026-05-16T10:00:00+00:00
Completed: 2026-05-16T10:02:15+00:00

  verified    : 2
  inconclusive: 1
  refuted     : 0
  blocked     : 0
  total       : 3
  verified %  : 66.7%
  Brier       : 0.1200

Scenarios:
  ✓ simple_greeting                            verified
  ? complex_multi_turn                         inconclusive
  ✓ skill_dispatch_smoke                       verified
```

### 使用例

```bash
reyn dogfood report a1b2c3d4-1234-...
reyn dogfood report a1b2c3d4-1234-... --json
```

---

## `reyn dogfood compare` — 回帰差分

候補ランをベースラインと比較します。verified 率の低下が `--threshold` を超えると終了コード 1 を返します。

### 概要

```
reyn dogfood compare <BASELINE> <CANDIDATE> [--threshold FLOAT] [--json]
```

### 位置引数

| 名前 | 説明 |
|------|------|
| `BASELINE` | ベースランの ID（またはパス）。 |
| `CANDIDATE` | 候補ランの ID（またはパス）。 |

### オプション

| フラグ | デフォルト | 説明 |
|--------|-----------|------|
| `--threshold FLOAT` | `0.05` | 回帰アラートを発する verified 率の低下幅。デフォルト 0.05 = 5 パーセントポイント。 |
| `--json` | — | 比較結果を JSON で出力。 |

### 終了コード

| コード | 意味 |
|--------|------|
| `0` | 回帰なし（または閾値以内）。 |
| `1` | 回帰アラート：verified 率が `--threshold` を超えて低下。 |
| `2` | エラー：ランディレクトリが見つからない。 |

### 出力例

```
  Baseline:  a1b2c3d4-...  (66.7% verified)
  Candidate: b2c3d4e5-...  (33.3% verified)
  Delta:     -33.4pp  /  threshold=-5.0pp
  Result:    REGRESSION ALERT

Regressed scenarios (1):
  - complex_multi_turn: verified → refuted
```

### 使用例

```bash
# 2 つのランを比較
reyn dogfood compare a1b2c3d4-... b2c3d4e5-...

# 閾値を厳しくする（10pp）
reyn dogfood compare a1b2c3d4-... b2c3d4e5-... --threshold 0.10

# CI 向け：JSON 出力 + 回帰時に終了コード 1
reyn dogfood compare baseline_run candidate_run --json

# 名前付きベースラインを使用
reyn dogfood compare .reyn/dogfood/baselines/v1.2-stable b2c3d4e5-...
```

---

## `reyn dogfood baseline` — ランを名前付きベースラインとして登録

`.reyn/dogfood/baselines/<label>/` 以下に保存済みランへのシンボリックリンクを作成します。

### 概要

```
reyn dogfood baseline <RUN_ID> [--label NAME]
```

### 位置引数

| 名前 | 説明 |
|------|------|
| `RUN_ID` | ベースラインとして登録するランの ID（またはパス）。 |

### オプション

| フラグ | デフォルト | 説明 |
|--------|-----------|------|
| `--label NAME` | *(run_id)* | ベースラインの識別ラベル（例：`v1.2-stable`）。 |

### 終了コード

| コード | 意味 |
|--------|------|
| `0` | ベースライン作成（または上書き）完了。 |
| `2` | ランディレクトリが見つからない。 |

### 使用例

```bash
# デフォルトラベル（= run_id）でタグ付け
reyn dogfood baseline a1b2c3d4-...

# リリースラベルでタグ付け
reyn dogfood baseline a1b2c3d4-... --label v1.2-stable

# compare で使用
reyn dogfood compare v1.2-stable b2c3d4e5-...
```

---

## シナリオセット YAML フォーマット

シナリオセットは、チャットルーターシナリオのセットを宣言する YAML ファイルです：

```yaml
type: dogfood_scenario_set
name: chat_router_smoke
description: チャットルーターインテントディスパッチスモークテスト
covers:
  - chat-router/intent-routing
  - stdlib-skill/direct_llm

scenarios:
  - id: simple_greeting
    covers: [chat-router/intent-routing, stdlib-skill/direct_llm]
    input: "こんにちは、何ができますか?"
    expected:
      reply:
        kind: judge
        rubric:
          - 高レベルで機能を説明する
          - chat / skills / agents に言及する
      events:
        must_emit:
          - { type: skill_run_spawned, count: ">=1" }
          - { type: skill_run_completed, status: success }
        must_not_emit:
          - { type: permission_denied }
      artifacts:
        - { skill: direct_llm, present: true }
      outcome_prediction:
        verified: 0.7
        inconclusive: 0.2
        refuted: 0.05
        blocked: 0.05
```

`outcome_prediction` により Brier スコア追跡が有効になります。各バンドへの確信度を宣言することで、フレームワークが時間経過とともにキャリブレーションを測定します。

---

## `reyn dogfood publish` — バッチ Discussion を GitHub に公開

保存済みランの `summary.json` を読み込み、Markdown テンプレートから Discussion 本文をレンダリングし、設定された GitHub Discussions カテゴリにスレッドを作成します。

**認証**: `GH_TOKEN` または `GITHUB_TOKEN` 環境変数を設定してください（`gh` CLI と同じ規約）。`--dry-run` なしでどちらも未設定の場合はエラーになります。

### 概要

```
reyn dogfood publish <RUN_ID> [--repo OWNER/REPO] [--category SLUG] \
                               [--dry-run] [--template PATH] \
                               [--batch-id N] [--topic TOPIC]
```

### 位置引数

| 名前 | 説明 |
|------|------|
| `RUN_ID` | ラン ID（UUID）またはランディレクトリのパス。 |

### オプション

| フラグ | デフォルト | 説明 |
|--------|-----------|------|
| `--repo OWNER/REPO` | `tya5/reyn`（または git remote から検出） | Discussion を投稿する GitHub リポジトリ。 |
| `--category SLUG` | `dogfood-batches` | Discussion カテゴリスラグ。 |
| `--dry-run` | — | GitHub に投稿せずタイトルと本文を標準出力にレンダリング。 |
| `--template PATH` | `docs/deep-dives/contributing/templates/dogfood-discussion-template.md` | テンプレートファイルのオーバーライド。 |
| `--batch-id N` | *（summary.json から）* | バッチ番号のオーバーライド（`summary.json` に `batch_id` がない場合は必須）。 |
| `--topic TOPIC` | *（summary.json から）* | トピック文字列のオーバーライド（`summary.json` に `topic` がない場合は必須）。 |

### 認証

`GH_TOKEN` が `GITHUB_TOKEN` より優先されます。トークンには `write:discussion` スコープ（プライベートリポジトリの場合は `repo` スコープ）が必要です。

```bash
export GH_TOKEN="ghp_..."
reyn dogfood publish <RUN_ID>
```

### 終了コード

| コード | 意味 |
|--------|------|
| `0` | Discussion 作成完了（または dry-run 完了）。 |
| `1` | GitHub API エラー（ネットワーク、認証、GraphQL エラー）。 |
| `2` | エラー：ランディレクトリが見つからない、summary.json が欠損、テンプレートが見つからない。 |

### タイトル形式

```
Batch <N> (YYYY-MM-DD): <topic> — <verified_pct>% verified, <regressed_count> regressed
```

例：

```
Batch 27 (2026-05-17): chat router smoke + stdlib core — 75% verified, 1 regressed
```

### 出力例

```
Discussion created: https://github.com/tya5/reyn/discussions/42
  Title  : Batch 27 (2026-05-17): chat router smoke — 75% verified, 1 regressed
  Number : #42
```

### 使用例

```bash
# dry-run：投稿せずにレンダリング結果を確認
reyn dogfood publish a1b2c3d4-... --dry-run

# デフォルトリポジトリ（tya5/reyn）に batch-id + topic を指定して投稿
reyn dogfood publish a1b2c3d4-... --batch-id 27 --topic "chat router smoke"

# フォークに投稿
reyn dogfood publish a1b2c3d4-... --repo acme/reyn-fork

# カスタムテンプレートを使用
reyn dogfood publish a1b2c3d4-... --template path/to/my-template.md
```

---

## 関連

- [Reference: `reyn eval compare`](eval.ja.md) — スキル単位ルーブリック回帰（直交する観測面）
- [Reference: `reyn run`](run.ja.md) — ヘッドレスなスキル実行（同じ Agent.run パス）
- [Concepts: events](../../concepts/events.md) — P6 イベントログ
- [Deep dive: dogfood discipline](../../deep-dives/contributing/dogfood-discipline.ja.md) — 4 段階アウトカム + 9 原則フレームワーク
- [Deep dive: dogfood reporting](../../deep-dives/contributing/dogfood-reporting.ja.md) — Discussion 形式 + Issue 起票ガイド
- [Dogfood シナリオフレームワーク](../../deep-dives/proposals/0036-dogfood-scenario-framework.md) — 完全設計仕様
