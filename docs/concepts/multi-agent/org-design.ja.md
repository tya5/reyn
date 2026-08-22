---
type: concept
topic: multi-agent
audience: [human, agent]
---

# LLM org-design ツール

Reyn は LLM にランタイムでライブなマルチエージェント組織を構築する 3 つのプリミティブを提供します：

| ツール | 機能 |
|------|-------------|
| `spawn_agent` | 名前とロール（+ 任意で base_dir、#5080）を持つ子 agent を作成。ケイパビリティは自分の ⊆、base_dir はプロジェクトワークスペースの ⊆ でキャップされる |
| `spawn_session` | タスクを分離して実行するための新鮮なコンテキストのサブセッションを開始 |
| `create_topology` | スポーンした agent をコミュニケーショントポロジーにワイヤリングし、オプションで各メンバーのケイパビリティを絞り込む |

これらのツールは**ルーター専用**（フェーズ内では使用不可）：スキルで作成された命令ではなく、実行中の agent が行う org-design の判断です。

> **オペレータートポロジーツールとの区別。** [オペレーター CLI（`reyn topology`）](../../reference/cli/topology.md)と Topology YAML は*人間のオペレーター*が org 構造を設定で事前定義するためのものです。このページのツールは*LLM 自身*がランタイムで org を設計するためのものです。補完的であり競合はしません。オペレーターが作成したトポロジーは既にそのメンバーである agent に対しては権威を持ちます。LLM は自分のスポーンサブツリー内でのみ構築できます。

---

## `spawn_agent` — 子 agent の作成

```text
spawn_agent(name: str, role: str = "", base_dir: str | None = None)
```

あなたの権限の下でレジストリに新しい agent を作成します。新しい agent のスポーン血統は LLM ではなく OS が設定します（forge-guard：LLM は親リンクを供給しません）。新しい agent の有効ケイパビリティは**設計によりあなたのケイパビリティの部分集合でキャップされます**——あなたができないことは何もできません（[⊆-parent ケイパビリティモデル](../runtime/permission-model.md#llm-spawn-capability-model)を参照）。

`base_dir`（#5080、任意）は新しい agent 自身の作業ディレクトリの既定値を設定します。その上限は**上記のケイパビリティキャップとも、下記の `spawn_session` 自身の `base_dir` とも異なります**——自分自身の有効 `base_dir` の配下ではなく、**プロジェクトワークスペース**の配下に解決される必要があります。省略すると、新しい agent は自分ではなくプロジェクト自身の `base_dir` を継承します。プロジェクトワークスペース外を指す要求は拒否され、決して切り詰められません。

`spawn_agent` は org の*identity レイヤー*を設計するために使います：誰が存在し、そのロールは何か。*誰が誰と話せるか*を制御してケイパビリティをさらに絞り込むには `create_topology` を使います。

### 戻り値が伝えること

`spawn_agent` はスポーン ack を返します（同期）——ツールが返る前に agent は作成・登録されています。ack には新しい agent の名前が含まれるため、後続の `create_topology` 呼び出しで参照できます。

---

## `spawn_session` — 新鮮なコンテキストでタスクを実行

```text
spawn_session(request: str, mode: "ephemeral" | "persistent" = "persistent",
              narrowing: dict | None = None, base_dir: str | None = None,
              agent: str | None = None, session: str | None = None)
```

現在の agent の下で（または `agent` 指定時、#4556: 自分の spawn subtree 内の任意の agent の下で）`request` を分離して実行する新しい Session を開始します。空のコンテキストウィンドウ、独立したワークスペース、この会話の記憶なし。スポーンされたセッションは即座に開始し、タスクの完了を待たずにスポーン ack を返します（非同期ディスパッチ）。

**`agent`**（オプション、#4556）：自分の spawn subtree 内の任意の agent を対象にできます — 自分自身、または `spawn_agent` 経由で（推移的に）作成した agent。`create_topology` 自身の subtree forge-guard が使うのと同じ `is_spawn_descendant` 述語でガードされます — subtree 外の agent を対象にする試みは拒否されます（`agent_outside_subtree`）、暗黙にリダイレクトされることはありません。省略すると自分自身の agent の下でスポーンします（デフォルト、従来どおりの挙動）。

**`session`**（オプション、#4556）：OS に自動生成させる代わりに、新しい session の id を自分で選べます。対象 agent について id が重複している場合は拒否されます（`session_already_exists`）、暗黙に上書きされることはありません。

**`mode`**：

- `ephemeral` — タスクが完了した後、セッションは自動的に消えます。残存状態が不要な単発作業に使います。
- `persistent` — タスク後もセッションは登録されたままです。後で参照したり作業を継続する必要がある場合に使います。

**`narrowing`**（オプション）：構築時にサブセッションに課すケイパビリティプロファイルの部分集合。制限のみ——自分自身のケイパビリティを超えてサブセッションにケイパビリティを付与することはできません。例：

```json
{"tool_deny": ["exec"]}
```

**`base_dir`**（オプション、#4200）：スポーンされる session の作業ディレクトリを上書きします。`narrowing` と同じ形で制限のみ——自分自身の実効 `base_dir` の内側に解決される必要があり、それ以外は拒否されます（暗黙にクランプされることはありません）。相対パスは自分自身の `base_dir` に対して解決されます。省略すると自分自身の `base_dir` をそのまま継承します（デフォルト）。

両モードとも巻き戻し安全です：巻き戻しカット後にスポーンされたセッションは巻き戻し再構成中に削除されます。

---

## `create_topology` — スポーンサブツリーのワイヤリングと絞り込み

```text
create_topology(
    name: str,
    kind: "network" | "team" | "pipeline",
    members: list[str],
    leader: str | None = None,      # kind=team の場合は必須
    profiles: dict[str, str] | None = None,
)
```

**あなたのスポーンサブツリー**（あなた自身 + `spawn_agent` 経由で推移的に作成した agent）の agent から名前付きコミュニケーショントポロジーを作成します。`can_send(A, B)` ルールはオペレーターが作成したトポロジーと同じ 3 種類に従います：

| Kind | 送信できる相手 |
|------|----------------------|
| `network` | すべてのメンバー ↔ すべてのメンバー |
| `team` | リーダー経由のみ — ピア ↔ ピアは禁止 |
| `pipeline` | 各メンバー → 次のメンバーのみ |

### `profiles` — メンバーケイパビリティの絞り込み

`profiles` は agent 名を `capability_profile` 名にマッピングします。バインドされたメンバーのセッションは既存の ⊆-parent キャップの上にそのプロファイルで制限されます——既に持っているエンベロープ内でのみ絞り込め、広げることはできません。プロファイルは `.reyn/capability_profiles/<name>.yaml` からロードされます。

```json
{
  "worker_a": "read_only",
  "worker_b": "no_subprocess"
}
```

### スポーンサブツリー制限（forge-guard）

自分のスポーンサブツリー内の agent のみメンバーとして含めることができます。OS はトポロジー作成 seam でこれを強制します——自分が作成していない（または推移的なスポーン子でない）agent をワイヤリングしようとすると拒否されます。これによりプロファイルバインディングが設計で安全になります：すべてのバインドされたメンバーは血統の論理積で既に ⊆ あなたであるため、バインディングはそのエンベロープ内でのみ絞り込めます。

トポロジーは WAL 追跡されるため、クラッシュリカバリと巻き戻しを生き延びます。

---

## 典型的な org-design フロー

```text
# 1. チームメンバーを作成
spawn_agent(name="researcher", role="gather background on topic X")
spawn_agent(name="writer",     role="draft the section from findings")

# 2. ワイヤリングとオプションの絞り込み
create_topology(
    name="research_team",
    kind="team",
    leader="researcher",   # researcher が writer を調整
    members=["researcher", "writer"],
    profiles={"writer": "no_subprocess"},
)

# 3. 単発の必要性のために分離タスクをスポーン
spawn_session(
    request="translate the draft to Japanese",
    mode="ephemeral",
)
```

---

## LLM スポーンツリーへのオペレーターバウンド

オペレーターは `reyn.yaml` の `safety.spawn` を使って LLM が設計する org の成長上限を設定できます。これは DoS ガードです——agent が無制限の組織を作成するのを防ぎます。LLM は自分自身の基本制限をランタイムに引き上げるパスを持ちません（設定は再起動専用の OUT レイヤーです）。

| キー | デフォルト | 効果 |
|-----|---------|--------|
| `safety.spawn.max_depth` | `10` | スポーン血統チェーンの最大深度（0 = 無制限） |
| `safety.spawn.max_children` | `20` | 親ごとの直接スポーン子の最大数、および `create_topology` 呼び出しのメンバー最大数 |

スポーンが制限を超える場合、ループキャップやバジェットキャップと同じモード駆動フレームワーク（`safety.on_limit` チェックポイント）が発火します：

- **`interactive`**（デフォルト）：オペレーターに拡張の承認を求めます。承認されると拡張はスポーナーごとに記録されるため、同じスコープで再プロンプトは発生しません。基本設定制限は変更されません——拡張は常にオペレーター承認であり、LLM 駆動ではありません。
- **`unattended`**：スポーンを即座に拒否（プロンプト不可。CI やスクリプト実行に使用）。
- **`auto_extend`**：`auto_extend_times` 回まで拡張を自動承認し、その後拒否。

`max_depth` と `max_children` には別々のスポーナーごとの拡張キーがあります：一方のオペレーター承認された増加が暗黙的に他方を広げることはありません。

[reyn-yaml § safety.spawn](../../reference/config/reyn-yaml.md#safetyspawn-fields) と [safety.on_limit](../../reference/config/reyn-yaml.md#safetyon_limit-fields) で完全なスキーマを確認してください。

---

## 参照

- [⊆-parent ケイパビリティモデル](../runtime/permission-model.md#llm-spawn-capability-model) — no-escalation-via-spawn セキュリティプロパティの強制方法
- [Concepts: topology（オペレーター）](../multi-agent/topology.md) — 人間 CLI の org-design サーフェス
- [Concepts: sessions](../multi-agent/sessions.md) — セッションが所有するもの、ephemeral / persistent ライフサイクル
- [Reference: reyn-yaml § safety.spawn](../../reference/config/reyn-yaml.md#safetyspawn-fields) — オペレーターバウンド

