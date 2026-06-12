---
title: Reyn の差別化・優位性
last_updated: 2026-06-13
status: stable
---

# Reyn の差別化・優位性

## 核心テーゼ

> **Predictability over autonomy** — 日本の高制約エンタープライズ向けに、
> LLM の autonomy を OS レベルで constrain し、再現性・監査可能性・説明責任を確保する。

---

## 1. 競合との根本的な違い

| 軸 | 一般的な agent framework | Reyn |
|---|---|---|
| LLM の役割 | executor（行動する） | constrained decision engine（決める、動かすのは OS） |
| 遷移制御 | LLM or code で任意に遷移 | OS が候補を提示、LLM が選ぶ（P4） |
| データフロー | in-memory / arbitrary pass | workspace 経由のみ（P5） |
| 監査 | なし / 任意 / SaaS 依存 | event log が append-only、replay 可能（P6）|
| Skill 追加 | framework 改修を伴うことがある | OS 変更不要（P7） |
| 出力形式 | 自由 | `{control, artifact, control_ir}` の schema validate 必須 |

---

## 2. "NEVER ルール" がそのまま差別化になる根拠

各 NEVER ルールと、それを守らない競合で実際に発生した問題を対応させる。

### NEVER allow LLM to choose arbitrary next phase (P4)

**違反している競合と問題:**

- **LangGraph**: `Command()` API を使うと LLM がノード関数内部で次遷移先を動的に決定できる。制約はスキル作者のコーディング規律に委ねられており、OS レベルの強制機構はない。(→ langgraph.md §1)
- **LangChain**: `create_agent()` では「どのツールを何回呼ぶか」を LLM が自律決定。次フェーズ候補を OS が絞り込む仕組みが原理的に存在しない。(→ langchain.md §1)
- **AutoGen**: `SelectorGroupChat` では次の発言者を LLM が自由選択する。候補制限機構なし。(→ autogen.md §1)
- **CrewAI**: Hierarchical Process で manager_llm が動的タスク割当・バリデーション・再割当を自律判断。`max_iterations` デフォルト 20 回まで LLM 判断でループが継続しうる。(→ crewai.md §1)

**Reyn の優位性:** OS が `next_phase` を Skill graph 宣言内の許可済み候補に限定し、候補外遷移は即 reject する (P4)。遷移ログが完全になり、「なぜこのフェーズに来たか」を常に説明できる。

---

### NEVER pass data between phases outside the workspace (P5)

**違反している競合と問題:**

- **LangChain**: デフォルトはインメモリ。Chain が途中でクラッシュしても「どこまで実行されたか」の追跡が LangSmith トレーシングに依存する。LangChain 単体には WAL 相当機能がない。(→ langchain.md §3)
- **AutoGen**: 会話スレッドはメモリ内オブジェクト受け渡しが基本。`save_state()` / `load_state()` はアプリケーション管理であり自動チェックポイントではない。GraphFlow では状態破損の既知バグ (#7043) がある。(→ autogen.md §3)
- **CrewAI**: エージェント間のタスク output は直接受け渡し。`@persist` は SQLite スナップショットだが自動 resume なし、排他制御なし、直近 1 run しか保持しない。(→ crewai.md §3)
- **Dify**: ノード間データは変数参照 (`{{node.output}}`) + in-memory/PostgreSQL 混在型。ワークスペース概念がなく、クラッシュ回復機能の実装が "Closed as not planned" で拒否されている。(→ dify.md §3)

**Reyn の優位性:** すべてのデータが workspace を通過するため (P5)、クラッシュ後の WAL + forward-replay が成立する。「フェーズ X まで完了した」事実がファイルシステムに残り、再実行時に安全にスキップできる。

---

### NEVER mutate runtime state without emitting an event (P6)

**違反している競合と問題:**

- **LangGraph**: LangSmith トレース (SaaS) が可観測性の中心。フレームワーク内部では append-only event log の保証がない。LangSmith なしでは何が起きたかを事後追跡しにくい。(→ langgraph.md §5)
- **LangChain**: LangSmith Enterprise でのみ監査ログ保持。フレームワーク単体は append-only 保証なし。State of Agent Engineering 調査で「本番未導入 45%」— 予測可能性の懸念が顕在化している根拠の一つ。(→ langchain.md §5, §8)
- **AutoGen**: OpenTelemetry でスパン出力するが append-only 保証・replay 機能なし。GraphFlow の状態破損バグはログ不完全が原因特定を困難にする。(→ autogen.md §5)
- **CrewAI**: OSS 版は外部ツール (OpenLIT/Langfuse) 依存。AMP Enterprise (有償) が Immutable Audit Logs を提供するが OSS では保証されない。(→ crewai.md §5)
- **Dify**: 実行ログは Langfuse 連携で取得するが、append-only 保証の公式明示なし。Enterprise の監査ログ分析は機能として存在するが設計上の強制力が不明。(→ dify.md §5)

**Reyn の優位性:** OS がすべての状態変化を強制的に event log に記録する (P6)。SaaS 契約なし・追加設定なしで append-only な監査証跡が常に生成される。日本企業のガバナンス部門に「イベントログを見せろ」と言われたとき、Reyn は即座に応答できる。

---

### NEVER put skill-specific strings in OS code (P7)

**違反している競合と問題:**

- **LangGraph**: ランタイムはフレームワーク固有の概念 (super-step、node type 等) を内包する。新しいスキル固有のルーティング条件を追加するとき、スキル実装にフレームワーク固有の API を呼ぶコードが混入しやすい。(→ langgraph.md §1)
- **CrewAI**: Engine (Crew/Flow 実行ロジック) はエージェントロール名・process type 等を内部的に参照する設計。OS に相当するランタイムがスキル固有概念から分離されていない。(→ crewai.md §1)

**Reyn の優位性:** OS に skill-specific 文字列がゼロであることを検出ルールとして定義している (CLAUDE.md P7)。新しいスキルは `skill.md` を追加するだけで OS 変更不要。スキル作者が OS の内部を知る必要がない。

---

### NEVER allow LLM output without full validation

**違反している競合と問題:**

- **LangGraph**: `.with_structured_output()` を使うが、これはモデル側 constrained decoding に依存する。モデル選択に強く依存し、OS レベルの強制ではない。self-correction ループはスキル作者が graph で実装する責務。(→ langgraph.md §3)
- **LangChain**: Pydantic 型ヒント + `with_structured_output()` はあるが OS レベルの reject/retry なし。ガバナンスはミドルウェアのコード設計に依存し、「middleware を書かなければ素通りする」。(→ langchain.md §3, §5)
- **AutoGen**: 型ヒントのみ。`control` ブロック相当の構造・スキーマ検証なし (実行時の JSON スキーマ検証は組み込まれていない)。(→ autogen.md §1)
- **CrewAI**: Pydantic response_format で構造化するが、Weak LLM (GPT-4o-mini 以下相当) で不安定事例あり。structured output が一貫して機能しない Community 報告。(→ crewai.md §3)
- **Dify**: JSON Schema Editor はオプト・イン。バリデーション失敗は実行時エラーとして扱われる (実行拒否ではない)。(→ dify.md §3)

**Reyn の優位性:** `{control, artifact, control_ir}` を毎回 JSON スキーマ検証し、violation は実行拒否 (REJECTED)。Transition ルール・Finish ルールの両方が OS レベルで強制される。不正出力は実行前に捕捉できる。

---

## 3. "Predictability over autonomy" の根拠

「予測可能性優先」は思想ではなく、競合分析から導かれた market gap である。

### 根拠 1: 本番導入率の低さ

LangChain の State of Agent Engineering 調査 (公式、2025) では **「agent を本番に入れていない」が 45%** であることが示されている。主な理由は「予測可能性・品質管理の難しさ」。LLM が自律的に動き回る agent は評価・デバッグ・説明責任が難しく、本番化を阻む最大の障壁になっている。(→ langchain.md §6, §8)

### 根拠 2: CrewAI のデフォルトテレメトリ問題

CrewAI OSS はデフォルトで **匿名テレメトリを ON** で収集する。収集内容: エージェントロール名・ツール名・モデル名・実行設定など。`CREWAI_DISABLE_TELEMETRY=true` で無効化可能だが、**EU データローカリティ違反**の GitHub Issue が提起されている。日本企業のセキュリティ審査 (情報漏洩リスク評価) では即座に問題になる設計。(→ crewai.md §5)

### 根拠 3: クラッシュ回復の設計差

Diagrid の外部調査 (2025) が指摘: LangGraph・CrewAI・Google ADK 等の「チェックポイント」は **durable execution ではない**。具体的には: 自動 resume なし / 単一プロセス前提 / 排他制御なし / 直近 1 run のみ保持。長時間実行ジョブや mission-critical ワークフローでの production 利用に本質的な限界がある。Dify に至っては回復機能の実装自体が "Closed as not planned"。(→ crewai.md §3, dify.md §3)

### 根拠 4: LangGraph の遷移制御の限界

LangGraph の `Command()` API を使うとノード内部から任意遷移が可能になる。「制約はスキル作者のコーディング規律に委ねられており、P4 相当の OS レベル制約は存在しない」(→ langgraph.md §1, §10)。ガバナンスを厳しく求める組織 (金融、医療、公共、日本官公庁向けシステム) ではこの gap が訴求ポイントになる。

### 根拠 5: 「汎用＝あらゆるトレードオフを設計者に委ねる」

LangChain は「開発者体験フレームワーク」であり、ガバナンスはミドルウェアのコード設計に依存する — 「middleware を書かなければ素通りする」。AutoGen は「LLM の自由な協調」を設計価値とする。これらは汎用ツールとして合理的だが、**「予め保証された制約」が必要な環境では使えない**。Reyn はその gap を埋める。

---

## 4. 競合が解いていて Reyn が未着手の問題 (正直なギャップリスト)

> **✅ 2026-05-08 以降に landed（= `docs/feature-map.md` が source of truth）**:
> 旧 gap list で「未実装 / Phase 2 / 計画中」と誤記されていた以下は既に実装済み —
> **MCP client + A2A**（sync / async tasks / webhook push）、**RAG framework**
> （`recall` / `index_query` / `index_drop` ops + `index_docs` / `index_events`
> stdlib、SQLite backend、ADR-0033 Phase 1）、**コード実行**（`sandboxed_exec` op +
> `DockerEnvironmentBackend` ⚗ Stage-2 MVP）、**OTel / Langfuse export**（optional-dep）、
> **async HITL**（A2A `ask_user` → `input-required`）、**stdlib 3 → 12 本**。
> 下表は更新後の genuine remaining gaps のみ。

| ギャップ | 競合の状況 | Reyn の現状 | 優先度 |
|---|---|---|---|
| **ドメインスキルの breadth** | LangChain 130+ vector stores / 200+ 統合、CrewAI 30+ ツール、Dify 50+ ツール | Control IR ops + RAG (`recall`/`index_docs`) + コード実行 (`sandboxed_exec` + Docker backend ⚗MVP) は landed、stdlib **12 本**。残るは DB 接続・PDF 処理等のドメイン特化スキルの breadth（vs 競合の 200+ 統合）| **MEDIUM** |
| **Advanced retrieval** | Dify は PDF/Word/HTML + ハイブリッド検索を標準装備。CrewAI は ChromaDB/Qdrant 統合 | RAG framework foundation は landed（上記）。残: rerank / HyDE / contextual retrieval + SQLite 以外の vector store plugin | **MEDIUM** |
| **可観測性 web dashboard** | LangSmith (LangGraph/LangChain)・AgentOps (AutoGen)・AMP Dashboard (CrewAI)・Langfuse 連携 (Dify) | TUI Events tab 在 + OTel/Langfuse/ietf_audit exporter landed（optional-dep）。残: 非エンジニア向けの web dashboard | **MEDIUM** |
| **Skill Authoring ガイド** | CrewAI・LangChain・LangGraph はいずれも豊富な cookbook / design guide を持つ | SKILL.md テンプレート・Phase 設計パターン・Artifact Schema Primer の整備（docs 監査 2026-05-08）| **MEDIUM** |
| **日本語ドキュメント** | — | README 言及機能 (a2a/mcp) の翻訳整備（docs 監査 2026-05-08）| **MEDIUM** |
| **RBAC / SSO** | LangGraph/LangChain/CrewAI/Dify いずれも Enterprise プランで実装済み | 未実装 (設計上のみ) | **LOW** (pre-OSS では不要) |
| **ノーコード UI** | Dify がセグメントリーダー。AutoGen Studio (研究プロトタイプ)、CrewAI AMP Studio (SaaS) | CLI + TUI のみ | **LOW** (セグメントが異なる) |
| **多言語対応 (TypeScript/.NET)** | LangGraph TypeScript、Microsoft Agent Framework .NET+Python | Python 専用 | **LOW** (pre-OSS フェーズ優先外) |

---

## 5. Reyn の勝ち筋 — 誰に対して何で勝つか

### 勝ち筋 A: ガバナンス最優先エンタープライズ vs. LangChain / LangGraph

**ターゲット顧客:** 金融・医療・公共・日本官公庁向けシステムインテグレーター。SIer が顧客に「なぜこのフェーズに遷移したか」「このデータは何が変更したか」を監査部門に説明できなければならない環境。

**勝てる理由:**
- LangGraph は遷移制御の安全性をスキル作者の規律に委ね、P4 相当の OS 強制がない
- LangChain は本番導入 45% 未満 (State of Agent Engineering)。予測可能性問題が顕在化済み
- Reyn は OS レベルで `{control, artifact, control_ir}` を validate してから実行 — 「OS が検証した」という説明責任を提供できる

**現実的なギャップ:** 可観測性 UI と RBAC が未実装。エンタープライズ営業には不足。OSS リリース後に優先実装が必要。

---

### 勝ち筋 B: クラッシュ安全性 vs. CrewAI / AutoGen / Dify

**ターゲット顧客:** 長時間実行ジョブ (数時間〜数日) を持つ製造業・物流・バックオフィス自動化のシステム担当者。プロセスクラッシュ時に「最初からやり直し」が許容できない環境。

**勝てる理由:**
- CrewAI `@persist` は自動 resume なし・単一プロセス前提・直近 1 run のみ (Diagrid 分析)
- AutoGen は組み込みチェックポイントなし (ロードマップ issue #2358 で要望中)
- Dify はクラッシュ回復を "Closed as not planned" で拒否
- Reyn WAL + forward-replay は OS レベルで自動 (ADR-0023 + PR21)

**現実的なギャップ:** Stdlib 不足で「長時間ジョブを動かすスキルそのものがない」。Stdlib 拡充なしには勝ち筋 B は活かせない。

---

### 勝ち筋 C: 情報漏洩ゼロ設計 vs. CrewAI

**ターゲット顧客:** 社内ネットワーク外にデータを出せない日本のエンタープライズ (銀行・保険・医療機関)。OSS 採用の情報セキュリティ審査が厳格な組織。

**勝てる理由:**
- CrewAI OSS はデフォルトでテレメトリ ON。エージェントロール名・ツール名・モデル名が外部送信される
- 日本企業のセキュリティ審査で「デフォルト ON テレメトリ」は即却下されるケースが多い
- Reyn は設計上テレメトリゼロ。データは workspace (ローカルファイルシステム) のみ

**現実的なギャップ:** Reyn は pre-OSS で認知度がない。「CrewAI のテレメトリ OFF 版として選ぶ」動機が生まれるためには、最低限の Stdlib と OSS 公開が必要。

---

### 勝ち筋 D: コード設計品質 vs. Dify

**ターゲット顧客:** Dify でプロトタイピングを終え、「本番運用のガバナンス・クラッシュ回復・監査証跡」が必要になったエンジニアチーム。

**勝てる理由:**
- Dify は「作るコストを下げる」ツール。クラッシュ回復なし・LLM 出力バリデーションはオプト・イン
- Dify の Workflow モードは確定的だが、Agent Node + 複雑なビジネスロジックが混在すると予測可能性が下がる
- Reyn は「動かし続けるコストを下げる・ガバナンス保証」の軸で補完関係にある (→ dify.md §9)

**現実的なギャップ:** Dify は日本市場での実績 (Kakaku.com 全社展開・CTC パートナーシップ) があり認知度が高い。Reyn が「Dify から移行する理由」を説明できるドキュメントと、Dify で構築した PoC を Reyn に移行するガイドが必要。

---

## 6. 弱点の正直な評価 (更新版)

| 弱点 | 詳細 | 対応方針 |
|---|---|---|
| **ドメインスキルの breadth** | OS 組み込み Control IR ops + RAG (`recall`/`index_docs`) + コード実行 (`sandboxed_exec` + Docker backend ⚗MVP) は landed。不足は DB・PDF 処理・GitHub 等のドメイン特化スキルの breadth。全競合は 30〜200+ の統合を持つ | 既存 ops/RAG を活用したドメインスキル例 + stdlib 拡充を継続 |
| **Weak LLM 依存** | gemini-2.5-flash-lite の empty-stop attractor を継続チューニング中（envelope-layer fix 等で改善、本番 N=∞ は継続観測）| envelope-layer fix + per-scenario attractor audit で継続改善 |
| **Skill Authoring Guide** | tutorials（01-05）は動くが「自分の skill を作る」への橋（SKILL.md テンプレート・Phase Best Practices・Design Patterns）が薄い。※ `control-ir.md` の全 op カタログは整備済み（registry と sync）| Authoring Template + Patterns をドキュメント化 |
| **日本語ドキュメント** | README 言及機能 (a2a/mcp) の翻訳整備が残る | a2a / mcp / use-an-mcp-server 等を日本語化（small コスト）|
| **可観測性 web dashboard** | TUI Events tab + OTel/Langfuse exporter は landed（optional-dep）。不足は非エンジニア向けの web dashboard | 既存 OTel export を外部ツールで可視化、将来 web dashboard |
| **エコシステム小** | pre-OSS、stars 非公開、単一チーム | OSS 公開が前提。MCP/A2A は landed 済で外部アクセスは可能、認知度向上の起点は OSS 公開 |
| **RBAC / SSO 未実装** | OAuth/auth (`reyn auth`, RFC 8628 device grant) は landed。RBAC/SSO は設計上のみ | pre-OSS では後回し。OSS 公開後に対応 |
| **single process 前提** | LangGraph PostgreSQL 連携や AutoGen gRPC 分散ランタイムに相当するスケーラビリティがない | 長期課題。pre-OSS では優先度低 |

---

## 関連 doc

- [competitive/README.md](../competitive/README.md) — 5 競合の横比較テーブル (詳細データ出所)
- [competitive/langgraph.md](../competitive/langgraph.md)
- [competitive/langchain.md](../competitive/langchain.md)
- [competitive/autogen.md](../competitive/autogen.md)
- [competitive/crewai.md](../competitive/crewai.md)
- [competitive/dify.md](../competitive/dify.md)
- [ADR-0019: OpenUI / Reyn internal framing](../../en/decisions/0019-openui-reyn-internal-framing.md)
- [Reyn vision (memory)](../../../memory/project_reyn_vision.md)
