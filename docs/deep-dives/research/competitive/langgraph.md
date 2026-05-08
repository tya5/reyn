---
title: LangGraph — 競合分析
last_updated: 2026-05-08
status: stable
sources:
  - url: https://docs.langchain.com/oss/python/langgraph/overview
    accessed: 2026-05-08
  - url: https://github.com/langchain-ai/langgraph
    accessed: 2026-05-08
  - url: https://changelog.langchain.com/announcements/langgraph-1-0-is-now-generally-available
    accessed: 2026-05-08
  - url: https://docs.langchain.com/oss/python/langgraph/persistence
    accessed: 2026-05-08
  - url: https://docs.langchain.com/oss/python/langgraph/workflows-agents
    accessed: 2026-05-08
  - url: https://www.langchain.com/pricing
    accessed: 2026-05-08
  - url: https://www.langchain.com/pricing-langgraph-platform
    accessed: 2026-05-08
  - url: https://www.langchain.com/blog/is-langgraph-used-in-production
    accessed: 2026-05-08
  - url: https://pypi.org/project/langgraph/
    accessed: 2026-05-08
---

# LangGraph — 競合分析

## TL;DR

LangGraph (LangChain Inc.) は Python/TypeScript で動く低レベルの **stateful agent オーケストレーションフレームワーク**。v1.1.10 (2026-04-27) が最新で MIT ライセンス。
Reyn との根本的な違いは 2 点: (1) **グラフ遷移の制御主体が LLM or スキル作者 (Reyn は OS が候補を提示し LLM はその中から選ぶ P4)**、(2) LangGraph は **汎用ツール**であり governance / 予測可能性より**柔軟性と生産性**を優先する設計哲学を持つ。

---

## 1. コアアーキテクチャ

### 全体スタック

```
Deep Agents (計画 + サブエージェント高レベル API)
    ↓
LangChain (モデル・ツール抽象)
    ↓
LangGraph (オーケストレーションランタイム) ← ここが競合対象
    ↓
LangSmith (可観測性・デプロイ・評価)
```

設計思想の出典は **Google Pregel** (super-step ベースのメッセージパッシング) および **Apache Beam**、グラフ API は **NetworkX** に倣う。

### グラフ実行モデル

- **StateGraph**: 型付きの共有状態 (state dict / TypedDict / Pydantic モデル) を持つ有向グラフ
- **Node**: Python 関数。state を受け取り、state の delta を返す。LLM 呼び出し、ツール実行、任意の Python コード何でも可
- **Edge**: 静的 (`add_edge`) と条件分岐 (`add_conditional_edges`) の 2 種類
- **Super-step**: 並列実行可能なノード群をひとまとめに実行する 1 tick。同一 super-step 内は並列、super-step をまたぐと逐次

### LLM の役割

LangGraph における LLM の役割は **ワークフロー型とエージェント型で異なる**:

| モード | LLM の役割 |
|--------|------------|
| Workflow (predetermined paths) | Executor — 特定ノードで決まったタスクを実行 |
| Router / Orchestrator | Decision engine — 次ノードを Literal enum の中から選択 |
| Agent (ReAct など) | Hybrid — ツールを呼ぶか・どれを呼ぶかを自律的に決定 |

**重要**: 条件付きエッジの遷移先は「Python ルーティング関数」が決定し、LLM はその入力となる structured output を返す。例えば `step: Literal["poem", "story", "joke"]` のような enum スキーマで LLM の選択肢を縛る。ただし `Command()` API を使うとノード関数内部で遷移先を動的に決定できるため、**LLM が実質的に任意のノード名を返せる**構成も可能 (**[Inferred]** 制約はスキル作者のコーディング規律に委ねられる)。

---

## 2. ワークフロー単位

### Reyn との対応関係

| Reyn 概念 | LangGraph 対応物 | 差異 |
|-----------|-----------------|------|
| Phase | Node | Phase は input_schema + instructions のみ宣言; Node は任意の Python コード |
| Skill (graph) | StateGraph | Skill はグラフ + final_output_schema を宣言; LangGraph はスキーマ宣言なし |
| OS (runtime) | LangGraph ランタイム自体 | Reyn OS は skill-agnostic (P7); LangGraph ランタイムはフレームワーク固有の概念を内包 |
| Control IR | State delta | Reyn は操作を宣言的 IR で記述し OS が実行; LangGraph ノードは命令的に直接実行 |
| Workspace | Checkpointer state | Reyn はファイルベース SSoT; LangGraph は DB (PostgreSQL/SQLite 等) |

### 主要プリミティブ

- **`create_react_agent`**: ツール呼び出し型 ReAct エージェントのプリビルド実装
- **`ToolNode`**: tool_calls を受け取り並列実行し ToolMessage を返す
- **`ValidationNode`**: Pydantic スキーマでツール呼び出しを検証
- **`tools_condition`**: tool_call があるかを判定してルーティングするプリビルド関数
- **`langgraph-supervisor`** (別ライブラリ): マルチエージェント supervisor パターンの実装

### サブグラフ / マルチエージェント

- 任意の StateGraph を別グラフのノードとして埋め込み可能 (subgraph)
- 親グラフと子グラフ間で state キーを共有するか変換するかを選択できる
- Supervisor エージェントが `handoff_tool` でワーカーエージェントへ委譲するパターンが標準的

---

## 3. 信頼性・回復力

### チェックポインター (Checkpointer) アーキテクチャ

| バックエンド | パッケージ | 用途 |
|-------------|-----------|------|
| InMemory | 標準同梱 | 開発・テスト |
| SQLite | `langgraph-checkpoint-sqlite` | ローカル・小規模 |
| PostgreSQL | `langgraph-checkpoint-postgres` | 本番推奨 |
| Azure Cosmos DB | `langchain-azure-cosmosdb` | Azure 環境 |

### クラッシュリカバリの仕組み

- 各 super-step 完了後に **state スナップショット** を checkpointer へ保存
- ノード失敗時: 成功済みノードの書き込みは `checkpoint_writes` テーブルに残り、再起動時には **失敗したノードとその下流のみ再実行** (成功済みノードは skip)
- `thread_id` を config に渡すことで同じスレッドの最終 checkpoint から resume
- **Time travel**: 過去の任意 checkpoint から replay 可能 (デバッグ用)

### Weak LLM 対応

LangGraph はフレームワークとして weak LLM への特別な対策機構を持たない。
対処法はスキル作者に委ねられる:
- `.with_structured_output()` + Pydantic スキーマによる schema-constrained generation
- 失敗時に self-correction ループを graph で実装 (validation node → retry edge)
- **[Inferred]** structured output 強制は推論エンジン (OpenAI / Gemini 等) 側の constrained decoding に依存しており、モデル選択に強く依存する

### Human-in-the-Loop

- `interrupt()` 関数: 任意ノード内で呼ぶと `GraphInterrupt` 例外を raise → executor がキャッチし state を serialize して checkpointer に保存
- `interrupt_before` / `interrupt_after`: compile 時または run 時に指定する static breakpoint
- 再開時の選択肢: approve (そのまま続行) / edit (state を修正して続行) / reject (フィードバック付き拒否) / respond (ask_user スタイル)
- 生産運用での推奨: 不可逆・高影響操作にのみ適用、TTL ベースで未再開スレッドを abandoned 扱い

---

## 4. Stdlib・標準装備

### コアパッケージ (`langgraph` 本体)

- StateGraph / MessageGraph
- InMemorySaver チェックポインター
- StreamPart 型安全ストリーミング (v2)
- GraphOutput 型安全 invoke (v2)
- Node/task level キャッシュ (2025-06 追加)
- DeltaChannel (beta, v1.2+): append-heavy チャンネルの差分保存

### プリビルドコンポーネント (`langgraph-prebuilt`)

- `create_react_agent`
- `ToolNode` (並列ツール実行)
- `ValidationNode` (Pydantic スキーマ検証)
- `tools_condition`

### 標準チェックポインター (`langgraph-checkpoint-*`)

- SQLite, PostgreSQL, AWS (Cosmos DB)

### LangChain エコシステムとの統合

LangGraph 単体でも動くが、LangChain の全統合 (OpenAI, Anthropic, Google, Bedrock, etc.) および LangChain Community の 600+ ツール・ローダー群を利用可能。
事実上の標準ライブラリは LangChain エコシステム全体 (**[Inferred]** 依存を取り込む判断はスキル作者が行う)。

### Reyn の stdlib との比較

Reyn の現在の stdlib: `skill_router`, `eval`, `skill_improver` の 3 本 + OS 組み込み Control IR ops (file read/write/edit/grep/glob、web_search、web_fetch、shell、mcp)。RAG・DB接続・翻訳・コード実行環境等のドメイン特化スキルは未実装。
LangGraph + LangChain の組み合わせは統合数・ツール数ともに圧倒的に多い。

---

## 5. Enterprise 機能

### 監査・可観測性

- **LangSmith**: トレース (LLM 呼び出し / ノード遷移 / state 変化を全て可視化)、評価、デバッグ、runtime metrics
- トレースは `langsmith.traceable` デコレータまたは LangChain callbacks で自動収集
- 14 日 (標準) / 400 日 (Extended, 追加課金) のトレース保持期間

### アクセス制御

- LangSmith: API キー認証
- LangGraph Platform: カスタム auth handler (OAuth, SAML 等) / RBAC (Enterprise プランのみ)
- Enterprise プランで self-hosted in VPC オプションあり → データが外部に出ない構成可能

### デプロイメント

| オプション | 対応プラン |
|-----------|-----------|
| Cloud SaaS | Plus, Enterprise |
| Hybrid (SaaS control plane + 自社 VPC data plane) | Enterprise のみ |
| Full self-hosted (Kubernetes / Docker) | Enterprise のみ |

### 暗号化

state の serialization に `EncryptedSerializer` (AES) を利用可能 (オプション)。

### 再現性

Time travel によりある checkpoint から再実行可能。ただし非決定論的 LLM 呼び出しが含まれるため完全再現は保証されない (**[Inferred]**)。

---

## 6. Ecosystem

### 規模 (2026-05-08 時点)

| 指標 | 数値 |
|------|------|
| GitHub Stars | 31,400+ |
| GitHub Forks | 5,300+ |
| GitHub Commits (main) | 6,825 |
| PyPI 最新版 | 1.1.10 (2026-04-27) |
| 月間ダウンロード | 約 3,450 万 (LangGraph 単体) |
| LangGraph Platform 採用企業数 | 約 400 社 |
| 言語サポート | Python, TypeScript (langgraphjs) |

### 採用企業例

- **Uber**: 大規模コードマイグレーション (ユニットテスト自動生成エージェント)
- **LinkedIn**: 採用候補者マッチング・ソーシング自動化
- **Klarna**: カスタマーサポート (8,500 万ユーザー、解決時間 80% 短縮)
- **AppFolio**: 物件管理コパイロット (応答精度 2x 向上、10 時間/週削減)
- **Elastic**: SecOps 脅威検知エージェント
- **JP Morgan, BlackRock, Cisco**: 採用事例あり (詳細非公開)

### 更新頻度

GitHub コミット頻度は非常に高い。langgraph-cli の最新リリースは 2026-05-07 (調査日の前日)。
公式ブログ・changelog も頻繁に更新されている。

### コミュニティ

- LangChain フォーラム, Discord, Twitter/X 公式アカウント
- 書籍 ("The Complete LangGraph Blueprint") や Udemy/DataCamp コース多数
- `awesome-LangGraph` キュレーションリスト、100+ チュートリアル記事

---

## 7. Pricing / License

### ライセンス

LangGraph 本体は **MIT License** (完全 OSS、商用利用可、改変・再配布可)。

### LangSmith / LangGraph Platform 料金

| プラン | 月額 | 主な制限 |
|--------|------|---------|
| Developer | 無料 + PAYG | 1 シート, トレース 5k/月, Fleet 50 runs/月 |
| Plus | $39/シート/月 + PAYG | トレース 10k/月, Fleet 500 runs/月, dev deployment 1 個無料 |
| Enterprise | カスタム | SSO/RBAC, hybrid/self-hosted, SLA, 専任サポート |

### 追加課金

- トレース: $2.50 / 1k (超過分), Extended ($5.00 / 1k)
- Deployment: dev $0.0007/分, prod $0.0036/分
- Fleet 超過: $0.05/run

### Self-hosted オプション

Developer プランで self-hosted 可 (月 10 万ノード実行まで無料)。
Enterprise のみ完全 self-hosted (データが自社 VPC から出ない)。

---

## 8. Reyn 対比

| 軸 | LangGraph | Reyn | 判定 |
|---|---|---|---|
| **LLM の役割** | executor / decision engine / hybrid (スキル作者が選択) | decision engine のみ (P4: OS が候補提示) | Reyn 優 (予測可能性) |
| **遷移制御** | Python ルーティング関数 + conditional edge (Command() で LLM が任意遷移も可能) | OS が候補遷移を提示 → LLM は列挙から選ぶ (P4 ハード制約) | Reyn 優 (ガバナンス) |
| **LLM 出力検証** | `.with_structured_output()` (モデル側 constrained decoding 依存) + スキル作者実装の self-correction ループ | OS が全 LLM 出力を JSON スキーマ検証してから実行 (Transition + Finish ルール) | Reyn 優 (OS レベル強制) |
| **データフロー** | Checkpointer (PostgreSQL / SQLite) 経由の状態管理; ノード間で直接 in-memory state 共有も可能 | workspace ファイルベース SSoT (P5); フェーズ間の in-memory 共有は禁止 | 同等 (設計哲学が異なる) |
| **監査** | LangSmith トレース (ノード遷移・LLM 呼び出しを可視化、14〜400 日保持) | event log append-only + replay 可能 (P6); LangSmith 相当の UI は未実装 | LangGraph 優 (UI・保持期間) |
| **クラッシュ回復** | Checkpointer: 成功済みノードをスキップして失敗ノードから再実行 | WAL + forward-replay (PR21); phase 単位で resume | 同等 |
| **OS 拡張性** | 新グラフ追加はコード追加のみ、ランタイム変更不要 | 新スキル追加は OS 変更不要 (P7) | 同等 |
| **Stdlib 充実度** | LangChain 統合 600+、ToolNode / ReAct / ValidationNode 等 | OS 組み込み ops (file/web/shell/mcp) + meta skill 3本。RAG・DB・翻訳等のドメインスキルなし | LangGraph 優 (大差) |
| **Weak LLM 対応** | フレームワーク非関与 (スキル作者の責任で retry ループを実装) | OS レベルの出力検証 + empty-stop attractor 問題あり (研究中) | 同等 (両者に課題あり) |
| **エコシステム** | 31k+ stars、400 社+ 採用、月 3,450 万 DL、Python + TypeScript | pre-OSS、stars 非公開、単一チーム | LangGraph 優 (圧倒的) |
| **日本語エンタープライズ適合** | 汎用設計、日本語 docs なし、RBAC/SSO は Enterprise のみ | 日本語 docs あり、予測可能性優先の設計哲学 | Reyn 優 (目標市場への特化) |
| **ライセンス** | MIT (OSS) | pre-OSS (未公開) | LangGraph 優 |

---

## 9. Reyn が追いつくために必要なこと

以下は LangGraph が解いていて Reyn が未着手または劣後している問題。
各項目に技術コスト (small = 1〜2 日 / medium = 1〜2 週 / large = 1 ヶ月+) を付記。

### 9-1. Stdlib の大幅拡充 [large]

LangGraph + LangChain の組み合わせが持つ実務ツール群 (DB 操作・RAG・翻訳・PDF 処理等) が Reyn には不足している。
OS 組み込み Control IR ops (file/web_search/web_fetch/shell) は実装済みだが、**ドメイン特化スキルとして提供する数が決定的に不足**している。
最低限必要なのは: recall_docs (RAG) / translate / http_call / code_exec の 4 スキル。

### 9-2. 可観測性 UI / トレース基盤 [large]

LangSmith は「ノード遷移・LLM 呼び出し・state 変化を全て可視化する Web UI」を提供する。
Reyn の `events/` log は append-only で正しい設計だが、**UI が存在しない**ため非エンジニアが見られない。
Event log を LangSmith 互換フォーマット (OpenTelemetry / OTEL) にエクスポートするアダプターだけでも medium コストで実現可能。

### 9-3. Python / TypeScript クライアント SDK の公開 [medium]

LangGraph は Python と TypeScript 両方の SDK を持ち、外部システムからエージェントを呼び出す API が整備されている。
Reyn は現状 CLI のみ。**MCP (Model Context Protocol) サーバー化** を Phase 2 のロードマップに持つが未着手。

### 9-4. PostgreSQL / DB バックエンドの Checkpointer [medium]

Reyn の WAL はファイルベースで、単一プロセス前提。
LangGraph は PostgreSQL / SQLite をバックエンドとしたスケーラブルな checkpointer を持ち、複数ワーカーが同一 thread_id を共有できる。
**長時間実行ジョブや複数インスタンス構成**には DB バックエンドが必要。

### 9-5. Time Travel デバッグ [medium]

LangGraph は任意の過去 checkpoint から replay できる。
Reyn の event log はリプレイ可能な設計 (P6) だが、**time travel デバッグの CLI / UI コマンドが未実装**。
Event log 構造は既にあるため実装コストは中程度。

### 9-6. Node/task キャッシュ [small]

LangGraph v2025-06 でノードレベルのキャッシュ (同一入力での重複計算スキップ) を追加。
Reyn は OS レベルのキャッシュなし。Phase 単位での idempotent replay はあるが、**キャッシュキーによる重複スキップ**は未実装。

### 9-7. マルチエージェント / サブグラフの正式サポート [large]

LangGraph の subgraph 機能と langgraph-supervisor は、**エージェントが別エージェントを呼び出す階層構造**を正式サポートする。
Reyn は `@sub_skill` graph node と `run_skill` Control IR op で設計上サポートするが、
**実装の成熟度とドキュメントが不十分**で、実際に複数の sub-skill を安定動作させた実績が少ない。

### 9-8. RBAC + SSO の標準化 [large]

LangGraph Platform Enterprise は OAuth / SAML SSO と RBAC (グラフ/アシスタント単位のアクセス制御) を持つ。
Reyn の permission model は phase/op 単位の制御を設計に持つが、**認証・組織管理レイヤーは未実装**。
日本エンタープライズ市場で最も要求される機能であり、優先度が高い。

---

## 10. 総評

LangGraph は「汎用、柔軟、エコシステム最大」の agent フレームワークとして事実上の業界標準に近い位置を占める。
Reyn の差別化点は「**OS レベルでの遷移制御・検証・監査の強制 (P4/P6)** + **日本エンタープライズ向け予測可能性優先の設計哲学**」にある。
この優位性は小規模・pre-OSS 段階の現在は主に設計思想上の優位だが、Stdlib 拡充と可観測性 UI の整備によって実用的な差別化に転換できる。

LangGraph のコアの限界: **遷移制御の安全性はスキル作者のコーディング規律に委ねられており、P4 相当の OS レベル制約は存在しない**。ガバナンスを厳しく求める組織 (金融、医療、公共) ではこの gap が Reyn の訴求ポイントになる。

---

## References

- [LangGraph Overview — docs.langchain.com](https://docs.langchain.com/oss/python/langgraph/overview)
- [LangGraph GitHub — langchain-ai/langgraph](https://github.com/langchain-ai/langgraph)
- [LangGraph 1.0 GA Announcement — changelog.langchain.com](https://changelog.langchain.com/announcements/langgraph-1-0-is-now-generally-available)
- [LangGraph Persistence — docs.langchain.com](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Workflows and Agents — docs.langchain.com](https://docs.langchain.com/oss/python/langgraph/workflows-agents)
- [LangSmith Pricing — langchain.com/pricing](https://www.langchain.com/pricing)
- [LangGraph Platform Pricing — langchain.com/pricing-langgraph-platform](https://www.langchain.com/pricing-langgraph-platform)
- [Is LangGraph Used in Production? — langchain.com/blog](https://www.langchain.com/blog/is-langgraph-used-in-production)
- [LangGraph PyPI — pypi.org/project/langgraph](https://pypi.org/project/langgraph/)
- [LangGraph Release Week Recap — blog.langchain.com](https://blog.langchain.com/langgraph-release-week-recap/)
- [LangSmith Deployment — langchain.com/langsmith/deployment](https://www.langchain.com/langsmith/deployment)
- [LangGraph AWS Marketplace — blog.langchain.com](https://blog.langchain.com/aws-marketplace-july-2025-announce/)
- [LangGraph 1.0 Released: Hard-Won Lessons — Medium](https://medium.com/@romerorico.hugo/langgraph-1-0-released-no-breaking-changes-all-the-hard-won-lessons-8939d500ca7c)
- [Do We Still Need LangGraph? — 2026-05 Research](https://atalupadhyay.wordpress.com/2026/05/04/do-we-still-need-langgraph-the-research-that-challenges-everything-we-know-about-ai-agents/)
