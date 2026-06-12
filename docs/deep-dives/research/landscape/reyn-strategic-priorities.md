---
title: Reyn — OSS ローンチ前戦略的優先事項
last_updated: 2026-05-10
status: stable
based_on:
  - competitive/langgraph.md
  - competitive/langchain.md
  - competitive/autogen.md
  - competitive/crewai.md
  - competitive/dify.md
  - competitive/openclaw.md
  - competitive/hermes-agent.md
  - landscape/emerging-players.md
  - docs_audit: 2026-05-08（docs/en/ + docs/ja/ + stdlib/skills/ 全体監査）
---

# Reyn — OSS ローンチ前戦略的優先事項

> ⚠️ **2026-06-13 supersession note**: 本ドキュメントは **2026-05-10 時点の
> pre-launch スナップショット（歴史的記録）**です。以降、当時「未実装 / Phase 2 /
> 計画中」とされた **MCP client・A2A（sync/async/webhook）・RAG framework
> (`recall`/`index_docs`)・コード実行（`sandboxed_exec` + Docker backend ⚗MVP）・
> stdlib 3→12** は既に landed しました。現在の実装状況は
> [`docs/feature-map.md`](../../../feature-map.md) を source of truth として参照。
> 以下の本文は当時の計画を保持するため inline 修正していません。

## エグゼクティブサマリー

競合 5 社 + 新興プレイヤー群の分析から見えた最重要発見は 2 点ある。第一に、**Reyn の設計上の差別化（P4/P5/P6）は genuine で競合に存在しない**が、その優位性が「動くコードで示せるもの」になっていない。第二に、**Stdlib の空洞化が Reyn の商品性を現時点でゼロに近づけている**。競合が 30〜600 本の実用ツールを揃えている中、Reyn は meta skill 3 本のみで、インストールして試した人間が即座に離脱する状態にある。OSS ローンチ前に最低限すべきことは「Stdlib の最小セット構築」と「可観測性の外部接続」の 2 つに集約される。それ以外の差別化（WAL・P4・P6 設計）はすでに実装済みであり、ドキュメントと訴求言語の整備で活かせる。

---

## 競合分析から導く優先ギャップ TOP5

### ギャップ 1: ドメインスキルの不足

> **⚠️ 修正 (2026-05-08)**: 競合分析時点では「ファイル操作・Web 検索が未実装」と記述していたが、
> 実装確認により **OS 組み込み Control IR ops として file (read/write/edit/grep/glob)・web_search・web_fetch・shell が既に実装済み**であることが判明。問題は OS プリミティブではなく、**ドメイン特化スキルの不足**に修正する。

- **問題**: OS 組み込み Control IR ops（file/web_search/web_fetch/shell）は実装済みで基本操作はカバーされている。一方、実業務で必要なドメイン特化スキル（RAG・DB 接続・PDF 処理・GitHub 統合・コード実行環境）は未実装であり、競合との差は依然として大きい。stdlib skill は 3 本（router/eval/improver）のみ。
- **競合の解法**:
  - LangChain: Vector Store 130+・LLM 100+・Document Loader 100+・Tool 50+（月間 2.37 億 DL）
  - CrewAI: 30+ 組み込みツール（Web 検索・PDF RAG・コード実行・DB・GitHub）+ UnifiedMemory
  - Dify: 50+ ツールプラグイン・Knowledge Base（PDF/Word/HTML）・Code Node（Python/Node.js）・HTTP Request Node
  - LangGraph: LangChain 統合 600+ を利用可能
- **Reyn への影響**: 「基本操作はできる」がドメインスキルなしでは実業務ワークフローが組めない。「勝ち筋 B（クラッシュ安全性）」も RAG や長時間処理スキルなしでは活かせない。
- **推奨アクション**:
  1. `recall_docs` — RAG（project_pending_recall_docs 計画済み）。既存 file ops と web_fetch を活用して構築可能
  2. `translate` — 多言語テキスト変換（日本企業用途で頻出）
  3. `http_call` — 外部 REST API 呼び出し（web_fetch との棲み分けを明確化）
  4. `code_exec` — **FP-0017 で設計済み**。`sandboxed_exec` op + `SandboxPolicy`/`SandboxBackend` 抽象層として起票。macOS は `sandbox-exec`（Seatbelt）、Linux は Landlock（コントリビュータ向け）。Docker 常駐プロセス不要のインプロセス方式を採用。
  この順で P7 準拠・`skill.md` 追加のみで実装する。
- **コスト見積**: `translate`/`http_call` は small（1〜2 日）。`recall_docs` は medium（ベクトル DB 設計を含む）。`code_exec` は FP-0017 設計済みのため medium（実装のみ）。
- **優先度**: **P1（ローンチ前必須）**

---

### ギャップ 2: 可観測性の外部接続なし

- **問題**: Reyn の `events/` は append-only・replay 可能という設計上の優位性を持つが、**非エンジニアが見られる UI がなく、外部オブザーバビリティツールへのエクスポートパスも未整備**。エンタープライズ営業の場で「監査ログを見せてください」に即答できない。
- **競合の解法**:
  - LangGraph + LangChain: LangSmith（ノード遷移・LLM 呼び出し・state 変化を全可視化、14〜400 日保持、RBAC/SSO Enterprise）
  - CrewAI: OpenTelemetry ネイティブ + OpenLIT/Langfuse/Dynatrace 連携が確立
  - AutoGen: OpenTelemetry 統合（任意 OTel バックエンドにエクスポート）
  - Dify: Langfuse / LangSmith / Opik 連携（Kakaku.com が本番ガバナンス中核に使用）
- **Reyn への影響**: P6 イベントログという genuine な差別化が「見えない」ままになる。Reyn の勝ち筋 A（ガバナンス最優先エンタープライズ）は可観測性 UI なしには証明できない。
- **推奨アクション**:
  - **最優先**: event log → OpenTelemetry (OTLP) エクスポーターを実装。Grafana / Jaeger / Langfuse 等と即接続できるようにする。
  - **次善策**: LangSmith 互換 SDK ラッパーを実装（small コスト）。これにより「P6 イベントログを既存ツールで見る」デモが即座に動く。
  - 独自 UI は large コストのため OSS ローンチ後にコミュニティに委ねる。
- **コスト見積**: OTel エクスポーター実装 = medium（1〜2 週）
- **優先度**: **P1（ローンチ前必須）**

---

### ギャップ 3: MCP クライアント統合（Server 側は実装済み）

> **⚠️ 修正 (2026-05-08)**: 実装確認により **MCP server（外部 LLM クライアントから Reyn エージェントを呼ぶ側）は `reyn mcp serve` として実装済み**であることが判明。未実装は MCP client（Reyn が外部 MCP server を呼ぶ側）と A2A のみ。

- **問題**: `reyn mcp serve` による MCP server は実装済み（Claude Code / Cursor / OpenAI SDK が `list_agents` + `send_to_agent` で Reyn を呼べる）。未実装は Reyn が外部 MCP server を呼ぶ **MCP client 側**と A2A プロトコル。この 2 つがないと「Reyn から外部ツール（GitHub MCP・Slack MCP 等）を呼べない」という制限になる。
- **競合の解法**:
  - AutoGen: MCP サーバ統合を Extensions で提供（GitHub・Azure Cognitive Search 等を MCP 経由で接続）
  - CrewAI: MCP Server/Client をツールとして統合。A2A プロトコルもネイティブサポート
  - OpenAI Agents SDK: MCP ネイティブ（月間 DL 1,030 万）
  - Google ADK: A2A プロトコルネイティブ対応
- **Reyn への影響**: MCP client がないと「Reyn スキルから MCP ツールを呼ぶ」パターンが使えない。2026 年に MCP が tool 接続の事実上の標準になる流れの中で、エコシステム拡張の制限になる。ただし MCP server が実装済みであるため、「Reyn を呼べない」問題は解消済み。
- **推奨アクション**:
  - MCP Client を Control IR の `run_mcp_tool` op として実装（P5・P7 準拠）。`MCPIROp` は models.py に定義済みで実装待ちの状態
  - A2A（`run_remote_agent` op）は MCP client 完了後に続けて実装
- **コスト見積**: MCP Client = small〜medium（権限モデルと P5 整合を含む）
- **優先度**: **P1（ローンチ前必須）** ※ Phase 2 ロードマップと整合

> **Docker MCP エコシステム（2026-05 新情報）**: Docker Desktop 4.42（2026-06）から `docker mcp` サブコマンドが標準搭載。`hub.docker.com/mcp` に 100+ サーバー（`registry.modelcontextprotocol.io` とは別レジストリ）。各サーバーはコンテナとして隔離実行され、`docker mcp gateway` がプロキシとして多重化。Reyn の `mcp_install.py` はすでに `registryType: "docker"` に対応済みだが、Docker MCP カタログ検索と `docker mcp gateway` 連携は未対応。**当面は不要**（Docker 常駐プロセスが必要なため）だが、エコシステムの成長に伴い将来対応が必要になる可能性がある。

---

### ギャップ 3.5: エージェント認証（FP-0016）

> **新規ギャップ（2026-05-10 追加）**

- **問題**: HTTP 型 MCP サーバーへの接続や長時間スキル実行において、認証情報の安全な管理・委任・リフレッシュの仕組みが未整備。MCP エコシステムの拡大（ギャップ 3 参照）に伴い、認証の問題が顕在化しつつある。
- **具体的なユースケースと必要な機能**:
  - **MCP HTTP Bearer ヘッダー**: HTTP 型 MCP サーバーへの接続に必要。即効性あり。
  - **OAuth トークン自動リフレッシュ**: 長時間スキルで必須。FP-0012 非同期実行と連動。
  - **Device Authorization Grant**: PAT 禁止の企業環境で必要（日本エンタープライズで頻出）。
  - **子スキルへのスコープ限定認証情報委任**: Confused Deputy 対策。
- **FP-0016 として起票済み**。優先度: MCP HTTP ヘッダー（SMALL）が最初のアンロッカー。
- **優先度**: **P1〜P2**（MCP HTTP Bearer は P1 必須、OAuth リフレッシュ・Device Flow は P2）

---

### ギャップ 4: Skill Authoring Guide の完全欠落

> **⚠️ docs 監査による修正 (2026-05-08)**: 競合分析時点では「Quickstart なし」と記述していたが、
> docs 監査で `tutorials/01〜05` が存在し「30 分で動く」は達成済みであることが判明。
> 問題は Quickstart ではなく、**「動かせた → 自分の skill を作れる」への橋が存在しないこと**。

- **問題**: tutorials（01-installation 〜 05-chat-mode）は存在し 30 分で動く。ただしドキュメント監査で以下が判明:
  - **Skill Authoring Guide が完全欠落**: SKILL.md テンプレート・Phase Best Practices・Artifact Schema 記法ガイド・デザインパターン集がゼロ
  - **「理解した → 実装できない」断絶**: `architecture.md` にコード例がなく、concepts は哲学的深さがある一方で「どう書くか」への実装パスがない
  - **Control IR ops カタログ未整備**: Phase 著者が「LLM に何を要求できるか」を参照できるドキュメントがない
  - **見本 skill が明示されていない**: `read_local_files`（2-phase 線形）や `skill_builder`（複雑ループ）が参考例として案内されていない
- **競合の解法**:
  - CrewAI: `role/goal/backstory` という人間に親しい API。公式 Quickstart で 10 分以内に動作確認できる。
  - LangGraph: node function + edge 定義だけで始められる。既存コードへの組み込みが直感的。
  - LangChain: Udemy/DataCamp コース多数・書籍・cookbook が充実。コミュニティ事例が豊富。
- **Reyn への影響**: 「Reyn でどう skill を書けばいいか」が分からないまま離脱するエンジニアが増える。設計原則（P1-P8）のドキュメントは業界水準以上だが、「良い skill とは何か」「どう実装するか」を教えるコンテンツがない。
- **推奨アクション**:
  - `SKILL.md Authoring Template`（frontmatter 全フィールドと説明のテンプレート）
  - `Phase Best Practices`（model_class 選択・allowed_ops 一覧・instructions 構造のルール）
  - `Artifact Schema Primer`（YAML JSON Schema vs Markdown notation 使い分け）
  - `Design Patterns: Simple Skill`（`read_local_files` パターン）/ `Complex Skill`（`skill_builder` パターン）
  - `reference/runtime/control-ir.md` に全 op 種別カタログを追加
  - Skill Author Contract ドキュメント完成（project_skill_author_contract_doc.md 計画済み）
- **コスト見積**: Template + Patterns = small（1〜3 日）; Control IR catalog = small; Skill Author Contract = small
- **優先度**: **P1（ローンチ前必須）**

---

### ギャップ 4.5: 日本語ドキュメントの未整備

> **docs 監査で新規追加 (2026-05-08)**

- **問題**: `docs/ja/` のカバレッジが 66%（108 en → 72 ja）。特に README で言及する機能の日本語版が存在しない。
  - `concepts/multi-agent/a2a.md` — README で言及、未翻訳
  - `concepts/tools-integrations/mcp.md` — README で言及、未翻訳
  - `how-to/use-an-mcp-server.md` — 実装ガイドの中核、未翻訳
  - `reference/upgrade-policy.md` — バージョン移行で日本ユーザーが詰まる、未翻訳
- **Reyn への影響**: 日本エンタープライズをターゲットとしながら、README で紹介する機能の説明が日本語で読めない状態は信頼性を損なう。日本の SIer が社内提案資料を作る際に英語ドキュメントしかない機能は説明しにくい。
- **推奨アクション**:
  - OSS ローンチ前に `concepts/multi-agent/a2a.md`・`concepts/tools-integrations/mcp.md`・`how-to/use-an-mcp-server.md` を日本語化
  - `reference/upgrade-policy.md` を追加
  - `decisions/` の翻訳は後回し可（設計調査者向けで初期ユーザーへの影響が低い）
- **コスト見積**: 4 ファイル = small（1〜2 日）
- **優先度**: **P1（ローンチ前必須）**

---

### ギャップ 5: LLM プロバイダ多様化の欠如

- **問題**: Reyn は現状 gemini-2.5-flash-lite（LiteLLM proxy）のみに依存。Weak LLM の empty-stop attractor 問題が ongoing であり、「モデルを変えれば解決する」オプションが利用者に提供されていない。
- **競合の解法**:
  - CrewAI: 25+ LLM プロバイダを LiteLLM 経由でサポート。モデル選択はスキル設定で可能
  - LangChain: Model Profiles（v1.1）でモデルの structured output 対応可否を自動判定・フォールバック
  - AutoGen / LangGraph: provider-agnostic（OpenAI / Anthropic / Google / Ollama 等）
- **Reyn への影響**: 日本エンタープライズは Azure OpenAI Service・Bedrock・オンプレ Ollama 等を使うケースが多い。「gemini-2.5-flash-lite しか動かない」では採用候補から外れる。また、Weak LLM 問題の恒久対策としてもモデル切り替え可能性が必要。
- **推奨アクション**:
  - OS の LLM 呼び出し層を provider-agnostic（LiteLLM 互換インタフェース）に抽象化
  - モデル選択を `reyn.yaml` またはスキル設定から指定可能にする
  - 最低限 OpenAI / Azure OpenAI / Anthropic / Ollama の 4 プロバイダで動作確認
- **コスト見積**: small〜medium（LiteLLM 経由なら設定変更主体）
- **優先度**: **P2（ローンチ後すぐ）** ※ Stdlib・可観測性の次

---

## 強調すべき差別化 TOP3

### 差別化 1: OS レベルの遷移制御（P4）— 競合に存在しない

**内容**: Reyn の OS は LLM に「次に遷移できるフェーズの候補」のみを提示し、候補外の遷移を即 reject する。`{control, artifact, control_ir}` を毎回 JSON スキーマ検証し、violation は実行前に拒否される。

**競合との比較根拠**:
- LangGraph: `Command()` API で LLM が任意ノード名を返せる構成が可能。制約はスキル作者のコーディング規律に委ねられる（langgraph.md §1, §10）
- LangChain: 「どのツールを何回呼ぶか」を LLM が自律決定。OS 側の絞り込みなし（langchain.md §1）
- AutoGen: `SelectorGroupChat` で次の発言者を LLM が自由選択（autogen.md §1）
- CrewAI: Hierarchical Process で manager_llm が動的タスク割当。候補制限機構なし（crewai.md §1）
- Dify: Workflow モードは確定的 DAG で LLM が遷移を決めないが、OS レベルの強制バリデーションは存在しない（dify.md §3）

**刺さる顧客セグメント**: 金融・医療・公共・官公庁向け SIer。「なぜこのフェーズに遷移したか」をガバナンス部門・監査部門に説明しなければならない組織。LangChain の State of Agent Engineering 調査で「本番未導入 45%」の主因が「予測可能性問題」であることが実証済み。

---

### 差別化 2: WAL + forward-replay による自動クラッシュ回復（P5 + ADR-0023）

**内容**: workspace ファイルベース SSoT（P5）を基盤とした WAL（Write-Ahead Log）+ forward-replay により、プロセスクラッシュ後に OS が自動的にフェーズ再開する。データはすべて workspace を経由するため「どこまで完了したか」がファイルシステムに残り、再実行時に安全にスキップできる。

**競合との比較根拠**:
- CrewAI `@persist`: 自動 resume なし・単一プロセス前提・直近 1 run のみ・排他制御なし（Diagrid 外部調査で実証）
- AutoGen: 組み込みチェックポイントなし。`save_state()` / `load_state()` はアプリケーション管理。ロードマップ issue #2358 で要望中
- Dify: クラッシュ回復 "Closed as not planned"（Issue #12083）
- LangGraph: Checkpointer は成熟しているが「durable execution ではない」（Diagrid 調査）。単一プロセス前提かつ複数ワーカー排他制御なし

**刺さる顧客セグメント**: 長時間実行ジョブ（数時間〜数日）を持つ製造業・物流・バックオフィス自動化の IT 担当者。「処理が失敗したら最初からやり直し」が許容できない本番システム設計者。

---

### 差別化 3: デフォルトゼロテレメトリ + append-only イベントログ（P6）

**内容**: Reyn は設計上テレメトリゼロ。すべてのデータは workspace（ローカルファイルシステム）のみに存在する。OS はすべての状態変化を `events/` に append-only で強制記録し、hash chain（planned）で改ざん検知可能にする。SaaS 契約なし・追加設定なしで即座に監査証跡が生成される。

**競合との比較根拠**:
- CrewAI OSS: デフォルトでエージェントロール名・ツール名・モデル名を外部送信。EU GDPR 懸念 GitHub Issue あり（crewai.md §5）
- LangGraph/LangChain: 監査ログ保持は LangSmith Enterprise（月 $2,000〜5,000）が必要。フレームワーク単体に append-only 保証なし
- AutoGen: OpenTelemetry スパンは append-only 保証なし・replay 不可
- Dify: Langfuse 連携は外部 SaaS 依存。append-only 保証の公式明示なし

**刺さる顧客セグメント**: 銀行・保険・医療機関など社内ネットワーク外にデータを出せない日本エンタープライズ。OSS 採用の情報セキュリティ審査が厳格な組織。「デフォルト ON テレメトリ」が即却下される日本のセキュリティ審査フローに対する回答。

---

## 競合別戦略マップ

**vs LangGraph**: LangGraph は「業界標準の汎用オーケストレーションフレームワーク」として事実上の標準に近い位置を占める（31,400+ stars、月間 3,450 万 DL、400 社+採用）。直接の数字比較では勝ち目がない。戦略は「LangGraph では解けない問題」を持つ顧客セグメントに集中することだ。具体的には `Command()` API の乱用による遷移の予測不能性を問題視する金融・公共 SIer が対象。「P4 = OS が遷移を保証する」という訴求を、LangGraph ユーザーが実際に遭遇した `Command()` 事故事例とセットで語れる外向きドキュメントを準備する。LangGraph の可観測性（LangSmith）が SaaS 依存であるのに対し、Reyn のイベントログがローカル完結・ライセンスフリーである点も日本エンタープライズで訴求できる。

**vs LangChain**: LangChain のエコシステム規模（136,000+ stars、月間 2.37 億 DL）と Stdlib の豊富さには真っ向対抗しない。LangChain 自身の State of Agent Engineering 調査が「本番未導入 45%」を示しており、その主因が予測可能性問題であることをそのまま Reyn の訴求に使う。「LangChain でプロトタイプを作ったが本番化できない」エンジニアへの移行支援ドキュメントが有効。LangChain のデバッグ困難さ（抽象レイヤー多層・LangSmith 依存）と対比して「Reyn のイベントログを grep すれば何が起きたか分かる」という実演デモを作る。

**vs AutoGen**: AutoGen v0.4 本体はメンテナンスモードに移行し、後継の Microsoft Agent Framework（Entra ID + Azure Monitor + SOC2）が本命になっている。Azure 環境前提の日本大企業とは直接競合するが、Reyn は「特定クラウドに依存しない OS agnosticism」を強みとして打ち出す。Azure 契約がない日本の中堅 SIer・製造業では Reyn の軽量ローカル完結設計が優位になる。AutoGen が持つ組み込みクラッシュ回復のなさと GraphFlow の既知バグを事実ベースで対比する。

**vs CrewAI**: CrewAI は「最速成長フレームワーク」として存在感が大きい（47,800+ stars、月間 5M DL、Fortune 500 60%+採用 [自社クレーム]、$18M 調達）。正面から戦う価値はなく、CrewAI の「デフォルト ON テレメトリ」問題を日本エンタープライズ向けに明確に突く。「CrewAI を情報セキュリティ審査に出したら否決された」という痛点を持つ日本企業の SIer が乗り換えターゲット。さらに Diagrid が実証した CrewAI のクラッシュ回復の構造的限界（自動 resume なし・単一プロセス・排他制御なし）は Reyn の WAL との比較で有利に働く。Stdlib の差は大きいため、最低限の Stdlib なしに CrewAI 移行を促すことはできない。

**vs Dify**: Dify は競合というより「補完関係」と位置づける（dify.md §9）。日本市場での実績（Kakaku.com 全社展開・CTC パートナーシップ・LangGenius K.K. 設立）は Reyn が現状では勝てない領域。戦略は「Dify でプロトタイプを作り、本番運用のガバナンスが必要になった段階で Reyn に移行する」という 2 段階シナリオを明示的にドキュメント化することだ。「Dify から Reyn への移行ガイド」は Reyn の上位ファネルとして機能する。Dify のクラッシュ回復「Closed as not planned」とエンタープライズ向け LLM 出力バリデーションのオプト・イン設計を、Reyn がフォールバック先として使われる根拠として活用する。

**vs 新興勢力（Microsoft Agent Framework / PydanticAI 等）**: Microsoft Agent Framework は Reyn の直接的な最大脅威（enterprise + Azure エコシステム + .NET/Python 対応）。日本の大手企業の多くが Microsoft 製品を使っており、Entra ID 統合の訴求力は強い。対抗策は「クラウドロックイン vs. ポータブルな OS 設計」で差別化する。Reyn の skill は OS 変更不要で移植可能（P7）であり、Azure を使わなくても動くことを強調する。PydanticAI は思想的に近い（型安全バリデーション）が OS 層を持たないため、Reyn の上位概念として位置づけられる。Agno の「5,000x faster」ベンチマークに対しては「確実性（N 回連続完走率）」ベンチマークで応じ、速度競争に入らない。

**vs OpenClaw**: OpenClaw（旧 Clawdbot/Moltbot）は 2026-03 までに 370K stars・500K インスタンスを達成し、「デファクトの grassroots エージェント OS」になりつつある。ただしその性格は Reyn とほぼ正反対: 個人ユーザー向けメッセージング PF UI・LLM 完全自律・セキュリティは後付け（138 CVE、CVSS 9.9 含む）・ambient authority モデル（per-skill ゲートなし）。戦略は真っ向対抗ではなく **「OpenClaw の governance-ready 版」** としてポジショニングすることだ。「OpenClaw は動く、Reyn は動くと保証できる」という訴求軸。日本エンタープライズが OpenClaw の 138 CVE と ambient authority を懸念する場面で、Reyn の P4/P5/P6 が直接の回答になる。また OpenClaw の急成長は「エンタープライズでも自律エージェントへの期待が高まっている」という市場シグナルであり、Reyn のターゲットへの追い風でもある。OpenClaw 開発者が OpenAI に入社し非営利財団移管の方針が出たため、プロジェクト運営の持続可能性も今後の注視点。

**vs Hermes Agent**: Hermes Agent（Nous Research、MIT、2026-02）は「自己改善するエージェント」という新カテゴリを GEPA（ICLR 2026 Oral）で定義し、7 週で 95K stars・139K stars（2026-05）を達成した。Reyn の設計哲学（predictability over autonomy）と真正面から対立する哲学（autonomy + self-improvement）を持つ。短期（2026）では Hermes v0.x の API 不安定・監査ログ未出荷（Issue #487）により本番エンタープライズ採用は困難で、Reyn の優位性（P6 出荷済み・WAL 自動回復）は明確。ただし Hermes が v1.0（2026 末 ETA）を出荷し Issue #487 が完了した場合、「監査ログがない」という最大の弱点が解消される。そのシナリオへの準備として: (a) P6 の「出荷済み・本番実績あり」という先行者優位を強調する外向きドキュメント、(b) Reyn の「再現可能な実行（スキルがバージョン固定）」vs Hermes の「自己改善（スキルが変化）」という二分法の訴求を確立する、の 2 点が中期の優先事項。長期的には Hermes の GEPA が示す「繰り返しタスクで 40% 速度向上」という価値を Reyn が提供できないままでは、同じ組織内で Hermes に置き換えられるリスクがある（→ P3: 自己改善/永続メモリの研究着手）。

---

## OSS ローンチ前チェックリスト

競合分析から導かれる「これがないと恥ずかしい」リスト:

### P1: ローンチ前に必須

**Stdlib / 機能実装**
- [ ] `file_read` / `file_write` stdlib skill 実装・テスト済み
- [ ] `http_call` stdlib skill 実装・テスト済み
- [ ] `translate` stdlib skill 実装・テスト済み（日本語 docs との整合）
- [ ] `web_search` stdlib skill 実装・テスト済み
- [ ] `recall_docs` stdlib skill — 最低限の実装（ベクトル DB なしの BM25 のみでも可）
- [ ] event log → OpenTelemetry エクスポーター実装（Grafana/Langfuse 等に繋がることを確認）
- [ ] MCP Client が Control IR から呼び出せる（`run_mcp_tool` op）

**ドキュメント（docs 監査で追加、2026-05-08）**
- [ ] **CHANGELOG.md の充実** — 0.1.0a1 の実装内容を記録。現在は `[Unreleased]` のみで実質空
- [ ] **README に競合比較表** — LangGraph / CrewAI / Dify との差をひと目で分かる表（5分で理解できること）
- [ ] **Troubleshooting guide 作成**（`how-to/troubleshooting.md`）— 競合はどこも持っている。Runtime errors / permission denied / validation error の典型障害を網羅
- [ ] **Skill Authoring Guide** — SKILL.md テンプレート・Phase Best Practices・Artifact Schema Primer・Design Patterns（Simple / Complex）
- [ ] **Control IR ops カタログ** — `reference/runtime/control-ir.md` に全 op 種別を列挙（Phase 著者の参照用）
- [ ] **日本語ドキュメント補完** — `concepts/multi-agent/a2a.md`・`concepts/tools-integrations/mcp.md`・`how-to/use-an-mcp-server.md`・`reference/upgrade-policy.md` の日本語化
- [ ] **Skill Author Contract ドキュメント公開**（project_skill_author_contract_doc.md を完成させる）
- [ ] ゼロテレメトリ設計を `README` と docs に明記（CrewAI との差別化として）
- [ ] OSS ライセンス決定（MIT 推奨。Apache 2.0 も可だが Dify 前例の追加条項問題を避ける）

### P2: ローンチ後すぐ

- [ ] LLM プロバイダ多様化（OpenAI / Azure OpenAI / Anthropic / Ollama の 4 プロバイダで動作確認）
- [ ] Human-in-the-loop の標準化（`ask_user` 非同期待機 + workspace への承認結果書き込み + forward-replay 再開のフルパス）
- [ ] LangGraph との差別化を外向き言語で整理したドキュメント（P4/P7/P6 の意味を非エンジニアに説明）
- [ ] サンプル Skill リポジトリ（GitHub、OSS ローンチ時同時公開）
- [ ] CrewAI テレメトリ問題との対比資料（情報セキュリティ審査向け FAQ）
- [ ] Dify から Reyn への移行ガイド（補完関係の明示）

### P3: 中長期

- [ ] RBAC / SSO 実装（ADR として設計先行）
- [ ] hash chain による改ざん検知（event log の P6 強化）— Hermes Issue #487 出荷前に完了させ「先行者」ポジションを確保
- [ ] PostgreSQL / DB バックエンドの Checkpointer（LangGraph の checkpointer に相当するスケーラビリティ）
- [ ] A2A プロトコル対応（`run_remote_agent` op）
- [ ] Time Travel デバッグ CLI（event log リプレイの UI）
- [ ] TypeScript SDK（日本フロントエンド開発者・Mastra 対抗）
- [ ] Skill registry（`reyn install <skill>` CLI）— コミュニティ skill 流通の基盤
- [ ] 永続メモリ基盤（Hermes の 3 レイヤーメモリに相当）— セッション横断ユーザーモデリング。workspace を SSoT として拡張する設計なら P5 と整合
- [ ] スキル自動生成 PoC（GEPA 参考）— 実行トレースから skill.md 候補を生成する研究。本番化は 2027+ 想定だが ADR で方向性を固める
- [ ] 弱 LLM 信頼度スコアリング（Hermes の fallback provider パターンを Reyn P4 制約と組み合わせたコスト最適化）

---

## 実装確認済み（FP 不要と判明）

> **2026-05-10 セッションで確認**。競合分析または過去のギャップ記述で「未実装」と想定していたが、コード調査の結果すでに実装済みと判明した項目。

- **エージェント単位コスト帰属**: `src/reyn/chat/tui/widgets/right_panel/cost_tab.py` に `by_agent` + `by_agent_skill` 集計が実装済み。競合（LangSmith Enterprise 相当）の機能がローカル完結で提供されていることを訴求ポイントに追加可能。
- **永続メモリ**: `src/reyn/memory/memory.py` + `src/reyn/chat/tui/widgets/right_panel/memory_tab.py` として実装済み。user / feedback / project / reference の 4 タイプをサポート。P3 チェックリスト「永続メモリ基盤」は設計検討の対象を「セッション横断ユーザーモデリングの高度化」に絞り直す。
- **マルチセッション文脈継続**: WAL + フェーズ境界復元で解決済み（P5 ワークスペース永続化の設計意図通り）。クラッシュ回復の仕組みがそのままマルチセッション継続にも機能する。

---

## 参照

- [competitive/langgraph.md](../competitive/langgraph.md)
- [competitive/langchain.md](../competitive/langchain.md)
- [competitive/autogen.md](../competitive/autogen.md)
- [competitive/crewai.md](../competitive/crewai.md)
- [competitive/dify.md](../competitive/dify.md)
- [competitive/openclaw.md](../competitive/openclaw.md)
- [competitive/hermes-agent.md](../competitive/hermes-agent.md)
- [competitive/README.md](../competitive/README.md) — 横比較テーブル
- [landscape/emerging-players.md](emerging-players.md)
- [positioning/reyn-differentiators.md](../positioning/reyn-differentiators.md)
