---
title: LangChain — 競合分析
last_updated: 2026-05-08
status: stable
sources:
  - url: https://docs.langchain.com/oss/python/langchain/overview
    accessed: 2026-05-08
  - url: https://changelog.langchain.com/announcements/langchain-1-0-now-generally-available
    accessed: 2026-05-08
  - url: https://changelog.langchain.com/announcements/langchain-1-1
    accessed: 2026-05-08
  - url: https://www.langchain.com/pricing
    accessed: 2026-05-08
  - url: https://www.langchain.com/langsmith
    accessed: 2026-05-08
  - url: https://github.com/langchain-ai/langchain
    accessed: 2026-05-08
  - url: https://pypi.org/project/langchain/
    accessed: 2026-05-08
  - url: https://docs.langchain.com/oss/python/integrations/vectorstores
    accessed: 2026-05-08
  - url: https://community.latenode.com/t/current-limitations-of-langchain-and-langgraph-frameworks-in-2025/30994
    accessed: 2026-05-08
  - url: https://octoclaw.ai/blog/why-we-no-longer-use-langchain-for-building-our-ai-agents
    accessed: 2026-05-08
  - url: https://www.langchain.com/state-of-agent-engineering
    accessed: 2026-05-08
  - url: https://deepwiki.com/langchain-ai/langchain/2.2-runnable-interface-and-lcel
    accessed: 2026-05-08
---

# LangChain — 競合分析

## TL;DR

LangChain は「LLMを呼び出す処理を composable なパイプとして組み上げる」開発者体験フレームワークであり、v1.0（2025年10月 GA）以降は LangGraph ランタイム上に構築された `create_agent()` を中心軸に置く。Reyn との根本的な違いは制御モデルにある: LangChain では LLM が実行チェーンを自由に歩き回り次のツール呼び出しを自ら決定するが、Reyn は OS が候補遷移を事前に絞り込み LLM は選択のみ行う (P4)。エコシステム規模 (GitHub 136K stars、月間 PyPI 2.37億 DL) と Stdlib の豊富さで LangChain が圧倒するが、予測可能性・監査証跡・厳格なガバナンスでは Reyn の設計思想が優位に立てる余地がある。

---

## 1. コアアーキテクチャ

### パッケージ構成 (v1.0 時点)

| パッケージ | 役割 |
|---|---|
| `langchain-core` | Runnable インターフェース、LCEL、基底クラス群 |
| `langchain` | `create_agent()` を含む高レベル API、ミドルウェアシステム |
| `langchain-community` | コミュニティ管理の統合 (Vector Store、Document Loader 等) |
| `langchain-[partner]` | OpenAI / Anthropic / Google 等のプロバイダー別パッケージ |
| `langchain-classic` | v0.x の旧 API 後方互換レイヤー (非推奨) |
| `langgraph` | 低レベルグラフ型オーケストレーションランタイム |

### LCEL (LangChain Expression Language)

LCEL は `|` (pipe 演算子) で Runnable コンポーネントを直列に接続する宣言型 DSL。

```python
chain = prompt | llm | StrOutputParser()
result = chain.invoke({"input": "..."})
```

すべての Runnable は共通インターフェースを持つ:
- **invoke / ainvoke** — 同期/非同期実行
- **stream / astream** — ストリーミング出力
- **batch / abatch** — バッチ処理
- **with_retry(stop_after_attempt=N)** — 指数バックオフ付きリトライ
- **with_fallbacks([fallback_chain])** — 失敗時フォールバック
- **configurable_fields()** — 実行時パラメータ動的変更
- **with_listeners()** — start / end / error ライフサイクルフック

### LLM の役割

LangChain における LLM は **自律的な意思決定エージェント**。Agent loop では「どのツールを何回呼ぶか」を LLM 自身が決定し、OS (= LangGraph ランタイム) はその決定を実行するだけ。次フェーズの候補を OS が絞り込む仕組みは存在しない。

### v1.0 の主要変更点 (2025年10月 GA)

- `create_agent()` を正式 API として確立 (LangGraph ランタイム上で動作)
- ミドルウェアシステム導入: human-in-the-loop / PII redaction / 要約 / リトライ を hook 化
- 旧 LLMChain / AgentExecutor を `langchain-classic` に分離
- Python 3.10+ 必須 (3.9 サポート終了)
- 2.0 までの破壊的変更なし を公約

### v1.1 の追加 (2025年末〜2026年)

- **Model Profiles**: `.profile` 属性でモデル機能 (structured output 対応可否等) を自動取得
- **Model-Retry Middleware**: 設定可能な指数バックオフ
- **Content Moderation Middleware**: OpenAI モデレーション API を agent loop 全体に適用

---

## 2. ワークフロー単位

### Chain

LCEL で定義した Runnable の直列/並列パイプライン。Reyn の Phase に相当するが、以下の点で異なる:

- **状態管理**: Chain はデフォルトでステートレス。状態を持たせるには `RunnableWithMessageHistory` や LangGraph のチェックポインタを使う
- **スキーマ強制**: 入力/出力スキーマは Pydantic で定義できるが、LangChain が実行時に検証する仕組みはオプション
- **遷移管理**: Chain 内の分岐ロジックは開発者がコードで書く (`RunnableBranch` 等)

### Agent (`create_agent`)

v1.0 以降の正式 API。内部は LangGraph の graph ノード (model ノード → tools ノード → middleware ノード) として実装されており、ループ継続/終了は LLM が tool_call の有無で決定する。

Reyn との対比:
- Reyn の Skill が「許可された遷移グラフ」を宣言し OS が LLM に候補を提示するのに対し、LangChain Agent は LLM にツール一覧を渡すだけで「どのツールを何回呼ぶか」の制限を原理的に持たない
- **[Inferred]** これは柔軟性を高める代わりに、本番環境での予測可能性・コスト上限制御が難しくなることを意味する

### LangGraph との関係

2026年時点では LangChain と LangGraph は「異なる抽象レベルの同一スタック」として位置づけられている:
- **LangChain** = 迅速な agent 構築のための高レベル API
- **LangGraph** = 高度にカスタマイズ可能な低レベルオーケストレーション

本分析は LangChain core (= `create_agent()` API) を対象とし、LangGraph の詳細は別途分析する。

---

## 3. 信頼性・回復力

### Weak LLM 対応

LangChain v1.1 で追加されたミドルウェア:
- **Model-Retry Middleware**: プロバイダー障害時に設定可能な指数バックオフでリトライ
- `with_retry()` / `with_fallbacks()`: Runnable レベルで宣言的に設定可能
- **Model Profiles** (v1.1): モデルの structured output 対応可否を自動検出し、非対応モデルへのフォールバック戦略を動的生成

ただし、LLM が empty stop / 誤形式出力を生成したときのリカバリは **開発者が Chain 設計で対応する責務**。OS レベルで出力を検証・拒絶・再試行する仕組みはビルトインされていない。

### 状態管理・クラッシュ回復

LangChain core の Chain はステートレスで、クラッシュ回復機能は **LangGraph のチェックポインタに委任**:
- `SqliteSaver` / `PostgresSaver` で各 superstep にスナップショット保存
- ノード失敗時は最後に成功した superstep から resume 可能
- 2025年10月に AWS Bedrock Session Management Service 連携チェックポインタも追加

LangChain core 単体では WAL 相当機能は持たない。LangGraph を使わない純粋な LCEL Chain にはクラッシュ回復機能が存在しない。

### エラー処理

- Runnable レベルの `with_retry` / `with_fallbacks` は直感的だが、**チェーン全体の atomic recovery はない**
- 長い Chain の途中でクラッシュした場合、どこまで実行されたかの追跡は LangSmith のトレーシングに依存
- 本番障害の根本原因特定が困難という批判が開発者コミュニティで頻出している

---

## 4. Stdlib・標準装備

LangChain の最大の強みはエコシステムの豊富さ。

### ビルトイン統合数

| カテゴリ | 数 |
|---|---|
| Vector Store | 130+ |
| LLM / Chat モデル | 100+ |
| Document Loader | 100+ |
| Embedding モデル | 50+ |
| Tool / Toolkit | 50+ (Web検索、SQLDb、Python REPL等) |
| Memory | 複数種 (ConversationBuffer, Summary, VectorStore-backed等) |

### 主要な組み込みツール

- **Retrieval / RAG**: 全主要ベクトルDB対応 (Pinecone, Weaviate, Qdrant, Chroma, PGVector, MongoDB Atlas 等)
- **Document処理**: PDF, HTML, CSV, JSON, Markdown, Office 形式等のローダー
- **外部 API**: Wikipedia, DuckDuckGo, Tavily, Wolfram Alpha, Gmail, Calendar 等
- **コード実行**: Python REPL, Bash, Docker
- **データベース**: SQL (SQLAlchemy), Spark
- **Human-in-the-loop**: ミドルウェアとして標準搭載 (v1.0)

### Stdlib の設計思想

Reyn が「OS は skill-agnostic (P7)」を鉄則とするのに対し、LangChain はドメイン固有の機能を `langchain-community` や partner パッケージとして **積極的に取り込む** 設計。これがエコシステムの厚みを生む反面、依存関係の肥大化・破壊的変更のリスクにも繋がっている。

---

## 5. Enterprise 機能

### LangSmith (observability プラットフォーム)

LangSmith は LangChain の monitoring / evaluation / deployment 管理基盤で、OSS フレームワークとは別製品。

**主要機能:**
- **トレーシング**: agent loop の各ステップを可視化。token 使用量・レイテンシ・エラー率のリアルタイムダッシュボード
- **評価 (Evals)**: LLM-as-judge + コード eval をオフライン/オンライン両方で実行
- **Annotation Queue**: ヒューマンレビューの workflow 管理
- **Prompt Hub**: プロンプトのバージョン管理・共有
- **Dataset 管理**: テストセット作成・管理
- **Fleet (Deployment)**: agent のデプロイ管理 (Plus: 1 dev デプロイ無料)

**Enterprise 固有機能:**
- Self-hosted / BYOC (AWS, GCP, Azure) デプロイ
- SSO + **RBAC** (カスタムロール・細粒度パーミッション、2024年5月追加)
- 監査ログ (Enterprise プランのみ)
- カスタム SLA・専任エンジニアサポート
- データ保持ポリシーのカスタマイズ (400日延長オプション)
- VPC 内でデータが出ない保証

### Agent-level のガバナンス機能

LangChain v1.0 のミドルウェアシステムで実現:
- **PII redaction**: 入力/出力の PII 自動マスク
- **Content Moderation**: OpenAI モデレーション API との連携
- **Human-in-the-loop**: ツール実行前に人間承認を要求するフック

ただし、Reyn のような「OS レベルの LLM 出力検証・Control IR 実行ゲート」は存在しない。ガバナンスは **ミドルウェアのコード設計に依存**し、middleware を書かなければ素通りする。

---

## 6. Ecosystem

### GitHub / コミュニティ規模

| 指標 | 数値 |
|---|---|
| GitHub Stars (langchain) | **136,000+** |
| GitHub Forks | 22,500+ |
| 総 Commits | 15,880+ |
| 直近オープン Issues | 397 |
| 最新バージョン | 1.2.17 (2026年4月30日) / 1.3.0a2 (2026年5月6日) |
| langchain-core 最新 | 0.3.86 (2026年5月7日) |
| Contributors (2023実績) | 2,000人超 |

### PyPI ダウンロード (2025〜2026)

| 期間 | ダウンロード数 |
|---|---|
| 月次 | 237,401,999 |
| 週次 | 55,303,563 |
| 日次 | 8,372,158 |

### 関連パッケージ / ツール

- **LangGraph**: 136K stars (langchain 本体と同等規模)
- **LangSmith**: SaaS 監視プラットフォーム
- **LangGraph Studio**: visual デバッグ UI
- **langchain-community**: 2,000+ integration コード
- 公式 TypeScript 版も並行開発中

### 更新頻度

2026年5月時点で週に複数のパッチリリースが継続しており、アクティブな開発が維持されている。

---

## 7. Pricing / License

### LangChain フレームワーク本体

**MIT License** — 商用・私的利用ともに完全無料。

### LangSmith 料金体系 (2026年5月時点)

| プラン | 月額 | 主な制限 |
|---|---|---|
| Developer | **$0** | 5K traces/月、1 seat、1 Fleet agent |
| Plus | **$39/seat** | 10K traces/月 (超過 $2.50/1K)、3 workspaces |
| Enterprise | **カスタム** | 無制限 seats/workspaces、RBAC、SSO、監査ログ、self-hosted |

**Enterprise 実績コスト**: 中規模チームで月 $2,000〜5,000 程度から、年間前払い。

**トレース保持**: ベーストレース 14 日 / 拡張 400 日 ($5.00/1K traces)。

**注意点**: LangChain フレームワーク自体は無料だが、本番運用で LangSmith を使うと SaaS コストが発生する。self-hosted は Enterprise プランのみ。

---

## 8. Reyn 対比

| 軸 | LangChain | Reyn | 判定 |
|---|---|---|---|
| **LLMの役割** | 自律的な意思決定エージェント (ツール選択・ループ継続を自ら判断) | decision engine (P4) — OS が候補を提示し LLM は選択のみ | 設計思想が根本的に異なる。予測可能性: **Reyn優** |
| **遷移制御** | 制御なし (LLM が自由にツール呼び出し) / LangGraph を使えば graph 定義可能 | OS候補提示 → LLM選択 (P4) + Skill graph 強制 | **Reyn優** (LangChain単体では制御できない) |
| **データフロー** | デフォルトはインメモリ。LangGraph チェックポインタを追加すれば永続化可能 | workspace 経由のみ (P5)。インメモリ渡しを原理的に禁止 | **Reyn優** (監査性・クラッシュ回復の基盤として) |
| **LLM出力検証** | なし (Pydantic 型ヒント + `with_structured_output` はあるが OS レベルの reject/retry なし) | Control IR + artifact を完全検証してから実行 | **Reyn優** |
| **監査証跡** | LangSmith トレーシング (SaaS、Enterprise プランでログ保持) | event log append-only (P6)、OSS、ローカル完結 | ガバナンス要件次第。LangSmith は成熟、Reyn は no SaaS 依存: **条件付き同等** |
| **skill追加** | 新 Tool / Chain は Python コードを書くだけ。ただし framework 依存の API 設計が必要 | OS変更不要 (P7) — skill.md を追加するだけ | **同等** (どちらもコード追加なし〜少で拡張可能) |
| **クラッシュ回復** | LangGraph チェックポインタ (SQLite/Postgres) — LangChain単体にはない | WAL + forward-replay ビルトイン | LangGraph 込みなら同等水準。LangChain 単体は: **Reyn優** |
| **Stdlib充実度** | 圧倒的: 130+ vector stores、200+ 統合、2,000+ コミュニティ integration | OS 組み込み ops (file/web/shell/mcp) + meta skill 3本。RAG・DB・翻訳等のドメインスキルなし | **LangChain優** (大差) |
| **エコシステム** | 136K GitHub stars、月間 2.37億 DL、2,000人超 contributors | pre-OSS、コミュニティなし | **LangChain優** (大差) |
| **ガバナンス・Enterprise** | LangSmith (RBAC/SSO/監査ログ) は Enterprise プランで利用可能 | 設計レベルでの予測可能性 (P4/P5/P6) が強み。管理 UI なし | LangSmith 機能は: **LangChain優**。設計の予測可能性: **Reyn優** |
| **学習コスト** | LCEL/Runnable の習得が必要。抽象レイヤーが多く内部デバッグが難しい | Phase/Skill/OS の3層設計 + skill.md 記法の習得が必要 | **同等** (どちらも独自概念の習得が必要) |
| **ライセンス** | MIT (フレームワーク) + LangSmith は SaaS 有料 | **[Inferred]** OSS 化前提で設計 | **Reyn優** (将来 SaaS 依存なし) |

---

## 9. Reynが追いつくために必要なこと

LangChain が解決していて Reyn が未着手の問題を、技術コスト付きで列挙する。

### 9-1. Stdlib の拡充 — **HIGH 優先度**

**技術コスト: medium〜large**

LangChain の差が最も大きい領域。具体的なギャップ:
- ファイル I/O スキル (読み書き、PDF 解析、CSV 処理) — 各 skill: **small**
- Web 検索・スクレイピングスキル — **small**
- SQL / DB アクセススキル — **small〜medium**
- RAG スキル (Document Loader + Embedding + VectorStore 一体型) — **medium**
- 翻訳・テキスト変換スキル — **small**
- コード実行スキル (Python sandbox) — **medium**

各スキルの実装自体はシンプルだが、スキル数を揃えるには継続的なコスト。OSS 化後にコミュニティ開発を促すか否かが LangChain との差を決定する。

### 9-2. Observability / Tracing UI — **HIGH 優先度**

**技術コスト: large**

LangSmith 相当の可観測性プラットフォームが Reyn にはない。P6 の event log は基盤として存在するが、UI・検索・可視化レイヤーが未実装。

現実的な戦略:
1. event log を OpenTelemetry フォーマットでエクスポート → Grafana / Jaeger 等で可視化 (**medium**)
2. LangSmith 互換の SDK トレース送信ラッパーを実装し LangSmith 自体に乗る (**small** — ただし SaaS 依存)
3. Reyn 独自のトレース UI を実装 (**large**)

### 9-3. Human-in-the-loop の標準化 — **MEDIUM 優先度**

**技術コスト: medium**

LangChain v1.0 はミドルウェアとして標準搭載。Reyn の Control IR `ask_user` は存在するが、承認フロー・レビューキュー・UI との統合が未整備。

### 9-4. Structured Output の堅牢化 — **MEDIUM 優先度**

**技術コスト: small〜medium**

LangChain は Model Profiles (v1.1) でモデルの structured output 対応可否を自動判定し、非対応モデルへのフォールバック戦略を動的生成する。Reyn は weak LLM (gemini-2.5-flash-lite) の empty-stop attractor に悩んでおり、OS レベルでの出力検証 + 自動リトライ機構の強化が必要。

### 9-5. マルチモデルルーティング — **LOW 優先度**

**技術コスト: medium**

LangChain はプロバイダー非依存の統一 API を持ち、コスト・品質・レイテンシに応じてモデルを動的切り替えできる。Reyn は現状単一モデル固定。Model Profiles 相当の機能追加により、タスク特性に応じた自動ルーティングが可能になる。

### 9-6. TypeScript / 多言語 SDK — **LOW 優先度**

**技術コスト: large**

LangChain は TypeScript 版を公式サポートしており、フロントエンド統合や Deno/Node.js エコシステムとの連携が容易。Reyn は Python 専用。OSS 化フェーズでの対応が現実的。

### 9-7. ドキュメント・オンボーディング — **MEDIUM 優先度**

**技術コスト: medium**

LangChain の最大の批判の一つが「ドキュメントの陳腐化」と「デバッグの困難さ」であり、Reyn が差別化できる領域でもある。Phase/Skill/OS の明確な境界と P1〜P8 原則を丁寧に文書化し、LangChain より "why" を説明できる設計ドキュメントを先行させることが採用障壁の低減につながる。

---

## References

- [LangChain Docs — Overview](https://docs.langchain.com/oss/python/langchain/overview)
- [LangChain 1.0 GA Announcement](https://changelog.langchain.com/announcements/langchain-1-0-now-generally-available)
- [LangChain 1.1 Changelog](https://changelog.langchain.com/announcements/langchain-1-1)
- [LangChain & LangGraph v1.0 Blog Post](https://www.langchain.com/blog/langchain-langgraph-1dot0)
- [LangSmith Pricing](https://www.langchain.com/pricing)
- [LangSmith Features](https://www.langchain.com/langsmith)
- [LangSmith RBAC Announcement](https://changelog.langchain.com/announcements/role-based-access-control-rbac-is-now-available-for-enterprise-customers)
- [LangChain GitHub Repository](https://github.com/langchain-ai/langchain)
- [langchain on PyPI](https://pypi.org/project/langchain/)
- [Vector Store Integrations](https://docs.langchain.com/oss/python/integrations/vectorstores)
- [Runnable Interface & LCEL (DeepWiki)](https://deepwiki.com/langchain-ai/langchain/2.2-runnable-interface-and-lcel)
- [Why We No Longer Use LangChain (OctoClaw)](https://octoclaw.ai/blog/why-we-no-longer-use-langchain-for-building-our-ai-agents)
- [State of Agent Engineering 2025 (LangChain)](https://www.langchain.com/state-of-agent-engineering)
- [Current Limitations of LangChain 2025 (Latenode Community)](https://community.latenode.com/t/current-limitations-of-langchain-and-langgraph-frameworks-in-2025/30994)
- [LangChain 1.0 vs LangGraph 1.0 (ClickIT)](https://www.clickittech.com/ai/langchain-1-0-vs-langgraph-1-0/)
- [LangChain Limitations 2025 Review (Sider.ai — 403 access blocked, data from search snippet)](https://sider.ai/blog/ai-tools/is-langchain-still-worth-it-a-2025-review-of-features-limits-and-real-world-fit)
