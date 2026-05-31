# Skill resume

プロセスのクラッシュ後に Reyn が実行中の Skill を復元する仕組みです。

## 復元される内容

Skill が実行中に Reyn プロセスが終了した場合（kill -9、OOM、マシン再起動など）、次の `reyn chat` 起動時に自動的に以下が行われます。

1. エージェントごとの snapshot を読み込む（`AgentSnapshot.load`）
2. WAL を最新の既知状態まで前方リプレイする
3. 実行中だった各 Skill について:
   - Skill ごとの snapshot を読み込む
   - snapshot と WAL イベントから `ResumePlan` を構築する
   - `SkillResumeCoordinator` でアクションを決定する（デフォルト: 曖昧な副作用 op はリトライ、実行中 Phase から再開）
4. 各アクティブな Skill をその `current_phase` から再開する（ファストフォワード）

完了済みの Phase は再実行されません。実行中 Phase の中では、すでにコミット済みの副作用 op はメモ化されます（WAL から結果を読み込み、再呼び出しはしない）。また、その Phase 内の LLM 呼び出しもメモ化されるため、再開時に LLM コストが再発生しません。

**World-purity op は再開時に再実行されます。** 読み取り専用のネットワーク呼び出し（`web_fetch`、`web_search` など）は `world` purity として分類されます — その結果は外部状態に依存し、変化している可能性があるため、再開時には記録された結果をリプレイするのではなく呼び出しを再発行します。これにより、一時的な API の問題（フレーキーな検索が「0 件」を返すなど）が Skill の状態に永続的に固定されることを防ぎます。副作用 op（`file/write`、`mcp/call_tool` の書き込み）と LLM 呼び出しは、コスト削減と重複書き込み防止のため引き続きメモ化されます。

## クラッシュをまたいで保持される状態

| 状態 | 保存場所 | クラッシュ後も存続 |
|---|---|---|
| Workspace artifacts | `.reyn/agents/<name>/workspace/` | はい |
| エージェントごとの状態（受信ボックス、チェーン、介入） | `agents/<name>/state/snapshot.json` | はい |
| Skill ごとの状態（現在の Phase、訪問カウント） | `agents/<name>/state/skills/<run_id>.snapshot.json` | はい |
| WAL（コミット済み op + LLM レスポンス） | `.reyn/state/wal.jsonl` | はい |
| アクティブな asyncio.Tasks | インメモリのみ | いいえ — 再起動時に新規タスクで再開 |

## 曖昧なステップと再開ポリシー

「副作用」op（例: 外部に書き込む `mcp/call_tool`、`file/write`、`shell`）は、基盤となる呼び出しを実行する前に `step_started` を WAL に発行します。`step_started` の後、`step_completed` の前にプロセスがクラッシュした場合、再開システムは副作用が実際に発生したかどうかを判断できません — この op は **曖昧（ambiguous）** です。

Coordinator は `reyn.yaml` の再開ポリシーを適用します。

```yaml
skill_resume:
  default: retry             # デフォルト: 曖昧な op を再呼び出し
  per_skill:
    blog_publisher: discard_skill  # 外部公開: 重複のリスクを避ける
    eval_runner: skip              # 冪等な読み取り — skip が安全
```

ポリシー値:

- `retry` — **デフォルト**。op を再呼び出しします。読み取り専用および冪等な op では安全です（自動再開の設計において自然な選択肢でもあります。読み取り API のメモは world-purity ルールにより再開時に無効化されます）。リスク: 冪等でない書き込みでは副作用が重複する可能性があります。
- `skip` — 曖昧なステップを空の結果で完了済みとして扱います。副作用の重複を防ぎますが、Skill は op が成功したかのように続行します。リスク: 下流でデータが欠損する可能性があります。
- `discard_skill` — Skill run 全体を中断します。
- `prompt` — レガシー / no-op。自動再開はインタラクティブなプロンプトでブロックしません。`prompt` を指定しても `retry` と同等に扱われます。

## 手動制御

個々の run を管理する必要がある場合:

```
/skill list                  # アクティブな Skill run を表示
/skill discard <run_id>      # 特定の run を 1 つ中断
```

新規に開始する場合:

```bash
reyn chat --no-restore       # この run では復元をスキップ（状態はディスク上に残る）
reyn chat --reset            # 実行中の Skill 状態を消去（確認あり）
```

## 関連情報

- [Upgrade policy](../../reference/upgrade-policy.md) — schema バージョンの拒否と `--reset` による修復
- [Permission model](../runtime/permission-model.md) — 副作用として扱われるものの定義
- [Events](../runtime/events.md) — WAL + 監査ログのアーキテクチャ
