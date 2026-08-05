import logging
import os
from datetime import datetime

import requests as requests_lib
import stripe
from flask import Flask, render_template, request, redirect, url_for, jsonify

import db
from digest import build_digest_for_subscriber, run_daily_digest, ENFORCE_BILLING, TRIAL_DAYS
from planit_client import fetch_applications
from billing import create_checkout_session

logger = logging.getLogger(__name__)

app = Flask(__name__)
db.init_db()

DIGEST_TRIGGER_SECRET = os.environ.get("DIGEST_TRIGGER_SECRET")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")

# Broader than a single word per trade on purpose — real planning
# descriptions are inconsistently worded ("erection of two storey rear
# addition" never contains the word "extension"), so matching on only one
# term per trade was quietly missing genuinely relevant jobs. These lists
# are a reasonable starting point, not exhaustive — worth refining further
# once you can see which phrasings show up most in your own subscribers'
# results.
TRADE_PRESETS = {
    "Extensions": "extension,rear extension,side extension,two storey,single storey,dormer,rear addition",
    "Loft conversions": "loft conversion,loft,dormer window,roof extension",
    "Garage conversions": "garage conversion,garage to,garage into,convert garage",
    "Conservatories": "conservatory,orangery,sunroom,garden room",
    "Outbuildings/annexes": "outbuilding,annexe,annex,garden room,summer house,studio",
}


def _client_ip() -> str:
    """Render sits in front of the app as a proxy, so request.remote_addr
    alone would just be Render's own address — the real visitor IP is in
    X-Forwarded-For (its first entry) when present."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _rate_limited(route: str, max_requests: int, window_minutes: int = 60) -> bool:
    """Returns True (and logs the request either way) if this IP has hit
    the limit for this route. A basic deterrent against a bot or an
    accidental refresh-loop hammering the search/signup endpoints — each
    anonymous search can trigger several outbound calls to postcodes.io
    and PlanIt, so unrestricted abuse here isn't free."""
    ip = _client_ip()
    allowed = db.check_rate_limit(ip, route, max_requests=max_requests, window_minutes=window_minutes)
    db.log_request(ip, route)
    return not allowed


@app.route("/")
def home():
    return render_template("home.html", trade_presets=TRADE_PRESETS, trial_days=TRIAL_DAYS)


@app.route("/pricing")
def pricing():
    return render_template("pricing.html", trial_days=TRIAL_DAYS, enforce_billing=ENFORCE_BILLING)


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """
    Step 1 of signup: search first, no email required. A visitor enters
    their postcode/radius/trade and immediately sees real matching planning
    applications, before we ever ask for an email — much stronger proof the
    product works than asking them to take it on faith. Step 2 (capturing
    name/email) happens in subscribe() below, once they've seen results.
    """
    ref = request.values.get("ref", "").strip()

    if request.method == "POST":
        if _rate_limited("signup", max_requests=20, window_minutes=60):
            return render_template("rate_limited.html"), 429

        selected = request.form.getlist("trades")
        custom = request.form.get("custom_keywords", "").strip()
        keyword_terms = []
        for label in selected:
            keyword_terms.append(TRADE_PRESETS[label])
        if custom:
            keyword_terms.append(custom)
        keywords = ",".join(keyword_terms) if keyword_terms else "extension"
        postcode = request.form["postcode"].strip().upper()
        radius_km = float(request.form.get("radius_km") or 3.0)
        ref = request.form.get("ref", "").strip()

        # Escalating search: try a fast, narrow, keyword-matched search
        # first, and only widen if it comes back empty. This matters for
        # conversion — showing "no results" on someone's very first touch
        # with PatchAlert, before they've given us an email, undermines the
        # whole point of proving the product works. Production testing
        # showed empty searches resolve fast regardless of area/day size
        # (a 5km/90-day search with zero matches took under half a second),
        # while searches that find a lot of matches are what's slow — so
        # each escalation step here is cheap unless it actually finds
        # something, and we stop at the first tier that does.
        #   1. their keywords, capped 3km, 10 days — the common case
        #   2. their keywords, capped 3km, 90 days — catches applications
        #      submitted a while ago but still genuinely relevant (PlanIt's
        #      "recent" filters by original submission date, not by status
        #      changes — confirmed in production)
        #   3. their keywords, capped 5km, 90 days — widen the area too
        #   4. ANY recent local planning activity, no keyword filter — last
        #      resort proof that real data exists nearby, even if nothing
        #      matched their specific trade recently
        ESCALATION_TIERS = [
            (3.0, 10, True),
            (3.0, 90, True),
            (5.0, 90, True),
            (5.0, 90, False),
        ]
        records = None
        matched_keywords = True
        sample_radius = sample_days = None
        for radius_cap, days, use_keywords in ESCALATION_TIERS:
            sample_radius = min(radius_km, radius_cap)
            sample_days = days
            matched_keywords = use_keywords
            try:
                result = fetch_applications(
                    postcode=postcode,
                    radius_km=sample_radius,
                    keywords=keywords if use_keywords else None,
                    recent_days=sample_days,
                )
            except requests_lib.exceptions.RequestException:
                logger.exception("Failed to fetch sample planning applications for anonymous search")
                records = None
                break
            if result:
                records = result
                break
            records = []  # keep the most recent (widest) empty attempt's scope for messaging

        return render_template(
            "results.html",
            postcode=postcode,
            radius_km=radius_km,
            keywords=keywords,
            sample_radius=sample_radius,
            sample_days=sample_days,
            matched_keywords=matched_keywords,
            records=records,
            ref=ref,
        )
    return render_template("signup.html", trade_presets=TRADE_PRESETS, ref=ref)


@app.route("/subscribe", methods=["POST"])
def subscribe():
    """Step 2 of signup: after seeing real results, capture name/email and
    save the search they already saw as their ongoing daily alert."""
    if _rate_limited("subscribe", max_requests=10, window_minutes=60):
        return render_template("rate_limited.html"), 429

    data = {
        "name": request.form["name"].strip(),
        "email": request.form["email"].strip().lower(),
        "postcode": request.form["postcode"].strip().upper(),
        "radius_km": float(request.form.get("radius_km") or 3.0),
        "keywords": request.form.get("keywords") or "extension",
        "referred_by": request.form.get("ref", "").strip() or None,
    }
    subscriber_id = db.add_subscriber(data)
    return redirect(url_for("preview", subscriber_id=subscriber_id))


@app.route("/preview/<int:subscriber_id>")
def preview(subscriber_id):
    subscriber = db.get_subscriber(subscriber_id)
    if not subscriber:
        return "Not found", 404
    try:
        subject, body, new_records = build_digest_for_subscriber(subscriber, preview=True)
    except requests_lib.exceptions.RequestException:
        # PlanIt is unreachable, slow, or returned an error — don't show a
        # bare 500 page to a real visitor. Log the full error for us to
        # see in Render's logs, and show a friendly holding message.
        logger.exception("Failed to fetch planning applications for preview")
        return render_template("preview_error.html", subscriber=subscriber), 502
    return render_template(
        "preview.html", subscriber=subscriber, subject=subject, body=body, records=new_records,
        trial_days=TRIAL_DAYS,
    )


@app.route("/unsubscribe/<token>")
def unsubscribe(token):
    ok = db.unsubscribe(token)
    return render_template("unsubscribed.html", ok=ok)


@app.route("/leads/<token>", methods=["GET", "POST"])
def leads(token):
    """A lightweight, login-free "leads so far" view for a subscriber —
    everything PatchAlert has ever sent them, with a simple status they can
    set (new / contacted / won / lost). Linked from the access-token-based
    URL in every digest email, so it's usable without building a full
    account/login system."""
    subscriber = db.get_subscriber_by_token(token)
    if not subscriber:
        return "Not found", 404

    if request.method == "POST":
        uid = request.form.get("application_uid")
        status = request.form.get("status", "new")
        note = request.form.get("note", "")
        if uid:
            db.set_lead_status(subscriber["id"], uid, status, note)
        return redirect(url_for("leads", token=token))

    sent = db.get_sent_alerts(subscriber["id"])
    statuses = db.get_lead_statuses(subscriber["id"])
    status_counts = {"new": 0, "contacted": 0, "won": 0, "lost": 0}
    for r in sent:
        current_status = statuses.get(r["application_uid"], {}).get("status", "new")
        status_counts[current_status] = status_counts.get(current_status, 0) + 1
    return render_template(
        "leads.html", subscriber=subscriber, sent=sent, statuses=statuses, status_counts=status_counts,
    )

@app.route("/account/<token>")
def account(token):
    """One page for everything beyond the original signup search: extra
    patches (pro only) and phone/SMS alert settings (pro only). Basic-plan
    subscribers see what they'd get by upgrading rather than a dead end."""
    subscriber = db.get_subscriber_by_token(token)
    if not subscriber:
        return "Not found", 404
    patches = db.get_additional_patches(subscriber["id"]) if subscriber.get("plan") == "pro" else []

    # Only relevant once billing is actually enforced (see digest.py) — shows
    # a countdown/expired notice so a Basic subscriber knows they'll need to
    # pay to keep receiving alerts, and links straight to both plans so
    # there's an actual way to do that (not just an "upgrade to Pro" link
    # with no equivalent path to just paying for Basic).
    trial_days_left = None
    if ENFORCE_BILLING and subscriber.get("plan") != "pro" and subscriber.get("subscription_status") != "active":
        created = db.parse_timestamp(subscriber.get("created_at"))
        if created:
            trial_days_left = max(TRIAL_DAYS - (datetime.utcnow() - created).days, 0)

    return render_template(
        "account.html",
        subscriber=subscriber,
        patches=patches,
        max_patches=db.MAX_ADDITIONAL_PATCHES,
        trial_days_left=trial_days_left,
    )


@app.route("/account/<token>/patches", methods=["POST"])
def add_patch(token):
    subscriber = db.get_subscriber_by_token(token)
    if not subscriber:
        return "Not found", 404
    if subscriber.get("plan") == "pro":
        postcode = request.form.get("postcode", "").strip().upper()
        radius_km = float(request.form.get("radius_km") or 3.0)
        keywords = request.form.get("keywords", "").strip() or subscriber["keywords"]
        if postcode:
            db.add_patch(subscriber["id"], postcode, radius_km, keywords)
    return redirect(url_for("account", token=token))


@app.route("/account/<token>/patches/<int:patch_id>/delete", methods=["POST"])
def delete_patch(token, patch_id):
    subscriber = db.get_subscriber_by_token(token)
    if not subscriber:
        return "Not found", 404
    db.delete_patch(patch_id, subscriber["id"])
    return redirect(url_for("account", token=token))


@app.route("/account/<token>/notifications", methods=["POST"])
def update_notifications(token):
    subscriber = db.get_subscriber_by_token(token)
    if not subscriber:
        return "Not found", 404
    if subscriber.get("plan") == "pro":
        phone = request.form.get("phone", "").strip()
        sms_opt_in = request.form.get("sms_opt_in") == "on"
        db.update_notification_settings(subscriber["id"], phone, sms_opt_in)
    return redirect(url_for("account", token=token))


@app.route("/upgrade/<int:subscriber_id>")
def upgrade(subscriber_id):
    subscriber = db.get_subscriber(subscriber_id)
    if not subscriber:
        return "Not found", 404
    plan = request.args.get("plan", "basic")
    if plan not in ("basic", "pro"):
        plan = "basic"
    checkout_url = create_checkout_session(
        subscriber["email"],
        success_url=url_for("preview", subscriber_id=subscriber_id, _external=True),
        cancel_url=url_for("preview", subscriber_id=subscriber_id, _external=True),
        plan=plan,
    )
    return redirect(checkout_url)


@app.route("/billing/not-configured")
def billing_not_configured():
    return render_template("billing_not_configured.html")


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """
    Without this, nothing ever actually flips a subscriber's plan/status
    in the database after they pay — /upgrade sends them to Stripe
    Checkout, but Checkout succeeding doesn't call back into this app on
    its own. This is that callback.

    Setup: in your Stripe dashboard, add a webhook endpoint pointing at
    https://your-app-url/stripe/webhook, subscribed to at least the
    "checkout.session.completed" event, then set STRIPE_WEBHOOK_SECRET to
    the signing secret Stripe gives you for that endpoint.

    Known gap: this only handles a successful checkout (upgrading someone
    to their paid plan). It does not yet handle a cancelled/expired
    subscription automatically downgrading them back to "basic" — that
    needs the subscriber's Stripe customer id stored at checkout time to
    reliably match a later cancellation event back to the right person,
    which isn't wired up yet.
    """
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "STRIPE_WEBHOOK_SECRET not configured"}), 503

    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception:
        logger.exception("Stripe webhook signature verification failed")
        return jsonify({"error": "invalid signature"}), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        email = session.get("customer_email") or (session.get("customer_details") or {}).get("email")
        plan = (session.get("metadata") or {}).get("plan", "basic")
        if email:
            db.set_plan_by_email(email.strip().lower(), plan, status="active")
        else:
            logger.warning("Stripe checkout.session.completed with no customer email — could not update plan")

    return jsonify({"ok": True})


@app.route("/run-digest", methods=["GET", "POST"])
def run_digest_endpoint():
    """
    Triggers the daily digest run over HTTP, so a free external scheduler
    (e.g. cron-job.org, or a GitHub Actions scheduled workflow doing
    `curl`) can fire it once a day without needing a paid hosting tier
    with built-in cron. Protected by a shared-secret token so randoms on
    the internet can't trigger it or see subscriber data.

    Set DIGEST_TRIGGER_SECRET as an environment variable on your host,
    then point your scheduler at:
        https://your-app-url/run-digest?token=YOUR_SECRET
    """
    if not DIGEST_TRIGGER_SECRET:
        return jsonify({"error": "DIGEST_TRIGGER_SECRET not configured on this deployment"}), 503
    token = request.args.get("token") or request.headers.get("X-Digest-Token")
    if token != DIGEST_TRIGGER_SECRET:
        return jsonify({"error": "unauthorized"}), 403
    summary = run_daily_digest()
    return jsonify({"ok": True, "results": summary})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=True)
