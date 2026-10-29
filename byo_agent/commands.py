"""run_command: lets the model run and test programs it writes, inside the workspace."""
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile

from . import config

OUTPUT_HEAD = 6000      # characters of output kept from the start ...
OUTPUT_TAIL = 3000      # ... and from the end
SECRET_VARS = ("QWEN_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "AGENT_EXTRA_BODY")


def blocked_reason(command):
    """Refuse commands that delete files outside /tmp. Files are deleted with delete_file instead."""
    for part in re.split(r"[;&|\n]+", command):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        if words and os.path.basename(words[0]) in ("rm", "rmdir", "shred", "unlink"):
            targets = [os.path.realpath(os.path.join(config.WORKSPACE, w)) for w in words[1:] if not w.startswith("-")]
            outside_workspace = all(os.path.commonpath([t, config.WORKSPACE]) != config.WORKSPACE for t in targets)
            if not targets or not outside_workspace or not all(t.startswith(("/tmp/", "/private/tmp/")) for t in targets):
                return ("ERROR: run_command may only delete files under /tmp. "
                        "Use delete_file for a file in the workspace, and never delete files the task does not ask for.")
    return None


# Short explanations of common Python errors, added after a command's output when they appear in it.
# Qwen3-14B often misreads these and keeps applying the same wrong fix.
ERROR_HINTS = [
    (r"decimal\.InvalidOperation|ConversionSyntax",
     "decimal.Decimal(text) raises decimal.InvalidOperation when the text is not a number, and "
     "decimal.InvalidOperation is NOT a subclass of ValueError, so 'except ValueError' does not catch it. "
     "Use 'except (decimal.InvalidOperation, ValueError):' and skip that row."),
    (r"UnboundLocalError",
     "A variable inside the function has the same name as a function or global used in that function "
     "(for example a variable named count in a function that also calls count()). Rename the variable."),
    (r"unrecognized arguments|the following arguments are required",
     "argparse rejected the command line. Every option must be added to the parser that reads it (with "
     "subcommands, to that subcommand's parser). A simpler way that accepts options anywhere: read "
     "sys.argv[1:] yourself in a loop. A word starting with -- is an option: flags such as --json take no "
     "value, other options take the next word as their value. The remaining words, in order, are the "
     "command and its arguments."),
    (r"time data .* does not match format|unconverted data remains",
     "datetime.strptime raises ValueError for an invalid date. Catch it and skip that row."),
    (r"TypeError: unsupported operand type\(s\).*(Decimal|float)",
     "Do not mix float and Decimal. Convert every amount with decimal.Decimal(text) and start sums at Decimal(0)."),
    (r"KeyError",
     "A dictionary key is missing. If it is a CSV column name, compare it with the header line exactly "
     "(spaces and capital letters matter). If it is a category or other value, use dict.get or check first."),
    (r"IndexError: list index out of range",
     "A list is shorter than expected. Check len() first; for CSV rows, skip rows with the wrong number of fields."),
    (r"bool is used as a file descriptor|not bool|'bool' object has no attribute|'bool' object is not",
     "An option that needs a value (for example --file PATH) got the value True instead of its value. Either "
     "that option is in FLAGS (only options that never take a value, such as --json, belong there) or its "
     "value is missing on the command line."),
    (r"(ModuleNotFoundError|ImportError)",
     "Use only modules from the Python standard library."),
]


def error_hints(text):
    """Return hint lines for known Python errors in a command's output."""
    hints = [hint for pattern, hint in ERROR_HINTS if re.search(pattern, text)]
    if "Traceback (most recent call last)" in text:
        hints.append("The traceback's last 'File ..., line N' entry in your program is where the error happened. "
                     "Read the lines around it with read_file before changing the code.")
    return "".join("\nHINT: %s" % hint for hint in hints[:3])


def run_command(command, timeout=60, cwd=None, ignore_budget=False):
    """Run a shell command in the workspace (or in cwd) and return its exit code and output.

    ignore_budget is only for the agent's own short final checks, which bring their own time limit.
    """
    command = str(command or "").strip()
    if not command:
        return "ERROR: empty command."
    reason = blocked_reason(command)
    if reason:
        return reason
    try:
        timeout = float(timeout or 60)
    except (TypeError, ValueError):
        timeout = 60.0
    if ignore_budget:
        limit = max(1.0, min(timeout, 120.0))
    else:
        if config.time_left() - 20 < 3:
            return "ERROR: not enough time left to run commands. Finish the task now."
        limit = max(1.0, min(timeout, 120.0, config.time_left() - 20))

    env = {k: v for k, v in os.environ.items() if k not in SECRET_VARS}
    env["PYTHONDONTWRITEBYTECODE"] = "1"   # keep __pycache__ folders out of the workspace
    process = subprocess.Popen(["bash", "-c", command], cwd=cwd or config.WORKSPACE, env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    timed_out = False
    try:
        output, _ = process.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        output, _ = process.communicate()

    text = output.decode("utf-8", "replace")
    if len(text) > OUTPUT_HEAD + OUTPUT_TAIL:
        text = "%s\n...(%d characters omitted)...\n%s" % (
            text[:OUTPUT_HEAD], len(text) - OUTPUT_HEAD - OUTPUT_TAIL, text[-OUTPUT_TAIL:])
    status = "timed out after %d seconds" % limit if timed_out else "exit code %d" % process.returncode
    if timed_out:
        text += ("\nHINT: the command did not finish in time. If it was your program, look for an endless loop "
                 "or a program waiting for input.")
    elif process.returncode != 0:
        hints = error_hints(text)
        if not hints and re.search(r"\.py\b", command):
            hints = ("\nHINT: the program reported an error. If the specification says this command should work, "
                     "find the code that produced this message (search_files for one or two fixed words of it, "
                     "not the parts that come from variables), read that code with read_file, and fix it.")
        text += hints
    return "[%s]\n%s" % (status, text if text else "(no output)")


def run_in_copy(command, timeout=30):
    """Run a command in a temporary copy of the workspace, so a check can never change the real files.

    Returns None if the workspace is too large to copy quickly.
    """
    total = 0
    for folder, dirs, names in os.walk(config.WORKSPACE):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(folder, name))
            except OSError:
                pass
    if total > 100 * 1024 * 1024:
        return None
    copy = tempfile.mkdtemp(prefix="agent_check_")
    try:
        target = os.path.join(copy, "w")
        shutil.copytree(config.WORKSPACE, target, symlinks=True)
        return run_command(command, timeout=timeout, cwd=target)
    finally:
        shutil.rmtree(copy, ignore_errors=True)
