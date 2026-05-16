---
type: concept
topic: skill-self-improvement
audience: [human, agent]
---

# スキル自己改善

Reyn のスキルは実行トレースから自動的に改善できます — バージョンアーカイブと 1 コマンドロールバック付きで。プロセス全体が、OS の外で発生する副作用ではなく、ガバナンス下のスキル（`skill_improver`）として実行されます。つまりすべての改善が[パーミッションモデル](permission-model.md)を通過し、ユーザー承認ゲートを設定でき、`skill_version_hash` を介して実行履歴と紐付けられます。5 つのコンポーネントが FP-0006 として 2026-05-15 に一括着地しました。

Hermes GEPA は 5+ ツール呼び出し後に OS 外の無制限な副作用として自己改善をトリガーします。Reyn の設計ではスキル改善をオペレーター管理下の first-class な操作として扱います。詳細は[Hermes GEPA との比較](#hermes-gepa-との比較)を参照してください。

## 全体像

```
skill_improver (stdlib スキル)
    │
    ├─ optional: collect_traces フェーズ ──► recall(sources=["events"]) → traces_summary.md
    │       (FP-0006 C — FP-0009 の events index が必要)
    │
    ├─ run_and_eval / plan_improvements / apply_improvements
    │
    └─ finalize
           ├─ apply 前に skill.md をスナップショット → .reyn/skill-versions/<name>/v<N>.md  (FP-0006 B)
           ├─ ask_user ゲート (設定: on_propose)                                             (FP-0006 D)
           └─ apply
              → run_skill_started イベントに skill_version_hash を付与                       (FP-0006 A)

監査 + 復元:
    reyn skill versions <name>   保存済みバージョン一覧      (FP-0006 E)
    reyn skill rollback <name>   前バージョンに戻す           (FP-0006 E)
    → skill_rolled_back P6 イベントを emit                   (FP-0006 E + follow-up)
```

`collect_traces` フェーズはオプションです — [Operational Intelligence](operational-intelligence.ja.md)（FP-0009）が events ログをインデックス化していることが前提です。インデックスが存在しない場合、`skill_improver` はトレース駆動コンテキストなしで `run_and_eval` を直接実行するフォールバックに切り替わります。

## コンポーネント一覧

| コンポーネント | 追加内容 | ソース |
|---|---|---|
| A | すべての `run_skill_started` イベントに `skill_version_hash` フィールドを付与 | `src/reyn/op_runtime/run_skill.py` |
| B | `.reyn/skill-versions/<name>/v<N>.md` スナップショット + `current` ポインタ | `skill_improver/version_snapshot.py` + `phases/finalize.md` |
| C | `collect_traces` フェーズ（recall パス + raw-events フォールバック） | `skill_improver/trace_collector.py` + `phases/collect_traces.md` |
| D | `on_propose: ask_user\|auto\|disabled` 設定 + finalize ゲート | `src/reyn/config.py` `SelfImprovementConfig` + `phases/finalize.md` |
| E | `reyn skill versions / rollback` CLI | `src/reyn/cli/commands/skill.py` |

## ワークフロー詳細

`my_skill` というプロジェクトスキルの自己改善を例に説明します。

**1. `skill_improver` を起動**

```bash
reyn run skill_improver '{"target": "my_skill", "improvement_source": "traces"}'
```

**2. トレース収集**

`skill_improver` が `recall(sources=["events"], query="my_skill の失敗パターン")` を呼び出し、直近のランの構造化サマリーを取得します（フェーズパス・エラー種別・コスト・`skill_version_hash` 別 pass rate）。結果はワークスペースの `traces_summary.md` に書き出されます。

**3. 改善計画と適用**

`plan_improvements` が `my_skill/skill.md`（インストラクション・フェーズグラフ・評価基準）への具体的な変更案を作成します。`apply_improvements` は `write_file` Control IR op 経由で改訂ファイルを書き込みます — パーミッションモデルによるゲートは通常の書き込みと同じです。

**4. eval 実行**

`run_and_eval` が `my_skill` を eval セットに対して実行し、pass rate スコアを算出します。スコアが `skill_improver` の eval 基準の閾値を下回る場合、設定された反復上限までリトライします。

**5. finalize — バージョンスナップショット + ユーザーゲート**

承認閾値に達すると `finalize` が:

- 現在の `my_skill/skill.md` を読み込み、`.reyn/skill-versions/my_skill/v2.md` に書き出す。
- `current` ポインタファイルを `"3"`（apply 後の新バージョン番号）に更新。
- `on_propose: ask_user`（デフォルト）の場合、`ask_user` 介入を発行:

  ```
  v3 を my_skill に適用しますか？（eval スコア: 0.85 → 0.92）
  [適用] [破棄]
  ```

- 承認後、改善済み `skill.md` を `reyn/project/my_skill/` に書き戻す。

**6. 次回実行時のバージョンハッシュ**

`my_skill` の次回実行時、`run_skill_started` イベントには新しい `skill.md` の sha256 が `skill_version_hash` として記録されます。`reyn eval compare` はハッシュ別にランをグルーピングし、回帰を自動検出できます。

**7. 必要に応じてロールバック**

```bash
reyn skill versions my_skill
#   v1  2026-05-01  (initial save)
#   v2  2026-05-05  improvement: plan_improvements フェーズのインストラクション改善
#   v3  2026-05-09  improvement: collect_traces によるエラーパターン対応  ← current

reyn skill rollback my_skill --to v2
```

ロールバックはアーカイブ済みの `v2.md` を `write_file` op 経由（パーミッションチェック済み）で `reyn/project/my_skill/skill.md` に書き戻し、P6 `skill_rolled_back` イベントを emit します:

```json
{"skill": "my_skill", "from_version": 3, "to_version": 2, "reason": "user rollback"}
```

## 設定（`reyn.yaml`）

```yaml
self_improvement:
  on_propose: ask_user   # ask_user | auto | disabled（デフォルト: ask_user）
  max_versions: 10       # スキルごとの保存バージョン上限（デフォルト: 10）
```

| モード | 動作 |
|---|---|
| `ask_user` | デフォルト。`finalize` が改善差分と eval デルタを表示して一時停止。ユーザーが承認または破棄を選択してから変更が確定する。 |
| `auto` | プロンプトなしで `finalize` が適用。オペレータの信頼が確立された CI パイプラインやスケジュールバッチ実行向け。 |
| `disabled` | `skill_improver` は全フェーズを実行して提案差分をアーティファクトとして emit するが、スキルへの書き戻しは行わない。ドライランモード。 |

`max_versions` に達すると、`finalize` は新しいスナップショットを書き込む前に最古のバージョン（`v1`）を削除します。

## パーミッションモデルとの統合

パーミッションモデルはメタ改善と stdlib 保護を特別なロジックなしに処理します。

**メタ改善はデフォルトで自動禁止。** `src/reyn/stdlib/` はデフォルトの書き込みゾーン外です。`skill_improver` 自身やその他の stdlib スキルを改善しようとすると、`write_file` op のディスパッチ段階で `PermissionError` になります。OS レイヤーで特別なチェックは不要（P7 準拠）。

**stdlib スキルのロールバックは CLI が拒否。** `reyn skill rollback` は `reyn/project/` および `reyn/local/` スキルのみを対象とします。stdlib スキル（`src/reyn/stdlib/skills/`）はシップバンドルされており、不変です。stdlib スキルをカスタマイズしたい場合は `reyn/project/<name>/` にコピーしてください — スキル解決順序（`reyn/project/` > `reyn/local/` > `src/reyn/stdlib/skills/`）によりプロジェクトコピーが優先されます。

**`on_propose: auto` はオペレータの信頼が必要。** インタラクティブ利用には `ask_user` が適切なデフォルトです。`auto` に切り替えるのは、オペレータが改善パイプラインをレビュー済みで自律的な書き込みを許容する環境（例: 1 週間のトレース評価後に `skill_improver` を実行する夜間 CI ジョブ）に限定してください。

## Hermes GEPA との比較

Hermes の GEPA はエージェントランタイム外の無制限な副作用として改善をトリガーします。Reyn はガバナンス下のスキル実行として改善を扱います。

| | Hermes GEPA | Reyn `skill_improver` |
|---|---|---|
| 実行モデル | OS 外の副作用 | stdlib スキル — OS ランタイムによるガバナンス |
| トリガー | 5+ ツール呼び出し後に自動 | ユーザー起動または cron（FP-0001） |
| パーミッションチェック | なし | `write_file` op → パーミッションモデル |
| ユーザー承認 | 不可 | `on_propose: ask_user\|auto\|disabled` |
| 変更記録 | なし | P6 監査ログの `skill_improved` イベント |
| 問題発生時の復元 | 困難（変更内容が不明） | `reyn skill rollback` + P6 イベントトレース |
| 再現性 | 保証されない | `skill_version_hash` で全ランをバージョンに紐付け |
| メタ改善 | 無制限 | パーミッションモデルによりデフォルト禁止 |

Hermes GEPA の詳細分析は [`docs/deep-dives/research/competitive/hermes-agent.md`](../deep-dives/research/competitive/hermes-agent.md) を参照してください。

## 関連情報

- [FP-0006: スキル自己改善](../deep-dives/proposals/0006-skill-self-improvement.ja.md) — 設計の詳細とコンポーネント実装ノート
- [リファレンス: `reyn skill versions / rollback`](../reference/cli/skill.ja.md) — CLI リファレンス
- [リファレンス: Events — `skill_rolled_back`](../reference/runtime/events.ja.md) — P6 イベントスキーマ
- [コンセプト: Operational Intelligence](operational-intelligence.ja.md) — Component C が依存する events RAG
- [コンセプト: パーミッションモデル](permission-model.ja.md) — メタ改善をゲートするパーミッションモデルの仕組み
- [Stdlib: `skill_improver`](../reference/stdlib/skill_improver.ja.md) — スキル自体のリファレンス
