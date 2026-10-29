"""Tool registry: the definitions sent to the model and the functions that run them."""
import inspect

from .commands import run_command
from .csvtools import (calculate, csv_add_to_value, csv_column_stats, csv_delete_rows,
                       csv_row_sum, csv_update_value)
from .files import (append_file, copy_file, delete_file, edit_file, list_files, read_file,
                    replace_lines, search_files, write_file)

# Maps each tool name the model can call to the function that runs it.
HANDLERS = {
    "list_files": list_files,
    "read_file": read_file,
    "search_files": search_files,
    "write_file": write_file,
    "append_file": append_file,
    "edit_file": edit_file,
    "replace_lines": replace_lines,
    "delete_file": delete_file,
    "copy_file": copy_file,
    "run_command": run_command,
    "csv_column_stats": csv_column_stats,
    "csv_row_sum": csv_row_sum,
    "csv_update_value": csv_update_value,
    "csv_add_to_value": csv_add_to_value,
    "csv_delete_rows": csv_delete_rows,
    "calculate": calculate,
}

# Tools that change files. Used to catch a "success" that no tool confirmed.
CHANGING_TOOLS = {"write_file", "append_file", "edit_file", "replace_lines", "delete_file", "copy_file",
                  "csv_update_value", "csv_delete_rows", "csv_add_to_value"}

# Alternative argument names the model sometimes uses, mapped to each parameter.
ARG_ALIASES = {
    "path": ("file_path", "filepath", "filename", "file", "directory", "dir", "csv_path", "input_path"),
    "source": ("src", "source_path", "from_path", "path", "file_path", "input_path"),
    "destination": ("dest", "destination_path", "to_path", "to", "output_path", "new_path", "target_path"),
    "output_path": ("output", "output_file", "save_as", "save_path", "destination", "new_path"),
    "content": ("text", "contents", "data", "code"),
    "old_text": ("old", "old_string", "find", "search", "original"),
    "new_text": ("new", "new_string", "replace", "replacement", "content", "text", "new_content"),
    "pattern": ("query", "text", "search", "regex", "keyword"),
    "command": ("cmd", "shell", "script"),
    "start_line": ("start", "from_line", "offset"),
    "end_line": ("end", "to_line"),
    "column": ("column_name", "field"),
    "match_column": ("id_column", "key_column", "where_column"),
    "match_value": ("id", "id_value", "key", "where_value", "row_id"),
    "new_value": ("value",),
    "amount": ("value", "delta", "add", "increment", "number"),
    "condition": ("operator", "op"),
    "columns": ("column_names", "fields"),
}


def tool(name, description, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required}}}


PATH = {"type": "string", "description": "File path relative to the workspace."}
OUTPUT_PATH = {"type": "string", "description": "Where to save the result. Omit to overwrite the original file."}
TEXT = {"type": "string"}

# Tool definitions sent to the model (OpenAI function-calling format).
TOOLS = [
    tool("list_files", "List all files (recursively) under a directory, with their line counts and sizes.",
         {"path": {"type": "string", "description": "Directory relative to the workspace. Use '.' for the root."}},
         []),
    tool("read_file", "Read a text file. Small files are returned whole. For large files give start_line and "
                      "end_line (1-based, inclusive) to read part of the file.",
         {"path": PATH, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"]),
    tool("search_files", "Find lines containing a text (or a regular expression) in one file or in all files "
                         "under a folder. Returns matching lines with file names and line numbers.",
         {"pattern": {"type": "string", "description": "Text to find. Plain text unless regex is true."},
          "path": {"type": "string", "description": "File or folder to search. Default: the whole workspace."},
          "regex": {"type": "boolean"},
          "ignore_case": {"type": "boolean", "description": "Default true."},
          "context": {"type": "integer", "description": "Neighbouring lines to show before and after each match "
                                                        "(0-3, default 0)."},
          "max_results": {"type": "integer", "description": "Default 40."}},
         ["pattern"]),
    tool("write_file", "Create or overwrite a file with exactly the given content. "
                       "Missing parent directories are created automatically.",
         {"path": PATH,
          "content": {"type": "string",
                      "description": "Exact file content. Use \\n between lines. Use an empty string for an empty file."}},
         ["path", "content"]),
    tool("append_file", "Add text to the end of a file. Use it to write a long file in several parts.",
         {"path": PATH, "content": TEXT}, ["path", "content"]),
    tool("edit_file", "Replace an exact piece of text in a file with new text, for example to fix a bug. "
                      "old_text must match the file exactly and occur once.",
         {"path": PATH, "old_text": TEXT, "new_text": TEXT,
          "replace_all": {"type": "boolean", "description": "Replace every occurrence. Default false."}},
         ["path", "old_text", "new_text"]),
    tool("replace_lines", "Replace a range of lines in a file (line numbers as shown by read_file) with new text. "
                          "Use an empty new_text to delete the lines.",
         {"path": PATH, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}, "new_text": TEXT},
         ["path", "start_line", "end_line", "new_text"]),
    tool("delete_file", "Delete one file. Never deletes directories.", {"path": PATH}, ["path"]),
    tool("copy_file", "Save an exact copy of a file (for example a CSV table) to a new path. "
                      "Missing parent directories are created automatically.",
         {"source": PATH, "destination": PATH}, ["source", "destination"]),
    tool("run_command", "Run a shell command in the workspace and get its exit code and output, for example "
                        "'python3 tally.py count' to test a program. Do not use it to delete or change files "
                        "that the task does not ask for. Put scratch files only in /tmp.",
         {"command": TEXT, "timeout": {"type": "integer", "description": "Seconds, default 60, at most 120."}},
         ["command"]),
    tool("csv_column_stats", "Compute the count, total (sum), average (mean), minimum and maximum "
                             "of a numeric column in a CSV file.",
         {"path": PATH, "column": {"type": "string", "description": "Column name from the header row."}},
         ["path", "column"]),
    tool("csv_row_sum", "Add the numeric values of selected columns in the row identified by a column value "
                        "(for example the row whose id is 1).",
         {"path": PATH,
          "match_column": {"type": "string", "description": "Column used to find the row, e.g. id."},
          "match_value": {"type": "string", "description": "Value that identifies the row, e.g. 1."},
          "columns": {"type": "array", "items": {"type": "string"},
                      "description": "Names of the columns whose values should be added."}},
         ["path", "match_column", "match_value", "columns"]),
    tool("csv_update_value", "Replace the value of one column in the row(s) identified by a column value "
                             "(for example the row with ID 104) and save the CSV.",
         {"path": PATH,
          "match_column": {"type": "string", "description": "Column used to find the row, e.g. id."},
          "match_value": {"type": "string", "description": "Value that identifies the row, e.g. 104."},
          "column": {"type": "string", "description": "Column to change."},
          "new_value": {"type": "string", "description": "New value for that column."},
          "output_path": OUTPUT_PATH},
         ["path", "match_column", "match_value", "column", "new_value"]),
    tool("csv_add_to_value", "Increase a numeric value by an amount in the row(s) identified by a column value "
                             "(for example add 259 to the amount of the row whose customer is Ana) and save the CSV.",
         {"path": PATH,
          "match_column": {"type": "string", "description": "Column used to find the row, e.g. customer."},
          "match_value": {"type": "string", "description": "Value that identifies the row, e.g. Ana."},
          "column": {"type": "string", "description": "Numeric column to increase."},
          "amount": {"type": "string", "description": "Number to add. Use a negative number to subtract."},
          "output_path": OUTPUT_PATH},
         ["path", "match_column", "match_value", "column", "amount"]),
    tool("csv_delete_rows", "Remove every row whose column meets a condition and save the remaining rows.",
         {"path": PATH,
          "column": {"type": "string", "description": "Column to test."},
          "condition": {"type": "string",
                        "enum": ["equals", "not_equals", "greater_than", "less_than",
                                 "greater_or_equal", "less_or_equal", "contains"]},
          "value": {"type": "string", "description": "Value to compare with."},
          "output_path": OUTPUT_PATH},
         ["path", "column", "condition", "value"]),
    tool("calculate", "Evaluate an arithmetic expression exactly, e.g. '(120 + 180) / 2'.",
         {"expression": TEXT}, ["expression"]),
    tool("finish", "Call exactly once when the task is complete or cannot be completed.",
         {"status": {"type": "string", "enum": ["success", "error"]},
          "message": {"type": "string",
                      "description": "For a question, the complete answer with every requested fact or number. "
                                     "Otherwise a short confirmation or the reason for the error."}},
         ["status", "message"]),
]

TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


def run_tool(name, args):
    """Run a tool by name and return its result. Errors are returned as text for the model."""
    handler = HANDLERS.get(name)
    if handler is None:
        return "ERROR: unknown tool %r. Available tools: %s" % (name, ", ".join(sorted(TOOL_NAMES)))
    accepted = inspect.signature(handler).parameters
    args = dict(args)
    # Accept common alternative argument names and drop arguments the tool does not take.
    for param in accepted:
        if param not in args:
            for alias in ARG_ALIASES.get(param, ()):
                if alias in args and alias not in accepted:
                    args[param] = args[alias]
                    break
    args = {key: value for key, value in args.items() if key in accepted}
    missing = [p.name for p in accepted.values() if p.default is p.empty and p.name not in args]
    if missing:
        return "ERROR: missing argument(s) for %s: %s." % (name, ", ".join(missing))
    try:
        return str(handler(**args))
    except Exception as e:
        return "ERROR: %s" % e
