"""FizzBuzz implementation — has 3 subtle bugs the agent must locate + fix.

The bugs are independent: each fails a different test, so the agent must
read pytest output, identify which test fails, locate the root cause,
fix, and re-run. They are also subtle enough that the agent can't
1-shot a full rewrite from the docstring — they need the test feedback.
"""


def fizzbuzz(n):
    # Bug 1: special-cases zero before the divisibility check. Most
    # naive implementations do this "to avoid the 0%3 corner case",
    # but it makes ``fizzbuzz(0)`` return "0" instead of "FizzBuzz".
    if n == 0:
        return "0"

    # Bug 2: positive-only guard. Many implementations assume FizzBuzz
    # is only defined for positive integers and add this check
    # defensively. The tests expect negatives to follow the same rule.
    if n > 0:
        if n % 15 == 0:
            return "FizzBuzz"
        if n % 3 == 0:
            return "Fizz"
        if n % 5 == 0:
            return "Buzz"

    # Bug 3: default branch returns the int directly. Looks fine in
    # most languages but the tests assert ``isinstance(result, str)``
    # — the missing ``str(...)`` call surfaces only on the type check.
    return n
