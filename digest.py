"""
Builds and "sends" digest emails for subscribers. Sending is stubbed out in
this prototype (writes to an outbox/ folder instead of a real SMTP/email
API call) so the whole pipeline can be demonstrated without needing real
credentials. Swap `send_email()` for a call to Postmark, SendGrid, AWS SES,
or similar before going live — the rest of the pipeline does not need to
change.
"""

import os
from datetime import datetime
from pathlib import Path

import requests

import db
from planit_client import fetch_applications

OUTBOX_DIR = Path(__file__).parent / "outbox"
OUTBOX_DIR.mkdir(exist_ok=True)

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_FROM_ADDRESS = os.environ.get("RESEND_FROM_ADDRESS", "alerts@yourdomain.example")


def build_digest_for_subscriber(subscriber: dict, preview: bool = False) -> tuple[str, str, list[dict]]:
    """
    Returns (subject, body_text, new_records) for one subscriber.

    When preview=True (the live "show me a preview" page a visitor waits on
    in their browser right after signing up), we deliberately search a
    smaller area and a shorter window than their real saved alert. PlanIt's
    API takes longer to answer the bigger the search radius and lookback
    are — a real subscriber's daily digest email runs later in the
    background where nobody is watching a loading spinner, but the
    interactive preview page has someone waiting right now, so we keep
    that one fast rather than risk another timeout.
    """
    effective_radius = min(subscriber["radius_km"], 3.0) if preview else subscriber["radius_km"]
    effective_days = 3 if preview else 7
    records = fetch_applications(
        postcode=subscriber["postcode"],
        radius_km=effective_radius,
        keywords=subscriber["keywords"],
