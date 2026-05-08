# Competitive Intelligence Index

競合 agent framework の横比較と個別分析の入口。

## 横比較テーブル

> 凡例: ★☆☆ = 薄い / ★★☆ = 中程度 / ★★★ = 豊富

### LLM の役割

| Framework | LLM の役割 |
|---|---|
| **LangGraph** | hybrid — Workflow モードは executor、Router/Orchestrator モードは decision engine (enum から選択)、Agent モードは hybrid。`Command()` API で LLM が任意ノードを返せる構成も可能 |
| **LangChain** | executor — `create_agent()` の agent loop で「どのツールを何回呼ぶか」を LLM が自律決定。次フェーズ候補を OS が絞り込む仕組みなし |
| **AutoGen** | executor / hybrid — `AssistantAgent` は自由にメッセージ生成。`SelectorGroupChat` では次の発言者を LLM が自由選択。候補制限なし |
| **CrewAI** | executor (高自律) — `Hierarchical Process` では manager_llm がタスク割当・バリデーション・再割当を自律判断。`max_iterations` デフォルト 20 回まで LLM 判断でループ |
| **Dify** | executor (constrained) — Workflow モードでは LLM は 1 ノードとして推論・生成のみ担当。遷移はユーザー定義 DAG が確定的に実行。Agent Node のみ ReAct/FC で自律判断 |
| **Reyn** | constrained decision engine のみ — OS が候補遷移・artifact・control_ir を提示し、LLM はその中からのみ選択 (P4)。OS が全出力を JSON スキーマ検証してから実行 |

### 遷移制御

| Framework | 遷移制御 |
|---|---|
| **LangGraph** | Python ルーティング関数 + conditional edge。`Command()` API でノード内部から動的遷移。制約はスキル作者のコーディング規律に委ねられる |
| **LangChain** | 制御なし (LangChain 単体)。LangGraph を使えば graph 定義可能。Tool 呼び出し回数・順序の上限は設計で別途実装が必要 |
| **AutoGen** | Termination Condition (11 種、AND/OR 組み合わせ) + チーム型選択 (RoundRobin / Selector / Swarm)。GraphFlow (experimental) で DAG 定義可能 |
| **CrewAI** | Sequential / Hierarchical Process + Flow イベント駆動 (`@start`/`@listen`/`@router`)。LLM が次行動を自由決定 |
| **Dify** | 視覚的エッジ定義 (DAG)。If/Else ノード・Question Classifier ノードで条件分岐。LLM は遷移に関与しない |
| **Reyn** | OS が許可済み遷移候補のみを LLM に提示 (P4)。`next_phase` は Skill graph 宣言内に限定。候補外遷移は OS が即 reject |

### データフロー

| Framework | データフロー |
|---|---|
| **LangGraph** | Checkpointer (PostgreSQL / SQLite) 経由の共有 state dict。ノード間で in-memory state 直接共有も可能 |
| **LangChain** | デフォルトはインメモリ。`RunnableWithMessageHistory` や LangGraph checkpointer を追加すれば永続化可能 |
| **AutoGen** | メモリ内オブジェクト受け渡し (会話スレッド)。`save_state()` / `load_state()` でオプション永続化 |
| **CrewAI** | インメモリ + コールバック + Flow ステート (SQLite `@persist`)。エージェント間はタスク output を直接受け渡し |
| **Dify** | 変数参照 (`{{node_name.output}}`)。in-memory + PostgreSQL DB 混在型。ファイルシステムベースのワークスペース概念なし |
| **Reyn** | workspace ファイルベース SSoT のみ (P5)。フェーズ間の in-memory 共有は原理的に禁止。Control IR 経由のみデータ操作可能 |

### 監査・event log

| Framework | 監査・event log |
|---|---|
| **LangGraph** | LangSmith トレース (ノード遷移・LLM 呼び出し・state 変化を可視化、14〜400 日保持)。replay はデバッグ用 time travel として実装 |
| **LangChain** | LangSmith トレーシング (SaaS、Enterprise プランで監査ログ保持)。フレームワーク単体は append-only 保証なし |
| **AutoGen** | OpenTelemetry 統合 (スパンとして出力、任意 OTel バックエンドにエクスポート可能)。append-only 保証・replay 機能なし |
| **CrewAI** | Flow `@persist` + AMP 実行ログ + OpenTelemetry。OSS 版は外部ツール (OpenLIT/Langfuse) 依存。AMP Enterprise は Immutable Audit Logs 提供 |
| **Dify** | 実行ログ・トレース (Langfuse 連携)。append-only 保証の公式明示なし。Enterprise は監査ログ分析機能あり |
| **Reyn** | event log append-only + replay-capable (P6)。OS がすべての状態変化を強制記録。hash chain (planned)。UI 未実装、外部エクスポート未整備 |

### クラッシュ回復

| Framework | クラッシュ回復 |
|---|---|
| **LangGraph** | Checkpointer: 各 super-step 完了後にスナップショット保存。失敗ノードとその下流のみ再実行 (成功済みノードはスキップ)。`thread_id` で同スレッド最終 checkpoint から resume |
| **LangChain** | LangGraph checkpointer に委任。LangChain core 単体では WAL 相当機能なし。純粋 LCEL Chain にはクラッシュ回復機能が存在しない |
| **AutoGen** | 組み込みチェックポイントなし。`save_state()` / `load_state()` はアプリケーション管理。GraphFlow に既知バグ (#7043)。ロードマップ issue #2358 で要望中 |
| **CrewAI** | `@persist` チェックポイント (SQLite)。ただし自動 resume なし (手動リカバリ必要)。単一プロセス前提。`task replay` は直近 1 run のみ。排他制御なし |
| **Dify** | **未実装**。GitHub Issue #12083 が "Closed as not planned" でクローズ。クラッシュ時は先頭から再実行が必要 |
| **Reyn** | WAL + forward-replay による自動クラッシュ回復 (ADR-0023 + PR21)。Phase 単位での resume。OS レベルで組み込み |

### Stdlib 充実度

| Framework | Stdlib 充実度 | 内容 |
|---|---|---|
| **LangGraph** | ★★★ | LangChain 統合 600+、ToolNode / ReAct / ValidationNode 等のプリビルドコンポーネント。月間 3,450 万 DL |
| **LangChain** | ★★★ | Vector Store 130+、LLM 100+、Document Loader 100+、Tool 50+。月間 2.37 億 DL |
| **AutoGen** | ★★☆ | AssistantAgent / CodeExecutorAgent / MCP 統合 / Docker 実行環境。AgentOps 連携 |
| **CrewAI** | ★★★ | 30+ 組み込みツール (Web 検索・PDF RAG・コード実行・DB・GitHub)、Knowledge RAG、UnifiedMemory、MCP/A2A |
| **Dify** | ★★★ | 50+ ツールプラグイン、Knowledge Base (PDF/Word/HTML)、RAG ハイブリッド検索、Code Node (Python/Node.js)、HTTP Request Node |
| **Reyn** | ★★☆ | **OS 組み込み Control IR ops**: file (read/write/edit/grep/glob/delete)・web_search (DuckDuckGo)・web_fetch・shell・ask_user・run_skill。stdlib skill は skill_router/eval/improver の 3 本のみ。RAG・DB 接続・コード実行環境・PDF 処理等のドメインスキルは未実装 |

### エコシステム規模

| Framework | GitHub Stars | 特記事項 |
|---|---|---|
| **LangGraph** | 31,400+ | 月間 3,450 万 DL、LangGraph Platform 採用約 400 社、Python + TypeScript |
| **LangChain** | 136,000+ | 月間 2.37 億 DL、Contributors 2,000+、業界最大規模エコシステム |
| **AutoGen** | 54,500+ | Microsoft Research バック。コミュニティが AG2 フォークと分裂。本体はメンテナンスモード移行 |
| **CrewAI** | 47,800+ | 月間 PyPI 5M+ DL、Fortune 500 企業の 60%+ 採用 (自社クレーム)、認定開発者 100,000+、$18M 調達 |
| **Dify** | 139,000+ | 180,000+ 開発者コミュニティ、日本に LangGenius K.K. 設立、CTC パートナーシップ |
| **Reyn** | 非公開 (pre-OSS) | 単一チーム開発。コミュニティ形成前 |

### エンタープライズ機能

| Framework | エンタープライズ機能 |
|---|---|
| **LangGraph** | LangSmith: RBAC/SSO (Enterprise)、self-hosted in VPC、AES 暗号化、カスタム auth (OAuth/SAML)。400 日トレース保持 (追加課金) |
| **LangChain** | LangSmith Enterprise: SSO + RBAC (2024-05 追加)、監査ログ、self-hosted/BYOC、PII redaction ミドルウェア |
| **AutoGen** | AutoGen v0.4 単体は弱い。Microsoft Agent Framework (後継): Entra ID 認証 + RBAC、Azure Monitor、SOC 2/HIPAA (Azure AI Foundry 経由) |
| **CrewAI** | AMP Enterprise: RBAC/SSO (MS Entra/Okta)、Immutable Audit Logs、HIPAA/SOC2/FedRAMP High、専用 VPC (AWS/Azure/GCP)。OSS はデフォルトテレメトリ **ON** |
| **Dify** | Enterprise: SSO (SAML/OIDC/OAuth2)、RBAC、MFA、マルチテナント管理、Admin API。Kakaku.com 全社 75% 登録・950 本アプリの実績。CTC 販売パートナーあり |
| **Reyn** | 設計レベルで P4/P5/P6 による予測可能性・監査証跡。SSO/RBAC/管理 UI は未実装。テレメトリゼロ (設計上のデフォルト) |

### ライセンス

| Framework | ライセンス | 備考 |
|---|---|---|
| **LangGraph** | MIT | フレームワーク本体。LangSmith/Platform は SaaS 有料 |
| **LangChain** | MIT | フレームワーク本体。LangSmith は SaaS 有料 (Enterprise 月 $2,000〜5,000+) |
| **AutoGen** | MIT | v0.4 本体。Azure AI Foundry 連携時は Azure 従量課金 |
| **CrewAI** | MIT | OSS コア。AMP (Enterprise SaaS) は別途有料。OSS デフォルトテレメトリ ON に注意 |
| **Dify** | Apache 2.0 + 追加条項 | マルチテナント SaaS 再配布には LangGenius 書面許可が必要。内部利用は実質自由 |
| **Reyn** | 未定 (pre-OSS) | OSS 化予定。ライセンス決定は Phase 3 (release prep) |

### Reyn 対比総評

| Framework | Reyn 対比総評 |
|---|---|
| **LangGraph** | 汎用・柔軟・最大エコシステム。遷移制御の安全性はスキル作者のコーディング規律に委ねられる。P4 相当の OS レベル制約なし。ガバナンス厳格要件の組織では Reyn の設計思想が訴求ポイントになる |
| **LangChain** | エコシステム最大 (月 2.37 億 DL)。LLM が自律的に tool 呼び出しを決定し OS レベルの制約なし。State of Agent Engineering 調査で「本番未導入 45%」が示す通り、予測可能性の課題が実業務で顕在化している |
| **AutoGen** | 会話駆動マルチエージェントが強み。OS レベルの出力バリデーションなし。組み込みクラッシュ回復なし。本体がメンテナンスモードで後継 (Microsoft Agent Framework) に移行中。Azure 環境依存が深まる方向 |
| **CrewAI** | role-based な自律エージェントが豊富な stdlib・RAG・Memory と組み合わさり実用性が高い。ただし OSS デフォルトテレメトリ ON・候補制限なし・手動クラッシュ回復という 3 点が日本企業の情報漏洩リスク審査で障壁になりうる |
| **Dify** | ノーコード・即戦力 stdlib・日本市場実績 (Kakaku.com/CTC) で最も日本市場に浸透している競合。Workflow モードは確定的だが LLM 出力の OS レベル強制バリデーションなし。クラッシュ回復は "Closed as not planned"。ガバナンス最優先のエンジニア向けには Reyn の設計が優位 |

---

## 個別分析ファイル

| Framework | ファイル | last_updated |
|---|---|---|
| LangChain | [langchain.md](langchain.md) | 2026-05-08 |
| LangGraph | [langgraph.md](langgraph.md) | 2026-05-08 |
| CrewAI | [crewai.md](crewai.md) | 2026-05-08 |
| AutoGen | [autogen.md](autogen.md) | 2026-05-08 |
| Dify | [dify.md](dify.md) | 2026-05-08 |
| Semantic Kernel | [semantic-kernel.md](semantic-kernel.md) | 2026-05-09 |

## 更新方針

- 各ファイルは随時更新型（`last_updated:` frontmatter で管理）
- Major release / アーキテクチャ変化があれば当該ファイルを更新し、この index の `last_updated` 列も更新
- 調査中メモは `tmp/research/competitive/` に置いてから昇格
