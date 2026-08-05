"""
Stripe Checkout for the PatchAlert subscription.

Two plans are supported:
  - "basic": the standard daily alert.
  - "pro": wider radius cap and newly-approved applications flagged and
    surfaced first — see digest.py for what actually differs.

Set these environment variables when you're ready to charge:
  STRIPE_SECRET_KEY        - from your Stripe dashboard
  STRIPE_PRICE_ID_BASIC    - the recurring Price ID for the basic plan
  STRIPE_PRICE_ID_PRO      - the recurring Price ID for the pro plan
(STRIPE_PRICE_ID, singular, still works as a fallback for the basic plan
only, so this stays compatible if you'd already set that one up.)

Until STRIPE_SECRET_KEY is set, /upgrade routes to a "not configured yet"
page instead of erroring — so this is always safe to leave switched off
during early access.
"""

import os

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PRICE_IDS = {
    "basic": os.environ.get("STRIPE_PRICE_ID_BASIC") or os.environ.get("STRIPE_PRICE_ID"),
    "pro": os.environ.get("STRIPE_PRICE_ID_PRO"),
}
IS_CONFIGURED = bool(STRIPE_SECRET_KEY and (STRIPE_PRICE_IDS["basic"] or STRIPE_PRICE_IDS["pro"]))

if STRIPE_SECRET_KEY:
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    # The pinned `stripe` package defaults to an older Stripe API version
    # that predates "Managed Payments" (a newer, account-level Stripe
    # feature) — without pinning this explicitly, every checkout session
    # creation fails with "Managed Payments is not supported on API
    # version ...". See: https://docs.stripe.com/managed-payments
    stripe.api_version = "2025-03-31.basil"


def create_checkout_session(subscriber_email: str, success_url: str, cancel_url: str, plan: str = "basic") -> str:
    """
    Returns a URL to redirect the subscriber to: either a real Stripe
    Checkout session, or a friendly "not configured yet" page if Stripe
    hasn't been set up on this deployment.
    """
    price_id = STRIPE_PRICE_IDS.get(plan) or STRIPE_PRICE_IDS.get("basic")
    if not STRIPE_SECRET_KEY or not price_id:
        return "/billing/not-configured"

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=subscriber_email,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url + ("&" if "?" in success_url else "?") + "checkout=success",
        cancel_url=cancel_url + ("&" if "?" in cancel_url else "?") + "checkout=cancelled",
        metadata={"plan": plan},
    )
    return session.url
