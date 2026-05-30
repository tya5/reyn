---
type: how-to
topic: runtime
audience: [human]
applies_to: [.reyn/events/]
---

# Events でランをデバッグする

**目的:** 保存されたイベントログのみを使用して、ランが予期しない出力を生成した（または失敗した）理由を見つける。

## ランを見つける

すべての `reyn run` は以下で終わります:

```
events saved → .reyn/events/<run_id>.jsonl
```

リプレイします:

```bash
reyn events .reyn/events/<run_id>.jsonl
```

出力はライブランと同じフォーマットで表示されます。

## よくあるデバッグの問い

### 「LLM が実際に何を見て / 何を言ったか？」

```bash
reyn events <log> --conversation
```

LLM に送信されたコンテキストフレームと生のレスポンスを順番に表示します。これが Reyn でデバッガーに最も近いものです。

### 「OS は LLM の出力をどこで拒否したか？」

```bash
reyn events <log> --filter validation_error --filter normalization_error
```

`validation_error` = 出力が選択したターゲットのスキーマに一致しなかった。
`normalization_error` = 出力をコントラクト JSON としてパースすらできなかった。

### 「なぜこの Phase がこんなに何度もヒットされたのか？」

```bash
reyn events <log> --filter phase_started --filter phase_completed
```

各 `phase_started` が訪問カウントを増加させます。同じ Phase が繰り返し表示される場合は、`phase_completed → next_phase` を見てどのループにいるかを確認します。

### 「Control IR op がなぜ拒否されたか？」

```bash
reyn events <log> --filter permission_denied
```

ペイロードに op と不足している Permission キーが含まれます。

### 「ランは訪問上限に達したか？」

```bash
reyn events <log> --filter loop_limit_exceeded
```

達している場合: Phase が本当にスタックしているか（プロンプトまたはグラフを修正）、上限が低すぎるか（`--max-phase-visits` を増やす）のどちらかです。

## イベントのフィルタリング

`--filter` と `--skip` はそれぞれイベント kind を受け取り、繰り返し指定できます:

```bash
reyn events <log> --filter llm_called --skip context_built
```

フィルターなしでは、すべてのイベントが出力されます。

## リプレイは LLM を呼び出さない

リプレイは保存されたイベントのレンダリングのみです。LLM は再呼び出しされません。プロンプトを変更して新しい動作を見たい場合は `reyn run` で再実行してください。リプレイは古いランを表示します。

## 関連情報

- [リファレンス: events](../../../reference/runtime/events.md) — 完全なイベント分類
- [コンセプト: events](../../../concepts/events.md)
- [リファレンス: run](../../../reference/cli/run.md) — `--events` フラグ
