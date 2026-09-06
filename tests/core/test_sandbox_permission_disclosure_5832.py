"""Tier 2: `looks_permission_related` — the #5832 disclosure gate.

Real-machine incident (owner, 2026-09-06): a shell hook's write was denied
by the sandbox (`write_paths:` unset for that hook), and the generic
failure log line carried nothing beyond the child's raw traceback. lead-
coder needed 10 steps to trace it to the real cause; the owner could not
take step 1 from the screen alone.

`looks_permission_related` is deliberately NOT a denial classifier (contrast
`classify_denial`, tested in `test_sandbox_denial_class_2820.py` /
`test_sandbox_denial_class_5244.py`): `PermissionError: [Errno 1] Operation
not permitted: '<path>'` is IDENTICAL whether the sandbox denied the write
or a real filesystem did (a read-only mount, an actual permissions
problem) — a write denial has no signature disjoint from a genuine
non-sandbox failure the way fork/network denials do. So this function only
gates whether reyn's granted sandbox range is worth disclosing; it makes no
causal claim, and these tests pin that it does not.
"""
from __future__ import annotations

from reyn.security.sandbox.denial import looks_permission_related

# The owner's own captured incident text (#5832 issue body, verbatim):
#   shell-hook 'python3 .../.reyn/hooks/broker_drain.py' exited 1
#   (stderr: Traceback (most recent call last):
#     ...
#     return io.open(self, mode, buffering, encoding, errors, newline)
#   PermissionError: [Errno 1] Operation not permitted:
#     '.../.reyn/agents/coder-brown/state/broker_drain_cursor.json.tmp')
_REAL_OWNER_INCIDENT_STDERR = (
    b"Traceback (most recent call last):\n"
    b'  File "<string>", line 5, in <module>\n'
    b"    return io.open(self, mode, buffering, encoding, errors, newline)\n"
    b"PermissionError: [Errno 1] Operation not permitted: "
    b"'/home/coder-brown/.reyn/agents/coder-brown/state/broker_drain_cursor.json.tmp'\n"
)


def test_real_owner_incident_stderr_is_permission_related():
    """Tier 2: the exact captured incident text triggers the disclosure gate."""
    assert looks_permission_related(_REAL_OWNER_INCIDENT_STDERR) is True


def test_eacces_variant_is_permission_related():
    """Tier 2: Landlock's own denial errno (EACCES, "Permission denied") —
    a DIFFERENT backend/OS shape than the Seatbelt EPERM incident above —
    is recognized too. Widening beyond one platform's own error text is
    the whole point of NOT tying this to a specific denial classifier."""
    assert looks_permission_related(
        b"PermissionError: [Errno 13] Permission denied: '/tmp/x'"
    ) is True


def test_is_case_insensitive():
    """Tier 2: signature match must not hinge on exact casing."""
    assert looks_permission_related(
        b"PERMISSIONERROR: [ERRNO 1] OPERATION NOT PERMITTED: '/tmp/x'"
    ) is True


def test_unrelated_failure_is_not_permission_related():
    """Tier 2: control arm — an ordinary bug (a syntax error, a assertion
    failure) must NOT trigger the disclosure; it would be noise on every
    unrelated hook failure, not signal."""
    assert looks_permission_related(
        b"  File \"<string>\", line 1\n    def f(:\n          ^\nSyntaxError: invalid syntax\n"
    ) is False


def test_accepts_str_as_well_as_bytes():
    """Tier 2: same predicate over the decoded str form (production passes
    both shapes depending on call site — see `classify_denial`'s own
    str/bytes handling for the same reasoning)."""
    assert looks_permission_related("PermissionError: [Errno 1] Operation not permitted") is True
