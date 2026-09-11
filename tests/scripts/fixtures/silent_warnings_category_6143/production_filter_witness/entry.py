"""Process entry point -- THIS module (and only this one, in this
fixture) is __main__ when run directly. Deliberately does not call
`warnings.warn` itself, so no frame in the chain it drives is `__main__`
at the point the warning fires -- the realistic shape of a warning raised
deep inside `src/reyn/**`, several imports away from any process entry
point. Run with plain `python3 entry.py` (no -W flag, no injected
filter) to read Python's own stock default behaviour, not a test
harness's own filter."""
from __future__ import annotations

import caller_mod

caller_mod.call_it()
