# FP-0010: RAG ルーティング — スキルカタログ + ルーティング履歴の semantic pre-filter

**Status**: proposed
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

router がユーザー入力に対して LLM を呼ぶ前に、OS レベルで `recall` を実行して
関連スキル候補を top-K に絞り、「Suggested for this request」セクションとして
システムプロンプトに注入する。スキル数が増えても LLM が見る候補を一定に保てる。
インデックスが未構築の場合はセクションをスキップし、既存動作にフォールバックする。

---

## Motivation

### ツール数増加への耐性

現在の router は 14〜23 個のツールをフラットに提示する。
FP-0006〜0009 が実装されるとスキルの種類がさらに増え、LLM の選択精度が劣化する。

```
現在:  LLM → 全スキル一覧から選択（スキルが100個になると非現実的）
本 FP: OS が recall で top-K に絞る → LLM は絞られた候補を見てから選択
```

### Layer 2：使うほど賢くなる

ルーティング履歴（`routing_decided` イベント）が蓄積すると、
「過去に似たリクエストで成功したスキル」を few-shot ヒントとして使えるようになる。
FP-0009（Operational Intelligence）の基盤がこれを自動で育てる。

---

## 設計の核心

### recall は LLM のツールではなく OS の前処理

```
【採用しない案 A】
  ユーザー入力 → LLM → recall ツール呼び出し → 結果を見て invoke_skill
  問題: 毎ターン 1 ツール呼び出し分の遅延。LLM が recall を呼ぶかどうかを誤判断する。

【採用する案 B】
  ユーザー入力 → [OS] recall 実行 → SP に top-K 注入 → LLM が最初から候補を参照
  利点: 追加ターン不要。インデックスなし時はセクションをスキップするだけ。
```

`indexed_sources` セクションが `router_loop.py` でSP構築前に注入される構造と対称。

### インデックスなし → スキップ（グレースフルデグラデーション）

```python
# router_loop.py
routing_hints = await recall_for_routing(user_input)  # None if no index
system_prompt = build_system_prompt(
    ...,
    routing_hints=routing_hints,  # None → セクションなし（既存動作を維持）
)
```

### P4 遵守

recall 結果はあくまで**ヒント**。最終制約は `invoke_skill` の enum（OS 提供の候補セット）。
LLM がヒントを無視して `list_skills` を使うことも正しい動作。

---

## Proposed implementation

### Component A — OS レベルの recall pre-filter（SMALL）

**`src/reyn/chat/router_loop.py` の変更:**

```python
async def _build_routing_hints(user_input: str) -> RoutingHints | None:
    """
    skill_catalog と routing_history に対して recall を実行する。
    いずれのインデックスも存在しない場合は None を返す（スキップ）。
    """
    manifest = get_source_manifest(Path.cwd())
    if not manifest.has_source("skill_catalog") \
       and not manifest.has_source("routing_history"):
        return None

    results = await recall_op(
        query=user_input,
        sources=["skill_catalog", "routing_history"],
        top_k=5,
        filter={"outcome": "success"},   # Layer 2: 成功ルートのみ
    )
    return RoutingHints(results=results)
```

`recall_op` は既存の `src/reyn/op_runtime/recall.py` をそのまま使用。
新たな OS 変更はこの呼び出しラッパーのみ。

### Component B — `routing_decided` P6 イベント（SMALL）

router が `invoke_skill` を実行したタイミングで emit する。
Layer 2 の知識ベースになる。

```python
# router_loop.py — invoke_skill ツールハンドラ内
event_log.emit("routing_decided",
    user_input=user_input,               # ユーザーの自然言語入力
    chosen_skill=skill_name,             # 選ばれたスキル
    top_k_considered=[r.name for r in routing_hints.results] if routing_hints else [],
    routing_source=routing_source,       # "rag_hint" | "list_skills" | "explicit"
    outcome=None,                        # 実行後に "success" / "wrong_skill" で更新
)
```

`outcome` フィールドはスキル実行完了後に `run_skill_completed` と突合して更新する。
「ユーザーが同ターン内で別スキルを再実行 → wrong_skill」として検出。

### Component C — 「Suggested for this request」セクション（SMALL）

**`src/reyn/chat/router_system_prompt.py` の変更:**

```
## Suggested for this request
あなたのリクエストに最も関連するスキル:

1. **swe_bench** — SWE-bench タスクを解く（スキルカタログより）
2. **code_review** — コードをレビューする（過去の類似リクエストより）

そのまま invoke_skill で実行できます。
別のスキルが必要な場合は list_skills で一覧を確認してください。
```

`routing_hints` が None（インデックスなし）→ セクション全体を省略。
`routing_hints` が空（インデックスあり・ヒットなし）→ セクション省略（混乱を避ける）。

セクションの挿入位置: `## Skills` セクションの直前（最も目立つ位置）。

### Component D — スキルカタログのインデックス化（SMALL）

`skill_catalog` ソースを `index_docs` で構築するコマンドを追加。

```
reyn run index_docs --source skill_catalog
```

skill.md のチャンク設計（ドキュメントと異なり構造化）:

```
[skill chunk: swe_bench]
name: swe_bench
description: SWE-bench タスクを解く — GitHub issue のコード修正と検証
tags: coding, benchmark, github, testing
input: リポジトリURL, issueの説明, テストパッチ
```

**自動更新トリガー（将来）:** スキルが追加・変更されたときに `skill_catalog` を再インデックスするフック。現時点は手動実行。

#### オプション: `example_phrases` フィールド

skill.md frontmatter に任意フィールドとして追加。スキル作者がセマンティックマッチを調整できる。

```yaml
# skill.md frontmatter
example_phrases:
  - "バグを修正して"
  - "テストが通るようにして"
  - "プルリクエストのコードを直して"
```

`index_docs` がこのフィールドをチャンクに含める（スキル作者任意）。

### Component E — ルーティング履歴のインデックス化（SMALL）

FP-0009 の `index_events` に `routing_decided` イベントの処理を追加。

```
[routing history chunk]
user_input: "django のバグを修正して"
chosen_skill: swe_bench
routing_source: explicit
outcome: success
timestamp: 2026-05-10T09:15:00
```

フィルタリング: `outcome == "success"` のみインデックス化。
失敗・訂正されたルートは除外（誤った few-shot を防ぐ）。

---

## 段階的実装

| Phase | 内容 | 前提 |
|---|---|---|
| **Phase 1** | Component A〜D（Layer 1: スキルカタログ）| ADR-0033 RAG ✅ |
| **Phase 2** | Component B の `outcome` 更新 + Component E（Layer 2: ルーティング履歴）| FP-0009 |

Phase 1 単独でも「スキルカタログ semantic routing」として価値がある。
Phase 2 は FP-0009 の `index_events` が育ってから追加。

---

## フロー全体図

```
ユーザー入力
    ↓
[OS] recall(sources=["skill_catalog", "routing_history"], filter={outcome:success})
    ├─ インデックスなし → None（スキップ）
    └─ あり → top-5 candidates
    ↓
[OS] build_system_prompt(routing_hints=top_5)
    → "## Suggested for this request" セクション注入（または省略）
    ↓
[LLM] ヒントを参照して invoke_skill (enum 制約) または list_skills
    ↓
[P6] routing_decided イベント emit（user_input / chosen_skill / routing_source）
    ↓
スキル実行完了後 → outcome 更新（success / wrong_skill）
    ↓
[FP-0009] index_events が定期的に routing_history をインデックス化
    → Layer 2 が自己育成
```

---

## Dependencies

- ADR-0033 RAG Phase 1（✅ landed）— `recall` op / SourceManifest が前提
- `src/reyn/chat/router_loop.py` — Component A / B（recall pre-filter + event emit）
- `src/reyn/chat/router_system_prompt.py` — Component C（セクション追加）
- `src/reyn/op_runtime/recall.py` — 変更なし（既存 recall op をそのまま使用）
- FP-0009（Operational Intelligence）— Component E の `routing_decided` 処理（Phase 2 のみ）

前提 PR: なし。Phase 1 は FP-0009 なしで独立実装可能。

---

## Cost estimate

**合計: MEDIUM**

| タスク | コスト | 備考 |
|---|---|---|
| Component A: recall pre-filter（router_loop.py）| SMALL | recall_op 呼び出しラッパー |
| Component B: routing_decided イベント + outcome 更新 | SMALL | emit 1 箇所 + run_skill_completed との突合 |
| Component C: SP への "Suggested" セクション注入 | SMALL | router_system_prompt.py に節追加 |
| Component D: skill_catalog インデックス化 + チャンク設計 | SMALL | index_docs の source 設定追加 |
| Component E: routing_history インデックス化（Phase 2）| SMALL | FP-0009 index_events の拡張 |
| テスト（Tier 2: router invariant）| SMALL | インデックスなし時のスキップ動作の contract test |

ボトルネックは **Component B の outcome 更新**（スキル実行後の成否と routing_decided の突合ロジック）。

---

## Related

- `src/reyn/chat/router_loop.py` — recall pre-filter 挿入点
- `src/reyn/chat/router_system_prompt.py` — "Suggested" セクション追加
- `src/reyn/op_runtime/recall.py` — 既存 recall op（変更なし）
- `src/reyn/index/source_manifest.py` — has_source() で存在チェック
- ADR-0033 (`docs/deep-dives/decisions/0033-rag-extensible-os.md`) — RAG 基盤
- FP-0009 (`0009-operational-intelligence.md`) — routing_history の自己育成基盤
