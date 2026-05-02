---
type: how-to
topic: multi-agent
audience: [human]
applies_to: [profile.yaml, allowed_skills]
---

# agent の Skill セットを制限する

**目的:** agent のルーターが選択できるプロジェクト/stdlib の Skill を制限する。専門化して集中すべき specialist agent や、オープンエンドなツールを呼び出すべきでない本番 agent に有用。

## 使うべき状況

- `researcher` agent は `article_writer` を呼び出すべきでない。
- 単一ワークフロー専用の agent が、隣接するツールに誘惑されたり（または幻覚で）入り込むべきでない。
- 20 の Skill が利用可能な場合にルーター LLM が何を選ぶか不安。

## 制限しないもの

`allowed_skills` は以下には**影響しません**:

- **stdlib システム Skill** — `skill_router`、`chat_compactor`、`skill_narrator`。これらは常に利用可能です。`allowed_skills: []` の agent でも chat とナレーションができます。
- **agent 間の委任** — `messages_to_agents` は Topology ルールによって管理され、Skill の allowlist によらない。Skill がない agent でも Skill を持つピアに委任できます。
- **Memory アクセス** — 共有と agent スコープの両方の Memory レイヤーは読み書き可能です。

## レシピ

`allowed_skills` は agent の `profile.yaml` に記述します。まだ CLI フラグはありません（残課題）。ファイルを直接編集してください。

### 1. ファイルを見つける

```bash
$EDITOR .reyn/agents/researcher/profile.yaml
```

### 2. フィールドを追加する

```yaml
name: researcher
role: |
  deep technical research, prefers primary sources.
created_at: 2026-05-01T12:00:00+00:00
allowed_skills:
  - web_search
  - recall_docs
  - text_summarizer
```

ファイルを保存します。次の `reyn chat researcher` が起動時に変更を反映します。

### 3. 確認する

```bash
reyn agent show researcher
```

```
name:        researcher
created_at:  2026-05-01T12:00:00+00:00
workspace:   /path/to/project/.reyn/agents/researcher
allowed_skills:
  - web_search
  - recall_docs
  - text_summarizer
role:
  deep technical research, prefers primary sources.
```

## 3 つの状態

`allowed_skills` は三値です。各状態は明確な動作を持ちます:

| 値 | 動作 |
|-------|----------|
| フィールドなし / `null` | **無制限。** すべてのプロジェクト + stdlib Skill がルーター LLM に提供されます。（新規 agent のデフォルト。） |
| `[]`（空リスト） | **ルーターのみ。** Skill の起動は行われません。ルーターは直接返信またはピアへの委任はできます。「純粋な会話」agent に有用。 |
| `[a, b, c]` | **Allowlist。** それらの Skill 名のみが提供されます。 |

意図的に会話的な agent の例:

```yaml
name: lead
role: triages requests and synthesizes worker output.
allowed_skills: []  # Skills を直接起動しない; 常に委任または直接返信
```

## 二重レイヤーの強制

1. **ルーター側のフィルター** — `_invoke_router` は LLM がカタログを見る前に `available_skills` を allowlist に絞り込みます。LLM はブロックされた Skill を知ることができません。
2. **多層防御** — `_spawn_skill` は起動時に再チェックします。LLM が幻覚の Skill 名を出力した場合（またはセッション中に allowlist を絞り込んだ場合）、起動はアウトボックスのエラーで拒否されます。

何かが期待通りに実行されないときに確認するのはこの防御パスです。

## 拒否の観察

LLM が allowlist にない `skills_to_run` エントリーを出力した場合、アウトボックスには以下が表示されます:

```
[error] skill 'article_writer' is not in allowed_skills for agent 'researcher'; refused
```

構造化イベントが `events.jsonl` に記録されます:

```json
{"type": "skill_spawn_refused", "data": {"reason": "allowlist", "skill": "article_writer", "agent": "researcher"}}
```

拒否をフィルタリングします:

```bash
grep '"skill_spawn_refused"' .reyn/agents/researcher/events.jsonl
```

## トラブルシューティング

**「ルーター LLM が何も提案しなくなった。」** allowlist を確認してください。`[]` はルーターのみです。いくつかの Skill が必要なら列挙してください。すべてが必要なら、フィールドを削除するか `null` にしてください。

**「`not in allowed_skills` エラーが出るが LLM が同じ Skill を選び続ける。」** これは多層防御のパスです。ルーター側のフィルターは通常、ブロックされた Skill を LLM から隠すので、このブランチに到達するということは LLM が Skill 名を幻覚していることを意味します。ロールプロンプトを絞り込んで抑止するか、実際に必要なら Skill を allowlist に追加してください。

**「Skill を allowlist に追加したが、ルーターがまだ選ばない。」** カタログに Skill が存在するか確認します:

```bash
reyn skills list
```

allowlist は既存のカタログをフィルタリングします。解決できない名前は黙って除外されます。

## 関連情報

- [リファレンス: profile-yaml](../reference/dsl/profile-yaml.md) — 完全なスキーマと三値セマンティクス
- [リファレンス: agent CLI](../reference/cli/agent.md)
- [コンセプト: multi-agent](../concepts/multi-agent.md) — allowlist がアーキテクチャのどこに位置するか
- [ハウツー: agent チームを構築する](build-an-agent-team.md) — 制限と Topology を組み合わせる
