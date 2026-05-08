---
type: concept
topic: architecture
audience: [human, agent]
---

# Memory

Memory は、1 回の実行を超えて保持すべき事実のための reyn のメカニズムです。ユーザーの好み、プロジェクトの規約、過去の決定、agent 固有の習慣などを管理します。ルーター Phase（`skill_router/classify`）はすべてのチャットターンで memory を読み、新しいエントリーを書き込むかどうかを判断します。

OS に独立した memory API はありません。memory は単なるファイルと、ルーター Phase が通常のパーミッションルールを通じて使用する `file/read` および `file/write` ops です。

## 2 つのレイヤー

| レイヤー | 場所 | 参照可能な対象 | 用途 |
|-------|----------|------------|---------|
| 共有 | `.reyn/memory/` | プロジェクトの全 agent | プロジェクト全体の事実：ユーザーのプロフィール、プロジェクトの決定、外部参照 |
| Agent | `.reyn/agents/<name>/memory/` | その agent のみ | Agent 固有の動作：研究者の好むソース、ライターの文体 |

両レイヤーとも同じ形状を共有します。`MEMORY.md` インデックスとエントリーごとの `<slug>.md` 本文ファイルです。ChatSession はすべてのルーターターンで両方の `MEMORY.md` ファイルを読み込み、1 つのビューにマージして `memory_index.content` としてルーティング artifact に埋め込みます。マージされたビューには 2 つのセクションが明確に区別されているため、LLM はエントリーがどのレイヤーのものかを判別できます：

```markdown
# Memory Index (shared)

- [User Role](user_role.md) — backend engineer with 10y Python
- [Project Vision](project_reyn_vision.md) — predictability over autonomy

# Memory Index (agent: researcher)

- [Search Pref](feedback_arxiv_first.md) — prefers arxiv before web search
```

どちらかのレイヤーにエントリーがない場合、`(empty)` がそのヘッダーの下に表示されます。

## レイヤーの選び方

ルータープロンプトは、不確かな場合には **共有** レイヤーを使用するよう LLM に指示します。広い可視性がより安全なデフォルトです：

- **共有**：すべての agent に有益な事実（ユーザーの役割、プロジェクトの決定、期限、外部システムへのポインタ）
- **Agent**：*この* agent にとってのみ意味のある事実（その文体、その検索の習慣、他の agent が引き継ぐべきでない動作）

LLM が保存を決定したとき、同じルーターターンで 2 つの ops を発行します：

1. 選択したレイヤーのパスに本文ファイルを書き込む `file/write`
2. そのレイヤーの `MEMORY.md` が変更を拾うための `file/regenerate_index` op

ランタイムは各本文ファイルの frontmatter から `MEMORY.md` を機械的に再構築します。LLM は `MEMORY.md` を直接書きません。これにより、インデックスの正確性がモデルの能力から独立します。インデックスを手動で再構築する際にエントリーを落としがちだった安価なモデルも、もはやそれができなくなります。

各レイヤーはディスク上に独自の MEMORY.md を持ちます。マージされた `(shared)` / `(agent)` のヘッダーは、ChatSession が LLM 向けに合成するインメモリビューにのみ存在します。

## 読み取りパス

```
ChatSession._invoke_router
  └─ _merge_memory_indexes(shared_path, agent_path, agent_name)
       ├─ .reyn/memory/MEMORY.md を読む（存在すれば）
       ├─ .reyn/agents/<name>/memory/MEMORY.md を読む（存在すれば）
       └─ {status, content} を返す  ← chat_routing_request artifact に埋め込む

skill_router classify phase
  └─ LLM は user_message + history とともに memory_index.content を見る
```

インデックスの説明が曖昧で回答できない場合、LLM は本文ファイル（共有なら `.reyn/memory/<slug>.md`、agent なら `.reyn/agents/<chat_id>/memory/<slug>.md`）への `file/read` を持つ `act` ターンを発行します。その Phase のパーミッションでは `.reyn/memory` と `.reyn/agents` 両方の配下での再帰的な読み取りが許可されています。

## 書き込みパス

ルーター Phase は両レイヤーに対して `file.write` パーミッションを持ちます。LLM は `chat_id`（= agent 自身の名前）からパスを構築し、他の agent のディレクトリに書き込むことはありません。OS レイヤーでの強制はディレクトリプレフィックスのパーミッション付与を超えるものはありません。信頼境界は LLM プロンプトであり、events ログによって監査されます。

各本文ファイルの書き込みの後、LLM は `file/regenerate_index` op を発行します。この op は完全にパラメータ化されています。`output_path`、`entry_template`、`header` は呼び出し元が提供するため、OS ファイルランタイムはフォーマットに依存しません（P7 に従い、`MEMORY.md` のファイル名やエントリーフォーマットは OS コードに埋め込まれません）。同じパラメータ化されたヘルパーが `reyn memory edit` / `delete` / `import` によって使用され、CLI の変更後もディスク上のインデックスを同期し続けます。

## ドキュメントとの対称性

memory とドキュメントの関係は意図的なものです：

| Memory | ドキュメント |
|--------|------|
| システムが *このユーザー/プロジェクト* について学んだこと | システムが一般的に *できること* |
| `skill_router` がインラインで読む | `recall_docs` が読む（計画中、未実装） |
| 実行をまたいで永続化 | 静的 |

`recall_docs` は残余リストにあります。実装されれば、同じ 2 層の形状を持ちながら異なる読み取りトリガーを持つ、プロジェクトドキュメントの類似物となります。

## memory と events の違い

| | Memory | Events |
|---|--------|--------|
| 実行をまたぐ状態？ | はい | いいえ（実行ごと、追記専用の監査） |
| 作成者 | ユーザー（ルーター LLM が事実を永続化することで） | OS |
| フォーマット | frontmatter 付き Markdown | JSONL |
| 読み取り主体 | `skill_router` classify phase | `reyn events` CLI |

Events は「この実行で何が起きたか？」に答え、memory は「次の実行に入るにあたって何を知っておくべきか？」に答えます。

## 陳腐化

Memory は特定の時点のスナップショットです。6 ヶ月前の「フィードバック」エントリーはもはや適用されない場合があります。ファイルパスを示す「プロジェクト」エントリーはファイルが移動していれば誤りです。ルーター LLM は具体的な内容に基づいて行動する前に確認するよう指示されています。

システムはエントリーを自動的に劣化または期限切れにしません。削除は `reyn memory delete` でユーザーが行います（本文ファイルを削除してインデックスを再同期します）。機械的な再生成により、別の `gc` ステップは不要になります。インデックスはディスク上の本文ファイルとずれることはありません。

## 参考

- [Reference: skill_router](../reference/stdlib/skill_router.md) — memory を読み書きする Phase
- [Reference: profile-yaml](../reference/dsl/profile-yaml.md) — agent プロファイルの形状
- [Reference: state-dir](../reference/config/state-dir.md) — `memory/` と `agents/<name>/` の場所
- [events.md](events.md)
