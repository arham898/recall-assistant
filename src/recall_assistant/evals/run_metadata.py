"""Shared helpers for evaluation scripts: git metadata, results.md sections."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from recall_assistant.config import PROJECT_ROOT

RESULTS_MD = PROJECT_ROOT / "docs" / "results.md"


def git_commit() -> str:
    """Short SHA of HEAD, or ``'unknown'`` on error."""
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(PROJECT_ROOT),
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def is_worktree_dirty() -> bool:
    """True when ``git status --porcelain`` reports changes."""
    try:
        out = (
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=str(PROJECT_ROOT),
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
        return bool(out)
    except (OSError, subprocess.SubprocessError):
        return True


def update_results_section(
    header: str, content: str, *, path: Path | None = None
) -> None:
    """Replace or append a ``## …`` section in docs/results.md.

    *header* is the full ``## …`` line (no trailing newline).
    If a section with that header already exists it is replaced;
    otherwise the new section is appended.
    """
    md_path = path or RESULTS_MD

    if md_path.exists():
        text = md_path.read_text(encoding="utf-8")
    else:
        text = ""

    section = f"{header}\n\n{content}\n"

    pattern = re.compile(
        r"^" + re.escape(header) + r"\n.*?(?=\n## |\Z)",
        re.DOTALL | re.MULTILINE,
    )

    if pattern.search(text):
        text = pattern.sub(section.rstrip(), text)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n" + section

    md_path.write_text(text, encoding="utf-8")
