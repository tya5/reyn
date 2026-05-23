# Task

`fizzbuzz.py` has **5 interleaved bugs** that cause most of `test_fizzbuzz.py` to fail. The bugs mask each other: fixing one can surface another that was previously hidden by it. You can't fix all 5 in one rewrite by pattern-matching — you must iterate.

## Your job

1. Run `python -m pytest test_fizzbuzz.py -q` to see the failures.
2. Read `fizzbuzz.py` to see the current implementation.
3. Edit `fizzbuzz.py` to address the failures.
4. **Re-run pytest** — you will likely see different failures now that previously-masked bugs are exposed.
5. Fix the new failures.
6. Repeat until all tests pass.

When all tests pass, reply with a short summary of what you changed and stop.

**Hint**: trust pytest's output over your reading of the code. Some bugs look like "obviously correct" Python idioms.
