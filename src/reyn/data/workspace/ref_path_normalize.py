"""#4482 PR-1: the ONE path-canonicalization function ref minting and the
ref → path lookup table both call — architect's review named this the
implementation hazard: "正規化を1箇所に置き、ref→pathの対応表と同じ関数を
使うこと... 2箇所に分かれると同じファイルに2つのrefが生まれます" (put
normalization in ONE place, used by both mint and lookup — split it across
two and the same file gets two different refs). Same shape this session has
hit repeatedly tonight: two call sites independently holding the same
invariant drift apart (#2957 PR-B, #4451).

Ref identity is ``(session, absolute path)`` 1:1 (architect's #4482 ruling —
content-hash identity was rejected: it conflicts with the owner's own
"don't copy, open the original" decision, since a content-hash ref can only
stay resolvable across a content change by having captured the bytes at
mint time). This module owns the "absolute path" half of that pair — the
session half is the caller's own scope, not this function's concern.

## Three axes, one canonical form

- **relative vs absolute** — a relative path is resolved against
  ``project_root``, matching :class:`MediaStore`'s own established
  convention (``(self._project_root / p).resolve()`` else ``p.resolve()``).
- **symlink** — ``Path.resolve()`` follows symlinks and collapses ``.``/
  ``..`` components, so a symlink and its real target normalize to the
  identical form.
- **case** — folded via ``os.path.normcase`` (stdlib): a no-op on POSIX,
  lowercases on Windows. **Deliberately NOT a real per-file filesystem
  probe** for macOS's case-insensitive-but-case-preserving DEFAULT (or a
  case-insensitive Linux mount): that would need to stat an alternate-case
  variant of every path at normalize time, a real I/O cost paid on every
  call for a condition with no measured instance yet — the same
  "measurement before mechanism" discipline #4478/#4476 both applied
  tonight. ``os.path.normcase`` is the honest stdlib answer for what
  Python itself considers case-normalization; the one known gap this
  leaves (macOS's default) is named here, not silently absent.
"""
from __future__ import annotations

import os
from pathlib import Path


def normalize_ref_path(path: "str | Path", project_root: Path) -> Path:
    """Canonical on-disk identity for *path*, relative to *project_root*.

    Two calls with different spellings of the SAME file (one relative, one
    absolute; one through a symlink, one direct; case-differing on a
    case-insensitive filesystem per :mod:`os.path`'s own ``normcase``)
    return the IDENTICAL ``Path``. This is what makes ref minting
    idempotent — the ref → path table keys on this function's output, never
    the caller's raw string, so "same path, different spelling" can never
    mint two refs for one file.

    Does not require *path* to exist — a caller minting a ref for a
    just-written file and a caller looking up an existing ref both get a
    normalized form; only the LOOKUP side needs to additionally check
    existence (a target that's vanished is a resolution failure, a
    different, later question than "does today's spelling normalize the
    way an earlier spelling did").
    """
    p = Path(path)
    if not p.is_absolute():
        p = project_root / p
    resolved = p.resolve()
    return Path(os.path.normcase(str(resolved)))
