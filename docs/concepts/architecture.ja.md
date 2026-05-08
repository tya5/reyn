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

## act-sense-react レンズから見た Reyn

agent コミュニティでは、「agent とは何か」の実用的な定義として、**act → sense → re-act のフィードバックループ**という概念への収束が見られます — システムが世界に影響を与え、その影響を感知し、追加のアクションを選択できることが最低要件だというものです。この framing は Tines のブログ記事 ["What, exactly, is an 'AI Agent'? Here's a litmus test"](https://www.tines.com/blog/a-litmus-test-for-ai-agents/) と、それに続く HN ディスカッションで複数のコメンターが独立して同じループの定式化に辿り着いたことで広く知られるようになりました。

Reyn はこのループを名目上ではなく **構造的に** 実装しています。ループの各ステップは具体的な primitive に対応します：

| ループのステップ | Reyn の primitive |
|-----------------|-------------------|
| **act** | Phase が `control_ir` を出力 — OS がディスパッチする LLM の決定 |
| **sense** | Workspace と Events を次の Phase の context frame が読み込む |
| **re-act** | LLM が新しい context で次の transition と artifact を生成する |
| **ループの閉性** | Skill graph の `transitions` と finish condition |

この対応が構造的であることが、ループが暗黙的な他のフレームワークとの違いです。多くの agent システムでは、「sensing」とは LLM がたまたま読んだもの、「acting」とは LLM がたまたま呼び出したツール、ループはLLM が続けると決めたから閉じる — という具合になっています。Reyn は各ステップを明示的かつ OS が所有するものとして定義します：

- Workspace が唯一の sensing チャネル — LLM が見るのは OS が context frame に組み込んだものだけです。
- Events が唯一の audit 記録 — すべての sense-act サイクルに再生可能なトレースが残ります ([events.md](events.md))。
- Control IR が唯一の acting 語彙 — LLM は宣言された op セット外の新しい操作を作れません。
- Skill graph が唯一の re-act パス — LLM は OS が検証した transition の中から選択し、実行中に新しいエッジを追加できません ([principles.md](principles.md#p3-os-controls-execution))。

これが [P3 (OS controls execution)](principles.md#p3-os-controls-execution) をループの framing で具体化したものです: OS がループ構造を所有し、LLM がその中で決定を行います。

LangGraph・AutoGen・Semantic Kernel といった他の agent フレームワークに慣れている読者にとって、この対応は直接的な置き換えマッピングを提供します。それらのシステムがループをプログラム可能な surface として公開しているのに対し、Reyn はそれを検証済みの runtime contract としてエンコードします。LLM の役割はどのシステムでも同じ (次のステップを決定すること) ですが、ループの境界がコードによって強制されるか、慣習に委ねられるかが異なります。

## 参考

- [principles.md](principles.md) — 8 つの制約
- [phase-vs-skill-vs-os.md](phase-vs-skill-vs-os.md) — 責務境界
- [workspace.md](workspace.md) — Workspace の詳細
- [events.md](events.md) — Events の詳細
- [Reference: control-ir](../reference/runtime/control-ir.md) — Control IR ops
- [Reference: events](../reference/runtime/events.md) — event 型
- [Agent engineering — 7 つのレンズ](agent-engineering/index.md) — 外部エンジニアリングの視点から見た同じアーキテクチャ
