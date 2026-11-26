"""Decides which flags are healthy, stale, unused or risky. Pure rules - no AI, so results are explainable."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .flagsmith_api import Experiment, Flag
from .scanner import Reference


class Verdict(str, Enum):
    EXPERIMENT_DONE = "experiment-done"  # A/B test finished with a winner -> keep the winner, delete the losers
    STALE_ON = "stale-on"          # ON everywhere for a long time -> remove flag, keep the new code
    STALE_OFF = "stale-off"        # OFF everywhere for a long time -> remove flag and the dead code
    UNUSED = "unused"              # exists in Flagsmith, never referenced in code -> archive it
    GHOST = "ghost"                # code asks for a flag that does not exist in Flagsmith -> bug risk
    NEEDS_HUMAN = "needs-human"    # stale-looking but has overrides / multivariate values -> do not auto-fix
    HEALTHY = "healthy"


ADVICE = {
    Verdict.EXPERIMENT_DONE: "The experiment is finished and a winner was rolled out. Keep the winning code, delete the losing variants.",
    Verdict.STALE_ON: "Fully rolled out. Delete the flag check and keep the ON code path.",
    Verdict.STALE_OFF: "Never turned on. Delete the flag check and the dead OFF-only code.",
    Verdict.UNUSED: "Not used anywhere in code. Safe to archive in Flagsmith.",
    Verdict.GHOST: "Code reads a flag that does not exist in Flagsmith. It always gets the default - likely a typo or a deleted flag.",
    Verdict.NEEDS_HUMAN: "Looks stale but has segment/identity overrides, variants or conflicting experiment results. Review by hand.",
    Verdict.HEALTHY: "Recently changed, still rolling out, or in a running experiment. Leave it.",
}


@dataclass
class Diagnosis:
    name: str
    verdict: Verdict
    reason: str
    flag: Flag | None = None
    references: list[Reference] = field(default_factory=list)
    age_days: int | None = None
    experiment: Experiment | None = None  # the finished experiment behind an EXPERIMENT_DONE verdict

    @property
    def fixable(self) -> bool:
        return self.verdict in (Verdict.STALE_ON, Verdict.STALE_OFF, Verdict.EXPERIMENT_DONE) and bool(self.references)

    @property
    def keep_mode(self) -> str:
        """What the code should do from now on: "on", "off", or "value"."""
        if self.verdict == Verdict.EXPERIMENT_DONE:
            return "off" if self.experiment.decision == "disable" else "value"
        return "on" if self.verdict == Verdict.STALE_ON else "off"

    @property
    def keep_value(self) -> Any:
        return self.experiment.winner_value if self.experiment else None

    @property
    def keep_label(self) -> str:
        """Short human description of what is kept, e.g. 'the ON path' or 'variant plan_visit'."""
        if self.keep_mode == "value":
            label = self.experiment.winner_label
            return "the control value" if label == "control" else f"winning variant '{label}' (value {self.keep_value!r})"
        return f"the {self.keep_mode.upper()} path"


def _days_since(ts: str | None, now: datetime) -> int | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (now - dt).days)


def _experiment_verdict(flag: Flag) -> tuple[Verdict, str, Experiment | None] | None:
    """Verdict driven by Flagsmith Experimentation, or None when the flag has no experiments."""
    if not flag.experiments:
        return None
    active = [e for e in flag.experiments if e.active]
    if active:
        e = active[0]
        return Verdict.HEALTHY, f"Experiment '{e.name}' is {e.status} in {e.environment}.", None
    decided = [e for e in flag.experiments if e.status == "completed" and e.decision]
    if not decided:
        return None
    decisions = {(e.decision, repr(e.winner_value)) for e in decided}
    if len(decisions) > 1:
        return Verdict.NEEDS_HUMAN, "Experiments in different environments picked different winners.", None
    e = max(decided, key=lambda x: x.ended_at or "")
    if e.decision == "variant" and e.winner_label is None:
        return Verdict.NEEDS_HUMAN, f"Experiment '{e.name}' rolled out a variant that no longer exists.", None
    winner = "turned the flag off" if e.decision == "disable" else f"rolled out '{e.winner_label}'"
    return Verdict.EXPERIMENT_DONE, f"Experiment '{e.name}' finished and {winner}.", e


def diagnose(
    flags: list[Flag],
    references: dict[str, list[Reference]],
    ghosts: dict[str, list[Reference]],
    stale_days: int,
    now: datetime | None = None,
) -> list[Diagnosis]:
    now = now or datetime.now(timezone.utc)
    results: list[Diagnosis] = []

    for flag in flags:
        if flag.is_archived:
            continue
        refs = references.get(flag.name, [])
        age = _days_since(flag.last_modified, now)
        enabled = {s.enabled for s in flag.states}
        values = {repr(s.value) for s in flag.states}
        old_enough = age is not None and age >= stale_days
        experiment = None
        from_experiment = _experiment_verdict(flag)

        if not refs:
            verdict, reason = Verdict.UNUSED, "No references found in the scanned code."
        elif from_experiment:
            verdict, reason, experiment = from_experiment
        elif not old_enough:
            verdict, reason = Verdict.HEALTHY, f"Changed {age} day(s) ago (stale after {stale_days})."
        elif len(enabled) != 1:
            verdict, reason = Verdict.HEALTHY, "ON in some environments and OFF in others - still rolling out."
        elif flag.multivariate or flag.num_segment_overrides or flag.num_identity_overrides or len(values) > 1:
            verdict, reason = Verdict.NEEDS_HUMAN, "Same on/off everywhere, but has overrides, variants or different values."
        elif enabled == {True}:
            verdict, reason = Verdict.STALE_ON, f"ON in all {len(flag.states)} environment(s), unchanged for {age} days."
        else:
            verdict, reason = Verdict.STALE_OFF, f"OFF in all {len(flag.states)} environment(s), unchanged for {age} days."
        results.append(Diagnosis(flag.name, verdict, reason, flag, refs, age, experiment))

    for name, refs in ghosts.items():
        results.append(Diagnosis(name, Verdict.GHOST, f"Used in {len(refs)} place(s) but missing from Flagsmith.", None, refs))

    order = list(Verdict)
    results.sort(key=lambda d: (order.index(d.verdict), d.name))
    return results
