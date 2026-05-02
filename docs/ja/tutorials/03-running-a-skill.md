---
type: tutorial
topic: getting-started
audience: [human]
---

# 03 — Skill を実行する

チュートリアル 02 で `my_explainer` を作成しました。このチュートリアルでは、ランタイム側をカバーします: 入力フォーマット、よく使うフラグ、イベントログの読み方。

## 3 つの入力方法

### 自然言語（自動ラップ）

```bash
reyn run my_explainer "photosynthesis"
```

ベアな文字列は `{"type": "user_message", "data": {"text": "photosynthesis"}}` になります。Skill のエントリー Phase は `user_message`（またはそれを含むユニオン）を受け入れる必要があります。

### JSON（そのまま使用）

```bash
reyn run my_explainer '{"type": "topic_input", "data": {"topic": "photosynthesis"}}'
```

文字列は有効な artifact としてパースできる必要があります: `type` と `data` キーを持つトップレベルのオブジェクト。

### stdin

```bash
echo "photosynthesis" | reyn run my_explainer
```

位置引数と同じ自動ラップが適用されます。

## よく使うフラグ

```bash
reyn run my_explainer "photosynthesis" \
  --model strong \
  --output-language en \
  --max-phase-visits 10 \
  --strict
```

- `--model strong` — このランのみにより強力なモデルを選択します（`reyn.yaml` をオーバーライド）。
- `--output-language en` — プロジェクトのデフォルトに関わらず LLM に英語で返信するよう指示します。
- `--max-phase-visits 10` — 任意の単一 Phase への再訪問を制限します。`0` = 無制限。
- `--strict` — すべてのネスト深さで必須フィールドを強制します（デフォルト: トップレベルのみ）。

完全なリストは [common-flags ページ](../reference/cli/common-flags.md) にあります。

## 何が起きたかを確認する

すべてのランは以下で終わります:

```
events saved → .reyn/events/<run_id>.jsonl
```

リプレイするには:

```bash
reyn events .reyn/events/<run_id>.jsonl
```

LLM の会話を具体的に見るには:

```bash
reyn events .reyn/events/<run_id>.jsonl --conversation
```

特定のイベント種別でフィルタリングするには:

```bash
reyn events .reyn/events/<run_id>.jsonl --filter validation_error
```

## 何かおかしいとき

1. 不正な出力を生成した Phase の `phase_completed` イベントを見つけます。
2. モデルが返したものについて、一致する `llm_called` イベントを確認します。
3. `validation_error` が見つかった場合、モデルの出力が次のターゲットのスキーマに合いませんでした。通常は Phase の指示の問題です。

[debug-with-events](../how-to/debug-with-events.md) ハウツーでこのフローを詳しく説明します。

## 学んだこと

- 入力は位置引数（JSON または自然言語）または stdin から来ます。
- よく使うフラグは 1 回のランのみ `reyn.yaml` をオーバーライドします。
- すべてのランは再現可能な JSONL ログを残します。リプレイ時に LLM は再呼び出しされません。

## 次へ

- [チュートリアル 04 — eval を書く](04-writing-an-eval.md)
- [ハウツー: events によるデバッグ](../how-to/debug-with-events.md)
- [リファレンス: run](../reference/cli/run.md)
