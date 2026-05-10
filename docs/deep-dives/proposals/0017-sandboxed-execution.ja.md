# FP-0017: サンドボックス実行 — ポリシー/バックエンド抽象化と exec Op の非推奨化

**Status**: proposed
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

Reyn は現在、`exec` op を介したシェルコマンド実行を直接 `subprocess.run()` 呼び出しで行っており、
フルユーザー権限のまま — ファイルシステム隔離なし、ネットワーク制限なし、リソース上限なし — で動作している。
Permission モデルは *スキルが何をすると宣言しているか* を制約するが、
悪意あるプロンプトインジェクションやバグのあるスキルが宣言スコープ外の破壊的操作を試みた場合の
ランタイム強制は一切行われない。
本提案では、ポリシー宣言（スキルに何が許可されるか）とメカニズム選択（OS がどのように強制するか）を分離する
`SandboxPolicy` / `SandboxBackend` 抽象化と、新しい `sandboxed_exec` op、
および無防備な `exec` op の即時非推奨化を導入する。

---

## Motivation

### 現状 — ランタイム隔離のない `exec` op

`src/reyn/op_runtime/exec.py` の `exec` op はフルユーザー権限で `subprocess.run()` を呼び出す。
スキルは Permission モデル（ADR-0029）で意図を宣言するが、OS はシステム境界でのランタイム強制を行わない。
処理対象コンテンツ（ドキュメント、Webページ、コードレビュー差分など）に埋め込まれた
プロンプトインジェクション攻撃がスキルに任意のシェルコマンドを実行させる可能性がある。
Permission モデルは事後的に P6 イベントへ違反を記録するが、防止はできない。

stdlib スキル全件のコード監査により、**現在 `exec` op を使用しているスキルはゼロ**であることが確認されている。
つまり、サンドボックス対応の代替が存在すれば、移行コストゼロで即座に `exec` op を非推奨にできる。

### サンドボックスがランタイム OS の関心事である理由（スキルの関心事ではない）

Reyn の原則 P3 は OS をランタイム強制レイヤーとして位置づけている — スキルは必要なものを記述し、
OS がどのように強制するかを決定する。サンドボックスはこの原則をシステムコール境界まで自然に拡張したものだ。
スキルはすでに Permission モデルでファイルシステムパスとネットワークアクセスを宣言している；
サンドボックスポリシーは、それらの宣言をカーネルレベルで拘束力のあるものにする強制レイヤーに過ぎない。

P7 は OS コードにスキル固有の文字列を含めることを禁じている。
`skill.md` のデータ（YAML）として表現されたサンドボックスポリシーは、
メカニズムを OS コード内に保ちながらポリシー宣言をスキル空間に置く — クリーンな境界だ。

### バックエンドの現状と抽象化の必要性

サンドボックス技術は急速に進化している：

- **Landlock**（Linux）: カーネル内のファイルシステムおよびネットワーク制限。ABI はカーネル 6.10 時点で
  バージョン 9 に達している。Linux 5.13（ABI v1）以降、本番利用が安定している。
- **macOS sandbox-exec / SBPL**: 成熟しているが上流で非推奨。Apple は macOS 26 で
  Apple Containers に置き換える予定。
- **Apple Containers**（macOS 26+）: sandbox-exec の後継。API はまだ確定していない。
- **seccomp-BPF**（Linux）: syscall サーフェスの削減。Landlock と直交する。スタック可能。
- **WASM ランタイム**: バイトコードによる隔離、別の実行モデル。本 FP のスコープ外。

`sandboxed_exec` op を今日いずれか単一のバックエンドに固定すると、
sandbox-exec を削除する最初の macOS メジャーリリース（macOS 26 が予定される）で破壊が生じる。
抽象化レイヤー（`SandboxBackend` Protocol）が op コードをバックエンドの変動から保護する。

### 設計上のインスピレーション

- **OpenBSD `pledge`/`unveil`**: プログラム自身による最小権限宣言、メカニズム非依存のカーネル強制。
  `skill.md` で `sandbox:` ポリシーを宣言するスキルは、OS/スキル境界での同じパターンだ。
- **systemd `PrivateTmp`/`ReadOnlyPaths`**: カーネルメカニズムにマップされる宣言的ポリシー。
  `SandboxPolicy` の YAML は systemd のサービスユニット宣言を模している。
- **最小権限の原則**: 信頼できないドキュメントを処理するスキルは
  「`{{workspace}}/input/` の読み取りと `{{workspace}}/output/` への書き込みだけ必要」と宣言し、
  LLM のアクションなしに OS がその境界を強制できるべきだ。

### Docker を採用しない理由

Docker はルートで動作する常駐デーモンプロセス（`dockerd`）を必要とする。
Landlock と sandbox-exec はインプロセスで、オーバーヘッドゼロ、デーモン不要、
プロセス起動遅延もない。Docker スタイルのフルコンテナ隔離は将来の選択肢として有効だが
（特に `AppleContainerBackend` 関連）、本 FP のスコープ外とする。
`SandboxBackend` Protocol は将来の `DockerBackend` も必要に応じて収容できる設計にする。

---

## Proposed implementation

### 抽象化レイヤー

設計は 2 つのレイヤーからなる：

```
SandboxPolicy（何が許可されるか）   ← skill.md で宣言
    ↓
SandboxBackend（どのように強制するか）  ← OS がプラットフォーム/カーネルに基づいて選択
```

これは Reyn の既存の Permission モデル構造（P3/P7）を反映している：
スキルは意図を宣言し、OS が強制する。
サンドボックスポリシーは既存の権限宣言をランタイム強制に拡張したものだ。

### Component A — `SandboxPolicy` スキーマ + `SandboxBackend` Protocol + `sandboxed_exec` op（SMALL）

**ポリシースキーマ**（`skill.md` で宣言）：

```yaml
sandbox:
  fs:
    - path: "{{workspace}}"   # テンプレート変数、ランタイムで解決
      ops: [read, write]
    - path: "/usr/bin"
      ops: [execute]
  net:
    deny: all   # または allow: [{host: "api.example.com", port: 443}]
  resources:
    max_cpu_sec: 30
    max_memory_mb: 512
```

**バックエンド Protocol**（`src/reyn/sandbox/backend.py`）：

```python
class SandboxCapability(Enum):
    FS_RESTRICT = "fs_restrict"     # ファイルシステム制限
    NET_RESTRICT = "net_restrict"   # ネットワーク制限
    RESOURCE_LIMITS = "resource_limits"  # リソース上限

class SandboxBackend(Protocol):
    def supports(self) -> set[SandboxCapability]: ...
    def apply(self, policy: SandboxPolicy) -> None: ...
```

**`sandboxed_exec` op**: `SandboxPolicy` を必須とする新しい Control IR op。
OS は適切なバックエンドを選択し、`backend.apply(policy)` を呼び出したあと、
制限された環境内でサブプロセスを起動する。
P6 イベント：`sandbox_applied`（ポリシー適用成功時）、`sandbox_violation`
（サブプロセスが宣言ポリシー外のアクションを試みた場合）。

既存の `exec` op は使用時に `sandboxed_exec` への移行を促す非推奨警告を出力するようになる。

対象ファイル：
- `src/reyn/sandbox/policy.py` — `SandboxPolicy` データクラス + `SandboxCapability` 列挙型
- `src/reyn/sandbox/backend.py` — `SandboxBackend` Protocol + 自動選択ロジック
- `src/reyn/op_runtime/sandboxed_exec.py` — `sandboxed_exec` op ハンドラー
- `src/reyn/op_runtime/exec.py` — 非推奨警告の追加
- `src/reyn/events/events.py` — `sandbox_applied`、`sandbox_violation` イベントペイロード
- `docs/reference/runtime/control-ir.md` — `sandboxed_exec` op セクション（**NEVER ルール：
  `OP_KIND_MODEL_MAP` への `sandboxed_exec` op 登録と同じ PR で更新必須**）

### Component B — `LandlockBackend`（MEDIUM）— コントリビュータ向け

> **注記**: 主要メンテナーの開発環境は macOS のみ。Component B は Linux 環境（Docker または
> GitHub Actions `ubuntu-latest` などの Linux CI）なしには検証できない。
> このコンポーネントは明示的に **コントリビュータ向け** としてマークされており、
> Linux 環境を持つコントリビュータが Component A で定義した `SandboxBackend` Protocol に従って
> 独立に実装・検証することを歓迎する。

Linux 5.13+ バックエンド。`landlock` PyPI パッケージ（ABI バージョン 1〜4 対応）を使用。

```python
class LandlockBackend(SandboxBackend):
    # landlock_add_rule(LANDLOCK_RULE_PATH_BENEATH) によるファイルシステムパスルール
    # landlock_add_rule(LANDLOCK_RULE_NET_PORT) による TCP ポートルール — ABI v4 以降
    # seccomp-BPF を上位にスタック（直交する保護範囲）
    ...
```

自動選択：Linux カーネル ≥ 5.13 → `LandlockBackend`。
ランタイムで利用可能な ABI バージョンを検出し、実行中のカーネルがサポートする機能のみを有効化する
（5.13+ 範囲内の古い ABI バージョンでもグレースフルデグレード）。

seccomp-BPF は Landlock の上にスタックされる：Landlock がパス/ポート制限を担当し、
seccomp-BPF が syscall サーフェスを制限する。これらは直交している —
Landlock は `ptrace` をブロックできないが、seccomp-BPF はできる。

対象ファイル：
- `src/reyn/sandbox/backends/landlock.py` — `LandlockBackend`
- `src/reyn/sandbox/backends/seccomp.py` — seccomp-BPF フィルタービルダー（Landlock バックエンドが使用）

### Component C — `SeatbeltBackend` + `NoopBackend`（SMALL）

macOS バックエンド。`SandboxPolicy` から生成した SBPL（Sandbox Policy Language）プロファイルで
`sandbox-exec` をラップする。ファイルシステムの許可/拒否ルールとネットワークアクセスルールを適用。

```python
class SeatbeltBackend(SandboxBackend):
    # SandboxPolicy から .sb プロファイルを生成
    # sandbox-exec -f <profile> <cmd> でサブプロセスを起動
    # 上流で非推奨（Apple が macOS 26 で削除予定）
    ...
```

自動選択：macOS < 26 → `SeatbeltBackend`。
内部的に非推奨マーク済み；`AppleContainerBackend` が macOS 26+ で置き換えることを
示すランタイム警告をログ出力する。

対象ファイル：
- `src/reyn/sandbox/backends/seatbelt.py` — `SeatbeltBackend`
- `src/reyn/sandbox/backends/noop.py` — `NoopBackend`（警告付きフォールバック；非対応プラットフォームで使用）

### Component D — `exec` op の非推奨化（TINY）

`src/reyn/op_runtime/exec.py` のすべての呼び出し時に `DeprecationWarning` を追加：

```
DeprecationWarning: `exec` op は非推奨であり、次のメジャーバージョンで削除されます。
明示的な SandboxPolicy を指定した `sandboxed_exec` を使用してください。
stdlib スキルは `exec` を使用していないため、stdlib の移行コストはゼロです。
カスタムスキルは `sandboxed_exec` へ移行してください。
```

次のメジャーバージョンでの削除をスケジュール。stdlib の移行は不要 —
`exec` を使用している stdlib スキルはゼロ。

対象ファイル：
- `src/reyn/op_runtime/exec.py` — 非推奨警告

### Component E — `AppleContainerBackend`（LARGE、延期）

Apple Containers を隔離プリミティブとして使用する macOS 26+ バックエンド。
macOS 26 のリリースとコンテナ API の確定まで延期。
`SandboxBackend` Protocol は OS コード変更なしにこのバックエンドを収容できる設計にする。

自動選択（将来）：macOS ≥ 26 → `AppleContainerBackend`（`SeatbeltBackend` を置き換え）。

### 自動選択ロジック

`reyn.yaml` デフォルト：`backend: auto`。

| プラットフォーム | 条件 | 選択バックエンド |
|---|---|---|
| Linux | カーネル ≥ 5.13 | `LandlockBackend`（+ seccomp スタック） |
| Linux | カーネル < 5.13 | `SeccompOnlyBackend` |
| macOS | < 26 | `SeatbeltBackend`（上流で非推奨） |
| macOS | ≥ 26（将来） | `AppleContainerBackend` |
| その他 | 任意 | `NoopBackend` + 警告 |

**設定**（`reyn.yaml`）：

```yaml
sandbox:
  backend: auto          # auto | landlock | seatbelt | none
  on_unsupported: warn   # warn | error | ignore
```

`on_unsupported: error` は要求されたバックエンドが利用不可の場合にスキルディスパッチを失敗させる。
強制の保証が必要な本番環境で有用。

---

## Priority ordering

**A → D → C → B → E**

Component A（Protocol + 新 op）はその他すべての基盤となる。
Component D（非推奨警告）はコストゼロで A と同時に投入できる。
Component C（Seatbelt）は次に投入する — macOS が主要開発環境であるため。
Component B（Landlock）は Linux デプロイターゲットをカバーする。
Component E は macOS 26 の提供まで延期。

---

## Reyn 原則との整合

| 原則 | 本 FP の整合 |
|---|---|
| P3 | OS がバックエンドを選択；スキルはポリシーのみを宣言する。LLM は強制メカニズムの選択に一切関与しない。 |
| P5 | ワークスペースパスが FS ルールの自然な許可リストルートとなる；`{{workspace}}` はランタイムで OS 管理のワークスペースに解決される。 |
| P6 | `sandbox_applied` と `sandbox_violation` イベントが強制アクションの完全な監査証跡を保持する。 |
| P7 | バックエンドコードにスキル固有の文字列は含まれない；`SandboxPolicy` はデータとして渡される。自動選択ロジックはプラットフォーム/カーネルの事実を参照し、スキル名は参照しない。 |
| P8 | Phase インストラクションはスキルが何を必要とするかを記述する；サンドボックス強制メカニズムは Phase インストラクションに記述されない。 |

---

## Dependencies

- **Component A、B、C、D には依存なし** — op ランタイムへのスタンドアロン追加
- **Component E**: macOS 26 のリリースと安定した Apple Containers API
- **CLAUDE.md NEVER ルール**: `docs/reference/runtime/control-ir.md` は
  `src/reyn/op_runtime/registry.py` の `OP_KIND_MODEL_MAP` への `sandboxed_exec` op 登録と
  同じ PR で更新しなければならない

---

## Cost estimate

**アクティブ作業合計: MEDIUM**

| Component | コスト | 備考 |
|---|---|---|
| A: ポリシースキーマ + バックエンド Protocol + `sandboxed_exec` op | SMALL | 新モジュール + op ハンドラー + P6 イベント 2 件 |
| B: `LandlockBackend` | MEDIUM | `landlock` PyPI + seccomp-BPF スタック；ABI バージョン検出 |
| C: `SeatbeltBackend` + `NoopBackend` | SMALL | SBPL プロファイルジェネレーター；シンプルなラッパー |
| D: `exec` op の非推奨化 | TINY | 1 行の警告追加 |
| E: `AppleContainerBackend` | LARGE | 延期 — macOS 26 が必要 |
| テスト | SMALL | Tier 1: `sandboxed_exec` op コントラクト；Tier 2: バックエンド自動選択不変条件 |

Component E は明示的に延期されているため、アクティブコスト見積もりから除外する。

---

## Related

- `src/reyn/op_runtime/exec.py` — 現在の `exec` op（Component D: 非推奨化）
- `src/reyn/op_runtime/registry.py` — `OP_KIND_MODEL_MAP`（Component A: `sandboxed_exec` 登録）
- `src/reyn/sandbox/policy.py` — 新規ファイル（Component A）
- `src/reyn/sandbox/backend.py` — 新規ファイル（Component A）
- `src/reyn/sandbox/backends/landlock.py` — 新規ファイル（Component B）
- `src/reyn/sandbox/backends/seatbelt.py` — 新規ファイル（Component C）
- `src/reyn/sandbox/backends/noop.py` — 新規ファイル（Component C）
- `src/reyn/op_runtime/sandboxed_exec.py` — 新規ファイル（Component A）
- `src/reyn/config.py` — `SandboxConfig`（backend + on_unsupported）
- `src/reyn/events/events.py` — `sandbox_applied`、`sandbox_violation`
- `docs/reference/runtime/control-ir.md` — `sandboxed_exec` op リファレンス
- ADR-0029 — Permission モデル（本 FP が強制に拡張する既存の宣言レイヤー）
- FP-0012（`0012-async-skill-execution.md`）— 非同期実行；信頼できない入力を処理する
  長時間タスクにとってサンドボックスは特に重要
