"""Hidden acceptance assertions. Run with the workspace as cwd; not part of the fixture."""

import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())

from todo import TodoList


def expect(exc, fn, *args):
    try:
        fn(*args)
    except exc:
        return
    raise AssertionError(f"{fn.__name__}{args} must raise {exc.__name__}")


with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "todos.json")

    todos = TodoList(path)
    assert todos.items() == []

    assert todos.add("  buy milk ") == 1
    assert todos.add("walk dog") == 2
    assert todos.add("read") == 3
    assert todos.items() == [
        {"id": 1, "title": "buy milk", "done": False},
        {"id": 2, "title": "walk dog", "done": False},
        {"id": 3, "title": "read", "done": False},
    ], todos.items()

    expect(ValueError, todos.add, "")
    expect(ValueError, todos.add, "   ")

    todos.complete(2)
    todos.complete(2)
    assert [i["id"] for i in todos.items(include_done=False)] == [1, 3]
    assert todos.items()[1] == {"id": 2, "title": "walk dog", "done": True}

    todos.delete(3)
    expect(KeyError, todos.complete, 3)
    expect(KeyError, todos.delete, 3)
    expect(KeyError, todos.complete, 99)

    todos.items()[0]["title"] = "mutated"
    assert todos.items()[0]["title"] == "buy milk", "items() must return copies"

    reopened = TodoList(path)
    assert reopened.items() == [
        {"id": 1, "title": "buy milk", "done": False},
        {"id": 2, "title": "walk dog", "done": True},
    ], reopened.items()
    assert reopened.add("new") == 4, "ids must not be reused after delete + reopen"

print("all assertions passed")
