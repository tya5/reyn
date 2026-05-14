---
type: how-to
topic: evaluation
audience: [human]
---

# 評価インフラのセットアップ

**目標:** ゴールデンデータセットに対して `reyn eval run` を実行し、pass rate で CI をゲートし、必要に応じてトレースを Langfuse または OTLP バックエンドに export する。

## 前提条件

- reyn がインストール済み（`pip install reyn`）
- 評価対象のスキル（例: `my_skill`）
- スキルが少なくとも 1 回インタラクティブ実行済みで、パーミッション承認が記録済み

---

## クイックスタート（5 ステップ）

### ステップ 1 — `reyn.yaml` に exporter を追加する

ファイル exporter はデフォルトで有効です（設定不要）。ローカルトレースアーカイブを作成するには:

```yaml
# reyn.yaml
eval:
  exporters:
    - type: file
      path: .reyn/traces/
```

トレースはスキル実行後に非同期で書き込まれます。exporter はスキル実行のレイテンシに影響しません。

### ステップ 2 — ゴールデンデータセットを作成する

`eval/golden.jsonl` を作成します — 1 行 1 JSON オブジェクト:

```jsonl
{"input": {"query": "非同期プログラミングの要点をまとめてください"}, "expected": {"summary": "非同期プログラミングはノンブロッキング I/O を可能にします..."}, "tags": ["smoke"]}
{"input": {"query": "コンテキストマネージャーとは何ですか?"}, "expected": {"summary": "コンテキストマネージャーはリソースのライフサイクルを管理します..."}, "tags": ["smoke"]}
{"input": {"query": ""}, "expected": null, "tags": ["edge-case", "empty-input"]}
```

フィールド:

| フィールド | 必須 | 説明 |
|---------|-----|-----|
| `input` | はい | スキルの実行入力として直接渡される |
| `expected` | いいえ | `mode: exact` 比較で使用。`mode: judge` では無視される |
| `tags` | いいえ | `--tags smoke` でランをフィルタリング |

### ステップ 3 — eval を実行する

```bash
reyn eval run my_skill --dataset eval/golden.jsonl --threshold 0.8
```

出力:

```
=== Eval: my_skill [3 case(s)] ===
    model=standard

━━━ case: smoke/0 ━━━
  input: 非同期プログラミングの要点をまとめてください
  ✓ score=0.91  passed

━━━ case: smoke/1 ━━━
  input: コンテキストマネージャーとは何ですか?
  ✓ score=0.87  passed

━━━ case: edge-case/empty-input ━━━
  input: (empty)
  ✗ score=0.31  failed

═══════════════════════════════════════════════════════
 ✗ 2/3 cases passed (66.7%)  threshold=0.8
 Results → .reyn/eval-results/my_skill/2026-05-14T12:00:00.jsonl
═══════════════════════════════════════════════════════
```

終了コード: `0` = 全ケース通過、`1` = スペックまたはデータセットエラー、`2` = pass rate がしきい値未満。

### ステップ 4 — レポートを確認する

```bash
reyn eval report my_skill
```

出力:

```
my_skill — 3 runs on record

  2026-05-14  dataset=eval/golden.jsonl  2/3 passed (66.7%)  model=standard
  2026-05-13  dataset=eval/golden.jsonl  3/3 passed (100%)   model=standard
```

完全な構造化 JSON を確認するには:

```bash
cat .reyn/eval-results/my_skill/2026-05-14T12:00:00.jsonl
```

### ステップ 5 — CI ステップを追加する

```yaml
# .github/workflows/eval.yml
name: Skill eval

on: [push, pull_request]

jobs:
  eval:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install reyn
      - run: reyn eval run my_skill --dataset eval/golden.jsonl --threshold 0.8
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

pass rate が 0.8 を下回るとジョブが失敗し、merge がブロックされます。

---

## Export のセットアップ

### Langfuse セルフホスト

Langfuse は OSS でセルフホスト可能です。データ主権要件がある環境に推奨します。

```yaml
# reyn.yaml
eval:
  exporters:
    - type: langfuse
      public_key: ${LANGFUSE_PUBLIC_KEY}
      secret_key: ${LANGFUSE_SECRET_KEY}
      host: https://langfuse.your-domain.example.com
```

`reyn secret` でキーを設定します:

```bash
reyn secret set LANGFUSE_PUBLIC_KEY
reyn secret set LANGFUSE_SECRET_KEY
```

トレースはスキル名をトレース名として Langfuse に表示されます。各フェーズ訪問がスパンにマッピングされます。

### OTLP（Jaeger、Grafana Tempo）

OpenTelemetry 互換バックエンドの場合:

```yaml
# reyn.yaml
eval:
  exporters:
    - type: otlp
      endpoint: http://localhost:4317   # gRPC — ローカル Jaeger
```

Grafana Cloud OTLP の場合:

```yaml
eval:
  exporters:
    - type: otlp
      endpoint: https://otlp-gateway-prod-us-central-0.grafana.net/otlp
      headers:
        Authorization: Basic ${GRAFANA_OTLP_TOKEN}
```

### IETF Agent Audit Trail

IETF Agent Audit Trail ドラフト（draft-sharif-agent-audit-trail）は identity、timing、routing、parameters をカバーする構造化ログフォーマットを定義しています。reyn の export は P6 イベントをドラフトの必須フィールドにマッピングします。

```yaml
# reyn.yaml
eval:
  exporters:
    - type: ietf_audit
      path: .reyn/audit/
      # 注意: IETF ドラフト仕様 — 標準化前にフォーマットが変更される可能性あり
```

監査ファイルは実行ごとに `.reyn/audit/<run_id>.jsonl` に書き込まれます。

### 複数の exporter

exporter は加算的です。ローカルファイルと Langfuse の両方に export するには:

```yaml
eval:
  exporters:
    - type: file
      path: .reyn/traces/
    - type: langfuse
      public_key: ${LANGFUSE_PUBLIC_KEY}
      secret_key: ${LANGFUSE_SECRET_KEY}
      host: https://langfuse.your-domain.example.com
```

---

## スキルフェーズで `judge_output` を使う

`judge_output` はフェーズが自身の出力を rubric に対してスコアリングし、結果に基づいて続行するか遷移するかを判断できる Control IR op です。rubric は常にスキルが供給します。OS はドメインを知らずに評価します。

例: 記事作成スキルが完了前に自己評価する場合:

```yaml
# phases/evaluate.md
---
input_schema: draft_article
---

ワークスペース内の記事ドラフトをレビューする。rubric に対してスコアリングする。
スコアがしきい値未満なら修正する。そうでなければ完了する。
```

LLM は `judge_output` Control IR op を emit します:

```json
{
  "op": "judge_output",
  "target": "artifact.data.body",
  "rubric": "0.0-1.0 でスコアリング: 記事は最初の段落で主な主張を明確に述べているか? 各セクションは少なくとも 1 つの具体的な例で裏付けられているか?",
  "threshold": 0.75,
  "on_fail": "transition"
}
```

`on_fail` の値:

| 値 | 動作 |
|----|------|
| `transition` | LLM が次フェーズを選択（通常は修正フェーズ） |
| `abort` | スキル実行を即座に abort |
| `continue` | スコアに関わらず実行を続行。スコアは workspace に記録のみ |

スコアは P6 イベントログに `tool_executed`（op=judge_output, score=0.72, passed=false）として記録されます。

---

## Workspace 隔離

各 `reyn eval run` ケースは隔離された workspace コピーで実行されます。本番 workspace の状態 — index 済みソース、承認、既存アーティファクト — は eval ケースから見えません。あるケースの結果が次のケースに影響しません。

この隔離は eval が通常のスキル実行と同じプロジェクトディレクトリで動作する場合でも保証されます。`.reyn/eval-results/` 出力ディレクトリが eval ランナーとプロジェクト workspace の唯一の共有書き込みパスです。

### 非インタラクティブパーミッション {#non-interactive-permissions}

`reyn eval run` はパーミッションプロンプトを表示しません。eval 実行前にスキルが必要とするパーミッションを事前承認してください:

**オプション 1 — インタラクティブで一度実行:**

```bash
reyn run my_skill '{"query": "テスト"}'
# パーミッションプロンプトを受け入れる — 選択は .reyn/approvals.yaml に永続化される
```

**オプション 2 — `reyn.yaml` で事前承認:**

```yaml
permissions:
  python.safe: allow
  file.write: allow
```

**オプション 3 — オペレーターローカルオーバーライド（gitignored）:**

```yaml
# reyn.local.yaml（gitignored — ローカル CI や dogfood 自動化向け）
permissions:
  python.safe: allow
  python.unsafe: allow
```

完全な 3 層事前承認モデルについては [コンセプト: パーミッションモデル](../concepts/permission-model.md) を参照してください。

---

## トラブルシューティング

**`reyn eval run` が code 1 で終了し「spec failed to load」と表示される**

データセットファイルの読み込みまたは JSONL のパースに問題があります。各行が有効な JSON であることを確認してください:

```bash
python -c "import json; [json.loads(l) for l in open('eval/golden.jsonl')]"
```

**ケースが「failed」ではなく「not-finished」として報告される**

スキルが eval 中にパーミッションゲートに遭遇しました（eval はプロンプトを表示しません）。上記のオプションのいずれかで必要なパーミッションを事前承認してください。失敗したケースのイベントログに `permission_denied` イベントが表示されます:

```bash
reyn events .reyn/events/<run_id>.jsonl --filter permission_denied
```

**`mode: judge` のスコアが期待より低い**

`judge_output` rubric がスコアを決定します。曖昧な rubric（「出力は良い」）は信頼性の低いスコアを生成します。rubric を具体的でテスト可能な記述に書き直してください:

- 曖昧: 「サマリーはよく書けている」
- 具体的: 「サマリーは 2-4 文。最初の文は主な結論を述べている。」

---

## 関連項目

- [コンセプト: 評価インフラ](../concepts/evaluation.md) — アーキテクチャとポジショニング
- [リファレンス: `reyn eval`](../reference/cli/eval.md) — 完全な CLI フラグリファレンス
- [コンセプト: イベント](../concepts/events.md) — P6 イベントログ
- [コンセプト: workspace](../concepts/workspace.md) — workspace 隔離モデル
- [コンセプト: パーミッションモデル](../concepts/permission-model.md) — 非インタラクティブ事前承認
- [はじめに: eval を書く](getting-started/05-writing-an-eval.md) — `eval_builder` を使った rubric ベースの eval
