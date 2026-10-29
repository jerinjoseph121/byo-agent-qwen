#!/usr/bin/env python3
"""End-to-end test runner for file, CSV and conversation tasks.

Each case in cases.json runs in a fresh temporary workspace. A case is either a
single request (no session ID) or a list of turns, each with its own session ID.
Every turn is a new agent process. Session history is kept in a temporary folder
that is removed after the run.

Usage (from the project root):
    export OPENAI_BASE_URL=<endpoint> MODEL=Qwen/Qwen3-14B QWEN_API_KEY=<key>
    python3 tests/run_tests.py            # run all cases
    python3 tests/run_tests.py session    # run cases whose name contains "session"
"""
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SUBMISSION_DIR = os.path.dirname(TESTS_DIR)
CASES_FILE = os.environ.get("CASES_FILE", os.path.join(TESTS_DIR, "cases.json"))


def snapshot(root):
    """Return {relative path: bytes} for every file under root."""
    files = {}
    for folder, _, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            with open(path, "rb") as f:
                files[os.path.relpath(path, root)] = f.read()
    return files


def setup_workspace(workspace, files, dirs=()):
    """Create the case's input files and empty folders in the workspace."""
    for rel in dirs:
        os.makedirs(os.path.join(workspace, rel), exist_ok=True)
    for rel, content in files.items():
        path = os.path.join(workspace, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)


def run_agent(workspace, prompt, session_id, session_dir):
    """Run run_agent.sh from the workspace, the way the agent is meant to be called."""
    env = dict(os.environ)
    env.setdefault("OPENAI_BASE_URL", "http://localhost:8000/v1")
    env.setdefault("MODEL", "Qwen/Qwen3-14B")
    env["AGENT_SESSION_DIR"] = session_dir
    command = ["bash", os.path.join(SUBMISSION_DIR, "run_agent.sh"), prompt]
    if session_id:
        command.append(str(session_id))
    return subprocess.run(command, cwd=workspace, env=env, capture_output=True, text=True, timeout=180)


def check_output(turn, proc, problems):
    """Check the exit code and that stdout is one JSON object with the expected status."""
    try:
        output = json.loads(proc.stdout)
    except ValueError:
        problems.append("stdout is not exactly one JSON object: %r" % proc.stdout[:200])
        return {}
    if not isinstance(output, dict) or set(output) != {"status", "message"}:
        problems.append("JSON must contain exactly 'status' and 'message': %r" % output)
        return output if isinstance(output, dict) else {}
    if not all(isinstance(v, str) for v in output.values()):
        problems.append("'status' and 'message' must be strings")
    expected_status = turn.get("status", "success")
    if output.get("status") != expected_status:
        problems.append("status is %r, expected %r (message: %r)"
                        % (output.get("status"), expected_status, output.get("message")))
    if (proc.returncode == 0) != (expected_status == "success"):
        problems.append("exit code %d does not match status %r" % (proc.returncode, expected_status))
    return output


def check_answer(turn, output, problems):
    """Check the message for expected facts, forbidden facts and exact answers (case-insensitive)."""
    message = output.get("message", "") if isinstance(output.get("message"), str) else ""
    for fact in turn.get("answer_contains", []):
        if fact.lower() not in message.lower():
            problems.append("answer is missing %r (got %r)" % (fact, message))
    for fact in turn.get("answer_not_contains", []):
        if fact.lower() in message.lower():
            problems.append("answer should not mention %r (got %r)" % (fact, message))
    if "answer_equals" in turn and message.strip().rstrip(".") != turn["answer_equals"]:
        problems.append("answer should be exactly %r (got %r)" % (turn["answer_equals"], message))


def parse_csv(data):
    return [row for row in csv.reader(io.StringIO(data.decode("utf-8-sig"), newline="")) if row]


def check_files(case, workspace, before, after, problems):
    """Check created, deleted and kept files, and that nothing unrelated changed."""
    for rel, expected in case.get("expect_files", {}).items():
        actual = after.get(rel)
        if actual is None:
            problems.append("missing file %s" % rel)
        # A single trailing newline difference is tolerated.
        elif actual.decode("utf-8", "replace").rstrip("\n") != expected.rstrip("\n"):
            problems.append("%s contains %r, expected %r" % (rel, actual.decode("utf-8", "replace"), expected))

    for rel, expected_rows in case.get("expect_csv", {}).items():
        actual = after.get(rel)
        if actual is None:
            problems.append("missing CSV file %s" % rel)
            continue
        try:
            rows = parse_csv(actual)
        except (csv.Error, UnicodeDecodeError) as e:
            problems.append("%s is not a valid CSV file: %s" % (rel, e))
            continue
        if rows != expected_rows:
            problems.append("%s has rows %r, expected %r" % (rel, rows, expected_rows))

    for rel in case.get("expect_absent", []):
        if rel in after:
            problems.append("%s should have been deleted" % rel)

    for rel in case.get("expect_dirs", []):
        if not os.path.isdir(os.path.join(workspace, rel)):
            problems.append("directory %s should still exist" % rel)

    allowed = (set(case.get("expect_files", {})) | set(case.get("expect_csv", {}))
               | set(case.get("expect_absent", [])) | set(case.get("may_change", [])))
    if "summary_file" in case:
        allowed.add(case["summary_file"])
    for rel, data in before.items():
        if rel not in allowed and after.get(rel) != data:
            problems.append("unrelated file %s was changed or deleted" % rel)
    for rel in after:
        if rel not in before and rel not in allowed:
            problems.append("unexpected new file %s" % rel)


def check_summary(case, after, problems):
    """Check the summary file exists, is under the word limit, and mentions the key facts."""
    if "summary_file" not in case:
        return
    text = after.get(case["summary_file"], b"").decode("utf-8", "replace")
    words = len(text.split())
    if not text.strip():
        problems.append("summary file %s is missing or empty" % case["summary_file"])
    if "max_words" in case and words >= case["max_words"]:
        problems.append("summary has %d words, limit is fewer than %d" % (words, case["max_words"]))
    for fact in case.get("summary_contains", []):
        if fact.lower() not in text.lower():
            problems.append("summary is missing %r" % fact)
    print("    summary (%d words): %s" % (words, text.strip()))


def case_turns(case):
    """A single-request case is one turn without a session ID."""
    if "turns" in case:
        return case["turns"]
    turn = {key: case[key] for key in ("prompt", "status", "answer_contains", "answer_not_contains", "answer_equals") if key in case}
    return [turn]


def run_case(case, session_dir):
    """Run every turn of one case in a fresh temporary workspace and return a list of problems."""
    workspace = tempfile.mkdtemp(prefix="agent_test_")
    problems = []
    try:
        setup_workspace(workspace, case.get("files", {}), case.get("dirs", []))
        before = snapshot(workspace)
        for number, turn in enumerate(case_turns(case), 1):
            label = "turn %d (session %s)" % (number, turn.get("session", "none"))
            try:
                proc = run_agent(workspace, turn["prompt"], turn.get("session"), session_dir)
            except subprocess.TimeoutExpired:
                problems.append("%s: agent did not finish within 180 seconds (the limit for short tasks)" % label)
                continue
            turn_problems = []
            output = check_output(turn, proc, turn_problems)
            check_answer(turn, output, turn_problems)
            problems.extend("%s: %s" % (label, p) for p in turn_problems)
            if turn_problems and proc.stderr.strip():
                print("    %s stderr (last lines):\n      %s"
                      % (label, "\n      ".join(proc.stderr.strip().splitlines()[-8:])))
        after = snapshot(workspace)
        check_files(case, workspace, before, after, problems)
        check_summary(case, after, problems)
        return problems
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def main():
    name_filter = sys.argv[1] if len(sys.argv) > 1 else ""
    if name_filter == "__probe__":   # development aid: show one raw reply from the endpoint
        sys.exit(subprocess.call([sys.executable, os.path.join(TESTS_DIR, "live", "probe.py")]))
    with open(CASES_FILE, encoding="utf-8") as f:
        cases = json.load(f)
    selected = [c for c in cases if name_filter in c["name"]]

    session_dir = tempfile.mkdtemp(prefix="agent_sessions_")
    failed = 0
    try:
        for case in selected:
            problems = run_case(case, session_dir)
            print("%s  %s" % ("PASS" if not problems else "FAIL", case["name"]))
            for problem in problems:
                print("    - " + problem)
            failed += bool(problems)
    finally:
        shutil.rmtree(session_dir, ignore_errors=True)

    print("\n%d passed, %d failed, %d total" % (len(selected) - failed, failed, len(selected)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
