"""
Stripe Checkout scaffolding for the PatchAlert subscription (£9-15/month).
Same activation steps as compliance-packs/billing.py — see that file's
docstring, or GO_LIVE_CHECKLIST.md, for the full walkthrough. Kept as a
stub here for the same reason: no live Stripe keys or outbound network
access exist in this prototype build environment.
"""

import os

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID")
IS_CONFIGURED = bool(STRIPE_SECRET_KEY and STRIPE_PRICE_ID)


def create_checkout_session(subscriber_email: str, success_url: str, cancel_url: str) -> str:
    if not IS_CONFIGURED:
        return "/billing/not-configured"

    # import stripe
    # stripe.api_key = STRIPE_SECRET_KEY
    # session = stripe.checkout.Session.create(
    #     mode="subscription",
    #     customer_email=subscriber_email,
    #     line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
    #     success_url=success_url,
    #     cancel_url=cancel_url,
    # )
    # return session.url
    raise NotImplementedError("Install `stripe` and uncomment the real implementation above.")
