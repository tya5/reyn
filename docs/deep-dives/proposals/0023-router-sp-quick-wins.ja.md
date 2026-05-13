# FP-0023: Router システムプロンプト — クイック改善

**Status**: proposed
**Proposed**: 2026-05-13
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

`router_system_prompt.py` に対する 5 つのピンポイント修正。LLM の応答準拠率向上・キャッシュ効率改善・JA UX 改善を目的とし、構造的変更はなし。変更はすべて既存プロンプトの追加・並び替えの範囲に留まる。

---

## Motivation

`router_system_prompt.py`（808 行）をコード分析・dogfood 測定・業界ベストプラクティスの 3 軸で評価した結果、独立した 5 つの問題が判明した。いずれも 1 日以内に修正可能。

### 問題 1 — キャッシュ効率：静的セクションが動的セクションの後に来る

現在のセクション順：

```
Identity（静的）
→ project_context（動的）
→ Role（静的）
→ Intent axis（静的）
→ skills / agents / memory / files / MCP（動的）
→ When asked what you can do（静的）
→ Behaviour（静的、~5,800 文字 — 最大セクション）
```

Anthropic prompt cache はプレフィックスが共通である場合にヒットする。最初の動的セクション（`project_context`）が早い段階に現れるため、その後のすべての静的セクション（`Behaviour` を含む）がキャッシュ対象外になる。実効キャッシュカバレッジ：**~20%**（Identity のみ）。

静的セクションを先頭にまとめるだけで、カバレッジは **~60%** に向上する（Identity + Role + Intent axis + When asked + Behaviour）。

### 問題 2 — 意図軸の 2 重説明

`## What you can do (intent axis)`（行 161–221）が内部ルーティングラベル（Action / Memory access / Save / Forget / Reply）を説明。

`## When asked what you can do`（行 310–328）が同じカテゴリをユーザー向け表現で再度説明。

LLM は両方を見る。リスク：内部ラベルがユーザーへの返答に漏れる（「あなたの意図は *Memory access* に分類されました」等）。解決策：1 セクションに統合し、内部用とユーザー向けを明確に分離する。

### 問題 3 — spawn-ack ルールに優先順位がない複数の MUST

行 521–564 で spawn-ack 応答に 3 つの MUST レベル制約を列挙：
1. `/tasks` リンクを返す
2. 返答を 1 文に絞る
3. invoke_skill を再度呼ばない

LLM が同レベルの MUST を複数見ると全体の準拠率が低下する。dogfood 測定では `/tasks` 準拠が最も不安定だった。番号付きの優先順（最重要から）で注意を集中させる。

### 問題 4 — `delegate_to_agent` の使い方が Behaviour にない

`delegate_to_agent` はツールリスト（行 176）にあるが、いつ・どう呼ぶかを示す Behaviour ルールがない。LLM はツールスキーマの description だけから推論しなければならない——これは主要ベンダーすべてが「SP に usage guidance を書く」と推奨している箇所の欠落。

### 問題 5 — JA の recall/memory 使い分けに JA 例文がない

行 354–366 で `recall`（indexed 検索）と `list_memory`/`read_memory_body`（memory 操作）の区別を正しく説明しているが、例文はすべて EN。dogfood 測定では JA 例文の追加がルーティング非準拠率を ~50% → ~5% に改善した（B12-R2）。同手法を recall/memory 区別にも適用すべき。

不足しているカバレッジ：
- `思い出して` / `前回の話` → indexed sources がある場合は `recall`
- `覚えて` / `メモして` / `記録して` → `remember_*`

---

## Proposed implementation

### 変更 1 — セクションを並び替えてキャッシュ効率を上げる

`build_system_prompt()` のセクション順を以下に変更：

```
[静的 — キャッシュプレフィックス対象]
1. Identity
2. Role statement
3. Intent axis（内部ルーティングガイド）
4. When asked what you can do
5. Behaviour

[動的 — セッションごとに変わる]
6. project_context
7. Skills catalog
8. Agents catalog
9. Memory section
10. Indexed sources
11. Files section
12. MCP servers section
13. User capabilities list（条件付き）
```

内容変更なし——純粋な並び替え。静的セクション全体がキャッシュプレフィックスになる。

### 変更 2 — 意図軸の重複を統合

`## What you can do (intent axis)` と `## When asked what you can do` を 1 セクションに統合：

```markdown
## Capabilities (routing guide)

Internal routing axes — これらのラベルをユーザーへの返答で使わないこと:
- Action: 何かを実行したい → invoke_skill / ツールを使う
- Memory access: 保存済み情報を取得したい → list_memory / recall
- Save: 何かを保存したい → remember_*
- Forget: 保存済み情報を削除したい → forget_memory
- Reply: 会話的な応答のみ → ツール不要

ユーザーに「何ができますか?」と聞かれたら、平易な言葉で答える:
「スキルを実行できます（…）、ドキュメントを検索できます（…）、情報を記憶できます（…）」
「あなたの意図は Action です」等のルーティングラベルを返答で使わないこと。
```

### 変更 3 — spawn-ack の MUST を優先順位付きで整理

フラットなリストを優先順位付きブロックに置き換え：

```markdown
invoke_skill が {status: "spawned", ...} を返したとき:

  優先 1（絶対）: `/tasks` リンクを返信に含める。
    スキルの実行状況を確認できるユーザーの唯一の手段。
  優先 2: 返答は 1〜2 文に留める。内容を作らない。
  優先 3: 同じリクエストに対して invoke_skill を再度呼ばない。
  優先 4: スキルが実行中の間は追加質問をしない。
```

### 変更 4 — `delegate_to_agent` の Behaviour ルールを追加

invoke_skill ルールの後に追加：

```markdown
## エージェントへの委譲

ユーザーのタスクがスキルではなく別エージェントに適合する場合:
  delegate_to_agent(to=<agent_name>, request=<ユーザーの要求>) を呼ぶ

使用すべき状況:
  - タスクが利用可能なスキルの範囲外で、別エージェントの役割に合致する
  - ユーザーが特定のエージェントを名指ししている

利用可能なスキルで解決できるタスクは委譲しないこと。
別エージェントは非同期で応答する。委譲した旨を 1 文で伝える。
```

### 変更 5 — JA の recall/memory 例文を追加

既存の disambiguation ブロック（行 354–366 の後）に追記：

```markdown
日本語入力の使い分け:
  - 「思い出して」「前回の話」「あのとき言ってた〜」
      → recall（indexed 検索）— indexed sources がある場合
      → list_memory / read_memory_body — indexed sources がない場合
  - 「覚えて」「メモして」「記録して」「保存して」「忘れないで」
      → remember_shared または remember_agent（memory 書き込み）
  - 「忘れて」「削除して」「消して」（memory エントリについて）
      → forget_memory
```

---

## 対象ファイル

| ファイル | 変更内容 |
|---|---|
| `src/reyn/chat/router_system_prompt.py` | 上記 5 つの変更すべて |

---

## Dependencies

なし。変更はすべて単一のプロンプト構築関数内。スキーマ・ランタイム・スキルの変更不要。

---

## Cost estimate

| タスク | コスト |
|---|---|
| セクション並び替え（変更 1） | SMALL |
| 意図軸統合（変更 2） | SMALL |
| spawn-ack 優先順位付け（変更 3） | SMALL |
| delegate_to_agent ルール追加（変更 4） | SMALL |
| JA 例文追加（変更 5） | SMALL |
| **合計** | **SMALL** |

変更はすべて文字列構築関数への編集。新しい抽象化・プロトコル変更なし。変更 1〜5 は独立しており、1 コミットでまとめても 5 コミットに分けても良い。

---

## Verification

1. **キャッシュ**: 変更 1 後、同じプロジェクトでの連続ターンで `cache_creation_input_tokens` が 2 ターン目以降に減少することを確認。
2. **ラベル漏れ**: 変更 2 後、10-shot dogfood でルーティングラベル（「Action」「Memory access」等）がユーザー返答に出ないことを確認。
3. **spawn-ack**: 変更 3 後、N=10 invoke_skill シナリオで `/tasks` が 100% の spawn-ack に含まれることを確認。
4. **委譲**: 変更 4 後、別エージェントに適合するタスクで `delegate_to_agent` が呼ばれることを確認（「できません」ではなく）。
5. **JA recall**: 変更 5 後、「思い出して」が indexed sources 存在時に `recall` にルーティングされることを確認（`list_memory` ではなく）。

---

## Related

- `src/reyn/chat/router_system_prompt.py` — 唯一の対象ファイル
- FP-0024 (`0024-router-sp-semantic-tool-selection.ja.md`) — 中期の後続 FP
- Dogfood バッチ B12-R2、B13-R3 — JA 例文の効果測定データ
- Anthropic "Writing Tools for Agents" (2025) — ツール記述のベストプラクティス
