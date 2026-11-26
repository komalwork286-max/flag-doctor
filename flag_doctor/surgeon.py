"""The AI part: asks Claude to remove one flag from one file, keeping exactly one code path."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .diagnose import Diagnosis
from .scanner import Reference

MODEL = "claude-opus-5-5"
MAX_FILE_CHARS = 40_000  # the whole file comes back in the answer, so keep it within max_tokens

SYSTEM_PROMPT = """You are Flag Doctor, a careful refactoring assistant that removes dead feature flags from source code.

You get one source file, the name of a Flagsmith feature flag, and which side of the flag must be kept:
- keep="on": the flag is permanently ON. Replace every check of the flag with the ON behaviour.
- keep="off": the flag is permanently OFF. Replace every check with the OFF behaviour and delete code that only ran when ON.
- keep="value" with a winning value: an A/B experiment on this multivariate flag has finished and the flag is
  permanently ON with that value for every user. Replace every read of the flag (including experiment calls such as
  getExperimentFlag / get_experiment_flag / useExperiment, which also record exposures) with the winning value's
  behaviour, and delete code, branches, and map/dictionary entries that only served the losing values.
  Also remove exposure tracking for this flag. Keep conversion tracking (trackEvent etc.) unless it exists only
  for this flag's experiment and nothing else - if unsure, keep it and say so in notes.

Rules:
- Change only what is needed to remove this one flag. Keep formatting, comments, and unrelated code exactly as they are.
- Simplify the code that is left: `if (true) { x }` becomes `x`; drop now-unused variables, imports, or helper functions that only existed for this flag.
- Leave other flags untouched.
- If the flag name appears in a way you cannot safely rewrite (for example built dynamically, or passed into code you cannot see), do not guess: set changed=false and explain in notes.
- Return the COMPLETE new file content, not a diff.
- risk: "low" for mechanical removals, "medium" if behaviour around the flag needed judgement, "high" if a reviewer must look carefully."""


class FileFix(BaseModel):
    changed: bool = Field(description="True if the file was modified.")
    new_content: str = Field(description="The full new file content. Same as input when changed is false.")
    summary: str = Field(description="One or two plain sentences describing what was removed or kept.")
    risk: Literal["low", "medium", "high"]
    notes: str = Field(description="Anything the reviewer should check. Empty string if nothing.")


GEMINI_MODEL = "gemini-3.6-flash"
# Tried in order when the main model is busy (503/504) or not offered to this key (404).
GEMINI_FALLBACKS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]


def build_prompt(diagnosis: Diagnosis, file: Path, refs: list[Reference], source: str) -> str:
    lines = "\n".join(f"  line {r.line}: {r.text}" for r in refs)
    keep = f'keep="{diagnosis.keep_mode}"'
    if diagnosis.keep_mode == "value":
        keep += f"\nWinning value: {diagnosis.keep_value!r} ({diagnosis.keep_label})"
        if diagnosis.experiment.hypothesis:
            keep += f"\nExperiment hypothesis: {diagnosis.experiment.hypothesis}"
    return (
        f"Flag name: {diagnosis.name}\n"
        f"{keep}\n"
        f"Why it is stale: {diagnosis.reason}\n"
        f"File: {file.as_posix()}\n"
        f"Lines that mention the flag:\n{lines}\n\n"
        f"<file>\n{source}\n</file>"
    )


def _too_large(file: Path, source: str) -> FileFix | None:
    if len(source) > MAX_FILE_CHARS:
        return FileFix(changed=False, new_content=source, summary="Skipped: file is too large to rewrite safely.",
                       risk="high", notes=f"{file} has {len(source)} characters; fix it by hand.")
    return None


def _double_check(fix: FileFix, diagnosis: Diagnosis) -> FileFix:
    if fix.changed and diagnosis.name in fix.new_content:
        fix.risk = "high"
        fix.notes = (fix.notes + " " if fix.notes else "") + f"The flag name '{diagnosis.name}' still appears in the new file."
    return fix


def make_surgeon(provider: str = "auto"):
    """Pick the AI: "claude", "gemini", or "auto" (whichever key is set, Claude first)."""
    if provider == "auto":
        provider = "claude" if os.environ.get("ANTHROPIC_API_KEY") else "gemini" if os.environ.get("GEMINI_API_KEY") else "claude"
    if provider == "gemini":
        return GeminiSurgeon()
    return Surgeon()


class GeminiSurgeon:
    """Same job as Surgeon, using Google Gemini (has a free tier). Needs GEMINI_API_KEY."""

    name = "Gemini"

    def __init__(self, client=None, model: str | None = None):
        from google import genai  # optional dependency: pip install google-genai
        from google.genai import types

        self.client = client or genai.Client(
            api_key=os.environ.get("GEMINI_API_KEY"), http_options=types.HttpOptions(timeout=120_000)
        )
        self.model = model or os.environ.get("GEMINI_MODEL", GEMINI_MODEL)

    def _generate(self, contents: str):
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=FileFix,
        )
        models = [self.model] + [m for m in GEMINI_FALLBACKS if m != self.model]
        for i, model in enumerate(models):
            try:
                return self.client.models.generate_content(model=model, contents=contents, config=config)
            except errors.APIError as e:
                busy_or_missing = e.code in (404, 500, 503, 504)
                if not busy_or_missing or i == len(models) - 1:
                    raise
                self.model = models[i + 1]  # shown in the CLI so the user knows which model answered

    def fix_file(self, repo: Path, diagnosis: Diagnosis, file: Path, refs: list[Reference]) -> FileFix:
        source = (repo / file).read_text(encoding="utf-8")
        if skipped := _too_large(file, source):
            return skipped
        response = self._generate(build_prompt(diagnosis, file, refs, source))
        fix = response.parsed
        if not isinstance(fix, FileFix):
            return FileFix(changed=False, new_content=source, summary="The model gave no usable answer.", risk="high",
                           notes="Fix this file by hand, or try again.")
        return _double_check(fix, diagnosis)


class Surgeon:
    name = "Claude"

    def __init__(self, client: anthropic.Anthropic | None = None, model: str = MODEL):
        self.client = client or anthropic.Anthropic()
        self.model = model

    def fix_file(self, repo: Path, diagnosis: Diagnosis, file: Path, refs: list[Reference]) -> FileFix:
        source = (repo / file).read_text(encoding="utf-8")
        if skipped := _too_large(file, source):
            return skipped
        user = build_prompt(diagnosis, file, refs, source)
        response = self.client.beta.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            output_config={"effort": "high"},
            # If the main model declines, Anthropic transparently retries on a suitable fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": user}],
            output_format=FileFix,
        )
        if response.stop_reason == "refusal":
            return FileFix(changed=False, new_content=source, summary="The model declined this request.", risk="high",
                           notes="Fix this file by hand.")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            return FileFix(changed=False, new_content=source, summary="The answer was cut off.", risk="high",
                           notes="File may be too long; fix it by hand.")
        return _double_check(response.parsed_output, diagnosis)
