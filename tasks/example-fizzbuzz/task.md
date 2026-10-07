---
id: example-fizzbuzz
kind: feature
repo: fixture
check: ./check.sh
timeout_minutes: 10
max_turns: 30
---

## What

Implement `fizzbuzz(n)` in `fizzbuzz.py`. It currently raises `NotImplementedError`.
Given a positive integer `n`, return the list of strings for the numbers 1 through `n`.

## Why

A tiny, unambiguous feature used to check that Crucible can run a task end to end.

## Acceptance criteria

- `fizzbuzz(n)` returns a list of `n` strings, one per number from 1 to `n`.
- Multiples of 3 give `"Fizz"`, multiples of 5 give `"Buzz"`, multiples of both give `"FizzBuzz"`.
- Every other number is returned as its decimal string, for example `"7"`.
- `fizzbuzz(0)` returns an empty list and a negative `n` raises `ValueError`.
