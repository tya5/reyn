---
title: AutoGen — 競合分析
last_updated: 2026-05-08
status: stable
sources:
  - url: https://microsoft.github.io/autogen/stable/index.html
    accessed: 2026-05-08
  - url: https://www.microsoft.com/en-us/research/blog/autogen-v0-4-reimagining-the-foundation-of-agentic-ai-for-scale-extensibility-and-robustness/
    accessed: 2026-05-08
  - url: https://devblogs.microsoft.com/autogen/autogen-reimagined-launching-autogen-0-4/
    accessed: 2026-05-08
  - url: https://learn.microsoft.com/en-us/agent-framework/overview/
    accessed: 2026-05-08
  - url: https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/termination.html
    accessed: 2026-05-08
  - url: https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/state.html
    accessed: 2026-05-08
  - url: https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/graph-flow.html
    accessed: 2026-05-08
  - url: https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/framework/telemetry.html
    accessed: 2026-05-08
---

# AutoGen — 競合分析

## TL;DR

AutoGen (Microsoft Research) は会話ベースのマルチエージェント協調を得意とする Python フレームワークで、2025年1月に v0.4「AgentChat」として全面再設計された。Reynとの根本的な違いは**LLM の役割**にある: AutoGen では LLM が自由にメッセージを生成・送信し次の発言者を決定するのに対し、Reyn では OS が候補遷移を提示し LLM はそこから選ぶのみ (P4)。また 2025年10月に Microsoft は AutoGen と Semantic Kernel を統合した **Microsoft Agent Framework** を公開し、AutoGen 本体はメンテナンスモードに移行した。

---

## 1. コアアーキテクチャ

### v0.4 設計の動機

v0.2 での主な不満は「スケーラビリティの限界・デバッグ困難・強固な監視ツールの欠如」だった。2024年初頭に設計を全面刷新し、**アクターモデル** (actor model) をベースとした非同期イベント駆動アーキテクチャに移行した。

### 3 層アーキテクチャ

| 層 | パッケージ名 | 役割 |
|---|---|---|
| **Core** | `autogen-core` | アクターモデルランタイム。非同期メッセージ交換、分散 gRPC ランタイム、Topic/Subscription モデル |
| **AgentChat** | `autogen-agentchat` | タスク駆動の高レベル API。v0.2 互換の抽象を再実装 |
| **Extensions** | `autogen-ext` | MCP サーバ、OpenAI Assistant API、Docker コード実行、Azure コードエグゼキュータ等のサードパーティ統合 |

### LLM の役割

- **AgentChat** では各エージェント (`AssistantAgent`) が LLM を呼び出してメッセージを生成。SelectorGroupChat ではモデルが次の発言者を **自由選択** する (AutoGen 内部で候補制限なし)。
- **Core 層** ではエージェントはアクターとしてメッセージに反応して任意の処理を行う。LLM 呼び出しはエージェント実装の責務。
- Reyn の P4「OS 候補提示 → LLM 選択」制約は AutoGen には存在しない。LLM の出力は直接実行される。

### 出力バリデーション

AutoGen は LLM の生出力に対して構造バリデーション (Reyn の `control` ブロック相当) を行わない。型安全性はインターフェース型ヒント (Python typing) レベルにとどまり、実行時の JSON スキーマ検証は組み込まれていない **[Inferred: ドキュメントに明示なし、型ヒントのみ言及]**。

---

## 2. ワークフロー単位

### Agent (単体)

`AssistantAgent` が基本単位。LLM クライアント、システムプロンプト、ツール (Python 関数) を設定して生成。`UserProxyAgent` はユーザ入力をエージェントに代理する特殊エージェント。

### Team (複数エージェント協調)

AgentChat が提供する 4 種類のチーム:

| チーム型 | 説明 |
|---|---|
| `RoundRobinGroupChat` | 全エージェントが共有コンテキストで輪番応答 |
| `SelectorGroupChat` | LLM が会話コンテキストから次の発言者を動的選択 |
| `Swarm` | `HandoffMessage` によるエージェント間の明示的ハンドオフ |
| `MagenticOneGroupChat` | Web・ファイルタスク向けの汎用マルチエージェント |

### GraphFlow (実験的)

`DiGraphGroupChat` による**有向グラフ**ベースのワークフロー (2025年追加、experimental)。`DiGraphBuilder` で逐次・並列・条件分岐・ループを構築可能。エッジに条件ラムダを設定して条件付きルーティングができる。ただし**グラフはエージェント実行順を制御するのみで、受け取るメッセージの内容は制御しない**。

### Reyn との対比

- Reyn の **Phase** に相当するのが AutoGen の **Agent**、Reyn の **Skill graph** に相当するのが AutoGen の **Team / GraphFlow**。
- ただし Reyn の Phase は `input_schema` だけを宣言し次遷移を知らない (P1)。AutoGen の Agent はシステムプロンプトで自由に振る舞いを定義し、ハンドオフ先も自身で決定しうる。
- AutoGen に Reyn の「OS が候補遷移を提示し LLM が選ぶ」プロトコルはない。

---

## 3. 信頼性・回復力

### 会話ループ制御

11 種類の Termination Condition が標準提供されている:
`MaxMessageTermination` / `TextMentionTermination` / `TokenUsageTermination` / `TimeoutTermination` / `HandoffTermination` / `SourceMatchTermination` / `ExternalTermination` / `StopMessageTermination` / `TextMessageTermination` / `FunctionCallTermination` / `FunctionalTermination`。

条件は `&` (AND) / `|` (OR) で組み合わせ可能。`run()` ごとにリセットされる。

### Human-in-the-Loop

- `UserProxyAgent` をチームに組み込み、実行中にユーザ入力を待機させる。
- `HandoffTermination` でエージェントがユーザへ制御を明示的に渡す。
- `ExternalTermination` で外部（UI の Stop ボタン等）からプログラム的に停止可能。
- FastAPI / ChainLit / Streamlit との統合サンプルが公式提供されている。

### クラッシュ・エラー対応

`save_state()` / `load_state()` でチーム全体の状態（会話履歴・ターン番号等）を JSON にシリアライズして永続化できる。**ただしこれはアプリケーション管理であり、AutoGen 自身が自動チェックポイントを取る仕組みはない**。

- クラッシュ後の再開は開発者が実装する必要がある。
- GraphFlow で割り込み発生時に状態が破損する既知 issue (GitHub #7043) がある。
- LangGraph の SqliteSaver のような組み込みチェックポイントは v0.4 には存在しない **[Inferred: ロードマップ issue #2358 で要望中]**。

Reyn の WAL + forward-replay によるクラッシュ自動回復 (PR21) に相当する機能は AutoGen には未実装。

---

## 4. Stdlib・標準装備

### 標準エージェント型

| エージェント | 説明 |
|---|---|
| `AssistantAgent` | LLM + ツール実行の汎用エージェント |
| `UserProxyAgent` | ユーザ入力代理 |
| `CodeExecutorAgent` | コードブロックを抽出・実行 |
| `SocietyOfMindAgent` | 内部チームの結果を要約 |

### 標準ツール・統合 (Extensions)

- MCP (Model Context Protocol) サーバ統合
- Docker / LocalCommandLineCodeExecutor によるサンドボックスコード実行
- Azure AI Foundry コード実行環境
- OpenAI Assistant API
- gRPC 分散ランタイム
- Azure Cognitive Search、GitHub 等は MCP 経由

### AutoGen Studio

Web ベースの no-code/low-code プロトタイピング UI。ドラッグ&ドロップでエージェントチームを構築・テスト・デプロイ (Docker コンテナ出力)。**本番利用不可の研究プロトタイプ**と明示されており、セキュリティ機能は未実装。

---

## 5. Enterprise 機能

### AutoGen v0.4 時点の Enterprise 機能

- **OpenTelemetry 統合**: runtime / tool / agent 呼び出しをスパンとしてトレース。任意の OTel バックエンドにエクスポート可能。
- **トークン使用量追跡**: `print_usage_summary()` / `gather_usage_summary()` でセッション全体のコスト集計 (v0.2 由来機能、v0.4 でも継続)。
- **AgentOps 連携**: LLM コール・コスト・レイテンシ・ツール実行の外部ダッシュボード管理。
- **型安全 API**: Python 型ヒントによるビルドタイム検証。

### Microsoft Agent Framework (2025年10月〜) での Enterprise 機能強化

AutoGen の後継として Microsoft Agent Framework 1.0 が 2026年4月に GA。AutoGen にはなかった以下が追加されている:

- **グラフベースワークフロー + チェックポイント**: 組み込みのステート永続化と Human-in-the-loop サポート。
- **Entra ID 認証 + RBAC**: Azure ネイティブのアクセス制御。
- **Azure Monitor / Application Insights 統合**: 本番グレードの可観測性。
- **コンプライアンス**: SOC 2、HIPAA 等のコンプライアンス保証 (Azure AI Foundry 経由)。
- **ミドルウェア・フィルタ**: エージェントアクションをインターセプトする Semantic Kernel 由来のミドルウェア層。

なお AutoGen v0.4 単体では上記の Azure/Entra 統合・コンプライアンス保証は含まれない。

---

## 6. Ecosystem

### リポジトリ規模

- GitHub Stars: **54,534** (2026年2月時点)
- Contributors: 559名以上
- Commits: 3,776+、Issues resolved: 2,488+、Releases: 98+

### コミュニティの分裂

2024年9月に AutoGen の原著者 (Chi Wang 等) が Microsoft を離れ、同年11月に **AG2** (ag2ai/ag2) をフォーク。AG2 は v0.2 互換 API を維持しつつ新機能を追加するコミュニティ主導プロジェクト。`autogen` / `pyautogen` PyPI パッケージと Discord コミュニティ (20,000名+) は AG2 側が継承。

Microsoft 側は v0.4 系列 (`autogen-core` / `autogen-agentchat`) として独自進化し、2025年10月に Semantic Kernel と統合して Microsoft Agent Framework に収束。

### 現在の状況 (2026年5月)

| プロジェクト | 状態 |
|---|---|
| AutoGen v0.4 | メンテナンスモード (バグ修正・セキュリティパッチのみ) |
| AG2 | コミュニティ主導で活発に開発継続 |
| Microsoft Agent Framework | 1.0 GA (2026年4月)。Microsoft の本命 |

### Microsoft バックの影響

- Azure AI Foundry、Azure OpenAI、Microsoft 365 Graph 等との深い統合パスが存在する。
- Microsoft Research の論文・カンファレンス発表による学術的認知度が高い。
- 大企業・研究機関での採用実績が多数 (具体的社名は非公開)。

---

## 7. Pricing / License

- **AutoGen v0.4**: MIT ライセンス。OSS、無料。
- **Microsoft Agent Framework**: MIT ライセンス。OSS、無料。
- **Azure AI Foundry 連携時のコスト**: Foundry プラットフォーム自体は無料。LLM トークン消費・コンピューティング・ストレージは Azure サービスの通常料金が発生。
- **Foundry Agent Service**: セッション時間・API コール数に応じた従量課金 (Azure ポータルで確認)。
- AutoGen Studio はセルフホスト型のため追加ライセンス費用なし。

---

## 8. Reyn 対比

| 軸 | AutoGen (v0.4) | Reyn | 判定 |
|---|---|---|---|
| **LLM の役割** | 自由なメッセージ生成・次発言者の自由選択 (SelectorGroupChat) | decision engine のみ。OS 提示候補から選択 (P4) | Reyn 優 (予測可能性・ガバナンス) |
| **遷移制御** | TerminationCondition + チーム型選択。LLM が次エージェントを自由決定しうる | OS 候補提示 → LLM 選択。候補外遷移は即 reject | Reyn 優 (制御性) |
| **LLM 出力バリデーション** | 型ヒントのみ。構造・スキーマ検証なし **[Inferred]** | `{control, artifact, control_ir}` を毎回スキーマ検証。不正出力は reject | Reyn 優 |
| **データフロー** | メモリ内オブジェクト受け渡し (会話スレッド)。ファイル永続化はオプション | workspace 経由のみ (P5)。クラッシュ回復の基盤 | Reyn 優 (クラッシュ安全性) |
| **監査** | OpenTelemetry でスパン出力。append-only ではない。replay 不可 | event log append-only (P6)。replay-capable | Reyn 優 (監査完全性) |
| **クラッシュ回復** | 組み込みチェックポイントなし。開発者実装が必要。GraphFlow に既知バグあり | WAL + forward-replay 自動回復 (PR21) | Reyn 優 |
| **skill/OS 分離** | skill-specific ロジックはエージェント実装に直接書く。フレームワーク層を汚染しうる **[Inferred]** | OS に skill 固有文字列ゼロ (P7)。新 skill は OS 変更不要 | Reyn 優 |
| **Stdlib 充実度** | AssistantAgent / CodeExecutorAgent / MCP 統合 / Docker 実行環境など豊富 | OS 組み込み ops (file/web/shell/mcp) + meta skill 3本。コード実行環境・RAG・翻訳等のドメインスキルなし | AutoGen 優 |
| **Human-in-the-loop** | UserProxyAgent + HandoffTermination + ExternalTermination。FastAPI/Streamlit 統合サンプルあり | ask_user 経由 (stdlib)。詳細は skill 実装依存 | AutoGen 優 |
| **ワークフロー表現力** | GraphFlow (DAG + 条件分岐 + 並列)。ただし experimental | Phase graph (Skill 宣言)。LLM 判断ベースの遷移 | 同等 (目的が異なる) |
| **エコシステム** | 54K+ Stars、Microsoft バック、AG2 コミュニティ。豊富なサンプル・統合 | pre-OSS、小規模。学習コスト高 | AutoGen 優 |
| **エンタープライズ統合** | Microsoft Agent Framework 経由で Azure/Entra/SOC2/HIPAA | 独立 OSS。Azure 統合なし | AutoGen 優 |
| **LLM 非依存性** | model-agnostic (OpenAI / Azure OpenAI / Anthropic / Ollama 等) | LiteLLM proxy 経由で任意モデル対応。ただし weak LLM 問題が既知 | 同等 |
| **日本語エンタープライズ向け** | 汎用グローバル設計。コンプライアンス制約への対応は利用者責任 | 日本企業の高ガバナンス要件を設計原則として優先 | Reyn 優 (設計思想) |

---

## 9. Reyn が追いつくために必要なこと

AutoGen が解いていて Reyn が未着手の問題を以下に列挙する。各項目には技術コスト (small / medium / large) を付記。

### 9-1. コード実行環境の標準提供 **[large]**

AutoGen は `CodeExecutorAgent` + Docker / LocalCommandLine により Python コードの安全な実行をフレームワークレベルで提供する。Reyn は `shell` Control IR op で任意コマンドを実行できるが、セキュアなサンドボックス環境は未整備。実行サンドボックスの設計・実装・セキュリティレビューは large。

### 9-2. no-code / low-code プロトタイピング UI **[large]**

AutoGen Studio は Web UI で非エンジニアがエージェントチームを組める。Reyn は skill authoring に Python 知識が必要でオンボーディングコストが高い。Web UI の構築は large だが、まず宣言的 JSON 設定による skill 定義のみでも medium 相当で改善可能。

### 9-3. 豊富な標準エージェント・ツール **[medium]**

RAG・translate・DB 接続等のドメイン特化スキルが AutoGen (Extensions) では豊富に揃っている。Reyn は file/web/shell を Control IR ops として持つが、ドメインスキルとしてのパッケージングが不足。individual skill は small だが網羅的に揃えるには累計 medium〜large。

### 9-4. Human-in-the-Loop の標準化 **[medium]**

AutoGen の HandoffTermination + UserProxyAgent パターンは Web フレームワーク統合サンプルも含め整備されている。Reyn の ask_user は基本的な入力取得のみ。非同期待機・複数承認ステップ・タイムアウト処理などを標準化するには medium。

### 9-5. MCP (Model Context Protocol) 統合 **[small〜medium]**

AutoGen Extensions は MCP サーバをツールとして直接接続できる。Reyn は MCP server（外部から Reyn を呼ぶ側）は実装済み。MCP client（Reyn から外部 MCP server を呼ぶ側）は未実装 (Phase 2)。権限モデル (P5 との整合) を含めると medium。

### 9-6. マルチ言語 / .NET 対応 **[large]**

Microsoft Agent Framework は Python + .NET (C#) を同等にサポートする。日本の大企業は .NET エコシステム依存が多く、エンタープライズ採用には .NET 対応が障壁になりうる。ただし Reyn の設計思想 (Phase/Skill/OS 分離) を .NET に移植する工数は large。

### 9-7. コスト追跡・予算管理の標準化 **[small]**

AutoGen は `gather_usage_summary()` でセッション全体のトークン・コストを集計する。Reyn の event log にはトークン使用量が記録可能な構造はあるが、集計・予算上限・アラートの標準機能は未実装。small で改善可能。

### 9-8. 分散ランタイム / スケーラビリティ **[large]**

AutoGen Core は gRPC ベースの分散エージェントランタイムをサポートし、クラウドスケールの並列エージェント実行が可能。Reyn はシングルプロセス前提の設計 (現時点)。分散対応は large で、pre-OSS フェーズでは優先度低と判断するのが現実的。

---

## References

- [AutoGen 公式ドキュメント](https://microsoft.github.io/autogen/stable/index.html)
- [AutoGen v0.4 アーキテクチャ解説 (Microsoft Research Blog)](https://www.microsoft.com/en-us/research/blog/autogen-v0-4-reimagining-the-foundation-of-agentic-ai-for-scale-extensibility-and-robustness/)
- [AutoGen 0.4 ローンチブログ](https://devblogs.microsoft.com/autogen/autogen-reimagined-launching-autogen-0-4/)
- [Microsoft Agent Framework 概要 (Microsoft Learn)](https://learn.microsoft.com/en-us/agent-framework/overview/)
- [AutoGen AgentChat チームドキュメント](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/teams.html)
- [AutoGen Termination Conditions](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/termination.html)
- [AutoGen State Management](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/state.html)
- [AutoGen GraphFlow](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/graph-flow.html)
- [AutoGen OpenTelemetry Integration](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/framework/telemetry.html)
- [AutoGen Studio](https://microsoft.github.io/autogen/stable/user-guide/autogenstudio-user-guide/index.html)
- [AG2 フォーク (ag2ai/ag2)](https://github.com/ag2ai/ag2)
- [AutoGen GitHub (microsoft/autogen)](https://github.com/microsoft/autogen)
- [AutoGen Persistence Roadmap Issue #2358](https://github.com/microsoft/autogen/issues/2358)
- [GraphFlow State Persistence Bug #7043](https://github.com/microsoft/autogen/issues/7043)
- [Microsoft Semantic Kernel + AutoGen 統合発表](https://visualstudiomagazine.com/articles/2025/10/01/semantic-kernel-autogen--open-source-microsoft-agent-framework.aspx)
- [Microsoft Agent Framework 1.0 GA (2026年4月)](https://visualstudiomagazine.com/articles/2026/04/06/microsoft-ships-production-ready-agent-framework-1-0-for-net-and-python.aspx)
