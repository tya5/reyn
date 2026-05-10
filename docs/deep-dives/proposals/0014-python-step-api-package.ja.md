# FP-0014: Python step 用 API package + mode 改名 (pure→safe, trusted→unsafe)

**Status**: **proposed**
**Proposed**: 2026-05-11
**Author**: 2026-05-11 設計議論 (R-PURE-MODE-REDEFINE Step 1 後)
**Trigger**: Step 1 (commit `18f4aaa`) で pure mode を 「ambient sources only」
として formalize したが、 3 つの構造的問題が未解消: (a) stdlib 内に
`mode: trusted` 宣言が 7 skill 残存、 仕様と乖離; (b) `trusted` という名前は
paradoxical (= Reyn が author を MORE trust する = LESS verify、 直感と反対);
(c) 自然な fix (= I/O 能力を増やす) が `run_op` kind を line-by-line に増殖
させる。 本提案は R-PURE-MODE-REDEFINE Step 2 を redesign 経由 supersede。

---

## Summary

3 つの coupled 変更を **1 commit clean break で land** (= 2 段階 migration なし、
現 user は我々だけ):

1. **改名**: `pure` → **`safe`**、 `trusted` → **`unsafe`**。 Rust 流儀。
   `safe` = Reyn が verified guarantee を提供; `unsafe` = author が責任を
   assert。 `pure` は FP jargon で 「副作用なし」 と混同される (= `random` /
   `time` が allowed なので構造的に矛盾)、 `trusted` は paradoxical (= Reyn
   が trust 多 = verify 少)。
2. **API package**: `reyn.safe.*` (= `safe` mode から import 可) + `reyn.unsafe.*`
   (= `unsafe` mode から import 可) を Reyn 提供 Python package として出荷。
   `reyn.unsafe.*` helper は既存 run_op dispatch の薄い wrapper (= permission
   gate + event emit + LLMReplay capture を tax-free で継承)。 author は
   YAML declarative step ではなく type hint / autocomplete / docstring 付きの
   Python を書く。
3. **`run_op` kind 統合**: end-user surface が API package に移ることで、 run_op
   は **内部 primitive layer** に降格。 形状重複する kind を統合 (= `file_read` /
   `file_write` / `file_delete` / `file_glob` → `file` 1 op に verb+path+scope
   params)、 Reyn 内部しか call しないので migration が局所化。

**Stdlib outcome**: land 後、 stdlib に **`mode: unsafe` 宣言ゼロ**。 全 stdlib
python step が `safe` mode で動き、 I/O は preprocessor chain 内の別 `run_op`
step に分離。 `reyn.unsafe.*` は **user unsafe-mode step 用**であって stdlib
では使わない (= stdlib は構造的に pure-safe を維持)。

---

## Motivation

### Step 1 が残した 3 つの構造的負債

Step 1 (commit `18f4aaa`) で pure mode の author-facing 定義 (「ambient sources
only」) は clean に書けたが、 仕様と実態が不一致:

1. **stdlib が `mode: trusted` を使い続けている** (= 7 skill: mcp_search /
   mcp_install / eval_builder / index_docs ×3 / skill_improver)。 plan file
   Step 2 audit で Class A (1 件、 真の I/O) / Class B (6 件、 I/O 分離可で
   python は pure 化) / Class C (4 件、 mis-labeled 純関数) に分類済。 これが
   解消するまで 「stdlib auto-trust」 と 「trusted は default の escape hatch」
   が operational に区別不能、 仕様 doc が現実を embellish。

2. **`trusted` という名前が paradoxical**。 Reyn モデルでは:
   - `pure` = Reyn が静的に safety を検証 (= operator にとってより安全)
   - `trusted` = Reyn が author を trust する (= 検証少なめ、 責任は author)

   `trusted` を素直に読むと 「より trusted = より安全」 だが意味論は逆。
   author / operator (= skill レビューする側) の注意が誤誘導される。

3. **I/O 能力追加が `run_op` 増殖を誘発**。 現 run_op family は file_read /
   file_write / file_delete / file_glob / web_fetch / shell / iterate /
   validate / lint_plan / python の 10 kind。 追加するたびに schema model /
   linter / events schema / Control IR JSON shape / docs に touch。 線形に
   DSL surface が膨らむ = scaling が悪い。

### なぜ API package が run_op 増殖より良いか

現状の設計は 「deterministic compute」 (= python step) と 「effectful I/O」
(= run_op step) を **YAML DSL layer** で分けている。 author は step type を
stitch する。 能力追加 = step type 追加。

代替: **Python API layer** で分ける。 step type は `python` 1 種、 何ができるか
は import する Reyn 提供 package が決定。 mode 宣言 (`safe`/`unsafe`) が
AST validator の permit する import を制御。

| 軸 | run_op 拡張 | Python API package |
|---|---|---|
| Author UX | I/O 種別ごと YAML declarative step | Python import + 関数 call (= type hint / docstring / autocomplete) |
| Permission gating | run_op dispatch 層 | **AST validator が parse 時点で禁止 import を reject** + 関数 call 時に同じ permission check (= PermissionResolver reuse) |
| Audit / events | run_op が emit | API 関数が内部で run_op dispatch を call → events 自動 emit |
| LLMReplay | run_op 経由記録 | 同 dispatch → 同 recording |
| Reyn evolve | YAML schema bump + linter 更新 | **Python package version up** だけ |
| Doc | run_op kind ごと reference | `help(reyn.unsafe.file)` + sphinx 出力可 |
| Spec enforcement | 文字列 allowlist match | AST + import resolution = 決定論的 |

**仕掛け**: `reyn.unsafe.file.read(path)` は既存 `file_read` run_op dispatch の
**薄い wrapper**。 permission check / event emission / replay capture / error
envelope はすべて既存インフラ reuse。 二重実装ゼロ。 run_op kind は内部
primitive に降格。

**P3 境界**: 本提案は P3 (= 「OS は実行せず、 Skill が実行する」) に違反しない。
python step body は引き続き author 記述、 Reyn の package は author が voluntary
に import する vetted helper layer。 LLM は python step を書かない、 author
(= user / stdlib) が書く。

### なぜ両方同時に rename

`trusted` → `unsafe` だけ rename して `pure` を残すと pair が非対称で newcomer
を引き続き混乱させる — `pure` は FP jargon、 `safe` は plain English。 対称な
`safe`/`unsafe` は Rust 流儀で全 working dev に 1 秒で transfer する pair。
mental model:

- **`safe`** = Reyn が検証 guarantee を提供 (AST allowlist / banned builtins /
  subprocess sandbox / ambient sources only)。
- **`unsafe`** = author が step の挙動に責任を assert。 Reyn は guarantee を
  解除し、 code を written 通り実行。

Rust の `unsafe { ... }` と同意味論: 「これは危険」 ではなく 「私 (= author) は
compiler が check できない invariants の責任を取る」。

### Clean break

現状外部 user 0、 既存 `pure` / `trusted` keyword は本 repo 外に production
install base なし。 標準 2-step deprecation (= warn → reject) は overhead のみ。
**1 commit で rename + stdlib refactor + API package ship + linter で旧 keyword
hard reject + docs 更新**。 in-flight branch は rebase 時 5 行修正で済む。

---

## Proposed implementation

### Component A — Mode rename (mechanical)

touch points (= schema / permission code 内で `trusted` + `pure` を grep):

- `src/reyn/schemas/models.py::PythonStep` — `mode: Literal["pure", "trusted"]` → `Literal["safe", "unsafe"]`
- `src/reyn/permissions/permissions.py::PythonPermission` — 同 field rename
- `src/reyn/permissions/permissions.py` — permission key `python.pure` / `python.trusted` → `python.safe` / `python.unsafe`
- CLI flag: `--allow-untrusted-python` → `--allow-unsafe-python`
- env var: `REYN_ALLOW_UNTRUSTED_PYTHON` → `REYN_ALLOW_UNSAFE_PYTHON`
- `_python_allowlist.py` comment + module docstring
- `docs/concepts/python-pure-mode.{md,ja.md}` → file 名 + 内容 rename
- `docs/concepts/python-unsafe-mode.{md,ja.md}` — pair doc 新規
- `docs/guide/for-skill-authors/add-a-python-preprocessor.{md,ja.md}` — sweep
- `docs/guide/for-skill-authors/glossary.md` — sweep
- `docs/guide/for-users/manage-permissions.{md,ja.md}` — sweep
- `docs/reference/dsl/preprocessor.{md,ja.md}` — sweep
- 全 stdlib skill yaml: `mode: trusted` を 削除 (= safe + run_op に refactor)
  もしくは `mode: unsafe` (= refactor 後は残らない見込み)
- test fixture sweep
- `reyn lint` で `mode: pure` / `mode: trusted` を clear migration message と
  共に hard reject

### Component B — `reyn.safe` package

`src/reyn/api/safe/` 配下に出荷。 `safe` mode python step から import 可。
stdlib を wrap + bare allowlist にない safe-mode-friendly helper を提供。

```python
# src/reyn/api/safe/__init__.py
"""Reyn 提供 helper、 `safe`-mode python step から call 可。

本 package の全関数は provably ambient: output は inputs + clock +
entropy + bundled static data からのみ決まる。 AST validator は
`safe` mode から `import reyn.safe.*` を allow する。
"""

from . import hash, schema, text, time, random, json
```

初期 surface (= 育てて良い):

- `reyn.safe.hash` — `sha256(b)` / `md5(b)` / `blake2b(b)` / file-content hash
- `reyn.safe.schema` — `validate(data, schema)` (= jsonschema) / `assert_type(...)`
- `reyn.safe.text` — `regex.findall_named(...)` / `template.render_safe(...)` (= Jinja escape なし)
- `reyn.safe.time` — `monotonic_seq()` (= 非決定性を明示する ambient clock helper)
- `reyn.safe.random` — `seeded(seed)` (= ambient entropy + 明示 seeding)
- `reyn.safe.json` — `loads_strict(s)` / `dumps_canonical(d)` (= sort_keys / ensure_ascii)

**I/O なし**。 file / http / shell / env / time-as-source 不可 (= 必要なら
`reyn.safe.time.monotonic_seq()` を使う、 read を log)。

### Component C — `reyn.unsafe` package

`src/reyn/api/unsafe/` 配下に出荷。 `unsafe`-mode python step から import 可
(= AST validator が allow)。 各関数は対応する run_op primitive に dispatch する
薄い wrapper。

```python
# src/reyn/api/unsafe/file.py
from reyn.api._internal import dispatch_op

def read(path: str, *, encoding: str = "utf-8") -> str:
    """File 読込。 Permission: `path` に対する file_read。

    内部 file_read run_op の wrapper。 declarative file_read と同じ
    permission gate / event emission / LLMReplay capture が適用される。
    """
    return dispatch_op("file", verb="read", path=path, encoding=encoding)

def write(path: str, content: str, *, encoding: str = "utf-8") -> None:
    """File 書込。 Permission: `path` に対する file_write。"""
    dispatch_op("file", verb="write", path=path, content=content, encoding=encoding)

def glob(pattern: str) -> list[str]:
    """glob match。 Permission: 各 match に対する file_read。"""
    return dispatch_op("file", verb="glob", pattern=pattern)
```

初期 surface:

- `reyn.unsafe.file` — read / write / delete / glob / exists / stat
- `reyn.unsafe.http` — get / post / put / delete (= JSON body 規約、 auto-encode)
- `reyn.unsafe.shell` — `run(argv, cwd=, env=)` → CompletedProcess 風
- `reyn.unsafe.workspace` — `path()` / `cwd()` / `list_artifacts()` (= ergonomic workspace access)
- `reyn.unsafe.env` — `get(key)` (= 明示 env read、 gated)

Permission gating は引き続き `PermissionResolver.require_*` を経由 — wrapper は
Python args を run_op IR shape に翻訳するだけ。

### Component D — `run_op` kind 統合

user surface が API package に移った結果、 run_op kind は内部 primitive に降格。
形状重複する kind を統合:

| Before (複数 kind) | After (parametrised 1 kind) |
|---|---|
| `file_read` / `file_write` / `file_delete` / `file_glob` | `file` (= `verb: "read"\|"write"\|"delete"\|"glob"`) |
| (将来: http_get / http_post / ...) | `http` (= `method:`) |
| `shell` | `shell` (= 変更なし) |

migration: 同 commit で IR shape migrate + 全 stdlib skill yaml + Control IR
producer (= LLM-driven phase output) 更新。 LLMReplay fixture 再記録
(= 許容、 現 cache scope は repo 全体)。

### Component E — Stdlib refactor (= 旧 Step 2)

`mode: trusted` 宣言する 7 stdlib skill を全 refactor:

- **Class A (1 件)**: `index_docs/apply_strategy` の file write + lock。 lock
  取得 + file write を chain 前段の `file` run_op step に分離、 python step は
  lock state を input に受けて決定論的 transform、 別 `file` run_op step が
  content を書き出す。 結果: python の I/O ゼロ。
- **Class B (6 件)**: registry fetch / analyzer / cost preflight /
  copy_to_work_resolver — I/O 部分を専用 run_op step に分離 (= HTTP fetch /
  file glob 等)、 python は dict 操作のみ。
- **Class C (4 関数)**: `skill_improver/copy_to_work.py` の純関数群 —
  `mode: trusted` 宣言を削除、 `safe` で動作。

**Acceptance criterion**: commit 後 `grep -r "mode: unsafe" src/reyn/stdlib` で
**0 件**。 linter が stdlib path prefix に対する hard rule として enforce。

### Component F — Lint 強化

`reyn lint` に 3 ルール追加:

- **`unsafe-in-stdlib`** — hard error。 stdlib skill が `mode: unsafe` 宣言。
  Message: "Stdlib skills must run in safe mode. Move I/O to a run_op step or
  `reyn.unsafe.*` package call from a user skill."
- **`unsafe-without-justification`** — warn。 user skill が `mode: unsafe`
  宣言 + 3 行以内に `# justification:` comment なし。 Message: "unsafe mode
  disables Reyn's safety guarantees. Add `# justification: <reason>` to
  document why unsafe is required."
- **`legacy-mode-keyword`** — hard error。 `mode: pure` / `mode: trusted` 検出。
  Message: "Renamed in FP-0014: pure → safe, trusted → unsafe. Update your
  skill.md."

---

## Open design questions (ADR delegate)

提案が原則的に accept された後に follow-up ADR で詰める:

1. **ADR-A: API package surface 安定性**。 `reyn.safe.*` / `reyn.unsafe.*`
   consumer を breaking change から守る versioning 戦略。 Reyn core から独立
   semver か Reyn version pin か。
2. **ADR-B: Dispatch internals**。 `reyn.unsafe.file.read()` は現 phase の
   run_op dispatcher に到達する必要がある。 現 dispatcher は phase 実行
   context に bind。 任意 Python 関数 call からどう正しい context を取るか。
   選択肢: (a) contextvars ベース ambient dispatcher、 (b) harness が threading
   する明示 `ctx` arg、 (c) python harness が inject する `__init__.py` レベル
   setup。
3. **ADR-C: `run_op` 統合 scope**。 `file_*` → `file` は straightforward。
   `iterate` / `validate` / `lint_plan` は形状違い、 別 kind 維持か統合か。
   `python` 自体も run_op kind、 entry point として維持か rename か。
4. **ADR-D: `reyn.safe.time` の semantics**。 stdlib `time.monotonic()` は今
   safe allowlist だが LLM replay で決定論的再現不可な ambient source。
   `reyn.safe.time` が log した read 付き wrap すべきか (= events 記録、 replay 可)。
5. **ADR-E: 将来の外部 user migration**。 現 user は我々だけだが post-1.0 で
   API package surface が public API の一部に。 1.0 出荷 **前に** `reyn.safe.*`
   / `reyn.unsafe.*` namespace か、 もっと保守的な (`reyn.sdk.*` / `reyn.runtime.*`
   等) で出すか決定。
6. **ADR-F: `--allow-unsafe-python` consent UX**。 flag は現状 `reyn run` 単位
   one-shot。 API package で各 `import reyn.unsafe.X` が permission-gated。
   flag は run 全体 / import / 各 call のどれを gate するか。 現設計案: import
   レベル gate (= flag が import を enable、 permission grant が skill 単位
   個別 call を cover)。

---

## Dependencies

- **R-PURE-MODE-REDEFINE Step 1 (LANDED 2026-05-11、 commit `18f4aaa`)** —
  本提案が build する 「ambient sources only」 定義を提供。
- **PR37 unified dispatch (LANDED)** — `dispatch_tool` が API package が
  reuse する permission gate + event emit インフラを提供。
- **ADR-0020 skill-only permissions (LANDED)** — `permissions:` field が
  Skill 単位 (= Phase でない)、 API package permission gating が reuse。

新規外部依存なし。

---

## Migration plan (1 commit)

段階 rollout なし — clean break 1 commit、 現 user (= 我々) は rebase 時に
5 行 yaml 修正で吸収。

1. schema field + permission key + CLI flag + env var を rename。
2. `reyn.api.safe` + `reyn.api.unsafe` package を初期 surface で出荷。
3. ambient dispatcher を wire (= ADR-B 解決)。
4. 7 stdlib skill を refactor (= `mode: trusted` 削除、 I/O を適切に移動)。
5. `file_*` run_op kind 統合 (= ADR-C 解決) → `file` 1 op。
6. `reyn lint` に 3 ルール追加。
7. Docs sweep: concept doc rename、 `python-unsafe-mode.md` pair 新規、
   API package reference 追加、 glossary / preprocessor / manage-permissions
   doc 更新。 EN + JA mirror。
8. Test sweep: fixture 再生、 assertion 更新、 API package wrapper の coverage 追加。
9. ADR drafting (= 6 open question を実装中に必要に応じて)。

---

## Cost estimate

**MEDIUM** (~4.5 day focused、 sonnet 並列で短縮可能)。

| 項目 | 見積もり |
|---|---|
| mechanical rename (schema + permissions + CLI flag + env vars + tests) | ~0.5 day |
| `reyn.safe` + `reyn.unsafe` package + wrapper test | ~1 day |
| Dispatch context wiring (ADR-B 解決) | ~0.5 day |
| stdlib 7-skill refactor | ~1 day |
| linter rules | ~0.5 day |
| run_op kind 統合 (`file_*` → `file`) + IR migration | ~0.5 day |
| Docs sweep EN+JA (concept doc + glossary + preprocessor + manage-permissions + reference) | ~0.5 day |
| Dogfood verify (= mcp_install / index_docs / skill_improver e2e) | ~0.5 day |
| ADR drafting (A–F、 必要なもの) | ~0.5 day |

Sonnet 並列化可能: rename + docs sweep + linter rules + stdlib refactor は
独立。 Dispatch context wiring が critical path (= 他 item の前提)。

---

## Risks

- **Dispatch context 解決 (ADR-B)** が non-trivial。 contextvars ベースで python
  harness が subprocess 実行 (= 現状そう) すると contextvars は process boundary
  を越えない。 明示 ctx threading か parent-child JSON channel 越し serialization
  protocol が必要。
- **Stdlib refactor で run_op primitive 不足が露呈**。 7 skill を詳細 audit すると
  Class A (index_docs file write + lock) が今ない run_op primitive (= file lock
  acquire/release semantics) を要求する可能性。 scope add ~0.5 day。
- **API package surface が contract を早期 freeze する**。 post-1.0 で
  `reyn.safe.*` / `reyn.unsafe.*` の変更は外部 skill author への breaking
  change。 ADR-A と ADR-E は 1.0 出荷 **前に** 決着、 出荷後ではない。
- **Linter で legacy keyword false-positive**。 string match scope を慎重に
  限定 (= skill yaml 内のみ、 python source 内ではない) して `# trusted by
  user` 等の comment を trip させない。

---

## Related

- **R-PURE-MODE-REDEFINE Step 1 (commit `18f4aaa`)** — 本提案が build する
  pure mode (= safe に rename) の author-facing 定義。
- **R-PURE-MODE-REDEFINE Step 2 (plan file 残件)** — **本 FP が supersede**。
  Step 2 の stdlib refactor scope は Component E に吸収。
- **ADR-0020 skill-only permissions (commit `7b93025` / `3dab751`)** — 本提案
  が reuse する permission 宣言単位。
- **PR37 unified dispatch (commit `d06cb94`)** — API package が call する
  dispatch + permission + event インフラ。
- **`docs/concepts/python-pure-mode.{md,ja.md}`** — commit 中に
  `python-safe-mode.{md,ja.md}` に rename、 内容に API package section 追加。
- **Rust `unsafe { ... }` 慣例** — rename の意味論 inspiration。 「author が
  compiler が check できない invariants の責任を取る」 mental model がそのまま
  transfer する。
