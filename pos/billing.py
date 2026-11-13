"""Charging organizers per event with Stripe Checkout.

Prices are set in the Stripe dashboard and referenced here by id, so changing
a price needs no code change.
"""

import json
import os

import stripe

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
# Tags our sessions in the Stripe Dashboard
INTEGRATION_IDENTIFIER = "splitthesale-event-checkout-qhvmtrwz"
PRICES = {
    "yard_sale": os.environ.get("STRIPE_PRICE_YARD_SALE", ""),
    "charity": os.environ.get("STRIPE_PRICE_CHARITY", ""),
}


class NotConfigured(Exception):
    pass


def create_checkout(event, email):
    """Start a Stripe Checkout for an event. Returns (session id, payment page URL)."""
    # Imported here so the app module finishes loading before this runs
    from app import PUBLIC_URL

    if not STRIPE_SECRET_KEY or not PRICES[event["mode"]]:
        raise NotConfigured
    client = stripe.StripeClient(STRIPE_SECRET_KEY)
    checkout = client.v1.checkout.sessions.create(
        params={
            "mode": "payment",
            "line_items": [{"price": PRICES[event["mode"]], "quantity": 1}],
            "customer_email": email,
            "client_reference_id": str(event["id"]),
            "metadata": {"event_id": str(event["id"])},
            "success_url": f"{PUBLIC_URL}/app?paid={event['id']}",
            "cancel_url": f"{PUBLIC_URL}/app",
            "integration_identifier": INTEGRATION_IDENTIFIER,
        },
    )
    return checkout.id, checkout.url


def paid_event_id(payload, signature):
    """The event id from a Stripe webhook if it says a checkout was paid, else None.

    Raises ValueError if the webhook didn't come from Stripe.
    """
    # Without a secret anyone could sign a webhook, so accept none at all
    if not STRIPE_WEBHOOK_SECRET:
        raise ValueError("STRIPE_WEBHOOK_SECRET is not set")
    try:
        stripe.WebhookSignature.verify_header(
            payload.decode() if isinstance(payload, bytes) else payload, signature, STRIPE_WEBHOOK_SECRET, 300
        )
        # Now that it's known to be from Stripe, read it as plain JSON
        notice = json.loads(payload)
    except (stripe.SignatureVerificationError, ValueError, UnicodeDecodeError):
        raise ValueError("Invalid Stripe signature") from None
    if notice["type"] not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return None
    session = notice["data"]["object"]
    # A delayed payment method reports "unpaid" on completion and is handled when it succeeds
    if session.get("payment_status") not in ("paid", "no_payment_required"):
        return None
    event_id = (session.get("metadata") or {}).get("event_id", "")
    return int(event_id) if event_id.isdigit() else None
