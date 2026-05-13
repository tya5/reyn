# FP-0011: `skill_narrator` 廃止 — スキル結果の narrate をルーターに委ねる

**Status**: **LANDED 2026-05-10** (= commit `59c991a`、 A+B+C+D+E + Component B
anti-optimism 強化)。 Follow-up: N≥10 flash-strong-tier retest で 1/6 hallucination が ~0 まで下がるか確認。
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

ルーターの他のツール（`recall`、`list_skills`、`read_memory_body` など）はすべて、
構造化された結果を自然言語の返答に変換する責任をルーター LLM が担っている。
`invoke_skill` だけが例外で、ルーター LLM の次のターンより先に `skill_narrator` を
起動してしまう。これがアーキテクチャ上の非対称と二重出力リスクを生んでいる。
`skill_narrator` を廃止し、スキル結果も他のツール結果と同じようにルーター LLM が
narrate する設計に一本化する。

---

## Motivation

### narrate 責任の非対称

```
list_skills / recall / read_memory_body:
  router LLM → tool_call → 構造化結果 → router LLM が narrate → ユーザーに届く

invoke_skill（現状）:
  router LLM → invoke_skill → スキル実行
    → skill_narrator 起動 → reply を outbox へ push   ← ユーザーが ① を受信
    → tool result {"status": "finished", "data": {...}} を messages に積む
    → router LLM 再呼び出し → text reply を生成 → ユーザーが ② を受信
```

ルーター LLM はすでに任意の構造化結果を narrate できる能力を持っている
（他のすべてのツールで実証済み）。`invoke_skill` の結果だけを別扱いする
アーキテクチャ上の理由はない。

### 二重出力リスク

`skill_narrator` はルーター LLM のポストツールターンより**前に** `reply_text` を
`outbox` へ push する。`invoke_skill` の `dispatch_kind` はデフォルトの `"sync"` なので
ループは継続し、ルーター LLM がツール結果を受け取って再度呼び出される。
ルーター LLM が何かテキストを生成すると（empty-stop でない場合）、ユーザーは
2 つの独立した返答を受け取ることになる。

```
narrator の返答:  「コードレビューが完了しました。auth.py に 3 件の問題が見つかりました。」
router の返答:    「完了です！スキルが正常に終了しました。」   ← 重複
```

### empty-stop はバグとして扱われていたが、実は意図した挙動だった

G12 empty-stop attractor（`invoke_skill` 後にルーター LLM が `finish=stop` かつ
コンテンツなしで終了する現象）は信頼性のバグとして記録され、繰り返し修正されてきた。
振り返ると、narrator が narrate する設計であれば empty-stop は正しいルーター挙動だった。
narrator を廃止することでこの矛盾が解消される。ルーター LLM はテキストを生成すべきで、
そうすることができる。

### スキル完了ごとに余分な LLM コール

`skill_narrator` は純粋 LLM フェーズ（`allowed_ops: []`）で、スキル実行 1 回ごとに
フル LLM コール 1 回が発生する。narrator を廃止すると、スキル完了ごとにこの
ラウンドトリップが不要になる。

### 既知の品質問題: B2-M4

ドッグフード発見 B2-M4（重要度 MED）: narrator が `final_output` からドメイン的に
意味のあるフィールドを抽出せず、汎用的な「スキルが完了しました」テキストを返す。
これは専用 narration スキルの自己否定的な失敗例で、多様なツール出力をすでに扱って
いるルーター LLM の方が実際には堅牢。

---

## Proposed implementation

### Component A — `session.py` から narrator 呼び出しを削除（SMALL）

`_invoke_narrator()` と `NARRATOR_SKILL_NAME` を削除。
`_run_one_skill()` と `_run_skill_awaitable()` の両方で、narrator 呼び出しブロック
（`narrated` / fallback raw-dump で outbox に push する部分）を削除。
ツール結果はすでに変更なくルーターループに返っているため、その後の流れは変わらない。

```python
# _run_one_skill — このブロックを削除（〜行 2694–2739）
# narrated = await self._invoke_narrator(...)
# if narrated: ...
# else: fallback raw-dump ...

# _run_skill_awaitable — 同様のブロックを削除（〜行 3075–3114）
```

`_run_skill_awaitable` は従来通り `{"status": ..., "data": ...}` を返す。
ルーター LLM はこれを `invoke_skill` の tool result として受け取る。

### Component B — ルーターシステムプロンプトに post-`invoke_skill` ガイダンスを追加（SMALL）

現状のルーター SP には `invoke_skill` 返却後の動作指示がない。
ステータス別の narration ルールを追加する:

```
- invoke_skill が返ったら: スキルが何をしたかを 1〜2 文で要約して返答する。
  ユーザーに関係するフィールドを結果から抽出する — JSON をそのまま出力しない。
  ステータス別の対応:
    "finished"             → 完了を端的に伝え、次のステップがあればヒントを添える。
    "loop_limit_exceeded"  → フェーズ予算切れで終了したことを伝え、再実行を提案。
    その他                 → 何が完了しなかったかを説明し、最も可能性の高い対処を提案。
```

これは `narrate.md` フェーズ指示の内容と同等であり、実際に効果が出る場所（ルーター SP）に配置する。

### Component C — `skill_narrator` stdlib スキルを削除（SMALL）

`src/reyn/stdlib/skills/skill_narrator/` ディレクトリを削除。
`profile.py` から narrator の always-available バイパス設定を削除。
テストの `_KNOWN_SKILL_NAMES` から narrator を除去。

### Component D — narrator 専用テストを削除（SMALL）

- `tests/test_replay_narrator.py` を削除（Tier 3a — narrator LLM 挙動の replay テスト）
- `tests/test_narrator_drift.py` を削除（Tier 2b — narrator のドリフト検出不変条件）
- `tests/test_router_loop_chatsession.py`: narrator が `available_skills` から除外される
  アサーションを削除（narrator 自体がなくなるため）
- `tests/test_multi_agent_p7.py`: `_KNOWN_SKILL_NAMES` から `skill_narrator` を削除

### Component E — post-invoke_skill narration の Tier 2 契約テストを追加（SMALL）

`invoke_skill` が成功した後、ルーターが空でないテキスト返答を生成することを
確認する新しい Tier 2 不変条件テスト。narrator テストが提供していたカバレッジを置き換える。

---

## 変更しないもの

- `invoke_skill` ツール定義と `dispatch_kind="sync"` 登録 — 変更なし
- ルーターへ返される tool result 形式 `{"status": ..., "data": ...}` — 変更なし
- ルーターループの構造 — 変更なし
- P6 `skill_run_completed` イベント — 変更なし。
  narration 出力は `role=agent/source=narrator` として履歴に保存されなくなる。
  ルーター LLM のテキスト返答が履歴エントリになる。

---

## Dependencies

なし。この変更は単独で実施できる。

---

## Cost estimate

**合計: SMALL**

| タスク | コスト | 備考 |
|---|---|---|
| Component A: session.py から narrator 呼び出し削除 | SMALL | 〜60 行削除、新規ロジックなし |
| Component B: ルーター SP ガイダンス追加 | SMALL | 〜5 行追加 |
| Component C: skill_narrator 削除 + profile.py 更新 | SMALL | ディレクトリ削除 |
| Component D: narrator テスト削除 | SMALL | 2 ファイル削除、2 ファイル更新 |
| Component E: 新規 Tier 2 不変条件テスト | SMALL | 新規 contract test 1 件 |

リスク: 弱いモデルでのルーター LLM narration 品質の劣化。
Component B（SP ガイダンス）と Component E（contract test）で軽減。
ランディング前に G4 spike（`gemini-2.5-flash`）での実行を推奨し、
強いモデルでの narration 品質ベースラインを確認する。

---

## Related

- `src/reyn/chat/session.py` — `_invoke_narrator`、`_run_one_skill`、`_run_skill_awaitable`
- `src/reyn/stdlib/skills/skill_narrator/` — 削除対象スキル
- `src/reyn/chat/router_system_prompt.py` — Component B の挿入箇所
- `src/reyn/chat/profile.py` — narrator always-available バイパスの削除箇所
- `docs/deep-dives/journal/dogfood/2026-05-04-batch-2-real/findings/B2-M4-narrator-generic-completion.md`
