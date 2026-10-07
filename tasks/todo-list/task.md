---
id: todo-list
kind: feature
repo: fixture
check: ./check.sh
timeout_minutes: 15
max_turns: 40
---

## What

Implement the `TodoList` class in `todo.py`. Every method currently raises `NotImplementedError`.
A `TodoList` is created with a path to a JSON file and stores its to-do items in that file, so a
new `TodoList` opened on the same path sees the same items.

Each item is a dict with exactly three keys: `id` (int), `title` (str) and `done` (bool).

- `TodoList(path)`: open the list stored at `path`. If the file does not exist, start empty
  without raising.
- `add(title)`: add a new item that is not done and return its `id`.
- `complete(item_id)`: mark the item as done.
- `delete(item_id)`: remove the item.
- `items(include_done=True)`: return the items in the order they were added. With
  `include_done=False`, leave out done items.

## Why

A small, well-specified feature with state, persistence and error handling. It is a step up
from FizzBuzz for comparing configurations on an easy task.

## Acceptance criteria

- `add` trims leading and trailing whitespace from the title. An empty or whitespace-only title
  raises `ValueError`.
- IDs start at 1 and increase by one per added item. An ID is never reused, even after its item
  is deleted and even after the list is reopened from the file.
- `complete` and `delete` raise `KeyError` for an ID that does not exist. Completing an item that
  is already done is allowed and changes nothing.
- `items()` returns copies, so changing the returned dicts does not change the stored list.
- Every change is saved to the file right away. A second `TodoList(path)` sees all earlier
  changes without any explicit save call.
- Only the Python standard library is used.
