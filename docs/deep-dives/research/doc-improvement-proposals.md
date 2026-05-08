---
title: ドキュメント改善提案
created: 2026-05-08
source: Researcher セッション（競合分析 + 実装確認）
status: actionable
---

# ドキュメント改善提案

Researcher セッションで競合分析・実装確認を行った結果、判明したドキュメントの問題を
優先度順にまとめる。

発見の経緯: 競合分析を行うにあたり Reyn の実装・ドキュメントを読んだ結果、
「実装は存在するが docs に記載がない」ケースが複数見つかった。
これらは外部からの評価を歪める直接的なリスク。

---

## P1: 即修正が必要（実装と docs の乖離）

### 1-A. `control-ir.md` に `web_search`・`web_fetch` op が未記載

**問題**: `docs/en/reference/runtime/control-ir.md`（および ja 版）の Op kinds テーブルと
個別セクションに `web_search`・`web_fetch` が存在しない。実装は
`src/reyn/op_runtime/web.py` に完全実装済み（`WebSearchIROp`・`WebFetchIROp`）。

**影響**: Phase 著者が「LLM に web 検索・URL 取得を要求できる」ことを知れない。
Researcher が docs を読んで「file ops と shell しかない」と誤判定した直接原因。

**修正箇所**:
- `docs/en/reference/runtime/control-ir.md` — Op kinds テーブルに 2 行追加 + セクション 2 つ追加
- `docs/ja/reference/runtime/control-ir.md` — 同上

**追加すべき内容（en 版）**:

Op kinds テーブルへの追加:
```
| `web_search` | Search the web via DuckDuckGo | none |
| `web_fetch`  | Fetch and extract text from a URL | none |
```

`web_search` セクション:
```markdown
## `web_search`

Searches the web using DuckDuckGo. Returns a list of results with titles,
URLs, and snippets.

​```json
{
  "kind": "web_search",
  "query": "LangGraph multi-agent patterns 2025",
  "max_results": 10
}
​```
```

`web_fetch` セクション:
```markdown
## `web_fetch`

Fetches a URL and returns extracted plain text. HTML tags are stripped.
Use `prompt` to hint what to extract (informational only).

​```json
{
  "kind": "web_fetch",
  "url": "https://example.com/docs",
  "prompt": "extract the API reference section",
  "max_length": 50000
}
​```
```

**コスト**: small（30 分以内）

---

### 1-B. `models.py` の outdated コメントと `tool`/`subagent` デッドコードの整合

**問題**: `src/reyn/schemas/models.py` line 300 のコメントに
`"file", "ask_user", "shell", "lint", "run_skill", "web_fetch", "web_search" are implemented; others are safely skipped.`
とあるが、`mcp` は既に実装済みであり、コメントが古い。
また `ToolIROp`・`SubAgentIROp` および `registry.py` の `"tool"`・`"subagent"` エントリはデッドコードと確認済み（削除予定）。

**影響**: デッドコード削除 PR 時にコメントを合わせて更新しないと、将来の読者が混乱する。

**修正箇所**: デッドコード削除 PR に以下を含める:
- `models.py` line 300 コメントを実装済み op の正確なリストに更新
- `ToolIROp`・`SubAgentIROp` モデル定義を削除
- `registry.py` の `"tool"`・`"subagent"` エントリを削除
- `control-ir.md` の Op kinds テーブルに `mcp` を追加（1-A と合わせて対応）

**コスト**: small（デッドコード削除 PR に同梱）

---

## P1: OSS ローンチ前に必要（Skill Authoring の道筋欠落）

### 2-A. Skill Authoring ガイドが存在しない

**問題**: `tutorials/01〜05` で「30 分で動く」は達成済み。
しかし「自分の skill を書く」への道案内が完全に欠落している。

具体的に欠けているもの:
- `skill.md` の全フィールドを説明するオーサリングテンプレート
  (`reference/dsl/skill-md.md` は存在するが「どう書くか」の実践的ガイドがない)
- Phase Best Practices（`model_class` 選択・`allowed_ops` 設定・instructions の書き方）
- Design Patterns（線形スキル・ループスキル・サブスキル呼び出しスキルの典型例）
- 良い skill の見本として `read_local_files`・`skill_builder` を案内するポインタ

**影響**: tutorials → 自作 skill という最も重要なジャーニーが docs でサポートされない。
競合（CrewAI/LangGraph/LangChain）は全て豊富な cookbook と設計ガイドを持つ。

**推奨アクション**（新規ファイル）:
1. `docs/en/how-to/write-your-first-custom-skill.md`
   — tutorial 02（your-first-skill）の次に来る「自分でゼロから書く」 how-to
   — skill.md の必須フィールド・phase.md の基本構造・allowed_ops の選び方・lint の使い方
2. `docs/en/concepts/skill-design-patterns.md`
   — 線形（read → process → write）/ ループ（generate → review → refine）/ 委譲（sub-skill 呼び出し）の 3 パターン
   — 実在するスキル（read_local_files / skill_builder）をリンクして説明
3. 日本語版（対応する ja/ ファイル）

**コスト**: medium（3〜5 日）

---

### 2-B. `architecture.md` にコード例がない

**問題**: `docs/en/concepts/architecture.md` と ja 版は設計思想の記述が充実しているが、
コード例がゼロ。「Phase とは」「Skill とは」を理解した後で「実際にどう書くか」への
接続がない。

**影響**: concepts は読んだが実装できないという状態が生まれる。
「理解した → 実装できない」断絶。

**推奨アクション**:
- 最小 skill（2 フェーズ）の構造を示すコードスニペットをアーキテクチャ図の横に追加
- 各レイヤー（OS / Skill / Phase / Control IR）の実コードとの対応表を追加
- `read_local_files` を「最もシンプルな実例」として冒頭でリンク

**コスト**: small（半日〜1 日）

---

### 2-C. Phase Preprocessor の全体像が docs にない

**問題**: `reference/dsl/preprocessor.md` は DSL 構文のリファレンスとして存在するが、
「どんな preprocessor 種別があり、それぞれ何ができるか」の概観ドキュメントがない。
`CLAUDE.md` は「stdlib skills の実装を読め」と指示するのみ。

利用可能な preprocessor 種別:
- `run_op` — Control IR op を Phase 実行前に確定論的に実行
- `iterate` — リスト入力を展開してループ
- `validate` — スキーマ検証
- `lint_plan` — plan DSL の lint
- `python` — 任意 Python 関数

**影響**: Phase Preprocessor は「LLM に委ねなくてよい処理を LLM から切り離す」Reyn の
差別化設計の核心（P3 + deterministic_split 原則）。ドキュメントがなければ
Skill 著者が活用できず、LLM に委ねるべきでない処理まで LLM に流してしまう。

**推奨アクション**:
- `docs/en/concepts/postprocessor.md` に相当する `docs/en/concepts/preprocessor.md` 新規作成
  （「なぜ確定論的処理を LLM から切り離すか」の設計思想 + 各種別の用途早見表）
- `reference/dsl/preprocessor.md` はそのまま DSL リファレンスとして残す
- 日本語版も同様

**コスト**: small（半日〜1 日）

---

### 2-D. `concepts/mcp.md` が MCP server / client の役割を区別していない

**問題**: `docs/en/concepts/mcp.md`（および ja 版）が、
Reyn の MCP における 2 つの役割を明確に区別していない。

| 役割 | 実装状況 | 説明 |
|---|---|---|
| **MCP server** | 実装済み | `reyn mcp serve` — 外部 LLM クライアント（Claude Code 等）から Reyn を呼べる |
| **MCP client** | Phase 2 | `mcp` Control IR op — Reyn から外部 MCP server を呼ぶ |

**影響**: Researcher がこのドキュメントを読んで「MCP は未実装」と誤判定した直接原因。
OSS ローンチ後に同様の誤認が広まる可能性が高い。
「Reyn は Claude Code から呼べる」という killer feature が認識されない。

**推奨アクション**:
- `concepts/mcp.md` の冒頭に上記の 2 役割表を追加
- MCP server（実装済み）と MCP client（Phase 2 予定）をセクション分けして記述
- `reyn mcp serve` の起動手順と使用例を追加（現状ゼロ）

**コスト**: small（2〜3 時間）

---

## P1: OSS ローンチ前に必要（日本語版 missing ファイル）

### 3-A. 英語版のみ存在し日本語版がないファイル（11 件）

以下ファイルが `docs/en/` にあるが `docs/ja/` に存在しない。

| ファイル | 優先度 | 理由 |
|---|---|---|
| `concepts/a2a.md` | **高** | README で言及。日本ユーザーが最初に確認する機能 |
| `concepts/mcp.md` | **高** | README で言及。Phase 2 の核心機能 |
| `how-to/use-an-mcp-server.md` | **高** | MCP 接続の実装ガイド。Phase 2 対応後の主要コンテンツ |
| `reference/upgrade-policy.md` | **高** | バージョン移行で日本ユーザーが最初に詰まる箇所 |
| `concepts/postprocessor.md` | **中** | postprocessor は実装済みの機能 |
| `concepts/skill-resume.md` | **中** | WAL クラッシュ回復の概念説明 |
| `how-to/author-a-design.md` | **中** | デザイン作成の how-to |
| `reference/dsl/postprocessor.md` | **中** | postprocessor DSL リファレンス |
| `reference/stdlib/read_local_files.md` | **中** | stdlib skill のリファレンス |
| `reference/testing/replay.md` | **低** | テスト向け詳細。初期ユーザーへの影響は限定的 |
| `reference/dogfood-tracing.md` | **低** | 開発者向け。初期ユーザーへの影響は限定的 |

**推奨アクション（OSS ローンチ前）**:
- 高優先度の 4 ファイルを翻訳
- 中優先度 4 ファイルはローンチ後第 1 スプリントで対応

**コスト**: 高優先度 4 ファイル = small（1〜2 日）

---

## P2: OSS ローンチ後（競合対比で見えやすさに影響）

### 4-A. マルチエージェントの 4 層構造が docs に明文化されていない

**問題**: `docs/en/concepts/multi-agent.md` と `how-to/build-an-agent-team.md` は存在するが、
Reyn のマルチエージェントが以下 4 層で構成されていることが一目でわかる記述がない。

```
Layer 1: @sub_skill graph node（静的グラフ内スキル埋め込み）
Layer 2: run_skill Control IR op（動的サブスキル呼び出し）
Layer 3: delegate_to_agent（エージェント間メッセージング・ホップ深度制限付き）
Layer 4: reyn mcp serve（外部 LLM クライアントから Reyn を呼ぶ MCP server）
```

**影響**: 競合（AutoGen/CrewAI/LangGraph）はマルチエージェントのアーキテクチャ図を
トップページ級のコンテンツとして扱っている。Reyn の「P4/P6 制約が multi-agent 全経路で
維持される」という genuine な差別化が外部から認識されない。

**推奨アクション**:
- `concepts/multi-agent.md` の冒頭に 4 層構造の概要図（ASCII art または Mermaid）を追加
- 各層が「なぜこの設計か」の 1 行説明を追加（P4/P5/P6 との対応を示す）
- 日本語版も同様に更新

**コスト**: small（半日）

---

### 4-B. README の競合比較・差別化訴求が欠落

**問題**: Reyn の README（プロジェクトルート）に競合との比較表がない。
「なぜ LangGraph/CrewAI ではなく Reyn なのか」の答えが README に書かれていない。

**影響**: OSS ローンチ後に GitHub ページを訪れた開発者が「これは何が違うのか」を
判断する情報がない。

**推奨アクション**:
- README に「Reyn vs alternatives」セクション（3〜4 行の簡潔な比較表）を追加
- 「Predictability over autonomy」のユースケース（日本エンタープライズ向け）を明示

**コスト**: small（半日）

---

### 4-C. Phase 実行フロー（LLM 出力 → Control IR 実行）のシーケンス図がない

**問題**: 「LLM が `control_ir` を出力してから実際に実行されるまでの流れ」を図示したドキュメントがない。
個別の概念ドキュメント（`events.md`, `workspace.md`, `control-ir.md`）は存在するが、
1 回の Phase 実行の全体シーケンスが追えない。

具体的に不明なフロー:
1. OS が LLM を呼ぶ（何を渡すか）
2. LLM が JSON を返す（`control` + `artifact` + `control_ir`）
3. OS が `control_ir` ops を順次実行する
4. 結果を workspace に書き込む（P5）
5. イベントを emit する（P6）
6. 次フェーズへ遷移する（または finish）

**影響**: 「点（個別ドキュメント）はあるが線（つながり）がない」状態。
アーキテクチャに興味ある読者（競合評価者・新規コントリビューター）が
Reyn の設計の実際を把握できない。
`architecture.md` の理解と実装の接続が困難。

**推奨アクション**:
- `concepts/architecture.md` に Phase 実行のシーケンス図（Mermaid sequence diagram）を追加
  または `docs/en/concepts/phase-lifecycle.md` として新規作成
- OS の各ステップ（context build → LLM call → validation → IR execution → event emit → transition）を可視化

**コスト**: small（半日）

---

### 4-D. `control-ir.md` への `mcp` op（Phase 2 後）の追加

**問題**: `MCPIROp`（`kind: "mcp"`）は `models.py` に定義済みだが未実装
（Phase 2 ロードマップ）。MCP client 実装後、docs に追加が必要。

**推奨アクション**: MCP client 実装 PR に `control-ir.md` の `mcp` セクション追加を含める。
「実装後に docs を書く」ではなく「実装 PR に docs を含める」ルールを徹底。

**コスト**: MCP client 実装時に同時対応（追加コストなし）

---

## 発見方法と再発防止

### なぜこれらの問題が生まれたか

1. **実装が先行し docs が追随しない**: `web_search`/`web_fetch` は実装済みだが
   `control-ir.md` に記載されなかった。`models.py` のコメントに記載はあるが
   docs として公開されていない。

2. **en/ と ja/ の同期がない**: 英語で新機能 doc を書いた後、日本語翻訳の
   タスク追跡がされていない。

3. **「動かした人」と「書いた人」の分離**: tutorials は動くが、
   その先の how-to を書く担当者がいない。

### 再発防止の提案

- 新機能実装 PR には対応する `reference/` ドキュメント更新を **必須チェックリスト** に含める
- `docs/en/` に新規ファイルを追加する際は対応する `docs/ja/` ファイルを同 PR または
  直後の PR で作成することを CONTRIBUTING.md に明記
- `control-ir.md` は `src/reyn/schemas/models.py` の `ControlIROp` union 定義と
  常に同期することを CLAUDE.md に追記する

---

## 改善後の期待効果

| 改善項目 | 期待効果 |
|---|---|
| control-ir.md web ops 追加 | Phase 著者が web_search/web_fetch を活用できる。stdlib スキルの品質が上がる |
| Skill Authoring ガイド | tutorials → 自作 skill のコンバージョン率向上。新規コントリビューター獲得 |
| Phase Preprocessor 概観 | 確定論的処理を LLM から切り離すパターンが普及する |
| concepts/mcp.md 役割分担 | MCP server（実装済み）が killer feature として認識される |
| 日本語版 4 ファイル | 日本エンタープライズの社内提案を容易にする |
| multi-agent 4 層図 | 競合比較での訴求力向上。「閉じた OS」という誤解の払拭 |
| Phase 実行シーケンス図 | アーキテクチャ興味読者が設計を正確に把握できる |
| README 比較表 | GitHub 流入時の離脱率低下 |
