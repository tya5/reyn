---
title: OpenClaw — 競合分析
last_updated: 2026-05-09
status: stable
sources:
  - url: https://openclaw.ai/
    accessed: 2026-05-09
  - url: https://docs.openclaw.ai/
    accessed: 2026-05-09
  - url: https://github.com/openclaw/openclaw
    accessed: 2026-05-09
  - url: https://www.digitalocean.com/resources/articles/what-is-openclaw
    accessed: 2026-05-09
  - url: https://docs.openclaw.ai/tools/skills
    accessed: 2026-05-09
  - url: https://docs.openclaw.ai/gateway/security
    accessed: 2026-05-09
  - url: https://docs.openclaw.ai/concepts/model-failover
    accessed: 2026-05-09
  - url: https://www.getpanto.ai/blog/openclaw-ai-platform-statistics
    accessed: 2026-05-09
  - url: https://blink.new/blog/openclaw-enterprise-hosting-compliance-sso-2026
    accessed: 2026-05-09
---

# OpenClaw — 競合分析

## TL;DR

OpenClaw（旧称 Clawdbot → Moltbot、2026-01 に改称）は Austrian 開発者 Peter Steinberger が 2025-11 に公開した **ローカルホスト型の自律 AI エージェントフレームワーク**。MIT ライセンス。
2026-03 までに **370,000+ GitHub stars**（GitHub 史上最速クラス）を達成し、500K+ の稼働インスタンスと 3.2M ユーザーを持つ。
Steinberger は 2026-02-14 に OpenAI 入社を発表し、プロジェクトは非営利財団へ移管予定。

Reyn との根本的な違いは 2 点: (1) **LLM が全ツール呼び出しを自律的に決定する（P4 制約なし）**、(2) メッセージングプラットフォーム（Discord / Telegram / WhatsApp / Signal 等）を主 UI とする **パーソナルアシスタント志向の設計**。Reyn の「governance-first + 予測可能性」とは対極の「LLM autonomy-first」設計哲学。

> ⚠️ **セキュリティ注記**: 2026-04 時点で 138 CVE が公開記録されており、CVSS 9.9 を含む 7 件の Critical、49 件の High を含む。日本エンタープライズ採用には追加のハードニング評価が必要。

---

## 1. コアアーキテクチャ

### 全体スタック

```
User (Discord / Telegram / WhatsApp / Signal / CLI / Web)
    ↓
Channel  (メッセージング PF ごとのアダプター)
    ↓
Gateway  (セッション管理・ルーティング・JSONL ストレージ)
    ↓
LLM Backend (Claude / GPT-5 / Gemini / DeepSeek / Ollama 等 18+ モデル)
    ↓
Skill System (SKILL.md ベースのツール合成レイヤー)
    ↓
Core Tools (8 種: read / write / edit / apply_patch / exec / browser / web_search / web_fetch)
```

### LLM の役割

OpenClaw の LLM は **pure decision engine + executor hybrid**。モデルが各ターンで「何のツールを呼ぶか」「いつ停止するか」「サブエージェントに委譲するか」のすべてを決定する。

| コンテキスト | LLM の役割 | 制約 |
|---|---|---|
| セッション推論 | Decision engine | 制約なし — 利用可能なツールすべてから自由選択 |
| ツール呼び出し | Executor | LLM がツール名と引数を直接出力 (Zod スキーマ検証は事後) |
| 停止判断 | Decision engine | LLM がツール呼び出しをやめれば停止 (ハードリミット別途) |
| サブエージェント委譲 | Decision engine | `spawn_agent` スキルで別エージェントを起動するか決定 |

**Reyn との根本差異**: Reyn OS は P4 として「次フェーズ候補セット + 許可ツールセット」を LLM に提示し、LLM はその enum 内から選ぶ。OpenClaw にはこのガードレールが存在しない。OpenClaw で制御規律を維持するにはプロンプトエンジニアリングとモデル能力への依存が必要。

### プロアクティブエージェンシー

LangGraph (reactive、明示的起動) と異なり、OpenClaw エージェントは **継続的に稼働し自律的にアクションを判断する**。スケジュール・イベント・メッセージ受信に対してプロアクティブに動作する設計。

---

## 2. ワークフロー単位

### Skill — OpenClaw の Phase 相当

OpenClaw の Skill は `SKILL.md` を置くだけで登録できる **ディレクトリベースのモジュール**。コード変更不要。

```
skill-name/
├── SKILL.md      # YAML frontmatter + Markdown 指示
└── (任意のサポートファイル)
```

`SKILL.md` 構成:
- **YAML frontmatter**: name / description / tags / context requirements / model hints
- **Body**: タスク記述 + 例 + エッジケースの自然言語説明
- スキルは「複数ツールをどう組み合わせるか」を LLM に教える「レシピ」として機能

| Reyn 概念 | OpenClaw 対応物 | 差異 |
|---|---|---|
| Phase | Skill (SKILL.md の 1 ブロック) | Phase は input_schema + OS 検証あり。Skill は自然言語のみ |
| Skill graph | なし (明示的グラフ定義なし) | OpenClaw は LLM が暗黙的に複数スキルを組み合わせる |
| OS runtime | Gateway | Reyn OS は P7 (skill-agnostic); Gateway はスキル概念を内包 |
| Control IR | Core Tools への直接呼び出し | Reyn は宣言的 IR → OS 実行; OpenClaw は LLM が直接ツール呼び出し |
| Workspace | ローカルファイル + JSONL | 哲学的に類似 (ファイルベース SSoT)。ただし OS 強制なし |

### エコシステム規模

| 種別 | 数 | 備考 |
|---|---|---|
| 公式スキル | 53 | Gmail / Calendar / GitHub / Home Assistant 等 |
| ClawHub コミュニティスキル | 44,000+ | 2026-02 の 5,700 から 2026-04 に 44,000+（670% 成長） |
| コアツール | 8 | read / write / edit / apply_patch / exec / browser / web_search / web_fetch |

---

## 3. 信頼性・回復力

### クラッシュ回復

**実装**:
- セッション状態は Gateway プロセスが所有
- トランスクリプトは **JSONL（append-only）** としてローカル保存
- メモリは **プレーン Markdown** をワークスペースに保存

**回復フロー**:
1. STATUS を PAUSED にマーク
2. JSONL から最後の良好な状態を読み込み
3. 失敗ステップのみ安価なモデルで再実行
4. 出力確認後 PAUSED フラグを解除

**既知の問題**:
- Gateway 再起動時の自動回復なし — 古いタスクレコードが手動介入まで残留 (Issue #65136)
- セッション自動コンパクション時の文脈喪失報告あり（45 時間分のコンテキストが失われた事例、Issue #5429）
- JSONL append-only 設計で不完全ターンが残ることがある

**Reyn との比較**: Reyn P23 (WAL + forward-replay) は クラッシュを自動検知し自動で前方再生を行う。OpenClaw は PAUSED フラグが手動トリガーであり、プロセス突然死には対応が弱い。

### 弱い LLM への対応

- `compat.supportsTools: false` フラグでツールスキーマ非対応モデルに対応
- プロンプト削減・セッション履歴短縮・軽量モデルへの切り替えで対処
- マルチモデル並列実行で出力を比較する手法も利用可能

**既知の問題**:
- LLM が無効なツール呼び出しを生成した場合 (Zod スキーマ検証失敗)、Gateway が未補足例外でセッションをアボート (Issue #38384)
- LangGraph の `ValidationNode` + retry ループ相当のフレームワーク組み込みフォールバックなし
- **[Inferred]** 弱 LLM への対策はプロンプト設計とモデル選択に依存しており、構造的な安全網は薄い

---

## 4. Stdlib・標準装備

### コアツール（8 種）

| カテゴリ | ツール |
|---|---|
| ファイル操作 | `read`, `write`, `edit`, `apply_patch` |
| システム実行 | `exec`（シェルコマンド） |
| Web アクセス | `web_search`, `web_fetch` |
| ブラウザ操作 | `browser`（クリック・フォーム・スクリーンショット） |

### 公式スキル（53 種）の主要カテゴリ

- **生産性**: Gmail / Google Calendar / Slack / Microsoft Teams / Notion
- **開発**: GitHub / GitLab / Jira / Linear / VS Code
- **SNS**: Twitter/X / LinkedIn / Mastodon
- **スマートホーム**: Home Assistant / Philips Hue / Tesla
- **その他**: Obsidian / Google Drive / Dropbox

### Reyn との Stdlib 比較

| カテゴリ | Reyn | OpenClaw | 評価 |
|---|---|---|---|
| コアツール（OS ops） | file / web / shell / mcp / lint / run_skill | 8 種（file 系 4 + exec + browser + web 2） | 同等（browser は Reyn 未実装） |
| ドメインスキル | 3 メタスキル (skill_router / eval / skill_improver) | 53 公式 + 44,000+ コミュニティ | **OpenClaw が圧倒** |
| RAG / Vector DB | なし | なし（ワークスペース Markdown 検索のみ） | 同等（両者未実装） |
| コード実行 | shell op | `exec` ツール | 同等 |
| ブラウザ操作 | なし | `browser` ツール | OpenClaw が優位 |

---

## 5. Enterprise 機能

### 監査ログ

- RBAC 監査ログ: ユーザー ID + タイムスタンプ + アクション + 結果 + IP を記録
- ELK Stack 経由で収集、90 日保持（ISO 27001 準拠）

**Gap**:
- デフォルトのログはローカルファイル書き込み → シェルアクセスで改ざん可能
- 中央集権型監視なし（SOC 2 CC7.2 準拠には外部 SIEM が必要）
- Reyn P6（append-only イベントログ、OS 強制）と比べると監査の堅牢性が低い

### 権限制御

- **Ambient authority モデル**: エージェントが起動ユーザーの権限をそのまま引き継ぐ
- スキルレベルのアクセスゲートなし（base OpenClaw）
- NemoClaw (NVIDIA エンタープライズ版) で YAML 定義のアクセスポリシーとサンドボックスを追加

**Reyn との比較**: Reyn は OS レベルで `PermissionResolver` を持ち、すべての操作を `Permission Gate` 経由で実行する。OpenClaw の ambient authority は regulated 環境では governance リスクとなる。

### 再現性

- 公式ドキュメントに再現性のフォーマルな記述なし
- JSONL ログによる履歴リプレイは可能だが API として提供されていない
- LLM の非決定論的出力・ツール副作用により完全再現は困難

### エンタープライズデプロイオプション

| ティア | 価格 | 特徴 |
|---|---|---|
| Self-hosted (OSS) | 無料 | 完全ローカル制御、SLA なし |
| OpenClaw Cloud | $59/月 (初月 $29.50) | マネージドホスティング |
| NemoClaw (NVIDIA) | カスタム | YAML ポリシー / OS サンドボックス / Box・Salesforce・SAP 等との統合 |

---

## 6. Ecosystem

### プロジェクト規模（2026-05 時点）

| 指標 | 値 | 文脈 |
|---|---|---|
| GitHub Stars | 370,000+ | GitHub 史上最速クラス（React を抜いた） |
| GitHub Forks | 76,400+ | |
| Contributors | 1,200+ | |
| 稼働インスタンス | 500,000+ | |
| アクティブユーザー | 3,200,000 | |
| Discord メンバー | 169,545 | |
| リリースサイクル | 約 2 日ごと | |

### 採用状況

- エンタープライズ Q1 2026 エージェント移行の 34%（Armalo AI 調査）
- 180+ スタートアップが OpenClaw 上で構築、合算 $320K+/月の収益
- NemoClaw 初期パートナー: Box / Cisco / Atlassian / Salesforce / SAP / CrowdStrike

### ドキュメント品質

- [docs.openclaw.ai](https://docs.openclaw.ai/) — 構造化されており読みやすい
- コア概念（モデルプロバイダ / フェイルオーバー / スキル / セキュリティ）は網羅
- エンタープライズ向けの詳細例・信頼性設計の記述は薄い
- コミュニティ: 100+ Medium 記事、`awesome-openclaw-skills`、`awesome-openclaw-agents`

---

## 7. Pricing / License

| 項目 | 内容 |
|---|---|
| ライセンス | MIT（商用利用・改変・再配布すべて可） |
| Self-hosted | 無料（インフラ + LLM API コストのみ） |
| OpenClaw Cloud | $59/月 |
| NemoClaw (Enterprise) | カスタム価格 |
| ベンダーロックインリスク | 低（スキルは SKILL.md ポータブル、セッションは JSONL 可読） |

---

## 8. Reyn 対比

### Reyn が優る点

| 項目 | 根拠 |
|---|---|
| **LLM 決定の安全性 (P4)** | Reyn は OS が候補セットを提示し LLM は enum 内から選択。OpenClaw は LLM が自由に任意ツールを呼ぶため、幻覚ツール呼び出しやアボートのリスクが高い |
| **OS レベル出力検証** | Reyn は JSON スキーマ検証を OS が事前実行。OpenClaw の Zod 検証は事後 + 失敗時クラッシュ (Issue #38384) |
| **監査ログ設計 (P6)** | Reyn の append-only イベントログは OS 強制。OpenClaw はローカルファイル書き込みで改ざん可 |
| **フェーズ/スキルの形式化** | Reyn Phase = input_schema + 型付き検証。OpenClaw Skill = 自然言語のみ（テスト困難） |
| **クラッシュ回復 (P23)** | Reyn WAL + 自動 forward-replay。OpenClaw は手動 PAUSED フラグ |
| **ガバナンス設計** | P4/P5/P6 の組み合わせで regulated 業界に対応。OpenClaw の ambient authority は規制環境では governance リスク |
| **セキュリティ体制** | Reyn は CVE 報告なし（pre-OSS）。OpenClaw は 138 CVE (CVSS 9.9 含む) |

### OpenClaw が優る点

| 項目 | 根拠 |
|---|---|
| **エコシステム規模** | 44,000+ コミュニティスキル vs Reyn 3 メタスキル。ドメインスキルの差は埋めるのに LARGE コスト必要 |
| **マルチモデルサポート** | Claude / GPT-5 / Gemini / DeepSeek / Grok / Mistral / Ollama をファーストクラスでサポート |
| **採用実績** | 370K stars / 500K インスタンス / 180+ スタートアップ。Reyn は pre-OSS |
| **ブラウザ操作** | `browser` ツール（クリック・スクリーンショット）を標準装備 |
| **Kubernetes 成熟度** | Docker/k8s でのマルチインスタンス運用が確立済み |
| **ドキュメント・コミュニティ** | 100+ 公開記事、169K Discord。Reyn は内部設計ドキュメントのみ |

### 同等・中立

| 項目 | 評価 |
|---|---|
| ライセンス | 両者 MIT（Reyn は OSS リリース後） |
| ファイルベース SSoT の哲学 | 同等（ただし Reyn は P5 として OS 強制） |
| RAG/ベクトル DB | 両者未実装 |
| コスト | 両者セルフホスト + LLM API コストのみ |

---

## 9. Reyn が追いつくために必要なこと

OpenClaw が解いていて Reyn が未対応の問題と実装コスト見積もり:

| # | 問題 | 競合の解法 | Reyn のギャップ | コスト |
|---|---|---|---|---|
| 1 | **ドメインスキル不足** | 44,000+ コミュニティスキル + ClawHub レジストリ | 3 メタスキルのみ。RAG / DB / 翻訳 / PDF / メール等なし | **LARGE** |
| 2 | **Observability UI** | ELK Stack + RBAC 監査 UI | イベントログはファイルのみ、非エンジニアが可視化不可 | **LARGE** |
| 3 | **RBAC + SSO** | NemoClaw で SAML 2.0 / OIDC + RBAC | 権限制御は設計のみ、実装なし | **LARGE** |
| 4 | **PostgreSQL Checkpointer** | マルチインスタンス運用確立済み | WAL はファイルベース単一プロセス前提 | **MEDIUM** |
| 5 | **ブラウザ操作** | `browser` ツール標準装備 | browser op なし | **MEDIUM** |
| 6 | **マルチモデルルーティング** | 18+ モデルのファーストクラスサポート | モデル抽象は設計あり、ルーティングは未実装 | **SMALL** |

---

## 最終評価

**OpenClaw の市場ポジション**: 「LLM に自由を与え、ローカルで動かす」という設計哲学で爆発的な grassroots 採用を実現。個人ユーザーや速度優先のスタートアップに強い。一方でセキュリティ（138 CVE）・信頼性（手動クラッシュ回復）・ガバナンス（ambient authority）に明確な弱点がある。

**Reyn の差別化機会**: OpenClaw が弱い「regulated 業界向けガバナンス」「監査可能な実行トレース」「弱 LLM でも予測可能な動作」の 3 点が、Reyn の P4/P5/P6 設計によってアーキテクチャレベルで解決されている。これは日本エンタープライズ市場（金融・医療・公共）への直接訴求となる。

**最大の課題**: エコシステム規模（44,000+ スキル差）と採用実績（370K stars 差）。技術的優位性をどう認知してもらうかが OSS ローンチの核心課題。

---

## 参考文献

- [OpenClaw — Personal AI Assistant](https://openclaw.ai/)
- [OpenClaw Documentation](https://docs.openclaw.ai/)
- [GitHub: openclaw/openclaw](https://github.com/openclaw/openclaw)
- [What is OpenClaw? | DigitalOcean](https://www.digitalocean.com/resources/articles/what-is-openclaw)
- [Skills — OpenClaw Docs](https://docs.openclaw.ai/tools/skills)
- [Security — OpenClaw Docs](https://docs.openclaw.ai/gateway/security)
- [Model Failover — OpenClaw Docs](https://docs.openclaw.ai/concepts/model-failover)
- [OpenClaw AI Platform Statistics 2026](https://www.getpanto.ai/blog/openclaw-ai-platform-statistics)
- [OpenClaw Enterprise Hosting: Compliance, SSO & Audit Logs](https://blink.new/blog/openclaw-enterprise-hosting-compliance-sso-2026)
- [GitHub: jgamblin/OpenClawCVEs](https://github.com/jgamblin/OpenClawCVEs/)
- [OpenClaw vs LangGraph: Which Fits Your Agent Stack?](https://delx.ai/openclaw/openclaw-vs-langgraph)
- [OpenClaw hit 346K GitHub stars. Here are all the numbers](https://openclawvps.io/blog/openclaw-statistics)
