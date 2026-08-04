# PatchAlert — Local Planning Application Alerts (MVP)

A working prototype: tradespeople sign up with a postcode, radius, and
trade type (extensions, loft conversions, etc.), and get a plain-English
email digest of new matching planning applications near them, sourced from
the free UK PlanIt API (planit.org.uk).

## Run it locally

```bash
pip install -r requirements.txt
python3 app.py
```

Then open http://localhost:5002 — try the signup flow, it shows you a live
preview of the digest email using bundled sample data.

## Run the daily digest job

```bash
python3 digest.py
```

This simulates what a scheduled job would do every morning: fetch matches
for every subscriber, "send" the email (written to `outbox/` in this
prototype instead of a real send), and remember what's already been sent
so nobody gets the same application twice.

## What's real vs. what's a stub

- **Real and tested:** signup, keyword/area matching logic, digest
  generation, de-duplication (`db.py`'s `sent_alerts` table).
- **Currently running on sample data:** `planit_client.py` defaults to a
  bundled fixture (`fixtures/sample_planit_response.json`) because this
  build environment has no outbound internet access to test the live
  PlanIt API. The real HTTP request code is written and ready — see the
  docstring in `planit_client.py` for exactly what to change (set
  `PLANIT_USE_FIXTURE=0`) once this is deployed somewhere with normal
  internet access. **This must be verified against a handful of real
  postcodes before launch** — confirm the API is still free/unauthenticated
  and that the field names in a live response match what `digest.py`
  expects.
- **Stubbed:** payments (`billing.py` — deliberately not linked from the
  UI right now; see the launch strategy below) and the actual scheduling
  of the daily job (see `render.yaml` for a ready-to-use cron config, but
  it needs to actually be deployed and turned on).
- **Ready but untested against real email:** `digest.py`'s `send_email()`
  now calls the Resend API (https://resend.com) if `RESEND_API_KEY` is
  set, and otherwise falls back to writing to `outbox/`. Not yet tested
  against a real Resend account since this build environment has no
  outbound internet access.
- **Production deployment files included:** `Procfile` and `render.yaml`
  are ready for Render (or adapt `Procfile` for Railway/Fly.io — the
  gunicorn command is the same everywhere).

## Launch strategy

Ship this as a free early-access beta first (see `MARKETING_COPY.md`),
rather than waiting until Stripe is wired up. The `/upgrade` route and
`billing.py` are built and ready for whenever you decide to turn pricing
on — they're just not linked from the UI yet, since asking people to pay
before you have any real usage or feedback is usually the wrong order.

## Next steps to take this live

See `DEPLOY_GUIDE.md` for click-by-click deployment steps (no command
line needed), `GO_LIVE_CHECKLIST.md` for the fuller picture including
business setup, and `MARKETING_COPY.md` for launch post drafts.
