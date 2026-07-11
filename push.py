"""
Web Push notifications for the scanner
======================================
Lets the phone app receive alerts even when it's closed. Standard Web Push:

  1. Backend holds a VAPID keypair (one-time generated, stored as env vars).
  2. The phone subscribes via the browser's push service and sends its
     subscription to POST /api/push/subscribe (see api.py).
  3. When scanner.run_once() finds a NEW signal above the alert threshold,
     dispatch_alert() calls send_push(), which pushes to every stored
     subscription. Dead subscriptions (410/404) are pruned automatically.

Everything here degrades gracefully: if the VAPID env vars aren't set or the
optional `pywebpush` dependency is missing, send_push() is a quiet no-op, so the
scanner and dashboard keep working exactly as before.

One-time setup — generate a keypair and paste the two lines it prints into your
environment / .env file:

    python push.py --generate-keys

Required env vars once you have them:
    VAPID_PUBLIC_KEY   base64url public key (also handed to the browser)
    VAPID_PRIVATE_KEY  base64url raw private key (keep secret)
    VAPID_CLAIM_EMAIL  contact email the push service can reach (e.g. you@ex.com)
"""

import os
import json
import base64
import sqlite3
from datetime import datetime, timezone

def _db_path(path: str = None) -> str:
    """Resolve the SQLite path at call time (never at import time) so there's no
    circular-import ordering issue with scanner.py, which imports this module."""
    if path:
        return path
    try:
        from scanner import DB_PATH
        return DB_PATH
    except Exception:
        return os.getenv("SCANNER_DB", "scanner.db")

VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_CLAIM_EMAIL = os.getenv("VAPID_CLAIM_EMAIL", "mailto:contact@example.com")

if VAPID_CLAIM_EMAIL and not VAPID_CLAIM_EMAIL.startswith("mailto:"):
    VAPID_CLAIM_EMAIL = "mailto:" + VAPID_CLAIM_EMAIL


# ------------------------------------------------------------------------
# Subscription storage (its own table in the same scanner.db)
# ------------------------------------------------------------------------

def init_push_db(path: str = None):
    conn = sqlite3.connect(_db_path(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            endpoint TEXT PRIMARY KEY,
            subscription TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_subscription(subscription: dict, path: str = None):
    endpoint = subscription.get("endpoint")
    if not endpoint:
        return False
    init_push_db(path)
    conn = sqlite3.connect(_db_path(path))
    conn.execute(
        "INSERT OR REPLACE INTO push_subscriptions (endpoint, subscription, created_at) VALUES (?,?,?)",
        (endpoint, json.dumps(subscription), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return True


def delete_subscription(endpoint: str, path: str = None):
    if not endpoint:
        return
    conn = sqlite3.connect(_db_path(path))
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
    conn.commit()
    conn.close()


def _all_subscriptions(path: str = None):
    init_push_db(path)
    conn = sqlite3.connect(_db_path(path))
    try:
        rows = conn.execute("SELECT subscription FROM push_subscriptions").fetchall()
    finally:
        conn.close()
    out = []
    for (raw,) in rows:
        try:
            out.append(json.loads(raw))
        except Exception:
            pass
    return out


def subscription_count(path: str = None) -> int:
    init_push_db(path)
    conn = sqlite3.connect(_db_path(path))
    try:
        n = conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0]
    finally:
        conn.close()
    return n


# ------------------------------------------------------------------------
# Config helpers
# ------------------------------------------------------------------------

def is_configured() -> bool:
    """True only if both VAPID keys are present and pywebpush is importable."""
    if not (VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY):
        return False
    try:
        import pywebpush  # noqa: F401
        return True
    except Exception:
        return False


def public_key() -> str:
    return VAPID_PUBLIC_KEY


# ------------------------------------------------------------------------
# Sending
# ------------------------------------------------------------------------

def send_push(title: str, body: str = "", url: str = "/", tag: str = "scanner-signal",
              path: str = None):
    """
    Push a notification to every stored subscription. No-op (with an info line)
    if push isn't configured. Never raises — a push failure must never crash a scan.
    """
    if not is_configured():
        return

    try:
        from pywebpush import webpush, WebPushException
    except Exception as e:
        print(f"[WARN] push: pywebpush unavailable ({e})")
        return

    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    claims = {"sub": VAPID_CLAIM_EMAIL}

    sent = 0
    for sub in _all_subscriptions(path):
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=dict(claims),  # webpush mutates this dict, so copy per call
            )
            sent += 1
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):  # subscription is gone — prune it
                delete_subscription(sub.get("endpoint"), path)
                print(f"[INFO] push: pruned expired subscription ({status})")
            else:
                print(f"[WARN] push send failed: {e}")
        except Exception as e:
            print(f"[WARN] push send error: {e}")
    if sent:
        print(f"[INFO] push: delivered to {sent} device(s)")


# ------------------------------------------------------------------------
# Key generation CLI
# ------------------------------------------------------------------------

def generate_keys():
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    priv = ec.generate_private_key(ec.SECP256R1())
    raw_priv = priv.private_numbers().private_value.to_bytes(32, "big")
    raw_pub = priv.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)

    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    print("# Add these to your environment or .env file (keep the private key secret):")
    print(f'export VAPID_PUBLIC_KEY="{b64(raw_pub)}"')
    print(f'export VAPID_PRIVATE_KEY="{b64(raw_priv)}"')
    print('export VAPID_CLAIM_EMAIL="you@example.com"   # a real contact address')


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Web Push helper for the scanner")
    p.add_argument("--generate-keys", action="store_true", help="Print a fresh VAPID keypair")
    args = p.parse_args()
    if args.generate_keys:
        generate_keys()
    else:
        print(f"push configured: {is_configured()} · subscriptions: {subscription_count()}")
