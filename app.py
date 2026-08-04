import os

from flask import Flask, render_template, request, redirect, url_for, jsonify

import db
from digest import build_digest_for_subscriber, run_daily_digest
from billing import create_checkout_session

app = Flask(__name__)
db.init_db()

DIGEST_TRIGGER_SECRET = os.environ.get("DIGEST_TRIGGER_SECRET")

TRADE_PRESETS = {
    "Extensions": "extension",
    "Loft conversions": "loft conversion",
    "Garage conversions": "garage conversion",
    "Conservatories": "conservatory",
    "Outbuildings/annexes": "outbuilding,annexe",
}


@app.route("/")
def home():
    return render_template("home.html", trade_presets=TRADE_PRESETS)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        selected = request.form.getlist("trades")
        custom = request.form.get("custom_keywords", "").strip()
        keyword_terms = []
        for label in selected:
            keyword_terms.append(TRADE_PRESETS[label])
        if custom:
            keyword_terms.append(custom)
        keywords = ",".join(keyword_terms) if keyword_terms else "extension"

        data = {
            "name": request.form["name"].strip(),
            "email": request.form["email"].strip().lower(),
            "postcode": request.form["postcode"].strip().upper(),
            "radius_km": float(request.form.get("radius_km") or 3.0),
            "keywords": keywords,
        }
        subscriber_id = db.add_subscriber(data)
        return redirect(url_for("preview", subscriber_id=subscriber_id))
    return render_template("signup.html", trade_presets=TRADE_PRESETS)


@app.route("/preview/<int:subscriber_id>")
def preview(subscriber_id):
    subscriber = db.get_subscriber(subscriber_id)
    if not subscriber:
        return "Not found", 404
    subject, body, new_records = build_digest_for_subscriber(subscriber)
    return render_template(
        "preview.html", subscriber=subscriber, subject=subject, body=body, records=new_records
    )


@app.route("/upgrade/<int:subscriber_id>")
def upgrade(subscriber_id):
    subscriber = db.get_subscriber(subscriber_id)
    if not subscriber:
        return "Not found", 404
    checkout_url = create_checkout_session(
        subscriber["email"],
        success_url=url_for("preview", subscriber_id=subscriber_id, _external=True),
        cancel_url=url_for("preview", subscriber_id=subscriber_id, _external=True),
    )
    return redirect(checkout_url)


@app.route("/billing/not-configured")
def billing_not_configured():
    return render_template("billing_not_configured.html")


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
