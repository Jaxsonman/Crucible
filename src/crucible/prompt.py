"""Prompt construction. See docs/design.md (Task document)."""

import hashlib

from crucible.task import Task

PREAMBLE = (
    "You are working autonomously in this repository. Do not ask questions; "
    "make reasonable decisions and finish the task."
)


def build_prompt(task: Task) -> str:
    criteria = "\n".join(f"- {c}" for c in task.criteria)
    return (
        f"{PREAMBLE}\n\n## What\n\n{task.what}\n\n## Why\n\n{task.why}\n\n"
        f"## Acceptance criteria\n\n{criteria}\n"
    )


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()
