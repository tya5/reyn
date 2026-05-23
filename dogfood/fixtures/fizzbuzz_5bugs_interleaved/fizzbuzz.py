"""FizzBuzz with 5 interleaved bugs.

Difficulty ramp over ``fizzbuzz_bug_planted``: the bugs **mask each other**.
Fixing one bug can surface another that was previously hidden — the
agent can't pattern-match a "5-fix template" and one-shot it; each fix
reveals what was masked behind it.

Bug inventory:

  1. **Zero special-case**: ``if n == 0: return "0"`` shadows the
     correct ``"FizzBuzz"`` answer for zero.
  2. **Positive-only guard**: ``if n > 0:`` wraps all the divisibility
     logic, so every negative falls straight through to the default.
     Hides bugs 3 / 4 / 5 for negatives until lifted.
  3. **Int return on default**: ``return n`` instead of ``return str(n)``.
     Only surfaces on the `test_return_type_is_str` assertion + any
     case where the divisibility branches don't match.
  4. **Typo in FizzBuzz literal**: ``"FizzBzz"`` (missing the "u").
     Hidden behind bug 5 until the order-of-check is fixed.
  5. **Order-of-check bug**: ``% 3`` is checked before ``% 15``, so
     ``fizzbuzz(15)`` returns ``"Fizz"`` and the ``% 15`` branch (=
     where bug 4 hides) is never reached.

Naive interaction (= why iteration is forced):

  - First pytest run: many tests fail. Fixing bug 5 (order) makes bug 4
    surface for n=15. Fixing bug 4 still leaves bug 1 + bug 2 + bug 3.
    Lifting bug 2 makes bug 3 surface for negative non-multiples.
"""


def fizzbuzz(n):
    if n == 0:
        return "0"

    if n > 0:
        if n % 3 == 0:
            return "Fizz"
        elif n % 5 == 0:
            return "Buzz"
        elif n % 15 == 0:
            return "FizzBzz"

    return n
