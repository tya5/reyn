---
type: concept
topic: [chat, compaction, context-window]
audience: [human, agent]
---

# チャット圧縮

長いチャットセッションがコンテキストウィンドウをオーバーフローしないようにする仕組みです。

## 概要

コンテキストが満杯になると、履歴の中間部分がローリングな構造化サマリーに折り畳まれます。LLM には 3 つのゾーンが提供されます：

- **Head** — 最初期のターン（生のまま、圧縮されない。元のタスクコンテキストを保持）
- **Body** — 圧縮エンジンが生成するローリングサマリー
- **Tail** — 最近のターン（生のまま、新鮮さを保持）

Head と Tail のサイズは**トークンバジェット制**です。固定のターン数ではなく、`component_weights` をモデルの実際のコンテキストウィンドウに対して割り当てます。チャットはまずウィンドウいっぱいまで生のまま蓄積され、履歴が有効なトリガーを超えたときだけ圧縮が発火します（トリガーはウィンドウ相対で派生します。絶対トークン数ではありません）。

`CompactionEngine` は OS 内部の Python ヘルパーで、LLM を直接呼び出してサマリーを生成します。stdlib スキルではありません。

## 圧縮パス

圧縮は 3 つの独立したパスから発火できます。3 つすべてが同一の `CompactionEngine` と Head/Body/Tail スライスロジックを使います。

### 1. 同期プリフレームガード

各ルーター LLM 呼び出しの前に `_maybe_force_compact_for_router` が現在の履歴の推定トークン使用量を有効なトリガーバジェット（ウィンドウ相対）と比較します。バジェットを超えている場合、LLM フレームが組み立てられる前に `force_compact_now` を同期的に呼び出します。これにより呼び出し*前に*プロンプトがバジェットを超えないことが保証されます（事後対応ではなく事前縮小）。

### 2. 自発的 compact op（LLM リクエスト）

ウィンドウが埋まってきたとき、OS は正確なトークン残量を含む `## Context window` ヘッダーをコンテキストサイズシグナルとして注入します。モデルはこれに応じて `compact` Control IR op を送信できます。現在の軸（chat またはフェーズ）でオンデマンド圧縮が発火し、解放されたトークンと新しいヘッドルームが返されます。op コントラクトは [`control-ir.md`](../../reference/runtime/control-ir.md) を参照してください。

### 3. `retry_loop` オーバーフロー安全網

プリフレームガードのトークン推定が過小評価でルーターがコンテキスト長エラーを発生させた場合、`retry_loop` が引き継ぎます。Head、Tail、生の中間部分を最小バジェットに向けて単調に縮小します（各イテレーションが縮小可能なバジェットを減らすため、終了が保証されます）。すべてのバジェットがフロアに達したとき、オーバーバジェットで継続する代わりに構造化された `UnrecoveredError` を発生させます。安全キャップがイテレーション数を制限しますが、それが制限要因になることはほぼありません。これがデッドエンドなし保証です：会話が回復不能な状態にオーバーフローすることはありません。

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

トークンバジェットは精度のためデフォルトで `litellm.token_counter` を使用し、レイテンシ重視のデプロイ向けに安価な `len(text) // 4` ヒューリスティックも利用可能です（`use_chars4_estimate: true`）。

## 圧縮軸

同一エンジンが 3 つの異なる圧縮軸に対応します：

- **Chat 軸** — 会話履歴（このドキュメント）
- **プランナーステップ軸** — アクティブなプラン内の古いプランステップ結果
- **フェーズ軸** — 実行中のフェーズの act ループ内の古い `control_ir_results`

各軸に自動圧縮（フレームごと）とオンデマンドの seam（LLM がコンテキストサイズシグナルに応じて使う `compact` Control IR op）の両方があります。

## コスト可視性

`/budget` コマンドはトークンとコストの使用量を**目的別**に表示します：`main`、`phase`、`compaction`、`judge`、agent 属性バケット。オペレーターはセッション全体で圧縮エンジンがトークン支出のどれだけを消費しているかを確認できます。

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
