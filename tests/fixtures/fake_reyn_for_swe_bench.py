"""fake_reyn_for_swe_bench.py — minimal fake `reyn run swe_bench` shim.

Used by tests/test_swe_bench_runner.py to exercise the subprocess path in
swe_bench_runner.run_reyn() without a real reyn installation.

Behaviour is controlled via environment variables so tests can inject any
scenario cleanly:

    FAKE_REYN_MODE   success      (default) — print a valid final output JSON
                     nonzero      — exit with code 1 and print an error message
                     bad_output   — exit 0 but print unparseable stdout
                     empty        — exit 0 but print nothing
                     hang         — sleep forever (triggers caller's timeout)

    FAKE_REYN_PATCH  the git diff string to embed (default: "diff --git a/f b/f")
"""
from __future__ import annotations

import json
import os
import sys
import time

MODE = os.environ.get("FAKE_REYN_MODE", "success")
PATCH = os.environ.get("FAKE_REYN_PATCH", "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n")


def main() -> int:
    if MODE == "nonzero":
        print("fake_reyn: simulated reyn failure", file=sys.stderr)
        return 1

    if MODE == "bad_output":
        print("not json at all", file=sys.stdout)
        return 0

    if MODE == "empty":
        return 0

    if MODE == "hang":
        time.sleep(9999)
        return 0

    # success — emit what reyn run would print
    result_data = {
        "instance_id": "fake__instance-0",
        "patch": PATCH,
        "tests_passed": True,
        "attempts": 1,
    }
    print("skill           : swe_bench")
    print("model           : standard")
    print()
    print("=== Final Output ===")
    print(json.dumps(result_data, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
