---
type: concept
topic: architecture
audience: [human, agent]
---

# Reliability Engineering

agent を障害から回復させること: 優雅に停止する bounded loop、タイムアウト + リトライ、そして run 途中でのプロセス死を生き延びるクラッシュリカバリ。目標は「何か問題が起きても、システムが定義された状態にとどまり — 何を達成したかを報告する」ことです。

## Reyn の実装方法

### クラッシュリカバリ — audit-event ベースではなく WAL ベース

クラッシュリカバリは WAL(`.reyn/state/wal.jsonl`)と seq 付きスナップショットから agent 状態を再構築します — これは P6 audit-event ログ(実行ごとのトレースであり、recovery のソースではない)とは別の基盤です。ユーザー起点の rewind(`/rewind`)も同じ WAL ベースの機構を使って過去のチェックポイントから履歴を分岐します。完全な仕組みは [Time-travel](../runtime/time-travel.ja.md) を参照してください。

### 優雅な force-close を伴う bounded loop

7 つの loop / timeout / budget チェックポイントは 1 つの関数 `handle_limit_exceeded` を共有しています — 呼び出し元はどの limit が発火したかを決めるだけで、チェックポイント自体が mode dispatch、operator-bus とのやり取り、extension の記帳、audit-event の発行を担います。3 つの on-limit mode(`safety.on_limit.mode`)がすべてのチェックポイントに一様に適用されます: `interactive`(オペレーターに尋ねる)、`auto_extend`(有限回数だけ延長してから abort)、`unattended`(即座に abort、決して尋ねない)。

重要なのは、これらのパスはどれも黙って hard-stop しないことです: limit が拒否されると、LLM は達成したことを要約する最後の 1 ターン(ツール無し)を与えられ、プロセスが消えるのではなく agent メッセージとして届きます。すべての deny パスは `limit_denied` という P6 audit-event も発行します — オペレーターは常にどの limit が発火し、なぜかを確認できます。

### LLM 呼び出しタイムアウトと一時的エラーのリトライ

各 LLM HTTP 呼び出しは LiteLLM を通じて渡される呼び出しごとのタイムアウト(`limits.llm.timeout`、デフォルト `60` 秒)と、一時的な障害(`429`、`5xx`、ネットワークリセット)に対する LiteLLM の組み込み指数バックオフリトライ(`limits.llm.max_retries`、デフォルト `3`)を持ちます。

### LLM router resilience(`llm.router.*`)

プロバイダーレベルの resilience のための opt-in な `litellm.Router` スロットイン。デフォルトは OFF(`llm.router.use: false`)— スイッチが off の場合、call path は直接の `litellm.acompletion` であり、この機能が存在する前の挙動とバイト同一です。`use: true` の場合、Router が infra-exception のリトライ、`Retry-After` ヘッダーの処理、per-deployment cooldown、cross-model fallback chain を担います。Reyn はこれらを再実装しません。

**Retry-After を考慮したリトライ。** `llm.router.num_retries`(デフォルト `3`)が infra リトライ(`429`、`5xx`、ネットワークリセット)を上限まで行います。単純な指数バックオフとは異なり、Router はプロバイダーの `Retry-After` レスポンスヘッダーをネイティブに尊重するため、リトライタイミングは固定のバックオフスケジュールではなく rate-limit のウィンドウを尊重します。

**Cross-model fallback chain。** `llm.router.fallbacks` は各 primary deployment を fallback モデルの順序付きリストにマッピングします。primary が失敗すると(リトライが尽きた後)、Router は各 fallback を順に試します。`llm.router.cooldown_time` + `allowed_fails` は繰り返し失敗した deployment を cool down させ、復旧するまで以降の呼び出しから bypass します。

**Fallback 時のコスト精度。** 実際に応答したモデルは `response.model` から記録されるため、コスト帰属は元々リクエストされたモデルではなく、実際にサービスした deployment を反映します。

**Replay 互換性。** Router は LLMReplay が monkeypatch する同じ `litellm.acompletion` chokepoint を経由するため、実現された fallback も replay 機構をそのまま行使します。

完全なフィールドリファレンスは [Config: llm block](../../reference/config/reyn-yaml.md#llm-block) を参照してください。

## まだ薄い部分

**冪等性は呼び出し元の責任です。** agent が Control IR op 経由でファイルを書き込む場合、リトライで同じ op が再実行されると再度書き込まれます — OS は op をあなたの代わりに冪等にはしません。外部から見える副作用を持つ呼び出し元は冪等性について自ら考える必要があります。

## 関連情報

- [`docs/concepts/architecture/charter.md`](../architecture/charter.md) — 7 つの feature family すべてで grounded された Reliability 行
- [Time-travel](../runtime/time-travel.ja.md) — WAL ベースのクラッシュリカバリと rewind 機構の全体
- [リファレンス: reyn.yaml](../../reference/config/reyn-yaml.md) — `safety` ブロック · [llm.router.*](../../reference/config/reyn-yaml.md#llm-block)
- [tool-contract-design.md](tool-contract-design.md) — op が実行される前に何が検証されるか
