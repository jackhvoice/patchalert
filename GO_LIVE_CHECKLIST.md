# Go-Live Checklist — PatchAlert

These are the steps that need you specifically. Everything else (the
code, new features, monitoring) I can keep doing for you.

## 1. Verify the data source before launch
- Re-check https://www.planit.org.uk/api/ for current terms — confirm it's
  still free/unauthenticated and check for any rate limits or attribution
  requirements. The site asks for optional donations to keep it running;
  worth considering once the product is making money, both as goodwill
  and to keep the data source healthy.
- Once you (or I, if given network access on a real host) can reach the
  live API, run a handful of real postcodes through `planit_client.py`
  with `PLANIT_USE_FIXTURE=0` and compare the response fields against
  what `digest.py` expects — the field names were taken from PlanIt's
  documentation but weren't tested against a live response in this build
  environment (no outbound internet access here).

## 2. Business setup
- Same as the compliance-pack checklist: register as sole trader or
  limited company, open a business bank account, and put basic terms of
  service in place (this product is lower legal-risk than the compliance
  one, but a landlord/homeowner objecting to being "found" via a public
  planning application is a foreseeable complaint worth having a clear,
  honest answer to in your terms — the data is entirely public record).

## 3. Domain and hosting — mostly ready
- `Procfile` and `render.yaml` are already written for a Render deploy
  (gunicorn, 2 workers, plus a cron entry for the daily digest). Buy a
  domain, create a Render account, and point it at wherever you push this
  code (Render deploys from a git repo — GitHub is the easiest pairing).
- If you'd rather use Railway or Fly.io instead of Render, the `Procfile`
  works as-is; you'd just skip `render.yaml` and configure the cron job
  in that host's own scheduler UI instead.

## 4. Payments — built, deliberately not switched on yet
- `billing.py` and the `/upgrade/<id>` route are ready. They're
  intentionally not linked from the UI right now — see "Launch strategy"
  in `README.md` for why (free early access first, pricing once there's
  real usage to justify it).
- When you're ready to turn pricing on: create the Stripe account, create
  a Product + recurring Price, set `STRIPE_SECRET_KEY` and
  `STRIPE_PRICE_ID`, and tell me — I'll finish `billing.py` and add the
  "Subscribe" button back into `preview.html`.

## 5. Real email sending — code ready, needs a real account
- `send_email()` in `digest.py` already calls the Resend API
  (https://resend.com) when `RESEND_API_KEY` is set. Sign up for a Resend
  account, verify your sending domain (SPF/DKIM — Resend's dashboard
  walks you through this), and set `RESEND_API_KEY` and
  `RESEND_FROM_ADDRESS` as environment variables on your host.
- This hasn't been tested against a real Resend account yet — worth
  sending yourself a test digest first before telling real subscribers
  it's live.

## 6. Scheduling the daily job — free, no paid tier needed
- The app now exposes `/run-digest?token=YOUR_SECRET`, which runs the same
  job as `digest.py` over a simple HTTP call. Set `DIGEST_TRIGGER_SECRET`
  to a random string (anything you make up) as an environment variable on
  Render.
- Easiest option: a GitHub Actions scheduled workflow is already included
  at `.github/workflows/daily-digest.yml` — it just needs two repository
  secrets added in GitHub (Settings → Secrets and variables → Actions):
  `APP_URL` (your deployed app's URL) and `DIGEST_TRIGGER_SECRET` (same
  value as on Render). Runs automatically once a day, no extra account
  needed since you already have GitHub for the code.
- Alternative: a free service like cron-job.org can call the same URL on
  a schedule if you'd rather not touch GitHub Actions.
- I can also set this up as a Cowork scheduled task that pings the
  deployed app on a cadence, once it's live somewhere I can reach.

## 7. Getting first customers
- Trade Facebook groups (search "[your region] builders/tradesmen") and
  local trade directories are the most direct channel — this audience
  doesn't typically go looking for "construction sales software," so
  organic community posts framed as "I built this for myself, thought
  others might find it useful" tend to land better than an ad.
- Consider a genuinely free tier (e.g. weekly digest instead of daily) to
  lower the barrier for the first cohort of users and gather feedback
  before asking anyone to pay.

## What I can keep doing for you
- Bug fixes, new features, and UI changes.
- Writing the production deployment config once you've picked a host.
- Wiring up real email sending once you have a provider.
- Setting up and monitoring the scheduled daily digest job.
- Verifying the live PlanIt API response and adjusting `planit_client.py`
  if field names differ from what's assumed here.
