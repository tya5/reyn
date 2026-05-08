---
title: Dify — 競合分析
last_updated: 2026-05-08
status: stable
sources:
  - url: https://dify.ai/
    accessed: 2026-05-08
  - url: https://docs.dify.ai/
    accessed: 2026-05-08
  - url: https://github.com/langgenius/dify
    accessed: 2026-05-08
  - url: https://dify.ai/enterprise
    accessed: 2026-05-08
  - url: https://dify.ai/pricing
    accessed: 2026-05-08
  - url: https://dify.ai/blog/dify-v1-0-building-a-vibrant-plugin-ecosystem
    accessed: 2026-05-08
  - url: https://dify.ai/blog/dify-agent-node-introduction-when-workflows-learn-autonomous-reasoning
    accessed: 2026-05-08
  - url: https://dify.ai/blog/boost-ai-workflow-resilience-with-error-handling
    accessed: 2026-05-08
  - url: https://dify.ai/blog/2025-dify-summer-highlights
    accessed: 2026-05-08
  - url: https://dify.ai/blog/kakaku-accelerates-ai-adoption-with-dify-fast-secure-and-scalable
    accessed: 2026-05-08
  - url: https://www.ctc-g.co.jp/en/company/release/20251024-02081.html
    accessed: 2026-05-08
  - url: https://jimmysong.io/blog/open-source-ai-agent-workflow-comparison/
    accessed: 2026-05-08
  - url: https://skywork.ai/blog/dify-review-2025-workflows-agents-rag-ai-apps/
    accessed: 2026-05-08
  - url: https://www.baytechconsulting.com/blog/what-is-dify-ai-2025
    accessed: 2026-05-08
  - url: https://github.com/langgenius/dify/issues/12083
    accessed: 2026-05-08
---

# Dify — 競合分析

## TL;DR

Dify は「ノーコード〜ローコードでAIアプリ/エージェントワークフローを構築・運用するプラットフォーム」であり、LLM の役割はワークフロー上の1ノード（推論・生成）に留まる。Reyn は「LLM が遷移制御の決定者」であり OS が構造的に制約を課すエンジン設計で、両者はセグメントと設計哲学の両面で根本的に異なる。Dify は非エンジニアの業務部門担当者が主要ターゲットであり、Reyn は制御・予測可能性を重視する日本エンタープライズ向けのエンジニアフレームワークである。

---

## 1. コアアーキテクチャ

### 全体設計

Dify は **Backend-as-a-Service + LLMOps プラットフォーム** として設計されている。バックエンドは Python (Flask) + PostgreSQL、フロントエンドは Next.js、実行基盤は Docker / Kubernetes (Helm charts)。最小構成は 2コアCPU / 4GB RAM、推奨は 8コア / 16GB RAM。

主要コンポーネント:
- **LLM Orchestration**: 50+ モデルプロバイダーとの接続レイヤー。OpenAI, Anthropic, Gemini, Ollama, Azure AI Foundry 等を Plugin として管理
- **Visual Workflow Studio**: ドラッグ&ドロップのノードベースキャンバス
- **Deployment Hub**: API エンドポイント / Chatbot / Web App として1クリックデプロイ
- **Knowledge Base**: RAG パイプライン (ベクトルDB + BM25 ハイブリッド検索)
- **Plugin Daemon**: v1.0 以降のモデル・ツール管理実行プロセス

### LLM の役割

**Dify においてLLMはワークフロー上の1ノードであり、遷移制御の決定者ではない。**

Dify Workflow モードでは、ノード間の遷移は視覚的に定義されたグラフ通りに確定的に実行される。LLM は「LLM ノード」として呼び出され、出力を次ノードの変数として渡すに過ぎない。動的な分岐はユーザーが定義した If/Else ノードと Question Classifier ノードで制御する。

唯一の例外が **Agent Node**（v1.0 以降）。このノード内では LLM が ReAct / Function Calling ストラテジーに基づいてツール選択と推論を自律的に実行する。ただし Agent Node 自体はワークフローの1ノードとして存在し、ワークフロー全体の遷移を支配はしない。

### 3つのアプリケーションモード

| モード | 特徴 | LLM の裁量 |
|--------|------|-----------|
| **Chatflow** | 会話型アプリ。マルチターン対話に特化。メモリ機能付きLLMノードを持つ | ノード内のみ |
| **Workflow** | バッチ処理・自動化パイプライン向け。ステートレス実行 | ノード内のみ |
| **Agent (Agent Node)** | ワークフロー内の1ノードとして組み込まれる自律推論ユニット | ノード内でReAct/FC自律判断 |

---

## 2. ワークフロー単位

### ノード設計

Dify のワークフローはノードとエッジで構成される DAG (有向非巡回グラフ)。コードを書かずにキャンバス上でノードを接続することで処理フローを構築する。

**組み込みノード一覧:**

| ノード種別 | 機能 |
|-----------|------|
| LLM Node | プロンプト→LLM呼び出し→出力。Structured Output (JSON Schema) 対応 |
| Knowledge Retrieval | ベクトルDB検索 (Milvus, TiDB, Weaviate 等) + BM25 ハイブリッド |
| Question Classifier | LLM駆動のルーティング。入力テキストをカテゴリ分類して分岐 |
| HTTP Request | 外部API呼び出し。認証設定込み |
| Code Node | Python / Node.js コード実行。サンドボックス環境 |
| If/Else | 条件分岐 |
| Iteration | ループ処理。リスト要素の並列/逐次処理 |
| Template (Jinja2) | 変数埋め込みと文字列変換 |
| Variable Assigner | ノード間変数の再代入 |
| Agent Node | 自律推論ユニット。ReAct/FC戦略をプラグイン選択 |
| Human Input (v1.13) | ワークフローを一時停止して人間のレビューを待機 |
| Tool Node | Plugin Marketplace の50+ツール統合 |

### データフロー

ノード間のデータは **変数参照** (`{{node_name.output}}`) で渡される。ワークフロー実行中のステート管理はDifyのバックエンドDB (PostgreSQL) が担う。ノード間でのデータ受け渡しはランタイムの変数スコープを通じた **in-memory + DB 混在型**であり、明示的なファイルシステムベースのワークスペース概念は存在しない。

### Reyn の Phase/Skill との比較

| 観点 | Dify | Reyn |
|------|------|------|
| 構成単位 | ノード (Canvas上の視覚部品) | Phase (input_schema + instructions) |
| 接続定義 | キャンバス上でエッジを引く | Skill の `graph` フィールドで宣言 |
| 新機能追加 | ノードタイプを追加 / Plugin作成 | Skill を追加 (OS変更不要: P7) |
| 実行前の静的検証 | なし (実行時エラーを error branch で処理) | LLM出力の transition/finish validation を全実行前に実施 |
| ノーコード対応 | Yes (主要ユースケース) | No (コード必須) |

---

## 3. 信頼性・回復力

### エラーハンドリング (v0.14.0〜)

Dify はノードレベルのエラー処理を持つ。対象ノード: LLM, HTTP, Tool, Code の4種。

3つの戦略:
1. **Default Value**: エラー時にあらかじめ定義したデフォルト値を出力として使用し、ワークフロー継続
2. **Fail Branch**: エラー発生時に `error_type` / `error_message` 変数を持つ専用エラーブランチへ分岐
3. **Retry on Failure**: ノード単位のリトライ設定 (最大回数・間隔を設定可)

並列ブランチの独立実行: v0.14.0 以降、並列ブランチの一方が失敗しても他方は継続できる。

### クラッシュ回復・チェックポイント

**Dify にはワークフローレベルのクラッシュ回復機能が存在しない。**

GitHub Issue #12083 (2024-12-25) にて "失敗ポイントからの再開" 機能が要求されたが、メンテナに **"Closed as not planned"** (実装予定なし) としてクローズされた。現状ではワークフローが途中でクラッシュした場合、先頭から再実行が必要。

### モデル切り替え・Fallback

- **Multi-Credential Management** (v1.13): 同一プロバイダーに複数 API キーを設定しロードバランシング。1キーが制限超過した場合は自動切り替え
- LLMノード内の fallback: LLM が不正な応答を返した場合 default value または error branch で対処
- モデル切り替えはユーザーがノード単位でモデルを変更する UI 操作で行う

### 構造化出力バリデーション (v1.3.0〜)

LLMノードに **JSON Schema Editor** が追加され、LLM 出力の構造を定義可能。ネイティブ対応モデルはJSON Schema を直接使用、非対応モデルはスキーマをプロンプトに組み込む (出力品質は保証されない)。バリデーション失敗時は `"Failed to parse structured output"` エラーが発生し、error handling 設定に従いフォールバック。

**[Reyn との差分]** Reyn の LLM 出力コントラクト (`control + artifact + control_ir`) は OS が実行前に必ず全フィールド検証を行い、violation は実行拒否となる強制的な設計。Dify の構造化出力はオプト・インであり、バリデーション失敗は実行時エラーとして扱われる。

---

## 4. Stdlib・標準装備

### 組み込み機能

Dify は豊富な標準機能を持つ。これが Reyn との最大の非対称性の一つ。

**Knowledge Base (RAG)**:
- PDF / Word / HTML / Markdown / CSV 等の文書インデックス作成
- ハイブリッド検索: ベクトル検索 (Milvus, TiDB, Weaviate, Couchbase) + BM25 全文検索
- Reranking, top-k 設定, メタデータフィルタリング
- Knowledge Pipeline (2025): マルチモーダル対応予定 (テキスト・画像・表)

**Tool Plugin (50+ 組み込み)**:
- Google Search, DALL·E, Stable Diffusion, WolframAlpha
- Perplexity, Firecrawl, Jina AI
- Discord, Slack, GitHub, Notion, Gmail (OAuth 認証付き)
- ComfyUI, Telegraph

**Agent Strategies**:
- ReAct (Think–Act–Observe ループ)
- Function Calling
- コミュニティ拡張: CoT, ToT, GoT, BoT

**外部統合**:
- MCP (Model Context Protocol) 対応: HTTP ベース MCP サービス統合 (事前認証・認証不要モード)
- Langfuse, LangSmith, Opik によるオブザーバビリティ統合

**[Reyn との差分]** Reyn は OS 組み込み Control IR ops (file/web_search/web_fetch/shell/mcp) を持つが、meta skill は 3 本のみ。翻訳・RAG・PDF処理・コード実行環境等のドメインスキルは未整備。Dify はこの領域で圧倒的に優位。

---

## 5. Enterprise機能

### Dify Enterprise の主要機能

**認証・アクセス管理**:
- SSO: SAML, OIDC, OAuth2 対応
- ロールベースアクセス制御 (RBAC)
- 多要素認証 (MFA) / 二段階認証
- Azure AD 統合 (Kakaku.com 実績: 社員証明書連携)

**マルチテナント管理**:
- ワークスペース分離: チームごとにアプリ・ユーザー・予算を独立管理
- Admin API: ワークスペース作成・権限管理の自動化スクリプト対応
- 公開リンク共有の一括無効化設定

**監査・ログ**:
- 使用ログ分析
- ワークスペース管理者の定期監査機能
- ノードレベルの実行トレース (Langfuse 連携で本番トレース)
- **注意**: イベントログの append-only 保証や hash chain 監査証跡は公式ドキュメント上で明示されていない **[Inferred: 監査要件の厳格さはReynのP6設計より低い可能性が高い]**

**デプロイメント**:
- On-Premise (自社サーバー)
- パブリッククラウド (AWS, Azure, GCP)
- VPC デプロイ
- Kubernetes (Helm charts) ネイティブ対応

**日本市場での実績**:
- 2025年2月: LangGenius K.K. (日本法人) 設立 (東京・日本橋)
- 2025年10月: CTC (伊藤忠テクノソリューションズ) が Dify Enterprise を販売開始。3年で30億円の売上目標。製造・金融・流通セクター向け
- 2025年10月: IF Con Tokyo 2025 開催、日本 Dify 協会 設立
- Kakaku.com: 全社員の75%が登録、950本近くの内部アプリを構築。GKE + Azure AD SSO で運用

---

## 6. Ecosystem

### GitHub

- Stars: 約139,000 (2026年4月時点。2025年末時点で100K突破)
- 初回リリース: 2023年
- v1.0.0 リリース: 2025年 (Plugin アーキテクチャへの転換)
- リリース頻度: 週次リリース (2026年3月に2026-03-18, 2026-03-27 と確認)
- Contributors: 290+
- Community: Discord + GitHub Issues (180,000+ 開発者コミュニティ)

### Plugin Marketplace

- 公式プラグイン: 120+ (Models, Tools, Agent Strategies, Extensions)
- レビュープロセス: Marketplace 掲載にはコードレビューと隔離実行確認が必要
- 配布チャネル: Marketplace / GitHub / ローカルパッケージ

### 更新頻度・メンテナンス

週次のリリースサイクル。2025年夏ハイライトとして v1.7〜v1.8 の機能追加が公開。2026年4月時点で過去30日以内にコミット有り。LangGenius 社が専任チームで継続開発中。

### 日本市場での採用状況

- CTC パートナーシップにより大企業向け販路確立済み
- Kakaku.com (価格.com / 食べログ運営) が全社展開の実績
- AWS Summit Japan 2025 に出展
- 日本 Dify 協会が PoC→本番移行の支援を組織化

---

## 7. Pricing / License

### SaaS 料金体系

| プラン | 月額 | メッセージクレジット | チームメンバー | アプリ数 |
|--------|------|---------------------|----------------|---------|
| Sandbox (無料) | $0 | 200 | 1 | 5 |
| Professional | $59 | 5,000/月 | 3 | 50 |
| Team | $159 | 10,000/月 | 50 | 200 |
| Enterprise | 要問い合わせ | カスタム | カスタム | カスタム |

※ 年間払い17%割引。Professional = $472/年、Team = $1,272/年

### Self-hosted (Community Edition)

無償。Docker Compose / Kubernetes で自己ホスト可能。最小要件: 2コアCPU / 4GB RAM。

### ライセンス

Apache License 2.0 ベース + **追加条項**: マルチテナント SaaS としての再配布には LangGenius の書面許可が必要。ロゴ・著作権表示の削除禁止。この追加条項の Apache 2.0 適合性についてはコミュニティで議論があるが、内部利用・エンタープライズ導入では実質的に問題なし。

---

## 8. Reyn対比

| 軸 | Dify | Reyn | 判定 |
|---|---|---|---|
| **LLMの役割** | ワークフロー上の1ノード（推論・生成のみ）。遷移はユーザー定義グラフが決定 | Decision engine (P4)。OS候補提示→LLM選択で遷移制御 | 設計哲学が根本的に異なる。比較不能 |
| **遷移制御** | 視覚的エッジ定義 (DAG)。LLM は遷移に関与しない | OS候補提示→LLM選択。P4により任意フェーズ選択は不可 | 設計方針の差。Difyは確定的、Reynは制約付き自律 |
| **データフロー** | 変数参照 (`{{node.output}}`)。in-memory + DB混在。ワークスペース概念なし | workspace経由のみ (P5)。ファイルシステムSSoT | Reyn優 (監査・クラッシュ回復の基盤として) |
| **監査** | 実行ログ・トレース (Langfuse連携)。append-only保証の明示なし | event log append-only (P6)。リプレイ可能 | Reyn優 (ガバナンス厳格要件向け) |
| **skill/node追加** | Plugin作成 (OS=Dify本体への変更なし)。ノーコード範囲内はキャンバスで即追加 | OS変更不要 (P7)。Skill追加のみ | 同等 (両者ともコア変更不要) |
| **クラッシュ回復** | **未実装**。Issue #12083が "Closed as not planned"。クラッシュ時は先頭から再実行 | WAL + forward-replay によるフェーズ再開 | **Reyn優** (長時間ジョブ・ミッションクリティカル用途) |
| **LLM出力バリデーション** | オプト・イン (JSON Schema Editor v1.3+)。バリデーション失敗はランタイムエラー | 全実行前の強制バリデーション (`control + artifact + control_ir`)。violation = 実行拒否 | Reyn優 (ガバナンス保証) |
| **エンジニア不要度** | 高。業務部門担当者がノーコードで構築可能 | 低。Skill設計・authoring にコード必須 | **Dify優** (裾野の広さ) |
| **stdlib 豊富さ** | 高。50+ツール、RAG、HTTP、Code、Knowledge Base が標準装備 | 中低。OS 組み込み ops (file/web/shell/mcp) + meta skill 3本。RAG・翻訳・PDF処理等のドメインスキルなし | **Dify優** (実業務適用の即戦力) |
| **エコシステム** | 大。139K GitHub stars、180K+ 開発者、CTC等の日本パートナー | 小。pre-OSS、コミュニティ形成前 | **Dify優** (採用・サポート・情報量) |
| **予測可能性** | Workflow モードは高い。Agent Node は LLM依存で可変 | 設計上の最大目標。候補制約 + OS validation で高水準を目指す | 同等〜Reyn優 (設計上の確信度) |
| **ガバナンス/コンプライアンス** | Enterprise: SSO, RBAC, MFA, 監査ログ実績あり (Kakaku.com) | 設計上 P6 でイベント監査証跡を持つが Enterprise機能未整備 | **Dify優** (現状の実装完成度) |
| **ライセンス** | Apache 2.0 + SaaS制限追加条項。Self-hosted は無償 | 未定 (pre-OSS) | Dify優 (明確な採用判断可能) |

---

## 9. Reynが追いつくために必要なこと

### Difyが解いていてReynが未着手の問題

**前提**: DifyはノーコードでReynはコード必須という根本的なセグメント差がある。以下はReynがエンジニア向けフレームワークとして取り組むべき機能ギャップを示す。

---

#### 9.1 Stdlib の充実 (技術コスト: medium)

**Difyの状況**: HTTP Request, Code Node (Python/Node.js), Knowledge Base, 50+ ツールが標準装備。業務部門が「何も準備せず」でも実業務ワークフローを構築できる。

**Reynの現状**: OS 組み込み Control IR ops (file/web/shell/mcp) は実装済み。不足しているのは RAG・翻訳・PDF処理・コード実行環境等のドメイン特化スキル。

**対応**: `recall_docs` (メモリ計画済み), `translate`, `code_exec` 等のドメインスキルを stdlib に追加する。コアの OS アーキテクチャ変更は不要 (P7準拠)。

---

#### 9.2 オブザーバビリティ / トレーシング統合 (技術コスト: medium)

**Difyの状況**: Langfuse, LangSmith, Opik との統合により本番環境での LLM 呼び出しトレースが即日構築できる。Kakaku.com はこれを本番ガバナンスの中核に使っている。

**Reynの現状**: `events/` ログは append-only で P6 準拠だが、外部オブザーバビリティツールへのエクスポートパスが未整備。`REYN_LLM_TRACE_DUMP` は dogfood 用ローカルツール。

**対応**: event log → OpenTelemetry / Langfuse エクスポーターの実装。P6の強みを外部から見えるようにする。

---

#### 9.3 Human-in-the-Loop (技術コスト: medium)

**Difyの状況**: v1.13 に Human Input Node が追加。ワークフローを一時停止して人間のレビューを待機し、承認/編集/リルートを受け付けてから再開できる。

**Reynの現状**: `ask_user` Control IR op は存在するが、長時間一時停止 (非同期 human review) とその後の再開には WAL + async dispatch の組み合わせが必要。現在 Phase 2.1 で async dispatch は実装済みだが、human review フローとしての統合は未整備。

**対応**: `ask_user` の非同期待機 + workspace への承認結果書き込み + forward-replay 再開のフルパス検証。

---

#### 9.4 Knowledge Base / RAG パイプライン (技術コスト: large)

**Difyの状況**: PDF/Word/HTML 等の文書取り込み→ベクトルDB→ハイブリッド検索→Reranking の一連パイプラインが UI から構築できる。`recall_docs` に相当する機能が製品として完成している。

**Reynの現状**: `recall_docs` は残タスクとして計画中。ベクトルDB選択・文書前処理・インデックス更新の設計が未着手。

**対応**: `recall_docs` stdlib skill の実装。ただしベクトルDB依存を workspace abstraction の外に出さないよう P5 準拠の設計が必要。

---

#### 9.5 Plugin / Tool マーケットプレイス (技術コスト: large)

**Difyの状況**: 120+ プラグインの Marketplace。コードレビュー・隔離実行・バージョン管理を含む governance 機構を持つ。

**Reynの現状**: Skill lookup (project > local > stdlib) の解決順序は定義済みだが、サードパーティ skill の配布・検索・インストール機構がない。

**対応**: Skill registry (最小限: GitHub URL + `reyn install <skill>` CLI) の設計。OSS化後に community skill が育つ前提でのロードマップとして位置づけ。

---

#### 9.6 Enterprise 機能 (技術コスト: large)

**Difyの状況**: SSO (SAML/OIDC/OAuth2), RBAC, MFA, マルチテナント, Admin API, 監査ログ分析 が Enterprise 版に揃い、CTC・Kakaku.com 等の国内大企業実績がある。

**Reynの現状**: P6 の event log は技術的には監査証跡として機能するが、SSO連携・RBAC・マルチテナント管理UI は未実装。

**対応**: pre-OSS フェーズでは不要だが、OSS リリース後の Phase 3 (release prep) に含めるべき。ADR として設計先行を推奨。日本エンタープライズ向けに「P6 event log + hash chain = immutable 監査証跡」の差別化ストーリーを構築する機会でもある。

---

### セグメント差の整理

| ターゲット | Dify | Reyn |
|-----------|------|------|
| 主要ユーザー | 非エンジニア (業務部門担当者) | エンジニア (システム開発者) |
| 構築スタイル | ノーコード/ローコード (GUI) | コード必須 (YAML + Python) |
| LLMの扱い | 生産性ツールとしての1部品 | 制御構造の中核意思決定エンジン |
| 予測可能性の担保方法 | 視覚的DAG + 確定的実行 | OS制約 + 全実行前バリデーション |
| 価値提案 | 「誰でも作れる」「すぐ動く」 | 「ガバナンス保証」「クラッシュしても再開」 |

Dify は「作るコストを下げる」、Reyn は「動かし続けるコストを下げる・ガバナンスを保証する」という軸で差別化できる。競合というよりも **補完関係** に近く、Dify で構築したアプリを Reyn のような厳格なワークフローエンジンに移行するユーザーが将来の獲得ターゲットになりうる。

---

## References

- [Dify Official Site](https://dify.ai/)
- [Dify Documentation](https://docs.dify.ai/)
- [GitHub - langgenius/dify](https://github.com/langgenius/dify)
- [Dify Enterprise](https://dify.ai/enterprise)
- [Dify Pricing](https://dify.ai/pricing)
- [Dify v1.0.0: Building a Vibrant Plugin Ecosystem](https://dify.ai/blog/dify-v1-0-building-a-vibrant-plugin-ecosystem)
- [Dify Agent Node Introduction](https://dify.ai/blog/dify-agent-node-introduction-when-workflows-learn-autonomous-reasoning)
- [Boost AI Workflow Resilience with Error Handling](https://dify.ai/blog/boost-ai-workflow-resilience-with-error-handling)
- [2025 Dify Summer Highlights](https://dify.ai/blog/2025-dify-summer-highlights)
- [Why A Reliable Visual Agentic Workflow Matters](https://dify.ai/blog/why-a-reliable-visual-agentic-workflow-matters)
- [Kakaku.com Case Study](https://dify.ai/blog/kakaku-accelerates-ai-adoption-with-dify-fast-secure-and-scalable)
- [CTC Press Release - Dify Enterprise Japan Launch](https://www.ctc-g.co.jp/en/company/release/20251024-02081.html)
- [Open Source AI Agent Platform Comparison 2026 (Jimmy Song)](https://jimmysong.io/blog/open-source-ai-agent-workflow-comparison/)
- [Dify Review 2025: Workflows, Agents & RAG (Skywork.ai)](https://skywork.ai/blog/dify-review-2025-workflows-agents-rag-ai-apps/)
- [Dify.ai 2025 Strategic Overview (BaytechConsulting)](https://www.baytechconsulting.com/blog/what-is-dify-ai-2025)
- [GitHub Issue #12083 - Checkpoint Restart (Closed as not planned)](https://github.com/langgenius/dify/issues/12083)
- [Dify Agent vs Workflow Differences](https://zediot.com/blog/dify-difference-between-agent-and-workflow/)
