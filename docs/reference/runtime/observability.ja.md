---
type: reference
topic: runtime
audience: [human, agent]
---

# Observability — OpenTelemetry (OTLP) エクスポート

Reyn は P6 監査イベントストリームを OpenTelemetry コレクターへ OTLP の span /
metric / log record としてエクスポートできます。このエクスポートは**追加的で
オプトイン、かつ fail-open な downstream** です — OTLP エンドポイントが設定されて
いない限り無効であり、セッションや durable store には一切影響しません。

## 位置づけ — そして何でないか

- **これは** P6 [監査イベント](../../concepts/runtime/events.md)ログの subscriber で
  あり、各イベントを OTLP テレメトリにマッピングして off-loop で送出します。
- **これは recovery source ではありません**。`.reyn/events` + WAL が durable な
  recovery/replay の Source-of-Truth のまま変わりません。エクスポーターはどちらにも
  書き込みません。
- **これは channel/client ではありません**。エクスポート専用で、Reyn は OTLP を
  受信しません。

## オプトイン — デフォルト無効

エクスポーターは、以下のいずれかで OTLP エンドポイントが設定された場合にのみ
セッションへ attach されます:

- `reyn.yaml` / `reyn.local.yaml` の `observability.otel.endpoint`、または
- 標準の `OTEL_EXPORTER_OTLP_ENDPOINT` 環境変数。

エンドポイント未設定ならエクスポーターは構築すらされず、オーバーヘッドはゼロで、
OTEL 無しのビルドとバイト単位で同一の挙動になります。

```yaml
observability:
  otel:
    endpoint: "http://localhost:4318"
    headers:
      Authorization: "Bearer ${OTEL_TOKEN}"
    service_name: "reyn"
    capture_content: false
```

OTEL SDK が必要です:

```
pip install reyn[observability]
```

SDK 未インストールでエンドポイントを設定した場合は一度だけ警告を出し、未 attach
（fail-open）のままです。各フィールドは
[`observability` 設定ブロック](../config/reyn-yaml.md#observability-ブロック)を
参照してください。

## イベント → テレメトリ マッピング

マッピングは OpenTelemetry の **GenAI semantic conventions** に従います。
convention は単一バージョンに pin され、すべての `gen_ai.*` 属性キーは名前付き
定数なので、送出される属性サーフェスは一箇所で監査できます。

| P6 監査イベント | OTLP 出力 | 主な属性 |
|----------------|-------------|----------------|
| `session_started` / `session_completed` | ルート span `session <agent>` | `gen_ai.agent.name`, `gen_ai.agent.id`, `gen_ai.conversation.id` |
| `turn_started` / `turn_completed` / `turn_cancelled` / `turn_settled` | turn span（session の子） | `gen_ai.operation.name` (`invoke_agent`)、`turn_cancelled` は error status |
| `llm_called` + `llm_response_received` | 子 span `chat <model>` | `gen_ai.operation.name` (`chat`), `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `reyn.usage.cost_usd` |
| `llm_response_received` | metric histogram | `gen_ai.client.token.usage`（input/output）, `gen_ai.client.cost.usd` |
| `tool_executed` / `mcp_called` / `mcp_failed` / `mcp_cancelled` | 子 span `execute_tool <name>` | `gen_ai.operation.name` (`execute_tool`), `gen_ai.tool.name` |
| `web_fetch_started` / `web_search_started`（+ completed/failed） | 子 span `execute_tool` | `gen_ai.tool.name`、failed 系は error status |
| `permission_granted` / `permission_denied` / `user_intervention_*` / safety イベント | log record | `reyn.event.type`, `run_id`, `agent_id`, `actor`, `phase`, `intervention_id` |

span は run ごとに 1 つの trace へ相関します: 子は開いている turn span の下、turn は
session ルートの下にネストし、`run_id`（無ければ `agent_id`）でキーイングされます。
順不同で届くイベントやペア欠落があってもエクスポーターはクラッシュせず、gap は
スキップされます。プロセス終了時にまだ開いている span は close + flush されるため、
orphan span はリークしません。

### pin された GenAI convention バージョン

GenAI semantic conventions はまだ Development-stability であり、属性名がリリース間で
変わり得ます。Reyn は convention バージョンを単一のモジュール定数に pin します
（`GENAI_CONVENTION_VERSION = 1.37.0`）。エクスポーターはその pin されたバージョンで
定義された `gen_ai.*` キーのみを送出します。GenAI conventions が扱わない cost は
`gen_ai.*` を捏造せず `reyn.*` 名前空間（`reyn.usage.cost_usd`）で出します。

## content はデフォルト OFF（プライバシー）

P6 イベントは ref とカウントであり、生の content ではありません。エクスポーターは
content capture を明示的に有効化しない限り、生の prompt/response body を span/log に
昇格させません:

```yaml
observability:
  otel:
    capture_content: true   # オプトイン; 信頼できるコレクター限定
```

`capture_content: false`（デフォルト）では、テレメトリは token/cost カウント・
モデル名・イベント識別子のみを持ち、メッセージ body は持ちません。

## 保証

- **Fail-open。** OTLP エンドポイントが到達不能・例外・遅延でも run は壊れません。
  すべてのエクスポートパスは例外を握りつぶします（1 警告に latch）。これは
  durability worker の fail-stop 契約の逆です。run は正常に完走し、`.reyn/events` +
  WAL は OTEL 無しと全く同じに書かれます。
- **Off-loop。** span はバッチ化されバックグラウンドスレッドで送出され、metric は
  周期的に送出されます。イベントループが OTLP のネットワークパスでブロックすることは
  ありません。
- **Recovery-independence。** OTEL は recovery source になりません。OTEL を停止・
  削除・エンドポイント喪失させても、`.reyn/events` + WAL からの recovery と replay は
  OTEL attach 時とバイト単位で同一です。OTEL の不在は recover される内容を変えません。

## 関連

- [コンセプト: Events](../../concepts/runtime/events.md) — このサーフェスが subscribe
  する P6 監査イベントの Source-of-Truth。
- [`.reyn/` ディレクトリレイアウト](reyn-dir-layout.md) — recovery-core と audit の
  区別; OTEL は recovery-core state を追加しません。
- [設定: `observability` ブロック](../config/reyn-yaml.md#observability-ブロック)。
