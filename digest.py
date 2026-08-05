"""
Builds and sends digest emails (and, optionally, SMS/WhatsApp texts for
pro subscribers) via the Resend and Twilio APIs if configured, otherwise
falls back to writing an "email" to disk under outbox/ so the whole
pipeline stays demonstrable without real credentials.
"""

import os
from datetime import datetime
from pathlib import Path

import requests

import db
import sms
from planit_client import fetch_applications

OUTBOX_DIR = Path(__file__).parent / "outbox"
OUTBOX_DIR.mkdir(exist_ok=True)

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_FROM_ADDRESS = os.environ.get("RESEND_FROM_ADDRESS", "alerts@yourdomain.example")

# Set to notify yourself (a normal inbox, not a subscriber) if a daily run
# has failures or comes back completely empty for everyone — otherwise a
# multi-day PlanIt outage or a Resend problem could go unnoticed until a
# paying customer complains. Leave unset to skip this (no crash either way).
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL")

# Turns on trial-expiry enforcement for the real (non-preview) digest. Off
# by default on purpose: this matches the documented "free during early
# access, pricing later" launch strategy — flip it to "1" once you're
# ready to actually require payment after someone's trial runs out.
ENFORCE_BILLING = os.environ.get("ENFORCE_BILLING", "0") == "1"
TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "21"))

# Plan-based radius cap, applied per patch (see _subscriber_patches below):
# "pro" subscribers get a wider area watched per patch, can add extra
# patches at all, and get their newly-approved applications surfaced first
# and optionally texted — the main way the two plans differ day-to-day,
# alongside price.
BASIC_RADIUS_CAP_KM = 5.0
PRO_RADIUS_CAP_KM = 15.0

# Used only for the post-signup preview (preview=True): escalate through
# progressively wider searches until something real is found, the same way
# the anonymous pre-signup search page does in app.py. Without this, someone
# could see real matches on the search page, sign up, and then immediately
# see "no results" on the very next page — a confusing regression right at
# the conversion moment. The real (non-preview) daily digest doesn't need
# this: "nothing new today" is a normal, expected outcome for an ongoing
# subscriber, not a make-or-break first impression. Multi-patch also isn't
# relevant here — a preview is specifically about the one search someone
# just ran on the signup page, before any additional patches could exist.
PREVIEW_SEARCH_TIERS = [
    (3.0, 3, True),
    (3.0, 90, True),
    (5.0, 90, True),
    (5.0, 90, False),
]


def _url(path: str) -> str:
    """Builds an absolute link for use inside an email. APP_URL is the same
    variable already used by the GitHub Actions daily-digest trigger, so no
    new environment variable is needed. Falls back to a bare path (still
    correct, just not clickable) if APP_URL isn't set — this must never
    raise, since a broken link shouldn't stop an email from sending."""
    base = os.environ.get("APP_URL", "").rstrip("/")
    return f"{base}{path}" if base else path


def _trial_expired(subscriber: dict) -> bool:
    created = db.parse_timestamp(subscriber.get("created_at"))
    if not created:
        return False
    return (datetime.utcnow() - created).days > TRIAL_DAYS


def _trial_ended_email(subscriber: dict) -> tuple[str, str, list]:
    upgrade_link = _url(f"/upgrade/{subscriber['id']}")
    subject = "Your PatchAlert free trial has ended"
    body = (
        f"Hi {subscriber['name']},\n\n"
        f"Your {TRIAL_DAYS}-day free trial of PatchAlert has ended, so alerts for "
        f"{subscriber['postcode']} are paused for now.\n\n"
        f"Subscribe to keep getting matching planning applications delivered daily:\n"
        f"{upgrade_link}\n\n"
        "If you'd rather not continue, no action needed — you simply won't receive "
        "further alerts, and nothing has been charged."
    )
    return subject, body, []


def _footer_lines(subscriber: dict) -> list:
    """Unsubscribe link (required — this is a recurring marketing-style
    email) plus an account/leads dashboard link and a low-friction referral
    line, all keyed off the subscriber's access_token rather than their raw
    database id so these links can't be walked/guessed."""
    lines = [""]
    token = subscriber.get("access_token")
    if token:
        lines.append(f"Your leads so far: {_url(f'/leads/{token}')}")
        lines.append(f"Manage patches, phone alerts, and settings: {_url(f'/account/{token}')}")
        lines.append(
            f"Know another trade who'd find this useful? Forward this email, or send "
            f"them straight to: {_url(f'/signup?ref={token}')}"
        )
        lines.append("")
        lines.append(f"Unsubscribe any time: {_url(f'/unsubscribe/{token}')}")
    return lines


def _subscriber_patches(subscriber: dict) -> list:
    """Primary patch (the subscriber's own signup search) plus any
    additional patches — additional patches only count while the
    subscriber is on the 'pro' plan. Checking the plan here rather than
    just trusting whatever rows exist means downgrading to 'basic'
    immediately stops watching the extra patches (and re-upgrading brings
    them straight back) without needing to delete or restore any rows."""
    patches = [{
        "postcode": subscriber["postcode"],
        "radius_km": subscriber["radius_km"],
        "keywords": subscriber["keywords"],
        "label": subscriber["postcode"],
    }]
    if subscriber.get("plan") == "pro":
        for p in db.get_additional_patches(subscriber["id"]):
            patches.append({
                "postcode": p["postcode"], "radius_km": p["radius_km"],
                "keywords": p["keywords"], "label": p["postcode"],
            })
    return patches


def _describe(r: dict, show_patch: bool) -> list:
    address_line = f"- {r['address']}"
    if show_patch and r.get("_patch_label"):
        address_line += f"  [{r['_patch_label']}]"
    out = [address_line, f"  {r['description']}"]
    if r.get("agent_name"):
        out.append(f"  Agent/architect: {r['agent_name']}")
    out.append(f"  Reference: {r['uid']}  |  Submitted: {r['start_date']}")
    out.append(f"  Details: {r['link']}")
    out.append("")
    return out


def build_digest_for_subscriber(subscriber: dict, preview: bool = False) -> tuple[str, str, list[dict]]:
    """
    Returns (subject, body_text, new_records) for one subscriber.

    When preview=True, this escalates through PREVIEW_SEARCH_TIERS above
    rather than a single fixed search — see that constant's comment for why.

    The real (non-preview) lookback window is much wider than 7 days on
    purpose. Production testing showed PlanIt's `recent` parameter filters
    by original submission date, not by when an application last changed
    status — so a genuinely live lead (e.g. an application that just got
    decided today, arguably the best moment to reach out) is invisible to
    any digest whose window is narrower than how long ago that application
    was first submitted. A 90-day window catches these without any risk of
    duplicate/spammy emails, since already_sent() below still only reports
    applications this subscriber hasn't already been sent.
    """
    if not preview and ENFORCE_BILLING and subscriber.get("subscription_status") != "active" and _trial_expired(subscriber):
        return _trial_ended_email(subscriber)

    multi_patch = False

    if preview:
        records = []
        matched_keywords = True
        effective_radius = effective_days = None
        for radius_cap, days, use_keywords in PREVIEW_SEARCH_TIERS:
            effective_radius = min(subscriber["radius_km"], radius_cap)
            effective_days = days
            matched_keywords = use_keywords
            result = fetch_applications(
                postcode=subscriber["postcode"],
                radius_km=effective_radius,
                keywords=subscriber["keywords"] if use_keywords else None,
                recent_days=effective_days,
            )
            if result:
                records = result
                break
    else:
        radius_cap = PRO_RADIUS_CAP_KM if subscriber.get("plan") == "pro" else BASIC_RADIUS_CAP_KM
        effective_days = 90
        matched_keywords = True
        patches = _subscriber_patches(subscriber)
        multi_patch = len(patches) > 1
        effective_radius = radius_cap  # approximate — only used in single-patch messaging below

        records = []
        seen_uids = set()
        for patch in patches:
            patch_radius = min(patch["radius_km"], radius_cap)
            try:
                found = fetch_applications(
                    postcode=patch["postcode"],
                    radius_km=patch_radius,
                    keywords=patch["keywords"],
                    recent_days=effective_days,
                )
            except requests.exceptions.RequestException:
                # One bad patch (typo'd postcode, transient PlanIt error for
                # that specific lookup) shouldn't take down every other
                # patch this subscriber is watching.
                continue
            for r in found:
                if r["uid"] in seen_uids:
                    continue  # overlapping patches can return the same application twice
                seen_uids.add(r["uid"])
                r["_patch_label"] = patch["label"]
                records.append(r)

    new_records = [r for r in records if not db.already_sent(subscriber["id"], r["uid"])]
    approved = [r for r in new_records if r.get("_stage") == "approved"]
    rest = [r for r in new_records if r.get("_stage") != "approved"]

    if preview and not new_records:
        scope_note = (
            f" — we checked as wide as {effective_radius:.0f}km over the last {effective_days} "
            "days and found nothing at all, so this looks like a genuinely quiet patch right now"
        )
    elif preview and not matched_keywords:
        scope_note = (
            f" — nothing matched '{subscriber['keywords']}' specifically, so this shows general "
            f"nearby activity within {effective_radius:.0f}km/{effective_days} days instead; your "
            f"real alert will keep watching specifically for '{subscriber['keywords']}' at your "
            f"full {subscriber['radius_km']:.0f}km radius"
        )
    elif preview:
        scope_note = (
            f" (a quick {effective_radius:.0f}km/{effective_days}-day taster for speed — your "
            f"real alert will cover the full {subscriber['radius_km']:.0f}km and 7 days)"
        )
    else:
        scope_note = ""

    if not new_records:
        if not preview and multi_patch:
            subject = "No new matching planning applications across your patches today"
            body = (
                f"Hi {subscriber['name']},\n\n"
                f"No new planning applications matching your alerts across the "
                f"{len(_subscriber_patches(subscriber))} patches you're watching, since your "
                "last alert.\n\nWe'll keep watching and email you as soon as something new comes up."
            )
        else:
            subject = f"No new matching planning applications near {subscriber['postcode']} today"
            body = (
                f"Hi {subscriber['name']},\n\n"
                f"No new planning applications matching '{subscriber['keywords']}' within "
                f"{effective_radius:.0f}km of {subscriber['postcode']} since your last alert"
                f"{scope_note}.\n\n"
                "We'll keep watching and email you as soon as something new comes up."
            )
        body += "\n" + "\n".join(_footer_lines(subscriber))
        return subject, body, []

    lines = [f"Hi {subscriber['name']},", ""]

    if approved:
        lines.append(
            f"{len(approved)} of these just got APPROVED — the best moment to reach out, "
            "before other trades even hear about it:"
        )
        lines.append("")
        for r in approved:
            lines.extend(_describe(r, multi_patch))

    if rest:
        if approved:
            lines.append("Also newly submitted:")
        elif multi_patch:
            lines.append(f"{len(rest)} new planning application(s) across your watched patches:")
        else:
            lines.append(
                f"{len(rest)} new planning application(s) matching your alert "
                f"('{subscriber['keywords']}' within {effective_radius:.0f}km of "
                f"{subscriber['postcode']}){scope_note}:"
            )
        lines.append("")
        for r in rest:
            lines.extend(_describe(r, multi_patch))

    lines.append("Reach out early — you'll usually be first to know before local competitors.")
    lines.extend(_footer_lines(subscriber))

    if approved and rest:
        subject = f"{len(approved)} newly approved + {len(rest)} new application(s) near {subscriber['postcode']}"
    elif approved:
        subject = f"{len(approved)} newly approved application(s) near {subscriber['postcode']}"
    else:
        subject = f"{len(new_records)} new planning application(s) near {subscriber['postcode']}"

    body = "\n".join(lines)
    return subject, body, new_records


def send_email(to_email: str, subject: str, body: str):
    """
    Sends via the Resend API (https://resend.com) if RESEND_API_KEY is
    set. Otherwise falls back to writing the 'email' to disk under
    outbox/ so the whole pipeline stays demonstrable and testable without
    real credentials.
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


def _maybe_send_sms(subscriber: dict, new_records: list):
    """Texts pro subscribers who've opted in and given a phone number, but
    only about newly-approved applications (the hottest, act-now leads) —
    not every routine submission, to keep this from feeling spammy or
    running up needless per-message cost. Runs once a day alongside the
    email (see the module docstring on why this isn't truly real-time).
    Returns a status string, or None if nothing was sent."""
    if subscriber.get("plan") != "pro" or subscriber.get("sms_opt_in") != "1" or not subscriber.get("phone"):
        return None
    approved = [r for r in new_records if r.get("_stage") == "approved"]
    if not approved:
        return None
    token = subscriber.get("access_token")
    link = _url(f"/leads/{token}") if token else ""
    body = (
        f"PatchAlert: {len(approved)} application(s) near you just got APPROVED — "
        f"the best moment to reach out. {link}".strip()
    )
    return sms.send_sms(subscriber["phone"], body)


def _maybe_notify_admin(summary: list):
    """Emails ADMIN_EMAIL (you) if the run had failures or came back
    completely empty for every subscriber — so a silent multi-day outage
    doesn't go unnoticed until a paying customer complains. Never lets a
    problem here break the digest run itself."""
    if not ADMIN_EMAIL or not summary:
        return
    try:
        failed = [s for s in summary if str(s["send_result"]).startswith("FAILED")]
        all_zero = all(s["new_matches"] == 0 for s in summary)
        if not failed and not all_zero:
            return
        lines = [f"Daily digest run — {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
        if failed:
            lines.append(f"{len(failed)} of {len(summary)} subscriber(s) FAILED to send:")
            for s in failed:
                lines.append(f"  - {s['subscriber']}: {s['send_result']}")
            lines.append("")
        if all_zero:
            lines.append(
                f"All {len(summary)} subscriber(s) got zero new matches today. Probably "
                "just a quiet day, but worth a quick check that PlanIt is responding normally "
                "if this repeats for more than a day or two."
            )
        send_email(ADMIN_EMAIL, "PatchAlert daily digest — needs a look", "\n".join(lines))
    except Exception:
        pass


def run_daily_digest() -> list[dict]:
    """
    This is what a scheduled daily job runs: for every subscriber, build
    their digest, send it (really, via Resend, if RESEND_API_KEY is set —
    otherwise to outbox/ as a stand-in), text pro subscribers about
    approved applications if they've opted in, and mark matched
    applications as sent so they aren't repeated tomorrow. Returns a
    summary for logging.
    """
    summary = []
    for subscriber in db.get_all_subscribers():
        try:
            subject, body, new_records = build_digest_for_subscriber(subscriber)
            send_result = send_email(subscriber["email"], subject, body)
            sms_result = _maybe_send_sms(subscriber, new_records)
            for r in new_records:
                db.mark_sent(
                    subscriber["id"], r["uid"],
                    address=r.get("address"), description=r.get("description"),
                    link=r.get("link"), stage=r.get("_stage"),
                )
            summary.append({
                "subscriber": subscriber["email"],
                "new_matches": len(new_records),
                "send_result": send_result,
                "sms_result": sms_result,
            })
        except Exception as exc:
            # One subscriber's failure (bad postcode, PlanIt hiccup, etc.)
            # shouldn't stop everyone else's digest from going out.
            summary.append({
                "subscriber": subscriber["email"],
                "new_matches": 0,
                "send_result": f"FAILED: {exc}",
                "sms_result": None,
            })
    _maybe_notify_admin(summary)
    return summary


if __name__ == "__main__":
    db.init_db()
    results = run_daily_digest()
    for r in results:
        extra = f" | sms: {r['sms_result']}" if r.get("sms_result") else ""
        print(f"{r['subscriber']}: {r['new_matches']} new match(es) -> {r['send_result']}{extra}")
