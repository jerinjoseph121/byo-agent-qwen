#!/usr/bin/env python3
"""byo-agent-qwen: a tool-calling agent built from scratch on Qwen3-14B.

Usage: agent.py "<prompt>" ["<session_id>"]

Writes exactly one JSON object to stdout. Diagnostics go to stderr.
The code is in the byo_agent package next to this file.
"""
import os
import sys

sys.dont_write_bytecode = True   # keep __pycache__ folders out of the project folder
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from byo_agent.main import main  # noqa: E402

if __name__ == "__main__":
    main()
