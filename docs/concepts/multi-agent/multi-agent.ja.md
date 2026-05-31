---
type: concept
topic: architecture
audience: [human, agent]
---

# マルチ agent

1 つの reyn プロセスは、任意の数の長期稼働 **agent** をホストできます。各 agent は独自のプロファイル、履歴、memory レイヤー、受信ボックス、Skill カタログビューを持つ ChatSession です。Agent は人間と（一度に 1 つ、attach 経由で）、そして互いに（構造化されたリクエスト/レスポンスチャンネルを通じて）通信します。

## Reyn の 4 layer マルチ agent

Reyn はマルチ agent 機能を 1 つ持つのではなく、異なるスコープと配線タイミングに対応した **4 つの独立した合成サーフェス** を持ちます。差別化の核心は、**4 つの layer すべてで同じ OS invariant を維持する** ことです — [P4](../architecture/principles.md#p4-llm-is-a-constrained-decision-engine)（候補セット制約）、[P6](../architecture/principles.md#p6-events-are-the-audit-truth)（すべての transition に event）、そして permission システム。多くのフレームワークはこれらのサーフェスを 1〜2 つしか持ちませんが、Reyn の差別点は 4 つすべてに uniform invariant を適用していることです。

```
┌──────────────────────────────────────────────────────────────────┐
│  Layer 4:  reyn mcp serve                                        │
│            (external MCP clients call INTO Reyn agents)          │
│              ↑ list_agents()  ↑ send_to_agent(name, msg)         │
├──────────────────────────────────────────────────────────────────┤
│  Layer 3:  delegate_to_agent                                     │
│            (agent → agent, in-process, chain_id correlated)      │
├──────────────────────────────────────────────────────────────────┤
│  Layer 2:  run_skill  Control IR op                              │
│            (phase invokes a sub-skill at runtime, LLM-chosen)    │
├──────────────────────────────────────────────────────────────────┤
│  Layer 1:  @sub_skill  graph node                                │
│            (skill graph statically embeds another skill)         │
└──────────────────────────────────────────────────────────────────┘
                All layers enforce: P4 + P6 + permissions
```

### Layer 一覧

| Layer | メカニズム | 配線タイミング | プロセス境界 | 典型的な用途 | 参照 |
|-------|-----------|--------------|-------------|-------------|------|
| 1 | `@sub_skill` graph node | compile-time | same-process | 静的合成（「phase A は常に skill X を呼ぶ」） | [graph.md](../../reference/dsl/graph.md) |
| 2 | `run_skill` Control IR op | LLM-runtime | same-process | 動的 sub-skill 選択（「phase が入力に応じて sub-skill を決定」） | [control-ir.md](../../reference/runtime/control-ir.md#run_skill) |
| 3 | `delegate_to_agent` | runtime + topology | same-process | 専門家への委譲（「research agent → writer agent」） | [../multi-agent/topology.md](../multi-agent/topology.md) |
| 4 | `reyn mcp serve` | runtime | external client | agent fleet を Claude Code、Cursor などの MCP 対応クライアントに公開する | [../tools-integrations/mcp.md](../tools-integrations/mcp.md) |

> **FP-0034 Phase 6 (2026-05-16) routing note**: Layer 3
> `delegate_to_agent` と Layer 2 `run_skill` は handler 名は不変。
> LLM-visible surface は universal `invoke_action(action_name=
> "agent.peer__<name>", args={...})` (= delegation) / `invoke_action(
> action_name="skill__<name>", args={...})` (= skill invocation)。
> `universal_dispatch.py` が同 handler に route。 permission / event /
> chain semantics は不変。

### 4 つの layer で変わらないもの

- **P4 — 候補セット制約。** どの layer でも、LLM は OS が用意した集合の中から選択します。所有している skill、topology 経由で到達可能な agent、または MCP server が公開しているツールです。どの layer も、カタログにない agent や skill を LLM が作り出すことは許しません。
- **P6 — すべての transition に event。** どの layer でも、入場・完了・失敗時に構造化 event を発行します。layer をまたいだチェーンも、各 agent の `events.jsonl` を `grep <chain_id>` することで end-to-end で再構成できます。event log が唯一の監査チャンネルです。
- **Permission ゲート。** file、MCP、shell、web の permission はどの layer 経由の呼び出しでも OS 層でチェックされます。Layer 3 の委譲呼び出しが permission ルールを迂回することはなく、Layer 2 の sub-skill は自身の permission を宣言しなければなりません。
- **Workspace 分離。** どの layer も skill スコープの workspace 境界を尊重します。Layer 1 または 2 経由で呼び出された sub-skill は、宣言した入力のみを読みます。

### どの layer を選ぶか

- 「skill X の中で常に step Y が必要」 → **Layer 1**（`@sub_skill` graph node）
- 「skill X が入力に応じて N 個の sub-skill のうち 1 つを呼ぶ」 → **Layer 2**（`run_skill` Control IR op）
- 「それぞれ独自の skill カタログを持つ複数の専門 role が互いに通信する」 → **Layer 3**（`delegate_to_agent`）
- 「外部の MCP 対応ツール（Claude Code、Cursor、OpenAI Agents SDK 等）が agent を呼べるようにしたい」 → **Layer 4**（`reyn mcp serve`）

## agent とは何か

Agent は `.reyn/agents/<name>/` にあるディレクトリと、ランタイムがオンデマンドで起動するインメモリ ChatSession です：

- `profile.yaml` — 名前、役割（システムプロンプトのペルソナ）、`allowed_skills`（オプション）
- `history.jsonl` — 追記専用の会話ログ
- `events.jsonl` — ランタイム監査ログ
- `memory/` — agent スコープの memory レイヤー（`.reyn/memory/` の共有レイヤーはすべての agent から見える）
- `runs/` — Skill スポーンごとの workspace

`default` agent は必要に応じて自動作成されます。名前付き agent は `reyn agent new` から作成します。

## AgentRegistry

プロセスごとに 1 つの `AgentRegistry` インスタンスがすべてのロード済み agent を所有します。処理内容：

- **遅延ロード** — agent は最初の attach またはエージェント間メッセージの受信時にインスタンス化されます。起動時ではありません。
- **attach ポインター** — 常に 1 つの agent だけが REPL にアタッチされています。デタッチされた agent は受信ボックスループ（バックグラウンドの Skill 進行、介入キュー）を実行し続けますが、一時的な送信ボックスメッセージは破棄されます。永続的な履歴のみが残ります。
- **送信ボックスフォワーダー** — agent ごとのタスクが、アタッチされた agent の送信ボックスを共有 REPL キューにポンプします。
- **Topology ゲート** — `permit(from, to)` が宣言されたトポロジーを参照してから agent 間の送信を許可します。[../multi-agent/topology.md](../multi-agent/topology.md) を参照してください。

## Attach モデル

`reyn chat researcher` が `researcher` をアタッチされた agent にします。アタッチ中は `/attach default` でポインターを切り替えられます。`researcher` は受信ボックスループを実行し続けます。スイッチしたときに委譲チェーンが進行中であれば、送信ボックスに解決済みの結果が戻ってきます。

## Agent 間メッセージング

ルーターの決定が `messages_to_agents: [{to, request}, ...]` を発行すると、ChatSession は各エントリーを対象の受信ボックスに `agent_request` ペイロードとしてルーティングします：

```
{from_agent, request, depth, chain_id}
```

受信 agent の `session.run()` がそれを処理し、自分自身のルーターを実行して、すぐに応答するか（送信者への `agent_response`）、さらに委譲したい場合は **遅延** します。

### 遅延応答

受信 agent のルーターが独自の `messages_to_agents` を発行する場合、上流への応答は保留されます。`chain_id` をキーとする `_PendingChain` が記録します：

- `origin_agent` — チェーンが解決したら返信する相手
- `origin_depth` — 返信を送る深さ
- `original_request` — 合成のために次のルーターターンに再生される上流リクエスト
- `waiting_on` — まだ応答待ちの agent のセット

各委譲先が応答するたびに、送信者が `waiting_on` から除かれます。セットが空になると、agent は全委譲先の応答を履歴に持った状態でルーターを再実行します。結果として得られた `reply_text` が上流に送られる単一の合成応答になります。2 回目のルーターパスがさらに委譲を発行する場合、チェーンは新しい `waiting_on` セットで保留状態を維持します。これは `max_hop_depth` によってのみ制限されます。

これにより「マネージャー → デリゲート → 合成」モデルが実現します。ユーザーはアタッチされた agent から暫定的な `（処理中）` を受け取り、その後、すべての委譲先の入力を取り込んだ単一の最終回答を受け取ります。

### chain_id

すべてのトップレベルのユーザー送信は `submit_user_text` で `chain_id`（uuid4 hex）を採番します。これは次の通り伝播します：

- 受信ボックスペイロード（すべてのホップ）
- チェーンに関わるすべての `_append_history` での履歴メタ（ソース：`agent_request`、`agent_request_outgoing`、`agent_response`、`agent_response_outgoing`）
- `agent_message_*` events

`chain_id` は **監査専用** です。ルーター LLM はそれを見ません。CLI は表示しません。複数の agent にまたがるチェーンを end-to-end でトレースするには、各 agent の `events.jsonl` と `history.jsonl` に対して `grep <chain_id>` します。

### ファンアウト

`messages_to_agents` には複数のエントリーを含めることができます。保留チェーンの `waiting_on` セットはそれらすべてを保持します。合成応答はすべての委譲先が応答した後にのみ発生します（wait-for-all）。遅い委譲先 1 つが合成全体を遅らせます。`safety.timeout.chain_seconds`（デフォルト 60 秒）が経過するまで遅延し、その時点で `chain_timeout` event が発火し、合成エラー応答が上流 agent のブロックを解除します。

## ユーザー起点 vs agent 起点のチェーン

遅延応答のメカニックは、上流に別の agent が待機しているチェーンにのみ適用されます。**ユーザー起点** のチェーンでは、起点 agent はルーターの `reply_text` をすぐにユーザーに送信し（暫定確認）、委譲先からの応答後に 2 回目のパスで最終回答を生成します。2 つの可視メッセージが生成され、1 つの合成まとまりにはなりません。

これにより既存のチャット UX（「処理中です」が見える）を維持しながら、agent 間チェーンがリクエストごとに 1 つの応答へとクリーンにまとまります。

## max_hop_depth

`safety.loop.max_agent_hops`（デフォルト 3）はチェーンがどこまで延びられるかを制限します。`depth = 0` がユーザー入力で、各 `_send_to_agent` でインクリメントされます。`depth > max_agent_hops` の送信は `agent_message_refused` event と共に拒否されます。[reference: multi-agent config](../../reference/config/multi-agent.md) を参照してください。

## OS が管理しないもの

- **Topology**：誰が誰に送信できるかは別のコンセプトです（[../multi-agent/topology.md](../multi-agent/topology.md) 参照）。レジストリの `permit()` で参照されます。
- **Skill アクセス**：LLM サイドの Skill フィルターは `profile.allowed_skills` による agent ごとの設定です。OS はプロファイルの内容に従うだけです。
- **Memory のレイヤリング**：共有と agent のレイヤーはルーターの classify phase によって読み書きされます。レジストリは memory ファイルを操作しません。

Agent はファーストクラスのアイデンティティと状態です。Topology と Skill アクセスはその上に重ねるポリシーです。

## Agent ID 伝播 (FP-0016 Component E)

エンタープライズ展開では agent ごとの帰属証明が必要です。SOC2 / ISO27001 / METI v1.1 の監査要件は、人間ユーザーレベルではなく **actor レベルで「どの agent が何をしたか」** を証明することを義務付けています。Reyn はすべての実行インスタンスに `agent.id`（`reyn.yaml` で設定、省略時は `reyn/<hostname>` がデフォルト）を割り当て、3 つのチャンネルを通じて伝播します：

1. **P6 events**：セッションから発行されるすべての event がペイロードに `agent_id` を含みます。これにより event log は agent 帰属アクションの監査トレイルとして replay 可能になります。
2. **MCP HTTP 呼び出し**：HTTP モードの MCP サーバーへの送信リクエストに `X-Reyn-Agent-Id: <agent.id>` ヘッダーを付加します。下流の MCP サーバーは呼び出し元 agent の identity に基づいて RBAC を適用できます（= Microsoft の identity model における「Entra Agent ID」パターン）。
3. **Sub-skill 呼び出し**：ネストされた `run_skill` 呼び出しは親の `agent_id` を継承します（= chat エントリーから最深の sub-skill まで、同じ identity がコールツリー全体を通じて維持されます）。

設定：

```yaml
# reyn.yaml
agent:
  id: "reyn/acme-corp/code-review-agent"
```

デフォルト動作：`agent.id` を省略した場合、Reyn は `reyn/<hostname>` を使用するため、監査トレイルが空になることはありません。

推奨フォーマット：`reyn/<org>/<role>`（= オペレーター定義。Reyn は空でない文字列であること以上の構造を強制しません）。

参照：
- [`docs/reference/config/reyn-yaml.md`](../../reference/config/reyn-yaml.md) — `agent:` ブロックのフィールドリファレンス
- [`docs/reference/runtime/events.md`](../../reference/runtime/events.md) — `agent_id` ベース event フィールド
- [`docs/concepts/runtime/secret-handling.md`](../runtime/secret-handling.md) — credential スコープ + OAuth ライフサイクル（= FP-0016 のもう一方の半分）

## 参考

- [Reference: agent CLI](../../reference/cli/agent.md)
- [Reference: profile-yaml](../../reference/dsl/profile-yaml.md)
- [Reference: multi-agent config](../../reference/config/multi-agent.md)
- [Concepts: topology](../multi-agent/topology.md)
- [Concepts: memory](../data-retrieval/memory.md)
