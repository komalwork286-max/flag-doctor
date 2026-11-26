"""Finds where feature flags are used in a code base (no AI - plain, fast, predictable)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".html",
    ".java", ".kt", ".go", ".rb", ".php", ".cs", ".dart", ".swift", ".rs", ".ex", ".exs",
}
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build", ".next", "vendor", ".idea", ".vscode"}

# Calls into Flagsmith SDKs whose first argument is a flag name.
# Used to find flags that the code asks for but that do not exist in Flagsmith ("ghost flags").
SDK_CALL = re.compile(
    r"""(?:hasFeature|getValue|isFeatureEnabled|getFeatureValue|is_feature_enabled|get_feature_value|"""
    r"""has_feature|get_value|IsFeatureEnabled|GetFeatureValue|feature_enabled\?|getFlag|useFlag|"""
    r"""getExperimentFlag|get_experiment_flag|useExperiment|trackExposureEvent)\s*\(\s*["']([A-Za-z0-9_.\-]+)["']"""
)


@dataclass(frozen=True)
class Reference:
    flag: str
    file: Path  # relative to repo root
    line: int
    text: str


def iter_code_files(root: Path):
    for path in root.rglob("*"):
        if path.is_dir() or path.suffix.lower() not in CODE_EXTENSIONS:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        yield path


def scan(root: str | Path, known_flags: list[str]) -> tuple[dict[str, list[Reference]], dict[str, list[Reference]]]:
    """Return (references to known flags, references to unknown "ghost" flags)."""
    root = Path(root).resolve()
    # A flag counts as used when its exact name appears as a quoted string.
    patterns = {name: re.compile(r"""["'`]""" + re.escape(name) + r"""["'`]""") for name in known_flags}
    found: dict[str, list[Reference]] = {name: [] for name in known_flags}
    ghosts: dict[str, list[Reference]] = {}

    for path in iter_code_files(root):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(root)
        for i, line in enumerate(lines, start=1):
            for name, pat in patterns.items():
                if pat.search(line):
                    found[name].append(Reference(name, rel, i, line.strip()))
            for m in SDK_CALL.finditer(line):
                name = m.group(1)
                if name not in patterns:
                    ghosts.setdefault(name, []).append(Reference(name, rel, i, line.strip()))
    return found, ghosts
