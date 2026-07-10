---
type: concept
topic: architecture
audience: [human, agent]
---

# Agent エンジニアリング — 8 つのレンズ

Reyn は 8 つのエンジニアリング視点を通じて読み解けます。各レンズは「このシステムは何を正しく実現しているか、そしてどこがまだ薄いか？」という問いに対する異なる見方です。同じドキュメントが複数のレンズから参照されており、このインデックスがその地図です。このページは `CLAUDE.md` の Constitution 節と [`docs/concepts/architecture/charter.md`](../architecture/charter.md)(7 つの feature family それぞれに 1 列を割り当てた、grounded な 8×7 グリッド全体)にある 8 レンズモデルを反映しています — このモデルの現行 canonical 版はその 2 つを読んでください。このページはレンズごとのナラティブな解説です。

## 全体像

```
 User
  │
  ▼
 Chat session ── router loop ──► LLM は以下から選択:
  │                                 Control IR ops(typed、schema-validated)
  │                                 Pipeline(deterministic DSL)
  │                                 Skill(段階的開示の instructions)
  │                                       │
  │                                       ▼
  │                          permission gate(exclude → permission → dispatch)
  │                                       │
  │                                       ▼
  │                                      OS ── op を実行
  │                                       │
  │                    ┌──────────────────┼──────────────────┐
  │                    ▼                  ▼                  ▼
  │               Workspace         WAL(crash-recovery /   P6 audit-event ログ
  │             (artifact、SSoT)      time-travel の基盤)     (実行ごとのトレース)
  ▼
 オペレーターに見えるサーフェス(CLI、ライブ audit chip、`reyn events` リプレイ)
```

各レイヤーには対応するエンジニアリングレンズがあります。レンズはシステムを分割するのではなく、意図的に重なり合います — 同じ機能が複数のレンズを grounding することがあります(charter.md の dual-facet ルールを参照)。

## 8 つのレンズ

### 1. [System Design](system-design.md)

マクロ構造: 制御フロー、状態、責任をレイヤーを横断してどのように分散させるか。現行の分割は LLM が決定し、OS が実行し、feature が自分のドメインを所有する、という形で、新しいレイヤー横断の結合はありません。

### 2. [Tool Contract Design](tool-contract-design.md)

LLM がどのように世界に作用するか: すべての副作用は typed で validated なエンベロープ(Control IR op)に乗り、LLM が自由形式で作る untyped な文字列にはなりません。

### 3. [Retrieval Engineering](retrieval-engineering.md)

適切なコンテキストを適切なタイミングで、決定論的に(`recall` + preprocessor ステップ)agent に渡すこと — プロンプトに無条件に詰め込むのではありません。これは憲章が明示する 2 つの honest thin area の 1 つです。

### 4. [Reliability Engineering](reliability-engineering.md)

障害からの回復: スキーマ検証 + 再プロンプト、優雅な force-close を伴う bounded loop、タイムアウト + opt-in の provider-retry。派生状態はすべて WAL truncation を生き延びます。

### 5. [Security](security.md)

permission-gated かつ sandbox-scoped: どのケイパビリティも gatekeeper を通過せずには世界に到達しません。

### 6. [Evaluation](evaluation.md)

run 内で rubric に対して出力をスコアリングする(`judge_output`: LLM スコアラー + threshold + `on_fail` ポリシー)。これは憲章が明示するもう 1 つの honest thin area です。

### 7. [Observability](observability.md)

何が起きたかを検査・再構築するのに十分な audit-event トレース(P6 audit ログ、`reyn events` リプレイ、ライブ audit chip)— 同じ「event」という語の WAL-event(crash-recovery)や hook-event(reactivity trigger)の意味とは鋭く区別されます。

### 8. [Product Think](product-think.md)

予測可能で、コスト規律があり、オペレーターにとって legible であること: CLI/CUI の使い勝手、コストレポート、そしてトークンコストの *削減*(例: ゼロトークンの `present`/offload)— これは cross-cutting band の `cost/budget (bounding)` メンバー(ハードキャップの機構であり、このレンズではありません)とは区別されます。

## Cross-cutting band

8 つのレンズのうち 3 つは、すべての feature がレンズに関わらず従う 5 つの band メンバーのいずれかを *universal mechanism* とする *discipline* を名指ししています: **permission**(Security)、**audit-events**(Observability)、**workspace-SSoT**、**crash-recovery/WAL**(Reliability)、**cost/budget bounding**(ハードキャップであり、Product Think のレポート/削減の側面とは別物)。band の完全な定義は `CLAUDE.md` の Constitution 節を参照してください — charter グリッドのすべての lens-cell が立脚する基盤です。

## このセクションの読み方

- agent エンジニアリング一般が初めてですか? まず `CLAUDE.md` の Constitution 節と [charter.md](../architecture/charter.md) を読んでください — それらが現行の grounded なモデルです。このページにはレンズごとのナラティブな解説のために戻ってきてください。
- 別のフレームワークから来ていますか? 最も興味のあるレンズにスキップしてください。クロスリンクが必要に応じて他のレンズに引き戻します。
- 自分のシステムの自己評価をしていますか? 「まだ薄い部分」の記述 — 特に憲章の 2 つの honest thin area である Retrieval と Evaluation — が率直な箇所です。
- **staleness についての注記**: この 8 つのレンズページのうち 2 つ(Security、Product Think)は、以前の engine 削除 arc で削除された phase-graph skill engine を前提に書かれており、それぞれ何が stale で何が現行かを明示する状態バナーをページ冒頭に持っています。Tool Contract Design、System Design、Retrieval Engineering、Reliability Engineering はすでに完全な書き直しを経ています。Evaluation と Observability は現行モデルに対して新規に書かれました。残り 2 ページの完全な de-drift パスは follow-up として、1 ページずつ追跡されています — `charter.md` 自体が family ごとに構築された方法をなぞっています。

## 関連情報

- `CLAUDE.md`(§ Constitution)— 8 レンズの pass-line + cross-cutting band、canonical
- [`docs/concepts/architecture/charter.md`](../architecture/charter.md) — `docs/feature-map.md` に grounded された完全な 8×7 グリッド
