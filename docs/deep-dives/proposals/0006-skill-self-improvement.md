# FP-0006: スキル自己改善 — 実行トレース駆動 + バージョン管理 + ロールバック

**Status**: proposed
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

`skill_improver` stdlib スキルは eval スコアベースの改善ループとして既に動作している。
これを拡張し、P6 イベントログ（実行トレース）を改善入力として使えるようにする。
あわせて `.reyn/skill-versions/` へのバージョン保存・`skill_version_hash` のイベント記録・
`reyn skill rollback` CLI を追加することで、Hermes の GEPA と同等の自己改善を
Permission model と ask_user 承認ゲートのもとで安全に実現する。

---

## Motivation

### Hermes GEPA との比較

Hermes Agent（ICLR 2026 Oral）は GEPA により実行トレースからスキルを自動改善し
繰り返しタスクで 40% の速度向上を報告している。ただしスキル変更は OS の外側で
副作用として起きるため、変更の追跡・権限制御・ロールバックが原理的に困難。

Reyn では同じ自己改善を「スキルとして実行し Permission model を通す」設計にすることで
ガバナンスを維持したまま実現できる。

### skill_improver の現状

現行 `skill_improver` は以下のフェーズで動作中（変更なし）:

```
prepare → copy_to_work → run_and_eval → plan_improvements → apply_improvements → finalize
                                ↑__________________________________|
```

- ワークスペースの **コピー** に対して改善を実施（元ファイルを直接変更しない）
- eval スコアが閾値 (0.85) 超 / regression / stagnation / max_iterations で終了
- finalize フェーズで改善済みファイルを元の場所にコピー戻す

**追加するのは:**
1. 実行トレース（P6 イベントログ）を改善入力として使うモード
2. バージョン保存と `skill_version_hash` の記録
3. ユーザー承認ゲート
4. ロールバック CLI

### バージョン管理が解く問題

現状の `skill_improver` は「改善後にどのバージョンが実行されたか」の記録がない。
以下の問いに答えられない:

- 「v1 と v2 でどちらが成功率が高かったか」
- 「先週の改善後から失敗率が上がった。元に戻したい」
- 「この実行は改善前と後、どちらのスキルで行ったか」

---

## Proposed implementation

### Component A — `skill_version_hash` をイベントに追加（SMALL）

`src/reyn/op_runtime/run_skill.py` の `run_skill_started` emit に
skill.md の content hash を追加する。

```python
# 変更前（run_skill.py:73 付近）
event_log.emit("run_skill_started", skill=skill_name, state_dir=str(state_dir))

# 変更後
skill_hash = _compute_skill_hash(skill_path)  # sha256(skill.md の内容)
event_log.emit("run_skill_started", skill=skill_name, state_dir=str(state_dir),
               skill_version_hash=skill_hash)
```

効果: 「このハッシュのスキルで実行した 50 回のうち成功率 85%」という履歴が
P6 イベントログに自然に蓄積される。`collect_traces` フェーズがこれを活用する。

### Component B — `.reyn/skill-versions/` バージョン保存（SMALL）

`skill_improver` の `finalize` フェーズが改善済みスキルを apply するとき、
同時にバージョンアーカイブを保存する。

```
.reyn/skill-versions/
  my_skill/
    v1.md      ← 初回 apply 時（apply 前のオリジナルを保存）
    v2.md      ← 1 回目の改善適用後
    v3.md      ← 2 回目の改善適用後
    current    ← "3"（カレントバージョン番号）
```

`.reyn/` はデフォルト書き込みゾーンなので **Permission 宣言不要**。

バージョン数が `self_improvement.max_versions`（デフォルト 10）を超えた場合、
最古のバージョンから削除する。ただし `current` が参照するバージョンは削除しない。

### Component C — 実行トレース駆動モード（MEDIUM）

```yaml
# skill_improver への入力パラメータ（新規追加）
improvement_source: traces   # traces | tests | both (デフォルト: tests — 既存互換)
trace_lookback_runs: 20      # 直近 N 回の実行を参照
```

`improvement_source: traces` または `both` の場合、
`collect_traces` フェーズ（新規）を `copy_to_work` の前に挿入:

```
prepare → collect_traces → copy_to_work → plan_improvements → apply_improvements → finalize
```

**collect_traces フェーズの動作:**

```markdown
# collect_traces

対象スキルの実行履歴を P6 イベントログから収集し、
改善に役立つ分析サマリーを workspace に保存する。

収集対象:
- run_skill_started / run_skill_completed（skill_version_hash でフィルタ）
- skill_node_started / skill_node_completed
- tool_executed（失敗したオペレーション）
- 直近 trace_lookback_runs 件に限定

出力: traces_summary.md（成功率・失敗パターン・頻出エラーのサマリー）
```

`plan_improvements` フェーズは `traces_summary.md` を参照して改善案を生成する。
`improvement_source: tests` の場合は既存の `run_and_eval` 結果のみを参照（変更なし）。

### Component D — `on_propose` 設定 + ask_user 承認ゲート（SMALL）

```yaml
# reyn.yaml
self_improvement:
  on_propose: ask_user   # ask_user | auto | disabled (デフォルト: ask_user)
  max_versions: 10
```

| モード | 動作 |
|---|---|
| `ask_user` | finalize でユーザーに改善適用の承認を求める（デフォルト）|
| `auto` | 承認なしで自動適用（信頼済み環境・CI 向け）|
| `disabled` | 改善を適用しない（ドライランのみ）|

`on_propose: ask_user` のとき、`finalize` フェーズは InterventionBus 経由で
ask_user を発行する（FP-0005 / FP-0003 と同じ機構）。

### Component E — `reyn skill rollback` CLI（SMALL）

```
reyn skill rollback <skill_name>           # 直前バージョンに戻す
reyn skill rollback <skill_name> --to v2   # 指定バージョンに戻す
reyn skill versions <skill_name>           # バージョン一覧を表示
```

`reyn skill versions` の出力例:

```
my_skill バージョン履歴:
  v1  2026-05-01 10:00  (初回保存)
  v2  2026-05-05 14:30  改善: plan_improvements フェーズの instruction 改善
  v3  2026-05-09 09:15  改善: collect_traces による失敗パターン対応  ← current
```

**ロールバックの内部実装:**

```python
# .reyn/skill-versions/<name>/v<N>.md の内容を
# reyn/project/<name>/skill.md に write_file する
# → Permission check（reyn/project/ への書き込みは Permission 宣言が必要）
# → P6 に skill_rolled_back イベントを emit
#   { skill: "my_skill", from_version: 3, to_version: 1, reason: "user rollback" }
```

ロールバック自体も Permission model を通るため、
権限のないスキルへのロールバックは PermissionError になる。

### メタ改善（新実装不要）

`src/stdlib/skills/skill_improver/skill.md` への書き込みは
`src/` がデフォルトゾーン外のため Permission 宣言なしで PermissionError になる。
ユーザーが明示的に `permissions.file.write` に stdlib パスを追加した場合のみ動作する。

**Permission model が自動的にメタ改善をデフォルト禁止にするため、追加実装不要。**

---

## Hermes GEPA との比較

| | Hermes GEPA | Reyn（本 FP 実装後）|
|---|---|---|
| 改善の実行主体 | OS 外の副作用 | `skill_improver` スキル（OS 内）|
| 改善のトリガー | 5+ ツール呼び出しで自動 | ユーザー実行 or cron（FP-0001）|
| Permission check | なし | write_file op → Permission model |
| ユーザー承認 | 不可 | `on_propose: ask_user` で制御可 |
| 変更の記録 | なし | P6 に `skill_improved` イベント |
| 壊れたときの回復 | 困難（何が変わったか不明）| `reyn skill rollback` + P6 追跡 |
| 再現性 | 保証なし | `skill_version_hash` で run と version を紐付け |
| メタ改善 | 制限なし | Permission model でデフォルト禁止 |

---

## Dependencies

- `src/reyn/op_runtime/run_skill.py` — Component A（`skill_version_hash` 追加）
- `src/reyn/stdlib/skills/skill_improver/` — Component B/C/D（フェーズ拡張）
- `src/reyn/config.py` — `SelfImprovementConfig` データクラス追加
- `src/reyn/cli/skill.py` — `rollback` / `versions` サブコマンド追加
- `src/reyn/user_intervention.py` / InterventionBus — Component D（ask_user、変更不要）

前提 PR: なし。Component A（SMALL）は単独でリリース可能。
FP-0005 と同じ InterventionBus を使うが、FP-0005 未完了でも ask_user 既存実装で代替可能。

---

## Cost estimate

**合計: MEDIUM**

| タスク | コスト | 備考 |
|---|---|---|
| Component A: `skill_version_hash` イベント追加 | SMALL | 1 ファイル、1 箇所の変更 |
| Component B: `.reyn/skill-versions/` 保存 | SMALL | finalize フェーズの Markdown 変更 |
| Component C: `collect_traces` フェーズ新規作成 | MEDIUM | 新規フェーズ + skill.md グラフ更新 |
| Component D: `on_propose` 設定 + ask_user | SMALL | config + finalize フェーズに分岐追加 |
| Component E: `reyn skill rollback` CLI | SMALL | CLI サブコマンド 2 つ + バージョンリスト読み取り |
| テスト（Tier 1 / Tier 2） | SMALL | Component A の contract test が主 |

ボトルネックは **Component C**（`collect_traces` フェーズの設計と、
`plan_improvements` フェーズが traces_summary.md を適切に活用できるかの調整）。

---

## Related

- `src/reyn/stdlib/skills/skill_improver/` — 既存実装
- `src/reyn/op_runtime/run_skill.py` — Component A の変更対象
- `src/reyn/events/events.py` — P6 イベント emit 機構
- `src/reyn/permissions/permissions.py` — デフォルト書き込みゾーン定義
- FP-0003 (`0003-budget-exceed-user-approval.md`) — ask_user 機構（Component D と同じ）
- FP-0005 (`0005-safety-as-checkpoint.md`) — InterventionBus の共有
- `docs/deep-dives/research/competitive/hermes-agent.md` — GEPA との設計比較
