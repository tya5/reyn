# Rules for `src/reyn/security/sandbox/`

- **`enforcement_self_test` (`self_test.py`) stays 2-layer** (deny leg only, write + spawn axes). The richer per-axis contract is CI-conformance-only. Folding a new axis in needs an owner-level decision.
