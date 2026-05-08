---
title: CrewAI — 競合分析
last_updated: 2026-05-08
status: stable
sources:
  - url: https://docs.crewai.com/
    accessed: 2026-05-08
  - url: https://docs.crewai.com/concepts/agents
    accessed: 2026-05-08
  - url: https://docs.crewai.com/concepts/crews
    accessed: 2026-05-08
  - url: https://docs.crewai.com/concepts/flows
    accessed: 2026-05-08
  - url: https://docs.crewai.com/concepts/tasks
    accessed: 2026-05-08
  - url: https://docs.crewai.com/concepts/memory
    accessed: 2026-05-08
  - url: https://docs.crewai.com/concepts/tools
    accessed: 2026-05-08
  - url: https://docs.crewai.com/en/concepts/llms
    accessed: 2026-05-08
  - url: https://docs.crewai.com/en/concepts/knowledge
    accessed: 2026-05-08
  - url: https://docs.crewai.com/en/concepts/planning
    accessed: 2026-05-08
  - url: https://docs.crewai.com/en/concepts/flows
    accessed: 2026-05-08
  - url: https://docs.crewai.com/en/changelog
    accessed: 2026-05-08
  - url: https://docs.crewai.com/en/enterprise/introduction
    accessed: 2026-05-08
  - url: https://docs.crewai.com/en/telemetry
    accessed: 2026-05-08
  - url: https://docs.crewai.com/en/observability/openlit
    accessed: 2026-05-08
  - url: https://docs.crewai.com/en/learn/a2a-agent-delegation
    accessed: 2026-05-08
  - url: https://crewai.com/pricing
    accessed: 2026-05-08
  - url: https://blog.crewai.com/crewai-oss-1-0-we-are-going-ga/
    accessed: 2026-05-08
  - url: https://github.com/crewAIInc/crewAI
    accessed: 2026-05-08
  - url: https://www.getpanto.ai/blog/crewai-platform-statistics
    accessed: 2026-05-08
  - url: https://www.diagrid.io/blog/checkpoints-are-not-durable-execution-why-langgraph-crewai-google-adk-and-others-fall-short-for-production-agent-workflows
    accessed: 2026-05-08
---

# CrewAI — 競合分析

## TL;DR

CrewAI は「role-based な自律エージェントチームを Crew / Flow / Agent / Task の 4 概念で組み上げるマルチエージェント OSS フレームワーク」である。LLM を意思決定エンジンとして解放・信頼する設計思想で、制御フローは明示的に宣言するのではなく LLM に任せる。Reyn との根本的差異は **「LLM の自律度の高さ vs. OS による候補制約 (P4)」** と **「ワークスペース経由の SSoT (P5) vs. インメモリ / コールバック伝達」** の 2 点にある。

---

## 1. コアアーキテクチャ

### 基本構成要素

CrewAI のアーキテクチャは以下 4 層で構成される。

| 概念 | 説明 |
|---|---|
| **Agent** | role / goal / backstory を持つ自律ユニット。LLM が認知エンジン。ツール・メモリ・デリゲーションを持つ |
| **Task** | エージェントへの作業指示。expected_output / guardrails / callbacks / 出力スキーマを持つ |
| **Crew** | Agent + Task のグループ。sequential / hierarchical の 2 プロセスモードを持つ |
| **Flow** | Crew を含む上位オーケストレーション層。イベント駆動 (`@start` / `@listen` / `@router`)、ステート永続化 (`@persist`) |

### LLM の役割

CrewAI における LLM は **ほぼ無制限の自律エージェント** として機能する。

- **Hierarchical Process** では manager_llm が自らタスクを割り振り・バリデーション・再割当を判断する
- **A2A 委任**では「どのリモートエージェントに委任するか」を LLM が自律選択する
- **AgentPlanner** では実行前に LLM がクルー全体の計画を生成し、各タスク説明に挿入する
- `max_iterations` デフォルト 20 回までリトライするが、中断条件は LLM の判断

**Reyn との差異**: Reyn の P4 では OS が次遷移候補を明示提示し LLM はそこからのみ選択する。CrewAI には候補制限機構が存在しない。

### role-based multiagent の思想

CrewAI は「実在組織の構造を模倣する」設計哲学を持つ。各エージェントに role / goal / backstory を与えることで、組織内の専門家チームと同等の協調動作を実現しようとする。これは Reyn の Phase / Skill 分離 (宣言的ワークフロー) とは対照的な「擬人化による役割設計」アプローチである。

---

## 2. ワークフロー単位

### Reyn の Phase/Skill に相当するもの

| Reyn 概念 | CrewAI 対応 | 差異 |
|---|---|---|
| Phase | Task | Reyn Phase は input_schema + instructions のみ宣言。CrewAI Task は expected_output / guardrails / callbacks 等を内包 |
| Skill | Crew + Flow | Reyn Skill はフェーズグラフを宣言。CrewAI は Crew (逐次・階層) + Flow (イベント駆動) の 2 モードが混在 |
| OS (runtime) | CrewAI Engine (Crew/Flow 実行ロジック) | Reyn OS は P7 でスキル固有文字列ゼロ。CrewAI engine はエージェントロール名や process type 等を内部的に参照 |
| Control IR | — | CrewAI に相当する概念なし。エージェントはツール呼び出し / デリゲーション等を LLM が直接決定 |

### Task 設計の特徴

- **output_pydantic**: Pydantic モデルで出力スキーマを強制。バリデーション失敗時はリトライ
- **guardrails**: 関数ベース (`(bool, Any)` 返却) と LLM ベース (自然言語条件) の 2 種。直列実行で `guardrail_max_retries` 上限
- **async_execution**: コンテキスト依存のない Task を並列実行可能
- **human-in-the-loop**: `@human_feedback` デコレータで Flow 一時停止・人間入力待機

---

## 3. 信頼性・回復力

### Weak LLM 対応

CrewAI には **LLM 出力を構造化する仕組み** (Pydantic + response_format) はあるが、Reyn のような **OS が候補を絞り込んで LLM の逸脱を物理的に防ぐ P4 機構はない**。

実際の問題として Community に報告されている事例:
- structured output が一貫して機能しない (response_format パラメータの LiteLLM との不整合)
- Weak LLM (GPT-4o-mini 以下相当) では tool input 解釈が不安定
- エージェントがループに入り `max_iterations` を消費する attractor 問題

### タスク失敗時の挙動

- **Guardrail**: 最大 `guardrail_max_retries` 回まで自動リトライ
- **Flow `@persist`**: SQLite バックエンドでステートをスナップショット保存
  - Resume モード: 同一 UUID でフロー継続
  - Fork モード: 別 UUID で新フロー開始
- **task replay** (`crew.replay(task_id=...)`): 直近 1 kickoff のみ対象

### 根本的クラッシュ回復の限界

外部調査 (Diagrid) が指摘する構造的限界:

1. `@persist` は **自動 resume しない** — クラッシュ検知 + リカバリトリガーは開発者責任
2. **単一プロセス前提** — プロセスクラッシュで実行中ワークフローは全消滅
3. `task replay` は **直近 1 run のみ保持** — 過去の失敗フローの追跡が困難
4. 複数プロセスによる並行リカバリに **排他制御なし** — 二重実行リスク
5. ReAct ループ内部の実行カーソルは永続化されない

**Reyn との差異**: Reyn は WAL + forward-replay による自動クラッシュ回復 (ADR-0023 + PR21) を OS レベルで提供。CrewAI は「チェックポイント・プリミティブ」を提供するが「耐久実行 (durable execution)」ではない。

### Human Feedback 統合

- Flow: `@human_feedback` デコレータで一時停止
- Task: `human_input=True` でエージェント実行前に人間入力を挿入
- Crew: `planning=True` で AgentPlanner がクルー全体を事前計画 (計画はただの追加コンテキスト、実行は LLM 任せ)

---

## 4. Stdlib・標準装備

### 標準 Tools (30+ 種)

| カテゴリ | 代表ツール |
|---|---|
| Web 検索 / スクレイピング | SerperDevTool, ExaSearchTool, FirecrawlCrawlWebsiteTool, ScrapeWebsiteTool |
| ドキュメント RAG | PDFSearchTool, DOCXSearchTool, CSVSearchTool, JSONSearchTool, TXTSearchTool |
| ファイル操作 | DirectoryReadTool, FileReadTool, DirectorySearchTool |
| コード実行 | CodeInterpreterTool (e2b sandbox) |
| マルチモーダル | DALL-E Tool, VisionTool |
| GitHub / YouTube | GithubSearchTool, YoutubeVideoSearchTool |
| DB | PGSearchTool |
| 汎用統合 | ComposioTool (500+ SaaS 接続), LlamaIndexTool, ApifyActorsTool |

### Knowledge / RAG

- **Knowledge Source**: テキスト / PDF / CSV / Excel / JSON / Web (CrewDoclingSource) を ChromaDB または Qdrant に格納
- **クエリ最適化**: 自動クエリ書き換えで検索精度向上
- **組み込み Embedding**: OpenAI `text-embedding-3-small` デフォルト (設定可能)

### メモリシステム

統合 `Memory` クラス (UnifiedMemory) で一元管理:
- LLM が保存時に scope / categories / importance を自動推論
- 検索時スコア: `composite = semantic * similarity + recency * decay + importance * importance`
- 非同期保存でエージェント実行をブロックしない
- scope 階層 (`/project/alpha`, `/agent/researcher`) でコンテキスト精度向上

**Reyn との差異**: Reyn は OS 組み込み Control IR ops (file/web/shell/mcp) を持つが、meta skill は 3 本のみ。CrewAI は実用 30+ ツール + RAG + Memory を標準装備しており、**ドメインスキルの充実度は CrewAI が大幅優位**。

### MCP / A2A 統合

- **MCP**: MCP Server / Client を CrewAI ツールとして統合可能
- **A2A**: `a2a-sdk` によるクロス-Crew・クロス-組織デリゲーション。LLM が委任先エージェントを自律選択。更新方式は poll / stream / push の 3 種

---

## 5. Enterprise 機能

### CrewAI AMP (Agent Management Platform)

OSS の上に構築されたエンタープライズ SaaS プラットフォーム:

| 機能 | 内容 |
|---|---|
| **Crew Studio** | ノーコード / ローコード ビジュアルエディタ |
| **GitHub 連携デプロイ** | リポジトリから直接 Crew をデプロイ |
| **実行トレース** | 実行ログ・詳細トレース・OpenTelemetry 出力 |
| **Guardrails (UI)** | ハルシネーションスコア / 人間レビュートリガー |
| **Tool Repository** | ツールのパブリッシュ・インストール管理 |
| **Webhook ストリーミング** | リアルタイムイベント配信 |
| **Usage Dashboard** | パフォーマンスメトリクス・コスト可視化 |
| **Cron スケジューリング** | ワークフロー定期実行 |

### Enterprise 固有機能

- **RBAC**: 粒度の高いロールベースアクセス制御
- **SSO**: MS Entra / Okta (SAML)
- **監査ログ (Immutable Audit Logs)**: エージェント実行ごとに不変ログ
- **コンプライアンス**: HIPAA / SOC 2 / NAT / SAM / FedRAMP High
- **デプロイ選択**: AMP Cloud / オンプレミス / 専用 VPC (AWS・Azure・GCP)
- **サポート**: 専用 Slack/Teams チャンネル + オンサイト支援 (50 時間/月)

### テレメトリとプライバシー

OSS 版は **デフォルトで匿名テレメトリを ON** で収集する。収集内容: エージェントロール名・ツール名・モデル名・実行設定など。`CREWAI_DISABLE_TELEMETRY=true` で無効化可能だが、デフォルト ON は日本企業の情報漏洩リスク感度に対して懸念点となる。過去に「EU データローカリティ違反」の GitHub Issue が提起されている。

---

## 6. Ecosystem

| 指標 | 値 (2026-05 時点) |
|---|---|
| GitHub Stars | **47,800+** |
| GitHub Forks | 6,500+ |
| PyPI 総ダウンロード | 27M+ |
| PyPI 月次ダウンロード | 5M+ |
| 月次エージェント実行数 | 450M+ (2026) / OSS 10M+ (2024) |
| 年間実行総数 | 20 億回+ |
| 最初のリリース | 2023-10 |
| 最新バージョン | v1.14.5a3 (2026-05-07) |
| コントリビューター | 250+ |
| 対応国 | 150+ カ国 |
| 認定開発者 | 100,000+ |
| Fortune 500 採用 | 60%+ (公式クレーム) |
| 主要採用企業 | IBM / Microsoft / P&G / Walmart / SAP / Adobe / PayPal |
| 資金調達 | Series A $18M (Insight Partners, 2024-10) |

リリースペース: 2025-2026 に 100+ リリース。週次ペースで機能追加・バグ修正が継続している。

---

## 7. Pricing / License

### OSS コア

- **ライセンス**: MIT License — 商用利用・自社ホスティングに制限なし
- **費用**: 無料。自社インフラで無制限実行可能

### AMP (CrewAI+) SaaS プラットフォーム

| プラン | 価格 | 実行数/月 | 主要機能 |
|---|---|---|---|
| **Basic** | 無料 | 50 回 (超過 $0.50/回) | ビジュアルエディタ・トレース・ガードレール |
| **Enterprise** | カスタム | 〜30,000 回無料 + 無制限 | RBAC・SSO・専用 VPC・FedRAMP・オンサイト支援 |

※ 旧 Starter ($29/月 / 1,000 回) や Professional ($99/月 / 5,000 回) のティア情報は一部情報源に残るが、現行ページは Basic + Enterprise の 2 ティアに整理されている。

---

## 8. Reyn 対比

| 軸 | CrewAI | Reyn | 判定 |
|---|---|---|---|
| **LLM の役割** | ほぼ無制限の自律エージェント。候補制限なし。Manager LLM が動的タスク割当・バリデーション | decision engine (P4) — OS が候補を絞り、LLM はそこからのみ選択 | Reyn 優 (予測可能性)、CrewAI 優 (柔軟性) |
| **遷移制御** | Sequential / Hierarchical Process + Flow イベント駆動 (`@listen`/`@router`)。LLM が自由に次行動を決定 | OS 候補提示 → LLM 選択 → OS バリデーション。不正遷移は OS が拒否 | Reyn 優 (ガバナンス) |
| **LLM 出力バリデーション** | Pydantic response_format で構造化。バリデーション失敗 → リトライ。Weak LLM で不安定事例あり | `{control, artifact, control_ir}` 形式の完全バリデーション。不正出力は REJECTED | Reyn 優 (強制力) |
| **データフロー** | インメモリ + コールバック + Flow ステート (SQLite)。エージェント間はタスク output を直接受け渡し | workspace 経由のみ (P5)。すべてのデータは workspace を通過 | Reyn 優 (SSoT 保証) |
| **監査** | Flow `@persist` + AMP 実行ログ + OpenTelemetry。OSS 版は外部ツール (OpenLIT/Langfuse) 依存 | event log append-only (P6)。OS がすべての状態変化を強制記録 | 同等〜Reyn 優 (OSS レベルでの強制監査) |
| **クラッシュ回復** | `@persist` チェックポイント (手動リカバリ必要)。単一プロセス前提。自動 resume なし | WAL + forward-replay による自動クラッシュ回復 | Reyn 優 |
| **Skill / 構造変更時の OS 変更** | Crew/Flow/Agent/Task は設定ファイルで定義。OS 変更不要 | OS 変更不要 (P7) | 同等 |
| **Stdlib 充実度** | 30+ 組み込みツール / Knowledge RAG / UnifiedMemory / MCP / A2A | OS 組み込み ops (file/web/shell/mcp) + meta skill 3本。RAG・DB・翻訳等のドメインスキルなし | **CrewAI 優 (大幅差)** |
| **Weak LLM 対応** | Guardrail + リトライあり。ただし候補制限なく attractor 問題が発生しやすい | P4 + LLM Output Contract で attractor を構造的に抑制 (ただし current の gemini-2.5-flash-lite で empty-stop 問題あり) | Reyn 優 (設計上)、実装上は互いに課題あり |
| **エコシステム** | 47k+ stars / 5M PyPI/月 / Fortune 500 60% / $18M 調達 / 100k 認定開発者 | pre-OSS / 小コミュニティ / 非公開 | **CrewAI 優 (圧倒的差)** |
| **Enterprise コンプライアンス** | AMP: HIPAA / SOC2 / FedRAMP High / オンプレ対応。OSS: デフォルトテレメトリ ON | 未実装 (pre-OSS) | **CrewAI 優** |
| **日本企業適合性** | デフォルトテレメトリ ON / GDPR 懸念あり。ガバナンスは AMP 有償層 | 予測可能性・ガバナンス優先設計。ただし stdlib 不足 | Reyn 優 (設計思想)、CrewAI 優 (実装成熟度) |

---

## 9. Reynが追いつくために必要なこと

CrewAI が解いていて Reyn が未着手 or 未成熟の問題を、技術コスト付きで列挙する。

### 9-A. Stdlib ツールセット拡充 【技術コスト: large】

CrewAI は 30+ の実用ツール (PDF RAG・コード実行・DB・GitHub など) を標準装備。Reyn は OS 組み込み ops (file/web_search/web_fetch/shell) を持つが、**ドメイン特化スキル（RAG・DB・PDF処理・GitHub統合）がなく実業務ワークフローを組むには自分でスキルを書く必要がある**。

必要な作業: recall_docs (RAG)・translate・code_exec のドメインスキルを優先実装。個々の skill は OS 変更不要 (P7) だが、数量が多く設計・テストコストが大きい。

### 9-B. Knowledge / RAG 統合 【技術コスト: large】

CrewAI は ChromaDB / Qdrant + 自動クエリ書き換えによるドキュメント知識統合を標準装備。Reyn にはエージェントが外部ドキュメントを参照するための組み込みメカニズムがない。

必要な作業: Control IR に `search_knowledge` op を追加。ベクトル DB アダプタ層の設計。P5 (workspace SSoT) との整合 (インデックス格納場所の設計)。

### 9-C. UnifiedMemory (クロスセッション記憶) 【技術コスト: medium】

CrewAI の UnifiedMemory は scope 階層・重要度スコア・非同期保存・重複防止を備えたクロスセッション記憶システム。Reyn は workspace が SSoT だが、実行間の記憶の継続性機構は未設計。

必要な作業: workspace に memory namespace を定義。Control IR に `save_memory` / `recall_memory` op を追加。検索スコアリング (semantic + recency + importance) の実装。

### 9-D. ノーコード / ビジュアルエディタ (AMP Studio 相当) 【技術コスト: large】

CrewAI AMP の Crew Studio は非エンジニアが Crew を組み上げられるビジュアル UI。日本企業では「IT 部門が設計、業務部門がカスタマイズ」という需要が高い。Reyn は現状 CLI + skill.md テキスト編集のみ。

必要な作業: Skill グラフのビジュアライズ + drag-and-drop 編集。Phase / Tool の GUI 設定。ADR でスコープ・優先度の合意が先に必要。

### 9-E. A2A / MCP first-class 統合 【技術コスト: medium】

CrewAI は A2A プロトコルをネイティブサポートし、クロス-Crew / クロス-組織デリゲーションが可能。MCP Server/Client もツールとして統合済み。Reyn は MCP server（外部から Reyn を呼ぶ側）は実装済み。MCP client（外部 MCP server を呼ぶ側）と A2A は Phase 2 ロードマップ。

必要な作業: Control IR に `run_remote_agent` op を追加。A2A `agent-card.json` の生成・解決機構。認証 (Bearer / OAuth2) の OS レベル抽象化。

### 9-F. 匿名テレメトリの明示的 OFF デフォルト 【技術コスト: small】

CrewAI OSS はデフォルト ON テレメトリが日本企業のセキュリティ審査で問題になりやすい。Reyn はゼロテレメトリが設計上のデフォルトであり、これは差別化ポイントとして明示すべきである。

必要な作業: CLAUDE.md / docs にゼロテレメトリ設計を明記。エンタープライズ向けドキュメント強化。

### 9-G. 実行トレース / OpenTelemetry エクスポート 【技術コスト: medium】

CrewAI AMP は OpenTelemetry ネイティブのトレース。OSS 版も OpenLIT / Langfuse / Dynatrace との統合が確立。Reyn は events/ append-only log (P6) があるが、外部監視ツールへのエクスポート機構が未整備。

必要な作業: events/ の OpenTelemetry Span へのマッピング。`OTLP_EXPORTER_ENDPOINT` による送信。Grafana / Datadog との疎通確認。

### 9-H. LLM プロバイダ多様化 (25+ 対応) 【技術コスト: small〜medium】

CrewAI は OpenAI / Anthropic / Google / Bedrock / Vertex AI / Ollama ほか 25+ LLM プロバイダを LiteLLM 経由でサポート。Reyn は現状 gemini-2.5-flash-lite (LiteLLM proxy) のみで Weak LLM 依存リスクがある。

必要な作業: OS の LLM 呼び出し層を provider-agnostic に抽象化 (LiteLLM 互換インタフェース化)。モデル選択をスキル設定から行えるようにする。

---

## References

- [CrewAI Docs — Overview](https://docs.crewai.com/)
- [CrewAI Docs — Agents](https://docs.crewai.com/concepts/agents)
- [CrewAI Docs — Crews](https://docs.crewai.com/concepts/crews)
- [CrewAI Docs — Flows](https://docs.crewai.com/concepts/flows)
- [CrewAI Docs — Tasks](https://docs.crewai.com/concepts/tasks)
- [CrewAI Docs — Memory](https://docs.crewai.com/concepts/memory)
- [CrewAI Docs — Tools](https://docs.crewai.com/concepts/tools)
- [CrewAI Docs — LLMs](https://docs.crewai.com/en/concepts/llms)
- [CrewAI Docs — Knowledge](https://docs.crewai.com/en/concepts/knowledge)
- [CrewAI Docs — Planning](https://docs.crewai.com/en/concepts/planning)
- [CrewAI Docs — A2A Agent Delegation](https://docs.crewai.com/en/learn/a2a-agent-delegation)
- [CrewAI Docs — Changelog](https://docs.crewai.com/en/changelog)
- [CrewAI Docs — Enterprise / AMP](https://docs.crewai.com/en/enterprise/introduction)
- [CrewAI Docs — Telemetry](https://docs.crewai.com/en/telemetry)
- [CrewAI Pricing](https://crewai.com/pricing)
- [CrewAI OSS 1.0 GA Blog Post](https://blog.crewai.com/crewai-oss-1-0-we-are-going-ga/)
- [GitHub — crewAIInc/crewAI](https://github.com/crewAIInc/crewAI)
- [CrewAI Platform Statistics 2026 (Panto)](https://www.getpanto.ai/blog/crewai-platform-statistics)
- [Diagrid: Checkpoints Are Not Durable Execution](https://www.diagrid.io/blog/checkpoints-are-not-durable-execution-why-langgraph-crewai-google-adk-and-others-fall-short-for-production-agent-workflows)
- [DEV Community: CrewAI vs LangGraph vs AutoGen 2026](https://dev.to/emperorakashi20/crewai-vs-langgraph-vs-autogen-which-multi-agent-framework-should-you-use-in-2026-5h2f)
