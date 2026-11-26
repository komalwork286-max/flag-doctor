"""Git / GitHub helpers. Every call here runs only after the user approved it."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _run(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitError(f"`{' '.join(args)}` failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def is_repo(repo: Path) -> bool:
    try:
        _run(["git", "rev-parse", "--is-inside-work-tree"], repo)
        return True
    except (GitError, FileNotFoundError):
        return False


def has_uncommitted_changes(repo: Path) -> bool:
    return bool(_run(["git", "status", "--porcelain"], repo))


def current_branch(repo: Path) -> str:
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo)


def head_revision(repo: Path) -> str:
    return _run(["git", "rev-parse", "HEAD"], repo)


def remote_url(repo: Path) -> str | None:
    try:
        url = _run(["git", "remote", "get-url", "origin"], repo)
    except GitError:
        return None
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url.split(":", 1)[1]
    return url.removesuffix(".git")


def create_branch_and_commit(repo: Path, branch: str, files: list[Path], message: str) -> None:
    _run(["git", "checkout", "-b", branch], repo)
    _run(["git", "add", "--", *[f.as_posix() for f in files]], repo)
    _run(["git", "commit", "-m", message], repo)


def push_and_open_pr(repo: Path, branch: str, base: str, title: str, body: str) -> str:
    if not shutil.which("gh"):
        raise GitError("GitHub CLI `gh` is not installed. Install it from https://cli.github.com and run `gh auth login`.")
    _run(["git", "push", "-u", "origin", branch], repo)
    return _run(["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body], repo)
