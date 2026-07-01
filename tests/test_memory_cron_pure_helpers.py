"""Tier 2: pure helpers in tools/memory.py and tools/cron.py.

  memory._strip_frontmatter(content)   — remove YAML frontmatter from text
  memory._parse_memory_index(content)  — parse MEMORY.md into entry list
  cron._jobs_list(data)                — extract cron.jobs list defensively
  cron._set_jobs_list(data, jobs)      — copy of data with cron.jobs set
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.tools.cron import _jobs_list, _set_jobs_list
from reyn.tools.memory import _parse_memory_index, _strip_frontmatter

# ---------------------------------------------------------------------------
# _strip_frontmatter
# ---------------------------------------------------------------------------


def test_strip_frontmatter_no_frontmatter_unchanged() -> None:
    """Tier 2: content without frontmatter is returned unchanged."""
    body = "Just a plain note.\nNo YAML here.\n"
    assert _strip_frontmatter(body) == body


def test_strip_frontmatter_removes_valid_block() -> None:
    """Tier 2: valid ---\\n...\\n--- block is stripped; body text remains."""
    content = "---\ntitle: test\ntype: memory\n---\n\nActual body.\n"
    result = _strip_frontmatter(content)
    assert "Actual body." in result
    assert "---" not in result
    assert "title:" not in result


def test_strip_frontmatter_unclosed_block_unchanged() -> None:
    """Tier 2: a leading --- with no closing --- is left unchanged."""
    content = "---\ntitle: orphan\nno closing marker"
    assert _strip_frontmatter(content) == content


def test_strip_frontmatter_empty_string_returns_empty() -> None:
    """Tier 2: empty content returns empty string."""
    assert _strip_frontmatter("") == ""


def test_strip_frontmatter_no_leading_dashes_unchanged() -> None:
    """Tier 2: content not starting with --- is returned as-is."""
    content = "# Memory Title\n\nSome content."
    assert _strip_frontmatter(content) == content


def test_strip_frontmatter_body_blank_line_stripped() -> None:
    """Tier 2: single blank line immediately after closing --- is also stripped."""
    content = "---\ntype: user\n---\n\nBody starts here.\n"
    result = _strip_frontmatter(content)
    assert result.startswith("Body")


# ---------------------------------------------------------------------------
# _parse_memory_index
# ---------------------------------------------------------------------------

_SAMPLE_INDEX = """\
# Memory Index (shared)
- [User Role](user_role.md) — TUI developer, feature branches only
- [Project Goal](project_goal.md) — deliver inline CUI

# Memory Index (agent:researcher)
- [Research Notes](research_notes.md) — key findings from paper survey
"""


def test_parse_memory_index_empty_content() -> None:
    """Tier 2: empty content returns empty list."""
    assert _parse_memory_index("") == []


def test_parse_memory_index_no_section_header() -> None:
    """Tier 2: content with no recognized section header returns empty list."""
    assert _parse_memory_index("# Some other header\n- [Note](note.md)\n") == []


def test_parse_memory_index_shared_entries() -> None:
    """Tier 2: entries under a shared section have layer='shared'."""
    entries = _parse_memory_index(_SAMPLE_INDEX)
    shared = [e for e in entries if e["layer"] == "shared"]
    assert any(e["slug"] == "user_role" for e in shared)
    assert any(e["slug"] == "project_goal" for e in shared)


def test_parse_memory_index_agent_entries() -> None:
    """Tier 2: entries under an agent section have layer='agent'."""
    entries = _parse_memory_index(_SAMPLE_INDEX)
    agent = [e for e in entries if e["layer"] == "agent"]
    assert any(e["slug"] == "research_notes" for e in agent)


def test_parse_memory_index_entry_name_and_description() -> None:
    """Tier 2: entry name and description are parsed correctly."""
    entries = _parse_memory_index(_SAMPLE_INDEX)
    user_role = next(e for e in entries if e["slug"] == "user_role")
    assert user_role["name"] == "User Role"
    assert "TUI developer" in user_role["description"]


def test_parse_memory_index_entry_without_description() -> None:
    """Tier 2: entry without a description dash produces empty description."""
    content = "# Memory Index (shared)\n- [MyNote](my_note.md)\n"
    entries = _parse_memory_index(content)
    assert entries[0]["description"] == ""


# ---------------------------------------------------------------------------
# _jobs_list
# ---------------------------------------------------------------------------


def test_jobs_list_standard_structure() -> None:
    """Tier 2: standard cron.jobs list is returned."""
    data = {"cron": {"jobs": [{"name": "j1"}, {"name": "j2"}]}}
    result = _jobs_list(data)
    assert {"name": "j1"} in result
    assert {"name": "j2"} in result


def test_jobs_list_empty_jobs() -> None:
    """Tier 2: cron.jobs=[] returns empty list."""
    assert _jobs_list({"cron": {"jobs": []}}) == []


def test_jobs_list_missing_cron_key() -> None:
    """Tier 2: no 'cron' key → empty list."""
    assert _jobs_list({}) == []


def test_jobs_list_cron_not_dict() -> None:
    """Tier 2: cron is not a dict → empty list (defensive)."""
    assert _jobs_list({"cron": "broken"}) == []


def test_jobs_list_jobs_not_list() -> None:
    """Tier 2: jobs is not a list → empty list (defensive)."""
    assert _jobs_list({"cron": {"jobs": "broken"}}) == []


# ---------------------------------------------------------------------------
# _set_jobs_list
# ---------------------------------------------------------------------------


def test_set_jobs_list_creates_cron_key() -> None:
    """Tier 2: empty data gets a 'cron.jobs' key with the provided list."""
    result = _set_jobs_list({}, [{"name": "new"}])
    assert result["cron"]["jobs"] == [{"name": "new"}]


def test_set_jobs_list_updates_existing_jobs() -> None:
    """Tier 2: existing cron.jobs is replaced with the new list."""
    data = {"cron": {"jobs": [{"name": "old"}]}}
    result = _set_jobs_list(data, [{"name": "new"}])
    assert result["cron"]["jobs"] == [{"name": "new"}]


def test_set_jobs_list_does_not_mutate_original() -> None:
    """Tier 2: the original data dict is not modified."""
    data = {"cron": {"jobs": [{"name": "orig"}]}}
    _set_jobs_list(data, [{"name": "new"}])
    assert data["cron"]["jobs"] == [{"name": "orig"}]


def test_set_jobs_list_preserves_other_cron_keys() -> None:
    """Tier 2: other keys inside cron are preserved."""
    data = {"cron": {"version": 2, "jobs": []}}
    result = _set_jobs_list(data, [{"name": "x"}])
    assert result["cron"]["version"] == 2


def test_set_jobs_list_preserves_top_level_keys() -> None:
    """Tier 2: top-level keys outside cron are preserved."""
    data = {"meta": "value", "cron": {"jobs": []}}
    result = _set_jobs_list(data, [])
    assert result["meta"] == "value"
