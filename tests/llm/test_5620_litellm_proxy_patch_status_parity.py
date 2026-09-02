"""Tier 2: #5620 — the standalone proxy patch's own status-file path
literal stays in sync with reyn's own constant, on EVERY litellm
version (never gated by the 1.95.0 pin `test_5620_litellm_proxy_patch_
d.py`'s own D-behavior tests carry — this check needs no litellm import
at all, architect's own #5620 design point 3: "版に依らないので skip の
外").

`scripts/litellm_proxy_patch/litellm_proxy_patch.py` cannot import
`reyn.llm.litellm_proxy_patch_status` (it must never import reyn at
all — owner's own "proxy はランタイムだけで良い" ruling, separate venv,
separate litellm version, no reyn install there) — so its own
``_STATUS_PATH_STR`` is a HAND-COPIED literal. A silent drift here
makes `reyn doctor` report "not installed" forever for a patch that is
actually running fine — this file is the ONE place that stays sync'd
mechanically rather than by discipline alone.
"""
from __future__ import annotations

from pathlib import Path

from reyn.llm.litellm_proxy_patch_status import LITELLM_PROXY_PATCH_STATUS_PATH_STR
from tests._support.paths import REPO_ROOT

_PROXY_PATCH_FILE = (
    REPO_ROOT / "scripts" / "litellm_proxy_patch" / "litellm_proxy_patch.py"
)


def test_standalone_patch_file_status_path_literal_matches_reyn_side() -> None:
    """Tier 2: #5620 accept — reads BOTH literals as plain text (no
    litellm import) and asserts they are byte-identical."""
    assert _PROXY_PATCH_FILE.is_file(), (
        f"sanity: the standalone patch file must exist at {_PROXY_PATCH_FILE}"
    )
    source = _PROXY_PATCH_FILE.read_text(encoding="utf-8")
    needle = f'_STATUS_PATH_STR = "{LITELLM_PROXY_PATCH_STATUS_PATH_STR}"'
    assert needle in source, (
        f"litellm_proxy_patch.py's own _STATUS_PATH_STR literal must equal "
        f"reyn.llm.litellm_proxy_patch_status.LITELLM_PROXY_PATCH_STATUS_PATH_STR "
        f"({LITELLM_PROXY_PATCH_STATUS_PATH_STR!r}) verbatim — expected to find "
        f"{needle!r} in the standalone file's own source; it was not found, "
        f"meaning the 2 copies have drifted"
    )


def test_edit_to_break_the_literal_makes_the_parity_check_fail() -> None:
    """Tier 2: #5620 strip-falsify — with the standalone file's own
    literal genuinely altered (a real byte-for-byte string edit, written
    to a scratch copy, never the real file), the SAME needle-search this
    accept test relies on correctly fails to find it — proving the
    accept test above is actually reading content, not vacuously passing
    on an empty or malformed comparison."""
    import tempfile

    source = _PROXY_PATCH_FILE.read_text(encoding="utf-8")
    broken = source.replace(
        f'_STATUS_PATH_STR = "{LITELLM_PROXY_PATCH_STATUS_PATH_STR}"',
        '_STATUS_PATH_STR = "~/.reyn/some-other-drifted-path.json"',
    )
    assert broken != source, "sanity: the replace above must have changed something"

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(broken)
        broken_path = Path(f.name)
    try:
        broken_source = broken_path.read_text(encoding="utf-8")
        needle = f'_STATUS_PATH_STR = "{LITELLM_PROXY_PATCH_STATUS_PATH_STR}"'
        assert needle not in broken_source, (
            "with the literal genuinely altered, the needle-search must "
            "NOT find it — if it does, this test's own edit did nothing"
        )
    finally:
        broken_path.unlink()
