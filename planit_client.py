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
documented lat/lng/krad/recent params) and does keyword filtering itself
in Python — the same logic already used for the bundled fixture data, so
there's only one matching code path to trust instead of two.

Design note 2: this client also does NOT pass a raw postcode to PlanIt's
own `pcode` parameter. A live production test showed PlanIt's own
postcode-to-location lookup taking ~30 seconds and still returning no
records (confirmed via PlanIt's own `secs_taken` field in the response).
Instead, this client geocodes the postcode itself via postcodes.io (a
free, fast, no-key UK postcode API) and queries PlanIt with `lat`/`lng`
directly, which PlanIt's docs describe as the optimized circular-search
path.

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
GEOCODE_BASE = "https://api.postcodes.io/postcodes"
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


def _geocode_postcode(postcode: str) -> tuple[float, float]:
    """
    Converts a UK postcode to (latitude, longitude) via postcodes.io — a
    free, fast, no-key-required UK postcode lookup service. See the module
    docstring for why we do this ourselves instead of letting PlanIt
    geocode the raw postcode.
    """
    compact = postcode.replace(" ", "").upper()
    resp = requests.get(f"{GEOCODE_BASE}/{compact}", timeout=8)
    resp.raise_for_status()
    result = resp.json()["result"]
    return result["latitude"], result["longitude"]


def _matches_keywords(record: dict, terms: list) -> bool:
    text = (record.get("description") or "").lower()
    return any(term in text for term in terms)


def fetch_applications(
    postcode: str,
    radius_km: float = 3.0,
    keywords: str | None = None,
    recent_days: int = 7,
    page_size: int = 100,
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

    lat, lng = _geocode_postcode(postcode)

    params = {
        "lat": lat,
        "lng": lng,
        "krad": radius_km,
        "recent": recent_days,
        "pg_sz": page_size,
        # Ask PlanIt for only the fields we actually use — a smaller
        # response is faster for them to build and for us to receive,
        # which helps avoid the read timeout a full-fat response can hit.
        "select": "uid,area_name,start_date,address,description,link",
    }

    headers = {
        "User-Agent": "PatchAlertBot/1.0 (+https://patchalert.onrender.com)",
        "Accept": "application/json",
    }
    resp = requests.get(API_BASE, params=params, headers=headers, timeout=25)
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
