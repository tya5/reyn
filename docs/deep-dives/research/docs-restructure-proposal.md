---
title: Docs Restructure Proposal
status: proposal
date: 2026-05-08
---

# ドキュメント体系 再構成提案

## 背景と問題意識

現状のドキュメントは「言語軸（`en/` / `ja/`）が第1階層」になっており、
読者ロール別の動線が存在しない。

**4つの読者ロール:**

| ロール | 主な関心 |
|---|---|
| Reyn 開発者 | contributing, ADR, concepts 深掘り, journal |
| Skill 開発者 | tutorials, how-to, reference/dsl, reference/runtime |
| チャットユーザ | 起動方法, chat-mode, 権限設定 |
| アーキテクチャ興味 | concepts, decisions, research/positioning |

**現状の構造上の問題:**

1. `docs/agent/` — skill-builder-checklist 等が `en/ja` 階層の外に浮いている
2. `en/concepts/agent-engineering/`（7本）は OS 説明でなく Skill 設計ベストプラクティスだが concepts/ に混在
3. `journal/`, `research/`, `openui/`, `web/` が利用者向けドキュメントと同列に並んでいる
4. ADR が `en/decisions/` に入っている（言語非依存なのに言語ディレクトリ配下）
5. `en/` にあって `ja/` にない未翻訳ファイルが 11 本（ギャップが不可視）

---

## 提案構成

コンテンツ種別を第1階層にする。

```
docs/
├── guide/
│   ├── getting-started/      ← 現 tutorials/（5本）
│   ├── for-users/            ← チャットユーザ向け how-to
│   └── for-skill-authors/    ← Skill 開発者向け how-to + 現 docs/agent/ を吸収
├── concepts/                 ← 現 concepts/ + agent-engineering/ をフラット化（23本）
├── reference/
│   ├── cli/                  ← 現状のまま（7本）
│   ├── config/               ← 現状のまま（4本）
│   ├── dsl/                  ← 現状のまま（8本）
│   ├── runtime/              ← 現状のまま（4本）
│   ├── stdlib/               ← 現状のまま（8本）
│   ├── testing/              ← 現状のまま（1本）
│   ├── builtin-models.md
│   ├── dogfood-tracing.md
│   └── upgrade-policy.md
└── deep-dives/
    ├── decisions/            ← 現 en/decisions/（ADR 25本 + README + discussion-log）
    ├── contributing/         ← 現 en/contributing/ + docs/contributing/cli-redesign.md
    ├── journal/              ← 現 docs/deep-dives/journal/（dogfood / insights / feature-verify）
    ├── research/             ← 現 docs/deep-dives/research/（competitive / landscape / positioning）
    └── spec/
        ├── openui/           ← 現 docs/deep-dives/spec/openui/
        └── design/           ← 現 docs/deep-dives/spec/design/
```

### 3階層フルマップ

**guide/**

    guide/
    ├── getting-started/
    │   ├── 01-installation.md
    │   ├── 02-your-first-skill.md
    │   ├── 03-running-a-skill.md
    │   ├── 04-writing-an-eval.md
    │   └── 05-chat-mode.md
    ├── for-users/
    │   ├── manage-permissions.md
    │   └── ask-user-mid-phase.md
    └── for-skill-authors/
        ├── import-an-existing-skill.md
        ├── compose-skills-with-run-skill.md
        ├── iterate-with-fan-out.md
        ├── add-a-python-preprocessor.md
        ├── validate-artifacts.md
        ├── persist-state.md
        ├── debug-with-events.md
        ├── build-an-agent-team.md
        ├── multi-hop-delegation.md
        ├── restrict-agent-skills.md
        ├── use-an-mcp-server.md
        ├── localize-output.md
        ├── author-a-design.md
        ├── skill-builder-checklist.md   ← 現 docs/agent/
        ├── eval-builder-rubric.md        ← 現 docs/agent/
        ├── skill-importer-mapping.md     ← 現 docs/agent/
        └── skill-improver-criteria.md    ← 現 docs/agent/

**concepts/** （フラット、23本）

    concepts/
    ├── architecture.md
    ├── principles.md
    ├── phase-vs-skill-vs-os.md
    ├── care-boundary.md
    ├── workspace.md
    ├── events.md
    ├── llm-as-decision-engine.md
    ├── permission-model.md
    ├── multi-agent.md
    ├── a2a.md
    ├── mcp.md
    ├── memory.md
    ├── topology.md
    ├── plan-mode.md
    ├── postprocessor.md
    ├── skill-resume.md
    ├── system-design.md                  ← 現 agent-engineering/
    ├── retrieval-engineering.md           ← 現 agent-engineering/
    ├── reliability-engineering.md         ← 現 agent-engineering/
    ├── evaluation-and-observability.md    ← 現 agent-engineering/
    ├── tool-contract-design.md            ← 現 agent-engineering/
    ├── security.md                        ← 現 agent-engineering/
    └── product-think.md                   ← 現 agent-engineering/

---

## i18n 方針の変更

### 現状

`en/` と `ja/` を完全ミラーする構造。言語が第1階層に固定されている。

### 提案

mkdocs-material の **suffix i18n** に移行:

- `file.md` = 英語（デフォルト）
- `file.ja.md` = 日本語オーバーライド（存在する箇所のみ）
- 日本語版がない場合は英語にフォールバック

**メリット:**

- 翻訳漏れが `.ja.md` の有無で即座に可視化される
- 現状 11 本の未翻訳ファイルを意識せずに放置できない構造になる
- コンテンツの単一ソース（en/ と ja/ に同一内容が 2 ファイル存在する状態が解消）
- ADR・journal 等の言語非依存コンテンツを言語ディレクトリの外に自然に置ける

---

## 実施タイミング

**OSS リリース前（Phase 3）に実施推奨。**

公開後に実施するとすべての外部リンクが切れる。
Phase 3 のタスクとして明示的に計画に入れることを提案する。

---

## 実施スコープ

1. ディレクトリ・ファイルの移動（git mv）
2. mkdocs.yml の nav 再構成
3. i18n プラグイン設定を suffix モードに変更
4. 内部リンク（`../concepts/architecture/architecture.md` 等）の一括修正
5. CLAUDE.md のドキュメントパス参照を更新

スコープは大きいが機械的作業が中心。サブエージェント並列で処理可能。
