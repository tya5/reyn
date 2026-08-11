"""#4284: the shared minimal-`reyn.yaml` fixture literal.

`llm.model` is the one field `load_config` requires to resolve a usable
config in most of these tests — every other section is optional. Before
this, ~60 test files hand-wrote the literal `"llm:\n  model: standard\n"`
(or the pre-#4174-T3 `"model: standard\n"`) as fixture boilerplate to make
a `reyn.yaml` non-empty/loadable, with no assertion on the model value
itself. #4174 T3 (LLM domain -> `llm:` consolidation) had to touch every
one of those ~60 sites' literal text in the same PR it moved the schema
field — the next schema rename under `llm:` would do the same rewrite
again. This constant is the single site a future rename touches instead.

A `write_minimal_reyn_yaml(path)` FUNCTION form was considered and
rejected: surveying the call sites found the majority write this as a
standalone `write_text()` call, but a real minority (~15 sites) CONCATENATE
it with additional YAML content (`MINIMAL_REYN_YAML + "offload:\n  enabled:
true\n"`) — a plain string constant composes naturally for both shapes; a
function would need an `extra=` parameter to support the concatenating
sites, which is more surface for the same job.

NOT for tests that assert on the model value itself (`cfg.llm.model ==
"standard"` or similar) — those tests are testing the field this fixture
happens to also satisfy, and moving their literal out from under them
would make the fixture-vs-subject distinction harder to see, not easier.
"""
from __future__ import annotations

MINIMAL_REYN_YAML = "llm:\n  model: standard\n"
