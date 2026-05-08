---
title: Agent Framework Ecosystem — 市場動向
last_updated: 2026-05-08
status: stable
---

# Agent Framework Ecosystem — 市場動向

## 現状スナップショット（2026-05）

### 主要 framework の方向性

| カテゴリ | 代表 | 動向 |
|---|---|---|
| Orchestration | LangGraph, LlamaIndex Workflows | ステートマシン / グラフ表現が主流。code-first |
| Multi-agent conversation | AutoGen, CrewAI | LLM 同士の role-based 対話。autonomy 優先 |
| No-code / Low-code | Dify, n8n | ビジュアル editor。enterprise non-engineer 向け |
| Protocol | MCP (Anthropic), A2A (Google) | tool / agent 間の interop 標準化が進行中 |
| Big Tech SDK | OpenAI Agents SDK, Google ADK, Microsoft Agent Framework | production-grade の本番 SDK を 2025 年に相次いでリリース |
| Constrained / auditable | Reyn | 高制約・監査要件特化（pre-OSS） |

---

## エコシステム規模数値（2026-05-08 時点）

競合分析から取得した実測値。

### GitHub Stars

| Framework | Stars | 備考 |
|---|---|---|
| Dify | 139,000+ | 最大規模。2025年末 100K 突破。週次リリース継続 |
| LangChain | 136,000+ | 業界最大 OSS エコシステム。Contributors 2,000+ |
| n8n | 150,000+ | AI 統合を強化した workflow automation。No-code 層のトップ |
| AutoGen | 54,500+ | Microsoft Research バック。コミュニティが AG2 にフォーク分裂 |
| CrewAI | 47,800+ | "fastest-growing 2025"。Fortune 500 60%+ 採用（自社クレーム） |
| Agno（旧 Phidata） | 39,000+ | 高性能 multi-agent runtime。LangGraph 比 5,000x 高速を主張 |
| LangGraph | 31,400+ | LangChain Inc. の orchestration エンジン。採用企業約 400 社 |
| smolagents | 26,000+ | HuggingFace 製ミニマリスト library |
| Mastra | 22,000+ | TypeScript agent framework。YC W25、月間 180 万 DL |
| OpenAI Agents SDK | 19,000+ | Swarm の後継。月間 1,030 万 DL |
| PydanticAI | 16,800+ | type-safe Python agent。Pydantic チーム製 |
| Google ADK | 15,600+ | A2A プロトコルネイティブ。2,800+ 採用プロジェクト |
| Letta（旧 MemGPT） | 14,000〜20,000 | memory-centric stateful agent |

### PyPI ダウンロード（月次）

| Package | 月次 DL |
|---|---|
| LangChain | 237,401,999（約 2.37 億） |
| LangGraph | 約 34,500,000（3,450 万） |
| CrewAI | 5,000,000+（500 万） |
| OpenAI Agents SDK | 10,300,000（1,030 万） |

### 資金調達・組織規模

| Framework | 状況 |
|---|---|
| CrewAI | Series A $18M（Insight Partners, 2024-10）。月次エージェント実行 4.5 億回+ |
| Mastra | $13M seed（YC W25、2025-10）|
| Dify | LangGenius K.K.（日本法人）設立 2025-02。CTC パートナーシップで 3 年 30 億円目標 |
| Microsoft Agent Framework | AutoGen + Semantic Kernel 統合。2026-04 GA。Azure AI Foundry バック |

---

## Key トレンド（競合調査で実証されたもの）

### 1. Big Tech の本番 SDK 参入（2025）

OpenAI（3 月）・Google ADK（4 月）・Microsoft Agent Framework（10 月）が相次いで production-grade SDK をリリース。OSS コミュニティが先行していた領域にプラットフォームベンダーが参入し、競合環境が質的に変化した。

- OpenAI Agents SDK: Guardrails・Handoffs・Tracing の 3 プリミティブで production-ready
- Google ADK: A2A プロトコルネイティブ対応。階層エージェントツリー
- Microsoft Agent Framework: Entra ID 認証 + RBAC + Azure Monitor + SOC2/HIPAA。Python と .NET を同等サポート

**Reyn への含意**: Azure/Entra 統合を持つ Microsoft の enterprise 訴求は日本市場で強い。Reyn は「クラウドロックインなし・OS agnosticism（P7）」を対抗軸にする。

---

### 2. MCP / A2A 標準プロトコルによる融合圧力

MCP（Model Context Protocol、Anthropic 主導）と A2A（Agent-to-Agent、Google 主導）が 2025〜2026 年を通じて急速に普及している。

**MCP 採用状況**:
- AutoGen: Extensions で MCP サーバ統合
- CrewAI: MCP Server/Client をツールとして統合
- Dify: HTTP ベース MCP サービス統合
- OpenAI Agents SDK: MCP ネイティブ

**A2A 採用状況**:
- Google ADK: A2A プロトコルネイティブ
- CrewAI: `a2a-sdk` によるクロス-Crew デリゲーション
- Microsoft Agent Framework: A2A エンドポイント対応

**Reyn への含意**: Reyn の `mcp_search` stdlib skill は対応済み。MCP Client（`run_mcp_tool` op）と A2A（`run_remote_agent` op）の実装が Phase 2 ロードマップにある。非対応のままでは「閉じた OS」と評価されるリスクあり。

---

### 3. Structured Output の標準化

JSON Schema 強制が各 LLM provider で標準化されつつある。LangChain は Model Profiles（v1.1）でモデルの structured output 対応可否を自動判定。Dify は LLM Node に JSON Schema Editor を追加（v1.3）。CrewAI は Pydantic `response_format` を採用。

**Reyn への含意**: Reyn の validation-first 設計（`{control, artifact, control_ir}` の全実行前バリデーション）は業界トレンドと完全に整合する。ただし Weak LLM（gemini-2.5-flash-lite）の empty-stop attractor 問題は現在進行形の課題。envelope-layer fix の方向性が業界では最善手と確認されている。

---

### 4. Observability（可観測性）の標準化

LLM トレーシングが企業採用の前提条件になりつつある。

| ソリューション | 採用状況 |
|---|---|
| LangSmith | LangGraph・LangChain のデファクト可観測性基盤。RBAC/SSO（Enterprise）|
| Langfuse | Dify・AutoGen との連携。OSS 自己ホスト可能 |
| OpenTelemetry | AutoGen ネイティブ統合。CrewAI OpenLIT 連携。OpenAI SDK OTel 対応 |
| AgentOps | AutoGen 連携 |

Kakaku.com（Dify + Langfuse）の全社展開事例が示すように、**監視インフラなしに本番採用は承認されない**というのが現在の日本大企業の判断基準になっている。

**Reyn への含意**: Reyn の `events/` append-only log は設計的優位性があるが UI・外部接続なしでは「見えない優位性」に留まる。OTel エクスポーターが P1 優先度で必要な理由。

---

### 5. Governance・Audit が enterprise 選定基準に台頭

2026 年が「compliance year」と表現される記事が増加。日本の AI 推進法（2025 年 5 月）など legislative push も相まって、監査ログ・説明可能性・クラッシュリカバリーが実装要件として浮上してきた。

各フレームワークの対応状況:
- **CrewAI AMP Enterprise**: Immutable Audit Logs・HIPAA/SOC2/FedRAMP High（ただし OSS は外部依存）
- **Microsoft Agent Framework**: Entra ID + Azure Monitor + SOC2/HIPAA（Azure AI Foundry 経由）
- **Dify Enterprise**: SSO/RBAC/MFA・監査ログ分析・Kakaku.com 実績
- **LangGraph/LangSmith Enterprise**: RBAC/SSO・400 日トレース保持・self-hosted in VPC

**Reyn への含意**: Reyn の P6（append-only event log）と WAL クラッシュ回復は market gap に正確に対応している。ただし「設計上の優位性」を「証明可能なエビデンス」に変える可観測性 UI と RBAC が未実装。「P6 event log + hash chain = immutable 監査証跡」という差別化ストーリーを docs と OTel 連携で具体化する必要がある。

---

### 6. No-code 層の爆発的成長と code-first 層の分化

Dify（139K stars）・n8n（150K stars）が LangChain・CrewAI を超える規模になっており、「可視化・操作性」訴求が圧倒的に広い裾野を持つことが実証されている。Non-engineer の業務担当者が AI ワークフローを構築する需要が急拡大している。

一方で、code-first フレームワークは「予測可能性・制御性・extensibility」で差別化せざるを得ない状況。No-code では提供できない「監査証跡・クラッシュリカバリー・OS レベル遷移制御」が code-first の存在価値になる。

**Reyn への含意**: Dify をプロトタイピング層、Reyn を本番ガバナンス層として補完関係を明示するポジショニングが有効。「Dify で作って Reyn で本番化する」という 2 段階シナリオを前提にした移行ガイドが上位ファネルになる。

---

### 7. Multi-agent orchestration の主流化

Gartner によれば Q1 2024〜Q2 2025 で multi-agent 問い合わせが 1,445% 増。単一エージェントから「エージェントチームの orchestration」へのシフトは全フレームワークで共通。

- LangGraph: `langgraph-supervisor` ライブラリでマルチエージェント supervisor パターンを標準化
- CrewAI: Hierarchical Process で manager_llm が自律的にエージェントに委任
- AutoGen: `SelectorGroupChat` / `Swarm` / `MagenticOneGroupChat`
- Google ADK: 階層エージェントツリー（root → sub-agents）がネイティブ設計

**Reyn への含意**: Reyn のマルチエージェントは実装済みの 4 層構造を持つ。(1) `@sub_skill` graph node — スキルのグラフに別スキルをノードとして静的に埋め込む、(2) `run_skill` Control IR op — フェーズ実行中に動的に別スキルを呼び出す（isolated/shared workspace 選択・parent_run_id で系譜追跡）、(3) `delegate_to_agent` / `send_to_agent` — 名前付きエージェント間メッセージング（`max_hop_depth=3` でループ防止、`chain_timeout_seconds` で応答タイムアウト制御）、(4) MCP server (`reyn mcp serve`) — 外部 LLM クライアントから Reyn エージェントを呼べる。競合に対する差別化は「マルチエージェント呼び出しの全経路でも P4/P5/P6 制約が維持される」点。ドキュメントとサンプルが未整備なため、この強みが外部から見えていないことが課題。

---

## 新興プレイヤー所見（2026-05）

emerging-players.md より重要点を抜粋。

| プレイヤー | 特記事項 | Reyn への示唆 |
|---|---|---|
| PydanticAI（★16,800+） | type-safe Python agent。型バリデーションという共通価値観。OS 層なし | Reyn の input/output schema 設計に参照価値。競合より補完的 |
| Agno（★39,000+） | "5,000x faster" パフォーマンス訴求。旧 Phidata | 速度競争に入らない。Reyn は「確実性（N 回完走率）」ベンチマークで応じる |
| Microsoft Agent Framework | AutoGen + Semantic Kernel 統合。Entra ID + Azure Monitor + SOC2。2026-04 GA | Reyn の最大の enterprise 脅威。「クラウドロックインなし + P7 OS agnosticism」で対抗 |
| OpenAI Agents SDK（★19,000+） | OpenAI ブランド。Guardrails・Tracing。月間 DL 1,030 万 | OTel 標準との interop 設計は Reyn のイベントログ外部接続に参照価値 |
| Google ADK（★15,600+） | A2A プロトコルネイティブ。急成長中 | A2A 対応を Phase 2 後のロードマップに明示すべき |
| smolagents（★26,000+） | HuggingFace 製ミニマリスト。1,000 行以下実装 | Code Execution Agent パターンの普及は Reyn `eval` skill 設計に参照価値 |

---

## 2026 年 forecast（根拠ベース）

1. **MCP が tool 接続の事実上の標準になる**: 2026 年末には MCP 非対応フレームワークは「レガシー」と分類されるリスクがある。Reyn の Phase 2 MCP 対応はタイムリーであり、遅延は競争上の致命傷になり得る。

2. **Multi-agent + audit trail が enterprise 採用条件に**: Microsoft・Google・CrewAI が enterprise audit を製品化している中、2026 年以降の大企業採用では「監査証跡の提示方法」が選定条件になる。Reyn の P6 設計は競合の SaaS 依存監査よりも内部ガバナンス要件に適合するが、それを証明する可観測性 UI が必要。

3. **日本 AI 推進法（2025-05）の運用明確化**: 2026 年中に AI システムへの説明責任・監査要件が具体化される可能性がある。「なぜ LLM がこの判断をしたか」を追跡できる Reyn の設計は、規制対応フレームワークとしての訴求ポイントになる。Reyn は日本の規制動向を market signal として活用すべき。

4. **No-code / code-first の二極化が固定**: Dify・n8n 等の No-code 層と LangGraph・Reyn 等の code-first 層の間に「設計者が異なる」という分断が確立する。Reyn は code-first の中で「governance 優先」という niche を占め、Dify との補完関係をポジティブに訴求する方が効果的。

5. **Weak LLM 問題の部分的解消**: 2026 年中に frontier LLM の structured output 能力が向上し、Reyn が依存する gemini-2.5-flash-lite の attractor 問題は減少する可能性がある。ただし「OS レベルでバリデーションする」Reyn の設計は LLM の品質に依存しない防衛ラインとして価値が残る。

---

## Tool discovery の業界標準化

→ 詳細は `docs/journal/insights/2026-05-07-industry-tool-discovery-survey.md`

- Anthropic / OpenAI ともに **defer_loading + meta-tool** パターンに収束
- Reyn の `intent-axis + per-category list_*` はこの (a) 派の簡略版として整合

---

## 参照

- [competitive/README.md](../competitive/README.md) — 横比較テーブル詳細
- [landscape/emerging-players.md](emerging-players.md) — 新興プレイヤー詳細スキャン
- [landscape/reyn-strategic-priorities.md](reyn-strategic-priorities.md) — 戦略的優先事項
- [positioning/reyn-differentiators.md](../positioning/reyn-differentiators.md) — 差別化根拠詳細
- [industry tool discovery survey](../../journal/insights/2026-05-07-industry-tool-discovery-survey.md)
