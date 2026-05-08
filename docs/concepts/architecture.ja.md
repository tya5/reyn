---
type: concept
topic: architecture
audience: [human, agent]
---

# アーキテクチャ概要

```
User → Agent → Skill → OS → Phase → Workspace
                  ↘ Event（すべてを記録）
```

## レイヤー

### Agent

ユーザーの意図を解釈します。Skill を選択または生成します。Phase を直接実行することはありません。

実際には、今日の「Agent」は CLI とチャットルーターです。どちらも薄い実装で、ユーザーの入力を Skill へとルーティングします。

### Skill

マークダウンと YAML ファイルのディレクトリです。Phase グラフと最終出力スキーマを定義します。実行可能コードは含みません（オプションの Python プリプロセッサーステップはサンドボックス内で動作します）。

### Phase

再利用可能な処理ユニットです。`input` と instructions だけを宣言します。

### OS

ランタイムの実行エンジンです。制御フローの唯一のオーナーです。[principles.md](principles.md) の P3 と P7 を参照してください。

### Workspace

データのシングルソースオブトゥルースです。すべてのファイル、ツール出力、artifact がここに置かれます。Phase は Control IR を通じて読み書きします。

### Artifact

Phase 間でやりとりされる構造化データです。`artifacts/*.yaml` で宣言されたスキーマに対してバリデーションされます。

### Event

すべての状態変化は event を発行します。デバッグおよび（将来的には）チェックポイントのために再生可能です。

## ランタイムループ

各 Phase 訪問ごとに：

1. コンテキストフレームを構築する（instructions + input + candidate outputs + control ops）。
2. プリプロセッサーステップがあれば実行する（決定論的 — `reference/dsl/preprocessor.md`、Phase 2）。
3. LLM を呼び出す。
4. 受け取る：`next_phase | finish`、artifact、オプションの Control IR ops。
5. OS ルールと選択した遷移先のスキーマに対して出力をバリデーションする。
6. Control IR ops を実行する（ファイル操作、ask_user、サブスキル呼び出しなど）。
7. workspace を更新する。
8. event を発行する。
9. 遷移または終了する。

## なぜこの形なのか

レイヤリングから 3 つの特性が生まれます：

- **再生可能性。** すべての状態変化が event であり、OS が唯一のミューテーターであるため、保存された event ログだけで同じワークフローを決定論的に再生できます（各 Phase 内の LLM の確率性は除く）。
- **Skill の移植性。** OS は特定の skill について何も知らないため（P7）、新しい skill を追加しても OS のコードは変わりません。Skill は純粋なデータと LLM が読める instructions です。
- **制約された LLM の創造性。** LLM は OS が提供した固定の遷移候補セットから選ぶため（P4）、不変条件を壊す制御フローを創造することはできません。

## 参考

- [principles.md](principles.md) — 8 つの制約
- [phase-vs-skill-vs-os.md](phase-vs-skill-vs-os.md) — 責務境界
- [Reference: control-ir](../reference/runtime/control-ir.md) — Control IR ops
- [Reference: events](../reference/runtime/events.md) — event 型
- [Agent engineering — 7 つのレンズ](../guide/for-skill-authors/agent-engineering/index.md) — 外部エンジニアリングの視点から見た同じアーキテクチャ
