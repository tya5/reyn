---
type: concept
topic: architecture
audience: [human, agent]
---

# Reliability Engineering

> **状態: partially stale。** このページは削除済みの phase-graph skill engine を
> 前提に書かれています。LLM 呼び出しタイムアウトのセクションは現行かつ影響を受けません
> — `docs/reference/config/reyn-yaml.md` で現行確認済み(EN 版にある LLM router
> resilience セクションはこの ja 版に未反映という別の EN/JA 差分もあります)。
> 「Python preprocessor タイムアウト」セクションは、現行の pipeline DSL に
> 存在しないステップ種別(`python` preprocessor)を記述しています
> (`docs/reference/runtime/pipeline-dsl.md` で確認済み — 該当ステップ種別なし)。
> 現行の crash-recovery/WAL 基盤をカバーする書き直しは follow-up として
> 追跡されています。当面は [Time-travel](../runtime/time-travel.ja.md) と
> [Events](../runtime/events.ja.md) を参照してください。

agent を障害から回復させること: スキーマ検証、拒否時の再プロンプト、ループ上限、ステップごとのタイムアウト、そして（より長期的には）リトライポリシーとチェックポイント/再開。目標は「LLM が間違えても、システムが定義された状態にとどまること」です。

## Reyn の実装方法

### LLM 呼び出しタイムアウトと一時的エラーのリトライ

各 LLM HTTP 呼び出しは LiteLLM を通じて渡される呼び出しごとのタイムアウト（`limits.llm.timeout`、デフォルト `60` 秒）と、一時的な障害（`429`、`5xx`、ネットワークリセット）に対する LiteLLM の組み込み指数バックオフリトライ（`limits.llm.max_retries`、デフォルト `3`）を持ちます。アプリケーションレベルの拒否（検証、正規化）は上記の再プロンプトループで別途処理されます。これらは異なる障害モードであり、バジェットを共有しません。

### Python preprocessor タイムアウト

`python` preprocessor ステップごとに、サブプロセス経由でウォールクロック `timeout`（デフォルト `30` 秒）が強制されます。タイムアウト時、親プロセスは子プロセスを SIGKILL し、ステップが失敗します。失敗は LLM に対してステップ結果として表面化し、LLM が反応できます。タイムアウトは偶発的に計算負荷の高い preprocessor 関数（正規表現の壊滅的なバックトラッキング、ユーザーコードの無限ループ）から保護します。

## まだ薄い部分

いくつかの信頼性プリミティブは今日意図的にシンプルで、深化はロードマップにあります:

**チェックポイント/再開は現在実装済みで、audit-event ベースではなく WAL ベースです。** クラッシュリカバリは WAL(`.reyn/state/wal.jsonl`)と seq 付きスナップショットから agent 状態を再構築し、ユーザー起点の rewind(`/rewind`)も同じ方法で過去のチェックポイントから履歴を分岐します。仕組みは [Time-travel](../runtime/time-travel.ja.md) を参照してください — ここに以前あった「まだ構築されていない」という記述はこれに置き換わりました。

**冪等性はワークフロー作者の責任。** ワークフローが Control IR 経由でファイルを書き込む場合、リトライで同じステップが再実行されると再度書き込まれます。決定論的な前処理は役立ちますが、外部から見える副作用を持つワークフローは冪等性について自ら考える必要があります。

## 関連情報

- [リファレンス: events](../../reference/runtime/events.md) — 完全なイベント分類
- [リファレンス: reyn.yaml](../../reference/config/reyn-yaml.md) — `limits` ブロック

- [tool-contract-design.md](tool-contract-design.md) — 検証される内容
