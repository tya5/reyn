---
type: landing
audience: [human]
---

# reyn

**LLM ワークフロー OS — Phase の状態遷移として実行する。**

reyn は LLM ワークフローを「状態機械」として実行します。Phase は状態を持たず再利用可能、Skill が graph と最終出力スキーマを所有し、OS が実行を制御します。LLM の役割は **OS が提示した遷移候補から選ぶこと**、そして構造化されたアーティファクトを返すことに限定され、制御フローを発明することはありません。

## どこから読む？

| 目的 | 行き先 |
|---|---|
| インストールして最初の skill を動かす | [Getting started](guide/getting-started/01-installation.md) |
| 仕様を正確に知りたい | [Reference](reference/cli/run.md) |
| 設計思想を理解したい | [コンセプト](concepts/architecture/principles.md) |

!!! note "翻訳状況"
    日本語版は主要ドキュメントから順次翻訳中です。未訳のページは英語版にフォールバックします。

## Powered by AI

reyn は 2 つの意味で「 AI 駆動」 です:

- **ランタイム**: すべての Skill 実行は LiteLLM 経由で LLM プロバイダに判断を委譲します。 reyn は設計上 LLM ワークフロー OS です。
- **開発**: コードベース、 stdlib スキル、 本ドキュメント、 ランディングページの相当部分は AI ツール (= 主に Claude Code (Anthropic) による実装、 Claude Design による Web サイト) で起草されました。 人間によるレビュー / 統合 / 最終的なアーキテクチャ判断はメンテナが担い、 AI の寄与は git 履歴の `Co-Authored-By: Claude ...` trailer として記録されています。

これはマーケティングコピーではなく透明性のための開示です。 来歴を監査する場合は `git log --grep="Co-Authored-By: Claude"` および `website/_design/` 配下のデザインプロンプトを参照してください。
