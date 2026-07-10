---
type: concept
topic: architecture
audience: [human, agent]
---

# Security

ケイパビリティのゲーティング、サンドボックス境界、トラスト スコーピング。目標は「オペレーターが承認していないケイパビリティを agent が暗黙的に取得せず、侵害された呼び出しが他の actor にエスカレートできないこと」です。

## Reyn の実装方法

### 三層の Permission モデル

```
defaults(常時有効)
   ↓ actor がより多くを必要とする場合
declared capability → 使用時点での JIT prompt(起動時ではない)
   ↓ プロジェクトを広く信頼する場合
プロジェクト全体の事前承認(reyn.yaml)
```

デフォルトは保守的です — プロジェクトルート配下であればどこでも read/glob/grep、write/edit/delete は `.reyn/` 配下のみ(それでも `.reyn/approvals.yaml` と `.reyn/index/sources.yaml` は狭い carve-out の対象です。これらのパスには直接書き込みを捕まえる downstream の use-time ゲートが無いためです)。それ以上はシェルも MCP も Python も一切ありません。それ以上必要なら declared capability が必要で、起動時の一括プロンプトではなく、実際に使用される時点で JIT にプロンプトされます。

**この 3 層の分割と charter の「4層 JIT approval」は矛盾ではなく、別の軸です。** この 3 層は *grant hierarchy* — actor の認可がどれだけ広いか(defaults / declared / project-wide)です。Charter の 4 層の記述は、JIT prompt 自体が実際にユーザーへ尋ねる前にチェックする *approval-source の解決順序* です: config 事前承認 → saved approvals(`.reyn/approvals.yaml`)→ session approvals(in-memory、現在の呼び出し限り)→ interactive prompt(最後の手段)。この 4 層の解決は、このセクションの中間層(「declared capability → JIT prompt」)の内部にまるごと存在します。

### Actor スコープの承認

永続的な承認選択は `<actor>/<op>/<path>` をキーとして `.reyn/approvals.yaml` に記録されます。キーは skill でもユーザーでもなく actor スコープです: ある actor の承認は別の actor に漏れません。これが組み合わせの安全性の性質です — chat router 自身の dispatch パスに付与された承認は、例えば background hook や cron caller のような別の actor identity 経由で動作するものには推移的に及びません。

### `sandboxed_exec` — typed で per-axis な `SandboxPolicy`

サブプロセス実行は `SandboxPolicy` でゲートされ、各 axis は意図的に非対称で、実際に安全性を買う厳しさにそれぞれ設定されています: `write_paths` はタイトな allowlist(プロセスが永続化できるものへのハードガード)、`network` はデフォルト off(流出ゲート)、`allow_subprocess` は子プロセス生成を制限し、`read` はデフォルトで broad-allow に加えオプションのセンシティブパス deny-list(`read_deny_paths`)を持ちます — 厳格な read-allowlist モデルは廃止されました。流出を実際に止めているのは read サーフェスではなく network ゲートだからです。enforcement はプラットフォームごとにバックエンドが選択されます(macOS では Seatbelt、Linux では Landlock + seccomp-BPF、どちらも使えない場合は audit-only の `NoopBackend` フォールバック)。

### 非インタラクティブな承認(run-once、CI)

`reyn run-once` はプロンプトを表示しません。ランの前に Permission が整っている必要があります。`reyn.yaml` で事前承認されているか(`permissions.<key>: allow`)、以前のインタラクティブなランから永続化されているかのどちらかです。モードが変わっても信頼モデルは変わりません。非対話ランはあなたがすでに下した決定を継承します。

### コンテンツレイヤー防御

信頼できないコンテンツは、LLM プロンプトに入る OS のシームでスキャン・フェンスされます。2 つのプリミティブ:

- **パターンスキャン**(`security/threat_patterns.py`)— インジェクション / 流出 / role-hijack / exec-scope 脅威の正規表現ベース検出。マッチは脅威イベントを emit し、block パターンは操作を中止します。
- **構造フェンス**(`security/content_fence.py`)— 明示的なデリミタで信頼できないコンテンツを囲み、モデルが指示ではなくデータとして扱うようにします。

これらのプリミティブは以下の OS シームに適用されます。各シームはその信頼方向に合った機構を使います(read シームはスキャン・フェンス、write シームはブロック):
- **ツール結果** — `security/content_guard.py` 経由で、プロンプト到達前にスキャン(全結果)＋構造フェンス(external-content 結果のみ)
- **メモリ書き込み** — 脅威パターンに一致する書き込みは router レベルでブロック
- **コンテキストファイル**(REYN.md/AGENTS.md)— ロード時にフェンス
- **A2A inbound メッセージ** — 到着時にフェンス＋スキャン
- **実行前コマンド** — `sandboxed_exec` がサブプロセス起動前に joined argv 全体を exec-scope 脅威についてスキャン
- **コンパクション入力** — シークレットらしきコンテンツは要約が永続化される前に除去(`security/secret_redaction.py`)

#### 構造フェンスの対象範囲

スキャンは広範(検出テレメトリのため read シームの全コンテンツで実行)ですが、**構造フェンス**は選択的に適用されます — *信頼できないソース*由来のコンテンツのみを、フェンスが有効なときに限り囲みます。2 つのゲートで決まります:

1. **設定ゲート** — `safety.threat_scan.enabled` *かつ* `safety.threat_scan.fence_enabled` が両方 on である必要があります(両方デフォルト `true`)。どちらかが off → コンテンツはフェンスされず通過します。
2. **ソース信頼ゲート** — シームごとに適用。信頼できる内部コンテンツ(OS 自身の framing、オペレーターが入力したもの)は決してフェンスされず、信頼できないソースのコンテンツのみがフェンスされます。

両ゲートが開いているとき、現在フェンスされるコンテンツ対象は以下:

| フェンス対象 | 内容 | ソース信頼ルール |
|---|---|---|
| **external-content ツール結果** | 外部コンテンツを返すツールの結果 — web fetch / web search、MCP 呼び出しとサーバー由来のツール記述、**recall / RAG 結果**、**メモリエントリの読み取り** | external content を返すとフラグされたツールのみフェンス。その他(信頼できる内部)のツール結果は**スキャンのみ**でフェンスされない |
| **プロジェクトコンテキストファイル** | システムプロンプトに差し込まれる `REYN.md` / `AGENTS.md` / `project_context_path` のテキスト | 常にフェンス — オペレーター編集可能なファイルはデータとして扱う |
| **A2A inbound peer メッセージ** | リモート peer エージェントからのメッセージテキスト(history に入る前) | 常にフェンス — リモート peer は信頼境界の外 |
| **外部 intervention 回答** | 外部 peer(A2A POST / webhook)から届いた回答 | history 連動(context)コピーのみフェンス。バッファ / choice-match された回答と監査レコードは raw のまま |
| **タスククエリ結果** | タスク read / list op が返すタスクの自由記述フィールド `description` / `name` / `result` | 常にフェンス。構造フィールド(id / status / 依存 / 日付)は OS 生成でフェンスしない |
| **委譲タスクの wake 記述** | assignee に実行を指示する wake メッセージに載る委譲タスクの description | 常にデータとしてフェンス。OS の「あなたが assignee — これを実行せよ」という framing が信頼される指示 |

したがってメモリは**両方向**でカバーされます: メモリの**読み取り**(recall またはメモリツール結果)は external content として入口でフェンスされ、メモリの**書き込み**は write シームでパターン**ブロック**されます — 別機構です(上記シームリスト参照)。意図的にフェンスされ*ない*コンテンツ: 信頼できる内部ツール結果、オペレーターの直接入力(`ask_user`、チャットメッセージ — 定義上信頼される)、実行前コマンドの argv(スキャンはするがフェンスしない)、コンパクション入力(シークレット除去はするがフェンスしない)。

## まだ薄い部分

**コンテンツレイヤー防御はシームベースの正規表現検出であり、プロンプトインジェクションの保証ではありません。** パターンスキャンは OS シームで既知の攻撃形状を捉えますが、正規表現に一致しない新規・難読化されたペイロードは通過します。信頼できないコンテンツがフェンスされてプロンプトに入った後でも、LLM は認識可能な攻撃パターンではなく自然言語として読める埋め込み指示に従う可能性があります。OS は LLM の*応答*をインジェクション残渣についてゲートしません — ケイパビリティ被害は Permission システムで制限されます(承認パス外への書き込み不可、宣言された `SandboxPolicy` 外での `sandboxed_exec` 不可)が、応答レベルの傍受は実装されていません。

**Landlock バックエンドの read-deny-list は enforce できません。** macOS では Seatbelt の last-match-wins セマンティクスにより、broad read-allow をセンシティブなパス(`~/.ssh`、`~/.aws` など)の deny-list で狭められます。Landlock(Linux)は allowlist-only です — より広く許可された親から、センシティブなサブパスを後から切り出すことはできません — そのため Linux では sandbox 内で侵害されたプロセスがそれらのパスを読み取れます。主たる境界(write-allowlist + network-off)は両バックエンドで同一に保持されます。deny-list は Linux では defense-in-depth であり、そこでの主たる保証ではありません。

## 関連情報

- [`docs/concepts/architecture/charter.md`](../architecture/charter.md) — 7 つの feature family すべてで grounded された Security 行
- [../runtime/permission-model.md](../runtime/permission-model.md) — JIT prompt UX と監査証跡を含む完全な permission モデル
- [../runtime/sandbox.md](../runtime/sandbox.md) — 完全な `SandboxPolicy` フィールドリファレンスとバックエンド選択表
- [リファレンス: permissions](../../reference/config/permissions.md) — 完全なスキーマ
- [ハウツー: Permission の管理](../../guide/for-users/manage-permissions.md)
- [reliability-engineering.md](reliability-engineering.md) — op が拒否されたときに何が起こるか
- [Feature map — Content-layer defense](../../feature-map.md#content-layer-defense) — 完全な機構インベントリ
