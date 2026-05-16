---
type: how-to
topic: multi-agent
audience: [human]
applies_to: [reyn chat, agent_request, agent_response]
---

# multi-hop delegation をトレース・デバッグする

**目的:** 1 つの agent のリクエストがピアに fan-out されて統合された返信として戻ってくるとき、何が起きているかを理解する。デバッグ、キャパシティプランニング、チェーンセマンティクスに依存する Skill の作成に有用。

## 使うべき状況

- ユーザーが最終的な返信を受け取ったが、どの agent が貢献したかを知りたい。
- チェーンがハングしており、デリゲートが応答していないと思われる。
- `safety.loop.max_agent_hops` を調整していて、実際のチェーンを見たい。
- `messages_to_agents` を出力する Skill を構築していて、外部から遅延返信メカニズムを検証したい。

## ユーザー視点で何が見えるか

ユーザーが開始したチェーンでは、発信元 agent の最初のルーターパスが即座に中間返信を送信します:

```
> DuckDB v1 の破壊的変更を調査し、200 語の変更ログサマリーを作成してください。
[lead] (researcher と writer で調査中)
```

すべてのデリゲートが応答した後、発信元 agent のルーターが返信を履歴に入れて再実行し、最終的な統合テキストを生成します:

```
[lead] DuckDB v1.0 (2024-06) には 4 つの破壊的変更が導入されました...
       (200 語のサマリーが続く)
```

中間メッセージはストリーミングや部分的な出力の成果物ではありません。独立した完全な LLM ターンです。遅延返信メカニズムは *agent が開始した* チェーンにのみ適用されます。ユーザーが開始したチェーンでは中間+最終の UX を維持し、「作業中です」をすぐに確認できます。

## このウォークスルーのセットアップ

```bash
reyn agent new lead       --role "team lead. Triages and synthesizes."
reyn agent new researcher --role "deep technical research, primary sources only."
reyn agent new archivist  --role "verifies historical context (release notes, blog posts)."
```

Topology なし — 自動管理の `_default` が 3 者をカバーするので、全員が自由に通信できます。`lead` にアタッチします:

```bash
reyn chat lead
```

## 1 つのチェーンをエンドツーエンドでトレースする

すべてのトップレベルのユーザー送信には新しい `chain_id`（uuid4 hex）が付与され、その後のすべての agent 間メッセージにスレッドされます。1 つのチェーンの `chain_id` を見つけます:

```bash
# ターンが終わった後:
tail -1 .reyn/agents/lead/events.jsonl | jq -r '.data.chain_id'
# → 71d6c8b8e7e04a0d8b6f1e3c8d92a4ab
```

次に、このチェーンに触れたすべてのイベントをすべての agent にわたって見つけます:

```bash
CHAIN=71d6c8b8e7e04a0d8b6f1e3c8d92a4ab
for agent in lead researcher archivist; do
    echo "=== $agent ==="
    grep "$CHAIN" .reyn/agents/$agent/events.jsonl
done
```

次のようなものが表示されます:

```
=== lead ===
{"type":"user_message_received","data":{"chain_id":"71d6...","text":"DuckDB v1 を調査..."}}
{"type":"agent_message_sent","data":{"kind":"agent_request","from_agent":"lead","to_agent":"researcher","depth":1,"chain_id":"71d6..."}}
{"type":"agent_response_received","data":{"from_agent":"researcher","depth":1,"chain_id":"71d6..."}}

=== researcher ===
{"type":"agent_request_received","data":{"from_agent":"lead","depth":1,"chain_id":"71d6..."}}
{"type":"agent_message_sent","data":{"kind":"agent_request","from_agent":"researcher","to_agent":"archivist","depth":2,"chain_id":"71d6..."}}
{"type":"agent_response_received","data":{"from_agent":"archivist","depth":2,"chain_id":"71d6..."}}
{"type":"agent_message_sent","data":{"kind":"agent_response","from_agent":"researcher","to_agent":"lead","depth":1,"chain_id":"71d6..."}}

=== archivist ===
{"type":"agent_request_received","data":{"from_agent":"researcher","depth":2,"chain_id":"71d6..."}}
{"type":"agent_message_sent","data":{"kind":"agent_response","from_agent":"archivist","to_agent":"researcher","depth":2,"chain_id":"71d6..."}}
```

上から下に読むと: `user → lead → researcher → archivist → researcher → lead → user`。depth は各ホップがユーザー送信からどれだけ離れているかを示します。

## 遅延返信がイベントで見えること

`researcher` は `archivist` からの `agent_response_received` が届くまで `lead` への `agent_message_sent (response)` を出力しないことに注目してください。これが遅延返信メカニズムです: `researcher` のルーターが `messages_to_agents`（ここでは `archivist` へ）を出力すると、レジストリは `chain_id` をキーとする `_PendingChain` を保持し、`waiting_on` のすべてのエントリーが解決されるまで `lead` への返信を待ちます。

fan-out の場合（researcher が 1 ターンで複数のピアに委任）、researcher のルーターが再び実行されて統合するまでに、すべてのデリゲートが応答する必要があります。遅い 1 つのデリゲートは `safety.timeout.chain_seconds`（デフォルト 60 秒）まで統合全体を遅延させます。時間を超えると `chain_timeout` イベントが発生し、上流の agent は統合されたエラーレスポンスを受け取るのでチェーンがハングしません。

## `/attach` でライブ監視する

`lead` がユーザーターンを処理している間、REPL ポインターをデリゲートに切り替えてその進捗を監視できます:

```
> DuckDB v1 の破壊的変更を調査し、200 語の変更ログサマリーを作成してください。
[lead] (researcher と writer で調査中)

/attach researcher
attached: researcher

[researcher] (archivist で確認中)
[researcher] DuckDB v1 では...
```

`lead` の `session.run()` はバックグラウンドで受信トレイを消費し続けるので、（`/attach lead` で）切り戻したときには、統合された最終返信がすでに届いています。

## `max_hop_depth` の拒否

重複する Topology が `safety.loop.max_agent_hops` が許可するよりも深いツリーを形成する場合、ランタイムは過度に深い送信を拒否します:

```
[error] agent message depth 4 exceeds limit 3; chain refused
```

そして監査イベントを発行します:

```json
{"type":"agent_message_refused","data":{"reason":"max_hop_depth","to_agent":"deep_specialist","depth":4,"chain_id":"71d6..."}}
```

発信元チェーンの上流 agent での保留状態は `safety.timeout.chain_seconds`（デフォルト 60 秒）を待ち、その後強制的に統合されたエラーレスポンスで解決されます。[events リファレンス](../../reference/runtime/events.md) の `chain_timeout` イベントを参照してください。上流 agent は自動的にブロック解除されます。プロセスの再起動は不要です。

## 履歴メタの確認

各 agent の `history.jsonl` は、送受信したメッセージを `meta.source` で識別しながら（どちら側にいたか）、`chain_id` と共に記録します:

```bash
grep "71d6c8b8" .reyn/agents/researcher/history.jsonl | jq '{role, source: .meta.source, depth: .meta.depth, text: .text[:60]}'
```

```
{"role":"user","source":"agent_request","depth":1,"text":"破壊的変更を調べてください..."}
{"role":"agent","source":"agent_request_outgoing","depth":2,"text":"v0.x のリリースノートを確認して..."}
{"role":"user","source":"agent_response","depth":2,"text":"v0.9 には破壊的変更はなかった..."}
{"role":"agent","source":"agent_response_outgoing","depth":1,"text":"DuckDB v1 では..."}
```

4 つのエントリー、4 つの `meta.source` 値: 受信リクエスト、送信委任、受信レスポンス、送信返信。この agent 側の完全なチェーンはファイルだけで再構築できます。

## アンチパターン: Skill 入力で chain_id に依存する

`chain_id` は**監査専用**です。ルーター LLM はそれを見ません。それを参照する Phase プロンプトを書かないでください。デバッグの breadcrumb として厳密に扱ってください。Skill コードでの cross-skill の相関が必要な場合は `run_id`（OS がすでに `meta` に組み込んでいる）を使用してください。

## 関連情報

- [コンセプト: multi-agent](../../concepts/multi-agent.md) — チェーンセマンティクス、遅延返信、fan-out
- [リファレンス: events](../../reference/runtime/events.md) — `chain_id` を持つ `agent_message_*` イベントのペイロード
- [リファレンス: multi-agent config](../../reference/config/multi-agent.md) — `max_hop_depth`
- [ハウツー: events によるデバッグ](debug-with-events.md)
- [ハウツー: agent チームを構築する](build-an-agent-team.md)
