# 🩺 Flag Doctor

**An AI agent that finds dead Flagsmith feature flags and removes them from your code, with a human approving every change.**

Built for **Toggle Wars: The Flagsmith Experimentation Hackathon** (WeMakeDevs × Flagsmith), **Track 02: AI Agents and Feature Flags**.

## 🎬 Demo video

**[▶ Watch the demo (docs/demo.mp4, 78 s)](docs/demo.mp4)**

A real run, not a mock-up:
1. `flagdoctor scan` asks **Flagsmith Cloud** (through the Flagsmith CLI) about the PawNest pet-store project and finds the finished A/B test `hero_cta_text`, won by `plan_visit`.
2. `flagdoctor fix` has **Gemini** remove the losing variant. The presenter approves `js/main.js` and rejects the change to the traffic simulator.
3. Flag Doctor commits the cleanup on its own branch.

The video was made with [`docs/record_demo.py`](docs/record_demo.py). It runs the commands for real and records every output byte with its real timing; the raw capture is in [`docs/demo-recording.json`](docs/demo-recording.json). Only the typing and the pauses before answering are simulated, and AI waits longer than 4 s are shortened.

---

## The problem

Feature flags are meant to be temporary. Teams add a flag, roll the feature out to 100%, and then forget about it. Months later the code is full of `if flag:` checks that always take the same path:

- **Dead code** keeps living in the repo (the old checkout flow nobody uses).
- **Confusion**: new developers can't tell which path is real.
- **Risk**: someone flips an old flag by mistake and resurrects a 2-year-old bug.
- **Ghost flags**: code asks for a flag that no longer exists in Flagsmith (or has a typo), so it silently gets the default value forever.

Cleaning this up by hand means: checking every flag in the dashboard, searching the code, carefully deleting the right branch, opening a PR, and archiving the flag. Nobody enjoys it, so it never happens.

## What Flag Doctor does

```
flagdoctor scan   →  check-up of every flag (Flagsmith API + code scan)
flagdoctor fix    →  Claude removes stale flags from code, you approve each diff, it opens a PR
flagdoctor archive→  archive flags that no code uses any more
flagdoctor sync-refs → tell Flagsmith where each flag lives in your code
```

### 1. Diagnosis (no AI, plain rules you can trust)

Flag Doctor reads every flag and its on/off state in **every environment** from the Flagsmith Admin API, scans your repo, and gives each flag a verdict:

| Verdict | Meaning | What to do |
|---|---|---|
| 🏆 `experiment-done` | A Flagsmith **experiment finished** and a winner was rolled out | Keep the **winning variant's code**, delete the losing variants and exposure tracking |
| 🟢 `stale-on` | ON in every environment, unchanged for N days | Remove the check, **keep the ON code** |
| 🟡 `stale-off` | OFF in every environment, unchanged for N days | Remove the check **and the dead code** |
| 🔵 `unused` | In Flagsmith, but no code uses it | Archive it |
| 🔴 `ghost` | Code reads a flag that doesn't exist in Flagsmith | Fix the typo, or remove it |
| 🟣 `needs-human` | Looks stale, but has segment/identity overrides or multivariate values | Leave it to a person |
| ⚪ `healthy` | Recently changed or still rolling out | Leave it |

### Experiment-aware cleanup 🏆

Flagsmith Experimentation tells you which variant won. It doesn't remove the losers from your code, so the old variant stays there forever. Flag Doctor closes that loop:

```
run an A/B test  →  Flagsmith picks a winner  →  you roll it out  →  flagdoctor fix removes the losing code
```

- **Completed** experiment with a rollout decision → `experiment-done`, fixable right away (no waiting N days).
- **Running, paused or not-yet-started** experiment → always `healthy`. Flag Doctor never touches a live test.
- Different winners in different environments → `needs-human`.

Real example from this hackathon: the PawNest pet store tested its hero button, "📍 Visit Our Store" vs "🗓️ Plan Your Visit". `plan_visit` won with 91% probability and was rolled out. `flagdoctor scan` then reports:

```
| hero_cta_text | experiment-done | 3 | Experiment 'Hero button text' finished and rolled out 'plan_visit'. |
```

`flagdoctor fix` then asks Claude to drop the `control` text, the `getExperimentFlag` exposure call and the flag lookup, keeping just "🗓️ Plan Your Visit".

"N days" comes from your Flagsmith project's own **stale flag setting** (`stale_flags_limit_days`), or `--stale-days`.

### 2. Surgery (AI, with a human in charge)

For each stale flag, Flag Doctor sends each affected file to an AI with exact instructions: *"this flag is permanently ON, keep that path and remove the check"*. The AI returns the rewritten file as structured JSON (new content, summary, risk level, reviewer notes).

| `--ai` | Model | Cost |
|---|---|---|
| `claude` | Claude (`claude-opus-5-5`) | paid API key |
| `gemini` | Google Gemini (`gemini-3.6-flash`, falls back to Flash-Lite when busy) | **free tier** key from AI Studio |
| `auto` (default) | Claude if `ANTHROPIC_API_KEY` is set, otherwise Gemini | |

Flag Doctor then:

1. **Checks the new code still parses** (Python via `compile`, JavaScript via `node --check`). AI models make mistakes: in our own PawNest test, Gemini dropped a closing `}` and called it "low risk". Flag Doctor catches that, marks it **HIGH risk / BROKEN CODE**, and defaults the answer to "no".
2. Also marks a change **high risk** if the flag name still appears in the new file.
3. Shows a colored **diff** for every file.
4. Asks **you** to approve each file (`y/n`). Nothing is written without a yes.
4. Asks before **creating a git branch + commit**, and again before **pushing and opening a GitHub PR** with a full description.
5. Asks before **archiving the flag in Flagsmith**. By default the answer is no, because the right time is after the PR is merged.

### Safety rules built in

- Never touches flags with overrides or variants (`needs-human`).
- Refuses to start if your repo has uncommitted changes, so its edits stay separate.
- `--dry-run` shows every diff and writes nothing.
- Every write (file, git, GitHub, Flagsmith) needs an explicit human "yes".

## How Flagsmith is used

Flagsmith is the source of truth. Flag Doctor uses the **Flagsmith Admin API** (`Authorization: Api-Key …`):

| Endpoint | Used for |
|---|---|
| `GET /projects/{id}/` | project name + its stale-flag day limit |
| `GET /projects/{id}/environments/` | list every environment |
| `GET /projects/{id}/features/` | all flags, last-modified time, overrides, variants |
| `GET /environments/{key}/featurestates/` | ON/OFF + value of every flag in every environment |
| `GET /environments/{key}/experiments/` | experiment status, rollout decision and the winning variant |
| `PATCH /projects/{id}/features/{id}/` | archive a flag (after approval) |
| `POST /projects/{id}/code-references/` | upload where each flag is used in code |
| `POST /projects/{id}/features/` | create the demo flags (`demo/seed_flagsmith.py`) |

Works with **Flagsmith Cloud** and **self-hosted Flagsmith** (set `FLAGSMITH_API_URL`).

**Two ways to connect:**
- `--use-cli`: every call goes through the **Flagsmith CLI** (`flagsmith api …`), using your `flagsmith login` session and the project in `flagsmith.json` (written by `flagsmith init`). No API key in any file.
- Default: an Organisation API key in `.env`, good for CI.

---

## Setup

Needs Python 3.10+ and Git. For opening PRs you also need the [GitHub CLI](https://cli.github.com) (`gh auth login`).

```bash
git clone <this repo>
cd flag-doctor
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # then fill it in
```

`.env`:

| Variable | Where to get it |
|---|---|
| `FLAGSMITH_API_KEY` | Flagsmith → Organisation Settings → API Keys |
| `FLAGSMITH_PROJECT_ID` | the number in the dashboard URL `/project/<id>/…` |
| `FLAGSMITH_API_URL` | keep the default for Cloud; self-hosted: `http://localhost:8000/api/v1` |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) (only needed for `fix`) |
| `GEMINI_API_KEY` | free alternative: [aistudio.google.com/apikey](https://aistudio.google.com/apikey) → "Create API key in new project". Install with `pip install -e ".[gemini]"` |

## Try it in 30 seconds (no accounts needed)

The `demo/` folder contains a small pet-shop app (Python backend + JS frontend) and a snapshot of its flags:

```bash
flagdoctor scan --repo demo/pet-shop --offline demo/flags-snapshot.json
```

```
| Flag                | Verdict     | Uses in code | Why                                                  |
|---------------------+-------------+--------------+------------------------------------------------------|
| new_checkout        | stale-on    |            2 | ON in all 2 environment(s), unchanged for 176 days.  |
| holiday_banner_2024 | stale-off   |            2 | OFF in all 2 environment(s), unchanged for 688 days. |
| legacy_search       | unused      |            0 | No references found in the scanned code.             |
| new_onbaording      | ghost       |            1 | Used in 1 place(s) but missing from Flagsmith.       |
| pricing_experiment  | needs-human |            1 | Same on/off everywhere, but has overrides, variants… |
| dark_mode           | healthy     |            1 | Changed 5 day(s) ago (stale after 30).               |
```

Then let Claude propose the cleanup (needs `ANTHROPIC_API_KEY`):

```bash
flagdoctor fix --repo demo/pet-shop --offline demo/flags-snapshot.json --dry-run
```

## Use it on a real Flagsmith project

With the Flagsmith CLI (recommended):

```bash
npm install -g @flagsmith/cli      # Flagsmith CLI v2
flagsmith login
cd path/to/your/app && flagsmith init --project <id> --environment <env key>
flagdoctor scan --repo path/to/your/app --use-cli
flagdoctor fix  --repo path/to/your/app --use-cli
```

Or with an API key in `.env`:

```bash
python demo/seed_flagsmith.py              # optional: creates the demo flags in your project
flagdoctor scan --repo path/to/your/app --report flag-doctor-report.md
flagdoctor fix  --repo path/to/your/app    # or: flagdoctor fix new_checkout
flagdoctor archive                         # archive unused flags
flagdoctor sync-refs --repo path/to/your/app
```

Brand-new demo flags aren't "old" yet, so add `--stale-days 0` while trying it out.

## Project structure

```
flag_doctor/
  flagsmith_api.py  Flagsmith Admin API client (+ offline snapshot mode)
  scanner.py        finds flag usages in code (15+ languages, ghost-flag detection)
  diagnose.py       rule-based verdicts (stale-on / stale-off / unused / ghost / needs-human / healthy)
  surgeon.py        Claude rewrites one file for one flag (structured output)
  git_ops.py        branch, commit, push, `gh pr create`
  cli.py            the `flagdoctor` command and all approval prompts
demo/               pet-shop demo app, flag snapshot, Flagsmith seed script
tests/              pytest suite (`pytest -q`)
```

## Tech

Python · Flagsmith Admin API + Flagsmith CLI · Anthropic Claude API (`claude-opus-5-5`, structured outputs, server-side refusal fallback) or Google Gemini API (free tier, structured outputs) · Rich · Git / GitHub CLI

## License

MIT
