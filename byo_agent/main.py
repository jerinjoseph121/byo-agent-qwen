"""Entry point logic: load the session, run the agent, save the turn, print one JSON result."""
import json
import sys
import time

from . import config
from .config import log
from .loop import run
from .session import Session


def emit(status, message):
    """Print the single JSON result and exit (0 on success, 1 on error)."""
    sys.stdout.write(json.dumps({"status": status, "message": str(message)}) + "\n")
    sys.stdout.flush()
    sys.exit(0 if status == "success" else 1)


def main():
    config.init()
    if not config.PROMPT.strip():
        emit("error", "No prompt was provided.")
    session = Session(config.SESSION_ID)
    log("session: %s" % (session.path or "none"))
    steps = []
    try:
        status, message = run(session, steps)
    except BaseException as e:
        log("Fatal error: %r" % (e,))
        status, message = "error", "Agent failed: %s" % e
    session.add_turn(config.PROMPT, steps, status, message)
    log("done: %s after %d model calls in %.1fs" % (status, config.model_calls, time.time() - config.START_TIME))
    emit(status, message)
