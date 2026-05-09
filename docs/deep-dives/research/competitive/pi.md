---
title: Pi (pi-mono) — 競合分析
last_updated: 2026-05-09
status: stable
sources:
  - url: https://mariozechner.at/posts/2025-11-30-pi-coding-agent/
    accessed: 2026-05-09
  - url: https://lucumr.pocoo.org/2026/1/31/pi/
    accessed: 2026-05-09
  - url: https://github.com/badlogic/pi-mono
    accessed: 2026-05-09
  - url: https://www.decisioncrafters.com/pi-mono-the-minimal-ai-agent-toolkit-with-44k-github-stars/
    accessed: 2026-05-09
  - url: https://agenticengineer.com/the-only-claude-code-competitor
    accessed: 2026-05-09
---

# Pi (pi-mono) — 競合分析

## TL;DR

Pi (pi-mono) は Mario Zechner（libGDX ゲームフレームワーク作者）が作った **TypeScript 製ミニマリスト AI エージェントツールキット**。MIT ライセンス。
2026-05 時点で **44,300+ GitHub stars**。OpenClaw（370K stars）の基盤フレームワークとして広く知られる。

> ⚠️ **カテゴリ注記**: Pi は主に **コーディングエージェント CLI**（Claude Code / Cursor / Windsurf の競合）として設計されており、Reyn とは直接の競合セグメントが異なる。ただし (1) OpenClaw の基盤であること、(2) 設計哲学が Reyn と真正面から対立していること、(3) Pi の「最小コア + 自己拡張」が業界の一方の極を代表することから、Reyn の方向性を定める上で重要な参照軸になる。

---

## 直接比較の前に: セグメント整理

| カテゴリ | 代表的製品 | Reyn との関係 |
|---|---|---|
| **コーディングエージェント CLI** | Pi / Claude Code / Cursor / Windsurf / OpenCode | **間接競合**（ターゲットユーザーが重ならない） |
| **エンタープライズ Workflow OS** | Reyn / LangGraph / CrewAI / AutoGen / Dify | **直接競合** |
| **パーソナル AI エージェント** | OpenClaw / Hermes Agent | **部分競合**（OpenClaw は Pi を基盤に使用） |

Pi は「コーディングエージェント」カテゴリの代表例だが、**そのアーキテクチャ選択（最小ツールセット・YOLO モデル・自己拡張）と Reyn の選択（P4 制約・PermissionResolver・OS 強制検証）が正反対**であることが重要。

---

## 1. コアアーキテクチャ

### モノレポ構成（5 パッケージ）

```
pi-mono/
├── pi-ai            # 統合 LLM API（15+ プロバイダ、streaming、tool calling）
├── pi-agent-core    # エージェントループ（ツール実行・検証・イベント emit）
├── pi-tui           # ターミナル UI（差分レンダリング、フリッカーレス）
├── pi-coding-agent  # CLI 本体（セッション管理・カスタムツール・プロジェクト設定）
└── pi-web-ui        # Web コンポーネント（チャット UI 向け）
```

### LLM の役割

Pi の LLM は **完全な decision engine + executor**。4 ツールから何を呼ぶかをモデルが完全自律で決定。ただし他のフレームワークと異なり、「ツールを最小限に絞ることで LLM の判断精度を上げる」という逆説的な設計思想を持つ。

| 設計選択 | Pi | Reyn |
|---|---|---|
| ツール数 | **4 本固定**（read / write / edit / bash） | OS ops 7 種 + スキル定義でカスタム |
| システムプロンプト | **〜200 トークン**（最小主義） | OS が動的生成（候補セット + スキーマ注入） |
| 候補制約 (P4 相当) | **なし**（LLM が自由に判断） | OS が候補セットを提示、違反は即 reject |
| 権限モデル | **「YOLO モード」デフォルト** — ファイルシステム・コマンド実行に制限なし | PermissionResolver — すべての操作を Permission Gate 経由で実行 |

### 自己拡張設計

Pi の最大の特徴は「エージェントが自分自身を拡張する」設計:
- 事前定義のツールセットや拡張機能をダウンロードするのではなく、**ユーザーがエージェントに「自分を拡張してほしい」と頼む**
- セッション間で拡張の状態を保持するメカニズムを持つ
- Hot reload: エージェントが自分の変更をループ内でテストできる

**Reyn との対比**: Reyn はスキル作者が Phase/Skill を明示的に設計する。Pi はエージェント自身が必要な機能を書く。

---

## 2. ワークフロー単位

### Pi に Skill/Phase 相当の概念はない

Pi は**タスクを逐次的に 1 エージェントが処理する**モデル。LangGraph の StateGraph や Reyn の Phase Graph に相当する「ワークフロー定義」は存在しない。

| Reyn 概念 | Pi 対応物 | 差異 |
|---|---|---|
| Phase | なし | Pi はエージェントループの 1 ターンを繰り返すのみ |
| Skill (graph) | なし | Pi はスキル定義なし。機能は拡張として都度実装 |
| OS (runtime) | pi-agent-core | 機能は類似だが P4/P5/P6/P7 制約なし |
| Control IR | 直接ツール呼び出し | Pi は宣言的 IR なし。LLM がツール名・引数を直接出力 |
| Workspace | ファイルシステム直接アクセス | Reyn は P5 (OS 強制 SSoT); Pi は無制限アクセス |

### セッション分岐

Pi の特徴的機能: **セッションをツリー構造に分岐**させ、デバッグツール作成などの「サイドクエスト」をメインコンテキストを消費せずに実行できる。

---

## 3. 信頼性・回復力

### YOLO モデルの含意

Pi の作者は「LLM がデータを読めてコードを実行できる時点で包括的なセキュリティは不可能」とし、**パーミッションチェックを「セキュリティシアター」として意図的に排除**。

これは Reyn の設計選択と根本的に対立する:

| 側面 | Pi | Reyn |
|---|---|---|
| デフォルト権限 | **全アクセス許可** | Permission Gate 経由で都度検証 |
| 誤操作保護 | なし（「エンジニアが責任を持つ」） | OS がスキーマ検証 + P4 候補制約で防御 |
| クラッシュ回復 | なし（設計外） | WAL + forward-replay (P23) |
| セッション永続化 | セッション状態のローカル保存 | Workspace SSoT (P5) |

### 弱 LLM 対応

Pi は「4 ツールに絞ることで弱いモデルでも正確に動く」というアプローチを採用。Terminal-Bench 2.0 で **Claude Opus + Pi（200 トークン SP）が Codex / Cursor / Windsurf と競合する結果**を出している。

**Reyn との比較**: Reyn は P4 で候補セットを制約することで弱 LLM の幻覚ループを防ぐ。Pi は候補制約なしでシンプルなツールセットに頼る。アプローチは異なるが「弱 LLM を使えるようにする」という目標は共通。

---

## 4. Stdlib・標準装備

### 4 ツールのみ（意図的）

| ツール | 内容 |
|---|---|
| `read` | ファイル読み込み |
| `write` | ファイル書き込み |
| `edit` | ファイル編集 |
| `bash` | シェルコマンド実行（事実上すべてに使用可能） |

**意図的な省略リスト**:
- ❌ MCP サポート（「MCP は複雑さを増すだけ」という作者の立場）
- ❌ サブエージェント（ファイルベースの代替で対応）
- ❌ プランモード / ToDo リスト
- ❌ バックグラウンド bash
- ❌ ブラウザ操作
- ❌ 組み込み RAG / データベース接続

**Reyn との比較**: Reyn は OS ops（file/web/shell/mcp/lint/run_skill）+ stdlib スキル群を持ち、Pi より機能が多い。ただし Pi は `bash` で事実上すべてのシェル操作が可能なため、熟練エンジニアには「足りない」とならない。

---

## 5. Enterprise 機能

### 明確に Enterprise 非対応

Pi の設計は個人開発者・エンジニア向けツールであり、エンタープライズ機能は意図的に省略されている:

| エンタープライズ要件 | Pi | Reyn |
|---|---|---|
| 監査ログ | なし | P6 append-only event log (出荷済み) |
| RBAC / SSO | なし | 設計あり（未実装） |
| 権限制御 | YOLO デフォルト | PermissionResolver (出荷済み) |
| 再現性保証 | なし | Control IR + state replay |
| テレメトリ | 不明 | ゼロテレメトリ設計 |

**結論**: Pi はエンタープライズ環境に導入できるフレームワークではない。逆に言えば **Pi が解こうとしている問題とReyn が解こうとしている問題は根本的に異なる**。

---

## 6. Ecosystem

### プロジェクト規模（2026-05 時点）

| 指標 | 値 |
|---|---|
| GitHub Stars | 44,300+ |
| 主要派生プロジェクト | `oh-my-pi`（LSP/browser/subagents 拡張版）、`pi-subagents` |
| Python 実装 | `pi-agent` (PyPI) — Pi の Python 再実装 |
| 関連プロジェクト | OpenClaw (370K stars) の基盤フレームワーク |

### OpenClaw との関係

Pi は OpenClaw（旧 Clawdbot）の **基盤エンジン**として機能している。Armin Ronacher の解説ブログ「Pi: The Minimal Agent Within OpenClaw」が詳述。この関係は重要で、Pi の adoption は OpenClaw の成長に連動して間接的に広がっている。

### コミュニティ

- Mario Zechner の個人ブログで設計思想を積極発信
- TypeScript エコシステム向け npm パッケージ（`@mariozechner/pi-coding-agent`）
- MCP 非対応が賛否を呼んでいる（MCP が標準化する中でどう動くか注目）

---

## 7. Pricing / License

| 項目 | 内容 |
|---|---|
| ライセンス | MIT |
| 価格 | 無料（Self-hosted） |
| LLM コスト | ユーザー負担（15+ プロバイダ選択可） |
| エンタープライズ tier | なし |
| ベンダーロックイン | なし（プロバイダ非依存） |

---

## 8. Reyn 対比

### 設計哲学の対立軸

Pi と Reyn は「エージェントの信頼性・制御をどう確保するか」という問いに**正反対の答えを出している**:

| 軸 | Pi の答え | Reyn の答え |
|---|---|---|
| ツール数 | **4 本に絞る**（複雑さを排除） | OS ops + スキル定義（拡張可能） |
| LLM への信頼 | **高信頼**（モデルに任せる。バグはレビューで対応） | **低信頼**（P4 で候補を制限、P6 で検証） |
| 権限モデル | **YOLO**（制限不要論） | **Permission Gate**（すべてを OS が検証） |
| 予測可能性 | LLM の判断に依存（意図的） | OS が構造的に保証（P4/P5/P6） |
| 監査証跡 | なし（設計外） | P6 append-only event log（出荷済み） |
| ターゲット | **個人開発者・エンジニア** | **企業・規制業種** |

### Reyn が優る点

| 項目 | 根拠 |
|---|---|
| **ガバナンス・監査** | Pi は監査ログなし・YOLO モデル。Reyn は P6 event log + PermissionResolver で regulated 環境に対応 |
| **エンタープライズ採用可能性** | Pi は「個人ツール」として設計。Reyn は日本エンタープライズを明示ターゲットにしている |
| **クラッシュ回復** | Pi は回復機能なし。Reyn は WAL + forward-replay (P23) |
| **再現性** | Pi はセッション分岐が面白いが再現性保証なし。Reyn は Control IR リプレイで保証 |
| **弱 LLM の構造的制御** | Pi は 4 ツールで実用性を保つが幻覚ループのガードレールなし。Reyn は P4 で構造的防御 |

### Pi が優る点

| 項目 | 根拠 |
|---|---|
| **Time to value** | `npm i @mariozechner/pi-coding-agent` で即動作。Reyn はスキル設計が必要 |
| **コーディングタスク特化** | コード読み書き・bash 実行に特化した 4 ツールで Terminal-Bench 2.0 の競合と対等の精度 |
| **システムプロンプト効率** | 〜200 トークンで Claude Code（10K+ トークン）より低コスト |
| **自己拡張能力** | エージェントが自分自身を拡張するモデルは開発者体験が独特 |
| **言語エコシステム** | TypeScript ネイティブ（Web/Node.js 開発者に親和性が高い） |
| **OpenClaw 基盤** | 370K stars の OpenClaw を動かす実証済みコア |

### 同等・中立

| 項目 | 評価 |
|---|---|
| ライセンス | 両者 MIT |
| LLM プロバイダ対応 | 両者 15+ プロバイダ（設計上） |
| コスト | 両者 Self-hosted + LLM API コストのみ |

---

## 9. Reyn が追いつくために必要なこと

Pi が解いていて Reyn が未対応の問題:

| # | 問題 | Pi の解法 | Reyn のギャップ | コスト |
|---|---|---|---|---|
| 1 | **最小 SP によるコスト最適化** | 〜200 トークン SP でモデルの学習能力に委ねる | OS が動的 SP を生成するため相対的にトークン消費大 | **MEDIUM** |
| 2 | **セッション分岐 (fork)** | セッションをツリー構造で保持、メインコンテキストを汚染せずサイドクエスト実行 | セッション分岐なし。単一ライン実行のみ | **MEDIUM** |
| 3 | **自己拡張メカニズム** | エージェントが自分自身を拡張し状態を保持 | スキルは静的・作者が設計。自己拡張なし | **LARGE** |
| 4 | **TypeScript SDK** | TypeScript ネイティブ（Web 開発者・Node.js エコシステムへの訴求） | Python のみ | **MEDIUM** |

> **注**: Pi の「YOLO モデル」「監査なし」は Reyn が追いつくべき要素ではなく、**Reyn が意図的に選ばないトレードオフ**。Reyn のターゲット（日本エンタープライズ・規制業種）にとって、Pi の設計は採用不可能。

---

## 最終評価

**Pi の市場ポジション**: 「熟練エンジニアが完全制御できる最小コーディングエージェント」という明確なニッチを占める。Claude Code の「batteries-included」に対するアンチテーゼ。OpenClaw という 370K stars のプロジェクトを産み出した基盤として実力は証明済み。ただしターゲットは個人開発者であり、エンタープライズ導入を想定していない。

**Reyn の位置づけ**: Pi と Reyn は**同じ問いに異なる優先順位で答えている**。

- Pi: 「LLM に最大の自由を与え、エンジニアがレビューで責任を持つ」
- Reyn: 「OS が構造的に制約し、LLM が範囲内で判断する」

どちらが「正解」かではなく、**ターゲット顧客が違う**。Pi は個人開発者・スタートアップのプロトタイピング。Reyn は監査・ガバナンス・再現性が要求されるエンタープライズワークフロー。両者が競合するシーンは少なく、むしろ「Pi でプロトタイプ → Reyn で本番化」という連続性がありうる。

**Reyn が Pi から学ぶべきこと**:
1. システムプロンプトの肥大化への意識（Reyn の SP トークン数を定期的に監視する）
2. 「4 ツールで何ができるか」という制約思考（Reyn のスキルが複雑化しすぎていないか確認）
3. セッション分岐という UX（デバッグ体験を改善するアイデアとして参照）

---

## 参考文献

- [Pi: The Minimal Agent Within OpenClaw (Armin Ronacher)](https://lucumr.pocoo.org/2026/1/31/pi/)
- [What I learned building an opinionated and minimal coding agent (Mario Zechner)](https://mariozechner.at/posts/2025-11-30-pi-coding-agent/)
- [GitHub: badlogic/pi-mono](https://github.com/badlogic/pi-mono)
- [Pi-Mono: Minimal AI Agent Toolkit with 44k+ Stars](https://www.decisioncrafters.com/pi-mono-the-minimal-ai-agent-toolkit-with-44k-github-stars/)
- [Pi Coding Agent: The Only Claude Code Competitor](https://agenticengineer.com/the-only-claude-code-competitor)
- [npm: @mariozechner/pi-coding-agent](https://www.npmjs.com/package/@mariozechner/pi-coding-agent)
- [GitHub: can1357/oh-my-pi](https://github.com/can1357/oh-my-pi) — Pi の拡張フォーク（LSP/browser/subagents）
