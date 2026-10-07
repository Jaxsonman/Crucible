#!/usr/bin/env python3
"""Fake `gh` CLI shared by tests. Never calls the network.

Env: FAKE_GH_DATA (path to JSON {"prs": [...], "issues": {"<n>": {...}}, "unsupported": [field,
...]}), FAKE_GH_ARGV_FILE (every call's argv is appended as one JSON line), FAKE_GH_EXIT (exit with
this code and an auth error instead of serving anything).
Serves `pr list --json f1,f2` and `issue view N --json f1,f2`, keeping only the asked fields.
"""

import json
import os
import sys

argv = sys.argv[1:]
if os.environ.get("FAKE_GH_ARGV_FILE"):
    with open(os.environ["FAKE_GH_ARGV_FILE"], "a") as f:
        f.write(json.dumps(argv) + "\n")
if os.environ.get("FAKE_GH_EXIT"):
    print("To get started with GitHub CLI, please run:  gh auth login", file=sys.stderr)
    sys.exit(int(os.environ["FAKE_GH_EXIT"]))
with open(os.environ["FAKE_GH_DATA"]) as fh:
    data = json.load(fh)
fields = argv[argv.index("--json") + 1].split(",") if "--json" in argv else []
for bad in data.get("unsupported", []):
    if bad in fields:
        print(f'Unknown JSON field: "{bad}"\nAvailable fields:\n  number', file=sys.stderr)
        sys.exit(1)


def pick(obj):
    return {k: v for k, v in obj.items() if k in fields}


if argv[:2] == ["pr", "list"]:
    print(json.dumps([pick(p) for p in data["prs"]]))
elif argv[:2] == ["issue", "view"]:
    issue = data["issues"].get(argv[2])
    if issue is None:
        print("issue not found", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(pick(issue)))
else:
    print(f"fake gh: unsupported {argv}", file=sys.stderr)
    sys.exit(2)
