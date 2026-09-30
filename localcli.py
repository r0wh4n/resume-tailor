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
TIMEOUT = int(os.environ.get("LOCAL_CLI_TIMEOUT", 180))

# Claude Code exposes these; naming them explicitly is what keeps an injected
# "run this command" in a job posting from being anything but text.
NO_TOOLS = ["Bash", "Read", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch",
            "Glob", "Grep", "Task", "TodoWrite"]

FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.M)


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
                       timeout=TIMEOUT, shell=False)
    if p.returncode != 0:
        raise RuntimeError(f"{argv[0]} exited {p.returncode}: "
                           f"{(p.stderr or p.stdout or '')[:300]}")
    return p.stdout


def chat(system, user, schema, model=None):
    """Same contract as the API providers: schema-shaped dict out.

    Returns (data, usage) where usage is {calls, tok_in, tok_out}.
    """
    prompt = (f"{system}\n\n"
              f"Reply with ONE JSON object and nothing else - no prose, no code fence. "
              f"It must validate against this JSON Schema:\n{json.dumps(schema)}\n\n"
              f"{user}")

    if CLI == "claude":
        argv = ["claude", "-p", "--output-format", "json", "--max-turns", "1",
                "--disallowedTools", *NO_TOOLS]
        if model:
            argv += ["--model", model]
        env = json.loads(_run(argv, prompt))
        if env.get("is_error"):
            raise RuntimeError(f"claude: {str(env.get('result'))[:300]}")
        u = env.get("usage") or {}
        return _json_from(env.get("result", "")), {
            "calls": 1,
            "tok_in": (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                       + u.get("cache_creation_input_tokens", 0)),
            "tok_out": u.get("output_tokens", 0),
        }

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
