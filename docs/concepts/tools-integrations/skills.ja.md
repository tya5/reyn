---
type: concept
topic: integration
audience: [human, agent]
---

# Skills

skill とは、再利用可能でタスク特化型の instruction セット — 業界標準の `SKILL.md` ファイル(YAML frontmatter + Markdown 本文)で、ある手法が*いつ*適用されるか、*どう*実行するかをモデルに伝えます。skill は明示的に登録され、軽量なメニューとしてモデルに提示され、必要になった時点で読み込まれます — MCP ツールと同じ段階的開示の形を、API ではなく instruction に適用したものです。

これは 1.0 以前の `skill.md` 駆動の phase-graph ワークフローエンジン(削除済み — 現行の実行モデルはマルチエージェント / Control IR のドキュメントを参照)とは別の機構です。ここでの「skill」は Claude Skills に近い概念です: OS が実行するプログラムではなく、モデルが読むかどうかを選ぶ instruction フォルダです。

## 登録: 明示的なエントリ、ディレクトリスキャンなし

skill は config 内の `skills.entries` 宣言でのみ登録されます — `mcp.servers` と同じモデルです。ディレクトリスキャンはありません。config エントリの無い `SKILL.md` はどのセッションからも見えません。

```yaml
# reyn.yaml
skills:
  entries:
    pdf_editing:
      path: skills/pdf-editing/SKILL.md
      description: "PDF フォームのフィールドを入力・結合・抽出する"
      enabled: true
      auto_invoke: true
```

| フィールド | 型 | デフォルト | 意味 |
|-----------|-----|----------|------|
| `path` | string | 必須 | `SKILL.md`(またはそれを含むディレクトリ)へのパス。project-root 相対または絶対。 |
| `description` | string | `""` | L1 メニューに表示される一行サマリー。最初の行のみ、200 文字上限で切り詰め。 |
| `enabled` | bool | `true` | `false` にするとエントリはレジストリから完全に除外されます(単に非表示ではありません)。 |
| `auto_invoke` | bool | `true` | `false` にすると skill は登録されたままですが L1 システムプロンプトメニューから除外されます — 他の何かがそれを表面化しない限り、モデルはその存在を知らされません。 |

レジストリ自体は `SKILL.md` を読みません — config エントリの `path` と `description` だけが L1 メニューに反映されます。ファイル自体は L2 で、必要になった時点で通常の file-read op によってモデルが読みます。

## Config カスケード

`skills.entries` は他の config セクションと同じ tier をまたいでマージされ、名前が衝突した場合は後の tier が優先します:

1. `~/.reyn/config.yaml` — ユーザーグローバル
2. `reyn.yaml` — プロジェクト
3. `reyn.local.yaml` — プロジェクトローカル(gitignore 対象)
4. `.reyn/config/skills.yaml` — ランタイム動的、`skill_management__install_*` ツールが書き込む

最初の 3 つを手編集するのが skill を登録する通常の方法です。4 つ目は下記のインストールツールが自動的に書き込むもので、セッションが自分自身のためにインストールした内容を反映します。

## `SKILL.md` を書く

```markdown
---
name: pdf-editing
description: PDF フォームのフィールドを入力・結合・抽出する
---

# PDF editing

フォームフィールド操作には `pypdf` を使用...
```

`name` と `description` は frontmatter のキーで、下記のインストールツールが `skills.yaml` エントリを事前入力する際に読みます — 実際にモデルに届くのは config エントリ自身の `description` なので、正確かつ短く保ってください(最初の行のみ。詳細は本文に書く)。Markdown 本文は自由形式です: これは OS がパースするスキーマではなく、モデル向けの instruction テキストです。

## 3 層の露出モデル

| レイヤー | モデルが見るもの | 機構 |
|---------|----------------|------|
| **L1 — メニュー** | 専用の `## Skills` システムプロンプトブロック。enabled + auto-invoke な skill ごとに 1 行: `name — description [path]`。 | 専用ディスパッチ無しで、ターンごとに 1 回構築。 |
| **L2 — instruction** | `SKILL.md` の本文全体。モデルが現在のタスクがエントリの description に一致すると判断した時のみ読まれる。 | 通常の `file__read` — 専用の「skill 呼び出し」op は無し。 |
| **L3 — バンドル資産** | skill の instruction が参照するその他のファイル(テンプレート、スクリプト、参照データ)。`SKILL.md` と同じ場所にある。 | 通常の `file__read`、他のパスと同様に標準パーミッションモデルでゲート。 |

どのレイヤーにも「この skill を実行する」専用プリミティブはありません — skill は L1 で発見され、L2 で読み込まれ、その資産は単なるファイルです。関連性の判断はモデルが L1 の description から行います。OS がゲートするのは「どの skill を読めるか」ではなく「どのパスを読めるか」だけです(標準パーミッションモデル — プロジェクトルート内の読み取りはデフォルト、それ以外は通常の宣言 + 承認が必要)。

## ホットリロード

`.reyn/config/skills.yaml` への編集は次のターン境界で `"skills"` リロードシームを通じて反映されます — セッション再起動不要。`reyn.yaml` / `reyn.local.yaml` を直接編集する場合は、他のセクションと同じ一般的な config ホットリロード経路に従います。[コンセプト: Config ホットリロード](../runtime/config-hot-reload.md) を参照。

## セッションごとの可視性トグル

skill は config を触らずに、単一セッションから非表示にできます — tool / MCP サーバー / カテゴリと同じステータスバー式の可視性オーバーライドを使います: `set_capability_visible("skill", name, visible)`。これは**制限のみ**です — 登録されていない skill 名(またはトポロジー/委譲エンベロープが既に拒否しているもの)をトグルしても静かな no-op になります。可視性は登録済みの範囲を超えて権限を付与することはできません。

## skill のインストール

`skill_management` カテゴリ配下の 2 つの chat 呼び出し可能ツールが `skills.yaml` エントリを書き込みます — v1/v2 に `reyn skill` という CLI 相当は存在しません(skill 管理は chat 駆動の対話内フローです)。

### `skill_management__install_local`

ローカルの skill ディレクトリ(または `SKILL.md` への直接パス)を `.reyn/config/skills.yaml` に登録します:

1. `SKILL.md` を解決(ディレクトリ → `<dir>/SKILL.md`、または直接ファイルパス)。
2. frontmatter から `name` / `description` を読む(`name` override 引数が優先。frontmatter に無ければディレクトリのベースネームにフォールバック)。
3. description を脅威スキャン(strict scope) — blocking severity のマッチでブロック。
4. `skills.yaml` の書き込みを標準の `require_file_write` パーミッションフローでゲート。
5. エントリを書き込み、config generation を記録(クラッシュリカバリ — WAL truncation を生き延びる)、`skill_installed` P6 イベントを発行、ホットリロードを要求。

### `skill_management__install_source`

git/GitHub URL から skill を取得してクローンをインストールします:

1. ソースホストに対して `require_http_get` をゲート。
2. リポジトリを shallow-clone(`--depth 1`)して `.reyn/skills/<name>/` へ。URL 末尾の `//subdir` サフィックス(Terraform のモジュール subdir 規約を踏襲)でクローン内のサブディレクトリを選択可能。
3. クローン内で `SKILL.md` を特定し、ローカルパスと同じ frontmatter 読み取り → 脅威スキャン → ゲート → 書き込み → ホットリロードのパイプラインへ進む。登録される `path` はインストール済みコピーを指す。

**パス安全性の強化**(両ツールとも、解決された名前が `.reyn/skills/` 配下のファイルシステムパスに使われるため): `name` 引数、`SKILL.md` frontmatter、または URL/subdir のベースネームから導出された名前は、単一の安全なパス要素(`[A-Za-z0-9._-]+`、`..` 無し、先頭ドット無し、区切り文字無し)でない限り即座に拒否されます。さらに belt-and-suspenders な containment チェック(`resolve()` + `relative_to()`)が、インストール先が `.reyn/skills/` の外に解決されるケースを拒否します — name チェック自体にギャップがあった場合の保険です。どちらのチェックも安全でない名前を黙って書き換えることはなく、明示的なエラーでインストールを拒否します。

## 現時点でのスコープ外

意図的に現行モデルに含まれていないもの — 今のギャップではなく、将来のレイヤーとして計画されているもの:

- skill ごとのツールパーミッションスコープ(`allowed-tools` 的なアクティベーションスコープ)
- skill instruction 内での動的シェルコマンド実行構文
- skill を発見するための marketplace / レジストリインデックス(MCP の公式レジストリとは異なり)
- `list_skills` / `describe_skill` の introspection ツールや CLI

## 関連情報

- [リファレンス: `reyn.yaml`](../../reference/config/reyn-yaml.md) — `skills:` ブロックのスキーマ
- [コンセプト: MCP](mcp.md) — 類似の外部ケイパビリティ登録モデル
- [コンセプト: パーミッションモデル](../runtime/permission-model.md) — skill が使う file-read/file-write ゲート
- [コンセプト: Config ホットリロード](../runtime/config-hot-reload.md) — 一般的なリロードサイクル
