"""Failing tests for the FizzBuzz TDD iteration scenario.

The agent's job: implement ``fizzbuzz(n: int) -> str`` in ``fizzbuzz.py``
so all of these pass. Tests cover the edge cases that catch most naive
first-attempt implementations.
"""
from fizzbuzz import fizzbuzz


def test_classic():
    """Classic FizzBuzz rules — multiples of 3 / 5 / 15."""
    assert fizzbuzz(3) == "Fizz"
    assert fizzbuzz(5) == "Buzz"
    assert fizzbuzz(15) == "FizzBuzz"
    assert fizzbuzz(1) == "1"
    assert fizzbuzz(7) == "7"


def test_zero():
    """0 is divisible by both 3 and 5 → FizzBuzz.

    Trap: implementations that special-case ``if n == 0: return "0"``
    or that use ``range(1, n+1)`` mental model fail here.
    """
    assert fizzbuzz(0) == "FizzBuzz"


def test_negative():
    """Negative integers follow the same rule (divisibility is sign-agnostic)."""
    assert fizzbuzz(-3) == "Fizz"
    assert fizzbuzz(-5) == "Buzz"
    assert fizzbuzz(-15) == "FizzBuzz"
    assert fizzbuzz(-7) == "-7"


def test_big_int():
    """Arbitrarily large Python ints work natively (no overflow)."""
    n = 3 ** 200  # divisible by 3, not by 5
    assert fizzbuzz(n) == "Fizz"
    # 7**60: factors are only 7, so divisible by neither 3 nor 5.
    # (10**50 would be divisible by 5 = 2*5; common test-author trap.)
    m = 7 ** 60
    assert fizzbuzz(m) == str(m)


def test_return_type_is_str():
    """Default branch must still return a str, not an int."""
    assert isinstance(fizzbuzz(1), str)
    assert isinstance(fizzbuzz(15), str)
    assert isinstance(fizzbuzz(-7), str)
