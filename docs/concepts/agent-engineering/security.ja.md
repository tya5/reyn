---
type: concept
topic: architecture
audience: [human, agent]
---

# Security

ケイパビリティのゲーティング、サンドボックス境界、トラスト スコーピング。目標は「ユーザーが承認していないケイパビリティを Skill が暗黙的に取得せず、侵害された Skill が他の Skill にエスカレートできないこと」です。

## Reyn の実装方法

### 三層の Permission モデル

```
defaults (常時有効)
   ↓ Skill がより多くを必要とする場合
Phase の宣言 → ユーザーが起動時に承認
   ↓ プロジェクトを広く信頼する場合
プロジェクト全体の事前承認 (reyn.yaml)
```

デフォルトは保守的です。プロジェクトルート配下であればどこでも読み取り、書き込みは `.reyn/` と `reyn/` 配下のみ、シェルなし、MCP なし、Python なし。それ以上は上位レイヤーのいずれかでオプトインが必要です。

### Phase レベルの宣言 + インタラクティブな承認

Phase は frontmatter で必要なケイパビリティを宣言します。起動時にランタイムが単一の承認プロンプトを表示します。永続的な選択は `<skill>/<op>/<path>` をキーとして `.reyn/approvals.yaml` に記録されます。

### Skill スコープの承認

承認はユーザーではなく Skill をキーとします。Skill A に `file.write:/tmp/output` が付与された場合、サブ Skill B（`run_skill` 経由で呼び出される）はその付与を推移的に継承しません。B は自身で要求する必要があります。これが組み合わせの安全性です: 1 つの Skill を信頼することは、それが呼び出す可能性のあるすべてを信頼することではありません。

### Python preprocessor ステップの AST サンドボックス

`python` preprocessor ステップは 2 つのモードで実行されます:

- **`safe`** — allowlist に対して AST 検証されます（`open`、`eval`、`exec`、`__import__`、`compile`、`subprocess` などは禁止）。インポートはキュレートされた allowlist に制限されます（`math`、`statistics`、`json`、`re`、`random`、`time`、`datetime` など、`reyn.yaml` で拡張可能）。制限された `__builtins__`。クラッシュ分離のためにウォールクロックタイムアウト付きのサブプロセスで実行されます。
- **`unsafe`** — AST チェックなし、完全な Python。`--allow-unsafe-python` CLI フラグ、および `skill.md` の `permissions.python` エントリーに `mode: unsafe` が必要です。`safe` では本当に必要なものがブロックされる場合にのみ使用します。

Skill 作者は `safe` に誘導されます。`unsafe` に手を伸ばすことはリンターがフラグを立てることができる意図的な選択です。

### 非インタラクティブな承認（eval、CI）

`reyn eval` はプロンプトを表示しません。ランの前に Permission が整っている必要があります。`reyn.yaml` で事前承認されているか（`permissions.<key>: allow`）、以前のインタラクティブなランから永続化されているかのどちらかです。モードが変わっても信頼モデルは変わりません。eval はあなたがすでに下した決定を継承します。

## まだ薄い部分

**プロンプトインジェクションに対する特定の防御がありません。** 信頼できないテキストが LLM に届く場合（フェッチされた Web ページ、ユーザー提供のファイル）、LLM は埋め込まれた指示に従う可能性があります。Reyn の Permission システムは侵害された LLM が到達できる*ケイパビリティ*を制限します（承認されたパス外への書き込み不可、`--allow-shell` なしではシェル不可など）が、LLM の入力をインジェクションに対して事前スクリーニングしません。そのレイヤーの防御（入力フィルタリング、デュアル LLM パターン、出力ゲーティング）は Skill の設計に属し、OS には属しません。

**`mode: unsafe` は OS レベルのトラストであり、OS レベルのサンドボックスではありません。** unsafe な Python ステップは同じユーザーとして同じファイルシステムアクセス権で実行されます。カーネルレベルのサンドボックスではありません。システムはユーザーが特定の（モジュール、関数）ペアを承認したことを信頼します。これは開発者ツールとしての正しい境界です。ただし unsafe なステップは Makefile のターゲットと同様にコードレビューに値します。

## 関連情報

- [permission-model.md](../permission-model.md) — コンセプト
- [リファレンス: permissions](../../reference/config/permissions.md) — 完全なスキーマ
- [リファレンス: reyn.yaml](../../reference/config/reyn-yaml.md) — `permissions:` キー
- [ハウツー: Permission の管理](../../guide/for-users/manage-permissions.md)
- [reliability-engineering.md](reliability-engineering.md) — op が拒否されたときに何が起こるか
