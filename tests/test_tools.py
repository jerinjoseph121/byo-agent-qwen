#!/usr/bin/env python3
"""Offline unit tests for the agent's tools, parsing, context trimming and sessions. No model or key needed.

Usage (from the project root):
    python3 tests/test_tools.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True   # keep __pycache__ out of the project folder
SUBMISSION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SUBMISSION_DIR)

from byo_agent import commands, config, csvtools, files, loop, session, tools  # noqa: E402


class Base(unittest.TestCase):
    session_id = ""

    def setUp(self):
        self.workspace = os.path.realpath(tempfile.mkdtemp(prefix="agent_ws_"))
        self.session_dir = tempfile.mkdtemp(prefix="agent_sessions_")
        os.environ["AGENT_SESSION_DIR"] = self.session_dir
        config.init(["agent.py", "test prompt", self.session_id], cwd=self.workspace)
        self.write("sales.csv", "month,revenue\nJanuary,120\nFebruary,180\n")
        self.write("orders.csv", "id,customer,status,amount\n1,Ana,shipped,20\n2,Ben,cancelled,35\n"
                                 "3,Cal,Cancelled,10\n4,Dee,pending,\"1,200.50\"\n")

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)
        shutil.rmtree(self.session_dir, ignore_errors=True)

    def write(self, rel, text):
        path = os.path.join(self.workspace, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def read(self, rel):
        with open(os.path.join(self.workspace, rel), encoding="utf-8", newline="") as f:
            return f.read()


class CsvToolTests(Base):
    def test_column_stats(self):
        result = csvtools.csv_column_stats("sales.csv", "revenue")
        self.assertIn("total=300", result)
        self.assertIn("average=150", result)

    def test_column_stats_with_formatted_numbers(self):
        self.assertIn("total=1265.5", csvtools.csv_column_stats("orders.csv", "Amount"))

    def test_row_sum(self):
        self.write("candidates.csv", "id,name,interview_score,test_score\n1,Ana,8.5,9\n2,Ben,7,6\n")
        result = csvtools.csv_row_sum("candidates.csv", "id", "1", ["interview_score", "test_score"])
        self.assertIn("sum=17.5", result)

    def test_update_value_saves_to_new_file(self):
        self.assertIn("Updated 1 row", csvtools.csv_update_value("orders.csv", "id", "2", "status", "complete", "updated.csv"))
        self.assertEqual(self.read("updated.csv"),
                         "id,customer,status,amount\n1,Ana,shipped,20\n2,Ben,complete,35\n"
                         "3,Cal,Cancelled,10\n4,Dee,pending,\"1,200.50\"\n")
        self.assertIn("2,Ben,cancelled,35", self.read("orders.csv"))

    def test_add_to_value(self):
        self.assertIn("20 -> 279", csvtools.csv_add_to_value("orders.csv", "customer", "ana", "amount", "259"))
        self.assertIn("1,Ana,shipped,279", self.read("orders.csv"))

    def test_update_value_missing_row(self):
        self.assertTrue(csvtools.csv_update_value("orders.csv", "id", "99", "status", "x", "out.csv").startswith("ERROR"))
        self.assertFalse(os.path.exists(os.path.join(self.workspace, "out.csv")))

    def test_delete_rows_case_insensitive(self):
        self.assertIn("Removed 2 row", csvtools.csv_delete_rows("orders.csv", "status", "equals", "cancelled", "active.csv"))
        self.assertEqual(self.read("active.csv"),
                         "id,customer,status,amount\n1,Ana,shipped,20\n4,Dee,pending,\"1,200.50\"\n")

    def test_delete_rows_numeric_condition(self):
        csvtools.csv_delete_rows("orders.csv", "amount", "less_than", "25", "big.csv")
        self.assertEqual(self.read("big.csv"), "id,customer,status,amount\n2,Ben,cancelled,35\n4,Dee,pending,\"1,200.50\"\n")

    def test_crlf_line_endings_preserved(self):
        self.write("crlf.csv", "id,v\r\n1,a\r\n2,b\r\n")
        csvtools.csv_update_value("crlf.csv", "id", "1", "v", "z")
        self.assertEqual(self.read("crlf.csv"), "id,v\r\n1,z\r\n2,b\r\n")

    def test_calculate(self):
        self.assertIn("= 150", csvtools.calculate("(120 + 180) / 2"))
        self.assertIn("= 0.3", csvtools.calculate("0.1 + 0.2"))


class FileToolTests(Base):
    def test_copy_file_is_exact(self):
        files.copy_file("sales.csv", "reports/sales_copy.csv")
        self.assertEqual(self.read("reports/sales_copy.csv"), self.read("sales.csv"))

    def test_path_outside_workspace_rejected(self):
        self.assertTrue(tools.run_tool("write_file", {"path": "../escape.txt", "content": "x"}).startswith("ERROR"))

    def test_list_files_shows_sizes(self):
        self.assertIn("sales.csv  (3 lines", files.list_files("."))

    def test_read_small_file_whole(self):
        self.assertEqual(files.read_file("sales.csv"), "month,revenue\nJanuary,120\nFebruary,180\n")

    def test_read_large_file_in_parts(self):
        self.write("big.log", "".join("line %d some text here\n" % i for i in range(1, 5001)))
        first = files.read_file("big.log")
        self.assertIn("has 5000 lines", first)
        self.assertLess(len(first), config.PART_READ_LIMIT + 500)
        part = files.read_file("big.log", 4000, 4002)
        self.assertIn("Showing lines 4000-4002", part)
        self.assertIn("line 4001 some text", part)

    def test_search_files(self):
        self.write("logs/a.log", "start\nreq=gw-1 order=ORD-1 failed\nnext line\nreq=gw-2 order=ORD-2 ok\n")
        self.write("logs/b.log", "trace=tr-9 order=ord-1 waiting\n")
        result = files.search_files("ORD-1", "logs")
        self.assertIn("logs/a.log:2: req=gw-1", result)
        self.assertIn("logs/b.log:1: trace=tr-9", result)          # case-insensitive by default
        with_context = files.search_files("gw-1", "logs/a.log", context=1)
        self.assertIn("context logs/a.log:1: start", with_context)
        self.assertIn("MATCH   logs/a.log:2: req=gw-1", with_context)
        self.assertIn("context logs/a.log:3: next line", with_context)
        # a match inside another match's context is still marked as a match
        self.assertIn("MATCH   logs/a.log:4: req=gw-2", files.search_files("req=gw", "logs/a.log", context=2))
        self.write("logs/many.log", "".join("order %d\n" % i for i in range(30)) + "order 99 ERROR stuck\n")
        many = files.search_files("order", "logs/many.log", max_results=1)
        self.assertIn("16 more matches", many)        # at least 15 are shown
        self.assertIn("errors or warnings:\nlogs/many.log:31: order 99 ERROR stuck", many)
        self.assertIn("gw-2", files.search_files(r"gw-\d ord.*ok", "logs", regex=True))
        self.assertTrue(files.search_files("(", "logs", regex=True).startswith("ERROR"))
        self.assertIn("No matches", files.search_files("zzz", "logs"))

    def test_write_file_checks_python_and_json(self):
        self.assertIn("syntax OK", files.write_file("ok.py", "print(1)\n"))
        self.assertIn("syntax error at line 1", files.write_file("bad.py", "def (:\n"))
        self.assertIn("2 keys: a, b", files.write_file("r.json", '{"a": 1, "b": "x"}'))
        self.assertIn("not valid JSON", files.write_file("bad.json", "{a: 1}"))

    def test_word_limit_only_for_text_files(self):
        config.init(["agent.py", "Summarize in under 5 words to s.txt", ""], cwd=self.workspace)
        self.assertIn("NOT under", files.write_file("s.txt", "one two three four five six"))
        self.assertNotIn("words", files.write_file("p.py", "x = 'one two three four five six'\n"))

    def test_append_and_edit(self):
        files.write_file("p.py", "a = 1\n")
        self.assertIn("now 2 lines", files.append_file("p.py", "b = 2\n"))
        self.assertIn("Replaced 1", files.edit_file("p.py", "b = 2", "b = 3"))
        self.assertEqual(self.read("p.py"), "a = 1\nb = 3\n")
        self.assertTrue(files.edit_file("p.py", "zzz", "y").startswith("ERROR"))
        files.write_file("q.py", "x\nx\n")
        self.assertIn("occurs 2 times", files.edit_file("q.py", "x", "y"))
        self.assertIn("Replaced 2", files.edit_file("q.py", "x", "y", replace_all=True))

    def test_edit_ignores_indentation_and_line_numbers(self):
        files.write_file("t.py", "def f():\n    if x:\n        print(f\"{{\"a\": 1}}\")\n    return 1\n")
        result = files.edit_file("t.py", "if x:\nprint(f\"{{\"a\": 1}}\")", "if x:\n    print(json.dumps({'a': 1}))")
        self.assertIn("matched ignoring indentation", result)
        self.assertEqual(self.read("t.py"), "def f():\n    if x:\n        print(json.dumps({'a': 1}))\n    return 1\n")
        result = files.edit_file("t.py", "     4| return 1", "     4| return 2")
        self.assertIn("Replaced", result)
        self.assertIn("return 2", self.read("t.py"))

    def test_edit_not_found_suggests_similar_line(self):
        files.write_file("t.py", "alpha = 1\nbeta = 2\n")
        self.assertIn("most similar line is line 2", files.edit_file("t.py", "beta = 3", "beta = 4"))

    def test_replace_lines(self):
        files.write_file("t.py", "a\nb\nc\nd\n")
        self.assertIn("Replaced lines 2-3", files.replace_lines("t.py", 2, 3, "B\nC\nC2"))
        self.assertEqual(self.read("t.py"), "a\nB\nC\nC2\nd\n")
        files.replace_lines("t.py", 1, 1, "")
        self.assertEqual(self.read("t.py"), "B\nC\nC2\nd\n")
        self.assertTrue(files.replace_lines("t.py", 3, 9, "x").startswith("ERROR"))

    def test_replace_lines_restores_indentation(self):
        files.write_file("t.py", "def f():\n    try:\n        x = 1\n    except ValueError:\n        pass\n")
        result = files.replace_lines("t.py", 3, 3, "x = 2\ny = 3")
        self.assertIn("syntax OK", result)
        self.assertEqual(self.read("t.py"), "def f():\n    try:\n        x = 2\n        y = 3\n    except ValueError:\n        pass\n")

    def test_edit_that_breaks_python_is_refused(self):
        good = "def f():\n    try:\n        x = 1\n    except ValueError:\n        pass\n"
        files.write_file("t.py", good)
        result = files.replace_lines("t.py", 2, 2, "    try:\n        y = 2\n    z = 3")
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("NOT applied", result)
        self.assertEqual(self.read("t.py"), good)
        self.assertTrue(files.edit_file("t.py", "x = 1", "x = (1").startswith("ERROR"))
        self.assertEqual(self.read("t.py"), good)
        # An already broken file may still be edited step by step.
        files.write_file("b.py", "if True:\nprint(1)\n")
        self.assertIn("Replaced", files.edit_file("b.py", "if True:", "if (True:"))

    def test_edit_indentation_is_fixed_when_it_breaks_python(self):
        files.write_file("t.py", "def f(x):\n    if x:\n        return 1\n    return 2\n")
        # first line given without indentation, the rest indented like the file
        files.replace_lines("t.py", 2, 3, "if x > 1:\n        return 3")
        self.assertEqual(self.read("t.py"), "def f(x):\n    if x > 1:\n        return 3\n    return 2\n")
        result = files.edit_file("t.py", "    if x > 1:\n        return 3", "if x > 2:\n        return 4")
        self.assertIn("adjusted", result)
        self.assertEqual(self.read("t.py"), "def f(x):\n    if x > 2:\n        return 4\n    return 2\n")
        self.assertIn("nothing changed", files.write_file("t.py", self.read("t.py")))

    def test_append_of_whole_file_replaces_it(self):
        files.write_file("p.py", "import a\nimport b\nx = 1\n")
        result = files.append_file("p.py", "import a\nimport b\nx = 1\ny = 2\n")
        self.assertIn("replaced the file", result)
        self.assertEqual(self.read("p.py"), "import a\nimport b\nx = 1\ny = 2\n")
        files.append_file("p.py", "z = 3\n")
        self.assertEqual(self.read("p.py"), "import a\nimport b\nx = 1\ny = 2\nz = 3\n")

    def test_syntax_error_shows_context(self):
        result = files.write_file("bad.py", "a = 1\nif a:\nprint(a)\n")
        self.assertIn("     2| if a:", result)

    def test_numbered_partial_read(self):
        self.write("n.txt", "one\ntwo\nthree\n")
        self.assertIn("     2| two", files.read_file("n.txt", 2, 3))

    def test_tool_aliases_and_missing_arguments(self):
        self.assertIn("Copied", tools.run_tool("copy_file", {"src": "sales.csv", "dest": "copy.csv"}))
        self.assertIn("missing argument", tools.run_tool("write_file", {"path": "x.txt"}))
        self.assertIn("unknown tool", tools.run_tool("explode", {}))


class CommandTests(Base):
    def test_run_command_output_and_exit_code(self):
        result = commands.run_command("echo hello; exit 3")
        self.assertIn("[exit code 3]", result)
        self.assertIn("hello", result)

    def test_run_command_runs_in_workspace(self):
        self.assertIn("sales.csv", commands.run_command("ls"))

    def test_run_command_timeout(self):
        self.assertIn("timed out", commands.run_command("sleep 5", timeout=1))

    def test_run_command_hides_api_key(self):
        os.environ["QWEN_API_KEY"] = "secret-test-key"
        try:
            self.assertNotIn("secret-test-key", commands.run_command("env"))
        finally:
            del os.environ["QWEN_API_KEY"]

    def test_run_command_blocks_deleting_workspace_files(self):
        self.assertTrue(commands.run_command("rm sales.csv").startswith("ERROR"))
        self.assertTrue(commands.run_command("echo hi && rm -rf .").startswith("ERROR"))
        self.assertTrue(os.path.exists(os.path.join(self.workspace, "sales.csv")))
        self.assertIn("exit code 0", commands.run_command("touch /tmp/agent_unit_x && rm /tmp/agent_unit_x"))

    def test_error_hints(self):
        result = commands.run_command("python3 -c \"import decimal; decimal.Decimal('x1')\"")
        self.assertIn("HINT: decimal.Decimal", result)
        self.assertIn("HINT: The traceback", result)
        self.assertNotIn("HINT", commands.run_command("echo ok"))

    def test_long_output_is_shortened(self):
        result = commands.run_command("python3 -c \"print('x' * 20000)\"")
        self.assertIn("characters omitted", result)
        self.assertLess(len(result), 10000)


class BudgetTests(Base):
    def test_time_budget_depends_on_task_size(self):
        config.init(["agent.py", "Create notes.txt"], cwd=self.workspace)
        self.assertEqual(config.TIME_BUDGET, 165)           # short task: 180-second limit
        self.write("TASK.md", "task")
        config.init(["agent.py", "Implement tally.py"], cwd=self.workspace)
        self.assertEqual(config.TIME_BUDGET, 280)           # long task: 300-second limit
        os.remove(os.path.join(self.workspace, "TASK.md"))
        config.init(["agent.py", "x" * 2000], cwd=self.workspace)
        self.assertEqual(config.TIME_BUDGET, 280)


class ReviewTests(Base):
    def test_test_runs_are_tracked(self):
        runs = loop.TestRuns()
        runs.record({"command": "python3 t.py total --x a"}, "[exit code 2]\nerror: bad\nHINT: x")
        runs.record({"command": "ls"}, "[exit code 1]\nnothing")
        self.assertEqual(runs.failing, {"python3 t.py total --x a": "exit code 2: error: bad"})
        runs.record({"command": "python3 t.py total --x a"}, "[exit code 0]\n5")
        self.assertEqual(runs.failing, {})
        runs.record({"command": "python3 t.py total"}, "[exit code 0]\n5")
        runs.record({"command": "python3 t.py range --from a"}, "[exit code 0]\ncount=0 total=0.00")
        notes = runs.suspicious()
        self.assertEqual(len(notes), 2)
        self.assertIn("same output", " ".join(notes))
        self.assertIn("zero", " ".join(notes))
        self.assertIn("still unchanged", runs.suspicious()[0])     # reported again, more firmly

    def test_review_finish_only_for_long_tasks(self):
        runs = loop.TestRuns()
        config.PROMPT = "short task"
        self.assertIsNone(loop.review_finish("error", 0, runs))
        config.PROMPT = "x" * 2000
        self.assertIn("Do not give up", loop.review_finish("error", 0, runs))
        self.assertIn("Not finished yet", loop.review_finish("success", 0, runs))
        self.assertIsNone(loop.review_finish("success", 1, runs))
        runs.failing["python3 t.py"] = "exit code 2: e"
        self.assertIn("python3 t.py", loop.review_finish("success", 1, runs))
        self.assertIsNone(loop.review_finish("error", 2, runs))
        self.assertIsNone(loop.review_finish("success", 6, runs))

    def test_unverified_report_values(self):
        self.write("logs/a.log", "req=gw-1 status=502 worker=inv-w3\n")
        self.write("REPORT.json", json.dumps({"request": "gw-1", "status": 502, "worker": "inv-w9", "n": 0}))
        config.PROMPT = "Write REPORT.json." + " " * 1500
        self.assertEqual(loop.unverified_values(), (['worker="inv-w9"', 'n=0'], []))
        guessed, unseen = loop.unverified_values(["MATCH   logs/b.log:1: something else"])
        self.assertEqual(unseen, ['request="gw-1"', "status=502"])
        runs = loop.TestRuns()
        runs.add_evidence("search_files", {"pattern": "gw"}, "MATCH   logs/a.log:1: req=gw-1 status=502\n"
                                                             "MATCH   REPORT.json:2: worker inv-w9")
        runs.add_evidence("read_file", {"path": "REPORT.json"}, '{"worker": "inv-w9"}')
        review = loop.review_finish("success", 1, runs)
        self.assertIn("inv-w9", review)
        self.assertNotIn("You have not seen", review)
        self.assertNotIn("inv-w9", "\n".join(runs.evidence))

    def test_diagnoses(self):
        runs = loop.TestRuns()
        runs.record({"command": "python3 t.py total --category food"}, "[exit code 0]\n5.00")
        runs.record({"command": "python3 t.py total --category Food"}, "[exit code 0]\n0.00")
        runs.record({"command": "python3 t.py avg Food"}, "[exit code 0]\n1.00")
        runs.record({"command": "python3 t.py avg --category Food"}, "[exit code 2]\nerror: missing argument")
        notes = runs.diagnose()
        self.assertEqual(len(notes), 3)
        self.assertIn("case-sensitively", notes[0])
        self.assertIn('options.get("--category")', notes[1])
        self.assertIn("something is missing", notes[2])
        self.assertEqual(len(runs.new_diagnoses()), 3)
        self.assertEqual(runs.new_diagnoses(), [])
        runs.record({"command": "python3 t.py avg --category Food"}, "[exit code 0]\n1.00")
        self.assertEqual(len(runs.diagnose()), 1)

    def test_report_saved_from_finish_message(self):
        config.PROMPT = "Write REPORT.json with keys a, b, c." + " " * 1500
        loop.save_report_from_message('Done: {"a": 1, "b": "x", "c": 3}')
        self.assertEqual(json.loads(self.read("REPORT.json")), {"a": 1, "b": "x", "c": 3})
        loop.save_report_from_message('```json\n{"a": 2, "b": "y"}\n```')
        self.assertEqual(json.loads(self.read("REPORT.json")), {"a": 2, "b": "y", "c": 3})
        loop.save_report_from_message('{"other": 1, "thing": 2}')       # unrelated JSON is ignored
        self.assertEqual(json.loads(self.read("REPORT.json")), {"a": 2, "b": "y", "c": 3})

    def test_case_check_reruns_with_capitals(self):
        self.write("t.py", "import sys\nprint(1 if sys.argv[-1] == 'food' else 0)\n")
        config.PROMPT = "Names are case-insensitive." + " " * 1500
        runs = loop.TestRuns()
        runs.record({"command": "python3 t.py total --category food"}, "[exit code 0]\n1")
        notes = loop.case_check(runs)
        self.assertEqual(len(notes), 1)
        self.assertIn("FOOD", notes[0])
        self.assertEqual(loop.case_check(runs), [])                  # not rerun while nothing changed
        self.assertTrue(any("case-sensitively" in n for n in runs.diagnose()))   # still reported at review
        config.PROMPT = "No such rule." + " " * 1500
        self.assertEqual(loop.case_check(loop.TestRuns()), [])

    def test_untested_spec_commands(self):
        config.PROMPT = ("Run `python3 tally.py total --category NAME` or `python3 tally.py top N` "
                         "and `python3 tally.py avg --category NAME`." + " " * 1500)
        runs = loop.TestRuns()
        runs.record({"command": "python3 tally.py total --category food"}, "[exit code 0]\n1.00")
        runs.record({"command": "python3 tally.py avg food"}, "[exit code 0]\n1.00")
        self.assertEqual(runs.untested(), ["python3 tally.py top N", "python3 tally.py avg --category NAME"])
        self.assertIn("top N", loop.review_finish("success", 1, runs))

    def test_final_state_checked_against_last_line(self):
        self.write("logs/p.log", "intent=pi_123 created\nintent=pi_123 status=requires_capture\n"
                                  "intent=pi_2 status=ok\nintent=pi_123 canceled reason=aborted\n")
        self.write("REPORT.json", json.dumps({"payment_intent_id": "pi_123",
                                              "payment_intent_final_status": "requires_capture"}))
        config.PROMPT = "Write REPORT.json." + " " * 1500
        guessed, unseen = loop.unverified_values(["intent=pi_123 status=requires_capture"])
        self.assertEqual(len(unseen), 1)
        self.assertIn("canceled reason=aborted", unseen[0])

    def test_rerun_finds_regressions(self):
        self.write("t.py", "print(5)\n")
        runs = loop.TestRuns()
        runs.record({"command": "python3 t.py total"}, "[exit code 0]\n4")
        runs.model_commands.add("python3 t.py total")
        notes = loop.rerun_passing(runs)
        self.assertEqual(len(notes), 1)
        self.assertIn("now prints 5", notes[0])
        self.assertEqual(loop.rerun_passing(runs), [])

    def test_event_timestamp_checked(self):
        self.write("logs/i.log", "2026-09-17T14:31:44.490Z INFO worker=w1 picked job hold=h1\n"
                                  "2026-09-17T14:31:44.990Z ERROR worker=w1 job=h1 stuck in X.run, worker stalled\n")
        config.PROMPT = "Write REPORT.json." + " " * 1500
        self.write("REPORT.json", json.dumps({"stall_started_at": "2026-09-17T14:31:44.490Z"}))
        guessed, unseen = loop.unverified_values(["2026-09-17T14:31:44.490Z"])
        self.assertEqual(len(unseen), 1)
        self.assertIn("worker stalled", unseen[0])
        self.write("REPORT.json", json.dumps({"stall_started_at": "2026-09-17T14:31:44.990Z"}))
        self.assertEqual(loop.unverified_values(["2026-09-17T14:31:44.990Z"]), ([], []))

    def test_unsearched_report_ids(self):
        config.PROMPT = "Write REPORT.json." + " " * 1500
        self.write("REPORT.json", json.dumps({"worker": "inv-w1", "hold_id": "hold-123", "cause": "Foo",
                                              "at": "2026-09-17T14:31:55.367Z", "code": 502}))
        self.assertEqual(loop.unsearched_ids(["hold-123"]), ["inv-w1 (worker)"])
        self.assertEqual(loop.unsearched_ids(["hold-123", "worker=inv-w1"]), [])

    def test_output_format_check(self):
        config.PROMPT = ("Run `python3 t.py top N`.\n1. **`months`** prints `YYYY-MM<TAB>AMOUNT`.\n"
                         "2. **`refunds`** prints `count=K total=X.XX`.\n" + " " * 1500)
        runs = loop.TestRuns()
        runs.record({"command": "python3 t.py months"}, "[exit code 0]\n2025-01-01\t3.00")
        runs.record({"command": "python3 t.py refunds"}, "[exit code 0]\ncount=2 total=-4.50")
        notes = loop.format_problems(runs)
        self.assertEqual(len(notes), 1)
        self.assertIn("YYYY-MM<TAB>AMOUNT", notes[0])

    def test_broken_late_edit_is_undone(self):
        good, bad = "print(1)\n", "print(1/0)\n"
        config.PROMPT = "Write t.py." + " " * 1500
        runs = loop.TestRuns()
        self.write("t.py", good)
        loop.remember_version(runs, {"path": "t.py"})
        runs.record({"command": "python3 t.py count"}, "[exit code 0]\n1")
        runs.ever_passed.append("python3 t.py count")
        self.write("t.py", bad)
        loop.remember_version(runs, {"path": "t.py"})
        loop.keep_best_version(runs)
        self.assertEqual(self.read("t.py"), good)
        # a late edit that silently changes a checked result is undone too
        self.write("t.py", "print(0)\n")
        loop.remember_version(runs, {"path": "t.py"})
        loop.keep_best_version(runs)
        self.assertEqual(self.read("t.py"), good)
        # a change whose new result the model has seen is kept
        self.write("t.py", "print(2)\n")
        loop.remember_version(runs, {"path": "t.py"})
        runs.record({"command": "python3 t.py count"}, "[exit code 0]\n2")
        loop.keep_best_version(runs)
        self.assertEqual(self.read("t.py"), "print(2)\n")

    def test_half_up_rule_checked_in_code(self):
        config.PROMPT = "Round half up." + " " * 1500
        runs = loop.TestRuns()
        self.write("t.py", "print('%.2f' % 1)\n")
        loop.remember_version(runs, {"path": "t.py"})
        self.assertIn("ROUND_HALF_UP", loop.source_problems(runs)[0])
        self.write("t.py", "from decimal import ROUND_HALF_UP\n")
        self.assertEqual(loop.source_problems(runs), [])

    def test_best_earlier_version_is_chosen(self):
        config.PROMPT = "Write t.py." + " " * 1500
        runs = loop.TestRuns()
        for name in ("a", "b"):
            runs.record({"command": "python3 t.py %s" % name}, "[exit code 0]\nok")
            runs.ever_passed.append("python3 t.py %s" % name)
        good = "print('ok')\n"
        half = "import sys\nprint('ok') if sys.argv[1] == 'a' else 1/0\n"
        bad = "1/0\n"
        for version in (good, half, bad):
            self.write("t.py", version)
            loop.remember_version(runs, {"path": "t.py"})
        loop.keep_best_version(runs)
        self.assertEqual(self.read("t.py"), good)     # not just the newest version that is better

    def test_missing_output_files(self):
        config.PROMPT = ("Read sales.csv and logs/app.log (or /nonexistent/x.csv), then write REPORT.json "
                         "and out/tally.py." + " " * 1500)
        self.assertEqual(loop.missing_outputs(), ["logs/app.log", "REPORT.json", "out/tally.py"])
        self.assertIn("REPORT.json", loop.review_finish("success", 1, loop.TestRuns()))


class ParsingTests(Base):
    def test_cut_off_write_is_salvaged(self):
        text = '<tool_call>\n{"name": "write_file", "arguments": {"path": "t.py", "content": "import sys\\nx = \\"a\\"\\ny = 1'
        self.assertEqual(loop.salvage_cut_off(text), ("write_file", "t.py", 'import sys\nx = "a"\n', False))
        broken = ('<tool_call>\n{"name": "write_file", "arguments": {"path": "t.py", "content": '
                  '"print("hi")\\nx = re.sub(r\'\\d\', y)\\n"}}\n</tool_call>')
        self.assertEqual(loop.salvage_cut_off(broken),
                         ("write_file", "t.py", 'print("hi")\nx = re.sub(r\'\\d\', y)\n', True))
        self.assertIsNone(loop.salvage_cut_off("no tool call here"))
        extra = '{"name": "write_file", "arguments": {"path": "t.py", "content": "a = 1\\nmain()"}}}'
        self.assertEqual(loop.salvage_cut_off(extra), ("write_file", "t.py", "a = 1\nmain()", True))

    def test_standard_tool_calls(self):
        message = {"tool_calls": [{"id": "a", "function": {"name": "read_file", "arguments": '{"path": "x"}'}}]}
        self.assertEqual(loop.parse_tool_calls(message), [("a", "read_file", {"path": "x"})])

    def test_arguments_with_raw_newlines(self):
        message = {"tool_calls": [{"id": "a", "function": {"name": "write_file",
                                                           "arguments": '{"path": "t.py", "content": "a = 1\nb = 2"}'}}]}
        self.assertEqual(loop.parse_tool_calls(message)[0][2]["content"], "a = 1\nb = 2")

    def test_tool_call_tags_in_text(self):
        message = {"content": '<think>\n</think>\n<tool_call>\n{"name": "finish", "arguments": {"status": "success", "message": "ok"}}\n</tool_call>'}
        self.assertEqual(loop.parse_tool_calls(message)[0][1:], ("finish", {"status": "success", "message": "ok"}))

    def test_bare_json_tool_call(self):
        message = {"content": '{"name": "finish", "arguments": {"status": "success", "message": "42"}}'}
        self.assertEqual(loop.parse_tool_calls(message)[0][2]["message"], "42")

    def test_plain_text_is_not_a_tool_call(self):
        self.assertEqual(loop.parse_tool_calls({"content": "The answer is 42."}), [])


class ContextTests(Base):
    def test_old_tool_output_is_trimmed_first(self):
        steps = []
        for i in range(8):
            steps.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": "c%d" % i, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
            steps.append({"role": "tool", "tool_call_id": "c%d" % i, "content": ("result %d " % i) * 2000})
        base = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        estimate = loop.fit_context(base, steps, 15000)
        self.assertLessEqual(estimate, 15000)
        self.assertTrue(steps[1]["content"].startswith(loop.TRIMMED_PREFIX))      # oldest trimmed
        self.assertFalse(steps[-1]["content"].startswith(loop.TRIMMED_PREFIX))    # newest kept

    def test_long_write_arguments_are_shortened(self):
        code = "x = 1\n" * 3000
        steps = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "w", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"path": "p.py", "content": code})}}]},
            {"role": "tool", "tool_call_id": "w", "content": "Wrote."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "r", "type": "function", "function": {
                "name": "run_command", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "r", "content": "ok"},
        ]
        loop.fit_context([{"role": "system", "content": "s"}], steps, 1000)
        self.assertIn("more characters omitted", steps[0]["tool_calls"][0]["function"]["arguments"])
        json.loads(steps[0]["tool_calls"][0]["function"]["arguments"])            # still valid JSON

    def test_context_error_parsing(self):
        from byo_agent import llm
        body = ('{"object":"error","message":"This model\'s maximum context length is 40960 tokens. However, '
                'you requested 45000 tokens (37000 in the messages, 8000 in the completion)."}')
        self.assertEqual(llm.context_error(body), (True, 37000))
        self.assertEqual(llm.context_error('{"message": "invalid tool"}'), (False, None))


class SessionTests(Base):
    def test_sessions_are_saved_and_isolated(self):
        session.Session("1001").add_turn("My drink is tea. Remember that.", [], "success", "Noted: tea.")
        again = session.Session("1001")
        self.assertEqual(len(again.turns), 1)
        self.assertIn("Noted: tea.", str(again.history_messages()))
        self.assertEqual(session.Session("2002").turns, [])

    def test_same_session_id_in_new_workspace_starts_fresh(self):
        session.Session("1001").add_turn("hi", [], "success", "ok")
        other = os.path.realpath(tempfile.mkdtemp(prefix="agent_ws_"))
        try:
            config.init(["agent.py", "p", "1001"], cwd=other)
            self.assertEqual(session.Session("1001").turns, [])
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_no_session_id_is_stateless(self):
        s = session.Session("")
        s.add_turn("hi", [], "success", "ok")
        self.assertIsNone(s.path)
        self.assertEqual(os.listdir(self.session_dir), [])

    def test_saved_steps_keep_valid_call_pairs(self):
        steps = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c0", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a"}'}},
                {"id": "c1", "type": "function", "function": {"name": "finish", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c0", "content": "x" * 9000},
        ]
        cleaned = session.clean_steps(steps, 2)
        self.assertEqual([c["id"] for c in cleaned[0]["tool_calls"]], ["t2_c0"])
        self.assertEqual(cleaned[1]["tool_call_id"], "t2_c0")
        self.assertLess(len(cleaned[1]["content"]), 4100)

    def test_first_turn_has_no_refusal_instruction(self):
        config.init(["agent.py", "My meeting is in room 204. Remember this.", "6202"], cwd=self.workspace)
        text = loop.current_request(session.Session("6202"))
        self.assertIn("first turn", text)
        self.assertNotIn("error", text.lower())


if __name__ == "__main__":
    unittest.main(verbosity=1)
