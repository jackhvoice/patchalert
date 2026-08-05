"""
Builds and sends digest emails (and, optionally, SMS/WhatsApp texts for
pro subscribers) via the Resend and Twilio APIs if configured, otherwise
falls back to writing an "email" to disk under outbox/ so the whole
pipeline stays demonstrable without real credentials.
"""

import os
from datetime import datetime
from html import escape as _esc
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

_ALL_SIZES = {"small", "medium", "large"}


def _parse_sizes(sizes_value) -> set | None:
    """Turns the subscribers.sizes column (e.g. 'small,medium') into a set
    for planit_client.fetch_applications()'s sizes= param. Missing, blank,
    or unrecognised values are treated as "no filter" (watch every size) —
    the same safe default as before this column existed, so an old
    subscriber row (or any bug) never silently narrows someone's alert to
    nothing they didn't ask for."""
    if not sizes_value:
        return None
    parsed = {s.strip().lower() for s in sizes_value.split(",") if s.strip()} & _ALL_SIZES
    return parsed or None


def _format_keywords(keywords: str, max_shown: int = 3) -> str:
    """Turns the raw comma-separated keywords string someone typed on the
    signup form (e.g. 'extension,rear extension,side extension,two storey,
    single storey,dormer,rear addition') into a short, natural-sounding
    phrase for use inside an email, instead of dumping the entire raw list
    verbatim — which reads as cluttered/technical once someone has more than
    a couple of keywords. This only affects displayed text; the actual
    search still checks every keyword regardless of how many are shown
    here."""
    parts = [k.strip() for k in (keywords or "").split(",") if k.strip()]
    if not parts:
        return keywords or ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} or {parts[1]}"
    if len(parts) <= max_shown:
        return ", ".join(parts[:-1]) + f", or {parts[-1]}"
    shown = ", ".join(parts[:max_shown])
    remaining = len(parts) - max_shown
    return f"{shown}, or {remaining} other keyword{'s' if remaining != 1 else ''} you're watching for"


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


def _trial_ended_email(subscriber: dict) -> tuple[str, str, str, list]:
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
    body_html = f"""
      <p style="margin:0 0 16px; font-size:15px; color:#182430;">Hi {_esc(subscriber['name'])},</p>
      <p style="margin:0 0 16px; font-size:14px; color:#182430; line-height:1.6;">
        Your {TRIAL_DAYS}-day free trial of PatchAlert has ended, so alerts for
        <strong>{_esc(subscriber['postcode'])}</strong> are paused for now.
      </p>
      <p style="margin:0 0 20px;">
        <a href="{_esc(upgrade_link)}" style="display:inline-block; background:#EF6A33; color:#FFFFFF;
           font-weight:700; font-size:14px; text-decoration:none; padding:12px 22px; border-radius:999px;">
          Subscribe to keep your alerts
        </a>
      </p>
      <p style="margin:0; font-size:13px; color:#667085;">
        If you'd rather not continue, no action needed — you simply won't receive further
        alerts, and nothing has been charged.
      </p>
    """
    html_body = _html_wrapper(body_html, "")
    return subject, body, html_body, []


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
    them straight back) without needing to delete or restore any rows.

    Only the primary patch carries a development-size filter for now — the
    "patches" table (extra Pro patches) doesn't have its own sizes column
    yet, so additional patches always watch every size (sizes=None below)
    until that's added as a follow-up."""
    patches = [{
        "postcode": subscriber["postcode"],
        "radius_km": subscriber["radius_km"],
        "keywords": subscriber["keywords"],
        "label": subscriber["postcode"],
        "sizes": _parse_sizes(subscriber.get("sizes")),
    }]
    if subscriber.get("plan") == "pro":
        for p in db.get_additional_patches(subscriber["id"]):
            patches.append({
                "postcode": p["postcode"], "radius_km": p["radius_km"],
                "keywords": p["keywords"], "label": p["postcode"],
                "sizes": None,
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


# ---------- HTML email rendering ----------
# All PlanIt-sourced text (address, description, agent_name, etc.) is
# escaped before being embedded in HTML — this is third-party data from
# council planning registers, not something we control, and descriptions
# routinely contain "&" and similar characters that would otherwise break
# the markup. Inline styles only (no <style> block/external CSS) and a
# table-based layout, since that's what actually renders consistently
# across real-world email clients (Gmail, Outlook, Apple Mail).

_BRAND_NAVY_DARK = "#0B1F33"
_BRAND_NAVY = "#14304D"
_BRAND_ORANGE = "#EF6A33"
_BRAND_INK = "#182430"
_BRAND_MUTED = "#667085"
_BRAND_MUTED_LIGHT = "#98A2B3"
_BRAND_CREAM = "#FBF7F2"
_BRAND_BORDER = "#E8E1D6"


def _describe_html(r: dict, show_patch: bool) -> str:
    patch_tag = ""
    if show_patch and r.get("_patch_label"):
        patch_tag = f' <span style="color:{_BRAND_MUTED}; font-weight:500;">[{_esc(r["_patch_label"])}]</span>'
    agent_line = ""
    if r.get("agent_name"):
        agent_line = (
            f'<p style="margin:0 0 6px; color:{_BRAND_MUTED}; font-size:13px;">'
            f'Agent/architect: {_esc(r["agent_name"])}</p>'
        )
    is_approved = r.get("_stage") == "approved"
    border = _BRAND_ORANGE if is_approved else _BRAND_BORDER
    bg = "#FFF6F0" if is_approved else "#FFFFFF"
    badge = ""
    if is_approved:
        badge = (
            f'<span style="display:inline-block; background:{_BRAND_ORANGE}; color:#FFFFFF; '
            f'font-size:11px; font-weight:700; letter-spacing:0.03em; padding:3px 10px; '
            f'border-radius:999px; margin-bottom:8px;">JUST APPROVED</span><br>'
        )
    return f"""
    <div style="border:1px solid {border}; background:{bg}; border-radius:9px; padding:14px 16px; margin-bottom:12px;">
      {badge}
      <p style="margin:0 0 4px; font-weight:700; color:{_BRAND_INK}; font-size:15px;">{_esc(r.get('address',''))}{patch_tag}</p>
      <p style="margin:0 0 8px; color:{_BRAND_MUTED}; font-size:14px;">{_esc(r.get('description',''))}</p>
      {agent_line}
      <p style="margin:0 0 8px; color:{_BRAND_MUTED_LIGHT}; font-size:12px;">
        Reference: {_esc(r.get('uid',''))} &middot; Submitted: {_esc(r.get('start_date',''))}
      </p>
      <p style="margin:0;">
        <a href="{_esc(r.get('link',''))}" style="color:{_BRAND_NAVY}; font-weight:600; font-size:13px; text-decoration:none;">
          View full application &rarr;
        </a>
      </p>
    </div>"""


def _footer_links_html(subscriber: dict) -> str:
    token = subscriber.get("access_token")
    if not token:
        return ""
    leads_url = _url(f"/leads/{token}")
    account_url = _url(f"/account/{token}")
    referral_url = _url(f"/signup?ref={token}")
    unsub_url = _url(f"/unsubscribe/{token}")
    return f"""
    <p style="margin:0 0 10px; font-size:13px;">
      <a href="{_esc(leads_url)}" style="color:{_BRAND_NAVY}; font-weight:600; text-decoration:none;">Your leads so far</a>
      &nbsp;&middot;&nbsp;
      <a href="{_esc(account_url)}" style="color:{_BRAND_NAVY}; font-weight:600; text-decoration:none;">Manage settings</a>
    </p>
    <p style="margin:0 0 10px; font-size:12px; color:{_BRAND_MUTED};">
      Know another trade who'd find this useful? Forward this email, or send them to:
      <a href="{_esc(referral_url)}" style="color:{_BRAND_NAVY}; text-decoration:none;">{_esc(referral_url)}</a>
    </p>
    <p style="margin:0; font-size:12px; color:{_BRAND_MUTED_LIGHT};">
      <a href="{_esc(unsub_url)}" style="color:{_BRAND_MUTED_LIGHT}; text-decoration:underline;">Unsubscribe</a>
    </p>"""


def _html_wrapper(body_html: str, footer_html: str) -> str:
    footer_block = ""
    if footer_html:
        footer_block = f"""
          <tr>
            <td style="padding: 18px 28px 24px; background:{_BRAND_CREAM}; border-top:1px solid {_BRAND_BORDER};">
              {footer_html}
            </td>
          </tr>"""
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0; padding:0; background:{_BRAND_CREAM}; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_BRAND_CREAM};">
    <tr>
      <td align="center" style="padding: 24px 16px;">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px; width:100%; background:#FFFFFF; border-radius:14px; overflow:hidden; border:1px solid {_BRAND_BORDER};">
          <tr>
            <td style="background:{_BRAND_NAVY_DARK}; padding: 18px 28px;">
              <span style="color:#FFFFFF; font-size:18px; font-weight:700; font-family: Georgia, 'Times New Roman', serif;">PatchAlert</span>
            </td>
          </tr>
          <tr>
            <td style="padding: 28px 28px 8px;">
              {body_html}
            </td>
          </tr>{footer_block}
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def build_digest_for_subscriber(subscriber: dict, preview: bool = False) -> tuple[str, str, str, list[dict]]:
    """
    Returns (subject, body_text, body_html, new_records) for one subscriber.

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
        preview_sizes = _parse_sizes(subscriber.get("sizes"))
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
                sizes=preview_sizes,
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
                    sizes=patch.get("sizes"),
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
            f" — nothing matched {_format_keywords(subscriber['keywords'])} specifically, so this "
            f"shows general nearby activity within {effective_radius:.0f}km/{effective_days} days "
            f"instead; your real alert will keep watching specifically for what you searched for at "
            f"your full {subscriber['radius_km']:.0f}km radius"
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
            message = (
                f"No new planning applications matching your alerts across the "
                f"{len(_subscriber_patches(subscriber))} patches you're watching, since your "
                "last alert."
            )
        else:
            subject = f"No new matching planning applications near {subscriber['postcode']} today"
            message = (
                f"No new planning applications matching {_format_keywords(subscriber['keywords'])} "
                f"within {effective_radius:.0f}km of {subscriber['postcode']} since your last alert"
                f"{scope_note}."
            )
        body = f"Hi {subscriber['name']},\n\n{message}\n\nWe'll keep watching and email you as soon as something new comes up."
        body += "\n" + "\n".join(_footer_lines(subscriber))
        body_html_content = f"""
          <p style="margin:0 0 16px; font-size:15px; color:{_BRAND_INK};">Hi {_esc(subscriber['name'])},</p>
          <p style="margin:0 0 16px; font-size:14px; color:{_BRAND_INK}; line-height:1.6;">{_esc(message)}</p>
          <p style="margin:0 0 20px; font-size:14px; color:{_BRAND_MUTED};">
            We'll keep watching and email you as soon as something new comes up.
          </p>
        """
        html_body = _html_wrapper(body_html_content, _footer_links_html(subscriber))
        return subject, body, html_body, []

    lines = [f"Hi {subscriber['name']},", ""]
    html_sections = [f'<p style="margin:0 0 16px; font-size:15px; color:{_BRAND_INK};">Hi {_esc(subscriber["name"])},</p>']

    if approved:
        lines.append(
            f"{len(approved)} of these just got APPROVED — the best moment to reach out, "
            "before other trades even hear about it:"
        )
        lines.append("")
        for r in approved:
            lines.extend(_describe(r, multi_patch))
        html_sections.append(
            f'<p style="margin:0 0 12px; font-size:14px; color:{_BRAND_INK}; font-weight:600;">'
            f'{len(approved)} of these just got approved — the best moment to reach out, before '
            f'other trades even hear about it:</p>'
        )
        html_sections.extend(_describe_html(r, multi_patch) for r in approved)

    if rest:
        if approved:
            rest_heading = "Also newly submitted:"
        elif multi_patch:
            rest_heading = f"{len(rest)} new planning application(s) across your watched patches:"
        else:
            rest_heading = (
                f"{len(rest)} new planning application(s) matching {_format_keywords(subscriber['keywords'])} "
                f"within {effective_radius:.0f}km of "
                f"{subscriber['postcode']}{scope_note}:"
            )
        lines.append(rest_heading)
        lines.append("")
        for r in rest:
            lines.extend(_describe(r, multi_patch))
        html_sections.append(
            f'<p style="margin:{"20px" if approved else "0"} 0 12px; font-size:14px; color:{_BRAND_INK}; font-weight:600;">'
            f'{_esc(rest_heading)}</p>'
        )
        html_sections.extend(_describe_html(r, multi_patch) for r in rest)

    lines.append("Reach out early — you'll usually be first to know before local competitors.")
    lines.extend(_footer_lines(subscriber))
    html_sections.append(
        f'<p style="margin:20px 0 0; font-size:13px; color:{_BRAND_MUTED};">'
        f'Reach out early — you\'ll usually be first to know before local competitors.</p>'
    )

    if approved and rest:
        subject = f"{len(approved)} newly approved + {len(rest)} new application(s) near {subscriber['postcode']}"
    elif approved:
        subject = f"{len(approved)} newly approved application(s) near {subscriber['postcode']}"
    else:
        subject = f"{len(new_records)} new planning application(s) near {subscriber['postcode']}"

    body = "\n".join(lines)
    html_body = _html_wrapper("".join(html_sections), _footer_links_html(subscriber))
    return subject, body, html_body, new_records


def send_email(to_email: str, subject: str, body: str, html: str | None = None):
    """
    Sends via the Resend API (https://resend.com) if RESEND_API_KEY is
    set. Otherwise falls back to writing the 'email' to disk under
    outbox/ so the whole pipeline stays demonstrable and testable without
    real credentials.

    `html`, if provided, is sent alongside the plain-text body — Resend
    (like virtually every email provider) accepts both in the same
    message, and mail clients that support HTML show that version while
    plain-text-only clients fall back to `body`. Passing html=None keeps
    this a plain-text-only send (used for the outbox/no-API-key path,
    where there's no real client rendering anything anyway).
    """
    if not RESEND_API_KEY:
        safe_ts = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
        out_path = OUTBOX_DIR / f"{safe_ts}_{to_email.replace('@', '_at_')}.txt"
        out_path.write_text(f"To: {to_email}\nSubject: {subject}\n\n{body}\n")
        return str(out_path)

    payload = {
        "from": RESEND_FROM_ADDRESS,
        "to": [to_email],
        "subject": subject,
        "text": body,
    }
    if html:
        payload["html"] = html

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json=payload,
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
            subject, body, html_body, new_records = build_digest_for_subscriber(subscriber)
            send_result = send_email(subscriber["email"], subject, body, html=html_body)
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
