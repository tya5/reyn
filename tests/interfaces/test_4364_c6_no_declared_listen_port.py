"""Tier 1: #4364 C-6 — reyn declares no listen-port config field.

C-6's own motivating example (architect's report, #4364: a *different*
project's ``settings.port`` silently stopped taking effect across a
dependency bump) illustrates the general "declared ≠ effective" shape a
doctor check needs a real DECLARATION to pair against. Measured before
writing any check (per lead-coder's explicit instruction on this issue):
``reyn web``'s ``--host``/``--port`` are bare CLI arguments with no
corresponding ``ReynConfig`` field anywhere — there is nothing for
doctor to compare an "effective" bound port against.

This guard witnesses that finding stays true — the moment a real port
config field DOES appear (walking the schema below no longer returns
empty), C-6's own "listen port" slice needs revisiting (doctor.py's
module docstring, the paragraph naming this issue), not silent drift
between a stale docstring and new config surface.
"""
from __future__ import annotations


def test_no_config_schema_leaf_names_a_port() -> None:
    """Tier 1: enumerate the REAL ``ReynConfig`` schema (not a hand-picked
    guess) and assert no leaf's dotted PATH SEGMENT is (or starts) "port"
    — the concrete absence C-6's doctor.py docstring paragraph relies on.

    Matched per dotted segment, not a bare substring: a naive substring
    check false-positives on ``tool_use.transport`` / ``sandbox.
    on_unsupported`` / ``external_transports`` (each contains the letters
    "port" inside an unrelated word) — this witness would otherwise flag
    itself as broken forever on those three, never on a real port field.
    """
    import re

    from reyn.config.config_schema import walk_config_schema

    nodes = walk_config_schema()
    assert nodes, "the schema walk enumerated nothing — nothing to witness"

    port_leaves = [
        n.key for n in nodes
        if any(re.match(r"^port(_|$)", seg.lower()) for seg in n.key.split("."))
    ]
    assert port_leaves == [], (
        f"found port-shaped config leaf(ves) {port_leaves!r} — C-6's "
        "'reyn declares no listen port' finding (doctor.py's module "
        "docstring, the C-6 paragraph) is stale; revisit whether a "
        "declared↔effective check is now buildable for it."
    )


def test_the_port_matcher_would_catch_a_real_port_field() -> None:
    """Tier 1: accept-side witness for the matcher itself — proves the
    regex above genuinely fires on port-shaped names (``port``,
    ``listen_port``'s trailing segment doesn't match, only a segment that
    STARTS with "port" does, matching the paragraph's own "gateway.port"
    -shaped example) and does NOT fire on the three known false-positive
    names, without needing to fabricate a schema."""
    import re

    matcher = re.compile(r"^port(_|$)")
    assert matcher.match("port")
    assert matcher.match("port_number")
    assert not matcher.match("transport")
    assert not matcher.match("unsupported")
    assert not matcher.match("external_transports")


def test_reyn_web_port_is_a_bare_cli_argument_not_a_config_field() -> None:
    """Tier 1: the concrete counterpart of the schema-emptiness witness
    above — ``reyn web``'s ``--port`` argparse argument exists (so the
    concept itself is real), it's just never persisted to config."""
    import argparse

    from reyn.interfaces.cli.commands.web import register

    sub = argparse.ArgumentParser().add_subparsers()
    register(sub)
    parser = sub.choices["web"]
    dest_names = {a.dest for a in parser._actions}
    assert "port" in dest_names
