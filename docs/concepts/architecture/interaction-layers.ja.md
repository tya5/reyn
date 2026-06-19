---
type: concept
topic: architecture
audience: [human, agent]
---

# Agent インタラクション層

agent を動かすものは構造的に 3 種類あります: **外部システムが呼び込む**、**Reyn
自身がターンを起こす**、**実行中のターンに割り込む**。これらを 3 層として名付ける
ことで、agent の制御プレーンを明示的かつ governable に保てます — これが要点です。
autonomy-first なフレームワーク（OpenClaw / Hermes 系の self-hosted agent）は「世界が
どう agent に到達するか」を自由な配線と LLM の裁量に大きく委ねますが、Reyn は trigger
境界を first-class で監査可能なサーフェスにします。predictability-over-autonomy の
姿勢と一貫しています。

3 層はいずれも最終的に同じ primitive — **agent の inbox** に置かれる message
（`send_to_agent_impl` パス）— に合流します。違いは *誰が起点で*、*いつ* かだけです。

```
┌─────────────────────────────────────────────────────────────┐
│  1. 外部接続層   (外 → Reyn; Reyn は server)                 │
│       MCP server · A2A server · gateway                       │
├─────────────────────────────────────────────────────────────┤
│  2. 内部トリガー層 (Reyn → 新しい agent ターン)              │
│       cron · inject_message (提案)                            │
├─────────────────────────────────────────────────────────────┤
│  3. ターン内介入層 (実行中ターンに割り込む)                  │
│       lifecycle hooks (提案)                                  │
└─────────────────────────────────────────────────────────────┘
                          ↓ 全層が合流
                   agent inbox / send_to_agent_impl
```

> **ステータス注記。** 層 1 と cron（層 2）は **実装済**。`inject_message`（層 2）と
> hook 層全体（層 3）は **提案段階** — 設計段階でありコードには未存在。本ページは各々を
> その旨マークします。提案中の機構を現状の挙動と読み違えないでください。

## 1. 外部接続層（外が Reyn を呼ぶ）

外部 caller が agent session に message を届け、Reyn は passive server として振る舞い
ます。3 つの接続種別、いずれも **実装済**:

| 接続 | caller | 返答の配送 |
|---|---|---|
| **MCP server**（SSE / stdio）— `src/reyn/mcp/server.py` | AI クライアント（Claude Code, Cursor） | 同期 — caller がブロックして待つ |
| **A2A server**（HTTP JSON-RPC）— `src/reyn/interfaces/web/routers/a2a.py` | peer AI エージェント（LangGraph, CrewAI 等） | 同期、または caller 指定の `webhook_url` へ非同期 |
| **gateway**（Slack / LINE 等）— `src/reyn/plugins/` | 人間（チャットプラットフォーム経由） | 非同期 — Reyn が platform API を呼んで配送 |

**outbound の非対称性。** MCP と A2A は **outbound 返答を caller に委ねられます**:
返答は同期で返るか、caller が提供した callback URL へ POST されるため、Reyn 側に
platform 固有の outbound コードは不要です。**gateway は違い、Reyn が outbound を担い
ます**: socket で待っている caller がいないため、Reyn が能動的に platform へ返答を
push する必要があります。

現状のコードでは gateway は **inbound のみ**配送します（`sample_line` /
`sample_slack` の webhook が `push_to_agent` を呼ぶ）。outbound 返答は gateway 自身で
なく別の MCP tool（例: Slack MCP server）経由が前提です。gateway が inbound と
outbound の両方を担う形 — 自前の outbound MCP tool を登録し、self-contained な
gateway が送受信を完結させる — はこの層の **提案中**の完成形です。（この双方向
ブリッジの役割を表すため `plugins/` パッケージを `gateway/` に **改名する提案**が
あります。現状のパッケージは `plugins/` です。）

関連: [A2A](../multi-agent/a2a.md)、[MCP](../tools-integrations/mcp.md)。

## 2. 内部トリガー層（Reyn がターンを起こす）

ここでは外部 caller を介さず、Reyn 自身が新しい agent ターンを開始します。

- **cron** — `src/reyn/runtime/cron/`（**実装済**）。スケジュールされた `CronJob` が
  対象 agent の inbox に message を dispatch し、スケジュールされた trigger からの
  attributed な agent ターンを生みます。
- **`inject_message`**（**提案**）— agent の inbox に message を置いてターンを起こす
  プログラム的な呼び出し。

両者は **構造的に同じ操作** — *agent の inbox に message を置いてターンを開始する* —
であり、dispatch の契機（スケジュール か 明示呼び出し か）だけが異なります。この
等価性こそが両者を 1 層にまとめる理由です。

## 3. ターン内介入層（実行中のターンに割り込む）— 提案

最初の 2 層がターンを *開始する* のに対し、この層は **既に実行中のターンに割り込み・
拡張** します。これは全体が **提案段階** です — Reyn には現状 hook 機構がありません
（router loop や session に lifecycle callback 点がない）。

提案されている形は lifecycle **hooks** の集合で、`src/reyn/core/dispatch/dispatcher.py`
から dispatch されます:

- `pre_tool_call` — tool 実行直前（block / 書き換え可能）。
- `post_tool_call` / `transform_tool_result` — tool 実行直後。
- `pre_llm_call` — LLM 呼び出し直前（例: context injection）。
- `transform_llm_output`、および session-lifecycle events。

これは調査した competitor の hook システムに最も直接対応する層です。Reyn では OS の
他部分と同じ permission / event 規律の下に置かれることになります。

## 実装済 vs 提案（まとめ）

| 層 | 実装済 | 提案 |
|---|---|---|
| 1 — 外部接続 | MCP, A2A, gateway（inbound） | gateway outbound 完成; `plugins/`→`gateway/` 改名 |
| 2 — 内部トリガー | cron | `inject_message` |
| 3 — ターン内介入 | — | lifecycle hooks（層全体） |

## なぜ 3 層か

価値は、agent に到達する新しい手段すべてに対する単一かつ網羅的な問い —
*これは 外→Reyn か、Reyn→新ターン か、ターン内 か?* — にあります。あらゆる
インタラクションがちょうど 1 つの層に属し、同じ inbox primitive に合流するため、
制御プレーンは監査可能なまま保たれ、新機構は後付けでなく OS の permission / event
保証を継承します。これらの経路が場当たり的な autonomy-first フレームワークに対する、
governance-first 側の対応物です。

## 関連

- [LLM invocation surfaces](llm-invocation-surfaces.md) — router vs phase（別の軸: LLM が *どう呼ばれるか* であって agent に *どう到達するか* ではない）
- [マルチエージェント](../multi-agent/multi-agent.md) · [A2A](../multi-agent/a2a.md) · [MCP](../tools-integrations/mcp.md)
- [Principles](principles.md) — P4（制約された decision engine）、本モデルの背後にある governance 姿勢
