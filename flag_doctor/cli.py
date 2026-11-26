"""flagdoctor - command line entry point.

  flagdoctor scan      Check every flag: stale, unused, ghost or healthy.
  flagdoctor fix       Let Claude remove stale flags from the code. You approve every file.
  flagdoctor archive   Archive flags that no code uses any more. You approve every flag.
  flagdoctor sync-refs Tell Flagsmith where each flag is used in your code.
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.syntax import Syntax
from rich.table import Table

from . import git_ops
from .diagnose import ADVICE, Diagnosis, Verdict, diagnose
from .flagsmith_api import CliFlagsmithClient, FlagsmithClient, FlagsmithError, OfflineFlagsmith, project_from_flagsmith_json
from .scanner import scan
from .syntax_check import check_syntax

console = Console()

COLORS = {
    Verdict.EXPERIMENT_DONE: "bold blue",
    Verdict.STALE_ON: "bold green",
    Verdict.STALE_OFF: "bold yellow",
    Verdict.UNUSED: "cyan",
    Verdict.GHOST: "bold red",
    Verdict.NEEDS_HUMAN: "magenta",
    Verdict.HEALTHY: "dim",
}


# ---------------------------------------------------------------- shared steps
def connect(args):
    if args.offline:
        return OfflineFlagsmith(args.offline)
    if args.use_cli:
        project = os.environ.get("FLAGSMITH_PROJECT_ID") or project_from_flagsmith_json(args.repo)
        return CliFlagsmithClient(project, cwd=args.repo)
    return FlagsmithClient(
        os.environ.get("FLAGSMITH_API_KEY", ""),
        os.environ.get("FLAGSMITH_PROJECT_ID", ""),
        os.environ.get("FLAGSMITH_API_URL", "https://api.flagsmith.com/api/v1"),
    )


def examine(args):
    fs = connect(args)
    with console.status("Reading flags from Flagsmith..."):
        project = fs.project()
        flags = fs.load_flags()
    stale_days = args.stale_days if args.stale_days is not None else (project.get("stale_flags_limit_days") or 30)
    repo = Path(args.repo).resolve()
    with console.status(f"Scanning code in {repo}..."):
        refs, ghosts = scan(repo, [f.name for f in flags])
    results = diagnose(flags, refs, ghosts, stale_days)
    return fs, repo, project, stale_days, results


def print_table(results: list[Diagnosis], project_name: str, stale_days: int) -> None:
    table = Table(title=f"Flag Doctor check-up: {project_name}  (stale after {stale_days} days)", show_lines=False)
    table.add_column("Flag", style="bold")
    table.add_column("Verdict")
    table.add_column("Uses in code", justify="right")
    table.add_column("Why")
    for d in results:
        table.add_row(d.name, f"[{COLORS[d.verdict]}]{d.verdict.value}[/]", str(len(d.references)), d.reason)
    console.print(table)

    counts = defaultdict(int)
    for d in results:
        counts[d.verdict] += 1
    summary = "  ".join(f"[{COLORS[v]}]{v.value}: {counts[v]}[/]" for v in Verdict if counts[v])
    console.print(summary)
    fixable = [d for d in results if d.fixable]
    if fixable:
        console.print(f"\n[bold]{len(fixable)} flag(s) can be removed automatically.[/] Run [cyan]flagdoctor fix[/] to start.")


def write_markdown(results: list[Diagnosis], project_name: str, stale_days: int, path: Path) -> None:
    lines = [f"# Flag Doctor report: {project_name}", "", f"Generated {date.today().isoformat()}. A flag is stale after {stale_days} days without changes.", ""]
    for verdict in Verdict:
        group = [d for d in results if d.verdict == verdict]
        if not group:
            continue
        lines += [f"## {verdict.value} ({len(group)})", "", f"_{ADVICE[verdict]}_", ""]
        for d in group:
            lines.append(f"- **`{d.name}`**: {d.reason}")
            for r in d.references[:10]:
                lines.append(f"  - `{r.file.as_posix()}:{r.line}`: `{r.text[:100]}`")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"Report saved to [cyan]{path}[/]")


def show_diff(old: str, new: str, file: Path) -> None:
    diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{file.as_posix()}", f"b/{file.as_posix()}"))
    console.print(Syntax(diff or "(no changes)", "diff", theme="ansi_dark", word_wrap=True))


# ---------------------------------------------------------------- commands
def cmd_scan(args) -> int:
    _, _, project, stale_days, results = examine(args)
    print_table(results, project.get("name", "project"), stale_days)
    if args.report:
        write_markdown(results, project.get("name", "project"), stale_days, Path(args.report))
    return 0


def cmd_fix(args) -> int:
    from .surgeon import make_surgeon  # imported here so `scan` works without an AI key

    fs, repo, project, stale_days, results = examine(args)
    targets = [d for d in results if d.fixable and (not args.flags or d.name in args.flags)]
    if args.flags:
        missing = set(args.flags) - {d.name for d in targets}
        for name in missing:
            match = next((d for d in results if d.name == name), None)
            why = f"it is '{match.verdict.value}': {match.reason}" if match else "it was not found in Flagsmith"
            console.print(f"[yellow]Skipping {name}: {why}[/]")
    if not targets:
        console.print("[green]Nothing to fix - no stale flags are used in this code.[/]")
        return 0

    use_git = git_ops.is_repo(repo)
    if use_git and not args.dry_run and git_ops.has_uncommitted_changes(repo):
        console.print("[red]Your repo has uncommitted changes. Commit or stash them first so Flag Doctor's edits stay separate.[/]")
        return 1

    surgeon = make_surgeon(args.ai)
    console.print(f"AI: [bold]{surgeon.name}[/] ({surgeon.model})")
    changed_files: list[Path] = []
    fixed_flags: list[Diagnosis] = []
    pr_notes: list[str] = []

    for d in targets:
        console.print(Panel(f"[bold]{d.name}[/]  ->  keep [bold]{d.keep_label}[/]\n{d.reason}\n{ADVICE[d.verdict]}",
                            title="Operating on flag", border_style=COLORS[d.verdict]))
        by_file: dict[Path, list] = defaultdict(list)
        for r in d.references:
            by_file[r.file].append(r)

        flag_files = []
        for file, refs in by_file.items():
            with console.status(f"{surgeon.name} is rewriting {file.as_posix()}..."):
                fix = surgeon.fix_file(repo, d, file, refs)
            old = (repo / file).read_text(encoding="utf-8")
            if fix.changed and (error := check_syntax(file, fix.new_content)):
                fix.risk = "high"
                fix.notes = f"BROKEN CODE - {error}. Do not apply. " + (fix.notes or "")
            console.print(f"\n[bold]{file.as_posix()}[/]  risk: [bold]{fix.risk}[/]\n{fix.summary}")
            if fix.notes:
                console.print(f"[yellow]Reviewer note:[/] {fix.notes}")
            if not fix.changed:
                console.print("[dim]No change proposed for this file.[/]")
                continue
            show_diff(old, fix.new_content, file)
            if args.dry_run:
                continue
            if Confirm.ask(f"Apply this change to {file.as_posix()}?", default=fix.risk == "low"):
                (repo / file).write_text(fix.new_content, encoding="utf-8", newline="")
                flag_files.append(file)
                pr_notes.append(f"- `{file.as_posix()}` ({d.name}, risk {fix.risk}): {fix.summary}")
            else:
                console.print("[dim]Skipped.[/]")
        if flag_files and len(flag_files) == len(by_file):
            fixed_flags.append(d)
        changed_files += flag_files

    if args.dry_run:
        console.print("\n[cyan]Dry run: nothing was written.[/]")
        return 0
    changed_files = list(dict.fromkeys(changed_files))  # a file can hold several flags
    if not changed_files:
        console.print("\nNo files were changed.")
        return 0

    console.print(f"\n[green]Changed {len(changed_files)} file(s).[/] Run your tests before shipping.")
    if use_git and Confirm.ask("Create a new git branch and commit these changes?", default=True):
        base = git_ops.current_branch(repo)
        names = [d.name for d in fixed_flags] or [d.name for d in targets]
        branch = f"flag-doctor/remove-{'-'.join(names)[:50]}"
        title = f"chore(flags): remove stale flag{'s' if len(names) > 1 else ''} {', '.join(names)}"
        git_ops.create_branch_and_commit(repo, branch, changed_files, title)
        console.print(f"Committed on branch [cyan]{branch}[/].")
        if Confirm.ask("Push the branch and open a GitHub pull request?", default=False):
            body = "\n".join([
                "Removed by **Flag Doctor** after a human reviewed every diff.", "",
                "## Flags", *[f"- `{d.name}`: {d.reason} Kept {d.keep_label}." for d in fixed_flags], "",
                "## Files", *pr_notes, "",
                "After merging, archive these flags in Flagsmith (`flagdoctor archive`).",
            ])
            url = git_ops.push_and_open_pr(repo, branch, base, title, body)
            console.print(f"[green]Pull request opened:[/] {url}")

    for d in fixed_flags:
        if Confirm.ask(f"Archive '{d.name}' in Flagsmith now? (best after the PR is merged and deployed)", default=False):
            fs.archive_flag(d.flag.id)
            console.print(f"[green]Archived {d.name}.[/]")
    return 0


def cmd_archive(args) -> int:
    fs, _, project, stale_days, results = examine(args)
    unused = [d for d in results if d.verdict == Verdict.UNUSED and (not args.flags or d.name in args.flags)]
    if not unused:
        console.print("[green]No unused flags to archive.[/]")
        return 0
    for d in unused:
        states = ", ".join(f"{s.environment}={'ON' if s.enabled else 'OFF'}" for s in d.flag.states)
        if Confirm.ask(f"Archive unused flag [bold]{d.name}[/] ({states})?", default=False):
            fs.archive_flag(d.flag.id)
            console.print(f"[green]Archived {d.name}.[/]")
    return 0


def cmd_sync_refs(args) -> int:
    fs, repo, *_ , results = examine(args)
    if not git_ops.is_repo(repo):
        console.print("[red]sync-refs needs a git repository with a GitHub remote.[/]")
        return 1
    url = git_ops.remote_url(repo)
    if not url:
        console.print("[red]No `origin` remote found.[/]")
        return 1
    refs = [{"feature_name": r.flag, "file_path": r.file.as_posix(), "line_number": r.line}
            for d in results if d.flag for r in d.references]
    console.print(f"Uploading {len(refs)} code reference(s) for {url} ...")
    fs.upload_code_references(url, git_ops.head_revision(repo), refs)
    console.print("[green]Done. Flagsmith now shows where each flag lives in your code.[/]")
    return 0


# ---------------------------------------------------------------- argparse
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", default=".", help="Path to the code to scan (default: current folder).")
    common.add_argument("--stale-days", type=int, default=None,
                        help="Days without changes before a flag counts as stale (default: the project's Flagsmith setting, else 30).")
    common.add_argument("--offline", metavar="JSON", help="Use a JSON snapshot instead of the Flagsmith API (for trying it out).")
    common.add_argument("--use-cli", action="store_true",
                        help="Talk to Flagsmith through the Flagsmith CLI (uses `flagsmith login`; project from flagsmith.json).")

    p = argparse.ArgumentParser(prog="flagdoctor", description="Find stale Flagsmith feature flags and remove them safely.")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", parents=[common], help="Check the health of every flag.")
    s.add_argument("--report", metavar="FILE.md", help="Also save a Markdown report.")
    s.set_defaults(func=cmd_scan)

    f = sub.add_parser("fix", parents=[common], help="Remove stale flags from code with Claude. You approve every change.")
    f.add_argument("flags", nargs="*", help="Only fix these flags (default: all fixable ones).")
    f.add_argument("--dry-run", action="store_true", help="Show the proposed diffs without writing anything.")
    f.add_argument("--ai", choices=["auto", "claude", "gemini"], default="auto",
                   help="Which AI rewrites the code (default: Claude if ANTHROPIC_API_KEY is set, else Gemini).")
    f.set_defaults(func=cmd_fix)

    a = sub.add_parser("archive", parents=[common], help="Archive flags that no code uses.")
    a.add_argument("flags", nargs="*", help="Only these flags.")
    a.set_defaults(func=cmd_archive)

    r = sub.add_parser("sync-refs", parents=[common], help="Upload where flags are used in code to Flagsmith.")
    r.set_defaults(func=cmd_sync_refs)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (FlagsmithError, git_ops.GitError) as e:
        console.print(f"[red]Error:[/] {e}")
        return 1
    except anthropic.AuthenticationError:
        console.print("[red]Error:[/] Claude API key is missing or wrong. Set ANTHROPIC_API_KEY in your .env file.")
        return 1
    except anthropic.APIError as e:
        console.print(f"[red]Claude API error:[/] {e}")
        return 1
    except Exception as e:  # Gemini SDK errors (optional dependency, so not imported at top level)
        if type(e).__module__.startswith("google.genai"):
            console.print(f"[red]Gemini API error:[/] {e}")
            return 1
        raise
    except KeyboardInterrupt:
        console.print("\nStopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
