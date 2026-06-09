#!/usr/bin/env python3
"""Append paper-related prompts to paper/paper_prompts.md.

Keeps a running, numbered log of the textual prompts that drive changes to the
paper (the LaTeX sources under /paper), separate from the project-wide
prompts.md.

Modes
-----
Manual (default) — log a prompt you pass in:
    python paper/log_paper_prompt.py "Reword the abstract to emphasise X"
    python paper/log_paper_prompt.py        # then type/paste, end with Ctrl-D
    echo "Add a related-work paragraph" | python paper/log_paper_prompt.py

--on-edit — automatic logging from the PostToolUse hook. Reads the most recent
    user prompt stashed by the UserPromptSubmit hook (paper/.pending_prompt.txt)
    and logs it, but only once per prompt (de-duplicated via
    paper/.logged_prompt.txt). Fired whenever a paper .tex file is edited, so a
    single prompt that touches several .tex files is logged a single time.
"""

import sys
from datetime import date
from pathlib import Path

PAPER_DIR = Path(__file__).resolve().parent
LOG_FILE = PAPER_DIR / "paper_prompts.md"
PENDING_FILE = PAPER_DIR / ".pending_prompt.txt"   # latest prompt, written by UserPromptSubmit hook
MARKER_FILE = PAPER_DIR / ".logged_prompt.txt"     # last prompt already logged here (dedup)

HEADER = """# Paper Prompts Log

A running record of the textual prompts that drive changes to the paper
(LaTeX sources under /paper). Separate from the project-wide prompts.md.
Entries marked automatically are appended whenever a paper .tex file changes.
"""


def read_prompt(argv: list[str]) -> str:
    """Return the prompt from argv, or from stdin if no args were given."""
    if argv:
        return " ".join(argv).strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    print("Enter the paper prompt, then press Ctrl-D:", file=sys.stderr)
    return sys.stdin.read().strip()


def next_index(text: str) -> int:
    """Find the highest existing entry number so we can continue the count."""
    highest = 0
    for line in text.splitlines():
        head = line.strip().split(".", 1)[0]
        if head.isdigit():
            highest = max(highest, int(head))
    return highest + 1


def append_entry(prompt: str) -> int:
    existing = LOG_FILE.read_text(encoding="utf-8") if LOG_FILE.exists() else HEADER
    index = next_index(existing)
    if not existing.endswith("\n"):
        existing += "\n"
    entry = f"\n{index}. ({date.today().isoformat()}) {prompt}\n"
    LOG_FILE.write_text(existing + entry, encoding="utf-8")
    return index


def on_edit() -> int:
    """Log the prompt behind the current paper edit, once per prompt."""
    if not PENDING_FILE.exists():
        return 0
    prompt = PENDING_FILE.read_text(encoding="utf-8").strip()
    if not prompt:
        return 0
    already = MARKER_FILE.read_text(encoding="utf-8").strip() if MARKER_FILE.exists() else ""
    if prompt == already:
        return 0  # this prompt already logged for an earlier file in the same turn
    append_entry(prompt)
    MARKER_FILE.write_text(prompt, encoding="utf-8")
    return 0


def main() -> int:
    if "--on-edit" in sys.argv[1:]:
        return on_edit()

    prompt = read_prompt(sys.argv[1:])
    if not prompt:
        print("No prompt provided; nothing logged.", file=sys.stderr)
        return 1
    index = append_entry(prompt)
    print(f"Logged paper prompt #{index} to {LOG_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
