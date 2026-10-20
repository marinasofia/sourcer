"""SOURCER_METROS widens the location rules beyond NYC, read at import time."""

from __future__ import annotations

import json
import os
import subprocess
import sys

PROBE = """
import json
from sourcer import config
print(json.dumps([
    config.location_in_scope("San Francisco, CA"),
    config.location_in_scope("Palo Alto, California"),
    config.location_in_scope("New York, NY"),
    config.location_in_scope("Seattle, WA"),
    config.title_location_conflict("Forward Deployed Engineer (West Coast)"),
    config.title_location_conflict("Data Analyst, Seattle"),
]))
"""


def run(metros: str | None) -> list[bool]:
    env = {k: v for k, v in os.environ.items() if k != "SOURCER_METROS"}
    if metros is not None:
        env["SOURCER_METROS"] = metros
    out = subprocess.run(
        [sys.executable, "-c", PROBE], env=env, capture_output=True, text=True, check=True
    )
    return list(json.loads(out.stdout))


def test_default_is_nyc_only() -> None:
    assert run(None) == [False, False, True, False, True, True]


def test_sf_adds_the_bay_area_and_the_west_coast() -> None:
    assert run("nyc,sf") == [True, True, True, False, False, True]


def test_unknown_metro_falls_back_to_nyc() -> None:
    assert run("mars") == run(None)
