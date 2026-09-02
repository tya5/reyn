---
type: concept
topic: [chat, compaction, context-window]
audience: [human, agent]
---

# チャット圧縮

長いチャットセッションがコンテキストウィンドウをオーバーフローしないようにする仕組みです。

## 概要

コンテキストが満杯になると、履歴の中間部分がローリングな構造化サマリーに折り畳まれます。LLM には 3 つのゾーンが提供されます：

- **Head** — 最初期の**未覆いの**ターン（生のまま。元のタスクコンテキストを保持）
- **Body** — 圧縮エンジンが生成するローリングサマリー
- **Tail** — 最近のターン（生のまま、新鮮さを保持）

Head と Tail のサイズは**トークンバジェット制**です。固定のターン数ではなく、`component_weights` をモデルの実際のコンテキストウィンドウに対して割り当てます。チャットはまずウィンドウいっぱいまで生のまま蓄積され、**実際に測定された**オーバーフローが起きたときだけ圧縮が発火します（ローカルな推定から先回りして予測することはありません — #5528、下記「圧縮パス」参照）。ただし発火した効果は**恒久的**です（#4954）：結果として得られる `covers_through_seq` watermark 以下のすべてのターンは、以降**すべて**の LLM 向け射影から除外されます — その後のターンでトークン合計がトリガーを再び下回っても、生のまま再送されることはありません。`history.jsonl` 自体はこれによって一切変更されません — 覆われたターンは射影からのみ除外され、`extend_history_backward` で引き続き完全に読み戻せます。恒久的に縮むのは LLM に**見える**ものだけです。

`CompactionEngine` は OS 内部の Python ヘルパーで、LLM を直接呼び出してサマリーを生成します。stdlib スキルではありません。

## 圧縮パス

圧縮は 2 つの独立したパスから発火できます。両方とも同一の `CompactionEngine` と Head/Body/Tail スライスロジックを使います。

#5528（owner 裁定）: かつて3つ目のパス — 各ルーター LLM 呼び出しの前に現在の履歴の推定トークン使用量を有効なトリガーバジェットと比較し、LLM フレームが組み立てられる前に先回りして強制圧縮する同期プリフレームガード — が存在しました。#5367 の elide 削除と同族の理由で削除されています：ローカルなトークン推定は実際のプロバイダーペイロード（システムプロンプト、ツールスキーマ、transport のラッピング、インライン media）を知り得ないため、推定に基づく事前圧縮は本来収まったはずの会話を不必要に要約・短縮する危険があり、#5296 が原則として決定し #5528 が実行しました。そのガードがあわせて行っていた別の挙動 — 新規メッセージ単体が大きすぎる場合にそれ自体を拒否する（何も落とさない。履歴に受け入れてから要約で消すのではない）— は `ContextBudgetAdvisor.enforce_new_msg_budget` として残っています。owner 自身の「force close」（#4381 PR-4）であり、圧縮とは別物だからです。

### 1. 自発的 compact op（LLM リクエスト）

ウィンドウが埋まってきたとき、OS は正確なトークン残量を含む `## Context window` ヘッダーをコンテキストサイズシグナルとして注入します。モデルはこれに応じて `compact` Control IR op を送信できます。オンデマンド圧縮が発火し、解放されたトークンと新しいヘッドルームが返されます。op コントラクトは [`control-ir.md`](../../reference/runtime/control-ir.md) を参照してください。

### 2. `retry_loop` オーバーフロー回復

ルーターの実際の LLM 呼び出しがコンテキスト長エラーを発生させた場合、`retry_loop` が引き継ぎます — プリフレームガードが無くなった今（#5528）、オーバーフローから回復する唯一のパスです。減少する測度は生の中間部分単体ではなく Head/Tail のトークン数です — 生の中間部分は（Head/Tail がまだ最小値を上回っているあいだ、そこから内容が移動してくるため）増えることがあり、圧縮自体は別に扱われ、失敗が繰り返されるたびに1ターン単位のフロアまでより小さいスライスへ分割されます。Head/Tail は増えないため、この測度は終了します：両方が最小バジェット以下になり、かつ生の中間部分がそれ以上分割できなくなった時点で、`retry_loop` はオーバーバジェットで継続したり内容を静かに失ったりする代わりに構造化された `UnrecoveredError` を発生させます。この測度自体が証明済みの終了根拠であり、独立したイテレーション上限は存在しません（`max_shrink_iterations` は #5531 PR-3 以降孤立フィールドで何も読みません。#5623 で退役済み — 値検証（`>= 1`）も外され、設定すると読み込み時に1版だけ警告が出ます — 詳細は [`reyn-yaml.md`](../../reference/config/reyn-yaml.md) 参照）。これは成功の保証ではなく構造化された失敗の保証です：`retry_loop` は永久にループしたり内容を静かに失ったりする代わりに、必ず明確に定義されたエラーで止まります — リクエストが最終的に収まることまでは保証しません。

## 圧縮の出力

`CompactionEngine` は新しいターンをセクションごとのトークンバジェット（`section_weights` から派生）を持つ 5 つのセクションに折り畳みます：

| セクション | 保持する内容 |
|---------|-----------------|
| `topic_arc` | セッションのハイレベルな流れ |
| `decisions` | 合意された選択肢と制約 |
| `pending` | 未完了タスクと未解決の疑問 |
| `session_user_facts` | ユーザーまたはプロジェクトに関する安定した事実 |
| `artifacts_referenced` | 読まれたファイル、取得した URL、MCP ツール呼び出し（パス / 行レベル） |

`covers_through_seq` は圧縮ポストプロセッサが決定論的に派生させ、結果は `history.jsonl` に `role: "summary"` エントリとして追記されます。

トークンバジェットは精度のためデフォルトで `litellm.token_counter` を使用し、レイテンシ重視のデプロイ向けに安価な `len(text) // 4` ヒューリスティックも利用可能です（`use_chars4_estimate: true`）。第三の経路は operator 設定によらず自動で発生します: `litellm.token_counter` が失敗した場合（ネットワークに本当に到達できない等）、`estimate_tokens()` は60秒のクールダウン期間だけ同じ `len(text) // 4` ヒューリスティックへフォールバックし、その後自動的に実トークナイザへ再試行します — 設定不要かつ恒久的な切り替えではありません（#4395）。

## 圧縮軸

エンジンは Chat 軸(会話履歴、このドキュメント)に対応します: 自動圧縮(フレームごと)とオンデマンドの seam(LLM がコンテキストサイズシグナルに応じて使う `compact` Control IR op)の両方があります。

## コスト可視性

`/budget` コマンドはトークンとコストの使用量を**目的別**(`compaction`、`judge`、`dogfood`)+ agent 属性バケットで表示します。オペレーターはセッション全体で圧縮エンジンがトークン支出のどれだけを消費しているかを確認できます。

## 設定（`reyn.yaml`）

```yaml
chat:
  compaction:
    # バジェット割り当て: 整数の重み、ランタイムで正規化
    # キー: head / body / tail / new_msg / compaction_batch
    component_weights:
      head:             10
      body:             5
      tail:             15
      new_msg:          10
      compaction_batch: 60

    # body 内のセクションバジェット重み、ランタイムで正規化
    section_weights:
      topic_arc:            5
      decisions:            40
      pending:              25
      session_user_facts:   10
      artifacts_referenced: 35

    # サマリー本文のトークンハードキャップ（切り詰め後）
    body_token_cap: 1500

    # true にすると litellm.token_counter の代わりに len(text)//4 を使用
    use_chars4_estimate: false
```

重みは合計が任意です（正の整数なら何でも機能します）。Reyn は起動時に正規化します。大きい値ほどそのコンポーネントにトークンバジェットが多く割り当てられます。

**削除されたキー：** `head_size`、`tail_size`、`trigger_total_tokens`、`min_compact_batch` は現在認識されません。`reyn.yaml` に存在する場合、Reyn は `DeprecationWarning` を発行して無視します。これらのキーを設定から削除してください。Head/Tail のサイジングは `component_weights` 経由のトークンバジェットになり、自動圧縮はウィンドウ相対になりました。

## トレードオフ

**保持されるもの：** トピックアーク、決定事項、保留アイテム、ユーザーファクト、参照アーティファクト（会話に関連する場合はファイル読み取り / URL 取得 / MCP ツール呼び出しのツールアクティビティが `artifacts_referenced` エントリとして記録）、生の Head および Tail ゾーン（モデルの実際のコンテキストウィンドウに相対したサイズのトークンバジェット制）。

**失われるもの：** 圧縮されたターンの逐語的表現、細かいやり取りの正確な順序。セクションバジェットはソフトです。わずかなオーバーランは次の圧縮パスで自己修正されます。

### ツール対応圧縮

`new_turns` には `tool_calls` を持つ `role="assistant"` エントリと `role="tool"` レスポンスエントリが含まれます。圧縮エンジンはこれらを構造化された入力として受け取り、呼び出しを `artifacts_referenced` に記録するかを判断します。ツールターンは通常の会話ターンと同様に Head/Tail/Body スライスにカウントされます。

圧縮はフレームの前に同期的に（パス 1）またはオンデマンドで（パス 2）実行されます。イベント `compaction_started` / `compaction_completed` / `compaction_failed` がセッションイベントログに発行されます（P6）。

## 参照

- `src/reyn/services/compaction/engine.py` — `CompactionEngine` 実装
- `src/reyn/runtime/services/compaction_controller.py` — chat 軸のワイヤリング
- [Control IR: compact](../../reference/runtime/control-ir.md#compact) — LLM リクエストの compact op
- [Events](../../reference/runtime/events.md)
