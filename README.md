# byo-agent-qwen

**Build your own agent on Qwen.** A tool-calling AI agent built from the ground up around **Qwen3-14B**, with no agent framework or SDK. You give it a task in plain English and it works inside a folder (the workspace) by calling tools: reading and writing files, editing CSV tables, running shell commands, and searching large logs.

It can:

- **Handle files:** create, delete and read files, answer questions about them, and summarize several files at once.
- **Work with CSV data:** totals, averages, sums within a row, updates by ID and row deletion, with exact decimal math.
- **Hold a conversation:** remember earlier turns of a session, even though every turn is a separate process.
- **Write and test programs:** build a whole command-line program from a written specification, then run it, find bugs and fix them.
- **Debug from logs:** follow a failed request through several large service logs to its root cause and write a JSON report.

The agent talks to any OpenAI-compatible `/chat/completions` endpoint and uses only the Python standard library.

## Quick start

```bash
git clone https://github.com/jerinjoseph121/byo-agent-qwen.git
cd my-task-folder                                   # the agent works in the current folder

export OPENAI_BASE_URL="https://your-endpoint/v1"   # any OpenAI-compatible server
export MODEL="Qwen/Qwen3-14B"
export QWEN_API_KEY="<your key>"

bash /path/to/byo-agent-qwen/run_agent.sh "Create notes.txt with three lines: red, green, blue"
bash /path/to/byo-agent-qwen/run_agent.sh "What is the total of the revenue column in sales.csv?" "session-1"
```

The second argument is an optional session ID. Calls with the same ID continue the same conversation.

The program prints exactly one JSON line to stdout, for example `{"status": "success", "message": "..."}`, and exits with 0 on success or 1 on error. Progress logs go to stderr.

**Requirements:** Python 3.8 or later and bash. No packages need to be installed (`requirements.txt` is empty on purpose). Tested on Ubuntu 26.04 (WSL 2) with Python 3.14, and the offline tests also pass on Python 3.8.

## How it works

```
run_agent.sh -> agent.py -> byo_agent/main.py
                               |
                               |-- load the session history (if a session ID was given)
                               |-- loop.run():
                               |      1. build the messages: system prompt, earlier turns, current request
                               |      2. trim old tool output if the prompt is getting too large
                               |      3. ask the model (llm.py)
                               |      4. run the tool calls it returns (tools.py -> files.py,
                               |         commands.py, csvtools.py) and add the results
                               |      5. repeat until the model calls finish
                               |      6. for long tasks, review the finish before accepting it
                               |-- save the turn to the session file
                               '-- print one JSON result and exit
```

## Project structure

```
byo-agent-qwen/
├── run_agent.sh            Entry point: bash run_agent.sh "<prompt>" ["<session_id>"]
├── agent.py                Starts the agent (imports byo_agent.main)
├── requirements.txt        Empty: only the Python standard library is used
├── keys.env.example        Template for the settings used by the live test runner
├── byo_agent/              The agent itself
│   ├── main.py             Reads the arguments, runs one turn, prints the JSON result
│   ├── config.py           Limits (time, model calls, context size) and run state
│   ├── llm.py              Client for /chat/completions: retries, deadlines, context errors
│   ├── loop.py             The agent loop, context trimming, reminders and final review checks
│   ├── prompts.py          System prompt and reminder messages
│   ├── tools.py            Tool definitions sent to the model and the dispatcher
│   ├── files.py            File tools: list, read, search, write, append, edit, replace lines
│   ├── commands.py         run_command: runs shell commands in the workspace with a timeout
│   ├── csvtools.py         Exact CSV tools (totals, averages, updates, row deletion)
│   └── session.py          Conversation history saved between runs
└── tests/
    ├── test_tools.py       Offline unit tests (no model or key needed)
    ├── cases.json          43 end-to-end cases for file, CSV and conversation tasks
    ├── run_tests.py        Runs cases.json against a real model endpoint
    ├── run_tasks.py        Runs and scores the programming and debugging tasks
    ├── tasks/              Task texts, data and log generators, and reference answers for run_tasks.py
    └── live/               Optional helper for running the test suites on another machine
```

## The parts in detail

### `main.py` and `config.py`: one run

`main.py` reads the prompt and session ID, loads the session, runs the loop, saves the turn and prints the result. Any crash is caught and reported as an error JSON, so stdout always has exactly one JSON line.

`config.py` holds the limits:

- **Time:** long tasks (a workspace with `TASK.md`, or a task description over 1500 characters) stop after about 280 seconds, inside a 300-second limit. Short tasks stop after about 165 seconds, inside a 180-second limit. Near the end the model is told to save its work and finish.
- **Model calls:** at most 110 per run.
- **Context:** a 40,960-token window, with the prompt kept under about 28,000 tokens so the reply has room.

### `llm.py`: talking to the model

- Sends the conversation and tool definitions with `temperature: 0` and `"chat_template_kwargs": {"enable_thinking": false}` (Qwen3's thinking mode is turned off).
- Retries busy-server errors (429, 5xx) with growing waits, and garbled proxy error pages (non-JSON 400, 414, 431) almost at once.
- Gives every request a hard total deadline. Python's urllib timeout only limits each read, so a slowly streamed reply could otherwise run past the time budget.
- Turns a "context too long" error into an exception the loop handles by trimming and retrying.
- Uses the token counts the server reports to keep its prompt-size estimate accurate.
- Sends a custom User-Agent header, because some servers behind Cloudflare block Python's default one.

### `loop.py`: the agent loop

Each step builds the messages, trims old tool output if needed (the newest results are always kept), calls the model and runs the tool calls it returns. It also handles the model's common slips:

- **Tool calls written as text:** recovered from `<tool_call>` tags or plain JSON.
- **Empty, cut-off or plain-text replies:** answered with a short reminder. If a reply was cut off while writing a file, the complete lines that arrived are saved and the model continues from there with `append_file`. A write_file call that is not valid JSON is recovered and saved.
- **Loops:** if the same call keeps giving the same result, the model is told to change approach.
- **False success:** if every attempt to change a file failed, a "success" is turned into an error.

For long tasks, a `finish` call is reviewed before it is accepted. The review sends the two most important open problems back to the model (up to six rounds while time is left):

- Output files named in the task that do not exist yet.
- **Programs:**
  - test commands that still fail
  - commands that a later edit broke
  - bugs visible in the test results (an option ignored or read from the wrong place, text compared with the wrong capitalization when the task says case does not matter, output lines that do not match a format written in the task such as `YYYY-MM<TAB>AMOUNT`, missing half-up rounding)
  - commands from the task that were never run
- **JSON reports:** values that are empty, zero, not found in any file, seen only next to a search match, stored in the logs under a different field name, contradicted by a later line about the same ID (for a "final" state), or taken from the wrong line (for an event's timestamp). It also lists report IDs that were never searched for.

Two safety nets protect a program the model is writing:

- After every edit, the earlier passing test commands are re-run in a temporary copy of the workspace, and any changed output is shown to the model at once.
- The last versions of the program are kept. Before the run ends, if a late edit changed results the model had already checked, the best earlier version is put back.

### `prompts.py`: the instructions

The system prompt has sections for general rules, files, CSV files, programming tasks, debugging tasks and multi-turn conversations. The programming section includes a small command-line parsing helper for the model to copy, because the model often got option parsing wrong with argparse. Below the system prompt are the short reminder messages the loop sends.

### `tools.py`: the tool list

Defines the tools in the OpenAI function-calling format and maps each name to its Python function. `run_tool()` accepts common alternative argument names (for example `file_path` for `path`), drops unknown arguments, reports missing ones, and turns exceptions into `ERROR: ...` text the model can read.

| Tool | Purpose |
| --- | --- |
| `list_files` | All files under a folder, with line counts and sizes |
| `read_file` | A whole file, or a range of numbered lines for large files and code |
| `search_files` | Lines matching text or a regex, marked `MATCH` and `context`. Hidden error lines are listed when there are too many matches |
| `write_file`, `append_file` | Create or overwrite a file, or add to it. Python files get a syntax check |
| `edit_file` | Replace an exact piece of text (also matches text that differs only in indentation) |
| `replace_lines` | Replace a range of lines by number, then show the edited lines with their new numbers |
| `delete_file`, `copy_file` | Delete one file (never a folder), or copy a file |
| `run_command` | Run a shell command in the workspace with a timeout, with hints for common Python errors |
| `csv_column_stats`, `csv_row_sum`, `csv_update_value`, `csv_add_to_value`, `csv_delete_rows`, `calculate` | Exact CSV work and arithmetic with `Decimal`, so the model never does math itself |
| `finish` | End the turn with a status and a message |

### `files.py`, `commands.py`, `csvtools.py`: the tool code

- **Paths:** every path is resolved inside the workspace, and paths that escape it are rejected.
- **Safe edits:** an edit that would break a Python file that compiled before is first retried with corrected indentation, and otherwise refused, leaving the file unchanged.
- **`run_command`:** removes API keys from the environment, refuses to delete files outside `/tmp`, and kills the whole process group on a timeout.
- **CSV tools:** keep the file's delimiter and line endings and leave untouched rows and columns exactly as they were.

### `session.py`: memory between runs

Each session is a JSON file in `$TMPDIR/agent_sessions/`, named from the session ID plus a hash of the workspace path, so different sessions never share history (`AGENT_SESSION_DIR` overrides the folder). Earlier turns are replayed to the model, trimmed to a size limit with the oldest tool details dropped first. A turn that only gives information to remember is acknowledged without touching any file, and the newest correction wins.

## Testing

```bash
# Offline unit tests (no key needed)
python3 tests/test_tools.py

# End-to-end tests against a real endpoint (set OPENAI_BASE_URL, MODEL and QWEN_API_KEY first)
python3 tests/run_tests.py                        # all 43 file, CSV and conversation cases
python3 tests/run_tests.py session                # only cases whose name contains "session"
python3 tests/run_tasks.py programming --seed 1   # write tally.py from a spec, scored on 18 requirements
python3 tests/run_tasks.py programming --seed 101 # the same task cut to its first 12 requirements
python3 tests/run_tasks.py debugging --seed 2     # trace a failed checkout, scored on 18 report keys
python3 tests/run_tasks.py all --repeat 3         # several seeds of each
```

- **`run_tests.py`** runs every case in a fresh temporary workspace with a new agent process per turn. It checks the JSON output, the answer text, the final files and CSV rows, and that unrelated files were left alone.
- **`run_tasks.py`** builds a task workspace (`TASK.md`, data or logs, and an unrelated folder), runs the agent once and scores the result.
  - Programming: the agent writes `tally.py`, a command-line tool that summarizes a messy 40,000-row CSV of transactions. It is compared with `tests/tasks/tally_ref.py` on the given data, a second generated file and small edge-case files.
  - Debugging: the agent traces a failed checkout through four service logs (gateway, orders, inventory, payments). Each key of its `REPORT.json` is compared with the answer known to the log generator, including its JSON type.
  - Each seed generates different data, logs and IDs, so the agent cannot memorize answers.
- **`tests/live/watcher.py`** is an optional helper. It waits for small request files and runs the test suites on a machine that can reach the model endpoint. It reads the endpoint and keys from `keys.env` (see `keys.env.example`) and never prints them.

## Results

On these tests, with Qwen3-14B:

| Test | Result |
| --- | --- |
| File, CSV and conversation cases | 43/43 in every final run |
| Programming task, 12 requirements | Usually 11 or 12 out of 12 |
| Debugging task, 18 report keys | Usually 16 to 18 out of 18 |
| Programming task, 18 requirements | 15 to 17 out of 18 on a fast endpoint |

Scores vary from run to run because the model writes a new program each time, and they depend heavily on how fast the endpoint answers.

## Known limitations

- A 14B model does not always follow a long specification. In big tasks some late requirements are not tested before time runs out.
- In debugging it occasionally names an earlier, unrelated error as the root cause.
- On a slow endpoint (tens of seconds per reply) a large programming task gets too few model calls to finish.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `OPENAI_BASE_URL` | Base URL of the OpenAI-compatible endpoint |
| `MODEL` | Model name, for example `Qwen/Qwen3-14B` |
| `QWEN_API_KEY` | API key for the endpoint |
| `AGENT_SESSION_DIR` | Optional folder for session history |
| `AGENT_EXTRA_BODY` | Optional JSON merged into every request (for testing on other providers, for example `{"reasoning": {"enabled": false}}` on OpenRouter) |
| `AGENT_TIME_BUDGET` | Optional time budget in seconds, for testing on a slow endpoint |
