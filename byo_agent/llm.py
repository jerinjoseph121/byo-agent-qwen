"""Client for the OpenAI-compatible /chat/completions endpoint (Python standard library only)."""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from . import config
from .config import log

# Characters per token, used to estimate prompt size. Starts conservative and is
# corrected from the token counts the server reports after each call.
chars_per_token = 3.0


class OutOfTime(Exception):
    """The run's time budget ended while waiting for the model."""


def post_with_deadline(request, seconds):
    """Send the request and return the response body, giving up after `seconds` in total.

    urllib's own timeout applies to each read, so a slowly streamed reply could run far past the
    budget. The request runs in a background thread instead, and is abandoned if it takes too long.
    """
    box = {}

    def work():
        try:
            with urllib.request.urlopen(request, timeout=max(5, seconds)) as response:
                box["body"] = response.read()
        except BaseException as e:      # handed to the caller below
            box["error"] = e

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(max(1, seconds))
    if thread.is_alive():
        raise OutOfTime("The model did not answer within the time left.")
    if "error" in box:
        raise box["error"]
    return box["body"]


class ContextTooLong(Exception):
    """The server rejected the request because prompt plus output exceeded the context window."""

    def __init__(self, detail, prompt_tokens=None):
        Exception.__init__(self, detail)
        self.prompt_tokens = prompt_tokens


def payload_chars(messages, tools):
    return len(json.dumps(messages)) + len(json.dumps(tools))


def estimate_tokens(messages, tools):
    """Rough prompt size in tokens, erring on the high side."""
    return int(payload_chars(messages, tools) / chars_per_token * 1.05) + 10 * len(messages) + 200


def context_error(raw):
    """If an error body says the context window was exceeded, return (True, prompt_tokens or None)."""
    text = raw.lower()
    if not any(key in text for key in ("context length", "maximum context", "context window",
                                       "max_tokens", "too many tokens", "prompt is too long")):
        return False, None
    for pattern in (r"(\d+)\s+(?:tokens\s+)?in the messages", r"prompt contains (\d+)",
                    r"(\d+) input tokens", r"prompt \((\d+) tokens\)"):
        match = re.search(pattern, raw, re.IGNORECASE)
        if match:
            return True, int(match.group(1))
    return True, None


def call_llm(messages, tools, max_tokens):
    """Send the conversation and return (assistant message, finish_reason).

    Busy-server and network errors are retried within the time budget.
    """
    global chars_per_token
    base_url = os.environ.get("OPENAI_BASE_URL", "http://localhost:8000/v1").rstrip("/")
    body = {
        "model": os.environ.get("MODEL", "Qwen/Qwen3-14B"),
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0,
        "max_tokens": int(max_tokens),
        "chat_template_kwargs": {"enable_thinking": False},  # required: Qwen3 thinking mode off
    }
    # Optional extra request fields for development endpoints (never set during grading),
    # e.g. AGENT_EXTRA_BODY='{"reasoning": {"enabled": false}}' for OpenRouter.
    extra = os.environ.get("AGENT_EXTRA_BODY")
    if extra:
        try:
            body.update(json.loads(extra))
        except ValueError:
            log("Ignoring invalid AGENT_EXTRA_BODY")
    payload = json.dumps(body).encode("utf-8")
    sent_chars = payload_chars(messages, tools)

    delay, proxy_errors = 2, 0
    while True:
        remaining = config.time_left()
        if remaining < 5:
            raise OutOfTime("Ran out of time waiting for the model.")
        request = urllib.request.Request(
            base_url + "/chat/completions",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + os.environ.get("QWEN_API_KEY", ""),
                # Some servers behind Cloudflare block Python's default User-Agent (error 1010).
                "User-Agent": config.USER_AGENT,
            },
        )
        try:
            data = json.loads(post_with_deadline(request, remaining - 3).decode("utf-8"))
            choice = data["choices"][0]
            message = choice.get("message") or {}
            config.model_calls += 1
            reasoning = message.get("reasoning_content") or message.get("reasoning")
            if not message.get("content") and not message.get("tool_calls"):
                log("reply without content: finish_reason=%s, completion_tokens=%s, provider=%s, fields=%s, "
                    "reasoning=%r" % (choice.get("finish_reason"), (data.get("usage") or {}).get("completion_tokens"),
                                      data.get("provider"), sorted(message), (reasoning or "")[:300]))
                # Some servers ignore enable_thinking=false and put the whole reply, tool calls included,
                # in a separate reasoning field. Treat that text as the reply.
                if isinstance(reasoning, str) and reasoning.strip():
                    message = dict(message, content=reasoning)
            prompt_tokens = (data.get("usage") or {}).get("prompt_tokens")
            if isinstance(prompt_tokens, int) and prompt_tokens > 500:
                chars_per_token = min(4.5, max(2.2, sent_chars / prompt_tokens))
            return message, choice.get("finish_reason")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            detail = " ".join(re.sub(r"<[^>]+>", " ", raw).split())[:300]
            log("HTTP error %d: %s" % (e.code, detail))
            is_json = raw.lstrip().startswith("{")
            if e.code in (400, 413, 422) and is_json:
                too_long, prompt_tokens = context_error(raw)
                if too_long:
                    raise ContextTooLong(detail, prompt_tokens)
            # Such a page (for example the shared server's "Bad request syntax" or "Request-URI Too Long"
            # page when it is busy) is retried.
            # A non-JSON 400/414/431 page comes from a proxy that garbled the request, not from the model.
            from_proxy = e.code in (400, 414, 431) and not is_json
            proxy_errors += 1 if from_proxy else 0
            if (e.code not in config.RETRYABLE_STATUS and not from_proxy) or proxy_errors > 10:
                raise RuntimeError("Model API error %d: %s" % (e.code, detail))
            if from_proxy:
                # A garbled request at the proxy, not an overloaded model: retry almost at once.
                time.sleep(1)
                continue
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError) as e:
            log("Request failed: %r" % (e,))

        if config.time_left() < delay + 5:
            raise OutOfTime("Model endpoint unavailable until the time ran out.")
        time.sleep(delay)
        delay = min(delay * 2, 16)
