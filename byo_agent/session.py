"""Conversation history for multi-turn sessions, saved as JSON between separate runs."""
import hashlib
import json
import os
import re
import tempfile

from . import config
from .config import log


class Session:
    """History for one session ID, stored outside the workspace.

    The file name combines the session ID with a hash of the workspace path, so the
    same ID used in a different task directory starts a new conversation.
    """

    def __init__(self, session_id):
        self.session_id = session_id
        self.turns = []
        self.path = None
        if not session_id:
            return
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:64]
        workspace_hash = hashlib.sha256(config.WORKSPACE.encode("utf-8")).hexdigest()[:16]
        name = "session_%s_%s.json" % (safe_id, workspace_hash)
        for folder in self.storage_dirs():
            try:
                os.makedirs(folder, exist_ok=True)
                if os.access(folder, os.W_OK):
                    self.path = os.path.join(folder, name)
                    break
            except OSError:
                continue
        self.load()

    @staticmethod
    def storage_dirs():
        dirs = []
        if os.environ.get("AGENT_SESSION_DIR"):
            dirs.append(os.environ["AGENT_SESSION_DIR"])
        dirs.append(os.path.join(tempfile.gettempdir(), "agent_sessions"))
        dirs.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".sessions"))
        return dirs

    def load(self):
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("session_id") == self.session_id and data.get("workspace") == config.WORKSPACE:
                self.turns = data.get("turns", [])
        except (OSError, ValueError) as e:
            log("Could not read session history: %r" % (e,))

    def history_messages(self, char_limit=None):
        """Earlier turns as chat messages, oldest first, trimmed to char_limit characters."""
        limit = config.HISTORY_CHAR_LIMIT if char_limit is None else char_limit
        turns = [dict(t) for t in self.turns]
        size = lambda: len(json.dumps(turns))
        # Drop tool details from the oldest turns first, then whole turns, if history is too long.
        for turn in turns:
            if size() <= limit:
                break
            turn["steps"] = []
        while turns and size() > limit:
            turns.pop(0)
        messages = []
        for turn in turns:
            messages.append({"role": "user", "content": turn["prompt"]})
            messages.extend(turn.get("steps", []))
            final = turn.get("message", "")
            if turn.get("status") == "error":
                final = "(Could not complete the request.) " + final
            messages.append({"role": "assistant", "content": final})
        return messages

    def add_turn(self, prompt, steps, status, message):
        """Record a finished turn and save the session file."""
        if not self.path:
            return
        self.turns.append({
            "prompt": prompt,
            "steps": clean_steps(steps, len(self.turns)),
            "status": status,
            "message": message,
        })
        data = {"session_id": self.session_id, "workspace": config.WORKSPACE, "turns": self.turns}
        try:
            tmp_path = self.path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp_path, self.path)
        except OSError as e:
            log("Could not save session history: %r" % (e,))


def clean_steps(steps, turn_index):
    """Prepare a turn's tool calls and results for storage.

    Keeps only tool calls that have a result (so finish is dropped), truncates long
    results, and gives call IDs a turn prefix so they stay unique across turns.
    Plain-text replies and reminders are left out.
    """
    answered = {m["tool_call_id"] for m in steps if m.get("role") == "tool"}
    cleaned = []
    for message in steps:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            calls = [dict(c) for c in message["tool_calls"] if c["id"] in answered]
            if not calls:
                continue
            for call in calls:
                call["id"] = "t%d_%s" % (turn_index, call["id"])
                arguments = call.get("function", {}).get("arguments", "")
                if len(arguments) > config.STORED_RESULT_LIMIT:
                    call["function"] = dict(call["function"], arguments=shorten_arguments(arguments))
            cleaned.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
        elif message.get("role") == "tool":
            content = message["content"]
            if len(content) > config.STORED_RESULT_LIMIT:
                content = content[:config.STORED_RESULT_LIMIT] + "\n...(truncated)"
            cleaned.append({"role": "tool", "tool_call_id": "t%d_%s" % (turn_index, message["tool_call_id"]),
                            "content": content})
    return cleaned


def shorten_arguments(arguments, keep=300):
    """Replace long string values in a tool call's JSON arguments with a short note."""
    try:
        data = json.loads(arguments)
    except ValueError:
        return json.dumps({"note": "arguments omitted (%d characters)" % len(arguments)})
    if not isinstance(data, dict):
        return json.dumps({"note": "arguments omitted"})
    for key, value in data.items():
        if isinstance(value, str) and len(value) > keep:
            data[key] = "%s...[%d more characters omitted]" % (value[:keep], len(value) - keep)
    return json.dumps(data)
