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


def build_digest_for_subscriber(subscriber: dict) -> tuple[str, str, list[dict]]:
    """Returns (subject, body_text, new_records) for one subscriber."""
    records = fetch_applications(
        postcode=subscriber["postcode"],
        radius_km=subscriber["radius_km"],
        keywords=subscriber["keywords"],
        recent_days=7,
    )
    new_records = [r for r in records if not db.already_sent(subscriber["id"], r["uid"])]

    if not new_records:
        subject = f"No new matching planning applications near {subscriber['postcode']} today"
        body = (
            f"Hi {subscriber['name']},\n\n"
            f"No new planning applications matching '{subscriber['keywords']}' within "
            f"{subscriber['radius_km']}km of {subscriber['postcode']} since your last alert.\n\n"
            "We'll keep watching and email you as soon as something new comes up."
        )
        return subject, body, []

    lines = [
        f"Hi {subscriber['name']},",
        "",
        f"{len(new_records)} new planning application(s) matching your alert "
        f"('{subscriber['keywords']}' within {subscriber['radius_km']}km of {subscriber['postcode']}):",
        "",
    ]
    for r in new_records:
        lines.append(f"- {r['address']}")
        lines.append(f"  {r['description']}")
        lines.append(f"  Reference: {r['uid']}  |  Submitted: {r['start_date']}")
        lines.append(f"  Details: {r['link']}")
        lines.append("")
    lines.append("Reach out early — you'll usually be first to know before local competitors.")

    subject = f"{len(new_records)} new planning application(s) near {subscriber['postcode']}"
    body = "\n".join(lines)
    return subject, body, new_records


def send_email(to_email: str, subject: str, body: str):
    """
    Sends via the Resend API (https://resend.com — simple REST API, easy
    free tier) if RESEND_API_KEY is set. Otherwise falls back to writing
    the 'email' to disk under outbox/ so the whole pipeline stays
    demonstrable and testable without real credentials.

    To go live: sign up at resend.com, verify your sending domain, set
    RESEND_API_KEY and RESEND_FROM_ADDRESS as environment variables on
    your host. No code change needed beyond that.
    """
    if not RESEND_API_KEY:
        safe_ts = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
        out_path = OUTBOX_DIR / f"{safe_ts}_{to_email.replace('@', '_at_')}.txt"
        out_path.write_text(f"To: {to_email}\nSubject: {subject}\n\n{body}\n")
        return str(out_path)

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={
            "from": RESEND_FROM_ADDRESS,
            "to": [to_email],
            "subject": subject,
            "text": body,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return f"sent via Resend, message id: {resp.json().get('id')}"


def run_daily_digest() -> list[dict]:
    """
    This is what a scheduled daily job runs: for every subscriber, build
    their digest, send it (really, via Resend, if RESEND_API_KEY is set —
    otherwise to outbox/ as a stand-in), and mark matched applications as
    sent so they aren't repeated tomorrow. Returns a summary for logging.
    """
    summary = []
    for subscriber in db.get_all_subscribers():
        subject, body, new_records = build_digest_for_subscriber(subscriber)
        send_result = send_email(subscriber["email"], subject, body)
        for r in new_records:
            db.mark_sent(subscriber["id"], r["uid"])
        summary.append({
            "subscriber": subscriber["email"],
            "new_matches": len(new_records),
            "send_result": send_result,
        })
    return summary


if __name__ == "__main__":
    db.init_db()
    results = run_daily_digest()
    for r in results:
        print(f"{r['subscriber']}: {r['new_matches']} new match(es) -> {r['send_result']}")
