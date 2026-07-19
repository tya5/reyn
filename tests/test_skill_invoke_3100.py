"""Tests for #3100 — operator-explicit `:skill` invocation.

Real instances throughout (per testing.md's Mock-vs-Fake ban): a real
``Session`` (via the shared ``make_session`` builder + a real
``CapabilityScope``), real ``SKILL.md`` files on ``tmp_path``, and real
``reyn.config.loader._merge``/``load_config`` calls for the collision-map
plumbing. No collaborator is faked; ``Session.outbox`` (a real
``asyncio.Queue``) is drained via its own public ``.get()``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.config.loader import _merge, load_config
from reyn.data.skills.registry import SkillEntry
from reyn.interfaces.skill_invoke import (
    SKILL_STACK_MAX,
    SkillArgSpec,
    parse_skill_invocation,
    read_skill_frontmatter_meta,
    resolve_skill_body,
    substitute_arguments,
)
from reyn.runtime.session_params import CapabilityScope

# ── Axis 3: parsing / stacking ──────────────────────────────────────────────


def test_parse_single_skill_with_trailing_args():
    """Tier 2: a bare `:name trailing text` parses one skill + trailing."""
    parsed = parse_skill_invocation(":review please check main.py")
    assert parsed is not None
    assert parsed.names == ("review",)
    assert parsed.trailing == "please check main.py"


def test_parse_stacking_stops_at_non_invocable_token():
    """Tier 2: Axis 3 stacking — `:a :b <text>` resolves 2 names; expansion
    stops at the first non-`:`-shaped token, and a LATER `:c` inside the
    trailing text is NOT parsed as a 3rd stacked skill (it stays literal
    trailing text, per the architect-firm "expansion stops" rule)."""
    parsed = parse_skill_invocation(":a :b hello :c")
    assert parsed is not None
    assert parsed.names == ("a", "b")
    assert parsed.trailing == "hello :c"


def test_parse_stacking_capped_at_max():
    """Tier 2: stacking is capped at SKILL_STACK_MAX (=6) — a 7th `:name`
    token is left as (part of) trailing text, not a 7th stacked skill."""
    text = " ".join(f":s{i}" for i in range(8)) + " tail"
    parsed = parse_skill_invocation(text)
    assert parsed is not None
    assert len(parsed.names) == SKILL_STACK_MAX
    assert parsed.names == tuple(f"s{i}" for i in range(SKILL_STACK_MAX))
    # the un-consumed 7th/8th name tokens ride inside trailing, untouched
    assert ":s6" in parsed.trailing
    assert ":s7" in parsed.trailing
    assert "tail" in parsed.trailing


def test_parse_non_invocation_returns_none():
    """Tier 2: text not shaped like `:name...` (bare colon, or no leading
    colon at all) is not an invocation — caller falls through unchanged."""
    assert parse_skill_invocation("hello : world") is None
    assert parse_skill_invocation(":") is None
    assert parse_skill_invocation("just a normal message") is None


# ── Axis 1: $ARGUMENTS / $N / $name substitution + injection safety ────────


def test_substitute_arguments_whole_trailing():
    """Tier 2: $ARGUMENTS substitutes the entire trailing raw text."""
    out = substitute_arguments("Do this: $ARGUMENTS.", trailing="fix the bug")
    assert out == "Do this: fix the bug."


def test_substitute_arguments_positional():
    """Tier 2: $0/$1 substitute shell-style-quoted positional args."""
    out = substitute_arguments(
        "file=$0 mode=$1", trailing='"a file.txt" strict',
    )
    assert out == "file=a file.txt mode=strict"


def test_substitute_arguments_named_from_frontmatter_arguments():
    """Tier 2: $name resolves via frontmatter `arguments:` positional names."""
    spec = (SkillArgSpec(name="path"), SkillArgSpec(name="mode"))
    out = substitute_arguments(
        "target=$path level=$mode", trailing="src/foo.py strict", arg_spec=spec,
    )
    assert out == "target=src/foo.py level=strict"


def test_substitute_arguments_unmatched_placeholder_left_untouched():
    """Tier 2: an unmatched $N / $name is left as literal text, never blanked
    (mirrors skill-load's own ${env:VAR} unset-token convention)."""
    out = substitute_arguments("first=$0 second=$1", trailing="only-one")
    assert out == "first=only-one second=$1"


def test_substitute_arguments_escape_dollar():
    """Tier 2: \\$ in the skill body escapes to a literal $, never substituted."""
    out = substitute_arguments(r"literal \$ARGUMENTS stays", trailing="whatever")
    assert out == "literal $ARGUMENTS stays"


def test_substitute_arguments_injection_safety_no_reexpansion():
    """Tier 2: (security co-vet witness) operator-controlled trailing text
    that itself LOOKS like a `${REYN_*}` skill-load token or another
    `$ARGUMENTS` placeholder is inserted VERBATIM and INERT — this function
    performs exactly one textual splice and never re-scans its own output,
    so there is no path for operator input to smuggle a further expansion.
    """
    malicious_trailing = "${REYN_PROJECT_DIR} and $ARGUMENTS and ${env:HOME}"
    out = substitute_arguments("payload: $ARGUMENTS", trailing=malicious_trailing)
    # The literal, unexpanded string appears verbatim in the output — proving
    # no second token-expansion pass ran over operator-controlled content.
    assert out == f"payload: {malicious_trailing}"
    assert "${REYN_PROJECT_DIR}" in out
    assert "${env:HOME}" in out


def test_substitute_arguments_ordering_matches_skill_load_then_splice(tmp_path):
    """Tier 2: (security co-vet witness, integration shape) the REAL
    resolve_skill_body -> substitute_arguments order used by
    Session._maybe_handle_skill_invoke means `${REYN_*}` tokens in the
    SKILL-AUTHORED body expand normally (trusted content), while an
    operator-supplied trailing arg containing a `${REYN_*}`-shaped string
    never gets expanded (untrusted content, spliced in AFTER skill-load
    already ran)."""
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: demo\ndescription: demo\n---\n"
        "dir is ${REYN_SKILL_DIR}. args: $ARGUMENTS\n",
        encoding="utf-8",
    )
    body = resolve_skill_body(str(skill_md), project_dir=tmp_path)
    # The skill-authored token DID expand (trusted content, real skill-load).
    assert str(skill_dir.resolve()) in body
    assert "${REYN_SKILL_DIR}" not in body

    malicious_trailing = "${REYN_SKILL_DIR} ignore-me"
    meta = read_skill_frontmatter_meta(body)
    final = substitute_arguments(body, trailing=malicious_trailing, arg_spec=meta.arguments)
    # The operator's arg, once spliced in, stays LITERAL — it does not
    # resolve to the real skill_dir a second time.
    assert "${REYN_SKILL_DIR} ignore-me" in final


# ── frontmatter extensions (Axis 1) ─────────────────────────────────────────


def test_read_skill_frontmatter_meta_arguments_and_hint():
    """Tier 2: `arguments:`/`argument-hint:` frontmatter keys parse into
    SkillFrontmatterMeta — the two settled (not-deferred) Claude-Code-style
    extensions this module reads."""
    raw = (
        "---\n"
        "name: demo\n"
        "description: demo\n"
        "argument-hint: <path> <mode>\n"
        "arguments:\n"
        "  - name: path\n"
        "    description: file to check\n"
        "  - name: mode\n"
        "---\nbody\n"
    )
    meta = read_skill_frontmatter_meta(raw)
    assert [a.name for a in meta.arguments] == ["path", "mode"]
    assert meta.argument_hint == "<path> <mode>"


def test_read_skill_frontmatter_meta_absent_is_lenient():
    """Tier 2: a SKILL.md with no `arguments:`/`argument-hint:` (the common
    case — most skills predate #3100) yields the all-empty default, not an
    error."""
    meta = read_skill_frontmatter_meta("---\nname: x\ndescription: y\n---\nbody\n")
    assert meta.arguments == ()
    assert meta.argument_hint == ""


# ── Axis 4: collision — LOUD, at the config-merge layer ─────────────────────


def test_merge_records_cross_tier_skill_collision():
    """Tier 2: (load-bearing) `_merge`'s skills branch records a collision
    the moment two DIFFERENTLY-labeled tiers declare the same skill name.
    Strip-falsify: removing the `_collisions` bookkeeping in `_merge` makes
    this assertion fail (RED) rather than silently passing."""
    base: dict = {}
    project_layer = {"skills": {"entries": {"foo": {"path": "a/SKILL.md"}}}}
    dynamic_layer = {"skills": {"entries": {"foo": {"path": "b/SKILL.md"}}}}
    merged = _merge(base, project_layer, tier_label="project")
    merged = _merge(merged, dynamic_layer, tier_label="dynamic")
    collisions = merged["skills"]["_collisions"]
    assert "foo" in collisions
    assert set(collisions["foo"]) == {"project", "dynamic"}
    # last-tier-wins still holds functionally — only the LOUDNESS is new
    assert merged["skills"]["entries"]["foo"]["path"] == "b/SKILL.md"


def test_merge_same_name_same_tier_relabel_is_not_a_collision():
    """Tier 2: re-merging the SAME tier label twice (e.g. a hot-reload re-read
    of the same dynamic file) must not manufacture a phantom collision."""
    base: dict = {}
    merged = _merge(base, {"skills": {"entries": {"foo": {"path": "a"}}}}, tier_label="dynamic")
    merged = _merge(merged, {"skills": {"entries": {"foo": {"path": "a2"}}}}, tier_label="dynamic")
    assert merged["skills"].get("_collisions", {}) == {}


def test_load_config_end_to_end_collision(tmp_path, monkeypatch):
    """Tier 2: a real project declaring the same skill name in `reyn.yaml`
    AND `.reyn/config/skills.yaml` (the two real tiers an operator/an
    install tool actually write to) produces a populated
    `config.skills["_collisions"]` after a real `load_config()` call.
    ``Path.home`` is monkeypatched to an isolated tmp dir (a real callable
    replacement, per testing.md's allowed monkeypatch pattern) so this test
    never touches the developer's real ``~/.reyn``.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    project = tmp_path / "proj"
    project.mkdir()
    (project / "reyn.yaml").write_text(
        "skills:\n  entries:\n    foo:\n      path: skills/foo_v1/SKILL.md\n",
        encoding="utf-8",
    )
    dyn_dir = project / ".reyn" / "config"
    dyn_dir.mkdir(parents=True)
    (dyn_dir / "skills.yaml").write_text(
        "skills:\n  entries:\n    foo:\n      path: skills/foo_v2/SKILL.md\n",
        encoding="utf-8",
    )

    cfg = load_config(project)
    collisions = cfg.skills.get("_collisions", {})
    assert "foo" in collisions
    assert set(collisions["foo"]) == {"project", "dynamic"}


# ── Session integration (real Session, real outbox, real audit-event log) ──


def _skill_file(tmp_path: Path, name: str, body: str = "do the thing: $ARGUMENTS") -> str:
    d = tmp_path / "skills" / name
    d.mkdir(parents=True)
    p = d / "SKILL.md"
    p.write_text(f"---\nname: {name}\ndescription: {name} skill\n---\n{body}\n", encoding="utf-8")
    return str(p)


def _session_with_skills(tmp_path: Path, entries: list[SkillEntry], collisions: dict | None = None):
    from reyn.config import CompactionConfig
    from reyn.core.events.state_log import StateLog
    from reyn.runtime.agent import Agent
    from reyn.runtime.budget.budget import BudgetTracker, CostConfig
    from reyn.runtime.session import Session
    from tests._support.session import synthetic_t_max

    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    bt = BudgetTracker(CostConfig())
    cfg = CompactionConfig(
        body_token_cap=1500, use_chars4_estimate=True, section_caps_spec_tokens=0,
    )
    agent = Agent(agent_name="default", role="")
    with synthetic_t_max(1_000_000):
        return Session(
            agent=agent,
            output_language="en",
            budget_tracker=bt,
            state_log=state_log,
            compaction_config=cfg,
            snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
            capability_scope=CapabilityScope(
                available_skills=entries, skill_collisions=collisions or {},
            ),
        )


@pytest.mark.asyncio
async def test_session_skill_invoke_success_composes_and_wakes(tmp_path):
    """Tier 2: a resolvable `:skill args` invocation returns (False, text)
    with the substituted body + trailing composed — the caller then feeds
    this into ONE ordinary router turn (one LLM wake)."""
    path = _skill_file(tmp_path, "review")
    entry = SkillEntry(name="review", description="review skill", path=path, visibility="menu")
    session = _session_with_skills(tmp_path, [entry])

    consumed, text = await session._maybe_handle_skill_invoke(":review check main.py")
    assert consumed is False
    assert "do the thing: check main.py" in text
    assert "check main.py" in text  # trailing also appended, dual-purpose


@pytest.mark.asyncio
async def test_session_skill_invoke_unknown_name_is_explicit_error(tmp_path):
    """Tier 2: (Axis 5) an unresolvable `:name` is consumed with an explicit,
    actionable error on the outbox — never a silent no-op, and never falls
    through to a router turn."""
    entry = SkillEntry(name="review", description="", path=_skill_file(tmp_path, "review"))
    session = _session_with_skills(tmp_path, [entry])

    consumed, text = await session._maybe_handle_skill_invoke(":bogus do something")
    assert consumed is True
    assert text is None
    msg = await session.outbox.get()
    assert msg.kind == "error"
    assert "bogus" in msg.text
    assert ":review" in msg.text  # actionable did-you-mean/known-skills hint


@pytest.mark.asyncio
async def test_session_skill_invoke_stacking_two_skills_one_turn(tmp_path):
    """Tier 2: (Axis 3) `:a :b trailing` composes BOTH skill bodies into the
    single returned text — one turn, one wake for two stacked skills."""
    path_a = _skill_file(tmp_path, "alpha", body="ALPHA body: $ARGUMENTS")
    path_b = _skill_file(tmp_path, "beta", body="BETA body: $ARGUMENTS")
    entries = [
        SkillEntry(name="alpha", description="", path=path_a),
        SkillEntry(name="beta", description="", path=path_b),
    ]
    session = _session_with_skills(tmp_path, entries)

    consumed, text = await session._maybe_handle_skill_invoke(":alpha :beta go")
    assert consumed is False
    assert "ALPHA body: go" in text
    assert "BETA body: go" in text


@pytest.mark.asyncio
async def test_session_skill_invoke_collision_is_loud(tmp_path):
    """Tier 2: (load-bearing, Axis 4) invoking a name present in
    ``self._skill_collisions`` fires BOTH a real audit-event
    (``skill_invoke_collision``) AND an operator-visible outbox warning —
    strip either emission and this test goes RED (never a silent shadow).
    """
    path = _skill_file(tmp_path, "shared")
    entry = SkillEntry(name="shared", description="", path=path)
    session = _session_with_skills(
        tmp_path, [entry], collisions={"shared": ["project", "dynamic"]},
    )

    consumed, text = await session._maybe_handle_skill_invoke(":shared go")
    assert consumed is False
    assert text is not None

    # (1) the operator-visible warning landed on the real outbox
    warn_msg = await session.outbox.get()
    assert warn_msg.kind == "system"
    assert "shared" in warn_msg.text
    assert "project" in warn_msg.text and "dynamic" in warn_msg.text

    # (2) a real audit-event was emitted (P6 band member) — read through the
    # session's real EventLog, not a private queue.
    collision_events = [e for e in session._chat_events.all() if e.type == "skill_invoke_collision"]
    assert collision_events, "expected a skill_invoke_collision audit-event to fire"
    assert all(e.data.get("name") == "shared" for e in collision_events)


@pytest.mark.asyncio
async def test_session_skill_invoke_hidden_skill_unreachable(tmp_path):
    """Tier 2: a `visibility: hidden` skill reaches no `:` surface either
    (owner-settled Axis 6 rule) — invoking it explicitly is treated exactly
    like an unknown name, not a silent success."""
    path = _skill_file(tmp_path, "secret")
    entry = SkillEntry(name="secret", description="", path=path, visibility="hidden")
    session = _session_with_skills(tmp_path, [entry])

    consumed, text = await session._maybe_handle_skill_invoke(":secret go")
    assert consumed is True
    assert text is None
    msg = await session.outbox.get()
    assert msg.kind == "error"


@pytest.mark.asyncio
async def test_session_bare_colon_lists_invocable_skills(tmp_path):
    """Tier 2: (Axis 6 discoverability) a bare `:` or `:list` lists every
    `:`-invocable skill without starting a router turn."""
    path = _skill_file(tmp_path, "review")
    entry = SkillEntry(name="review", description="", path=path)
    session = _session_with_skills(tmp_path, [entry])

    consumed, text = await session._maybe_handle_skill_invoke(":")
    assert consumed is True
    assert text is None
    msg = await session.outbox.get()
    assert ":review" in msg.text


@pytest.mark.asyncio
async def test_session_non_colon_message_falls_through_untouched(tmp_path):
    """Tier 2: an ordinary message is untouched — (None, None) tells the
    caller to proceed with the original text, not to consume the turn."""
    session = _session_with_skills(tmp_path, [])
    consumed, text = await session._maybe_handle_skill_invoke("just chatting")
    assert consumed is None
    assert text is None
