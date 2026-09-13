#!/usr/bin/env python3
"""Google sign-in and Razorpay billing.

Both are plain HTTPS against documented endpoints, so there is no SDK here - the whole
surface is four requests. Everything is optional: with no Google credentials the app
runs BYOK-only, and with no Razorpay credentials the upgrade button simply isn't shown.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import urllib.parse
import urllib.request

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
RAZORPAY_API = "https://api.razorpay.com/v1"

PLAN_AMOUNT = int(os.environ.get("PLAN_AMOUNT_PAISE", 29900))   # ₹299
PLAN_CURRENCY = os.environ.get("PLAN_CURRENCY", "INR")

google_enabled = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
razorpay_enabled = bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)


def _post(url, data, headers=None, auth=None, form=True):
    body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
    h = dict(headers or {})
    if not form:
        h["Content-Type"] = "application/json"
    if auth:
        h["Authorization"] = "Basic " + base64.b64encode(":".join(auth).encode()).decode()
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- Google

def login_url(redirect_uri, state):
    return GOOGLE_AUTH + "?" + urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    })


def exchange(code, redirect_uri):
    """Auth code -> the user's identity. The id_token comes straight from Google's
    token endpoint over TLS, so its claims are trustworthy without re-verifying the
    signature ourselves; we never accept an id_token from the browser."""
    tok = _post(GOOGLE_TOKEN, {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })
    claims = json.loads(base64.urlsafe_b64decode(
        tok["id_token"].split(".")[1] + "=" * (-len(tok["id_token"].split(".")[1]) % 4)))
    if not claims.get("email_verified"):
        raise ValueError("Google account has no verified email address.")
    return {"sub": claims["sub"], "email": claims["email"].lower(),
            "name": claims.get("name") or claims["email"].split("@")[0]}


def new_state():
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------- Razorpay

def payment_link(uid, email, name, callback_url):
    """A hosted checkout page. Payment Links need no client-side SDK, so the whole
    flow is one redirect out and a webhook back."""
    r = _post(f"{RAZORPAY_API}/payment_links", {
        "amount": PLAN_AMOUNT,
        "currency": PLAN_CURRENCY,
        "accept_partial": False,
        "description": f"Resume Tailor - {os.environ.get('PLAN_DAYS', '30')} days unlimited",
        "customer": {"email": email, "name": name},
        "notify": {"email": False, "sms": False},
        "reminder_enable": False,
        "notes": {"uid": uid},
        "callback_url": callback_url,
        "callback_method": "get",
    }, auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), form=False)
    return r["short_url"]


def verify_webhook(body: bytes, signature: str) -> bool:
    """Razorpay signs the raw body with the webhook secret (HMAC-SHA256, hex).

    Constant-time compare, and the RAW bytes must be used - re-serialising the JSON
    changes the digest and every event would be rejected.
    """
    if not (RAZORPAY_WEBHOOK_SECRET and signature):
        return False
    expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def payment_from_event(event: dict):
    """(payment_id, uid, amount, currency) from a paid event, or None if not one."""
    kind = event.get("event", "")
    payload = event.get("payload", {})
    if kind == "payment_link.paid":
        link = payload.get("payment_link", {}).get("entity", {})
        pay = payload.get("payment", {}).get("entity", {})
        uid = (link.get("notes") or {}).get("uid")
        pid = pay.get("id") or link.get("id")
    elif kind == "payment.captured":
        pay = payload.get("payment", {}).get("entity", {})
        uid = (pay.get("notes") or {}).get("uid")
        pid = pay.get("id")
    else:
        return None
    if not (pid and uid):
        return None
    return pid, uid, pay.get("amount", 0), pay.get("currency", PLAN_CURRENCY)


if __name__ == "__main__":
    print("google:  ", "configured" if google_enabled else "not configured (BYOK only)")
    print("razorpay:", "configured" if razorpay_enabled else "not configured (no upgrade button)")
    print("plan:    ", f"{PLAN_AMOUNT/100:.0f} {PLAN_CURRENCY} / {os.environ.get('PLAN_DAYS','30')} days")
