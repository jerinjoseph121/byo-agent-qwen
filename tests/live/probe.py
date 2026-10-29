#!/usr/bin/env python3
"""Send one tool-calling request to the configured endpoint and print the raw reply (development aid)."""
import json
import os
import sys
import urllib.request

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from byo_agent import config, prompts, tools  # noqa: E402

config.init(["agent.py", "probe", ""], cwd=os.getcwd())
body = {
    "model": os.environ.get("MODEL", "Qwen/Qwen3-14B"),
    "messages": [{"role": "system", "content": prompts.SYSTEM_PROMPT},
                 {"role": "user", "content": "Request: Create hello.py that prints hello. Write it with write_file."}],
    "tools": tools.TOOLS, "tool_choice": "auto", "temperature": 0, "max_tokens": 1024,
    "chat_template_kwargs": {"enable_thinking": False},
}
if os.environ.get("AGENT_EXTRA_BODY"):
    body.update(json.loads(os.environ["AGENT_EXTRA_BODY"]))
request = urllib.request.Request(os.environ["OPENAI_BASE_URL"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": config.USER_AGENT,
                                          "Authorization": "Bearer " + os.environ.get("QWEN_API_KEY", "")})
try:
    with urllib.request.urlopen(request, timeout=120) as response:
        print(json.dumps(json.loads(response.read().decode()), indent=1)[:4000])
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode()[:2000])
