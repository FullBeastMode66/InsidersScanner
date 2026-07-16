---
name: scanner-insider-politician
description: >-
  Development and maintenance guide for the Insider / Politician Buy Scanner —
  a free scanner that tracks SEC Form 4 insider buys and congressional stock
  disclosures, scores each 0–100, dedupes in SQLite, and pushes alerts. USE THIS
  SKILL for ANY work in this repo: editing scanner.py / api.py / dashboard.py /
  push.py / the webapp PWA, adding a data source or alert channel, refining
  scoring, fixing bugs, updating env vars, or touching the README. It encodes the
  architecture, the Signal contract, scoring rules, the "not real-time" data
  reality, and a validation checklist. Reach for it before writing code here, not
  after — it prevents the mistakes that have actually bitten this project.
---

# Insider / Politician Buy Scanner — Development Guide

## Overview

Enforces consistent architecture, scoring logic, and conventions for the Insider /
Politician Buy Scanner project. It tracks two public-filing signals — SEC Form 4
insider buys and congressional trade disclosures — scores each 0–100 like a breakout
scanner scores a chart pattern, dedupes against SQLite, and fires alerts. No paid API
key is required for the baseline.

## When to use this skill

Use it for **any** change in this repository, including:

- Editing the polling engine (`scanner.py`), the FastAPI/PWA layer (`api.py`), the
  Streamlit UI (`dashboard.py`), Web Push (`push.py`), or the installable app
  (`webapp/`).
- **Expanding the scanner**: a new data source, a new alert channel, or a scoring
  refinement.
- **Swapping in a paid API** (SEC-API.io, Quiver, Polygon) behind an existing fetch
  function.
- **Bug fixes or performance work** anywhere in the fetch → dedupe → score → alert
  pipeline.
- **Docs/config**: adding or renaming an environment variable, changing a data
  source URL, or updating the README's "Scoring logic" section.

If you are about to touch this codebase, read the relevant section below first.

## Key principles

- **Separation of concerns**: `fetch → dedupe → score → alert`. Each stage is
  modular and independently replaceable. A source only produces `Signal` objects; it
  never writes to the DB or sends alerts itself.
- **Free-tier friendly**: no paid API key required to run. Paid upgrades are optional
  swap-ins behind the same fetch-function boundary, never a hard dependency.
- **Data reality (non-negotiable)**: insiders must file Form 4 within **2 business
  days** (near-real-time); members of Congress have up to **45 days** under the STOCK
  Act. This is a feed of *when a trade became public*, not of live trading. Never
  imply otherwise in code, docstrings, alerts, scoring reasons, or UI copy.
- **Signal dataclass invariant**: every source, baseline or paid, must produce
  `Signal(id, source, ticker, person, role, action, value, trade_date, filed_date,
  score, reasons, url)`. Dates that flow into scoring are ISO (`YYYY-MM-DD`) — run
  feed dates through `normalize_date()` at the fetch boundary.
- **Explainable scoring**: score functions always return `(score: int 0–100, reasons:
  str)`. The `reasons` string is human-readable and every point (and the recency
  multiplier) is itemized, so the dashboard and app can always show "why".
- **Fail soft**: a failing source logs `[WARN]` and is skipped; it must never crash
  the scan loop.
- **No secrets in git**: all keys/tokens/URLs come from environment variables,
  documented in `README.md` and `.env.example`.

## Architecture

```
   SEC Form 4 atom feed              House / Senate Stock Watcher JSON
   (fetch_sec_form4 +                (fetch_congress_trades, per chamber)
    resolve_form4_detail)                        |
            |                                     |
            v                                     v
        score_insider()                    score_politician()
            |                                     |
            +------------------+------------------+
                               v
                     Signal(...) objects   ── normalize_date() on feed dates
                               v
                    SQLite scanner.db  ── dedupe on Signal.id (INSERT OR IGNORE)
                               v
                 new & score >= SCANNER_MIN_SCORE
                               v
                       dispatch_alert()
              console · Pushover · Discord · Telegram · Web Push (push.py)
                               |
   dashboard.py (Streamlit) ───+─── api.py (FastAPI JSON + PWA)  ── read-only from DB
```

`api.py` and `dashboard.py` **only read** `scanner.db` and may trigger
`run_once()`; they never reimplement fetching or scoring.

## Extending the scanner

### Adding a new data source

Write one `fetch_*` function that returns `list[Signal]`. Mirror
`fetch_congress_trades()`:

```python
def fetch_myfeed(url: str) -> list[Signal]:
    """One-line what + a schema note (date format, quirks) verified against the feed."""
    resp = requests.get(url, timeout=60)          # SEC feeds: add headers={"User-Agent": SEC_USER_AGENT}
    resp.raise_for_status()
    signals = []
    for row in resp.json():
        # 1. filter to open-market BUYS only (skip sells/grants/options)
        # 2. drop non-stock assets: ticker in ("", "--", "-", "N/A")
        ticker = (row.get("ticker") or "").strip().upper()
        if not ticker or ticker in ("--", "-", "N/A"):
            continue
        trade_date = normalize_date(row.get("transaction_date") or "")   # -> ISO for scoring
        filed_date = normalize_date(row.get("disclosure_date") or "")
        score, reasons = score_politician(person, committees, amount, trade_date, filed_date)
        signals.append(Signal(
            id=f"MYFEED-{person}-{ticker}-{trade_date}-{amount}",   # see dedup rules below
            source="MyFeed", ticker=ticker, person=person, role=role, action="BUY",
            value=amount, trade_date=trade_date, filed_date=filed_date,
            score=score, reasons=reasons, url=row.get("source_url") or "",
        ))
    return signals
```

Then call it from `run_once()` inside its **own** `try/except` that logs `[WARN]` and
continues (never let it abort the other sources):

```python
try:
    all_signals.extend(fetch_myfeed(MYFEED_URL))
except Exception as e:
    print(f"[WARN] MyFeed fetch failed: {e}")
```

**Dedup key (`Signal.id`) rules** — the id is the primary key; a collision silently
drops the row (`INSERT OR IGNORE`), a too-unique id re-alerts forever. Follow the
established shape: a `SOURCE-` prefix plus the fields that make a filing unique.
Existing patterns:

- SEC: `f"SEC-{link}"` (the filing URL is already unique)
- Congress: `f"CONGRESS-{chamber}-{person}-{ticker}-{trade_date}-{amount}"`

Use stable fields (person, ticker, trade date, amount) — never a timestamp or a
row index, or the same filing re-alerts every scan.

### Refining scoring

Edit `score_insider()` or `score_politician()` in `scanner.py`. Rules:

- Keep the signature returning `(int, str)`; append a `"<reason> (+N)"` fragment to
  `reasons` for **every** component so the "why" stays complete. State `(+0)` cases
  too (e.g. `"Slow disclosure, 47d (+0)"`) — silence reads as a bug.
- `min(score, 100)` before returning; scores are clamped to 0–100.
- Recency is already applied via `_apply_recency()` / `recency_factor()` — a
  multiplier keyed on **trade age** (≤30d ×1.00, 31–90d ×0.85, 91–180d ×0.60, >180d
  ×0.35). Keep recency (trade age) distinct from the disclosure-speed bonus (trade →
  public lag); they must not double-count.
- Scoring must **never** use wall-clock `now` non-deterministically in a way tests
  can't pin — the score functions take an injectable `now=None`. Preserve that.
- Scores are **frozen at first insert** (dedup never re-scores). Recency is computed
  once; design new rules knowing a stored score won't update as a signal ages.

### Adding an alert channel

Follow the `send_pushover` / `send_discord` / `send_telegram` pattern in
`scanner.py`: a `send_*(message)` function that **no-ops when its env vars are
unset**, then one line in `dispatch_alert()`. Never make a channel mandatory; the
console channel always works with zero config. Wrap the network call so one channel's
failure can't stop the others.

```python
def send_myalert(message: str):
    if not MYALERT_TOKEN:          # unset -> silent no-op, never raise
        return
    requests.post(MYALERT_URL, json={"text": message}, timeout=10)
```

### Swapping in a paid API

Replace the body of `fetch_sec_form4()` / `fetch_congress_trades()` (or add a
parallel fetch and switch on an env flag). The **only** contract that matters: it
returns `list[Signal]` with the invariant fields populated and dates ISO-normalized.
Do not let a paid schema leak past the fetch boundary — scoring, dedup, storage, and
the UI must not know or care which provider produced a Signal. Keep the free feed as
the default so the project still runs with no key.

## Validation checklist (every change)

- [ ] `python -m py_compile scanner.py api.py dashboard.py push.py` passes on all
      changed files.
- [ ] Every external API call is wrapped in `try/except` with a `[WARN]` log line; a
      failing source cannot crash `run_once()`.
- [ ] SEC requests send `headers={"User-Agent": SEC_USER_AGENT}` (SEC policy).
- [ ] Score functions return `(int 0–100, str)` and itemize every component in
      `reasons`.
- [ ] New `Signal.id`s follow the `SOURCE-<stable fields>` dedup pattern (no
      timestamps / row indexes).
- [ ] Feed dates run through `normalize_date()` before scoring.
- [ ] `README.md` updated: new env vars listed, data sources noted, and any scoring
      change documented in the **Scoring logic** section. Add new env vars to
      `.env.example` too.
- [ ] No hardcoded secrets — keys, tokens, and source URLs are env vars.
- [ ] No text (code, docstring, alert, scoring reason, UI) claims politician trades
      are "real-time" or "live".
- [ ] `python -m pytest tests/` passes (see Testing below).

## Common pitfalls

- **Letting an API failure crash `run_once()`** — always the per-source `try/except`
  + `[WARN]`, never a bare fetch.
- **Broken dedup** — a non-stable `Signal.id` (timestamp, index) re-alerts the same
  filing every scan; a too-broad id silently swallows distinct trades.
- **Scoring without reasons** — a bare number the dashboard can't explain. Every
  point needs a `reasons` fragment.
- **Assuming politician trades are real-time** — they lag up to 45 days. This is the
  project's cardinal rule.
- **Frozen-score surprise** — dedup never re-scores, so recency/scoring changes only
  affect *newly seen* signals; the existing DB keeps old scores until re-inserted.
- **Secrets in code or commit messages** — env vars only; scrub before committing.
- **Date bugs** — feeds use `MM/DD/YYYY`; `datetime.fromisoformat()` throws on them.
  Always `normalize_date()` first. Watch tz-aware vs naive datetime subtraction.

## Testing strategy

**Automated** — `tests/test_scanner.py` (pytest) covers the pure logic with no live
network: `parse_dollar_high()` range parsing, `score_insider()` / `score_politician()`
math, `recency_factor()` tiers, and the dedup round-trip against a temp SQLite file.
When adding logic:

- Assert the `Signal` invariant and that scores stay within 0–100.
- Pin `now=date(...)` in any test that exercises scoring so recency is deterministic
  and tests don't rot against the wall clock.
- Assert on `reasons` substrings so the "why" stays covered.
- Mock feed payloads (a small `list[dict]`); do not hit the network in tests.

**Manual** — one-off pass then verify dedup and the UI:

```bash
python scanner.py                 # single scan pass; run twice — 2nd finds 0 new
streamlit run dashboard.py        # visual table, score bars, filters
python -m uvicorn api:app --host 127.0.0.1 --port 8000   # PWA + JSON API
```

## Environment & deploy notes

- Windows dev box: use `cmd` / the `.bat` / `.ps1` launchers in `tunnel/`, not the
  `.sh` scripts. Git branch is **master**, not `main`.
- Deployed on Render (Docker, persistent disk at `/data`, auto-deploy on push to
  `master`). Render ignores the local `.env` — set env vars in the Render dashboard.
  The Docker `CMD` must bind `0.0.0.0` on `${PORT}` or Render's port scan fails.
- Key env vars: `SCANNER_DB`, `SCANNER_INTERVAL`, `SCANNER_MIN_SCORE`,
  `SEC_USER_AGENT`, `HOUSE_TRADES_URL`, `SENATE_TRADES_URL`, `SCANNER_CORS_ORIGINS`,
  the alert-channel vars (`PUSHOVER_*`, `DISCORD_WEBHOOK_URL`, `TELEGRAM_*`), and Web
  Push (`VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_CLAIM_EMAIL`).

## Out of scope

Brokerage integration, automated trade execution, and anything resembling investment
advice. This tool surfaces public filings and scores them — it does not recommend
trades.

## References

- SEC Form 4 (statement of changes in beneficial ownership):
  https://www.sec.gov/about/forms/form4.pdf
- SEC EDGAR full-text & current filings: https://www.sec.gov/cgi-bin/browse-edgar
- SEC access/User-Agent policy: https://www.sec.gov/os/webmaster-faq#developers
- STOCK Act (45-day disclosure): https://www.congress.gov/bill/112th-congress/senate-bill/2038
- Paid upgrade swap-ins (optional): SEC-API.io (https://sec-api.io), Quiver
  Quantitative (https://www.quiverquant.com), Polygon.io (https://polygon.io)
