#!/usr/bin/env bash
# Usage: bash run_agent.sh "<prompt>" ["<session_id>"]
# Runs from the task workspace. Source files are resolved relative to this script.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' '{"status": "error", "message": "python3 is not installed."}'
    exit 1
fi

exec python3 "$SCRIPT_DIR/agent.py" "$@"
