---
type: how-to
topic: multi-agent
audience: [human]
applies_to: [reyn agent, reyn topology]
---

# agent チームを構築する

**目的:** 専門家 agent の小さなチームを立ち上げ、ワークフローに合った通信制限を設ける。

## 使うべき状況

- 1 つの chat agent が多くの役割を持つジェネラリストになりすぎている。
- 専門家（例: リサーチ vs. 草稿作成 vs. レビュー）に作業を分割し、連携させたい。
- 「ワーカーはリーダーを迂回できない」または複数レベルの階層を表現したい。

## クイックレシピ — リーダー + 2 ワーカー

3 つのコマンドで、1 人のリーダーと 2 人のワーカーを持つ `team` Topology を立ち上げます。

### 1. agent を作成する

```bash
reyn agent new lead --role "team lead. Triages requests and synthesizes worker output."
reyn agent new researcher --role "deep technical research, prefers primary sources (arxiv, RFCs)."
reyn agent new writer --role "concise long-form prose. Strict word budgets, no headings unless asked."
```

各コマンドは `.reyn/agents/<name>/profile.yaml` をプロビジョニングし、空の memory レイヤーをシードします。

### 2. team Topology を宣言する

```bash
reyn topology new launch --kind team \
    --leader lead \
    --members lead,researcher,writer
```

`team` kind は `leader ↔ member` エッジのみを許可します。ワーカーは互いに直接送信できません。`lead` を経由してルーティングする必要があります。

### 3. 構造を確認する

```bash
reyn topology show launch
```

```
name:        launch
kind:        team
leader:      lead
members:     lead*, researcher, writer
created_at:  2026-05-01T12:00:00+00:00

permitted edges (4):
  lead → researcher
  lead → writer
  researcher → lead
  writer → lead
```

アスタリスクはリーダーを示します。`researcher → writer` がエッジリストに**ない**ことに注目してください。これが team ルールの効果です。

### 4. 使用する

```bash
reyn chat lead
```

リサーチと草稿作成の両方に関わる質問を lead に聞いてください:

```
> DuckDB v1 の破壊的変更を調査し、200 語の変更ログサマリーを作成してください。
```

`lead` のルーターは `researcher` への `messages_to_agents` を（そして、レスポンスが届いた後に）`writer` へのものを出力するかもしれません。ユーザーの視点では、「(作業中です)」という中間表示の後、最終的な統合された返信が届きます。裏で何が起きているかは [ハウツー: multi-hop delegation](multi-hop-delegation.md) を参照してください。

## メンバーを後で追加する

```bash
reyn agent new reviewer --role "edits drafts for clarity, never adds new claims."
reyn topology add-member launch reviewer
```

`reviewer` は同じ制約を持ちます: `lead` とのみ通信します。

## メンバーを削除する

```bash
reyn agent rm researcher --yes
```

これは `researcher` をメンバーとしてリストしているすべての Topology にカスケードします。`launch` のメンバーは `[lead*, writer, reviewer]` になります。リーダーを失った、または空になった Topology は完全に削除されます。

agent を削除せずにメンバーだけを外すこともできます:

```bash
reyn topology rm-member launch writer
```

この後、`writer` は `lead` との共有 Topology を持たなくなり（`launch` が唯一の Topology だった場合）、自動管理の `_default` Topology に再参加して、再び他の無所属 agent と自由に通信できます。

## 2 レベルのツリーにする

実際の組織は単一チームではなく、入れ子になっています。`tree` という kind はありませんが、**重複する team Topology** がツリーをそのまま表現します:

```bash
# 3 人のエグゼクティブが ceo に報告
reyn agent new ceo --role "..."
reyn agent new vp_eng --role "..."
reyn agent new vp_sales --role "..."

# vp_eng 配下のエンジニア
reyn agent new eng_a --role "..."
reyn agent new eng_b --role "..."

# vp_sales 配下の営業
reyn agent new sales_a --role "..."

# 親チームの関係ごとに 3 つのチーム
reyn topology new team_exec --kind team --leader ceo \
    --members ceo,vp_eng,vp_sales

reyn topology new team_eng --kind team --leader vp_eng \
    --members vp_eng,eng_a,eng_b

reyn topology new team_sales --kind team --leader vp_sales \
    --members vp_sales,sales_a
```

結果:

| エッジ | 許可? | 理由 |
|------|------------|-----|
| `ceo ↔ vp_eng` | ✓ | `team_exec`（リーダー ↔ メンバー） |
| `vp_eng ↔ eng_a` | ✓ | `team_eng`（リーダー ↔ メンバー） |
| `vp_eng ↔ vp_sales` | ✗ | `team_exec`はピア ↔ ピアを禁止 |
| `ceo ↔ eng_a` | ✗ | 共有 Topology なし — `ceo` は `vp_eng` 経由でエスカレートが必要 |
| `eng_a ↔ eng_b` | ✗ | `team_eng` ピア ↔ ピア禁止 |

複数レベルのエスカレーションは単一ホップの繰り返し（`ceo → vp_eng → eng_a`）で行われ、`safety.loop.max_agent_hops`（デフォルト 3、より深いツリーには増やす）で制限されます。なぜこれが特別な kind なしに設計から導き出されるかは [コンセプト/topology — ツリーパターン](../../../concepts/multi-agent/topology.md#tree-pattern) を参照してください。

## kind の選び方

| Kind | 使う場面 |
|------|----------|
| `network` | 自由なピアチーム。構造的制限なし; 全員が全員に聞ける。 |
| `team` | リーダーが集約ポイント。ワーカーがリーダーを迂回すべきでない。 |
| `pipeline` | 線形ワークフロー（トリアージ → 草稿 → 公開）。各ステージは次のステージとのみ通信。 |

自動管理の `_default` Topology は、ユーザー宣言の Topology に配置されていない agent をカバーします。それらの agent は自由に到達可能な状態にとどまり、早期プロトタイピング中に望ましい状態です。

## トラブルシューティング

**ルーター LLM が委任を提案しない。** ターゲットがソース agent の `available_agents` に表示されているか確認してください:

```bash
reyn topology show launch  # 両方の agent がメンバーであることを確認
```

共有 Topology がない場合、エッジは拒否されます（ルーターはターゲットすら見えません）。ソースがユーザー Topology に引き込まれた後は `_default` のメンバーシップも役に立ちません。

**アウトボックスに `agent X: blocked by topology rules`。** LLM が提案すべきでない委任ターゲットを幻覚しました。Topology の kind が意図に合っているか確認してください。例えば、`network` を意図して `team` を宣言していたかもしれません。

**`agent message depth N exceeds limit M; chain refused`。** 重複するチームが `safety.loop.max_agent_hops` が許可するよりも深いツリーを形成しています。`reyn.yaml` で制限を引き上げてください:

```yaml
safety:
  loop:
    max_agent_hops: 5
```

## 関連情報

- [コンセプト: topology](../../../concepts/multi-agent/topology.md) — kind のセマンティクス、単一許可ルール、ツリーパターン
- [コンセプト: multi-agent](../../../concepts/multi-agent/multi-agent.md) — agent のアイデンティティ、AgentRegistry、チェーンセマンティクス
- [リファレンス: topology CLI](../../../reference/cli/topology.md)
- [リファレンス: agent CLI](../../../reference/cli/agent.md)
- [ハウツー: multi-hop delegation](multi-hop-delegation.md) — チェーンが複数の agent にまたがる場合に何を期待するか
