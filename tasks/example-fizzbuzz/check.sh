#!/usr/bin/env bash
# Run with the workspace as cwd. Exits non-zero if the hidden assertions fail.
set -e
exec python3 "${CRUCIBLE_TASK_DIR:-$(dirname "$0")}/hidden_test.py"
