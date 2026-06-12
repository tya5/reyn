---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [skill_router]
---

# `skill_router`

ユーザー（またはピア agent）の発話を、適切な Skill・agent・直接返答へルーティングします。`reyn chat` が毎ターン使用します。

## 実装

router はネイティブツールを用いた `RouterLoop` として実装されており（通常の skill ディレクトリと LLM Phase 遷移グラフではありません）、毎ターン会話履歴と利用可能なディスパッチパス（Skill 実行・agent デリゲーション・直接返答）を表すネイティブツールセットを受け取り、LLM がツールを選択してターンが解決されるまでループします。

メモリ書き込み（ユーザー／フィードバック／プロジェクト／参照情報の永続化）はループ内で LLM が `file/write` op を発行したときに行われます。メモリの読み込みはループ実行前に ChatSession が事前にマージします（[concepts/memory](../../concepts/data-retrieval/memory.md) を参照）。

## ソース

[`src/reyn/chat/router_loop.py`](https://github.com/tya5/reyn/blob/main/src/reyn/chat/router_loop.py) — 通常の skill ディレクトリではなく、組み込みシステム skill として実装されています

## 関連情報

- [コンセプト: memory](../../concepts/data-retrieval/memory.md) — 2 層の読み書きコントラクト
- [コンセプト: multi-agent](../../concepts/multi-agent/multi-agent.md) — `messages_to_agents` とチェーンのセマンティクス
- [リファレンス: profile-yaml](../dsl/profile-yaml.md) — `allowed_skills` フィルター
- [リファレンス: chat CLI](../cli/chat.md)
