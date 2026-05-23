# Task

The directory contains:

- `fizzbuzz.py` — a `fizzbuzz(n)` implementation that **looks reasonable but has bugs**.
- `test_fizzbuzz.py` — pytest tests that catch the bugs.

## Your job

1. Run `python -m pytest test_fizzbuzz.py -q` to see which tests fail.
2. Read each failing test's message + look at the corresponding code path in `fizzbuzz.py`.
3. Fix the bug.
4. Re-run pytest. Some bugs are independent — fixing one may not reveal the others until you re-run.
5. Repeat until all tests pass.

When all tests pass, reply with a one-line summary like "all tests pass — bugs fixed: <list>" and stop.

Hint: the bugs are subtle — implementation looks right at a glance. Trust pytest's output over your reading of the code.
