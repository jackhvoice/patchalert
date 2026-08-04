"""
Client for the UK PlanIt API (https://www.planit.org.uk/api/) — a free,
national aggregator of planning application data covering ~420 UK planning
authorities. No API key is required as of the documentation reviewed for
this build (August 2026); re-check https://www.planit.org.uk/api/ before
going live in case usage terms have changed.

NOTE ON SANDBOX TESTING: this build environment has no outbound access to
arbitrary internet hosts, so the real HTTP path in `fetch_applications()`
could not be exercised live during development. Set `PLANIT_USE_FIXTURE=1`
(the default in this prototype) to run against the bundled sample response
in fixtures/sample_planit_response.json instead. Once this app is deployed
somewhere with normal internet access, unset that variable (or pass
use_fixture=False) to hit the real API.
"""

import json
import os
from pathlib import Path

import requests

API_BASE = "https://www.planit.org.uk/api/applics/json"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_planit_response.json"


def fetch_applications(
    postcode: str,
    radius_km: float = 3.0,
    keywords: str | None = None,
    recent_days: int = 7,
    page_size: int = 200,
    use_fixture: bool | None = None,
) -> list[dict]:
    """
    Fetch planning applications near a postcode, optionally filtered by a
    keyword search against the application description (e.g. "extension"
    OR "loft conversion" OR "garage conversion").

    Returns a list of raw record dicts as provided by the PlanIt API.
    """
    if use_fixture is None:
        use_fixture = os.environ.get("PLANIT_USE_FIXTURE", "1") == "1"

    if use_fixture:
        with open(FIXTURE_PATH) as f:
            data = json.load(f)
        records = data["records"]
        if keywords:
            terms = [t.strip().lower() for t in keywords.split(",") if t.strip()]
            records = [r for r in records if any(t in r.get("description", "").lower() for t in terms)]
        return records

    params = {
        "pcode": postcode,
        "krad": radius_km,
        "recent": recent_days,
        "pg_sz": page_size,
    }
    if keywords:
        # PlanIt's `search` param supports quoted phrases joined with "or"
        terms = [t.strip() for t in keywords.split(",") if t.strip()]
        params["search"] = " or ".join(f'"{t}"' for t in terms)

    resp = requests.get(API_BASE, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("records", [])
