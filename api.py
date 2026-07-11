"""
Mobile API for the Insider / Politician Buy Scanner
===================================================
A thin FastAPI layer *in front of* the existing scanner.py engine. It:

  * reads the SAME scanner.db that scanner.py / dashboard.py write to,
  * serves stored signals to the phone app as JSON,
  * exposes POST /api/scan which calls scanner.run_once() in the background,
  * serves the installable PWA (the webapp/ folder) so one URL does everything.

It does NOT re-implement any scanning or scoring logic — that all still lives in
scanner.py. This file only *reads* the DB and *triggers* an existing scan pass,
so the "scanner.py = polling engine, dashboard.py = UI" architecture is preserved;
the phone app is just a second UI onto the same database.

Run (on your home device, then later on a cloud box):
    pip install -r requirements.txt
    uvicorn api:app --host 0.0.0.0 --port 8000

Then on your phone (same Wi-Fi) open:  http://<this-machine-LAN-ip>:8000
and use the browser's "Add to Home Screen" to install it as an app.

DATA REALITY CHECK is preserved end to end: congressional rows reflect when a
trade became public (STOCK Act allows up to 45 days), not when it happened. The
app UI states this explicitly; do not change that copy to imply real-time.
"""

import os
import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import scanner  # existing engine
import push     # web push channel (subscriptions live in the same scanner.db)

from fastapi import Request

WEBAPP_DIR = Path(__file__).parent / "webapp"

app = FastAPI(title="Insider / Politician Buy Scanner API", version="1.0")

# The phone loads the app from the same origin, so CORS is not strictly needed
# for that. It IS needed if you later host the frontend separately (e.g. on a CDN)
# and point it at a cloud API box — so allow it, scoped by env var when you tighten.
_allowed = os.getenv("SCANNER_CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _allowed],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Make sure the tables exist before the first request (idempotent).
scanner.init_db()
push.init_push_db()

# --- lightweight scan state so the phone can show a live "scanning…" indicator ---
_scan_lock = threading.Lock()
_scan_state = {
    "running": False,
    "last_run": None,     # ISO timestamp of last completed run
    "last_error": None,   # str if the last run raised
    "started_at": None,   # ISO timestamp of the in-flight run
}


# ------------------------------------------------------------------------
# Signal reads (query the same SQLite DB scanner.py writes)
# ------------------------------------------------------------------------

# App-facing source buckets -> how they're stored in the DB by scanner.py
_SOURCE_SQL = {
    "insider": ("source = ?", ["SEC Form 4"]),
    "congress": ("source LIKE ?", ["Congress%"]),
}


def _query_signals(min_score: int, source: str, ticker: str, limit: int):
    clauses = ["score >= ?"]
    params: list = [min_score]

    bucket = _SOURCE_SQL.get((source or "all").lower())
    if bucket:
        clauses.append(bucket[0])
        params.extend(bucket[1])

    if ticker:
        clauses.append("ticker LIKE ?")
        params.append(f"%{ticker.upper()}%")

    sql = (
        "SELECT id, source, ticker, person, role, action, value, trade_date, "
        "filed_date, score, reasons, url, seen_at FROM signals "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY score DESC, seen_at DESC LIMIT ?"
    )
    params.append(limit)

    conn = sqlite3.connect(scanner.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _run_scan_background():
    with _scan_lock:
        if _scan_state["running"]:
            return
        _scan_state["running"] = True
        _scan_state["started_at"] = datetime.now(timezone.utc).isoformat()
        _scan_state["last_error"] = None

    def _worker():
        try:
            scanner.run_once()
        except Exception as e:  # never let a scan crash the API process
            with _scan_lock:
                _scan_state["last_error"] = str(e)
            print(f"[WARN] background scan failed: {e}")
        finally:
            with _scan_lock:
                _scan_state["running"] = False
                _scan_state["started_at"] = None
                _scan_state["last_run"] = datetime.now(timezone.utc).isoformat()

    threading.Thread(target=_worker, daemon=True).start()


# ------------------------------------------------------------------------
# API routes
# ------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "min_alert_score": scanner.MIN_ALERT_SCORE,
        "interval_seconds": scanner.INTERVAL_SECONDS,
        "db_path": str(scanner.DB_PATH),
        "scan": dict(_scan_state),
        "note": "Congressional rows reflect when a trade became public (up to 45 "
                "days after the trade under the STOCK Act), not when it happened.",
    }


@app.get("/api/signals")
def signals(
    min_score: int = Query(0, ge=0, le=100),
    source: str = Query("all"),
    ticker: str = Query(""),
    limit: int = Query(300, ge=1, le=2000),
):
    rows = _query_signals(min_score, source, ticker.strip(), limit)
    return {"count": len(rows), "min_score": min_score, "signals": rows}


@app.post("/api/scan")
def scan():
    """Kick off scanner.run_once() in the background and return immediately."""
    _run_scan_background()
    with _scan_lock:
        return {"started": True, "scan": dict(_scan_state)}


@app.get("/api/scan/status")
def scan_status():
    with _scan_lock:
        return dict(_scan_state)


# ------------------------------------------------------------------------
# Web Push: the phone subscribes here so it can be alerted while closed
# ------------------------------------------------------------------------

@app.get("/api/push/key")
def push_key():
    """Hand the browser the VAPID public key it needs to subscribe."""
    return {"configured": push.is_configured(), "public_key": push.public_key()}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    sub = await request.json()
    ok = push.save_subscription(sub)
    return {"ok": ok, "subscriptions": push.subscription_count()}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request):
    body = await request.json()
    push.delete_subscription(body.get("endpoint", ""))
    return {"ok": True, "subscriptions": push.subscription_count()}


# ------------------------------------------------------------------------
# Serve the installable PWA (must be mounted AFTER the API routes above)
# ------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(WEBAPP_DIR / "index.html")


# Static assets (JS/CSS/manifest/icons/service worker) served from webapp/
app.mount("/", StaticFiles(directory=str(WEBAPP_DIR), html=True), name="webapp")
