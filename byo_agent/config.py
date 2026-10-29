"""Run-wide settings and state: limits, the workspace, the prompt and the session ID."""
import os
import sys
import time

# Time per run. Long tasks (a workspace with TASK.md, or a long task description) get about 280 seconds
# so they finish inside a 300-second limit. Short tasks get about 165 seconds (a 180-second limit).
LONG_TASK_BUDGET = 280
SHORT_TASK_BUDGET = 165
TIME_BUDGET = SHORT_TASK_BUDGET
MAX_MODEL_CALLS = 110        # stays under a limit of 120 successful model calls per run
WRAP_UP_CALLS = 100          # after this many calls the model is told to finish up
WRAP_UP_SECONDS = 50         # ... or when this little time is left
CONTEXT_LIMIT = 40960        # model context window, prompt plus output tokens
MAX_OUTPUT_TOKENS = 8192     # largest reply we ask for (enough for a full program)
MIN_OUTPUT_TOKENS = 1024
PROMPT_TOKEN_TARGET = 28000  # older tool output is trimmed to keep the prompt under this

HISTORY_CHAR_LIMIT = 40000   # earlier turns of a session sent to the model
STORED_RESULT_LIMIT = 4000   # tool results are truncated to this when a turn is saved
FULL_READ_LIMIT = 40000      # read_file returns a whole file up to this many characters
PART_READ_LIMIT = 20000      # ... and at most this much for a partial read

USER_AGENT = "byo-agent-qwen/1.0"
RETRYABLE_STATUS = (408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529)

START_TIME = time.time()
WORKSPACE = ""
PROMPT = ""
SESSION_ID = ""
model_calls = 0              # successful chat completions so far in this run


def init(argv=None, cwd=None):
    """Read the prompt and session ID from the command line and fix the workspace path."""
    global WORKSPACE, PROMPT, SESSION_ID, START_TIME, model_calls, TIME_BUDGET
    argv = sys.argv if argv is None else argv
    WORKSPACE = os.path.realpath(cwd or os.getcwd())   # the agent runs from the task directory
    PROMPT = argv[1] if len(argv) > 1 else ""
    SESSION_ID = argv[2].strip() if len(argv) > 2 else ""
    START_TIME = time.time()
    model_calls = 0
    long_task = os.path.isfile(os.path.join(WORKSPACE, "TASK.md")) or len(PROMPT) > 1500
    TIME_BUDGET = LONG_TASK_BUDGET if long_task else SHORT_TASK_BUDGET
    # Testing only: AGENT_TIME_BUDGET gives a slow test endpoint the time a fast one would need.
    # Leave it unset in normal use.
    if os.environ.get("AGENT_TIME_BUDGET", "").isdigit():
        TIME_BUDGET = max(30, min(int(os.environ["AGENT_TIME_BUDGET"]), 3600))


def time_left():
    """Seconds remaining in this run's time budget."""
    return TIME_BUDGET - (time.time() - START_TIME)


def log(*args):
    """Write diagnostic output to stderr so stdout stays clean JSON."""
    print(*args, file=sys.stderr, flush=True)
