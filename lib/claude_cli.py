"""Generate text through the local ``claude`` CLI, with every tool disabled.

Used by the skills whose deterministic script needs a summary in the middle of
a batch (reading-list, the Reader ingester) rather than an agent turn around the
whole run. The prompts carry article text fetched from the internet, which is
untrusted input: an instruction-like passage must not be able to make the model
touch the filesystem, shell or network. So the call runs with ``--tools ""``,
no MCP servers, no settings from the user's own Claude Code config, and anything
that would prompt for permission is denied outright.
"""

from __future__ import annotations

import subprocess


def claude_generate(prompt: str, model: str = "claude-haiku-4-5", timeout: int = 120) -> str:
    """Run the prompt through ``claude --print`` and return trimmed stdout."""
    try:
        proc = subprocess.run(
            [
                "claude", "--print",
                "--tools", "",                       # no built-in tools at all
                "--strict-mcp-config",               # and no MCP servers from the user's config
                "--mcp-config", '{"mcpServers":{}}',
                "--setting-sources", "",             # ignore user/project settings (hooks, allowlists)
                "--permission-mode", "dontAsk",      # deny anything that would still ask
                "--permission-prompts", "none",
                "--model", model,
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("the `claude` CLI is not on PATH") from exc
    if proc.returncode != 0:
        # The CLI reports some failures on stdout with an empty stderr, most
        # notably "Prompt is too long", so report whichever stream said something.
        err = (proc.stderr or "").strip()
        out = (proc.stdout or "").strip()
        detail = " | ".join(p[:400] for p in (err, out) if p) or "(no output on stdout or stderr)"
        raise RuntimeError(f"Claude CLI failed ({proc.returncode}): {detail}")
    return proc.stdout.strip()
