---
type: concept
topic: architecture
audience: [human, agent]
---

# Agent エンジニアリング — 7 つのレンズ

Reyn は 7 つのエンジニアリング視点を通じて読み解けます。各レンズは「このシステムは何を正しく実現しているか、そしてどこがまだ薄いか？」という問いに対する異なる見方です。同じドキュメントが複数のレンズから参照されており、このインデックスがその地図です。

## 全体像

```
              ┌──────────────────────────────────────────────────┐
              │                                                  │
              │    User                                          │
              │     │                                            │
              │     ▼                                            │
              │    Agent ── selects a Skill ─────► Skill         │   ← System Design
              │                                     │            │   ← Tool Contract
              │                                     ▼            │
              │             OS ◄──── runtime loop ──┤            │   ← Reliability
              │              │                      ▼            │
              │              │                    Phase          │   ← Retrieval
              │              │             (input + instructions)│       (preprocessor)
              │              │                      │            │
              │              │                      ▼            │
              │              │                  Workspace        │   ← Security
              │              │                                   │       (permissions)
              │              ▼                                   │
              │         ┌────────┐                               │   ← Evaluation
              │         │ Events │ ─────► JSONL replay log       │       Observability
              │         └────────┘                               │
              │                                                  │
              └──────────────────────────────────────────────────┘
                                                                       ← Product Think
                                                                          (CLI, cost, UX)
```

各レイヤーには対応するエンジニアリングレンズがあります。レンズはシステムを分割するのではなく、意図的に重なり合います。

## 7 つのレンズ

### 1. [System Design](system-design.md)

マクロ構造: 制御フロー、状態、責任をレイヤーを横断してどのように分散させるか。Reyn ではこれが **Phase / Skill / OS** の分割として現れます。Phase はステートレスで再利用可能、Skill は構造を所有し、OS は実行を所有します。

### 2. [Tool Contract Design](tool-contract-design.md)

LLM がどのように世界に作用するか: 副作用を運ぶ型付きエンベロープ（Control IR）、決定を運ぶ型付きエンベロープ（`candidate_outputs`）、そして決定論的なエンリッチメントフック（preprocessor）。

### 3. [Retrieval Engineering](retrieval-engineering.md)

適切なコンテキストを適切なタイミングで agent に渡すこと。Reyn にはプロジェクトスコープおよびユーザースコープのファクトに対する `recall_memory` があり、preprocessor ステップとして統合されています。これはシステムの中でも薄い領域の 1 つです。詳細はそのページを参照してください。

### 4. [Reliability Engineering](reliability-engineering.md)

障害からの回復: 検証、再プロンプト、ループ上限、タイムアウト。Reyn はすべての LLM 出力を次のターゲットのスキーマに対して検証し、拒否時には再プロンプトを行います。長いループは `limits.phase.max_visits` とフェーズごとのウォールクロックバジェットで制限されます。より豊富なリトライポリシーとチェックポイント/再開はロードマップに残っています。

### 5. [Security](security.md)

ケイパビリティのゲーティング、サンドボックス境界、トラスト スコーピング。三層の Permission モデル + 純粋な Python ステップの AST サンドボックス + Skill スコープの承認が核心です。

### 6. [Evaluation and Observability](evaluation-and-observability.md)

agent が機能しているかどうかを知り、その理由を把握すること。イベントログが「なぜ？」に答え、eval Skill が「機能しているか？」に答えます。どちらも第一級です。同じチャネルがデバッグレンダリング、リプレイ、eval アナリティクスを動かします。

### 7. [Product Think](product-think.md)

製品としての agent: CLI の使い勝手、コスト規律、予測可能な UX。モデルクラス（`light`/`standard`/`strong`）、ラン単位のコストレポート、`output_language` によるローカライゼーションが、Reyn が現在提供するレバーです。

## このセクションの読み方

- agent エンジニアリング一般が初めてですか? 順に読んでください。レンズは語彙を積み上げていきます。
- 別のフレームワークから来ていますか? 最も興味のあるレンズにスキップしてください。クロスリンクが必要に応じて他のレンズに引き戻します。
- 自分のシステムの自己評価をしていますか? Retrieval と Reliability の「まだ薄い部分」の記述が率直な箇所です。

## 関連情報

- [../architecture/principles.md](../architecture/principles.md) — これらのレンズを形作る 8 つの設計原則
- [../architecture/architecture.md](../architecture/architecture.md) — 完全なレイヤー図
- [../architecture/phase-vs-skill-vs-os.md](../architecture/phase-vs-skill-vs-os.md) — 責任の分割
