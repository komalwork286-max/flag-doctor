"""Checks that AI-rewritten code still parses, before a human is asked to approve it."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

JS_SUFFIXES = {".js", ".mjs", ".cjs"}


def check_syntax(file: Path, content: str) -> str | None:
    """Return an error message if `content` does not parse, None if it is fine or cannot be checked."""
    suffix = file.suffix.lower()
    if suffix == ".py":
        try:
            compile(content, str(file), "exec")
        except SyntaxError as e:
            return f"Python syntax error on line {e.lineno}: {e.msg}"
        return None
    if suffix in JS_SUFFIXES and shutil.which("node"):
        fd, tmp = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            proc = subprocess.run([shutil.which("node"), "--check", tmp], capture_output=True, text=True, encoding="utf-8")
            if proc.returncode != 0:
                lines = [ln for ln in proc.stderr.splitlines() if "Error" in ln]
                return "JavaScript syntax error: " + (lines[0].strip() if lines else proc.stderr.strip()[:200])
        finally:
            os.unlink(tmp)
    return None
