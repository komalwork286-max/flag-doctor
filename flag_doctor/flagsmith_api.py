"""Small client for the Flagsmith Admin (management) REST API.

Endpoints used (from https://api.flagsmith.com/api/v1/swagger.json):
  GET   /projects/{id}/
  GET   /projects/{id}/environments/
  GET   /projects/{project_pk}/features/                   (paginated)
  GET   /environments/{env_api_key}/featurestates/         (paginated)
  PATCH /projects/{project_pk}/features/{id}/              (archive)
  POST  /projects/{project_pk}/features/                   (create, used by demo seed)
  POST  /projects/{project_pk}/code-references/            (report where flags live in code)
Auth header: "Authorization: Api-Key <organisation api key>".
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests


@dataclass
class EnvState:
    environment: str
    enabled: bool
    value: Any
    updated_at: str | None = None


@dataclass
class Experiment:
    id: int
    name: str
    environment: str
    status: str                       # created | running | paused | completed
    decision: str | None = None       # control | variant | disable (set when completed)
    winner_value: Any = None          # the value everyone now gets
    winner_label: str | None = None   # "control" or the variant key, for humans
    ended_at: str | None = None
    hypothesis: str | None = None

    @property
    def active(self) -> bool:
        return self.status in ("created", "running", "paused")


@dataclass
class Flag:
    id: int
    name: str
    created_date: str | None
    last_modified: str | None
    is_archived: bool
    multivariate: bool
    num_segment_overrides: int
    num_identity_overrides: int
    states: list[EnvState] = field(default_factory=list)
    experiments: list[Experiment] = field(default_factory=list)


def _option_value(option: dict) -> Any:
    for key in ("string_value", "integer_value", "boolean_value"):
        if option.get(key) is not None:
            return option[key]
    return None


def parse_experiment(raw: dict, environment: str) -> Experiment:
    """Turn an /experiments/ API object into an Experiment, resolving the winning value."""
    feature = raw.get("feature") or {}
    decision = raw.get("rollout_decision")
    winner_value, winner_label = None, None
    if decision == "control":
        winner_value, winner_label = feature.get("initial_value"), "control"
    elif decision == "variant":
        option = next((o for o in feature.get("multivariate_options", []) if o.get("id") == raw.get("rollout_variant")), None)
        if option:
            winner_value, winner_label = _option_value(option), option.get("key") or str(_option_value(option))
    elif decision == "disable":
        winner_label = "flag off"
    return Experiment(
        id=raw["id"], name=raw.get("name", ""), environment=environment, status=raw.get("status", ""),
        decision=decision, winner_value=winner_value, winner_label=winner_label,
        ended_at=raw.get("ended_at"), hypothesis=raw.get("hypothesis"),
    )


class FlagsmithError(RuntimeError):
    pass


class FlagsmithClient:
    def __init__(self, api_key: str, project_id: int | str, base_url: str = "https://api.flagsmith.com/api/v1"):
        if not api_key:
            raise FlagsmithError("FLAGSMITH_API_KEY is missing. Add it to your .env file.")
        if not project_id:
            raise FlagsmithError("FLAGSMITH_PROJECT_ID is missing. Add it to your .env file.")
        self.project_id = int(project_id)
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"})

    # ---- low level -------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        resp = self.session.request(method, url, timeout=30, **kwargs)
        if resp.status_code >= 400:
            raise FlagsmithError(f"{method} {url} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else None

    def _paginate(self, path: str, params: dict | None = None) -> list[dict]:
        data = self._request("GET", path, params=params)
        if isinstance(data, list):
            return data
        items = list(data.get("results", []))
        while data.get("next"):
            data = self._request("GET", data["next"])
            items.extend(data.get("results", []))
        return items

    # ---- reads -----------------------------------------------------------
    def project(self) -> dict:
        return self._request("GET", f"/projects/{self.project_id}/")

    def environments(self) -> list[dict]:
        return self._paginate(f"/projects/{self.project_id}/environments/")

    def load_flags(self) -> list[Flag]:
        raw = self._paginate(f"/projects/{self.project_id}/features/", params={"page_size": 100})
        flags = {
            f["id"]: Flag(
                id=f["id"],
                name=f["name"],
                created_date=f.get("created_date"),
                last_modified=f.get("last_modified_in_any_environment") or f.get("created_date"),
                is_archived=bool(f.get("is_archived")),
                multivariate=bool(f.get("multivariate_options")),
                num_segment_overrides=f.get("num_segment_overrides") or 0,
                num_identity_overrides=f.get("num_identity_overrides") or 0,
            )
            for f in raw
        }
        for env in self.environments():
            # Experiments (Flagsmith Experimentation). Older self-hosted versions don't have this endpoint.
            try:
                experiments = self._paginate(f"/environments/{env['api_key']}/experiments/")
            except FlagsmithError:
                experiments = []
            for raw_exp in experiments:
                feature_id = (raw_exp.get("feature") or {}).get("id")
                if feature_id in flags:
                    flags[feature_id].experiments.append(parse_experiment(raw_exp, env["name"]))
            for fs in self._paginate(f"/environments/{env['api_key']}/featurestates/"):
                # Only environment-level defaults: skip identity and segment overrides.
                if fs.get("identity") or fs.get("feature_segment"):
                    continue
                flag = flags.get(fs.get("feature"))
                if flag:
                    flag.states.append(
                        EnvState(env["name"], bool(fs.get("enabled")), fs.get("feature_state_value"), fs.get("updated_at"))
                    )
        return list(flags.values())

    # ---- writes (only ever called after a human said yes) ----------------
    def archive_flag(self, flag_id: int) -> None:
        self._request("PATCH", f"/projects/{self.project_id}/features/{flag_id}/", data=json.dumps({"is_archived": True}))

    def create_flag(self, name: str, default_enabled: bool, description: str = "") -> dict:
        body = {"name": name, "default_enabled": default_enabled, "description": description}
        return self._request("POST", f"/projects/{self.project_id}/features/", data=json.dumps(body))

    def upload_code_references(self, repository_url: str, revision: str, refs: list[dict]) -> None:
        body = {"repository_url": repository_url, "revision": revision, "vcs_provider": "github", "code_references": refs}
        self._request("POST", f"/projects/{self.project_id}/code-references/", data=json.dumps(body))


class CliFlagsmithClient(FlagsmithClient):
    """Same client, but every call goes through the Flagsmith CLI (`flagsmith api ...`).

    Uses the credentials from `flagsmith login`, so no API key has to be copied into .env.
    """

    def __init__(self, project_id: int | str, cwd: str | Path | None = None):
        exe = shutil.which("flagsmith")
        if not exe:
            raise FlagsmithError("Flagsmith CLI not found. Install it (npm install -g @flagsmith/cli) and run `flagsmith login`.")
        if not project_id:
            raise FlagsmithError("No project id. Run `flagsmith init` in the repo, or set FLAGSMITH_PROJECT_ID.")
        self.exe = exe
        self.cwd = str(cwd) if cwd else None
        self.project_id = int(project_id)

    def _request(self, method: str, path: str, **kwargs) -> Any:
        if path.startswith("http"):
            path = path.split("/api/v1/", 1)[1]
            path = "/" + path
        args = [self.exe, "api", f"api/v1{path}", "-X", method, "--json"]
        body = kwargs.get("data")
        params = kwargs.get("params")
        if params:
            args[2] += ("&" if "?" in args[2] else "?") + "&".join(f"{k}={v}" for k, v in params.items())
        if body is not None:
            args += ["--input", "-"]
        # The CLI also reads FLAGSMITH_API_URL, but without the /api/v1 suffix that this tool's .env uses.
        env = dict(os.environ)
        if env.get("FLAGSMITH_API_URL"):
            env["FLAGSMITH_API_URL"] = env["FLAGSMITH_API_URL"].rstrip("/").removesuffix("/api/v1")
        proc = subprocess.run(args, input=body, capture_output=True, text=True, encoding="utf-8", cwd=self.cwd, env=env)
        if proc.returncode != 0:
            raise FlagsmithError(f"flagsmith api {method} {path} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
        out = proc.stdout.strip()
        if not out and method == "GET":
            raise FlagsmithError(f"flagsmith api GET {path} returned nothing. Check `flagsmith auth status`.")
        return json.loads(out) if out else None


def project_from_flagsmith_json(repo: str | Path) -> int | None:
    """Read the project id that `flagsmith init` wrote into flagsmith.json."""
    path = Path(repo) / "flagsmith.json"
    if path.exists():
        try:
            return int(json.loads(path.read_text(encoding="utf-8")).get("project"))
        except (ValueError, TypeError):
            return None
    return None


class OfflineFlagsmith:
    """Reads flags from a JSON snapshot so the tool can be tried without an account.

    File format: {"stale_days": 30, "flags": [{"id":1,"name":"x","last_modified":"...","states":[{"environment":"prod","enabled":true,"value":null}]}]}
    """

    project_id = 0

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def project(self) -> dict:
        return {"name": self.data.get("project_name", "offline-demo"), "stale_flags_limit_days": self.data.get("stale_days", 30)}

    def load_flags(self) -> list[Flag]:
        out = []
        for f in self.data["flags"]:
            out.append(
                Flag(
                    id=f["id"],
                    name=f["name"],
                    created_date=f.get("created_date"),
                    last_modified=f.get("last_modified"),
                    is_archived=f.get("is_archived", False),
                    multivariate=f.get("multivariate", False),
                    num_segment_overrides=f.get("num_segment_overrides", 0),
                    num_identity_overrides=f.get("num_identity_overrides", 0),
                    states=[EnvState(s["environment"], s["enabled"], s.get("value")) for s in f.get("states", [])],
                    experiments=[parse_experiment(e, e.get("environment", "offline")) for e in f.get("experiments", [])],
                )
            )
        return out

    def archive_flag(self, flag_id: int) -> None:
        for f in self.data["flags"]:
            if f["id"] == flag_id:
                f["is_archived"] = True
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def upload_code_references(self, *args, **kwargs) -> None:
        raise FlagsmithError("Uploading code references needs a real Flagsmith project (not --offline).")
