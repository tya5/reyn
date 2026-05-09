---
title: Hermes Agent — 競合分析 (Nous Research)
last_updated: 2026-05-09
status: stable
sources:
  - url: https://hermes-agent.nousresearch.com/
    accessed: 2026-05-09
  - url: https://hermes-agent.nousresearch.com/docs/
    accessed: 2026-05-09
  - url: https://github.com/nousresearch/hermes-agent
    accessed: 2026-05-09
  - url: https://hermes-agent.nousresearch.com/docs/user-guide/features/skills
    accessed: 2026-05-09
  - url: https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban
    accessed: 2026-05-09
  - url: https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers
    accessed: 2026-05-09
  - url: https://github.com/NousResearch/hermes-agent/releases/tag/v2026.5.7
    accessed: 2026-05-09
  - url: https://hermesatlas.com/reports/state-of-hermes-april-2026/
    accessed: 2026-05-09
---

# Hermes Agent — 競合分析 (Nous Research)

## TL;DR

Hermes Agent は Nous Research が 2026-02-25 に公開した **自己改善型オープンソース AI エージェントフレームワーク**。MIT ライセンス。
公開から 7 週間で 95,000 stars を達成し、2026-05 時点で **139,000+ GitHub stars**、295+ contributors/リリースという急成長中のプロジェクト。
最新安定版は v0.13.0「The Tenacity Release」(2026-05-07)。

Reyn との根本的な違いは **哲学の対立軸**: Reyn が「predictability over autonomy（P4 候補セット制約・決定論的前処理）」を掲げるのに対し、Hermes は「**autonomy + self-improvement**（LLM の自由な判断 + 経験からの学習）」を追求する。双方とも MIT / self-hosted / governance-optional という点で類似するが、設計思想は対極にある。

> ⚠️ **安定性注記**: v0.x はバージョン間の API 安定性保証なし。独立系レビュー（2026-03〜04）では「通常運用での本番使用には早すぎる」との評価あり。v1.0 ETA は 2026 末。

---

## 1. コアアーキテクチャ

### 全体スタック

```
User Input
    ↓
[3 エントリポイント]
  CLI セッション (synchronous)
  Gateway  (async: Telegram / Discord / Slack / WhatsApp / Teams 等 15+ platform)
  Cron Job (scheduled autonomous execution)
    ↓
AIAgent (run_agent.py — 同期オーケストレーションループ)
    ↓
Provider Abstraction (18+ LLM プロバイダ: Nous Portal / OpenRouter / Anthropic / NVIDIA NIM / AWS Bedrock 等)
    ↓
Skill System (Markdown ベース + GEPA 自己改善)
    ↓
Tool Layer (68+ ツール / 52 toolset)
    ↓
Kanban Board (SQLite — マルチエージェント協調)
```

### LLM の役割

Hermes の LLM は **完全な decision engine**。68+ ツールの中から何を呼ぶか、いつ停止するか、他エージェントに委譲するかをすべてモデルが決定する。Reyn の P4 候補制約に相当するものはない。

| コンテキスト | LLM の役割 | 制約 |
|---|---|---|
| セッション推論 | Decision engine | 制約なし — 68+ ツール全体から自由選択 |
| ツール呼び出し | Executor | LLM が直接ツール名・引数を出力 |
| 停止判断 | Decision engine | LLM 判断 + タイムアウト設定 |
| スキル生成 | Creator | 5+ ツール呼び出しのタスク後に自律的にスキルを作成 (GEPA) |
| マルチエージェント委譲 | Orchestrator | Kanban ボードのタスクをクレームし他エージェントへ渡す |

### GEPA — 自己改善ループ (ICLR 2026 Oral)

GEPA（Generative Experience and Pattern Aggregation）は Hermes の中核的差別化機能:
1. **経験収集**: タスク実行後の Execution Trace（ツール呼び出し列・結果・文脈）を記録
2. **スキル生成**: 5+ ツール呼び出しのタスクが完了すると Markdown スキルを自動生成
3. **スキル改善**: 再実行ごとにスキルを更新（因果分析で「なぜ失敗したか」を特定）
4. **ベンチマーク**: 繰り返しタスクで平均 40% の速度向上を報告（ICLR 2026 Oral 論文）

**Reyn との根本差異**: Reyn のスキルは静的（スキル作者が明示的に設計）。Hermes のスキルは動的（LLM が実行から学習し自律的に改善）。

---

## 2. ワークフロー単位

### Skill — Hermes の Phase/Skill 相当

Hermes Skill は `~/.hermes/skills/` に配置する **Markdown ファイル**。agentskills.io 標準に準拠。

```markdown
---
name: research-and-summarize
description: Web で情報収集し要約する
tags: [research, web, writing]
---
# Research and Summarize

1. web_search で対象トピックの上位結果を取得
2. web_fetch で各ページの詳細を取得
...
```

**GEPA による自動生成**: 5+ ツール呼び出しのタスク完了後、Hermes が自動でスキルを生成し登録する。

v0.10.0 バンドル: **118 の事前定義スキル**（research / writing / code / DevOps / PDF / web scraping 等）

### Kanban — マルチエージェント協調 (Phase/Skill に非相当)

Kanban は **SQLite バックドの永続タスクボード** で、複数エージェント間の動的協調を実現する:

```
Kanban Table: tasks (id, title, description, status, agent_claim, heartbeat, result, ...)
```

- エージェントがタスクをアトミックに claim（compare-and-swap 的なロック）
- 作業完了後に `complete` または `block`
- Heartbeat 消滅でタスクを自動 reclaim（zombie 検知）
- タスク単位の retry + 幻覚回復

**Reyn との対比**:

| 概念 | Reyn | Hermes |
|---|---|---|
| ワークフロー制御 | Phase Graph（静的・OS が強制） | Skill（動的・LLM が選択） + Kanban（マルチエージェント） |
| マルチエージェント | @sub_skill + run_skill op | Kanban ボード（複数 AIAgent が協調） |
| 学習 | なし（スキルは静的） | GEPA による継続的自己改善 |
| 状態管理 | Workspace (P5, OS 強制 SSoT) | SQLite + ファイルシステム（可搬性重視） |

---

## 3. 信頼性・回復力

### フォールバックプロバイダ（ターン単位）

Hermes v0.13.0 の目玉機能「The Tenacity Release」:

- **プライマリモデル失敗** → バックアッププロバイダ:モデルへ自動切り替え
- 失敗トリガー: レート制限 (429) / サーバーエラー (500-503) / 認証エラー (401/403) / 無効なレスポンス
- **文脈保持**: 会話履歴・ツール呼び出し・中間状態を保ったまま切り替え
- **ターン単位**: 次メッセージでプライマリ復帰（フォールバックループ防止）

例: Claude Sonnet 4.6 がレート制限 → Gemini 2.5 Flash に自動切り替え → 同一地点から再開

### Checkpoints v2 (v0.13.0)

- 単一ストア設計 + 実 pruning + ディスク上限ガード
- v0.x のチェックポイント蓄積問題（孤立 shadow repo）を解消
- **プロセスクラッシュ後の自動 resume**: Gateway 再起動後にセッションを自動復元
- `/update` 再起動でセッション状態を保持

**Reyn との比較**:
- Reyn P23 (WAL + forward-replay) はクラッシュを自動検知し人手介入なしで前方再生
- Hermes Checkpoints v2 は Gateway 再起動後に自動 resume するが、突然死への対応は Reyn の方が堅牢

### 弱 LLM 対応

**実測値（独立検証）**:
- Hermes 3 8B (Ollama) で単一ステップツール呼び出し: ✓ 安定
- 4+ ステップのマルチステップチェーン: 成功率低下（中間状態の維持が困難）
- **Hermes 3 8B が LangGraph ベンチマーク比 GPT-4o から -3 ポイント (91% ツール呼び出し精度)** を達成

P4 制約がないため、弱 LLM はツール名の幻覚やマルフォームド引数を生成しやすい。ただし fallback provider と per-task retry により実用上の耐性を確保している。

### 監査ログ（未出荷）

- Issue #487: 暗号ハッシュチェーン監査証跡（SHA-256 + Ed25519 署名）を計画
- **2026-05 時点では未出荷**
- GDPR / EU AI Act / SOC 2 準拠に必要な本格的監査ログはエンタープライズ向けサードパーティ（Petronella）経由のみ

---

## 4. Stdlib・標準装備

### ツール・スキル規模

| 種別 | 数 | 備考 |
|---|---|---|
| 組み込みツール | 68+ | 52 toolset（各 toolset が自己登録） |
| バンドルスキル | 118 | v0.10.0+ |
| コミュニティスキル | agentskills.io 標準で共有可能 | ファイル置くだけ |

### 主要ツールカテゴリ

| カテゴリ | 内容 |
|---|---|
| Web | search (SerperDev) / browse / extract / vision / TTS / 画像生成 |
| ターミナル | shell コマンド実行 / デバッグ |
| コード実行 | Python / JavaScript / bash |
| ファイル操作 | read / write / ディレクトリ横断検索 |
| ドキュメント | PDF / Word / CSV / JSON パース・検索 |
| MCP | MCP サーバーへのクライアント接続 (v0.6.0+) |

### メモリ（3 レイヤー）

1. **セッションメモリ**: 現在の会話（コンテキストウィンドウ内）
2. **エージェントメモリ**: セッション横断の永続メモリ（エージェント自律キュレーション）
3. **ユーザーメモリ**: Honcho ダイアレクティックユーザーモデル（プロファイル・好み・履歴）

FTS5（SQLite 全文検索）+ LLM サマリーで過去セッションから情報を想起。

### Reyn との Stdlib 比較

| カテゴリ | Reyn | Hermes | 評価 |
|---|---|---|---|
| OS ops (file/web/shell/mcp) | ✓ 実装済み | ✓ 同等機能あり | 同等 |
| ドメインスキル | 3 メタスキル | 118 バンドル + コミュニティ | **Hermes が圧倒** |
| RAG / Vector DB | なし | なし（FTS5 のみ） | 同等 |
| ブラウザ操作 | なし | ✓ あり | Hermes 優位 |
| コード実行サンドボックス | なし | ✓ あり（Terminal 抽象） | Hermes 優位 |
| 永続メモリ | なし | ✓ 3 レイヤー + Honcho | **Hermes が差別化** |
| MCP クライアント | Phase 2 ロードマップ | ✓ v0.6.0 で実装済み | Hermes 優位 |

---

## 5. Enterprise 機能

### 現状（v0.13.0、2026-05）: 黎明期

**出荷済み**:
- SQLite への会話ログ保存（session + message テーブル）
- JSON トラジェクトリスナップショットによるセッション永続化
- Gateway アクセス制御（機密ツール実行の承認ルーティング）
- Checkpoints v2（状態保持と回復）

**計画中・未出荷**:
- 監査証跡（Issue #487: SHA-256 ハッシュチェーン + Ed25519 署名）
- テナント単位のメモリ分離
- GDPR/コンプライアンス監査ログ

### セキュリティ懸念

Issue #7826 でのセキュリティ監査: Critical 4 件 + High 9 件（デフォルト設定での問題）:
- OAuth 認証情報の露出リスク
- ツール実行のレート制限不足
- Gateway エンドポイントのリクエスト検証欠如
- ツール呼び出し引数のインジェクションベクター

本番運用にはデフォルト以外のハードニングが推奨。

### エンタープライズオプション

| ティア | 価格 | 内容 |
|---|---|---|
| Self-hosted (MIT) | 無料 | 完全ローカル、SLA なし |
| FlyHermes (managed) | 非公開 | Docker/VPS 不要のマネージドサービス |
| Petronella (managed compliance) | $5K–$40K+ | CMMC / HIPAA / SOC 2 対応、エンタープライズ支援 |

**Reyn との比較**:

| 機能 | Reyn | Hermes | 評価 |
|---|---|---|---|
| append-only イベントログ | P6（出荷済み） | 計画中（未出荷） | **Reyn 勝ち** |
| 自動クラッシュ回復 | P23 WAL + forward-replay | Checkpoints v2（manual trigger 要） | **Reyn 勝ち** |
| 再現性保証 | Control IR フルリプレイ可 | スキルが自己改善するため再現性不明 | **Reyn 勝ち** |
| テナント分離 | P5 workspace-per-tenant | 計画中 | **Reyn 勝ち** |
| 監査コンプライアンス | P6 設計で対応可 | Petronella 経由のみ | **Reyn 勝ち** |

---

## 6. Ecosystem

### プロジェクト規模（2026-05 時点）

| 指標 | 値 |
|---|---|
| GitHub Stars | 139,000+ |
| GitHub Forks | 21,500+ |
| Contributors / リリース | 295+ (v0.13.0) |
| リリースサイクル | 7–14 日 |
| v0.12 からのコミット数 | 864 |
| v0.12 からのマージ PR | 588 |

### コミュニティ

- 84 の品質フィルター済みコミュニティプロジェクト（HermesAtlas.com）
- LocalHermes / HermesX 等の派生プロジェクト
- agentskills.io — スキル共有標準
- `awesome-hermes-agent`（0xNyk）、`hermes-agent-docs`（mudrii v0.2.0）

### ドキュメント品質

- [hermes-agent.nousresearch.com/docs](https://hermes-agent.nousresearch.com/docs/) — 体系的な学習パス（beginner → expert）
- クイックスタート: 5 分でインストール + 初会話
- `/llms.txt` でドキュメントインデックスを LLM コンテキスト用に提供（約 17KB）
- Use Case 別テーブルで非線形ナビゲーション対応

**安定性懸念**: v0.x は破壊的変更が deprecation 警告なしで入ることがある。欧州中堅企業の 2026-03〜04 独立レビューでは「通常業務の本番利用には早すぎる」と評価。v1.0（2026 末 ETA）まで様子見推奨。

---

## 7. Pricing / License

| 項目 | 内容 |
|---|---|
| ライセンス | MIT |
| Self-hosted | 無料（$5–10/月 VPS + LLM API コスト） |
| タスク単価目安 | ~$0.30/複雑タスク（バジェットモデル使用時） |
| FlyHermes | 非公開（マネージド） |
| Petronella | $5K–$40K+（エンタープライズ managed） |
| ベンダーロックイン | なし（SQLite はポータブル、スキルは Markdown、LLM 非依存） |

---

## 8. Reyn 対比

### 設計哲学の対立軸

| 軸 | Reyn | Hermes |
|---|---|---|
| LLM の自由度 | P4 制約（候補セット内のみ） | 完全自由（68+ ツール全体から選択） |
| 実行の決定論性 | OS 強制・deterministic | LLM 駆動・stochastic |
| スキルの性質 | 静的（スキル作者が設計） | 動的（GEPA が実行から学習・改善） |
| 状態管理の哲学 | Workspace SSoT（P5、OS 強制） | SQLite + ファイル（可搬性重視） |
| 監査/コンプライアンス | P6 出荷済み | 計画中（未出荷） |
| 価値提案 | 予測可能性・監査可能性・ガバナンス | 自律性・自己改善・速度 |

### 能力マトリクス

| 機能 | Reyn | Hermes | 優位 |
|---|---|---|---|
| フェーズ遷移候補制約 | ✓ (P4) | ✗ | Reyn |
| OS レベル出力検証 | ✓ | ✗ (事後検証) | Reyn |
| append-only 監査ログ（出荷済み） | ✓ (P6) | ✗（計画のみ） | **Reyn** |
| 自動クラッシュ回復 | ✓ (WAL + forward-replay) | 部分（Checkpoints v2）| **Reyn** |
| 再現性保証 | ✓ (Control IR リプレイ) | ✗（スキルが変化する） | **Reyn** |
| スキル自己改善 | ✗ | ✓ (GEPA, ICLR 2026 Oral) | **Hermes** |
| セッション横断メモリ | ✗ | ✓ (3 レイヤー + Honcho) | **Hermes** |
| Stdlib 幅 | OS ops + 3 メタスキル | 68+ ツール + 118 スキル | **Hermes** |
| マルチエージェント協調 | @sub_skill + run_skill | Kanban (本番グレード) | Hermes |
| 弱 LLM の構造的安全性 | ✓ (P4 が幻覚ループを防ぐ) | ✗（fallback で実用的対処） | Reyn（構造面） |
| Time to first agent | ~30 分（スキル設計必要） | 5 分（CLI で即起動） | Hermes |
| エコシステム momentum | Pre-OSS | 139K stars、急成長 | Hermes |

### Reyn が優る点

1. **ガバナンス優先設計**: P4/P5/P6 の組み合わせが regulated 業界（金融・医療・公共）に必要な監査・再現性・権限制御をアーキテクチャレベルで保証
2. **自動クラッシュ回復**: WAL + forward-replay が人手介入なしで動作。Hermes の Checkpoints v2 は Gateway 再起動後 resume だが、突然死への対応は Reyn の方が堅牢
3. **決定論的再現性**: Control IR + state replay で同一入力から同一出力を再現可能。Hermes はスキルが自己改善するため過去の挙動の再現は保証されない
4. **スキルバージョン安定性**: スキルが変化しないため、本番環境でのデプロイ計画が立てやすい

### Hermes が優る点

1. **自己改善 (GEPA)**: 繰り返しタスクで 40% 速度向上（ICLR 2026 Oral）。同じ組織が使い続けるほど賢くなる
2. **永続メモリ**: セッション横断のユーザーモデリング（Honcho）で長期的なコンテキスト維持
3. **Stdlib の幅**: 68+ ツール + 118 スキルで即座に多様なユースケースに対応
4. **マルチエージェント (Kanban)**: SQLite バックドの永続タスクボードで本番グレードのマルチエージェント協調
5. **Time to value**: 5 分でエージェント起動。Reyn はスキル設計 + フェーズグラフ設計が必要

---

## 9. Reyn が追いつくために必要なこと

Hermes が解いていて Reyn が未対応の問題:

| # | 問題 | Hermes の解法 | Reyn のギャップ | コスト |
|---|---|---|---|---|
| 1 | **スキル自動生成** | GEPA: 実行トレースからスキルを自動生成 | スキルは手動設計のみ | **LARGE** |
| 2 | **セッション横断メモリ** | 3 レイヤー + Honcho ユーザーモデリング | Workspace はスキル実行単位、セッション横断なし | **MEDIUM** |
| 3 | **GEPA 自己改善** | ICLR 2026 Oral: 因果分析 + 反復改善で 40% 速度向上 | 自己改善メカニズムなし | **LARGE** |
| 4 | **Kanban マルチエージェント** | SQLite 永続タスクボード + atomic claim | single coordinator 前提。本番マルチエージェントは未実装 | **LARGE** |
| 5 | **MCP クライアント** | v0.6.0 で実装済み（OAuth 2.1 対応） | Phase 2 ロードマップ | **MEDIUM** |
| 6 | **監査証跡ハッシュチェーン** | Issue #487 で設計中（未出荷） | P6 はあるが tamper-evident でない | **MEDIUM** |
| 7 | **弱 LLM 信頼度スコアリング** | fallback provider + per-task retry | P4 で幻覚ループは防ぐが弱 LLM でのコスト最適化は未実装 | **SMALL** |

**戦略的含意**: Hermes が監査証跡を出荷した場合（Issue #487, ETA 不明）、Reyn の最大の差別化ポイントのひとつが失われる。Reyn は P6 + P23 の優位性を「すでに出荷済み・本番実績あり」として強調する必要がある。

---

## 最終評価

**Hermes の市場ポジション**: 「自己改善する個人エージェント」という新しいカテゴリを定義しつつある。GEPA + 永続メモリ + Kanban による「使うほど賢くなるエージェント」は LangGraph/CrewAI にはないポジショニング。ただし API 安定性の問題（v0.x）と監査ログ未出荷により、エンタープライズ採用は現時点では困難。

**Reyn の差別化機会**:
- 「Hermes は自律・成長型。Reyn は予測・監査型」という明確な二分法が成立する
- Hermes が弱い「再現可能な実行」「出荷済み監査ログ」「スキルバージョン安定性」はそのまま Reyn の訴求点
- 日本エンタープライズ（規制業種）では「過去の実行を監査担当者に説明できる」というユースケースが Reyn に有利

**監視すべき動向**:
- Issue #487（監査証跡）がいつ出荷されるか
- v1.0 マイルストーン（2026 末 ETA）での API 安定化
- GEPA の OSS 公開とサードパーティ実装の普及

---

## 参考文献

- [Hermes Agent Official](https://hermes-agent.nousresearch.com/)
- [Hermes Agent Docs](https://hermes-agent.nousresearch.com/docs/)
- [GitHub: NousResearch/hermes-agent](https://github.com/nousresearch/hermes-agent)
- [Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- [Kanban Documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban)
- [Fallback Providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers)
- [Release v0.13.0 — "The Tenacity Release"](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.5.7)
- [State of Hermes Agent (April 2026) — HermesAtlas](https://hermesatlas.com/reports/state-of-hermes-april-2026/)
- [GEPA Self-Evolution Repo](https://github.com/NousResearch/hermes-agent-self-evolution)
- [GitHub Issue #487 — Cryptographic Audit Trail](https://github.com/NousResearch/hermes-agent/issues/487)
- [GitHub Issue #7826 — Security Audit Findings](https://github.com/NousResearch/hermes-agent/issues/7826)
- [Hermes Agent Review (Krzysztof Słomka)](https://kisztof.medium.com/hermes-agent-review-nous-researchs-self-improving-ai-agent-e72bc244435a)
- [Hermes vs OpenClaw: The Self-Improving AI Race](https://www.contextstudios.ai/blog/hermes-agent-vs-openclaw-the-self-improving-ai-race)
- [Best Models for Hermes Agent 2026](https://www.remoteopenclaw.com/blog/best-models-for-hermes-agent)
