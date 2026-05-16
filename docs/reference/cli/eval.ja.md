---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn eval]
---

# `reyn eval`

Skill を評価します。3 つのサブコマンド:

| サブコマンド | 説明 |
|------------|------|
| `run` | ゴールデン JSONL データセットに対してスキルを実行し、pass rate で CI をゲート |
| `report` | スキルの過去の `reyn eval run` 結果をサマリー表示 |
| `compare` | P6 イベントログを使用して 2 つのスキルバージョン間の pass rate を比較 |
| `spec` | レガシー: `eval.md` スペックファイルを非インタラクティブに実行（後方互換） |

## 概要

```
reyn eval run <SKILL_NAME> [OPTIONS]
reyn eval report <SKILL_NAME> [OPTIONS]
reyn eval compare <SKILL_NAME> [OPTIONS]
reyn eval spec FILE [OPTIONS]
```

## 非インタラクティブ制約

すべての `reyn eval` サブコマンドは非インタラクティブです。ターゲット Skill が必要とするすべての Permission は事前承認されている必要があります:

- ターゲットをインタラクティブで一度実行（`reyn run <target> "<sample>"`）してプロンプトを受け入れる。選択は `.reyn/approvals.yaml` に永続化されます。または
- `reyn.yaml` にプロジェクト全体の付与を設定:

```yaml
permissions:
  python.safe: allow
  python.unsafe: allow   # ランタイムの --allow-unsafe-python も必要
```

事前承認がない場合、ターゲットランは失敗し、ケースは未完了として報告されます。

## 関連情報

- [run.md](run.md) — `reyn run`（基盤となる実行パス）
- [リファレンス: stdlib/eval](../stdlib/eval.md) — eval Skill が生成するもの
- [リファレンス: stdlib/eval_builder](../stdlib/eval_builder.md) — スペックファイルを生成
- [リファレンス: permissions](../config/permissions.md) — 事前承認のメカニズム

---

## `reyn eval run` — ゴールデンデータセット実行

JSONL ゴールデンデータセットに対してスキルを実行し、pass rate で CI をゲートします。

### 概要

```
reyn eval run <SKILL_NAME> --dataset <FILE> [OPTIONS]
```

### 位置引数

| 名前 | 説明 |
|-----|-----|
| `SKILL_NAME` | 評価するスキルの名前（標準スキルルックアップ順で解決）。 |

### オプション

| フラグ | 説明 |
|------|-----|
| `--dataset FILE` | ゴールデン JSONL データセットへのパス。**必須。** 各行は `input` フィールドを持つ JSON オブジェクト。`expected` と `tags` は任意。 |
| `--threshold FLOAT` | exit code 0 の最小 pass rate（0.0〜1.0）。デフォルト: `0.0`（全結果を記録し、rate では失敗しない）。 |
| `--tags TAG[,TAG...]` | `tags` 配列に指定したタグのいずれかを含むケースのみ実行。 |
| `--mode MODE` | 比較モード: `judge`（デフォルト、`judge_output` で LLM スコアリング）または `exact`（`expected` との完全一致）。 |
| `--model MODEL` | モデルクラス（`light`/`standard`/`strong`）または LiteLLM モデル文字列。デフォルトは `reyn.yaml` から。 |
| `--output-language LANG` | スキルに渡す出力言語コード。デフォルトは `reyn.yaml` から。 |
| `--max-phase-visits N` | ケースごとの単一フェーズ再訪問の上限。`0` = 無制限。デフォルトは `reyn.yaml` または `25`。 |

### 終了コード

| コード | 意味 |
|------|-----|
| `0` | pass rate が `--threshold` 以上（またはしきい値未設定）。 |
| `1` | データセットファイルが見つからない、JSONL が不正、またはスキルが見つからない。 |
| `2` | pass rate が `--threshold` 未満。 |

### 出力

ケースごとのサマリー行が stdout に表示されます:

```
=== Eval: my_skill [3 case(s)] ===
    model=standard

━━━ case: smoke/0 ━━━
  input: 非同期プログラミングの要点をまとめてください
  ✓ score=0.91  passed

━━━ case: edge-case/empty-input ━━━
  input: (empty)
  ✗ score=0.31  failed

═══════════════════════════════════════════════════════
 ✗ 2/3 cases passed (66.7%)  threshold=0.8
 Results → .reyn/eval-results/my_skill/2026-05-14T12:00:00.jsonl
═══════════════════════════════════════════════════════
```

完全な構造化結果は `.reyn/eval-results/<skill>/<timestamp>.jsonl` に書き込まれます。各行はケースの入力、expected、実際の `final_output`、スコア、passed フラグ、`skill_version_hash` を記録します。

### Workspace 隔離

各ケースは隔離された workspace コピーで実行されます。本番 workspace の状態（index 済みソース、承認、既存アーティファクト）は eval ケースから見えません。あるケースの結果が次のケースに影響しません。

### 非インタラクティブ制約

`reyn eval run` はプロンプトを表示しません。スキルが必要とするすべてのパーミッションは事前承認されている必要があります。[非インタラクティブ事前承認ガイド](../../guide/evaluation.md#non-interactive-permissions) または [リファレンス: permissions](../config/permissions.md) を参照してください。

### 例

```bash
# ゴールデンデータセットに対して実行し、pass rate が 80% を下回ったら CI を失敗させる
reyn eval run my_skill --dataset eval/golden.jsonl --threshold 0.8

# smoke タグのケースのみ実行
reyn eval run my_skill --dataset eval/golden.jsonl --tags smoke --threshold 1.0

# 完全一致比較（データセットの全行に 'expected' が必要）
reyn eval run my_skill --dataset eval/golden.jsonl --mode exact

# 開発中の高速イテレーション向けに安価なモデルを使用
reyn eval run my_skill --dataset eval/golden.jsonl --model light
```

---

## `reyn eval report` — 結果サマリー（FP-0007）

スキルの過去の `reyn eval run` 結果をサマリー表示します。

### 概要

```
reyn eval report <SKILL_NAME> [OPTIONS]
```

### 位置引数

| 名前 | 説明 |
|-----|-----|
| `SKILL_NAME` | 結果を表示するスキルの名前。 |

### オプション

| フラグ | 説明 |
|------|-----|
| `--limit N` | 表示する最新ランの件数。デフォルト: `10`。 |
| `--json` | デフォルトのテーブル形式の代わりに JSON 配列として出力。 |
| `--dataset FILE` | このデータセットファイルを使用したランのみにフィルタリング。 |

### 終了コード

| コード | 意味 |
|------|-----|
| `0` | 結果が見つかり表示された（結果なしのケースを含む）。 |
| `1` | スキルが見つからない、または `.reyn/eval-results/` の読み取りエラー。 |

### 出力

デフォルトのテーブル形式:

```
my_skill — 3 runs on record

  2026-05-14  dataset=eval/golden.jsonl  2/3 passed (66.7%)  model=standard
  2026-05-13  dataset=eval/golden.jsonl  3/3 passed (100%)   model=standard
  2026-05-12  dataset=eval/golden.jsonl  1/3 passed (33.3%)  model=light
```

結果が記録されていない場合:

```
No eval results found for 'my_skill'.
Try: reyn eval run my_skill --dataset eval/golden.jsonl
```

### 例

```bash
# 最新 10 ランを表示
reyn eval report my_skill

# マシン可読出力
reyn eval report my_skill --json

# 特定のデータセットに対するランのみフィルタリング
reyn eval report my_skill --dataset eval/golden.jsonl --limit 5
```

---

## `reyn eval compare` — バージョン回帰比較（FP-0006 A + FP-0007 C）

P6 イベントログを使用して、2 つのバージョン間でスキルの pass rate を比較します。`run_skill_started` イベントの `skill_version_hash` フィールドで集計するため、追加のスキル実行は不要です。

### 概要

```
reyn eval compare <SKILL_NAME> [OPTIONS]
```

### 位置引数

| 名前 | 説明 |
|------|-------------|
| `SKILL_NAME` | 比較するスキルの名前（標準スキルルックアップ順で解決）。 |

### オプション

| フラグ | 説明 |
|------|-------------|
| `--baseline HASH_OR_LABEL` | ベースラインバージョンのハッシュプレフィックスまたはラベル。省略時は自動選択（後述の自動ベースライン選択ルールを参照）。 |
| `--candidate HASH_OR_LABEL` | 比較対象バージョンのハッシュプレフィックスまたはラベル。省略時は最新ハッシュを自動選択。 |
| `--threshold FLOAT` | この値以上の低下で回帰アラート（exit code 1）を発動。デフォルト: `0.05`（5 パーセントポイント低下でアラート）。 |
| `--format FORMAT` | 出力形式: `text`（デフォルト）または `json`。 |
| `--dataset FILE` | 特定のゴールデンデータセットを使用したランのみにフィルタリング。省略可。 |
| `--since DATE` | この ISO 日付以降のランのみ対象。省略可。 |

### 自動ベースライン選択ルール

`--baseline` を省略した場合、対象スキルの `.reyn/events/*.jsonl` から `skill_version_hash` 値を読み取り、初回出現タイムスタンプ順で以下を選択します:

- **candidate** = 最も新しいハッシュ
- **baseline** = 2 番目に新しいハッシュ

ログ内に 2 種類未満のハッシュしか存在しない場合、exit code 2 と説明メッセージで終了します。

### 終了コード

| コード | 意味 |
|------|---------|
| `0` | 候補の pass rate がベースライン − threshold 以上。回帰なし。 |
| `1` | 回帰アラート: 候補の pass rate がベースラインより `--threshold` 以上低下。 |
| `2` | エラー: スキルが見つからない、バージョン履歴不足、または I/O 失敗。 |

### text フォーマット例

```
reyn eval compare my_skill

  Skill:     my_skill
  Baseline:  sha:abc12345  (72% pass, 50 ラン中 36 通過)  2026-05-01 〜 2026-05-05
  Candidate: sha:def67890  (88% pass, 50 ラン中 44 通過)  2026-05-05 〜 2026-05-15
  Delta:     +16pp  /  threshold=-5pp
  Result:    OK — 回帰なし
```

### json フォーマット例

```bash
reyn eval compare my_skill --format json
```

```json
{
  "skill": "my_skill",
  "baseline": {
    "hash": "abc123456789abcdef...",
    "pass_rate": 0.72,
    "run_count": 50,
    "date_range": ["2026-05-01", "2026-05-05"]
  },
  "candidate": {
    "hash": "def678901234567890...",
    "pass_rate": 0.88,
    "run_count": 50,
    "date_range": ["2026-05-05", "2026-05-15"]
  },
  "delta_pp": 16.0,
  "threshold_pp": -5.0,
  "regression": false
}
```

### 参照: `skill_version_hash`

`reyn eval compare` は実行時のスキル `skill.md` の sha256 を持つ `skill_version_hash` フィールドに依存します。フィールドの契約は [FP-0006 Component A](../../deep-dives/proposals/0006-skill-self-improvement.ja.md)、イベントエンベロープは [リファレンス: events](../runtime/events.md) を参照してください。

### 例

```bash
# ベースラインと候補を自動選択
reyn eval compare my_skill

# 特定のハッシュを指定して比較
reyn eval compare my_skill --baseline abc123 --candidate def456

# pass rate が 10pp 以上低下したら失敗
reyn eval compare my_skill --threshold 0.10

# CI 向けマシン可読出力
reyn eval compare my_skill --format json --threshold 0.05
```

---

## `reyn eval spec` — レガシースペック実行

`eval.md` スペックファイルをターゲット Skill に対して非インタラクティブに実行します。各ケースはルーブリック基準に対して Phase ごとに採点されます。ケースごとの結果と全体のサマリーが `.reyn/eval-results/` に書き込まれます。

### 概要

```
reyn eval spec FILE [OPTIONS]
```

### 位置引数

| 名前 | 説明 |
|------|------|
| `FILE` | eval スペック Markdown へのパス（例: `reyn/local/my_skill/eval.md`）。スペックは `skill_dsl_path` frontmatter フィールドでターゲット Skill を参照します。 |

### オプション

| フラグ | 説明 |
|------|------|
| `--model MODEL` | モデルクラス（`light`/`standard`/`strong`）または LiteLLM モデル文字列。**優先度:** CLI > スペック > `reyn.yaml`。 |
| `--dsl-root DIR` | ターゲット Skill の DSL ルートオーバーライド。デフォルトでは Skill パスから推論されます。 |
| `--output-language LANG` | eval Skill とターゲット Skill の両方に渡される出力言語コード。デフォルトは `reyn.yaml` から。 |
| `--max-phase-visits N` | ケースごとの単一 Phase 再訪問の上限。`0` = 無制限。デフォルトは `reyn.yaml` または `25`。 |

### 終了コード

| コード | 意味 |
|------|------|
| `0` | すべてのケースが通過 |
| `1` | スペックの読み込みに失敗（例: 不正な eval.md） |
| `2` | 1 つ以上のケースが基準に失敗 |

### 例

```bash
reyn eval spec reyn/project/article_writer/eval.md
reyn eval spec reyn/local/my_skill/eval.md --model strong
```

---

## 関連情報

- [run.md](run.md) — `reyn run`（基盤となる実行パス）
- [コンセプト: 評価インフラ](../../concepts/evaluation.md) — アーキテクチャ概要と競合比較
- [ガイド: 評価インフラのセットアップ](../../guide/evaluation.md) — クイックスタート、export バックエンド、CI 連携
- [リファレンス: `reyn.yaml`](../config/reyn-yaml.md) — `eval.exporters` 設定
- [リファレンス: control-ir](../runtime/control-ir.md) — `judge_output` op スキーマ
- [リファレンス: stdlib/eval](../stdlib/eval.md) — eval Skill が生成するもの
- [リファレンス: permissions](../config/permissions.md) — 事前承認のメカニズム
