import json
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from flag_doctor import cli
from flag_doctor.diagnose import Verdict, diagnose
from flag_doctor.flagsmith_api import OfflineFlagsmith
from flag_doctor.scanner import scan
from flag_doctor.surgeon import FileFix

DEMO = Path(__file__).resolve().parents[1] / "demo"
NOW = datetime(2026, 11, 25, tzinfo=timezone.utc)


def load():
    fs = OfflineFlagsmith(DEMO / "flags-snapshot.json")
    flags = fs.load_flags()
    refs, ghosts = scan(DEMO / "pet-shop", [f.name for f in flags])
    return flags, refs, ghosts


def test_scanner_finds_python_and_js_references():
    _, refs, ghosts = load()
    files = {r.file.as_posix() for r in refs["new_checkout"]}
    assert files == {"app.py", "web/shop.js"}
    assert refs["legacy_search"] == []
    assert list(ghosts) == ["new_onbaording"]


def test_every_verdict_in_demo():
    flags, refs, ghosts = load()
    verdicts = {d.name: d.verdict for d in diagnose(flags, refs, ghosts, stale_days=30, now=NOW)}
    assert verdicts == {
        "new_checkout": Verdict.STALE_ON,
        "holiday_banner_2024": Verdict.STALE_OFF,
        "dark_mode": Verdict.HEALTHY,
        "pricing_experiment": Verdict.NEEDS_HUMAN,
        "legacy_search": Verdict.UNUSED,
        "new_onbaording": Verdict.GHOST,
        "hero_cta_text": Verdict.EXPERIMENT_DONE,
    }


def _hero(flags, refs, ghosts):
    return next(d for d in diagnose(flags, refs, ghosts, stale_days=30, now=NOW) if d.name == "hero_cta_text")


def test_finished_experiment_keeps_the_winner():
    d = _hero(*load())
    assert d.fixable
    assert d.keep_mode == "value" and d.keep_value == "plan_visit"
    assert "plan_visit" in d.keep_label
    assert {r.file.as_posix() for r in d.references} == {"web/shop.js"}


def test_running_experiment_is_never_touched():
    flags, refs, ghosts = load()
    hero = next(f for f in flags if f.name == "hero_cta_text")
    hero.experiments[0].status = "running"
    hero.experiments[0].decision = None
    assert _hero(flags, refs, ghosts).verdict == Verdict.HEALTHY


def test_conflicting_winners_need_a_human():
    flags, refs, ghosts = load()
    hero = next(f for f in flags if f.name == "hero_cta_text")
    other = replace(hero.experiments[0], environment="Production", decision="control", winner_value="control", winner_label="control")
    hero.experiments.append(other)
    assert _hero(flags, refs, ghosts).verdict == Verdict.NEEDS_HUMAN


def test_disabled_experiment_keeps_off_path():
    flags, refs, ghosts = load()
    hero = next(f for f in flags if f.name == "hero_cta_text")
    hero.experiments[0] = replace(hero.experiments[0], decision="disable", winner_value=None, winner_label="flag off")
    d = _hero(flags, refs, ghosts)
    assert d.verdict == Verdict.EXPERIMENT_DONE and d.keep_mode == "off"


def test_young_flag_is_healthy():
    flags, refs, ghosts = load()
    d = next(d for d in diagnose(flags, refs, ghosts, stale_days=10_000, now=NOW) if d.name == "new_checkout")
    assert d.verdict == Verdict.HEALTHY


def test_syntax_check_catches_broken_ai_output():
    from flag_doctor.syntax_check import check_syntax

    assert check_syntax(Path("a.py"), "x = 1\n") is None
    assert "line 1" in check_syntax(Path("a.py"), "def f(:\n")
    if shutil.which("node"):
        assert check_syntax(Path("a.mjs"), "function f() { return 1; }\n") is None
        # The exact mistake Gemini made in the PawNest demo: a missing closing brace.
        assert check_syntax(Path("a.mjs"), "async function w() {\n  while (x) {\n    try { a(); } catch (e) { b();\n  }\n}\n")


def test_fix_applies_only_approved_files(tmp_path, monkeypatch):
    repo = tmp_path / "shop"
    shutil.copytree(DEMO / "pet-shop", repo)
    snap = tmp_path / "snap.json"
    shutil.copy(DEMO / "flags-snapshot.json", snap)

    class FakeSurgeon:
        def fix_file(self, repo_path, diagnosis, file, refs):
            old = (repo_path / file).read_text(encoding="utf-8")
            return FileFix(changed=True, new_content=old.replace(diagnosis.name, "REMOVED"), summary="fake", risk="low", notes="")

    answers = iter([True, False, False])  # apply app.py, reject shop.js, don't archive
    FakeSurgeon.name, FakeSurgeon.model = "Fake", "fake-model"
    monkeypatch.setattr("flag_doctor.surgeon.make_surgeon", lambda provider: FakeSurgeon())
    monkeypatch.setattr(cli.Confirm, "ask", lambda *a, **k: next(answers))

    code = cli.main(["fix", "new_checkout", "--repo", str(repo), "--offline", str(snap)])
    assert code == 0
    assert "new_checkout" not in (repo / "app.py").read_text(encoding="utf-8")
    assert "new_checkout" in (repo / "web" / "shop.js").read_text(encoding="utf-8")
    assert not any(f.get("is_archived") for f in json.loads(snap.read_text())["flags"])
