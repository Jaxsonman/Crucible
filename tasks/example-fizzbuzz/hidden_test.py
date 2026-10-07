"""Hidden acceptance assertions. Run with the workspace as cwd; not part of the fixture."""

import os
import sys

sys.path.insert(0, os.getcwd())

from fizzbuzz import fizzbuzz

result = fizzbuzz(15)
assert result == [
    "1", "2", "Fizz", "4", "Buzz", "Fizz", "7", "8", "Fizz", "Buzz",
    "11", "Fizz", "13", "14", "FizzBuzz",
], result
assert fizzbuzz(0) == []
assert fizzbuzz(1) == ["1"]
try:
    fizzbuzz(-1)
except ValueError:
    pass
else:
    raise AssertionError("negative n must raise ValueError")
print("all assertions passed")
