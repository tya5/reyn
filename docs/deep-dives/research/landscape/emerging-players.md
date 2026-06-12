---
title: 新興 Agent Framework スキャン
last_updated: 2026-05-10
status: stable
scan_sources:
  - Hacker News (2025-2026)
  - GitHub trending (Python/AI)
  - arxiv cs.AI (2025-2026)
  - Decision Crafters / AgentMarketCap / Firecrawl / ByteByteGo 各種調査記事
---

# 新興 Agent Framework スキャン（2026-05）

## TL;DR

2025〜2026 の agent framework 市場は「乱立から収束」フェーズに入りつつある。  
GitHub スター数トップ層（Dify 130k+、n8n 150k+、LangChain 97k+）は可視化・low-code 層が席巻し、  
コード first フレームワークは Agno (39k)・CrewAI (45k) など高スター競合が存在する。  
Big Tech（OpenAI / Google / Microsoft）が本番向け SDK を相次いで投入し、  
標準プロトコル（MCP・A2A）によるエコシステム融合が加速している。  
Reyn の「高制約エンタープライズ向け予測可能性」ポジションに直接競合するプレイヤーはまだ少ないが、  
Microsoft Agent Framework の enterprise push と LangGraph の governance 訴求は注視が必要。

---

## 注目プレイヤー

### PydanticAI
- **概要**: Pydantic チーム製の type-safe Python agent framework。OpenAI / Anthropic / Google SDK が採用する Pydantic のバリデーション層をそのままエージェント設計に適用。2024 年末 early-access → 2025 年 9 月 V1 stable。
- **問題解決**: 型安全性のない LLM 出力を production grade で扱うことの困難さ。structured outputs・dependency injection・テスト容易性を提供。
- **GitHub**: ★ 16,800+ [`pydantic/pydantic-ai`](https://github.com/pydantic/pydantic-ai)
- **Reyn との関係**: **部分的競合**。型バリデーションという共通価値観を持つが、PydanticAI は OS 層（状態管理・ワークフロー遷移・イベント監査）を持たない。Reyn の制御 IR + schema validation レイヤーと思想的に近い。
- **Reyn への示唆**: Pydantic の型システム活用は Reyn の input/output schema 設計で参照価値あり。PydanticAI の「dependency injection for testing」パターンは Reyn のテスト戦略 (LLMReplay Fake) と相補。

---

### Agno（旧 Phidata）
- **概要**: 2025 年 1 月に Phidata から改名。"Run agents as production software" を標榜する高性能 multi-agent runtime。LangGraph 比 5,000x 高速な agent instantiation・50x 低メモリを主張。
- **問題解決**: 大規模 multi-agent システムのパフォーマンスと production deployability。チーム・サブエージェント・ストレージを統合した All-in-One 設計。
- **GitHub**: ★ 39,000+ [`agno-agi/agno`](https://github.com/agno-agi/agno)
- **Reyn との関係**: **競合**（ただしセグメントが異なる）。Agno は速度・規模訴求、Reyn は予測可能性・監査可能性訴求。日本企業向けガバナンス要件には Agno はノーポジション。
- **Reyn への示唆**: Agno のパフォーマンスベンチマーク公表戦略は Reyn の差別化訴求に対するカウンター。Reyn 側は「速さではなく確実性」の価値をより明示すべき。

---

### smolagents（HuggingFace）
- **概要**: HuggingFace 製の「コードで考えるエージェント」ミニマリスト library。1,000 行以下の実装で CodeAgent（Python snippet）と ToolCallingAgent を提供。2025 年公開。
- **問題解決**: agent framework の複雑さ・依存関係を排除し、研究者・OSS コミュニティが任意 LLM でエージェントを構築する際の最小フットプリント。
- **GitHub**: ★ 26,000+ [`huggingface/smolagents`](https://github.com/huggingface/smolagents)
- **Reyn との関係**: **異セグメント**。smolagents は研究・プロトタイピング向けの minimalist ライブラリ。ガバナンス・監査・クラッシュリカバリーは設計外。HuggingFace エコシステム（Hub の tool 共有等）は Reyn にない強み。
- **Reyn への示唆**: コード実行エージェント（CodeAgent パターン）の普及は Reyn の `eval` stdlib skill 設計の参考になる。sandbox 実行統合（E2B・Modal・Docker）の成熟も注目。

---

### Letta（旧 MemGPT）
- **概要**: MemGPT 論文発の memory-centric stateful agent platform。OS パラダイム（core memory / recall memory / archival memory の階層）を採用。Letta Code は Terminal-Bench #1 のコーディングエージェント。
- **問題解決**: LLM のコンテキストウィンドウ制約を超えた長期記憶と学習継続。セッションをまたいで成長するエージェントの構築。
- **GitHub**: ★ 推定 14,000〜20,000 [`letta-ai/letta`](https://github.com/letta-ai/letta)（MemGPT 時代から継続成長中）
- **Reyn との関係**: **補完的**。Letta は記憶管理の深さが強み、Reyn は制御フローとガバナンスの厳格さが強み。組み合わせ可能な設計になっていく余地がある。ただし Letta の「LLM がメモリを自己管理」という哲学は Reyn の P5（workspace SSoT）と緊張関係。
- **Reyn への示唆**: 長期実行・長期記憶系のユースケースは Reyn の強化領域。RAG framework は実装済み（`recall` / `index_docs`、`recall_docs` skill は ADR-0033 で op に collapse 済）。次の論点は memory layer の深化。

---

### Mastra
- **概要**: Gatsby チーム出身者が創業した TypeScript agent framework。YC W25、2025 年 10 月 $13M seed 調達。2026 年 1 月 v1.0 リリース。月間 DL 数 180 万（2026 年 2 月）。
- **問題解決**: フロントエンド・Node.js エンジニアが AI エージェントを TypeScript のファーストクラス機能として実装できる開発者体験。評価・可観測性・RAG を統合。
- **GitHub**: ★ 22,000+ [`mastra-ai/mastra`](https://github.com/mastra-ai/mastra)
- **Reyn との関係**: **異セグメント**（言語・対象が異なる）。TypeScript 専用のため Python エコシステム前提の Reyn とは直接競合しない。ただし「TypeScript ネイティブ + 可観測性統合」のアプローチは Reyn の将来的な Web/UI layer 設計に参照価値。
- **Reyn への示唆**: Mastra の急成長は「フロントエンド開発者がエージェントを構築したい」需要を示す。Reyn Web（現在実装中）のターゲット層と一部重複する可能性。

---

### OpenAI Agents SDK（旧 Swarm）
- **概要**: OpenAI が 2025 年 3 月リリースした production-ready multi-agent SDK。Swarm の実験的実装を本番グレードに昇格。Handoffs・Guardrails・Tracing の 3 プリミティブ。2026 年に Sandbox Agents・Voice Agents 対応を追加。
- **問題解決**: OpenAI API を使った multi-agent ワークフローの最小抽象。provider-agnostic 設計（100+ LLM 対応）。
- **GitHub**: ★ 19,000+ [`openai/openai-agents-python`](https://github.com/openai/openai-agents-python)（月間 DL 1,030 万）
- **Reyn との関係**: **競合**（ベンダー影響力の差が大）。OpenAI ブランドの信頼性と普及速度は脅威。ただし Guardrails・Tracing は Reyn の P6（イベントログ）・validation layer と機能的に重複。Reyn は「OS として LLM を制御する」深度で差別化。
- **Reyn への示唆**: OpenAI の Tracing 標準化（OpenTelemetry 連携）は Reyn のイベントログ設計において互換性を持つ機会。将来的な観測 infra の interop を検討する価値あり。

---

### Google ADK（Agent Development Kit）
- **概要**: Google が 2025 年 4 月リリースした open-source Python/TypeScript/Go の agent toolkit。階層エージェントツリー（root → sub-agents）と A2A プロトコルネイティブ対応が特徴。Gemini 統合だが model-agnostic。
- **問題解決**: 企業が複数フレームワーク間でエージェントを連携させる A2A 標準化と、Google Cloud での本番デプロイメント。
- **GitHub**: ★ 15,600 [`google/adk-python`](https://github.com/google/adk-python)（急成長中、2,800+ 採用プロジェクト）
- **Reyn との関係**: **競合（Big Tech 圧力）**。Google の A2A プロトコル推進は業界標準化圧力。Reyn の Phase/Skill グラフ設計が将来 A2A と互換する設計になっているか要確認。
- **Reyn への示唆**: A2A（Agent-to-Agent）プロトコルは Reyn の `@sub_skill` / `run_skill` Control IR ops の外部連携標準として採用を検討すべき重要動向。

---

### Microsoft Agent Framework（AutoGen + Semantic Kernel 統合）
- **概要**: 2025 年 10 月 preview → 本番リリース。AutoGen（50,000+ stars）と Semantic Kernel を統合した enterprise-grade Python/.NET SDK。AutoGen は maintenance mode へ移行し本 SDK に集約。
- **問題解決**: 研究実験フレームワーク (AutoGen) と enterprise 基盤 (Semantic Kernel) の乖離を解消。Azure Monitor・Entra ID・OpenTelemetry・CI/CD を統合した enterprise 向け multi-agent orchestration。
- **GitHub**: ★ AutoGen 50,400+ [`microsoft/autogen`](https://github.com/microsoft/autogen) + [`microsoft/agent-framework`](https://github.com/microsoft/agent-framework)
- **Reyn との関係**: **最大の競合脅威（enterprise 領域）**。Azure + Entra ID 認証 + OpenTelemetry + CI/CD という enterprise 統合パッケージは、Reyn のターゲットである「ガバナンス・監査・コンプライアンス重視の日本企業」と直接競合する。ただし Reyn の P1–P8 制約設計（OS 層の LLM 非依存性）は Microsoft Framework が持たない差別化。
- **Reyn への示唆**: Azure との統合を前提とした日本企業への訴求が強化される可能性。Reyn は「LLM 制約の透明性・説明可能性・OS agnosticism（特定クラウドに依存しない）」を強みとして打ち出すべき。

---

### LangGraph / CrewAI（確立プレイヤー・参照用）
- **概要**: LangGraph（LangChain の graph-based workflow エンジン）は月間 DL 3,450 万・enterprise 採用でトップ。CrewAI は「fastest-growing framework 2025」として ★45,900。どちらも 2024〜25 年に業界標準的地位を確立。
- **GitHub**: LangChain ★97,000+、CrewAI ★45,900+
- **Reyn との関係**: **競合（業界標準ポジション）**。LangGraph の graph-based 遷移設計は Reyn の Phase/Skill グラフと概念的に近い。ただし LangGraph は P7 原則（OS skill-agnostic）を持たず、skill-specific ロジックが framework コアに混入しやすい設計。
- **Reyn への示唆**: LangGraph が enterprise で「audit trail + rollback」を訴求し始めている動向は Reyn の P6（イベントログ）との差別化ポイントとして整理が必要。

---

### Dify / n8n（No-Code / Low-Code 層）
- **概要**: Dify (★130,000+) は LLM アプリの production platform（RAG・エージェント・ワークフロー統合）。n8n (★150,000+) は AI 統合を強化した workflow automation。どちらも 2026 年時点で最大 GitHub スター数クラス。
- **Reyn との関係**: **異セグメント（ただし購買層が重複する可能性）**。技術者ではなく非エンジニアのビジネスユーザーが主対象。Reyn のターゲット（エンタープライズ向け auditable workflow を構築するエンジニア）とは異なる。ただし「エンジニアなしで動く」訴求は Reyn の upper funnel に影響し得る。
- **Reyn への示唆**: No-code 層の成熟が「コード first framework の需要を絞る」シナリオに注意。Reyn の価値は No-code では提供できない制御可能性・監査性・クラッシュリカバリーである点を訴求すること。

---

### Docker MCP Catalog & Toolkit（MCP エコシステム）
- **概要**: Docker Desktop 4.42（2026-06、`hub.docker.com/mcp`）に統合された MCP サーバー実行基盤。`registry.modelcontextprotocol.io`（Anthropic 管理）とは独立した別レジストリ（100+ サーバー）。各 MCP サーバーはコンテナとして隔離実行（CPU 1コア・RAM 2GB 上限、ホスト FS アクセスなし）。
- **CLI**: `docker mcp catalog ls/show`、`docker mcp gateway run`（複数サーバーのプロキシ多重化）、`docker mcp tools call`、`docker mcp secret`、`docker mcp oauth`
- **セキュリティ**: Docker がビルド・署名・SBOM 付きで配布。OAuth はコンテナ外で処理（認証情報がコンテナに渡らない）。FP-0016 Component C（Device Grant）の Docker 側実装に相当。
- **Reyn との関係**:
  - `mcp_install.py` の `registryType: "docker"` は `docker run --rm -i <image>` で個別コンテナを起動する形で対応済み
  - Docker MCP カタログ（`hub.docker.com/mcp`）の検索・`docker mcp gateway` 連携は未対応
  - Docker 常駐デーモンが必要なため Reyn の設計方針（常駐プロセス不要）と相容れず、当面は対応しない
  - FP-0017（sandboxed execution）の将来バックエンドとして `DockerBackend` の枠は確保済み
- **注目理由**: MCP エコシステムの HTTP 型サーバー移行と合わせて、コンテナ隔離が MCP サーバー配布の標準になる可能性がある。Reyn の `SandboxPolicy`/`SandboxBackend` 抽象がこれを収容できる設計になっていることを確認済み。

---

## 業界トレンドの発見

1. **Big Tech の本番 SDK 投入（2025）**: OpenAI (3 月)・Google ADK (4 月)・Microsoft Agent Framework (10 月) が相次いで production-grade SDK をリリース。OSS コミュニティが先行していた領域に主要ベンダーが参入し、競合環境が急変した。

2. **標準プロトコル (MCP・A2A) による融合圧力**: MCP (Model Context Protocol) は 2025 年を通じて広く採用され、tool 接続の標準になりつつある。A2A (Agent-to-Agent) は Google が主導し、フレームワーク間のエージェント通信を標準化。Reyn の Sub-skill / run_skill 設計はこれらへの対応が今後必要になる。

3. **Multi-agent orchestration が主流化**: Gartner によれば Q1 2024〜Q2 2025 で multi-agent 問い合わせが 1,445% 増。単一エージェントから「エージェントチームの orchestration」へのシフトは全フレームワークで共通。Reyn の `@sub_skill` グラフはこのニーズに沿う設計。

4. **Governance・Audit が enterprise 選定基準に台頭**: 2026 年を「compliance year」と表現する記事が増加。日本の AI Promotion Act (2025 年 5 月) など legislative push も相まって、監査ログ・説明可能性・クラッシュリカバリーが実装要件として浮上し始めた。

5. **No-code 層の爆発的成長と code-first 層の分化**: Dify/n8n の GitHub スターが LangChain/CrewAI を超える規模になっており、「可視化・操作性」訴求が強い。code-first 層は「予測可能性・制御性・extensibility」で差別化せざるを得ない。

---

## Reyn への優先示唆

1. **A2A / MCP 対応を roadmap に明示**: Google ADK・OpenAI SDK・Microsoft Agent Framework が全て対応する A2A / MCP が事実上の相互運用標準になる。Reyn の `run_skill` / Control IR ops が外部エージェント（A2A エンドポイント）を呼べる経路を設計しておくことで「閉じた OS」から「接続可能な OS」へのアップグレードパスが生まれる。

2. **Microsoft Agent Framework の enterprise push に対抗する「説明可能性」差別化**: Azure / Entra ID 統合を持つ Microsoft の enterprise 訴求は日本市場でも強い。Reyn の強みである P1–P8 制約設計（OS が LLM を制御する構造の透明性）・append-only イベントログ・WAL リカバリーを「監査エビデンス」として日本企業に提示できる仕様書・デモを用意すべき。

3. **memory layer の深化を再評価**: Letta の成長（長期記憶・学習継続）はユーザーニーズを示す。RAG framework (`recall` / `index_docs`) は landed 済、次は memory layer の `recall(sources=["memory"])` 移行（ADR-0033 Phase 1.5）。

4. **LangGraph との差別化軸を言語化**: LangGraph も「graph-based 遷移 + audit trail」を訴求し始めている。Reyn との違い（OS が skill-agnostic, P7 厳守, Phase が next を知らない P1 設計, LLM 出力の制約 P4）を外向きドキュメントとして整備する。

5. **パフォーマンスではなく「確実性」ベンチマーク**: Agno が「5,000x faster」ベンチマークを公表している中、Reyn は速度競争に入るべきでない。代わりに「N 回連続完走率」「クラッシュリカバリー成功率」「LLM 制御違反率」などの確実性指標を公表する準備を進める。

---

## References

- [PydanticAI — GitHub](https://github.com/pydantic/pydantic-ai)
- [PydanticAI V1 — AgentMarketCap](https://agentmarketcap.ai/blog/2026/04/06/pydanticai-python-agent-framework-langgraph-crewai-comparison)
- [smolagents — GitHub](https://github.com/huggingface/smolagents)
- [smolagents 26k stars — Decision Crafters](https://www.decisioncrafters.com/smolagents-build-powerful-ai-agents-in-1-000-lines-of-code-with-26-3k-github-stars/)
- [Letta — GitHub](https://github.com/letta-ai/letta)
- [Letta V1 agent loop — letta.com](https://www.letta.com/blog/letta-v1-agent)
- [Agno — GitHub](https://github.com/agno-agi/agno)
- [Agno production-ready — Decision Crafters](https://www.decisioncrafters.com/agno-production-ready-ai-agents-scale/)
- [OpenAI Agents SDK — GitHub](https://github.com/openai/openai-agents-python)
- [OpenAI Agents SDK next evolution](https://openai.com/index/the-next-evolution-of-the-agents-sdk/)
- [Mastra — GitHub](https://github.com/mastra-ai/mastra)
- [Mastra Complete Guide 2026](https://www.generative.inc/mastra-ai-the-complete-guide-to-the-typescript-agent-framework-2026)
- [Google ADK — GitHub](https://github.com/google/adk-python)
- [Google ADK Review 8k stars](https://andrew.ooo/posts/google-adk-agent-development-kit-review/)
- [Microsoft Agent Framework — Visual Studio Magazine](https://visualstudiomagazine.com/articles/2025/10/01/semantic-kernel-autogen--open-source-microsoft-agent-framework.aspx)
- [Microsoft AutoGen — GitHub](https://github.com/microsoft/autogen)
- [LangGraph vs CrewAI 2026 — DEV Community](https://dev.to/pooyagolchian/ai-agents-in-2026-langgraph-vs-crewai-vs-smolagents-with-real-benchmarks-on-local-llms-4ma1)
- [Dify — GitHub](https://github.com/langgenius/dify)
- [n8n vs Dify 2026 comparison](https://hostadvice.com/blog/ai/automation/n8n-vs-dify/)
- [Best Open Source Agent Frameworks 2026 — Firecrawl](https://www.firecrawl.dev/blog/best-open-source-agent-frameworks)
- [Agentic AI Trends 2026 — MachineLearningMastery](https://machinelearningmastery.com/7-agentic-ai-trends-to-watch-in-2026/)
- [LLM Orchestration 2026 — orq.ai](https://orq.ai/blog/llm-orchestration)
- [Japan AI Promotion Act — IBA](https://www.ibanet.org/japan-emerging-framework-ai-legislation-guidelines)
- [Julep — GitHub](https://github.com/julep-ai/julep)
- [Top AI GitHub Repositories 2026 — ByteByteGo](https://blog.bytebytego.com/p/top-ai-github-repositories-in-2026)
