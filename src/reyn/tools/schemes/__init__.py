"""Built-in tool-use schemes (#1593).

Each scheme module calls ``register_scheme`` at import time (self-registration),
and this package ``__init__`` imports every built-in module — so importing the
package (or *any* submodule, since a submodule import runs the package ``__init__``
first) registers the full built-in set. The OS scheme resolver
(``_resolve_tool_use_scheme``) therefore no longer needs to name any scheme class:
it imports this package and resolves by name (P7 cleanliness, #1608 ④).

Completeness invariant: all built-in scheme names resolve after importing this
package, with NO prior explicit scheme import by the caller (pinned by a
fresh-interpreter registry self-test).
"""
from . import (  # noqa: F401 — imported for the register_scheme import-time side effect
    codeact,
    enumerate_all,
    retrieval,
    universal_category,
)
