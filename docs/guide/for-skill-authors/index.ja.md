---
type: landing
topic: skill-authoring
audience: [human]
---

# Skill 開発者向け

Skill / agent / それを支えるランタイムを構築するためのタスク指向ハウツー集です。やりたいことに応じてクラスタを選んでください。

まだ済ませていなければ、[Getting started](../getting-started/01-installation.md) を先に進めてください。これらのハウツーは Reyn がインストール済みで、[コンセプト: phase vs skill vs OS](../../concepts/architecture/phase-vs-skill-vs-os.md) を一読していることを前提にしています。

## Foundation（基礎）

チュートリアルを終え、自分で Skill をオーサリングしたい人はここから。

- **[自作 skill をゼロから書く](foundation/write-your-first-custom-skill.md)** — `skill.md` / `phases/<name>.md` / `artifacts/<name>.yaml` を手書きで構築する完結した例。
- **[既存の Skill を import する](foundation/import-an-existing-skill.md)** — `skill_importer` でプロンプトや他フレームワークの仕様を Reyn DSL に持ち込む。

## Composition & multi-agent（合成・マルチエージェント）

Skill 同士、 agent 同士の組み立て方。

- **[`run_skill` で Skill を合成](composition/compose-skills-with-run-skill.md)** — Skill から別の Skill を呼び出す。
- **[fan-out で iterate](composition/iterate-with-fan-out.md)** — リストに対してサブステップを適用し結果を収集。
- **[Agent チームを構築](composition/build-an-agent-team.md)** — 役割ごとに Skill allowlist を持つ複数 agent をセットアップ。
- **[Multi-hop delegation](composition/multi-hop-delegation.md)** — 委任を複数 agent でチェイン。
- **[マルチステップタスクに Plan mode を使う](composition/use-plan-mode.md)** — 複雑な chat リクエストを非同期 step に分解し、crash recovery とオペレーター介入を実現する。
- **[Agent の Skill を制限](composition/restrict-agent-skills.md)** — チーム内 agent ごとに実行可能な Skill を絞る。

## Phase mechanics（Phase 内部）

Phase 内の決定論的コントロール — pre-LLM ステップ、 スキーマ検証、 state 永続化。

- **[Python preprocessor を追加](phase-mechanics/add-a-python-preprocessor.md)** — `safe` / `unsafe` モード、 関数シグネチャ、 sandbox 境界。
- **[Artifact を検証](phase-mechanics/validate-artifacts.md)** — strict モードのチェックとスキーマパターン。
- **[State を永続化](phase-mechanics/persist-state.md)** — 実行をまたいで何が残るか、 Workspace がどう保存するか。

## Operations（運用）

デバッグ、 外部サービス連携。

- **[Events でデバッグ](operations/debug-with-events.md)** — JSONL ログを読んで何が起きたかを理解する。
- **[MCP server を使う](operations/use-an-mcp-server.md)** — `mcp` Control IR op で外部ツールを phase に組み込む。

## UX & polish（仕上げ）

完成した Skill のユーザー側面。

- **[Design をオーサリング](ux-polish/author-a-design.md)** — Claude Design 連携でビジュアル artifact を作る。
- **[出力をローカライズ](ux-polish/localize-output.md)** — `output_language` と phase 単位のロケール扱い。
- **[音声入力を有効化](ux-polish/enable-voice-input.md)** — 音声駆動の chat モード。

## stdlib オーサリングツールと付き合う

LLM 駆動オーサリング stdlib（`skill_builder` / `skill_improver` / `skill_importer` / `eval_builder`）のルーブリックと参考資料。 主読者は Reyn の Skill ですが、 何かが想定どおり動かなかったときに人間も読みます。

- **[skill-builder checklist](stdlib-authoring-tools/skill-builder-checklist.md)**
- **[eval-builder rubric](stdlib-authoring-tools/eval-builder-rubric.md)**
- **[skill-importer mapping](stdlib-authoring-tools/skill-importer-mapping.md)**
- **[skill-improver criteria](stdlib-authoring-tools/skill-improver-criteria.md)**
- **[Glossary](stdlib-authoring-tools/glossary.md)** — ルーブリック横断の用語集。

## See also

- [コンセプト](../../concepts/architecture/principles.md) — これらのハウツーが使うパターンの「なぜ」。
- [Reference / DSL](../../reference/dsl/skill-md.md) — frontmatter と YAML スキーマの厳密な定義。
- [Reference / CLI](../../reference/cli/run.md) — `reyn run` / `reyn lint` / `reyn eval` / `reyn chat`。
