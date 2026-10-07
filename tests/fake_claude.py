#!/usr/bin/env python3
"""Fake `claude` CLI shared by tests. Never calls the network.

Env: FAKE_CLAUDE_ARGV_FILE (write argv JSON), FAKE_CLAUDE_SCENARIO (success | max_turns | crash |
garbage | hang), FAKE_CLAUDE_WRITE (JSON {relative_path: content} written into cwd first),
FAKE_CLAUDE_RESULT (override `result`), FAKE_CLAUDE_RESULT_SEQ_FILE (JSON list of results, one
per call),FAKE_CLAUDE_MODEL (override primary model ID),
FAKE_CLAUDE_PID_FILE (hang only: writes "<fake pid> <grandchild pid>").
Model IDs come from pricing.yaml in $CRUCIBLE_HOME (parsed without yaml; no deps).
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def pricing_models() -> list[str]:
    home = os.environ.get("CRUCIBLE_HOME")
    ids: list[str] = []
    if home and (Path(home) / "pricing.yaml").is_file():
        in_models = False
        for line in (Path(home) / "pricing.yaml").read_text().splitlines():
            if line.startswith("models:"):
                in_models = True
            elif in_models:
                m = re.match(r"^  ([^\s#:][^\s:]*):\s*$", line)
                if m:
                    ids.append(m.group(1))
    return ids or ["claude-opus-5-5", "claude-sonnet-5-5"]


def usage(inp, out, cc, cr, cost):
    return {
        "inputTokens": inp,
        "outputTokens": out,
        "cacheReadInputTokens": cr,
        "cacheCreationInputTokens": cc,
        "webSearchRequests": 0,
        "costUSD": cost,
    }


def main() -> int:
    argv_file = os.environ.get("FAKE_CLAUDE_ARGV_FILE")
    if argv_file:
        Path(argv_file).write_text(json.dumps(sys.argv[1:]))
    for rel, content in json.loads(os.environ.get("FAKE_CLAUDE_WRITE") or "{}").items():
        p = Path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    for rel, content in json.loads(os.environ.get("FAKE_CLAUDE_WRITE_LATIN1") or "{}").items():
        Path(rel).write_bytes(content.encode("latin-1"))  # non-UTF-8 bytes in the workspace

    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "success")
    if scenario == "crash":
        print("fake claude: boom (crash scenario)", file=sys.stderr)
        return 1
    if scenario == "garbage":
        print("this is not json {{{")
        return 0
    if scenario == "hang":
        child = subprocess.Popen(["sleep", "600"])
        pid_file = os.environ.get("FAKE_CLAUDE_PID_FILE")
        if pid_file:
            Path(pid_file).write_text(f"{os.getpid()} {child.pid}")
        time.sleep(600)
        return 0

    models = pricing_models()
    primary = os.environ.get("FAKE_CLAUDE_MODEL") or models[0]
    secondary = next((m for m in models if m != primary), "claude-secondary-fake")
    usage_map = {
        primary: usage(1000, 500, 2000, 3000, 0.0123),
        secondary: usage(200, 100, 400, 600, 0.0045),
    }
    is_max_turns = scenario == "max_turns"
    out = {
        "type": "result",
        "subtype": "error_max_turns" if is_max_turns else "success",
        "is_error": is_max_turns,
        "num_turns": 3 if is_max_turns else 5,
        "duration_ms": 4200,
        "duration_api_ms": 3900,
        "total_cost_usd": 0.0168,
        "session_id": "fake-session-0001",
        "modelUsage": usage_map,
    }
    if not is_max_turns:
        result = os.environ.get("FAKE_CLAUDE_RESULT", "ok")
        seq_file = os.environ.get("FAKE_CLAUDE_RESULT_SEQ_FILE")
        if seq_file:  # JSON list of results, consumed one per call (last one repeats)
            seq = json.loads(Path(seq_file).read_text())
            counter = Path(seq_file + ".n")
            n = int(counter.read_text()) if counter.exists() else 0
            counter.write_text(str(n + 1))
            result = seq[min(n, len(seq) - 1)]
        out["result"] = result
    line = json.dumps(out)
    if os.environ.get("FAKE_CLAUDE_LATIN1_STDOUT"):
        sys.stdout.buffer.write(line.encode("utf-8") + b"\n# caf\xe9\n")
        sys.stdout.buffer.flush()
        return 0
    print(line)
    return 1 if is_max_turns else 0


if __name__ == "__main__":
    sys.exit(main())
