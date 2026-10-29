"""The agent loop: ask the model, run its tool calls, send back the results, until it calls finish."""
import json
import os
import re
import shutil
import tempfile
import time

from . import config, llm, prompts
from .config import log
from .commands import run_command, run_in_copy
from .files import list_files
from .tools import CHANGING_TOOLS, TOOL_NAMES, TOOLS, run_tool

KEEP_RECENT_RESULTS = 4          # newest tool results that are never trimmed
TRIMMED_PREFIX = "[Older output removed to save space."
LOOSE_JSON = json.JSONDecoder(strict=False)   # accepts raw newlines inside strings, which the model sometimes emits


def loads(text):
    return LOOSE_JSON.decode(text.strip())


def parse_arguments(arguments):
    """Convert tool arguments (usually a JSON string) into a dict."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = loads(arguments)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def strip_think(text):
    """Remove any <think>...</think> block from the model's text."""
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def parse_tool_calls(message):
    """Return (id, name, arguments) for each tool call in the model's reply.

    Falls back to <tool_call> tags or bare JSON in the text if the server did not parse them.
    """
    calls = []
    for index, tool_call in enumerate(message.get("tool_calls") or []):
        function = tool_call.get("function") or {}
        calls.append((tool_call.get("id") or "call_%d" % index, function.get("name"),
                      parse_arguments(function.get("arguments"))))
    if calls:
        return calls

    content = strip_think(message.get("content"))
    for index, block in enumerate(re.findall(r"<tool_call>\s*(.*?)\s*</tool_call>", content, re.S)):
        block = re.sub(r"^```(?:json)?\s*|\s*```$", "", block)
        try:
            data = loads(block)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("name") in TOOL_NAMES:
            calls.append(("call_%d" % index, data["name"],
                          parse_arguments(data.get("arguments", data.get("parameters")))))
    if calls:
        return calls

    # The model sometimes writes a tool call as bare JSON text, e.g. {"name": "finish", "arguments": {...}}.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    try:
        data = loads(text)
    except ValueError:
        return calls
    for index, item in enumerate(data if isinstance(data, list) else [data]):
        if isinstance(item, dict) and item.get("name") in TOOL_NAMES:
            calls.append(("call_%d" % index, item["name"],
                          parse_arguments(item.get("arguments", item.get("parameters")))))
    return calls


def current_request(session):
    """The user message for this turn: workspace files, conversation position and the request."""
    if not session.session_id:
        position = "This request is independent. There are no earlier turns."
    elif session.turns:
        position = "This is turn %d of this conversation. The earlier turns are shown above." % (
            len(session.turns) + 1)
    else:
        position = "This is the first turn of this conversation."
    # The file list helps the model match names like "the file named duplicate.csv" and spot large files.
    # "/no_think" is Qwen3's own switch for answering without a thinking phase. It backs up
    # enable_thinking=false on servers that ignore that setting, and changes nothing where it is honored.
    return "Files currently in the workspace:\n%s\n\n%s\n\nRequest: %s\n\n/no_think" % (
        list_files("."), position, config.PROMPT)


def shorten_arguments(call, keep=200):
    """Shorten long string arguments (for example a whole program) in an older tool call."""
    arguments = call["function"].get("arguments", "")
    try:
        data = loads(arguments)
    except ValueError:
        return False
    changed = False
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, str) and len(value) > keep + 100:
                data[key] = "%s...[%d more characters omitted]" % (value[:keep], len(value) - keep)
                changed = True
    if changed:
        call["function"] = dict(call["function"], arguments=json.dumps(data))
    return changed


def trim_once(steps, keep):
    """Shrink the oldest large item in this turn's steps. Returns False when nothing is left to trim."""
    tool_indexes = [i for i, m in enumerate(steps) if m.get("role") == "tool"]
    for i in tool_indexes[:max(0, len(tool_indexes) - keep)]:
        content = steps[i]["content"]
        if len(content) > 500 and not content.startswith(TRIMMED_PREFIX):
            steps[i] = dict(steps[i], content="%s It began: %s ...]" % (TRIMMED_PREFIX, content[:300]))
            return True
    call_indexes = [i for i, m in enumerate(steps) if m.get("role") == "assistant" and m.get("tool_calls")]
    for i in call_indexes[:max(0, len(call_indexes) - 1)]:
        calls = [dict(c) for c in steps[i]["tool_calls"]]
        if any(shorten_arguments(c) for c in calls):
            steps[i] = dict(steps[i], tool_calls=calls)
            return True
    return False


def fit_context(base, steps, target):
    """Trim old tool output until the estimated prompt fits the target. Returns the estimate."""
    keep = KEEP_RECENT_RESULTS
    while True:
        estimate = llm.estimate_tokens(base + steps, TOOLS)
        if estimate <= target:
            return estimate
        if trim_once(steps, keep):
            continue
        if keep > 1:
            keep -= 1
            continue
        return estimate


def build_base(session, history_limit=None):
    base = [{"role": "system", "content": prompts.SYSTEM_PROMPT}]
    base.extend(session.history_messages(history_limit))
    base.append({"role": "user", "content": current_request(session)})
    return base


class TestRuns:
    """Results of the commands the model ran to test a program, used to review a finish call."""

    def __init__(self):
        self.failing = {}       # command -> latest failure
        self.passing = {}       # command -> latest output, for commands that exited with code 0
        self.reported = set()   # suspicious results already shown to the model
        self.evidence = []      # tool output the model has seen, without search context lines
        self.case_runs = 0      # automatic capitalization checks run so far
        self.model_commands = set()   # program commands the model itself ran
        self.patterns = []      # search patterns the model used
        self.versions = {}      # program path -> its contents after each edit, oldest first
        self.ever_passed = []   # program commands of the model that exited with code 0 at least once

    def add_evidence(self, name, args, result):
        """Keep what the model has seen of the workspace files, so report values can be checked against it.

        Search context lines and the output files themselves (for example reading REPORT.json back) do not count.
        """
        if name == "search_files":
            self.patterns.append(str(args.get("pattern") or ""))
        if name not in ("search_files", "read_file", "run_command"):
            return
        outputs = [os.path.basename(n) for n in FILE_NAME.findall(config.PROMPT) if n.endswith(".json")]
        target = str(args.get("path") or args.get("command") or "")
        if name != "search_files" and any(n in target for n in outputs):
            return      # reading the report back shows only what the model itself wrote
        kept = [l for l in result.split("\n") if not l.startswith("context ")
                and not any(n in l.split(":", 1)[0] for n in outputs)]
        self.evidence.append("\n".join(kept))

    def record(self, args, result):
        command = str(args.get("command") or "").strip()[:200]
        if not re.search(r"\.py\b", command):
            return
        lines = [l for l in result.split("\n") if l.strip() and not l.startswith("HINT:")]
        if not lines or not lines[0].startswith("["):
            return      # refused before running (for example a blocked command)
        self.failing.pop(command, None)
        self.passing.pop(command, None)
        if result.startswith("[exit code 0]"):
            self.passing[command] = "\n".join(lines[1:]).strip()[:300]
        else:
            detail = lines[-1].strip()[:160] if len(lines) > 1 else "(no output)"
            self.failing[command] = "%s: %s" % (lines[0].strip("[]"), detail)

    def diagnose(self):
        """Specific explanations for patterns in the test results that point to a known kind of bug."""
        notes = []
        passing = list(self.passing.items())
        for i, (command, output) in enumerate(passing):
            for other, other_output in passing[i + 1:]:
                a, b = command.split(), other.split()
                if (len(a) == len(b) and a != b and [w.lower() for w in a] == [w.lower() for w in b]
                        and output != other_output):
                    notes.append("'%s' and '%s' differ only in capital letters but print different results (%s / "
                                 "%s), so the program compares text case-sensitively. If the specification says "
                                 "case does not matter, apply .strip().lower() to the value the user typed as well "
                                 "as to the values from the data before comparing."
                                 % (command, other, output[:40].replace("\n", " | "),
                                    other_output[:40].replace("\n", " | ")))
        for i, (command, output) in enumerate(passing):
            program, words, options = command_shape(command)
            for other, other_output in passing:
                o_program, o_words, o_options = command_shape(other)
                added = options - o_options - {"--json", "--file", "--verbose", "--quiet"}
                if (output and output == other_output and o_program == program and o_words == words
                        and o_options < options and added):
                    notes.append("'%s' prints exactly the same as '%s', so the program ignores %s for '%s'. "
                                 "Make the code for '%s' read %s with options.get and use it."
                                 % (command, other, " and ".join(sorted(added)), words[0] if words else "",
                                    words[0] if words else "", " and ".join(sorted(added))))
                    break
        for command in self.failing:
            program, words, options = command_shape(command)
            parts = command.split()
            for j, part in enumerate(parts[:-1]):
                if not part.startswith("--") or parts[j + 1].startswith("-"):
                    continue
                value = parts[j + 1].lower()
                for other in self.passing:
                    o_program, o_words, o_options = command_shape(other)
                    if (o_program == program and o_words[:1] == words[:1] and part not in o_options
                            and value in [w.lower() for w in o_words[1:]]):
                        notes.append("'%s' fails but '%s' works, so the program reads this value from the plain "
                                     "words of the command line instead of from the option %s. If the "
                                     "specification writes the command with %s, read the value with "
                                     "options.get(\"%s\") in the code for '%s'."
                                     % (command, other, part, part, part, words[0] if words else ""))
                        break
        for command, failure in self.failing.items():
            parts = command.split()
            given = [p for j, p in enumerate(parts[:-1]) if p.startswith("--") and not parts[j + 1].startswith("-")]
            if given and re.search(r"missing|required|expected .*argument|usage", failure, re.I):
                program, words, options = command_shape(command)
                split_words, split_options = split_like_prompt(command.split(".py", 1)[-1].split())
                notes.append("'%s' gives %s with a value, but the program says something is missing (%s). With the "
                             "split_args function, this command line gives words=%s and options=%s: options and "
                             "their values are never in words. So in the code for '%s', remove every check of "
                             "words for these options (such as len(words) < 2 or words[1] == ...), and read the "
                             "values only with %s." % (command, " and ".join(given), failure[:80],
                                                       json.dumps(split_words), json.dumps(split_options),
                                                       words[0] if words else command,
                                                       " and ".join('options.get("%s")' % g for g in given)))
        unique = []
        for n in notes:
            if n not in unique:
                unique.append(n)
        return unique[:4]

    def new_diagnoses(self):
        """Diagnoses not shown to the model yet (added to the command's output as hints)."""
        fresh = [n for n in self.diagnose() if ("diagnosis", n) not in self.reported]
        for n in fresh:
            self.reported.add(("diagnosis", n))
        return fresh

    def untested(self):
        """Commands shown in the task that the model never ran with the same subcommand and options,
        and options described in the task that none of its commands used."""
        ran = [command_shape(c) for c in list(self.passing) + list(self.failing)]
        missing = []
        if spec_commands():
            used = set().union(*[o for p, w, o in ran]) if ran else set()
            for option in re.findall(r"`(--[a-z][\w-]*)(?: [A-Z][A-Z_]*)?`", config.PROMPT):
                if option not in used and ("a command with " + option) not in missing:
                    missing.append("a command with " + option)
        for example in spec_commands():
            program, words, options = command_shape(example)
            covered = any(p == program and w[:1] == words[:1] and options <= o for p, w, o in ran)
            if not covered and example not in missing:
                missing.append(example)
        return missing[:12]

    def suspicious(self):
        """Passing results that often hide a bug: options that change nothing, and all-zero output.

        A result that is still the same after it was pointed out is pointed out again, more firmly.
        """
        notes = []
        items = list(self.passing.items())
        for i, (command, output) in enumerate(items):
            numbers = re.findall(r"-?\d+(?:\.\d+)?", output)
            if output and numbers and all(float(n) == 0 for n in numbers):
                notes.append(self.note(("zero", command, output),
                                       "- %s printed only zero values (%s). Check that the data really gives zero, "
                                       "for example by using a value that you know exists in the data."
                                       % (command, output[:80])))
            for other, other_output in items[i + 1:]:
                if output and output == other_output and command.split()[:3] == other.split()[:3]:
                    notes.append(self.note(("same", command, other, output),
                                           "- '%s' and '%s' printed the same output (%s), so one of them may ignore "
                                           "an option or argument." % (command, other,
                                                                       output[:80].replace("\n", " | "))))
        return notes[:6]

    def note(self, key, text):
        if key in self.reported:
            return text + " This is still unchanged since the last review: read the code for this command and fix it."
        self.reported.add(key)
        return text


CASE_WORDS = re.compile(r"case[- ]insensitive|ignor\w* (?:the )?case|regardless of case|any capitali[sz]ation", re.I)


def case_check(runs, commands=None):
    """If the task says text is compared without regard to case, rerun passing commands with an option
    value in other capitalizations and report any output that changes.

    Each command is checked again only when its output changes, and at most 8 checks run per turn.
    The variants' results are recorded, so diagnose() keeps reporting the bug until it is fixed.
    """
    if not CASE_WORDS.search(config.PROMPT):
        return []
    notes = []
    for command in list(commands if commands is not None else runs.passing):
        output = runs.passing.get(command)
        key = ("case", command, output)
        if output is None or key in runs.reported or runs.case_runs >= 8:
            continue
        runs.reported.add(key)
        parts = command.split()
        for j, part in enumerate(parts[:-1]):
            value = parts[j + 1]
            if not (part.startswith("--") and re.match(r"^[A-Za-z][A-Za-z_-]*$", value)):
                continue
            runs.case_runs += 1
            changed = []
            for other in sorted({value.lower(), value.upper()} - {value}):
                variant = " ".join(parts[:j + 1] + [other] + parts[j + 2:])
                result = run_in_copy(variant)
                if result is None:
                    return notes
                runs.record({"command": variant}, result)
                runs.reported.add(("case", variant[:200], runs.passing.get(variant[:200])))
                body = runs.passing.get(variant[:200], result)
                if body != output:
                    changed.append((variant, body))
            if changed:
                notes.append("The task says text is compared without regard to case, but these give different "
                             "results: '%s' -> %s, %s. So the program compares case-sensitively. Apply "
                             ".strip().lower() to the value the user typed as well as to the values from the "
                             "data before comparing, then test again."
                             % (command, output[:40].replace("\n", " | "),
                                ", ".join("'%s' -> %s" % (v, b[:40].replace("\n", " | ")) for v, b in changed)))
            break
    return notes


def split_like_prompt(argv):
    """The split_args function given to the model in the system prompt, used to explain its results."""
    words, options = [], {}
    i = 0
    while i < len(argv):
        word = argv[i]
        if word.startswith("--"):
            if word not in ("--json", "--verbose", "--quiet", "--help") and i + 1 < len(argv) \
                    and not argv[i + 1].startswith("--"):
                options[word] = argv[i + 1]
                i += 1
            else:
                options[word] = True
        else:
            words.append(word)
        i += 1
    return words, options


def rerun_passing(runs, limit=12):
    """Rerun (in a copy of the workspace) the program commands that passed earlier and report any whose
    output has changed since the model last saw it, which usually means a later edit broke them."""
    notes = []
    # Only commands the model ran itself (not the agent's own variants), most recent first.
    chosen = [(c, o) for c, o in runs.passing.items() if c in runs.model_commands][-limit:]
    for command, before in chosen:
        result = run_in_copy(command)
        if result is None or config.time_left() < 40:
            break
        runs.record({"command": command}, result)
        after = runs.passing.get(command, result.split("\n", 1)[-1].strip())
        if after != before:
            notes.append("'%s' printed %s when you last ran it, but now prints %s. A later change affected "
                         "it: check whether that is intended." % (command, before[:50].replace("\n", " | "),
                                                                 after[:80].replace("\n", " | ")))
    return notes[:5]


def review_finish(status, reviews, runs):
    """For a long task (programming, debugging), return a message that asks the model to keep working
    instead of accepting its finish call, or None to accept it.

    A success gets one general final review. After that it is sent back only while concrete problems
    remain (missing output files, doubtful report values, failing or changed commands, bugs seen in the
    test results, untested commands from the task), showing the two most important ones at a time.
    A give-up is sent back up to twice while time remains.
    """
    if (len(config.PROMPT) <= 1500 or reviews >= 6 or config.time_left() < 35
            or config.model_calls >= config.WRAP_UP_CALLS):
        return None
    issues = []     # most important first
    success = status == "success"
    missing = missing_outputs() if success else []
    if missing:
        issues.append("These files named in the task do not exist in the workspace: %s. If the task asks you to "
                      "create them, create them now with the exact name." % ", ".join(missing))
    unverified, unseen = unverified_values(runs.evidence) if success else ([], [])
    save = (" When you have checked them, save the corrected report with write_file: only the file counts, "
            "not your finish message.")
    if unverified:
        issues.append("These values in your report are empty, zero, or do not appear anywhere in the workspace "
                      "files, so they were probably guessed: %s. Find the line that states each value (search for "
                      "the related identifiers or for a word from the key name) and copy it exactly.%s"
                      % (", ".join(unverified), save))
    if unseen:
        issues.append("These report values may be wrong: %s. Either you have not seen them in a MATCH line of a "
                      "search or in a file you read during this task (a value seen only in a context line usually "
                      "belongs to another request), or the files store them under a different field name (then it "
                      "is probably the answer to a different question). Search for the identifiers and the key's "
                      "main word and confirm or correct each value.%s" % ("; ".join(unseen), save))
    unfollowed = unsearched_ids(runs.patterns) if success and runs.patterns else []
    if unfollowed:
        issues.append("You have not searched for these identifiers from your report: %s. Search for each one (it "
                      "often leads to the root cause, for example what that worker or job was doing) and correct "
                      "the report if needed." % ", ".join(unfollowed))
    if runs.failing:
        issues.append("These commands that run the program failed the last time you ran them:\n%s\nIf the "
                      "specification says a command should work, fix the program and run the command again. If it "
                      "is an error case that the specification says must fail like this, it is fine."
                      % "\n".join("- %s -> %s" % item for item in list(runs.failing.items())[-6:]))
    regressions = rerun_passing(runs) if success and runs.passing else []
    if regressions:
        issues.append("The program changed since some of your tests:\n%s"
                      % "\n".join("- " + r for r in regressions))
    diagnoses = runs.diagnose() + (case_check(runs) + format_problems(runs) + source_problems(runs)
                                   if success else [])
    if diagnoses:
        issues.append("Your own test results show these bugs. Fix each one in the code now (read the code for "
                      "that command, then use replace_lines or edit_file), and run the same commands again:\n%s"
                      % "\n".join("- " + d for d in diagnoses))
    suspicious = runs.suspicious() if success else []
    if suspicious:
        issues.append("Check these test results against the specification:\n%s\nIf one is wrong, fix the "
                      "program and test again." % "\n".join(suspicious))
    untested = runs.untested() if success and (runs.passing or runs.failing) else []
    if untested:
        issues.append("The task shows these commands, but you have not run any command with the same command word "
                      "and options:\n%s\nRun each one (with real values in place of placeholders such as NAME or "
                      "N) and check its output against the specification." % "\n".join("- " + c for c in untested))
    if status == "error":
        if reviews >= 2:
            return None
        return prompts.GIVE_UP_CHECK + "".join("\n\n" + i for i in issues[:2])
    if issues:
        text = "Not finished yet. Fix these problems first, then call finish again:\n\n" + "\n\n".join(issues[:2])
        if len(issues) > 2:
            text += "\n\n(%d more points will be shown after these are fixed.)" % (len(issues) - 2)
        return text
    if reviews == 0:
        return prompts.FINAL_CHECK
    return None
    note = ""
    missing = missing_outputs()
    if missing and status == "success":
        note += ("\n\nThese files named in the task do not exist in the workspace: %s. If the task asks you to "
                 "create them, create them now with the exact name." % ", ".join(missing))
    unverified, unseen = unverified_values(runs.evidence) if status == "success" else ([], [])
    if unseen:
        note += ("\n\nThese report values may be wrong: %s. Either you have not seen them in a MATCH line of a "
                 "search or in a file you read during this task (a value seen only in a context line usually "
                 "belongs to another request), or the files store them under a different field name (then it is "
                 "probably the answer to a different question). Search for the identifiers and the key's main word "
                 "and confirm or correct each value." % "; ".join(unseen))
    if unverified:
        note += ("\n\nThese values in your report are empty, zero, or do not appear anywhere in the workspace "
                 "files, so they were probably guessed: %s. Find the line that states each value (search for the "
                 "related identifiers or for a word from the key name) and copy it exactly." % ", ".join(unverified))
    unfollowed = unsearched_ids(runs.patterns) if status == "success" and runs.patterns else []
    if unfollowed:
        note += ("\n\nYou have not searched for these identifiers from your report: %s. Search for each one "
                 "(it often leads to the root cause, for example what that worker or job was doing) and correct "
                 "the report if needed." % ", ".join(unfollowed))
    if unseen or unverified:
        note += ("\nWhen you have checked them, save the corrected report with write_file: only the file is "
                 "what counts, not your finish message.")
    if runs.failing:
        note += ("\n\nThese commands that run the program failed the last time you ran them:\n%s\nIf the "
                 "specification says a command should work, fix the program and run the command again. If it is "
                 "an error case that the specification says must fail like this, it is fine."
                 % "\n".join("- %s -> %s" % item for item in list(runs.failing.items())[-6:]))
    regressions = rerun_passing(runs) if status == "success" and runs.passing else []
    if regressions:
        note += "\n\nThe program changed since some of your tests:\n%s" % "\n".join("- " + r for r in regressions)
    diagnoses = runs.diagnose() + (case_check(runs) + format_problems(runs) if status == "success" else [])
    if diagnoses:
        note += ("\n\nYour own test results show these bugs. Fix each one in the code now, then run the same "
                 "commands again:\n%s" % "\n".join("- " + d for d in diagnoses))
    untested = runs.untested() if status == "success" and (runs.passing or runs.failing) else []
    if untested:
        note += ("\n\nThe task shows these commands, but you have not run any command with the same command word "
                 "and options:\n%s\nRun each one (with real values in place of placeholders such as NAME or N) and "
                 "check its output against the specification." % "\n".join("- " + c for c in untested))
    suspicious = runs.suspicious() if status == "success" else []
    if suspicious:
        note += ("\n\nCheck these test results against the specification:\n%s\nIf one is wrong, fix the "
                 "program and test again." % "\n".join(suspicious))
    if status == "error":
        return prompts.GIVE_UP_CHECK + note if reviews < 2 else None
    if reviews == 0 or note:
        return prompts.FINAL_CHECK + note
    return None


def unverified_values(evidence=None):
    """Check the values of a JSON report named in the task.

    Returns (guessed, unseen): values that are empty, zero or found in no other workspace file, and
    values never shown to the model in a matching search line or a file it read (`evidence`).
    """
    names = [n for n in FILE_NAME.findall(config.PROMPT) if n.endswith(".json")]
    reports = [os.path.join(config.WORKSPACE, n) for n in names if os.path.isfile(os.path.join(config.WORKSPACE, n))]
    if not reports:
        return [], []
    texts = []
    for folder, dirs, files_here in os.walk(config.WORKSPACE):
        for name in files_here:
            full = os.path.join(folder, name)
            if full in reports or os.path.getsize(full) > 20000000:
                continue
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    texts.append(f.read())
            except OSError:
                continue
    found, unseen = [], []
    seen = "\n".join(evidence or [])
    everything = "\n".join(texts)
    for report in reports:
        try:
            with open(report, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            if value is None:
                found.append("%s=null" % key)
                continue
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                continue
            text = str(value)
            if value in (0, "") or (len(text) >= 3 and not any(text in t for t in texts)):
                found.append("%s=%s" % (key, json.dumps(value)))
            elif evidence is not None and len(text) >= 3 and text not in seen:
                unseen.append("%s=%s" % (key, json.dumps(value)))
            elif isinstance(value, str) and re.search(r"(^|_)(final|last|latest|current|end)(_|$)", key.lower()):
                later = later_line(texts, data, key, text)
                if later:
                    unseen.append("%s=%s (the key asks for the final state, but a later line about the same "
                                  "identifier says: %s)" % (key, json.dumps(value), later[:160]))
            elif isinstance(value, str) and TIMESTAMP.match(text) and nearby_event_line(texts, key, text):
                unseen.append("%s=%s (the line with this timestamp does not mention the event the key asks about, "
                              "but a nearby line does: %s)" % (key, json.dumps(value),
                                                               nearby_event_line(texts, key, text)[:160]))
            elif len(text) >= 3:
                fields = field_names(everything, text)
                if fields and not any(similar_words(key, f) for f in fields):
                    unseen.append("%s=%s (in the files this value appears only as %s, which does not match the "
                                  "key name)" % (key, json.dumps(value),
                                                 ", ".join("%s=%s" % (f, text) for f in fields[:3])))
    return found[:8], unseen[:8]


def unsearched_ids(patterns):
    """Identifier values in the JSON report (for example inv-w1 or hold-123) never used in a search."""
    found = []
    for name in [n for n in FILE_NAME.findall(config.PROMPT) if n.endswith(".json")]:
        try:
            with open(os.path.join(config.WORKSPACE, name), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            if (isinstance(value, str) and re.match(r"^[A-Za-z][\w.-]*\d[\w.-]*$", value) and len(value) >= 4
                    and not any(value.lower() in p.lower() or (len(p) >= 4 and p.lower() in value.lower())
                                for p in patterns)):
                found.append("%s (%s)" % (value, key))
    return found[:6]


def later_line(texts, report, key, value):
    """For a 'final state' value, the last line about the same identifier, if it comes later and does not
    contain the value."""
    idents = [str(v) for k, v in report.items() if k != key and isinstance(v, str) and len(v) >= 5]
    for text in texts:
        lines = text.split("\n")
        for ident in idents:
            about = [i for i, l in enumerate(lines) if ident in l]
            holding = [i for i in about if value in lines[i]]
            if holding and about[-1] > holding[-1] and value not in lines[about[-1]]:
                return lines[about[-1]].strip()
    return None


TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\d[T ]\d\d:\d\d")
EVENT_SKIP = {"time", "timestamp", "date", "started", "start", "began", "when", "first", "last", "ended", "end"}


def nearby_event_line(texts, key, value):
    """For a timestamp value: if its own line does not mention the key's event word (for example 'stall'
    for stall_started_at) but a line within three lines does, return that nearby line."""
    words = [w for w in re.split(r"[_\W]+", key.lower()) if len(w) >= 4 and w not in EVENT_SKIP]
    if not words:
        return None

    def mentions(line):
        tokens = re.findall(r"[a-z]{4,}", line.lower())
        return any(t[:4] == w[:4] for t in tokens for w in words)

    for text in texts:
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if value in line and not mentions(line):
                for j in range(max(0, i - 3), min(len(lines), i + 4)):
                    if j != i and mentions(lines[j]) and TIMESTAMP.match(lines[j].strip()):
                        return lines[j].strip()
            elif value in line:
                return None
    return None


GENERIC_WORDS = {"id", "ms", "at", "the", "of", "for", "code", "name", "value", "num", "number"}


def field_names(text, value):
    """Names of the key=value fields in which `value` appears, for example ['latency_ms']."""
    names = []
    for name in re.findall(r"([A-Za-z_][\w.-]*)=\"?" + re.escape(value) + r"(?![\w.-])", text):
        if name not in names:
            names.append(name)
    return names


def similar_words(key, field):
    """True if a report key and a log field name share a word (by its first three letters)."""
    key_words = [w for w in re.split(r"[_\W]+", key.lower()) if w and w not in GENERIC_WORDS]
    field_words = [w for w in re.split(r"[_\W]+", field.lower()) if w and w not in GENERIC_WORDS]
    return any(a[:3] == b[:3] for a in key_words for b in field_words)


def command_shape(command):
    """(program, words, options) of a command line such as 'python3 tally.py top 3 --json'."""
    parts = command.replace("`", " ").split()
    for i, part in enumerate(parts):
        if part.endswith(".py"):
            rest = parts[i + 1:]
            options = {w.split("=")[0] for w in rest if w.startswith("--")}
            words = [w for j, w in enumerate(rest) if not w.startswith("-")
                     and not (j > 0 and rest[j - 1].startswith("--") and "=" not in rest[j - 1])]
            return os.path.basename(part), words, options
    return "", [], set()


FORMAT_TOKENS = [("<TAB>", r"\t"), ("YYYY", r"\d{4}"), ("MM", r"\d{2}"), ("DD", r"\d{2}"), ("X.XX", r"-?\d+\.\d{2}"),
                 ("AMOUNT", r"-?\d+\.\d{2}"), ("TOTAL", r"-?\d+\.\d{2}"), ("CATEGORY", r"[^\t]+"),
                 ("NAME", r"[^\t]+"), ("COUNT", r"\d+"), ("K", r"\d+"), ("N", r"\d+")]


def output_formats():
    """{command word: [(template, regex)]} for line formats such as `YYYY-MM<TAB>AMOUNT` or
    `count=K total=X.XX` written in the same numbered requirement as the command."""
    formats = {}
    items = re.split(r"\n(?=\s*\d+\.\s)", config.PROMPT)
    words = {command_shape(c)[1][0] for c in spec_commands() if command_shape(c)[1]}
    for item in items:
        spans = re.findall(r"`([^`\n]+)`", item)
        if spans and re.match(r"^[a-z][a-z_-]*$", spans[0].split()[0] if spans[0].split() else ""):
            words.add(spans[0].split()[0])      # the command a numbered requirement is about
        commands = [sp.split()[0] for sp in spans if sp.split() and sp.split()[0] in words]
        if not commands:
            continue
        for span in spans:
            if "<TAB>" not in span and not re.search(r"\w=[A-Z]", span):
                continue
            token_regex = dict(FORMAT_TOKENS)
            parts = re.split(r"(<TAB>|(?<![A-Za-z])(?:YYYY|MM|DD|AMOUNT|TOTAL|CATEGORY|NAME|COUNT|K|N)(?![A-Za-z])|"
                             r"X\.XX)", span)
            pattern = "".join(token_regex.get(part, re.escape(part)) for part in parts)
            formats.setdefault(commands[0], []).append((span, re.compile(pattern + "$")))
    return formats


def source_problems(runs):
    """Rules from the task that the program's code visibly does not follow."""
    notes = []
    if re.search(r"half[- ]up", config.PROMPT, re.I):
        for rel in runs.versions:
            full = os.path.join(config.WORKSPACE, rel)
            try:
                with open(full, encoding="utf-8") as f:
                    code = f.read()
            except OSError:
                continue
            if "ROUND_HALF_UP" not in code:
                notes.append("The task asks to round half up, but %s never uses ROUND_HALF_UP. Formatting a Decimal "
                             "with f\"{value:.2f}\" or \"%%.2f\" rounds half to even (2.005 becomes 2.00). Round with "
                             "value.quantize(decimal.Decimal(\"0.01\"), rounding=decimal.ROUND_HALF_UP) where the "
                             "task says so." % rel)
    return notes


def format_problems(runs):
    """Outputs of the model's own passing commands that do not match the line format the task gives."""
    notes = []
    formats = output_formats()
    for command, output in runs.passing.items():
        program, words, options = command_shape(command)
        if not words or words[0] not in formats or "--json" in options or not output.strip():
            continue
        lines = output.split("\n")
        if len(output) >= 300:
            lines = lines[:-1]      # the stored output was cut, so its last line may be incomplete
        for line in lines:
            if line.strip() and not any(regex.match(line) for _, regex in formats[words[0]]):
                notes.append("'%s' printed the line %r, but the task gives the format %s for '%s'."
                             % (command, line[:60], " or ".join(t for t, _ in formats[words[0]]), words[0]))
                break
    return notes[:4]


def spec_commands():
    """Example commands for a program written in the task, for example `python3 tally.py total --category NAME`."""
    found = []
    for match in re.findall(r"`(python3? [^`\n]*\.py[^`\n]*)`", config.PROMPT):
        if match.strip() not in found and "<" not in match.split(".py", 1)[1][:3]:
            found.append(match.strip())
    # Short forms such as `avg --category NAME` next to full ones are commands of the same program.
    programs = sorted({c.split(".py")[0] + ".py" for c in found})
    if len(programs) == 1:
        for match in re.findall(r"`([a-z][\w-]* [^`\n]*--[^`\n]*)`", config.PROMPT):
            command = "%s %s" % (programs[0], match.strip())
            if ".py" not in match and command not in found and not any(c.startswith(command) for c in found):
                found.append(command)
    # A sentence such as "`count`, `refunds` and `months` also accept `--category NAME`" adds those combinations.
    if len(programs) == 1:
        known = {command_shape(c)[1][0] for c in found if command_shape(c)[1]}
        for item in re.split(r"\n(?=\s*\d+\.\s)", config.PROMPT):
            first = re.findall(r"`([^`\n]+)`", item)[:1]
            if first and re.match(r"^[a-z][a-z_-]*$", first[0]):
                known.add(first[0])     # the command a numbered requirement is about
                if not any(command_shape(c)[1][:1] == [first[0]] for c in found):
                    found.append("%s %s" % (programs[0], first[0]))
        for sentence in re.split(r"(?<=[.!?])\s+|\n\s*\n", config.PROMPT):
            spans = re.findall(r"`([^`\n]+)`", sentence)
            words = [sp for sp in spans if sp in known]
            options = [sp for sp in spans if re.match(r"^--[a-z][\w-]*( [A-Z][A-Z_]*)?$", sp)]
            if len(words) >= 2 and options:
                for word in words:
                    for option in options:
                        found.append("%s %s %s" % (programs[0], word, option))
    unique, shapes = [], []
    for command in found:
        program, words, options = command_shape(command)
        shape = (program, words[:1], sorted(options))
        if shape not in shapes:
            shapes.append(shape)
            unique.append(command)
    return unique


def save_report_from_message(message):
    """If the final message holds the JSON report the task asks for, make sure the report file has it.

    The model sometimes puts its final, corrected values only in the finish message.
    """
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(message).strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return
    if not isinstance(data, dict) or len(data) < 2:
        return
    for name in [n for n in FILE_NAME.findall(config.PROMPT) if n.endswith(".json")]:
        full = os.path.join(config.WORKSPACE, name)
        old = None
        if os.path.isfile(full):
            try:
                with open(full, encoding="utf-8") as f:
                    old = json.load(f)
            except (OSError, ValueError):
                old = None
        if isinstance(old, dict) and len(set(old) & set(data)) < max(2, len(old) // 2):
            continue        # the message is about something else
        merged = dict(old) if isinstance(old, dict) else {}
        merged.update(data)
        if merged != old:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2)
                f.write("\n")
            log("  saved the JSON from the finish message to %s" % name)
        return


FILE_NAME = re.compile(r"(?<![\w/.-])([\w][\w/.-]*\.(?:json|py|csv|txt|md|yaml|yml|html|sh|log))\b")


def missing_outputs():
    """File names mentioned in the task that do not exist in the workspace (possible outputs not yet written)."""
    missing = []
    for name in FILE_NAME.findall(config.PROMPT):
        name = name.rstrip(".")
        if name not in missing and not os.path.exists(os.path.join(config.WORKSPACE, name)):
            missing.append(name)
    return missing[:5]


def decode_loose_string(raw):
    """Decode the inside of a JSON string written by the model, tolerating its usual mistakes:
    unescaped quotes, stray backslashes and an escape cut in half at the end."""
    raw = re.sub(r"\\u[0-9a-fA-F]{0,3}$|(?<!\\)\\$", "", raw)
    raw = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", raw)
    raw = re.sub(r'(?<!\\)"', r'\\"', raw)
    try:
        return LOOSE_JSON.decode('"%s"' % raw)
    except ValueError:
        return None


def salvage_cut_off(text):
    """If a reply holds a write_file or append_file call as text that could not be parsed (cut off, or
    not valid JSON), return (tool, path, content, complete). For a cut-off call, content holds only the
    complete lines received, so the model can continue from there."""
    match = re.search(r'"name"\s*:\s*"(write_file|append_file)".*?"path"\s*:\s*"([^"]+)".*?"content"\s*:\s*"',
                      text, re.S)
    if not match:
        return None
    raw = text[match.end():]
    end = re.search(r'"\s*}(?:\s*})*\s*(?:</tool_call>)?\s*$', raw)
    if end:
        content = decode_loose_string(raw[:end.start()])
        if content is not None:
            return match.group(1), match.group(2), content, True
    content = decode_loose_string(raw)
    if content is None or "\n" not in content:
        return None
    return match.group(1), match.group(2), content[:content.rfind("\n") + 1], False   # complete lines only


def looks_like_code(text):
    return "```" in text or re.search(r"^\s*(def |class |import |from \w+ import )", text, re.M) is not None


def read_tail(path, count=4):
    """The last lines of a workspace file, numbered, to show where a cut-off file ends."""
    try:
        with open(os.path.join(config.WORKSPACE, path.lstrip("/")), encoding="utf-8") as f:
            lines = f.read().split("\n")
    except OSError:
        return ""
    if lines and lines[-1] == "":
        lines.pop()
    first = max(0, len(lines) - count)
    return "\n".join("%6d| %s" % (n + 1, lines[n]) for n in range(first, len(lines)))


def remember_version(runs, args):
    """Keep the contents of a program file after each change (the last eight versions)."""
    path = str(args.get("path") or args.get("destination") or "")
    if not path.endswith(".py"):
        return
    try:
        full = os.path.realpath(os.path.join(config.WORKSPACE, path.lstrip("/")))
        with open(full, encoding="utf-8", newline="") as f:
            content = f.read()
    except OSError:
        return
    rel = os.path.relpath(full, config.WORKSPACE)
    versions = runs.versions.setdefault(rel, [])
    if not versions or versions[-1] != content:
        versions.append(content)
        del versions[:-8]


def keep_best_version(runs):
    """Before the run ends: if the program no longer gives the results the model last saw for its
    passing test commands (a late edit broke it and there was no time to notice), put back the newest
    earlier version that reproduces more of them. Checks run in a copy of the workspace and stop at a
    hard deadline.
    """
    deadline = config.START_TIME + config.TIME_BUDGET + 8
    for rel, versions in runs.versions.items():
        name = os.path.basename(rel)
        # Commands the model saw passing, with the output it last saw for each.
        commands = [c for c in runs.ever_passed if name in c and c in runs.passing][-10:]
        full = os.path.join(config.WORKSPACE, rel)
        if len(versions) < 2 or not commands or not os.path.isfile(full):
            continue
        with open(full, encoding="utf-8", newline="") as f:
            current = f.read()
        copy = tempfile.mkdtemp(prefix="agent_versions_")
        try:
            target = os.path.join(copy, "w")
            shutil.copytree(config.WORKSPACE, target, symlinks=True)

            def passes(content):
                """How many commands still exit with code 0 and print what the model last saw (None if out
                of time)."""
                with open(os.path.join(target, rel), "w", encoding="utf-8", newline="") as f:
                    f.write(content)
                count = 0
                for command in commands:
                    if time.time() > deadline - 2:
                        return None
                    seconds = min(10.0, deadline - time.time() - 1)
                    result = run_command(command, timeout=seconds, cwd=target, ignore_budget=True)
                    lines = [l for l in result.split("\n") if l.strip() and not l.startswith("HINT:")]
                    if result.startswith("[exit code 0]") and "\n".join(lines[1:]).strip()[:300] == runs.passing[command]:
                        count += 1
                return count

            now = passes(current)
            if now is None or now == len(commands):
                continue
            # Try earlier versions, newest first, and keep the one that reproduces the most results.
            best, best_score = None, now
            for earlier in list(reversed(versions[:-1]))[:6]:
                if earlier == current:
                    continue
                score = passes(earlier)
                if score is None:
                    break
                if score > best_score:
                    best, best_score = earlier, score
                    if score == len(commands):
                        break
            if best is not None:
                with open(full, "w", encoding="utf-8", newline="") as f:
                    f.write(best)
                log("restored an earlier version of %s: %d of %d test commands pass instead of %d"
                    % (rel, best_score, len(commands), now))
        except OSError as e:
            log("version check failed: %r" % (e,))
        finally:
            shutil.rmtree(copy, ignore_errors=True)


def run(session, steps):
    """Run the agent (see run_turn). For long tasks, make sure a late edit did not break the program."""
    runs = TestRuns()
    try:
        return run_turn(session, steps, runs)
    finally:
        if len(config.PROMPT) > 1500 and runs.versions:
            keep_best_version(runs)


def run_turn(session, steps, runs):
    """Run the agent until it calls finish. Everything this turn adds is appended to `steps`.

    Returns (status, message).
    """
    history_limit = config.HISTORY_CHAR_LIMIT
    base = build_base(session, history_limit)
    target = config.PROMPT_TOKEN_TARGET
    reminders, context_errors, step = 0, 0, 0
    wrapped_up = False
    attempted_changes, failed_changes = 0, []
    last_calls, repeats = None, 0
    reviews = 0             # finish calls sent back for another look (long tasks only)
    empty_replies = 0
    seen_results = {}       # (call, result) -> how many times it happened this turn, to spot loops
    syntax_errors = 0       # consecutive file edits that left a Python syntax error

    while config.model_calls < config.MAX_MODEL_CALLS:
        if config.time_left() < 12:
            return "error", "Ran out of time before the task was finished."
        if not wrapped_up and (config.time_left() < config.WRAP_UP_SECONDS
                               or config.model_calls >= config.WRAP_UP_CALLS):
            wrapped_up = True
            steps.append({"role": "user", "content": prompts.REMINDER_WRAP_UP})

        estimate = fit_context(base, steps, target)
        room = config.CONTEXT_LIMIT - estimate - 256
        if room < config.MIN_OUTPUT_TOKENS and history_limit > 0:
            # Still too long after trimming this turn: send less of the earlier conversation.
            history_limit = history_limit // 2 if history_limit > 4000 else 0
            base = build_base(session, history_limit)
            continue
        max_tokens = max(512, min(config.MAX_OUTPUT_TOKENS, room))

        started = time.time()
        try:
            message, finish_reason = llm.call_llm(base + steps, TOOLS, max_tokens)
        except llm.OutOfTime as e:
            log("stopping: %s" % e)
            return "error", "Ran out of time before the task was finished."
        except llm.ContextTooLong as e:
            context_errors += 1
            if context_errors > 5:
                raise RuntimeError("The conversation does not fit in the model's context window.")
            if e.prompt_tokens:
                llm.chars_per_token = max(2.0, llm.payload_chars(base + steps, TOOLS) / float(e.prompt_tokens))
            target = int(target * 0.8)
            log("context too long, trimming to about %d tokens" % target)
            continue

        content = strip_think(message.get("content"))
        # Give every call an ID that is unique across the whole session history.
        calls = [("call_%d_%d" % (step, i), name, args)
                 for i, (_, name, args) in enumerate(parse_tool_calls(message))]
        log("step %d (%.1fs, ~%d prompt tokens, call %d): %s" % (
            step, time.time() - started, estimate, config.model_calls,
            [c[1] for c in calls] or content[:200]))
        step += 1

        if not calls:
            if not content and empty_replies < 5:
                # An empty reply (seen on busy servers) is retried with a nudge instead of ending the task.
                empty_replies += 1
                steps.append({"role": "user", "content": prompts.REMINDER_EMPTY})
                continue
            saved = salvage_cut_off(content)
            if saved and saved[3] and saved[2].strip():
                # A complete write_file call that was not valid JSON: run it with the recovered content.
                tool_name, path, part, _ = saved
                result = run_tool(tool_name, {"path": path, "content": part})
                log("  recovered a %s of %s from text: %s" % (tool_name, path, result[:120]))
                if not result.startswith("ERROR"):
                    remember_version(runs, {"path": path})
                    steps.append({"role": "assistant", "content": "(I wrote %s with %s.)" % (path, tool_name)})
                    steps.append({"role": "user", "content": "Your %s call was not valid JSON, but its content was "
                                  "recovered and saved: %s. Continue the task with tool calls." % (tool_name, result)})
                    continue
            if finish_reason == "length" or (saved and not saved[3]):
                if saved and not saved[3] and saved[2].strip():
                    tool_name, path, part, _ = saved
                    result = run_tool(tool_name, {"path": path, "content": part})
                    log("  saved the cut-off %s of %s: %s" % (tool_name, path, result[:120]))
                    if not result.startswith("ERROR"):
                        remember_version(runs, {"path": path})
                        lines = read_tail(path)
                        steps.append({"role": "assistant", "content": "(My reply was cut off while writing %s.)" % path})
                        steps.append({"role": "user", "content": prompts.REMINDER_CUT_OFF_SAVED % (path, lines)})
                        continue
                steps.append({"role": "assistant", "content": content[:1500]})
                steps.append({"role": "user", "content": prompts.REMINDER_CUT_OFF})
                continue
            code_like = looks_like_code(content)
            if reminders < (3 if code_like else 1):
                reminders += 1
                steps.append({"role": "assistant", "content": content[:1500]})
                steps.append({"role": "user",
                              "content": prompts.REMINDER_CODE_AS_TEXT if code_like else prompts.REMINDER_PLAIN_TEXT})
                continue
            if content and not code_like:
                review = review_finish("success", reviews, runs)
                if review:
                    reviews += 1
                    steps.append({"role": "assistant", "content": content[:1500]})
                    steps.append({"role": "user", "content": review})
                    continue
                return "success", content
            return "error", "The model did not complete the task."

        # If the calls were recovered from the text, do not repeat that text as content.
        spoken = content[:1500] if message.get("tool_calls") else ""
        steps.append({
            "role": "assistant",
            "content": spoken,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}
                           for call_id, name, args in calls],
        })

        signature = json.dumps([(name, args) for _, name, args in calls], sort_keys=True)
        repeats = repeats + 1 if signature == last_calls else 0
        last_calls = signature

        finished = None
        stuck = False
        for call_id, name, args in calls:
            if name == "finish":
                status = "error" if args.get("status") == "error" else "success"
                text = str(args.get("message") or content or ("Done." if status == "success" else "Failed."))
                # Do not report success if every attempt to change a file failed.
                if status == "success" and attempted_changes and len(failed_changes) == attempted_changes:
                    finished = ("error", "No file was changed. %s" % failed_changes[-1])
                    break
                review = review_finish(status, reviews, runs)
                if review:
                    reviews += 1
                    log("  finish(%s) sent back for review" % status)
                    steps.append({"role": "tool", "tool_call_id": call_id, "content": review})
                    continue
                if status == "success" and len(config.PROMPT) > 1500:
                    save_report_from_message(text)
                finished = (status, text)
                break
            result = run_tool(name, args)
            if name in CHANGING_TOOLS:
                attempted_changes += 1
                if result.startswith("ERROR"):
                    failed_changes.append(result)
                else:
                    remember_version(runs, args)
                if (not result.startswith("ERROR") and str(args.get("path", "")).endswith(".py")
                        and runs.model_commands and config.time_left() > 60):
                    # Re-run the earlier passing tests at once, so a fix that breaks something is noticed.
                    regressions = rerun_passing(runs, limit=15)
                    if regressions:
                        result += ("\nNOTE: after this change, some earlier tests give different output:\n%s"
                                   % "\n".join("- " + r for r in regressions))
            shown = "%s(%s)" % (name, json.dumps({k: v for k, v in args.items()
                                                    if k not in ("content", "new_text", "old_text")})[:160])
            log("  %s -> %s" % (shown, result[:200].replace("\n", " | ")))
            runs.add_evidence(name, args, result)
            if name == "run_command":
                runs.record(args, result)
                this_command = str(args.get("command") or "").strip()[:200]
                runs.model_commands.add(this_command)
                if result.startswith("[exit code 0]") and this_command not in runs.ever_passed:
                    runs.ever_passed.append(this_command)
                fresh = runs.new_diagnoses()
                if result.startswith("[exit code 0]"):
                    this = str(args.get("command") or "").strip()[:200]
                    fresh += case_check(runs, [this])
                    for problem in format_problems(runs):
                        if problem.startswith("'%s' " % this) and ("format", problem) not in runs.reported:
                            runs.reported.add(("format", problem))
                            fresh.append(problem)
                if fresh:
                    result += "".join("\nHINT: " + d for d in fresh)
            steps.append({"role": "tool", "tool_call_id": call_id, "content": result})
            # Rerunning a passing test is normal, so only other repeated call/result pairs count as a loop.
            if not (name == "run_command" and result.startswith("[exit code 0]")):
                key = json.dumps([name, args, result[:3000]], sort_keys=True)
                seen_results[key] = seen_results.get(key, 0) + 1
                stuck = stuck or seen_results[key] in (3, 5, 7, 9)
            if name == "run_command" and "Traceback" in result:
                # The same exception from different commands also means the fix is not working.
                errors = re.findall(r"^\w[\w.]*(?:Error|Exception|InvalidOperation)\b.*$", result, re.M)
                if errors:
                    key = "traceback " + errors[-1]
                    seen_results[key] = seen_results.get(key, 0) + 1
                    stuck = stuck or seen_results[key] in (3, 5, 7, 9)
            if name in CHANGING_TOOLS and name != "append_file":   # a file written in parts is incomplete for a while
                syntax_errors = syntax_errors + 1 if "Python syntax error" in result else 0

        if finished:
            return finished
        if syntax_errors >= 3:
            syntax_errors = 0
            steps.append({"role": "user", "content": prompts.REMINDER_REWRITE})
        if stuck:
            log("  loop detected: same call and result seen again")
            steps.append({"role": "user", "content": prompts.REMINDER_STUCK})
        elif repeats >= 2:
            steps.append({"role": "user", "content": prompts.REMINDER_REPEAT})

    return "error", "Stopped after reaching the limit of %d model calls." % config.MAX_MODEL_CALLS
