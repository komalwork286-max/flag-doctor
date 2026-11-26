"""Creates the demo flags in YOUR Flagsmith project so you can try Flag Doctor for real.

Usage (from the flag-doctor folder, with .env filled in):
    python demo/seed_flagsmith.py

Then set the environment values in the Flagsmith dashboard to match the story:
    new_checkout        ON  in every environment   -> stale-on
    holiday_banner_2024 OFF in every environment   -> stale-off
    dark_mode           ON in Development only     -> healthy (rolling out)
    pricing_experiment  add 2 variations           -> needs-human
    legacy_search       OFF, never used in code    -> unused
Because the flags are brand new, run the scan with --stale-days 0 to treat them as old.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flag_doctor.flagsmith_api import FlagsmithClient, FlagsmithError  # noqa: E402

DEMO_FLAGS = [
    ("new_checkout", True, "One-page checkout. Fully rolled out."),
    ("holiday_banner_2024", False, "Seasonal banner from last year."),
    ("dark_mode", False, "Dark theme, rolling out."),
    ("pricing_experiment", True, "A/B test of pricing page layout."),
    ("legacy_search", False, "Old search page. Code was deleted long ago."),
]


def main() -> None:
    load_dotenv()
    client = FlagsmithClient(os.environ.get("FLAGSMITH_API_KEY", ""), os.environ.get("FLAGSMITH_PROJECT_ID", ""),
                             os.environ.get("FLAGSMITH_API_URL", "https://api.flagsmith.com/api/v1"))
    existing = {f.name for f in client.load_flags()}
    for name, enabled, description in DEMO_FLAGS:
        if name in existing:
            print(f"exists   {name}")
            continue
        try:
            client.create_flag(name, enabled, description)
            print(f"created  {name}")
        except FlagsmithError as e:
            print(f"failed   {name}: {e}")


if __name__ == "__main__":
    main()
