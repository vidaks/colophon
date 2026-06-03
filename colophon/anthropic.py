"""Haiku adjudication.

Deployed: direct Anthropic Messages API (`ANTHROPIC_API_KEY` from the vault via
env), structured output via a forced tool. Dev: the `claude` CLI (existing Claude
Code auth, no key needed) with `--json-schema`. The model only *selects* among
provided candidates and never originates an identifier — the caller validates the
chosen id against the candidate set (no hallucinated ids).
"""
import json
import os
import subprocess
import urllib.request

MODEL = "claude-haiku-4-5-20251001"
API = "https://api.anthropic.com/v1/messages"


def have_key():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def adjudicate(system, user, schema):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return _via_api(system, user, schema, key.strip())
    return _via_cli(system, user, schema)


def _via_api(system, user, schema, key):
    tool = {"name": "submit", "description": "Submit the structured decision", "input_schema": schema}
    body = {
        "model": MODEL, "max_tokens": 1024, "system": system,
        "messages": [{"role": "user", "content": user}],
        "tools": [tool], "tool_choice": {"type": "tool", "name": "submit"},
    }
    req = urllib.request.Request(
        API, data=json.dumps(body).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    resp = json.load(urllib.request.urlopen(req, timeout=120))
    for block in resp.get("content", []):
        if block.get("type") == "tool_use":
            return block["input"]
    raise RuntimeError("no tool_use block in Haiku response")


def _via_cli(system, user, schema):
    prompt = system + "\n\n" + user + "\n\nReturn ONLY the structured decision."
    p = subprocess.run(
        ["claude", "-p", prompt, "--model", MODEL, "--json-schema", json.dumps(schema),
         "--output-format", "json"],
        capture_output=True, text=True, timeout=180,
    )
    try:
        env = json.loads(p.stdout)
    except Exception:
        raise RuntimeError(f"claude CLI non-JSON: {p.stdout[:150]} {p.stderr[:150]}")
    so = env.get("structured_output")
    if isinstance(so, dict):
        return so
    r = env.get("result")
    if isinstance(r, str) and r.strip():
        return json.loads(r)
    raise RuntimeError(f"no structured_output from claude CLI (turns={env.get('num_turns')})")
