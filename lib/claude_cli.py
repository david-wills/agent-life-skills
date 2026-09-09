"""Generate text through the local ``claude`` CLI.

Used by the skills whose deterministic script needs a summary in the middle of
a batch (reading-list) rather than an agent turn around the whole run.
"""

from __future__ import annotations

import subprocess


def claude_generate(prompt: str, model: str = "claude-haiku-4-5", timeout: int = 120) -> str:
    """Run the prompt through ``claude --print`` and return trimmed stdout."""
    try:
        proc = subprocess.run(
            ["claude", "--print", "--permission-mode", "bypassPermissions", "--model", model],
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
