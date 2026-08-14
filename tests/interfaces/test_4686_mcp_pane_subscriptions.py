"""Tier 1: #4686 — the mcp pane's subscription grammar (``_mcp_pane_entries``).

Owner-approved grammar (issue #4686, architect comment 2026-08-14, verbatim):

    broker         [on]  · subscribed
        broker://inbox/reyn-reviewer
    filesystem     [on]
    some-server    [--]  · turn_context

Row population is the REQUESTED URI set (never honored-only — the issue's
own core motivation: a declined URI must stay visible, not disappear).
Three URI states follow (architect's follow-up comment, also #4686):
requested ∈ honored → no mark; requested ∉ honored → ``· not honored``;
honored is ``None`` for the whole connection → the SERVER row (not each
URI) gets ``· unconfirmed`` instead.

Pure-function tests against ``_mcp_pane_entries`` directly — no Session, no
MCP connection, matching this repo's existing ``DrawerRow`` pane tests
(``test_drawer_row_3691.py``)."""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.chrome import _mcp_pane_entries
from reyn.interfaces.repl.read_model import project_remote_snapshot
from reyn.interfaces.repl.status import _session_mcp_subscriptions


def _snap(mcp_subscriptions: "list[dict] | None" = None) -> dict:
    items = [
        {"kind": "mcp", "name": "broker", "on": True, "denied": False, "denied_reason": None},
        {"kind": "mcp", "name": "filesystem", "on": True, "denied": False, "denied_reason": None},
        {"kind": "mcp", "name": "some-server", "on": False, "denied": True, "denied_reason": "turn_context"},
    ]
    return {"visibility_items": items, "mcp_subscriptions": mcp_subscriptions or []}


def test_a_server_with_no_subscriptions_is_unchanged():
    """Tier 1: a server absent from mcp_subscriptions (or present with an
    empty uris list) renders exactly as the pre-#4686 base row — no note
    added, no sub-rows."""
    rows = _mcp_pane_entries(_snap())
    assert rows[0] == ("[on] broker", "/visibility off mcp broker")
    assert rows[1] == ("[on] filesystem", "/visibility off mcp filesystem")
    assert not any(row[0].startswith("    ") for row in rows), (
        "no indented URI sub-row may appear for a server with no subscriptions"
    )


def test_confirmed_subscription_gets_subscribed_note_and_uri_subrow():
    """Tier 1: honored=[] (Listen, fully confirmed) → server row gets
    ``· subscribed``, and the URI sub-row carries NO mark (it's live)."""
    rows = _mcp_pane_entries(_snap([
        {"server": "broker", "mode": "listen", "uris": ["broker://inbox/x"], "unhonored": []},
    ]))
    assert rows[0] == ("[on] broker  · subscribed", "/visibility off mcp broker")
    assert rows[1] == ("    broker://inbox/x", "")


def test_unconfirmed_honored_marks_the_server_row_not_the_uri():
    """Tier 1: honored=None (Legacy, or no successful open yet) → the SERVER
    row gets ``· unconfirmed`` — never a per-URI mark, since there is
    nothing to distinguish URI-by-URI when honored-ness can't be read at
    all for this connection."""
    rows = _mcp_pane_entries(_snap([
        {"server": "broker", "mode": "legacy", "uris": ["broker://inbox/x"], "unhonored": None},
    ]))
    assert rows[0] == ("[on] broker  · unconfirmed", "/visibility off mcp broker")
    assert rows[1] == ("    broker://inbox/x", "")  # the URI row itself carries no mark


def test_a_declined_uri_stays_visible_with_a_not_honored_mark():
    """Tier 1: THE core #4686 motivation — a URI the server did not honor
    (honored is a real set, this uri isn't in it) stays in the row list
    with ``· not honored``, rather than disappearing from the pane."""
    rows = _mcp_pane_entries(_snap([
        {
            "server": "broker", "mode": "listen",
            "uris": ["broker://inbox/x", "broker://inbox/y"],
            "unhonored": ["broker://inbox/y"],
        },
    ]))
    assert rows[0] == ("[on] broker  · subscribed", "/visibility off mcp broker")
    assert rows[1] == ("    broker://inbox/x", "")
    assert rows[2] == ("    broker://inbox/y  · not honored", "")


def test_denied_server_keeps_its_own_note_and_appends_the_subscription_note():
    """Tier 1: a [--] denied row already carries its own note (#3380); the
    subscription note is APPENDED after it, not replacing it — the two
    notes answer different questions (#3378's "two axes, two markers" is
    about STATE marks, not note text)."""
    rows = _mcp_pane_entries(_snap([
        {"server": "some-server", "mode": "legacy", "uris": ["x://y"], "unhonored": None},
    ]))
    (denied_row,) = [r for r in rows if r[0].startswith("[--]")]
    assert denied_row == (
        "[--] some-server  · denied while untrusted content is in context · unconfirmed",
        "",
    )


def test_a_uri_subrow_is_inert():
    """Tier 1: a URI sub-row has no slash command — it exists to be read, not
    operated (mirrors the agent pane's own session-under-agent rows, which
    ARE operable via /session switch; a subscribed URI has no analogous
    action)."""
    rows = _mcp_pane_entries(_snap([
        {"server": "broker", "mode": "listen", "uris": ["broker://inbox/x"], "unhonored": []},
    ]))
    assert rows[1][1] == ""


# ── status.py / read_model.py wiring ────────────────────────────────────────


def test_session_mcp_subscriptions_defaults_to_empty_when_seam_absent():
    """Tier 1: a session with no ``mcp_subscription_state`` accessor (or one
    that raises) degrades to [] — never a crash of the whole status
    readout, mirroring ``_session_pipelines``'s own defensiveness."""
    class _NoSeam:
        pass

    assert _session_mcp_subscriptions(_NoSeam()) == []


def test_session_mcp_subscriptions_reads_the_real_accessor():
    """Tier 1: when the accessor is present, its return value passes through
    unchanged."""
    class _WithSeam:
        def mcp_subscription_state(self):
            return [{"server": "broker", "mode": "legacy", "uris": ["x"], "unhonored": None}]

    assert _session_mcp_subscriptions(_WithSeam()) == [
        {"server": "broker", "mode": "legacy", "uris": ["x"], "unhonored": None},
    ]


def test_remote_snapshot_defaults_mcp_subscriptions_to_empty_list():
    """Tier 1: #4686 — a remote (AG-UI) client's projected snapshot always
    has the key, defaulting to [] (never raising a KeyError downstream in
    ``_mcp_pane_entries``, which does ``snap.get("mcp_subscriptions")``)."""
    snap = project_remote_snapshot(None)
    assert snap["mcp_subscriptions"] == []
