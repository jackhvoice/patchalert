"""
SMS/WhatsApp alerts via Twilio's REST API, called directly over HTTPS (no
`twilio` SDK dependency needed for one endpoint).

This is entirely opt-in and off by default — nothing is sent, and no
Twilio account is required, until you set all three environment variables
below. Used for exactly one thing: texting "pro" plan subscribers who've
opted in when a newly-approved (hottest) application shows up in their
daily digest — see digest.py's run_daily_digest(). It rides on the same
once-a-day job as the email digest, so despite the name this is not a
true real-time alert; it's a same-day text alongside the email, which
still tends to get opened faster than an inbox someone doesn't check
often.

Set these when you're ready to turn it on:
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_FROM_NUMBER   - your Twilio number, e.g. "+441234567890" for SMS,
                         or "whatsapp:+14155238886" for WhatsApp (Twilio's
                         sandbox/business number format)
"""

import os

import requests

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")
IS_CONFIGURED = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER)


def _normalize_uk_number(phone: str) -> str:
    """Twilio needs international format. Converts a UK domestic number
    starting with '0' to '+44...' as a convenience; leaves anything already
    starting with '+' (or 'whatsapp:+') untouched."""
    phone = (phone or "").strip().replace(" ", "")
    if phone.startswith("0"):
        return "+44" + phone[1:]
    return phone


def send_sms(to_number: str, body: str):
    """Returns a status string on success, a 'FAILED: ...' string on
    failure, or None if Twilio isn't configured or no number was given.
    Never raises — a failed text is a shame, not a reason to break the
    daily digest run, since email remains the primary, reliable channel."""
    if not IS_CONFIGURED or not to_number:
        return None

    to_number = _normalize_uk_number(to_number)
    # If the sender is a WhatsApp number, the recipient needs the same prefix.
    if TWILIO_FROM_NUMBER.startswith("whatsapp:") and not to_number.startswith("whatsapp:"):
        to_number = f"whatsapp:{to_number}"

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    try:
        resp = requests.post(
            url,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={"From": TWILIO_FROM_NUMBER, "To": to_number, "Body": body[:1500]},
            timeout=10,
        )
        resp.raise_for_status()
        return f"sent, sid: {resp.json().get('sid')}"
    except Exception as exc:
        return f"FAILED: {exc}"
