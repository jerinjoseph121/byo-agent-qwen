#!/usr/bin/env python3
"""Live test runner for development: waits for test requests and runs them against a real endpoint.

Run it in a terminal that can reach the model endpoints (for example WSL), from the project root:
    python3 tests/live/watcher.py --keys ../keys.env

It watches tests/live/requests/ for small JSON files such as
    {"id": "r1", "endpoint": "openrouter", "suite": "basic", "filter": "session"}
    {"id": "r2", "endpoint": "qwen", "suite": "tasks", "kind": "debugging", "seed": 2, "repeat": 1}
    {"id": "r3", "endpoint": "qwen", "suite": "tasks", "kind": "programming", "budget": 480}
and writes each run's full output to tests/live/results/<id>.txt. Only these fixed test suites
can be run: basic (tests/run_tests.py), tasks (tests/run_tasks.py) and unit (tests/test_tools.py).

keys.env holds QWEN_BASE_URL=... and QWEN_API_KEY=... for your own Qwen endpoint and/or
OPENROUTER_API_KEY=... (see keys.env.example). Keys are never printed.
This tool is for local testing only and is not used by the agent.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

LIVE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(LIVE_DIR))
REQUESTS = os.path.join(LIVE_DIR, "requests")
RESULTS = os.path.join(LIVE_DIR, "results")
MAX_PARALLEL = 3

ENDPOINTS = {
    "openrouter": {"OPENAI_BASE_URL": "https://openrouter.ai/api/v1", "MODEL": "qwen/qwen3-14b",
                   "key": "OPENROUTER_API_KEY",
                   "AGENT_EXTRA_BODY": json.dumps({"reasoning": {"enabled": False},
                                                   "provider": {"require_parameters": True}})},
    "qwen": {"OPENAI_BASE_URL": None, "MODEL": "Qwen/Qwen3-14B", "key": "QWEN_API_KEY"},   # URL from keys.env
}


def load_keys(path):
    keys = {}
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            if "=" in line:
                name, value = line.split("=", 1)
                keys[name.strip()] = value.strip().strip('"').strip("'")
    return keys


def command_for(request):
    suite = request.get("suite")
    if suite == "basic":
        cmd = [sys.executable, "tests/run_tests.py"]
        if request.get("filter"):
            cmd.append(str(request["filter"]))
        return cmd
    if suite == "tasks":
        kind = request.get("kind", "all")
        if kind not in ("programming", "debugging", "all"):
            raise ValueError("bad kind")
        return [sys.executable, "tests/run_tasks.py", kind, "--seed", str(int(request.get("seed", 1))),
                "--repeat", str(int(request.get("repeat", 1)))]
    if suite == "unit":
        return [sys.executable, "tests/test_tools.py"]
    raise ValueError("unknown suite %r" % suite)


def run(request, keys):
    endpoint = ENDPOINTS[request.get("endpoint", "openrouter")]
    env = dict(os.environ)
    for name, value in endpoint.items():
        if name != "key":
            env[name] = value if value is not None else keys.get("QWEN_BASE_URL", "")
    if "AGENT_EXTRA_BODY" not in endpoint:
        env.pop("AGENT_EXTRA_BODY", None)
    env["QWEN_API_KEY"] = keys.get(endpoint["key"], "")
    env["AGENT_RESULTS_DIR"] = RESULTS
    env.pop("AGENT_TIME_BUDGET", None)
    if request.get("budget"):
        # A slow shared endpoint can be given the time a fast one would need (testing only).
        env["AGENT_TIME_BUDGET"] = str(int(request["budget"]))
    cmd = command_for(request)
    out_path = os.path.join(RESULTS, "%s.txt" % request["id"])
    started = time.time()
    with open(out_path + ".running", "w", encoding="utf-8") as out:
        out.write("request: %s\ncommand: %s\n\n" % (json.dumps(request), " ".join(cmd)))
        out.flush()
        proc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT,
                              timeout=int(request.get("timeout", 7200)))
        out.write("\n[finished with exit code %d in %.0fs]\n" % (proc.returncode, time.time() - started))
    os.replace(out_path + ".running", out_path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keys", default=os.path.join(ROOT, "..", "keys.env"))
    args = parser.parse_args()
    keys = load_keys(args.keys)
    os.makedirs(REQUESTS, exist_ok=True)
    os.makedirs(RESULTS, exist_ok=True)
    print("Watching %s (keys loaded: %s). Press Ctrl+C to stop." % (REQUESTS, ", ".join(sorted(keys))), flush=True)
    active = []
    while True:
        active = [t for t in active if t.is_alive()]
        for name in sorted(os.listdir(REQUESTS)):
            if not name.endswith(".json") or len(active) >= MAX_PARALLEL:
                continue
            path = os.path.join(REQUESTS, name)
            try:
                with open(path, encoding="utf-8") as f:
                    request = json.load(f)
                os.remove(path)
            except (OSError, ValueError):
                continue
            request.setdefault("id", name[:-5])
            thread = threading.Thread(target=worker, args=(request, keys), daemon=True)
            thread.start()
            active.append(thread)
        time.sleep(3)


def worker(request, keys):
    print("%s running %s" % (time.strftime("%H:%M:%S"), json.dumps(request)), flush=True)
    try:
        run(request, keys)
        print("%s finished %s" % (time.strftime("%H:%M:%S"), request["id"]), flush=True)
    except Exception as e:   # keep watching even if one request is bad
        print("request %s failed: %r" % (request["id"], e), flush=True)
        with open(os.path.join(RESULTS, "%s.txt" % request["id"]), "w", encoding="utf-8") as f:
            f.write("request failed: %r\n" % (e,))


if __name__ == "__main__":
    main()
