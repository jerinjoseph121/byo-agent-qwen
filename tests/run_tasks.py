#!/usr/bin/env python3
"""End-to-end tests for the long tasks (programming and debugging).

Each run builds a fresh task workspace (task files, TASK.md and an
unrelated folder), runs the agent once with the task prompt and a session ID, then
scores the result:
  programming: the agent's tally.py is compared with tests/tasks/tally_ref.py on 18 requirements
  debugging:   the agent's REPORT.json is compared with the generator's known answers (18 keys)

Usage (from the project root):
    export OPENAI_BASE_URL=<endpoint> MODEL=Qwen/Qwen3-14B QWEN_API_KEY=<key>
    python3 tests/run_tasks.py programming                 # one programming run, seed 1
    python3 tests/run_tasks.py debugging --seed 3          # one debugging run with other logs
    python3 tests/run_tasks.py all --repeat 3              # 3 runs of each, seeds 1-3
    python3 tests/run_tasks.py programming --keep          # keep the workspace for inspection

Agent logs are saved under $TMPDIR/agent_task_results/.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SUBMISSION_DIR = os.path.dirname(TESTS_DIR)
TASKS_DIR = os.path.join(TESTS_DIR, "tasks")
RESULTS_DIR = os.environ.get("AGENT_RESULTS_DIR") or os.path.join(tempfile.gettempdir(), "agent_task_results")
REF = os.path.join(TASKS_DIR, "tally_ref.py")
TOTAL = 18      # requirements / report keys per task (6 published, 12 written for these tests)


def make_workspace(prompt):
    workspace = tempfile.mkdtemp(prefix="agent_task_")
    with open(os.path.join(workspace, "TASK.md"), "w", encoding="utf-8") as f:
        f.write(prompt)
    os.makedirs(os.path.join(workspace, "unrelated", "empty"))
    with open(os.path.join(workspace, "unrelated", "sentinel.bin"), "wb") as f:
        f.write(b"\x00\x01sentinel\xff")
    return workspace


def run_agent(workspace, prompt, session_id, label):
    env = dict(os.environ)
    env.setdefault("OPENAI_BASE_URL", "http://localhost:8000/v1")
    env.setdefault("MODEL", "Qwen/Qwen3-14B")
    started = time.time()
    try:
        proc = subprocess.run(["bash", os.path.join(SUBMISSION_DIR, "run_agent.sh"), prompt, str(session_id)],
                              cwd=workspace, env=env, capture_output=True, text=True,
                              timeout=int(env.get("AGENT_TIME_BUDGET") or 280) + 50)
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:
        stdout, stderr, code = "", (e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or ""), "timeout"
    elapsed = time.time() - started
    os.makedirs(RESULTS_DIR, exist_ok=True)
    log_path = os.path.join(RESULTS_DIR, "%s_%s.log" % (time.strftime("%Y%m%d-%H%M%S"), label))
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("exit: %s\nstdout: %s\n\n%s" % (code, stdout, stderr))
    calls = re.search(r"after (\d+) model calls", stderr)
    print("  agent finished in %.0fs, exit %s, %s model calls, stdout %s" % (
        elapsed, code, calls.group(1) if calls else "?", stdout.strip()[:200]))
    print("  log: %s" % log_path)
    problems = []
    try:
        output = json.loads(stdout)
        if set(output) != {"status", "message"}:
            problems.append("stdout JSON keys are %s" % sorted(output))
    except ValueError:
        problems.append("stdout is not one JSON object")
    if elapsed > 300:
        problems.append("took %.0fs, over the 300s limit" % elapsed)
    return problems


def check_unrelated(workspace):
    ok = os.path.isdir(os.path.join(workspace, "unrelated", "empty"))
    with open(os.path.join(workspace, "unrelated", "sentinel.bin"), "rb") as f:
        ok = ok and f.read() == b"\x00\x01sentinel\xff"
    return [] if ok else ["unrelated files were changed"]


# ---------------------------------------------------------------- programming
def run_cli(script, args, cwd):
    proc = subprocess.run([sys.executable, script] + args, cwd=cwd, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


def small_files(folder):
    """Hand-made files for edge cases: ties, invalid rows, half-up averages."""
    files = {
        "tie.csv": "date,description,category,amount\n2025-01-02,a,Beta,10.00\n2025-01-03,b,alpha,4.00\n"
                   "2025-01-04,c,ALPHA,6.00\n2025-01-05,d,gamma,3.50\n2025-01-06,e,Delta,-1.00\n",
        "invalid.csv": "date,description,category,amount\n2025-02-01,ok,food,1.00\n2025-02-30,bad date,food,2.00\n"
                       "01/03/2025,bad format,food,4.00\n2025-03-01,bad amount,food,abc\n2025-03-02,no category, ,8.00\n"
                       "2025-03-03,too,many,fields,16.00\n2025-03-04,few,32.00\n"
                       "2025-03-05,\"quoted, comma\",Food,64.00\n 2025-03-06 , spaces , FOOD , 128.00 \n\n",
        "avg.csv": "date,description,category,amount\n2025-04-01,a,x,1.00\n2025-04-02,b,x,1.01\n"
                   "2025-04-03,c,y,2.00\n2025-04-04,d,y,2.01\n2025-04-05,e,y,2.02\n",
        "max.csv": "date,description,category,amount\n2025-05-09,a,Rent,500.00\n2025-05-02,b,food,500\n"
                   "2025-05-02,c,Gifts,500.00\n2025-05-01,d,food,499.99\n2025-05-01,e,food,abc\n",
        "empty.csv": "date,description,category,amount\n2025-02-30,bad,food,1.00\n",
    }
    paths = {}
    for name, text in files.items():
        paths[name] = os.path.join(folder, name)
        with open(paths[name], "w", encoding="utf-8") as f:
            f.write(text)
    return paths


def programming_checks(other, extra):
    """(requirement number, args) pairs. 'other' is a second data file, extra holds small files."""
    t, i, a = extra["tie.csv"], extra["invalid.csv"], extra["avg.csv"]
    m, e = extra["max.csv"], extra["empty.csv"]
    return [
        (1, ["count"]), (1, ["count", "--file", other]),
        (2, ["total"]), (2, ["total", "--file", other]),
        (3, ["total", "--category", "groceries"]), (3, ["total", "--category", "DINING", "--file", other]),
        (3, ["total", "--file", other, "--category", "Rent"]), (3, ["total", "--category", "nosuch"]),
        (4, ["top", "3"]), (4, ["top", "50"]), (4, ["top", "1", "--file", other]), (4, ["top", "4", "--file", t]),
        (5, ["top", "12"]), (5, ["total", "--category", "GrOcErIeS"]), (5, ["top", "10", "--file", t]),
        (6, ["range", "--from", "2025-03-01", "--to", "2025-03-31"]),
        (6, ["range", "--to", "2026-01-31", "--from", "2026-01-01", "--file", other]),
        (6, ["range", "--from", "2025-01-02", "--to", "2025-01-02", "--file", t]),
        (7, ["count", "--file", i]), (7, ["total", "--file", i]), (7, ["top", "5", "--file", i]),
        (8, ["months"]), (8, ["months", "--file", other]),
        (9, ["avg", "--category", "travel"]), (9, ["avg", "--category", "x", "--file", a]),
        (9, ["avg", "--category", "Y", "--file", a]), (9, ["avg", "--category", "nosuch"]),
        (10, ["refunds"]), (10, ["refunds", "--file", other]),
        (11, ["count", "--json"]), (11, ["total", "--json"]),
        (11, ["range", "--json", "--from", "2025-06-01", "--to", "2025-06-30"]),
        (12, ["bogus"]), (12, ["top", "abc"]), (12, ["count", "--file", "/nonexistent/missing.csv"]),
        (13, ["max"]), (13, ["max", "--file", other]), (13, ["max", "--file", m]),
        (14, ["categories"]), (14, ["categories", "--file", t]), (14, ["categories", "--file", i]),
        (15, ["top", "3", "--json"]), (15, ["top", "2", "--file", t, "--json"]),
        (16, ["top", "0"]), (16, ["top", "-3"]), (16, ["range", "--from", "2025-02-30", "--to", "2025-03-01"]),
        (16, ["range", "--from", "2025-03-31", "--to", "2025-03-01"]), (16, ["range", "--from", "2025-03-01"]),
        (17, ["count", "--category", "groceries"]), (17, ["refunds", "--category", "Rent"]),
        (17, ["months", "--category", "TRAVEL", "--file", other]),
        (18, ["count", "--file", e]), (18, ["total", "--file", e]), (18, ["refunds", "--file", e]),
        (18, ["top", "3", "--file", e]), (18, ["months", "--file", e]), (18, ["max", "--file", e]),
        (18, ["categories", "--file", e]),
    ]


def compare(req, mine, ref):
    code, out, err = mine
    rcode, rout, rerr = ref
    if req in (12, 16):
        if code != rcode:
            return "exit code %s, expected %s" % (code, rcode)
        if out.strip():
            return "printed to stdout: %r" % out[:100]
        if not err.strip().startswith("error:"):
            return "stderr does not start with 'error:': %r" % err[:100]
        return None
    if req in (11, 15):
        try:
            if json.loads(out) != json.loads(rout):
                return "JSON %r, expected %r" % (out.strip()[:200], rout.strip())
            return None
        except ValueError:
            return "not JSON: %r (expected %r)" % (out.strip()[:200], rout.strip())
    if code != 0:
        return "exit code %s, stderr %r" % (code, err.strip()[-300:])
    if out != rout:
        return "output %r, expected %r" % (out[:300], rout[:300])
    return None


def short_task(text):
    """The task with only requirements 1-12 (the original size of the task)."""
    start = text.index("\n13. ")
    end = text.index("When you are done")
    return (text[:start + 1] + "\n" + text[end:]).replace("each of the 18 requirements",
                                                            "each of the 12 requirements")


def programming(seed, session_id, keep):
    with open(os.path.join(TASKS_DIR, "tally_task.md"), encoding="utf-8") as f:
        prompt = f.read()
    # Seeds of 100 and more test the 12-requirement version of the task (data seed = seed - 100).
    limit = 18
    if seed >= 100:
        seed, limit, prompt = seed - 100, 12, short_task(prompt)
    workspace = make_workspace(prompt)
    subprocess.run([sys.executable, os.path.join(TASKS_DIR, "gen_transactions.py"),
                    os.path.join(workspace, "transactions.csv"), "--seed", str(seed)], check=True)
    print("programming seed %d, workspace %s" % (seed, workspace))
    problems = run_agent(workspace, prompt, session_id, "programming_seed%d" % seed)
    problems += check_unrelated(workspace)
    score, failures = 0, {}
    script = os.path.join(workspace, "tally.py")
    if not os.path.isfile(script):
        problems.append("tally.py was not created")
    else:
        # Keep a copy of the agent's program next to the logs for inspection.
        os.makedirs(RESULTS_DIR, exist_ok=True)
        shutil.copy(script, os.path.join(RESULTS_DIR, "%s_programming_seed%d_tally.py"
                                          % (time.strftime("%Y%m%d-%H%M%S"), seed)))
        extra_dir = tempfile.mkdtemp(prefix="agent_task_data_")
        other = os.path.join(extra_dir, "other.csv")
        subprocess.run([sys.executable, os.path.join(TASKS_DIR, "gen_transactions.py"), other,
                        "--seed", str(seed + 100), "--rows", "3000"], check=True)
        extra = small_files(extra_dir)
        for req, args in programming_checks(other, extra):
            if req > limit:
                continue
            try:
                mine = run_cli(script, args, workspace)
            except subprocess.TimeoutExpired:
                mine = ("timeout", "", "")
            error = compare(req, mine, run_cli(REF, args, workspace))
            if error and req not in failures:
                failures[req] = "%s: %s" % (" ".join(args), error)
        shutil.rmtree(extra_dir, ignore_errors=True)
        score = limit - len(failures)
    print("  programming score: %d/%d" % (score, limit))
    for req in sorted(failures):
        print("    requirement %d FAILED  %s" % (req, failures[req]))
    for p in problems:
        print("    PROBLEM: %s" % p)
    if not keep:
        shutil.rmtree(workspace, ignore_errors=True)
    return score


# ---------------------------------------------------------------- debugging
def debugging(seed, session_id, keep):
    order = "ORD-77341" if seed == 1 else "ORD-%05d" % (70000 + seed * 137)
    with open(os.path.join(TASKS_DIR, "debug_task.md"), encoding="utf-8") as f:
        prompt = f.read().replace("{ORDER}", order)
    workspace = make_workspace(prompt)
    generated = subprocess.run([sys.executable, os.path.join(TASKS_DIR, "gen_logs.py"), workspace,
                                "--seed", str(seed), "--order", order], check=True, capture_output=True, text=True)
    expected = json.loads(generated.stdout)
    print("debugging seed %d (%s), workspace %s" % (seed, order, workspace))
    problems = run_agent(workspace, prompt, session_id, "debugging_seed%d" % seed)
    problems += check_unrelated(workspace)
    report_path = os.path.join(workspace, "REPORT.json")
    report = {}
    if not os.path.isfile(report_path):
        problems.append("REPORT.json was not created")
    else:
        try:
            with open(report_path, encoding="utf-8") as f:
                report = json.load(f)
            if not isinstance(report, dict):
                problems.append("REPORT.json is not a JSON object")
                report = {}
        except ValueError as e:
            problems.append("REPORT.json is not valid JSON: %s" % e)
    extra_keys = sorted(set(report) - set(expected))
    if extra_keys:
        problems.append("REPORT.json has unexpected keys: %s" % ", ".join(extra_keys))
    score = 0
    for key, value in expected.items():
        got = report.get(key, "<missing>")
        if got == value and type(got) is type(value):
            score += 1
        else:
            print("    %-28s FAILED  got %r, expected %r" % (key, got, value))
    print("  debugging score: %d/%d" % (score, len(expected)))
    for p in problems:
        print("    PROBLEM: %s" % p)
    if not keep:
        shutil.rmtree(workspace, ignore_errors=True)
    return score


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["programming", "debugging", "all"])
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    kinds = ["programming", "debugging"] if args.kind == "all" else [args.kind]
    scores = {k: [] for k in kinds}
    for n in range(args.repeat):
        seed = args.seed + n
        for kind in kinds:
            session_id = 9000 + seed * 10 + kinds.index(kind)
            fn = programming if kind == "programming" else debugging
            scores[kind].append(fn(seed, session_id, args.keep))
    print("\nSUMMARY")
    for kind, values in scores.items():
        print("  %-12s %s  (average %.1f/%d)" % (kind, " ".join("%d/%d" % (v, TOTAL) for v in values),
                                                   sum(values) / float(len(values)), TOTAL))


if __name__ == "__main__":
    main()
