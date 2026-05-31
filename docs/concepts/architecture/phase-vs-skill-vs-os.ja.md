---
type: concept
topic: architecture
audience: [human, agent]
---

# Phase vs. Skill vs. OS

reyn は責務を 3 つのレイヤーに分割します。この分割がシステムを拡張可能にします。新しい Skill は純粋なデータであり、OS コードの変更を必要としません。

## 分割

| レイヤー | 所有するもの | 知っていること |
|-------|------|-------------|
| **OS** | 制御フロー、バリデーション、events、Control IR ディスパッチ | Skill のドメインは何も知らない — DSL コントラクトのみ |
| **Skill** | Phase グラフ、entry phase、final output schema | 自分の Phase と artifact のみ |
| **Phase** | input artifact 型と LLM への instructions | 自分の `input` 以外は何も知らない |

### Phase：ステートレスで再利用可能

Phase は input と instructions を宣言します。Phase は **知りません**：

- 次に来る Phase が何か
- 自分の出力がどんな形か
- どの Skill に属しているか

これが `revise` のような Phase を Skill をまたいで再利用可能にするものです。`revise.md` の中には特定のドラフト生成者との結合はありません。

### Skill：構造のみ

Skill は「`entry` から、グラフはこれらの遷移を許可し、最終出力はこのようになる」と言います。自分ではコードを実行しません。Skill は OS が読むデータです。

### OS：Skill 不可知

OS は Skill のグラフを読み込み、LLM コンテキストを構築し、結果をバリデーションし、Control IR をディスパッチします。特定の Phase、artifact、フィールドを名指す文字列リテラルを一切含みません。新しい Skill が現れても OS コードは変わりません。

## 各変更の着地点

| 変更 | 着地点 |
|--------|----------|
| 「別の artifact フィールドを生成する」 | Phase instructions + 次の Phase の `input` スキーマ |
| 「リビジョンループを追加する」 | Skill の `graph`（`review → revise → review` を追加） |
| 「新しい control operation の種類を追加する」 | OS（新しい Control IR op） — すべての Skill が無料で使える |
| 「ユーザーの入力に基づいて 2 つの Skill を選択する」 | ルーター Skill（例：`skill_router`）。OS は Skill を選ばない |

## 「このレイヤーではない」という臭い

次のことをしたいと感じたとき：

- Phase の内側から隣接 Phase の名前を参照する → 間違ったレイヤー（P1）。接続を Skill グラフに移す。
- OS コードに特定の artifact 型をハードコードする → 間違ったレイヤー（P7）。代わりに Skill から型を読む。
- Phase instructions に制御フローロジックを埋め込む（「X なら Phase Y に行く」）→ 間違ったレイヤー（P8）。選択肢を `candidate_outputs` としてエンコードする。

## 一般的なワークフローシステムとの比較

| システム | 「Skill」に相当するもの | 「Phase」に相当するもの | 制御フローの場所 |
|--------|------------------------|------------------------|---------------------------|
| 命令型 agent（例：シンプルなプロンプトループ） | （なし） | （プロンプト全体） | LLM が次の呼び出しを自由に選ぶ |
| ステートマシン | 状態図 | 状態 | 図 |
| reyn | Skill フォルダー | Phase マークダウン | Skill のグラフ + LLM が許可エッジの中から選ぶ |

reyn 特有の重要な点：LLM は *エッジ* を選びますが、OS がグラフに対して選択をバリデーションします。LLM は実行中に新しいエッジを追加することはできません。

## 参考

- [../architecture/principles.md](../architecture/principles.md) — P1、P2、P3、P7
- [../architecture/llm-as-decision-engine.md](../architecture/llm-as-decision-engine.md)
- [Reference: skill.md](../../reference/dsl/skill-md.md)
- [Reference: phase.md](../../reference/dsl/phase-md.md)
- [Reference: graph](../../reference/dsl/graph.md)
