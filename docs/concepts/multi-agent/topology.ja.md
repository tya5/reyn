---
type: concept
topic: architecture
audience: [human, agent]
---

# Topology

Topology は「誰が誰と通信できるか」を構造として宣言します。3 種類の Topology（`network`、`team`、`pipeline`）が存在し、自動管理される `_default` Topology が空の状態を使いやすくします。結果として、数行のコードに収まる単一の許可ルールが生まれます。

## Topology が第一級市民である理由

Topology が導入される前、プロセス内の agent は暗黙の完全グラフを形成していました。誰もが誰にでも送信でき、唯一の安全策は `max_hop_depth` だけでした。2 エージェントのおもちゃ構成ならそれで動きますが、組織構造を表現しようとするとすぐに破綻します。3 チームの組織をアドホックなフィルターで表現すると、すぐに一貫性が失われます。

Reyn のスタンス: 構造を一度モデリングし、どこでも強制する。AutoGen / CrewAI / LangGraph はそれぞれ 1 つの形状をハードコードします（GroupChat マネージャー、階層型、スーパーバイザー）が、Reyn は形状を宣言的にします。

## 3 種類

各種類は `.reyn/topologies/<name>.yaml` にある YAML ファイルで、`name`、`kind`、`members`、（`team` の場合は）`leader` を持ちます。各 kind の `can_send(A, B)` ルール:

| Kind | ルール |
|------|------|
| `network` | `A != B and A,B ∈ members` — 完全グラフ |
| `team` | `leader ∈ {A, B} and A != B and A,B ∈ members` — リーダーを中心とするスター型；ピア間の通信は禁止 |
| `pipeline` | `members.index(B) == members.index(A) + 1` — 有向パス、ジャンプ不可、逆方向不可 |

例:

```yaml
# network: ピアが自由に通信するチーム
name: kitchen
kind: network
members: [chef, sous, baker]
```

```yaml
# team: マネージャー + ワーカー、ワーカーはマネージャーを迂回できない
name: research_lead
kind: team
leader: manager
members: [manager, researcher_a, researcher_b]
```

```yaml
# pipeline: トリアージ → 草稿作成 → 公開
name: publish_pipe
kind: pipeline
members: [triage, drafter, publisher]
```

## `_default` Topology

レジストリは、ユーザーが宣言した Topology のメンバーに含まれない**すべての** agent を含む `_default` network Topology を自動的に生成します。これはメモリ上のみで管理され、永続化されません。

これにより空の状態が使いやすくなります。Topology をゼロ宣言すれば `_default` が全員をカバーし、ランタイムは完全に許可的になります。agent をユーザー宣言の Topology に追加した瞬間、その agent は `_default` を離れ、ユーザー宣言のルールのみが適用されます。制限は宣言された瞬間に強制されます。

`_default` は透明性のために `reyn topology list` に表示されます:

```
NAME      KIND      MEMBERS
team1     team      default*, alpha
_default  network   beta, gamma
```

`_default` を直接作成・削除・変更することはできません。自動管理です。

## 単一の許可ルール

```python
def permit(from_agent, to_agent):
    if from_agent == to_agent:
        return False
    candidates = list(user_topologies) + [default_topology()]
    shared = [t for t in candidates if from_agent in t.members and to_agent in t.members]
    if not shared:
        return False
    return any(t.can_send(from_agent, to_agent) for t in shared)
```

以上です。フォールバックなし、ポリシーモードなし、per-agent オーバーライドなし。重複する複数の Topology はそれぞれの `can_send` を提供し、そのどれかが許可すればエッジは許可されます。

## ルールが発動する箇所

2 つの強制ポイント（多層防御）:

1. **`iter_reachable_agents`** — ルーターが `available_agents` リストを構築する際、呼び出し元が到達できない agent はフィルタリングされます。LLM は到達不可能なターゲットを見ることがなく、ブロックされた委任を提案できません。
2. **`_send_to_agent`** — 送信時に `permit()` が参照されます。ブロックされた送信はアウトボックスに `error` メッセージ（「agent X: topology ルールによりブロックされました」）として現れ、`agent_message_sent` は**送出されません**。

## ツリーパターン

`tree` という kind はありません。階層は**重複する `team` Topology**として表現します:

```yaml
# .reyn/topologies/team_exec.yaml
name: team_exec
kind: team
leader: ceo
members: [ceo, vp_eng, vp_sales]
```

```yaml
# .reyn/topologies/team_eng.yaml
name: team_eng
kind: team
leader: vp_eng
members: [vp_eng, eng_a, eng_b]
```

```yaml
# .reyn/topologies/team_sales.yaml
name: team_sales
kind: team
leader: vp_sales
members: [vp_sales, sales_a]
```

結果:

| エッジ | 許可? | 理由 |
|------|------------|-----|
| `ceo ↔ vp_eng` | ✓ | `team_exec`（リーダー ↔ メンバー） |
| `vp_eng ↔ eng_a` | ✓ | `team_eng`（リーダー ↔ メンバー） |
| `vp_eng ↔ vp_sales` | ✗ | `team_exec`（ピア ↔ ピアは禁止） |
| `ceo ↔ eng_a` | ✗ | 共有 Topology なし — vp_eng 経由でエスカレートが必要 |
| `eng_a ↔ eng_b` | ✗ | `team_eng`（ピア ↔ ピアは禁止） |

ツリーが期待する通り、直接の親 ↔ 子エッジのみが許可されます。複数レベルのエスカレーションは単一ホップの繰り返し（`ceo → vp_eng`、次に `vp_eng → eng_a`）を通じて行われ、エンドツーエンドのトレースのための [chain_id](../multi-agent/multi-agent.md#chain_id) と自然に統合されます。

`validate-tree` コマンド（残課題）は、重複する team Topology のセットが実際にツリーを形成しているか（単一ルート、サイクルなし、複数の親なし）を検証します。厳密さのために有用ですが、ランタイムの動作には必須ではありません。

## agent 削除のカスケード

`reyn agent rm <name>` は Topology にカスケードします:

- agent はすべての Topology の `members` から削除されます。
- `team` のリーダーが削除された場合、Topology 全体が削除されます（リーダーのいないチームは意味をなしません）。
- `members` が空になった Topology も削除されます。

カスケード後、レジストリは `_default` を再計算します。最後のユーザー宣言の所属を失った agent は自動的に `_default` に再参加します。

## 関連情報

- [リファレンス: topology CLI](../../reference/cli/topology.md)
- [リファレンス: topology-yaml](../../reference/dsl/topology-yaml.md)
- [コンセプト: multi-agent](../multi-agent/multi-agent.md)
