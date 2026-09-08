"""Tier 1: `ResidentBytes` and `BodyBytes` are mypy-incompatible (#5973
裁定4, precursor to that issue's ①②③) — passing one where the other is
expected is a real type-checker finding, not merely a naming convention
a reader has to remember to honor.

## Why this PR exists (architect's own framing, quoted)

#5973's root cause: "表現を『本体 inline』から『参照』へ変えると、それ
まで 1 つだった『bytes』が 2 つの量に割れる — ⑴ 行そのものの常駐量と
⑵ その行が引き寄せられる本体の量。既存の bound は全部 ⑴ を数え続け、
痛いのは ⑵ になる." Both stayed a bare `int` throughout that migration,
so nothing could have caught a caller that measured one and used it as
the other. `ResidentBytes` (`ChatMessage.resident_bytes()`, a row's own
serialized size) and `BodyBytes` (`CONTENT_BYTES_META_KEY`, the size of
the body a `content_ref` row points AT) are the same two quantities,
now given distinct `typing.NewType`s.

**This PR moves ZERO bounds** — `history_resident.max_bytes` is still
256 MiB, still counts the same thing it always counted (a row's
serialized size). It only makes that thing's TYPE say what it is, so a
future PR (#5973's own ①②③, building a "materialized BODY bytes"
budget on top of this) cannot accidentally reuse the wrong count without
mypy refusing to compile it.

## Why this is tested via a REAL mypy subprocess, not a runtime assertion

`typing.NewType` has ZERO runtime footprint — `ResidentBytes(5) == 5` is
literally `True`, `isinstance(ResidentBytes(5), int)` is `True`, and no
Python exception is ever raised for "wrong currency" at execution time;
the entire guarantee lives in the type checker's own analysis. A pytest
assertion cannot observe that guarantee — only mypy, actually invoked,
can. This is the real collaborator here (CLAUDE.md's "never fake a
collaborator" applied to a type checker: a hand-rolled AST scan
pretending to be mypy would be exactly the fake this rule forbids)."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_resident_bytes_and_body_bytes_are_mypy_incompatible(
    tmp_path: Path, out_of_process_reyn: str,
) -> None:
    """Tier 1: LOAD-BEARING — the required witness (architect's own
    acceptance for #5973's precursor PR): passing a `BodyBytes` value
    where `ResidentBytes` is expected is a real mypy `[arg-type]`
    finding; passing a genuine `ResidentBytes` value at the SAME call
    shape is not.

    strip: change either `NewType("...", int)` declaration in
    `chat_message.py` to a plain type alias (`ResidentBytes = int`) —
    the deliberately-wrong call below stops being flagged, and this
    test goes red (`assert result.returncode != 0` fails)."""
    probe = tmp_path / "probe_5973_resident_body_bytes.py"
    probe.write_text(
        textwrap.dedent("""\
            from reyn.runtime.chat_message import BodyBytes, ResidentBytes


            def _takes_resident(n: ResidentBytes) -> None:
                pass


            _wrong_currency: BodyBytes = BodyBytes(5)
            _takes_resident(_wrong_currency)

            _right_currency: ResidentBytes = ResidentBytes(5)
            _takes_resident(_right_currency)
        """),
        encoding="utf-8",
    )
    # Line 9 (1-indexed) is the deliberately wrong call; line 12 is the
    # correct one — asserted by position, never by re-deriving the file
    # content, so a probe edit above cannot silently desync the numbers
    # this test reads.
    wrong_call_line = 9
    right_call_line = 12

    repo_root = Path(out_of_process_reyn).parent
    env = {**os.environ, "PYTHONPATH": out_of_process_reyn}
    result = subprocess.run(
        [
            sys.executable, "-m", "mypy",
            "--cache-dir", str(tmp_path / ".mypy_cache"),
            "--no-error-summary",
            str(probe),
        ],
        capture_output=True, text=True, cwd=repo_root, env=env,
    )

    assert result.returncode != 0, (
        "expected mypy to flag BodyBytes passed where ResidentBytes is "
        f"required, got a clean exit.\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    reported_lines = {
        int(line.split(":", 2)[1])
        for line in result.stdout.splitlines()
        if line.startswith(str(probe)) and ": error:" in line
    }
    assert wrong_call_line in reported_lines, (
        f"expected an error on the BodyBytes-where-ResidentBytes-expected "
        f"line ({wrong_call_line}), got errors on {reported_lines!r} -- "
        f"full output:\n{result.stdout}"
    )
    assert right_call_line not in reported_lines, (
        f"the genuinely-correct ResidentBytes call (line {right_call_line}) "
        f"must NOT be flagged -- got errors on {reported_lines!r}, "
        f"full output:\n{result.stdout}"
    )
