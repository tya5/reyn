---
type: how-to
topic: dsl
audience: [human]
applies_to: [phases/*.md, skill.md]
---

# `run_skill` で Skill を組み合わせる

**目的:** Skill の内部から別の Skill を呼び出す。LLM の前に決定論的に（preprocessor）、または LLM 駆動の副作用として（Control IR）。

## 2 つの方式

| 方式 | 実行タイミング | 制御 |
|--------|---------------|---------|
| **Preprocessor** `run_skill` | LLM の前、各 Phase 訪問ごと | Skill 作者が決定 |
| **Control IR** `run_skill` | LLM の後、LLM が要求したとき | LLM が決定 |

依存関係が構造的な場合（この Phase は常にその Skill の出力が必要）は preprocessor を選びます。LLM が呼び出しを判断すべき場合は Control IR を選びます。

## Preprocessor パターン

```yaml
---
type: phase
name: write_post
input: post_request
preprocessor:
  - run_skill:
      skill: recall_memory
      input:
        type: user_message
        data: { text: "what does the user prefer about post format?" }
      into: relevant_memories
---

投稿を書いてください。`relevant_memories` に記述された好みがあれば
それに従い、なければデフォルトの 3 段落構成にしてください。
```

サブ Skill は完了まで実行されます。その `final_output` artifact は `input.relevant_memories` に束縛されます。Phase はそれを他の入力フィールドと同様に読み取ります。

## Control IR パターン

LLM 駆動の呼び出しの場合、preprocessor には何も宣言しません。OS が `run_skill` を `available_control_ops` に注入し、LLM は決定したときに op を出力します:

```json
{
  "kind": "run_skill",
  "skill": "recall_memory",
  "input": {"type": "user_message", "data": {"text": "..."}}
}
```

Phase の指示は LLM がサブ Skill をいつ呼び出すべきかを記述します。op の構文は記述しません（P8）。例: 「ユーザーの好みが不明な場合は、`recall_memory` を呼び出して確認してください。」

## グラフ内のサブ Skill ノード

グラフのエントリーに Skill を直接参照させることもできます:

```yaml
graph:
  prepare:        [@my_subskill]
  '@my_subskill': [aggregate]
  aggregate:      [end]
```

これは `run_skill` より重いです: サブ Skill は一度限りの副作用ではなくグラフのノードになります。親のフローが特定の時点でサブ Skill を待つ場合に使用します。

## Skill の解決順序

3 つのパターンすべてが同じ方法で名前を解決します:

1. `reyn/project/<name>/skill.md`
2. `reyn/local/<name>/skill.md`
3. `src/stdlib/skills/<name>/skill.md`

## 関連情報

- [リファレンス: preprocessor](../../reference/dsl/preprocessor.md) — `run_skill` ステップ
- [リファレンス: control-ir](../../reference/runtime/control-ir.md) — `run_skill` op
- [リファレンス: graph](../../reference/dsl/graph.md) — サブ Skill ノード
- [iterate-with-fan-out.md](iterate-with-fan-out.md)
