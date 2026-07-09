---
type: concept
topic: architecture
audience: [human, agent]
---

# Security

> **状態: partially stale。** 以下の「3層 permission モデル」「phase レベル宣言」
> 「skill スコープの承認」セクションは、削除済みの phase-graph skill engine の
> 承認フロー(phase の frontmatter 宣言ごと、`run_skill` の継承)を記述しています
> — 現行ソースにどちらの概念も存在しないことを直接 grep で確認済みです。現行モデルは
> `docs/concepts/runtime/permission-model.ja.md` に記述されている 4 層の
> just-in-time capability 承認フロー(config 事前承認 → saved → session →
> interactive prompt)です。「Python preprocessor 用 AST サンドボックス」セクションは、
> 現行の pipeline DSL に存在しないステップ種別を記述しています。**以下の
> 「Content-layer defense」セクション(とそれ以下すべて)は現行かつ影響を受けません**
> — `docs/feature-map.md` の Content-layer defense セクションと一致しています。
> stale なセクションの書き直しは follow-up として追跡されています。

ケイパビリティのゲーティング、サンドボックス境界、トラスト スコーピング。目標は「ユーザーが承認していないケイパビリティをワークフローが暗黙的に取得せず、侵害されたワークフローが他のワークフローにエスカレートできないこと」です。

## Reyn の実装方法

### 三層の Permission モデル

```
defaults (常時有効)
   ↓ ワークフローがより多くを必要とする場合
Phase の宣言 → ユーザーが起動時に承認
   ↓ プロジェクトを広く信頼する場合
プロジェクト全体の事前承認 (reyn.yaml)
```

デフォルトは保守的です。プロジェクトルート配下であればどこでも読み取り、書き込みは `.reyn/` と `reyn/` 配下のみ、シェルなし、MCP なし、Python なし。それ以上は上位レイヤーのいずれかでオプトインが必要です。

### Phase レベルの宣言 + インタラクティブな承認

Phase は frontmatter で必要なケイパビリティを宣言します。起動時にランタイムが単一の承認プロンプトを表示します。永続的な選択は `<skill>/<op>/<path>` をキーとして `.reyn/approvals.yaml` に記録されます。

### Skill スコープの承認

承認はユーザーではなく Skill をキーとします。Skill A に `file.write:/tmp/output` が付与された場合、サブスキル B（`run_skill` 経由で呼び出される）はその付与を推移的に継承しません。B は自身で要求する必要があります。これが組み合わせの安全性です: 1 つの Skill を信頼することは、それが呼び出す可能性のあるすべてを信頼することではありません。

### Python preprocessor ステップの AST サンドボックス

`python` preprocessor ステップは常にサンドボックス化されます: allowlist に対して AST 検証されます（`open`、`eval`、`exec`、`__import__`、`compile`、`subprocess` などは禁止）。インポートはキュレートされた allowlist に制限されます（`math`、`statistics`、`json`、`re`、`random`、`time`、`datetime` など、`reyn.yaml` で拡張可能）。制限された `__builtins__`。クラッシュ分離のためにウォールクロックタイムアウト付きのサブプロセスで実行されます。

非ambient なケイパビリティ（operator のファイル、ネットワーク、環境変数、プロセス起動）が必要なステップは、その I/O を `run_op` ステップ（独自の permission gate と event-log エントリーを持つ）に分離するか、permission でゲートされた `reyn.api.safe.*` サーフェスを使用しなければなりません。サンドボックスなしのエスケープバルブはありません: `mode: unsafe` の宣言はロード時に拒否されます。

### 非インタラクティブな承認（eval、CI）

`reyn eval` はプロンプトを表示しません。ランの前に Permission が整っている必要があります。`reyn.yaml` で事前承認されているか（`permissions.<key>: allow`）、以前のインタラクティブなランから永続化されているかのどちらかです。モードが変わっても信頼モデルは変わりません。eval はあなたがすでに下した決定を継承します。

### コンテンツレイヤー防御

信頼できないコンテンツは、LLM プロンプトに入る OS のシームでスキャン・フェンスされます。2 つのプリミティブ:

- **パターンスキャン**（`security/threat_patterns.py`）— インジェクション / 流出 / role-hijack / exec-scope 脅威の正規表現ベース検出。マッチは脅威イベントを emit し、block パターンは操作を中止します。
- **構造フェンス**（`security/content_fence.py`）— 明示的なデリミタで信頼できないコンテンツを囲み、モデルが指示ではなくデータとして扱うようにします。

これらのプリミティブは以下の OS シームに適用されます。各シームはその信頼方向に合った機構を使います（read シームはスキャン・フェンス、write シームはブロック）:
- **ツール結果** — `security/content_guard.py` 経由で、プロンプト到達前にスキャン（全結果）＋構造フェンス（external-content 結果のみ）
- **メモリ書き込み** — 脅威パターンに一致する書き込みは router レベルでブロック
- **コンテキストファイル**（REYN.md/AGENTS.md）— ロード時にフェンス
- **A2A inbound メッセージ** — 到着時にフェンス＋スキャン
- **実行前コマンド** — `sandboxed_exec` がサブプロセス起動前に joined argv 全体を exec-scope 脅威についてスキャン
- **コンパクション入力** — シークレットらしきコンテンツは要約が永続化される前に除去（`security/secret_redaction.py`）

#### 構造フェンスの対象範囲

スキャンは広範（検出テレメトリのため read シームの全コンテンツで実行）ですが、**構造フェンス**は選択的に適用されます — *信頼できないソース*由来のコンテンツのみを、フェンスが有効なときに限り囲みます。2 つのゲートで決まります:

1. **設定ゲート** — `safety.threat_scan.enabled` *かつ* `safety.threat_scan.fence_enabled` が両方 on である必要があります（両方デフォルト `true`）。どちらかが off → コンテンツはフェンスされず通過します。
2. **ソース信頼ゲート** — シームごとに適用。信頼できる内部コンテンツ（OS 自身の framing、オペレーターが入力したもの）は決してフェンスされず、信頼できないソースのコンテンツのみがフェンスされます。

両ゲートが開いているとき、現在フェンスされるコンテンツ対象は以下:

| フェンス対象 | 内容 | ソース信頼ルール |
|---|---|---|
| **external-content ツール結果** | 外部コンテンツを返すツールの結果 — web fetch / web search、MCP 呼び出しとサーバー由来のツール記述、**recall / RAG 結果**、**メモリエントリの読み取り** | external content を返すとフラグされたツールのみフェンス。その他（信頼できる内部）のツール結果は**スキャンのみ**でフェンスされない |
| **プロジェクトコンテキストファイル** | システムプロンプトに差し込まれる `REYN.md` / `AGENTS.md` / `project_context_path` のテキスト | 常にフェンス — オペレーター編集可能なファイルはデータとして扱う |
| **A2A inbound peer メッセージ** | リモート peer エージェントからのメッセージテキスト（history に入る前） | 常にフェンス — リモート peer は信頼境界の外 |
| **外部 intervention 回答** | 外部 peer（A2A POST / webhook）から届いた回答 | history 連動（context）コピーのみフェンス。バッファ / choice-match された回答と監査レコードは raw のまま |
| **タスククエリ結果** | タスク read / list op が返すタスクの自由記述フィールド `description` / `name` / `result` | 常にフェンス。構造フィールド（id / status / 依存 / 日付）は OS 生成でフェンスしない |
| **委譲タスクの wake 記述** | assignee に実行を指示する wake メッセージに載る委譲タスクの description | 常にデータとしてフェンス。OS の「あなたが assignee — これを実行せよ」という framing が信頼される指示 |

したがってメモリは**両方向**でカバーされます: メモリの**読み取り**（recall またはメモリツール結果）は external content として入口でフェンスされ、メモリの**書き込み**は write シームでパターン**ブロック**されます — 別機構です（上記シームリスト参照）。意図的にフェンスされ*ない*コンテンツ: 信頼できる内部ツール結果、オペレーターの直接入力（`ask_user`、チャットメッセージ — 定義上信頼される）、実行前コマンドの argv（スキャンはするがフェンスしない）、コンパクション入力（シークレット除去はするがフェンスしない）。

## まだ薄い部分

**コンテンツレイヤー防御はシームベースの正規表現検出であり、プロンプトインジェクションの保証ではありません。** パターンスキャンは OS シームで既知の攻撃形状を捉えますが、正規表現に一致しない新規・難読化されたペイロードは通過します。信頼できないコンテンツがフェンスされてプロンプトに入った後でも、LLM は認識可能な攻撃パターンではなく自然言語として読める埋め込み指示に従う可能性があります。OS は LLM の*応答*をインジェクション残渣についてゲートしません — ケイパビリティ被害は Permission システムで制限されます（承認パス外への書き込み不可、宣言された `SandboxPolicy` 外での `sandboxed_exec` 不可）が、応答レベルの傍受は実装されていません。オペレーターの直接入力（`ask_user`、チャットメッセージ）は定義上信頼され、スキャンされません。ワークフロー設計は依然として重要です: 信頼できないコンテンツは verbatim で渡すのではなく要約し、構造化出力を検証し、重要な決定には `judge_output` でゲートをかけてください。

**AST サンドボックスは honor-system であり、カーネルサンドボックスではありません。** safe モードの validator + 制限された builtins は正直な作者のミス（うっかりした `import os`、紛れ込んだ `open`）を止めますが、`getattr` チェーンやその他のメタプログラミングを使う動機ある作者はなおエスケープできます。真の安全境界は subprocess 分離と `run_op` / `reyn.api.safe.*` サーフェスの permission gate であり、validator ではありません。これは開発者ツールとしての正しい境界です。ただし python ステップは Makefile のターゲットと同様にコードレビューに値します。

## 関連情報

- [../runtime/permission-model.md](../runtime/permission-model.md) — コンセプト
- [リファレンス: permissions](../../reference/config/permissions.md) — 完全なスキーマ
- [リファレンス: reyn.yaml](../../reference/config/reyn-yaml.md) — `permissions:` キー
- [ハウツー: Permission の管理](../../guide/for-users/manage-permissions.md)
- [reliability-engineering.md](reliability-engineering.md) — op が拒否されたときに何が起こるか
