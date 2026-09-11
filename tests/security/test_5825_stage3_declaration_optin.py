"""Tier 2: #5825 stage 3 -- FP-0069 §5's own acceptance (owner-accepted
2026-09-06, "declaration is opt-in"), pinned directly.

architect's own framing of this stage's deliverable: "段3の成果物は変える
より固定する側に寄る" -- the audit found that file / http.get / network are
ALREADY just-in-time (no declaration -> ask, not a hard raise); `require_tool`'s
own "not declared" raise is unreachable (#5848 deleted the TOOL-axis
constraint it depended on); `require_mcp` stays declaration-required as a
DELIBERATE policy exception (§6: "operator-declared surfaces ... unchanged");
`require_secret_write` stays declaration-required as a DELIBERATE structural
exception (§5's own opt-in logic is "a declaration's absence is safe because
the gate can ASK instead" -- `require_secret_write` has no `bus` parameter at
all, so it structurally cannot ask; making it optional would not create a
prompt, it would delete the gate). Nothing in `src/` needed to CHANGE for §5
to already be true. What was missing is what this file adds: without a
pinned acceptance, the next PR that reintroduces a mandatory declaration on
any of the JIT gates goes green, because nothing here would have been
watching.

## §5's own acceptance, verbatim (architect draft, lead-coder-adopted)

    宣言を config から消しても、attended な run が「拒否」ではなく
    「1 度 訊かれて続行」になること。
    赤にする方法: どれか 1 つの gate に「宣言が無ければ raise」を
    bus を引く前に 1 行 戻す。

Four criteria per §5-subject gate (① ② ③ from the acceptance's own "ask,
not raise" claim; ④ is the REQUIRED companion architect's own review added --
without it, "the declaration became MEANINGLESS" and "the declaration became
OPTIONAL" are indistinguishable, since both would still see ① pass):

1. no declaration + the bus answers YES/ALWAYS -> returns (no raise)
2. no declaration + the bus answers NO -> raises (the operator's own answer,
   not the missing declaration)
3. no declaration + bus=None -> raises (§4's own fail-closed: unattended
   means denied, never "pending")
4. WITH a declaration that is already pre-approved -> returns WITHOUT ever
   touching the bus (proves the declaration still does something -- it
   remains a real fast path, not a no-op the JIT ask would produce anyway)

## Population is DERIVED, never a hand-typed list (architect's own design)

`_require_gates()` walks the real `PermissionResolver` class via
`inspect.getmembers` -- the SAME shape architect's own audit command used.
Classification (`_OUT_OF_SCOPE` / `_POLICY_EXCEPTION` / `_DEAD` /
`_STRUCTURAL_EXCEPTION` / `_SUBJECT_GATES`) is a reasoned, hand-maintained
table (necessarily -- WHY a gate is excluded is a policy judgment no
signature can derive), but its own COMPLETENESS is machine-checked:
`test_every_gate_is_classified_exactly_once` fails loud if a NEW `require_*`
method lands uncovered by any bucket, or is claimed by more than one.

## The 5 things this stage was explicitly told not to write

`startup_guard` (does not exist in `src/`), `strict_declaration` (not in
§2's dial, dropped from §5 by lead-coder ruling 3 -- see
docs/deep-dives/proposals/0069-permission-posture-dial.md's own correction),
"will become a hard error in a future release" (the opposite of what §5
settled), a posture branch inside the resolver (none of the below reads
`permissions.mode` at all -- the opt-in is the SAME behaviour in every
mode except `read_only`, which the resolver's own doc-facing §5 correction
names as the one mode declaration stays required in, unchanged by this
file), and `require_tool` counted as a passing JIT gate (it is `_DEAD`
below, excluded from `_SUBJECT_GATES` on purpose).
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from reyn.intervention_choices import ALWAYS, NO, YES
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import InterventionAnswer, UserIntervention


class _FakeBus:
    """Real RequestBus-compatible fake that pre-answers with a scripted
    choice -- same pattern test_5825_require_network.py / test_require_
    file_jit_ask_1505.py already establish. No mocks."""

    def __init__(self, choice: str) -> None:
        self._choice = choice
        self.asks: "list[UserIntervention]" = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.asks.append(iv)
        return InterventionAnswer(text=self._choice, choice_id=self._choice)


def _resolver(tmp_path: Path, *, config: "dict | None" = None) -> PermissionResolver:
    return PermissionResolver(
        config_permissions=config or {}, project_root=tmp_path, interactive=True,
    )


# ── population + classification (derived, not hand-typed) ───────────────


def _require_gates() -> "list[str]":
    """Every `require_*` method on the real `PermissionResolver` class --
    architect's own audit command (`inspect.getmembers(PermissionResolver,
    callable)`, filtered to the `require_` prefix)."""
    return sorted(
        name for name, _ in inspect.getmembers(PermissionResolver, callable)
        if name.startswith("require_")
    )


def _has_param(name: str, param: str) -> bool:
    sig = inspect.signature(getattr(PermissionResolver, name))
    return param in sig.parameters


# decl=False: the axis this gate covers has no per-workflow declared list at
# all (size/trust checks, or a docstring-stated "no declaration required")
# -- structurally outside §5's own subject ("the declaration"), not merely
# exempted from it.
_OUT_OF_SCOPE = frozenset({
    "require_media_load", "require_plugin_git_run_code_trust", "require_web_fetch",
})

# decl=True, bus=True, but §6 names this an operator-declared surface
# ("hooks, MCP servers, skill_install ... unchanged") -- the declaration
# here is the operator's own inventory, not LLM ceremony §5 exists to
# remove. Reopen condition: none stated -- §6 itself would need to change.
_POLICY_EXCEPTION = frozenset({"require_mcp"})

# decl=True, bus=True, but #5848 deleted the AgentLayer TOOL-axis
# constraint / decl.tool this gate's own "not declared" raise depended on
# -- the raise is UNREACHABLE (its own docstring says so). Counting it as
# a passing JIT gate would be a vacuous green (six-questions ④): nothing
# ever exercises the raise branch to prove it doesn't fire.
_DEAD = frozenset({"require_tool"})

# decl=True, bus=False -- §5's own opt-in logic ("no declaration is safe
# because the gate can ask instead") cannot apply: there is no bus to ask
# with. Architect ruling 2 (adopted): stays declaration-required.
# Reopen condition (grep-able, not "someday"): the day `require_secret_
# write` gains a `bus` parameter -- see
# `test_the_bus_less_structural_exception_is_exactly_one_gate` below,
# which is that reopen condition, executable.
_STRUCTURAL_EXCEPTION = frozenset({"require_secret_write"})

# decl=True, bus=True, none of the above -- §5's actual subject. Verified
# ALREADY just-in-time (architect's audit, #5825 stage 3): no declaration
# asks via `bus`, never raises for the declaration's absence alone.
_SUBJECT_GATES = frozenset({
    "require_file_read", "require_file_write", "require_http_get", "require_network",
})


def test_the_require_gate_population_has_not_shrunk_to_nothing() -> None:
    """Tier 1: vacuity floor -- if the real scan ever finds 0 (or a
    handful) of `require_*` gates, the classification tests below would
    trivially "pass" over an empty or near-empty population. `>= 10`
    pins architect's own real count as the floor a broken scan cannot
    quietly slip under."""
    assert len(_require_gates()) >= 10, _require_gates()


def test_every_gate_is_classified_exactly_once() -> None:
    """Tier 1: the load-bearing completeness witness -- a NEW `require_*`
    method landing on `PermissionResolver` with no entry in any bucket
    above is UNCLASSIFIED, and this test goes red for it specifically
    (not folded silently into `_SUBJECT_GATES` by omission, and not
    silently ignored). Also guards against a gate claimed by two buckets
    at once, which would hide which policy actually governs it."""
    gates = frozenset(_require_gates())
    buckets = [_OUT_OF_SCOPE, _POLICY_EXCEPTION, _DEAD, _STRUCTURAL_EXCEPTION, _SUBJECT_GATES]
    union = frozenset().union(*buckets)
    assert union == gates, (
        f"unclassified: {gates - union}, "
        f"classified but no longer exists: {union - gates}"
    )
    total_len = sum(len(b) for b in buckets)
    assert total_len == len(union), "a gate is claimed by more than one bucket"


def test_out_of_scope_gates_are_exactly_the_ones_with_no_decl_parameter() -> None:
    """Tier 1: `_OUT_OF_SCOPE`'s own membership is DERIVED, not asserted
    by fiat -- every gate whose signature has no `decl` parameter at all
    must be in this bucket, and nothing else may be (a hand-typed set
    that silently drifted from the real signatures would be caught
    here)."""
    derived = frozenset(n for n in _require_gates() if not _has_param(n, "decl"))
    assert derived == _OUT_OF_SCOPE, (derived, _OUT_OF_SCOPE)


def test_the_bus_less_structural_exception_is_exactly_one_gate() -> None:
    """Tier 1: architect's own adopted re-open condition, executable --
    quoted directly from #5825's own thread: "{name for name in require_
    gates if has_decl(name) and not has_bus(name)} == {'require_secret_
    write'}". Goes red in EITHER direction: `require_secret_write` gains
    a `bus` parameter (its own structural reason for staying declaration-
    required is now gone -- re-open the exception), OR a DIFFERENT gate
    becomes decl-but-busless (a new, unreviewed instance of the same
    structural shape, landing without anyone deciding whether it also
    needs the exception)."""
    derived = frozenset(
        n for n in _require_gates() if _has_param(n, "decl") and not _has_param(n, "bus")
    )
    assert derived == _STRUCTURAL_EXCEPTION, (derived, _STRUCTURAL_EXCEPTION)


# ── the 4-criterion acceptance bundle, one §5-subject gate at a time ────
# Each function below is ONE of the 4 declared-gate criteria applied to
# ONE of the 4 `_SUBJECT_GATES` -- 4x4 = 16 tests. Grouped by gate so a
# single gate's own regression reads as one clear block, not scattered.


# ── require_file_read ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_file_read_no_decl_yes_returns(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Tier 2: ① no declaration + the bus answers YES -> returns, not
    raises. The zone root defaults to the resolver's own project_root, so the
    probe path must live under a GENUINELY separate directory, not merely
    a subdirectory of tmp_path (read defaults broad -- zone-root and
    below -- so anything under tmp_path is IN scope, the opposite of
    what "no declaration, outside scope" needs)."""
    r = _resolver(tmp_path)
    outside = str(tmp_path_factory.mktemp("outside_zone") / "secret.txt")
    await r.require_file_read(PermissionDecl(), outside, "skill_x", bus=_FakeBus(YES))


@pytest.mark.asyncio
async def test_file_read_no_decl_no_raises(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Tier 2: ② no declaration + the bus answers NO -> raises (the
    operator's own answer, not the missing declaration, is why)."""
    r = _resolver(tmp_path)
    outside = str(tmp_path_factory.mktemp("outside_zone") / "secret.txt")
    with pytest.raises(PermissionError):
        await r.require_file_read(PermissionDecl(), outside, "skill_x", bus=_FakeBus(NO))


@pytest.mark.asyncio
async def test_file_read_no_decl_no_bus_raises(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Tier 2: ③ no declaration + bus=None -> raises (§4 fail-closed:
    unattended means denied)."""
    r = _resolver(tmp_path)
    outside = str(tmp_path_factory.mktemp("outside_zone") / "secret.txt")
    with pytest.raises(PermissionError):
        await r.require_file_read(PermissionDecl(), outside, "skill_x", bus=None)


@pytest.mark.asyncio
async def test_file_read_pre_approved_returns_without_touching_bus(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Tier 2: ④ a path already approved (the declaration's own fast
    path) -> returns without the bus ever being asked. `session_approve_path` is
    the same real, public seam an operator-startup pre-approval would use
    -- not a private-state poke."""
    r = _resolver(tmp_path)
    outside = str(tmp_path_factory.mktemp("outside_zone") / "secret.txt")
    r.session_approve_path(outside, "skill_x", "file.read")
    bus = _FakeBus(NO)  # would deny if ever asked -- proves it was never asked
    await r.require_file_read(PermissionDecl(), outside, "skill_x", bus=bus)
    assert bus.asks == []


# ── require_file_write ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_file_write_no_decl_yes_returns(tmp_path: Path) -> None:
    """Tier 2: ① no declaration + the bus answers YES -> returns, not
    raises. The default write zone is narrow (`<zone-root>/.reyn`), so a
    plain path under `tmp_path` is already outside it."""
    r = _resolver(tmp_path)
    outside = str(tmp_path / "elsewhere" / "out.txt")
    await r.require_file_write(PermissionDecl(), outside, "skill_x", bus=_FakeBus(YES))


@pytest.mark.asyncio
async def test_file_write_no_decl_no_raises(tmp_path: Path) -> None:
    """Tier 2: ② no declaration + the bus answers NO -> raises (the
    operator's own answer, not the missing declaration, is why)."""
    r = _resolver(tmp_path)
    outside = str(tmp_path / "elsewhere" / "out.txt")
    with pytest.raises(PermissionError):
        await r.require_file_write(PermissionDecl(), outside, "skill_x", bus=_FakeBus(NO))


@pytest.mark.asyncio
async def test_file_write_no_decl_no_bus_raises(tmp_path: Path) -> None:
    """Tier 2: ③ no declaration + bus=None -> raises (§4 fail-closed:
    unattended means denied)."""
    r = _resolver(tmp_path)
    outside = str(tmp_path / "elsewhere" / "out.txt")
    with pytest.raises(PermissionError):
        await r.require_file_write(PermissionDecl(), outside, "skill_x", bus=None)


@pytest.mark.asyncio
async def test_file_write_pre_approved_returns_without_touching_bus(tmp_path: Path) -> None:
    """Tier 2: ④ a path already approved (the declaration's own fast
    path) -> returns without the bus ever being asked."""
    r = _resolver(tmp_path)
    outside = str(tmp_path / "elsewhere" / "out.txt")
    r.session_approve_path(outside, "skill_x", "file.write")
    bus = _FakeBus(NO)
    await r.require_file_write(PermissionDecl(), outside, "skill_x", bus=bus)
    assert bus.asks == []


# ── require_http_get ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_get_no_decl_yes_returns(tmp_path: Path) -> None:
    """Tier 2: ① no declaration + the bus answers YES -> returns, not
    raises (the real #6140/#6141-fixed legacy compat path)."""
    r = _resolver(tmp_path)
    await r.require_http_get(PermissionDecl(), "example.com", _FakeBus(YES), "skill_x")


@pytest.mark.asyncio
async def test_http_get_no_decl_no_raises(tmp_path: Path) -> None:
    """Tier 2: ② no declaration + the bus answers NO -> raises (the
    operator's own answer, not the missing declaration, is why)."""
    r = _resolver(tmp_path)
    with pytest.raises(PermissionError):
        await r.require_http_get(PermissionDecl(), "example.com", _FakeBus(NO), "skill_x")


@pytest.mark.asyncio
async def test_http_get_no_decl_no_bus_raises(tmp_path: Path) -> None:
    """Tier 2: ③ no declaration + bus=None -> raises (§4 fail-closed:
    unattended means denied)."""
    r = _resolver(tmp_path)
    with pytest.raises(PermissionError):
        await r.require_http_get(PermissionDecl(), "example.com", None, "skill_x")


@pytest.mark.asyncio
async def test_http_get_pre_approved_returns_without_touching_bus(tmp_path: Path) -> None:
    """Tier 2: ④ declared via `session_approve_host` (the SAME per-host
    key the declared-and-approved path persists to, #571 Phase 7) -- no
    `decl` needed on the call itself, since a persisted approval short-
    circuits membership the same way a live declaration would; returns
    without the bus ever being asked."""
    r = _resolver(tmp_path)
    r.session_approve_host("example.com", "skill_x", "http.get")
    bus = _FakeBus(NO)
    await r.require_http_get(PermissionDecl(), "example.com", bus, "skill_x")
    assert bus.asks == []


# ── require_network ──────────────────────────────────────────────────────
# `decl` is accepted for call-site parity but not itself consulted (the
# gate's own docstring) -- "declared" here means the config/ledger grant
# `permissions.network: allow` (or a persisted ALWAYS) provides, the
# actual mechanism #5825① wired.


@pytest.mark.asyncio
async def test_network_no_decl_yes_returns(tmp_path: Path) -> None:
    """Tier 2: ① no declaration + the bus answers YES -> returns, not
    raises."""
    r = _resolver(tmp_path)
    await r.require_network(PermissionDecl(), _FakeBus(YES), "skill_x", argv=["curl", "x"])


@pytest.mark.asyncio
async def test_network_no_decl_no_raises(tmp_path: Path) -> None:
    """Tier 2: ② no declaration + the bus answers NO -> raises (the
    operator's own answer, not the missing declaration, is why)."""
    r = _resolver(tmp_path)
    with pytest.raises(PermissionError):
        await r.require_network(PermissionDecl(), _FakeBus(NO), "skill_x", argv=["curl", "x"])


@pytest.mark.asyncio
async def test_network_no_decl_no_bus_raises(tmp_path: Path) -> None:
    """Tier 2: ③ no declaration + bus=None -> raises (§4 fail-closed:
    unattended means denied)."""
    r = _resolver(tmp_path)
    with pytest.raises(PermissionError):
        await r.require_network(PermissionDecl(), None, "skill_x", argv=["curl", "x"])


@pytest.mark.asyncio
async def test_network_pre_approved_returns_without_touching_bus(tmp_path: Path) -> None:
    """Tier 2: ④ declared via `permissions.network: allow` (config pre-
    approval, the real mechanism this axis uses) -- returns with
    bus=None even, proving the config grant alone is the fast path,
    nothing to ask."""
    r = _resolver(tmp_path, config={"network": "allow"})
    await r.require_network(PermissionDecl(), None, "skill_x", argv=["curl", "x"])


@pytest.mark.asyncio
async def test_network_persisted_always_returns_without_touching_bus(tmp_path: Path) -> None:
    """Tier 2: ④, the OTHER declared-and-approved path for this axis --
    a prior ALWAYS answer persisted to the ledger, then a later call
    with a bus that would deny if asked, proving the persisted grant is
    the fast path."""
    r = _resolver(tmp_path)
    await r.require_network(PermissionDecl(), _FakeBus(ALWAYS), "skill_x", argv=["curl", "x"])
    bus = _FakeBus(NO)
    await r.require_network(PermissionDecl(), bus, "skill_x", argv=["curl", "x"])
    assert bus.asks == []
