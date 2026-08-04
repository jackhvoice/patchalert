"""
Client for the UK PlanIt API (https://www.planit.org.uk/api/) — a free,
national aggregator of planning application data covering ~420 UK planning
authorities. No API key is required as of the documentation reviewed for
this build (August 2026); re-check https://www.planit.org.uk/api/ before
going live in case usage terms have changed.

Design note: this client deliberately does NOT rely on PlanIt's `search`
query parameter to do keyword matching server-side. A live test against a
real postcode returned a 400 Bad Request when `search` was included, and
this environment's fetch tools are blocked by PlanIt's robots.txt from
directly inspecting the exact grammar it expects. Rather than guess at
that syntax, this client fetches by location only (the simple, clearly
documented pcode/krad/recent params) and does keyword filtering itself in
Python — the same logic already used for the bundled fixture data, so
there's only one matching code path to trust instead of two.

NOTE ON SANDBOX TESTING: this build environment has no outbound access to
arbitrary internet hosts, so the real HTTP path in `fetch_applications()`
could not be exercised live during development, and still can't be
directly verified here even after this fix (see above). Set
`PLANIT_USE_FIXTURE=1` (the default in this prototype) to run against the
bundled sample response in fixtures/sample_planit_response.json instead.
"""

import json
import os
from pathlib import Path

import requests

API_BASE = "https://www.planit.org.uk/api/applics/json"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_planit_response.json"


def _normalize_postcode(postcode: str) -> str:
    """
    UK postcodes need a space before the final 3-character inward code
    (e.g. 'TW1 1LU', not 'TW11LU'). Users won't reliably type that space,
    so normalize it here rather than relying on form input being perfect.
    """
    compact = postcode.replace(" ", "").upper()
    if len(compact) > 3:
        return f"{compact[:-3]} {compact[-3:]}"
    return compact


def _matches_keywords(record: dict, terms: list) -> bool:
    text = (record.get("description") or "").lower()
    return any(term in text for term in terms)


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
    keyword match against the application description (e.g. "extension",
    "loft conversion", "garage conversion"). Matching is done locally in
    Python, not via the remote API's own search syntax — see module
    docstring for why.

    Returns a list of raw record dicts as provided by the PlanIt API.
    """
    if use_fixture is None:
        use_fixture = os.environ.get("PLANIT_USE_FIXTURE", "1") == "1"

    terms = [t.strip().lower() for t in keywords.split(",") if t.strip()] if keywords else []

    if use_fixture:
        with open(FIXTURE_PATH) as f:
            data = json.load(f)
        records = data["records"]
        if terms:
            records = [r for r in records if _matches_keywords(r, terms)]
        return records

    params = {
        "pcode": _normalize_postcode(postcode),
        "krad": radius_km,
        "recent": recent_days,
        "pg_sz": page_size,
    }

    resp = requests.get(API_BASE, params=params, timeout=15)
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        # Surface PlanIt's actual error body in the logs (raise_for_status
        # alone discards it), so any future failure is diagnosable
        # straight from a Render log line instead of needing guesswork.
        raise requests.exceptions.HTTPError(
            f"{exc} — response body: {resp.text[:500]}", response=resp
        ) from exc

    records = resp.json().get("records", [])
    if terms:
        records = [r for r in records if _matches_keywords(r, terms)]
    return records
