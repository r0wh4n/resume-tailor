#!/usr/bin/env python3
"""Use a locally installed Claude Code or Codex CLI as the model provider.

This is how a self-hoster runs the app on a Claude or ChatGPT subscription instead of
a metered API key: the CLI on this machine is already signed in, so there is no key to
paste and nothing metered per résumé.

It only works when the app and the CLI are on the SAME machine, so it is off unless
LOCAL_CLI is set explicitly, and a hosted deployment must never set it - a server has
no access to a visitor's local CLI, and enabling it there would spend the operator's
own subscription on every request.

Safety notes, because this feeds untrusted text (job postings, uploaded résumés) to an
agent that can otherwise run commands:
  * every tool is disabled with --disallowedTools / sandboxed exec, so the worst a
    prompt injection can do is produce silly résumé text
  * the prompt goes in on stdin, never argv - no shell, no quoting, no length limit
  * subprocess is called with a list and shell=False
"""
import json
import os
import re
import shutil
import subprocess

CLI = os.environ.get("LOCAL_CLI", "").strip().lower()
TIMEOUT = int(os.environ.get("LOCAL_CLI_TIMEOUT", 120))
# Haiku with thinking off answers this workload in ~11s. Override if you want more.
MODEL = os.environ.get("LOCAL_CLI_MODEL", "claude-haiku-4-5")
# Writing is forgiving; grading is not. Haiku writes fine but marks a career-change
# resume far too generously, so the screening pass gets a stronger model by default.
JUDGE_MODEL = os.environ.get("LOCAL_CLI_JUDGE_MODEL", "claude-sonnet-4-5")

# Claude Code exposes these; naming them explicitly is what keeps an injected
# "run this command" in a job posting from being anything but text.
NO_TOOLS = ["Bash", "Read", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch",
            "Glob", "Grep", "Task", "TodoWrite"]

FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.M)

# The CLI is an interactive coding agent by default, and all of that is dead weight for
# one JSON answer. Measured on a real résumé prompt:
#   default                     135s  (13,810 thinking tokens to emit one object)
#   MAX_THINKING_TOKENS=0        11s
# MCP servers and skills are skipped too - every configured server's tool definitions
# would otherwise be loaded into context on every single call.
FAST_ENV = {"MAX_THINKING_TOKENS": "0", "DISABLE_TELEMETRY": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


def available():
    return bool(CLI) and bool(shutil.which(CLI))


def _json_from(text):
    """CLIs are chattier than an API. Strip fences, then fall back to the outermost
    {...} span so a stray sentence around the JSON does not fail the request."""
    text = FENCE.sub("", (text or "").strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        return json.loads(text[i:j + 1])
    raise ValueError(f"{CLI} did not return JSON: {text[:200]}")


def _run(argv, prompt):
    p = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                       timeout=TIMEOUT, shell=False, env={**os.environ, **FAST_ENV})
    if p.returncode != 0:
        raise RuntimeError(f"{argv[0]} exited {p.returncode}: "
                           f"{(p.stderr or p.stdout or '')[:300]}")
    return p.stdout


# The API providers enforce the schema server-side; the CLI does not, so the model is
# free to answer in prose. It does exactly that when the candidate is a poor fit -
# "this is a complete mismatch" instead of the object. Say plainly that a bad fit is
# still expressed IN the JSON, which is what the notes field is for.
JSON_ONLY = """

OUTPUT FORMAT - this overrides any instinct to explain yourself.
Reply with ONE JSON object and nothing else: no preamble, no commentary, no code fence,
no apology, not a single character before the opening brace or after the closing one.
It must validate against this JSON Schema:

If the candidate is a poor fit for this role, that is a normal outcome and it is still
expressed as JSON - a short resume plus every unmet requirement listed in "notes".
Refusing, or explaining the mismatch in prose, is a failed response."""


def chat(system, user, schema, model=None, _retry=True):
    """Same contract as the API providers: schema-shaped dict out.

    Returns (data, usage) where usage is {calls, tok_in, tok_out}.
    """
    prompt = system + JSON_ONLY + json.dumps(schema) + f"\n\n{user}"

    if CLI == "claude":
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"mcpServers": {}}, f)
            mcp = f.name
        argv = ["claude", "-p", "--output-format", "json", "--max-turns", "1",
                "--strict-mcp-config", "--mcp-config", mcp,
                "--disable-slash-commands",
                "--model", model or MODEL,
                "--disallowedTools", *NO_TOOLS]
        try:
            env = json.loads(_run(argv, prompt))
        finally:
            os.unlink(mcp)
        if env.get("is_error"):
            raise RuntimeError(f"claude: {str(env.get('result'))[:300]}")
        u = env.get("usage") or {}
        usage = {
            "calls": 1,
            "tok_in": (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                       + u.get("cache_creation_input_tokens", 0)),
            "tok_out": u.get("output_tokens", 0),
        }
        try:
            return _json_from(env.get("result", "")), usage
        except ValueError:
            if not _retry:
                raise
            # One sharper retry beats failing the request; without schema enforcement
            # this is the only backstop.
            data, again = chat(
                system + "\n\nYour previous reply was prose, which is invalid. "
                         "Emit only the JSON object.",
                user, schema, model, _retry=False)
            again["calls"] += usage["calls"]
            again["tok_in"] += usage["tok_in"]
            again["tok_out"] += usage["tok_out"]
            return data, again

    if CLI == "codex":
        # codex enforces the schema itself via --output-schema, so no fence stripping
        # is needed; the file must persist for the length of the call.
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(schema, f)
            path = f.name
        try:
            out = _run(["codex", "exec", "--output-schema", path,
                        "--skip-git-repo-check", "--sandbox", "read-only", "-"], prompt)
        finally:
            os.unlink(path)
        return _json_from(out), {"calls": 1, "tok_in": 0, "tok_out": 0}

    raise RuntimeError(f"LOCAL_CLI={CLI!r} is not supported (use 'claude' or 'codex')")


if __name__ == "__main__":
    import sys
    if not CLI:
        sys.exit("Set LOCAL_CLI=claude or LOCAL_CLI=codex first.")
    if not shutil.which(CLI):
        sys.exit(f"{CLI} is not installed or not on PATH.")
    schema = {"type": "object", "additionalProperties": False,
              "required": ["title", "skills"],
              "properties": {"title": {"type": "string"},
                             "skills": {"type": "array", "items": {"type": "string"}}}}
    data, usage = chat("You write résumé content.",
                       "Give a job title and three skills for a DevSecOps engineer.",
                       schema)
    print(json.dumps(data, indent=2))
    print("usage:", usage)
