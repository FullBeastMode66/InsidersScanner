"""
Insider / Politician Buy Scanner
=================================
Continuously scans for:
  1. SEC Form 4 insider buys (free, official SEC "current filings" feed — no API key)
  2. Congressional stock purchases (free, House Stock Watcher + Senate Stock Watcher datasets)

Scores each hit like a breakout scanner scores a chart pattern (0-100 "signal strength"),
dedupes against a local SQLite DB, and fires alerts (console / Pushover / Discord / Telegram)
for anything above your threshold.

Run:
    python scanner.py                    # one pass
    python scanner.py --loop             # continuous, polls every INTERVAL_SECONDS
    python scanner.py --purge-stale-senate  # one-off: drop retired-mirror Senate rows
    streamlit run dashboard.py           # visual scanner table (separate file)

No paid API key is required to get this running. Optional keys (SEC-API.io, Quiver, Polygon)
can be dropped in later for lower latency / richer data — see README.md.
"""

import os
import re
import time
import sqlite3
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional

import requests

# Optional Web Push channel for the phone app (graceful if not configured).
try:
    import push
except Exception:
    push = None

# ------------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------------

DB_PATH = os.getenv("SCANNER_DB", "scanner.db")
INTERVAL_SECONDS = int(os.getenv("SCANNER_INTERVAL", "300"))  # 5 min default
MIN_ALERT_SCORE = int(os.getenv("SCANNER_MIN_SCORE", "50"))   # 0-100 scale

# SEC requires a descriptive User-Agent identifying you (their policy, not optional)
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "InsiderPoliticianScanner contact@example.com")

# Optional notification channels (leave unset to just print to console)
PUSHOVER_TOKEN = os.getenv("PUSHOVER_TOKEN")
PUSHOVER_USER = os.getenv("PUSHOVER_USER")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Free, no-key congressional trade datasets, each a GitHub repo whose Actions cron
# scrapes the official disclosures and commits fresh JSON (so we read a static raw
# file, not a rate-limited/bot-walled site). Both are overridable via env var; swap
# for Quiver/FMP/Finnhub later if you want faster refresh — see README.md.
#   * House  — House Stock Watcher mirror (flat list, disclosure_date + amount fields).
#   * Senate — a fork of the "legislative-alpha" tracker, whose daily Action scrapes
#     efdsearch.senate.gov (which 403s datacenter IPs like Render, but not GitHub's
#     runners) and commits data.json as {"trades": [...]}. Unlike the old dollar-only
#     Stock Watcher mirror (dead since 2020), this feed includes the disclosure date.
#     fetch_congress_trades() handles both the flat-list and {"trades": [...]} shapes.
# Point SENATE_TRADES_URL at your own fork so the pipeline is one you control.
HOUSE_TRADES_URL = os.getenv(
    "HOUSE_TRADES_URL",
    "https://raw.githubusercontent.com/TattooedHead/house-stock-watcher-data/master/data/all_transactions.json",
)
SENATE_TRADES_URL = os.getenv(
    "SENATE_TRADES_URL",
    "https://raw.githubusercontent.com/FullBeastMode66/legislative-alpha/HEAD/data.json",
)

# SEC's free "current filings" atom feed — no key, updated continuously through the trading day
SEC_CURRENT_FORM4_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=4&company=&dateb=&owner=include&count=100&output=atom"
)

# High-signal insider titles (weighted in scoring — mirrors how a breakout scanner
# weights volume confirmation)
TOP_TITLES = ("CEO", "CHIEF EXECUTIVE", "CFO", "CHIEF FINANCIAL", "PRESIDENT", "CHAIRMAN")

# Congressional committees that plausibly correlate with sector-relevant trades.
# Extend this mapping as you like.
COMMITTEE_SECTOR_HINTS = ("ARMED SERVICES", "FINANCIAL SERVICES", "ENERGY", "HEALTH", "BANKING")

# Recency decay. A signal's usefulness fades as the underlying trade ages: for a tool
# that pushes phone alerts, last week's buy matters more than a 2022 one. This is a
# multiplier on the final score (a stale signal is discounted in proportion to its
# strength, rather than merely missing a flat bonus), keyed on the *trade* date — how
# long ago it happened. That is deliberately distinct from disclosure speed (the
# trade -> public lag), which is scored separately. Note this measures the age of the
# trade, not how "live" it is: congressional disclosures lag the trade by up to 45
# days under the STOCK Act (see README) — this scoring never implies otherwise.
# Each tier is (max_age_days, multiplier); anything older than the last tier is "stale".
RECENCY_TIERS = ((30, 1.00), (90, 0.85), (180, 0.60))
RECENCY_STALE_FACTOR = 0.35  # applied to trades older than the last tier boundary

# Cross-chamber normalization. Politician scoring has three components with these
# maximum weights:
POL_MAX_DOLLAR = 35
POL_MAX_COMMITTEE = 15
POL_MAX_DISCLOSURE = 20
# CHAMBER_MAX_BASE records what each chamber's feed can structurally earn; a chamber
# whose feed omits a field is scaled up to the common ceiling so it isn't permanently
# capped below richer ones (see _normalize_chamber). Neither free feed carries a
# committee field, so that bonus is dead against both (kept only for a future richer
# source) and isn't counted here. As of the current feeds BOTH chambers carry
# transaction + disclosure dates — the House Stock Watcher mirror, and the Senate feed
# scraped from efdsearch.senate.gov (see SENATE_TRADES_URL) which, unlike the old
# dollar-only mirror, includes the disclosure date. So both ceilings are 55 and the
# scaling is currently a no-op; it stays as defensive machinery for a future
# field-poor source (e.g. a dollar-only feed), which would be scaled back up.
CHAMBER_MAX_BASE = {
    "House": POL_MAX_DOLLAR + POL_MAX_DISCLOSURE,    # 55
    "Senate": POL_MAX_DOLLAR + POL_MAX_DISCLOSURE,   # 55 (feed carries disclosure dates)
}
POL_TARGET_BASE = max(CHAMBER_MAX_BASE.values())    # 55 — common ceiling


# ------------------------------------------------------------------------
# DATA MODEL
# ------------------------------------------------------------------------

@dataclass
class Signal:
    id: str                # stable dedup key
    source: str             # "SEC Form 4" | "Congress (House)" | "Congress (Senate)"
    ticker: str
    person: str
    role: str                # insider title, or politician + party/chamber
    action: str               # e.g. "BUY"
    value: str                 # dollar amount / range as reported
    trade_date: str
    filed_date: str
    score: int                  # 0-100 signal strength
    reasons: str                  # human-readable score breakdown
    url: str = ""


# ------------------------------------------------------------------------
# STORAGE
# ------------------------------------------------------------------------

def init_db(path: str = DB_PATH):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id TEXT PRIMARY KEY,
            source TEXT,
            ticker TEXT,
            person TEXT,
            role TEXT,
            action TEXT,
            value TEXT,
            trade_date TEXT,
            filed_date TEXT,
            score INTEGER,
            reasons TEXT,
            url TEXT,
            seen_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def already_seen(signal_id: str, path: str = DB_PATH) -> bool:
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT 1 FROM signals WHERE id=?", (signal_id,)).fetchone()
    conn.close()
    return row is not None


def save_signal(sig: Signal, path: str = DB_PATH):
    conn = sqlite3.connect(path)
    conn.execute("""
        INSERT OR IGNORE INTO signals
        (id, source, ticker, person, role, action, value, trade_date, filed_date, score, reasons, url, seen_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        sig.id, sig.source, sig.ticker, sig.person, sig.role, sig.action, sig.value,
        sig.trade_date, sig.filed_date, sig.score, sig.reasons, sig.url,
        datetime.now(timezone.utc).isoformat()
    ))
    conn.commit()
    conn.close()


def purge_stale_senate(before: str, path: str = DB_PATH) -> int:
    """
    Delete Senate signals whose trade happened before `before` (ISO YYYY-MM-DD) and
    return how many were removed. These are leftovers from the retired dollar-only
    Senate mirror (data ended in 2020); the current feed carries only recent trades,
    so a purge is permanent — nothing re-adds them. Rows with no trade_date are left
    alone. Run once via `python scanner.py --purge-stale-senate`.
    """
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(
            "DELETE FROM signals WHERE source = 'Congress (Senate)' "
            "AND trade_date != '' AND trade_date < ?",
            (before,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ------------------------------------------------------------------------
# SCORING  (0-100, breakout-scanner style: several weighted "confirmations" stack up)
# ------------------------------------------------------------------------

def parse_dollar_high(value_text: str) -> float:
    """Congressional disclosures report ranges like '$100,001 - $250,000'. Take the high end."""
    vals = []
    for token in re.findall(r"[\d,]+", value_text or ""):
        digits = token.replace(",", "")
        if digits.isdigit():  # a stray "," token cleans to "" -> skip, don't float("")
            vals.append(float(digits))
    return max(vals) if vals else 0.0


def normalize_date(value: str) -> str:
    """
    Congressional feeds report dates as MM/DD/YYYY; Form 4 / atom use ISO. Return an
    ISO (YYYY-MM-DD) string so scoring can compare them, or "" if unparseable.
    Without this, fromisoformat() raised on every congressional row and the
    disclosure-speed bonus silently never applied.
    """
    v = (value or "").strip()
    if not v or v in ("--", "-"):
        return ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return ""


def _to_date(value: str):
    """normalize_date() + parse to a naive date object, or None if unparseable."""
    iso = normalize_date(value)
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).date()
    except ValueError:
        return None


def recency_factor(trade_date: str, filed_date: str = "", now=None) -> tuple[float, str]:
    """
    Multiplier in [RECENCY_STALE_FACTOR, 1.0] reflecting how long ago the trade
    happened, plus a human-readable reason. Keyed on trade_date; falls back to
    filed_date (disclosure) only when the trade date is missing. If neither parses,
    no decay is applied (x1.0) since age can't honestly be measured.

    `now` is injectable (a date or datetime) so tests stay deterministic instead of
    silently rotting as the wall clock advances. A trade dated in the future is bad
    data, not a fresh signal, so it is treated as unknown rather than rewarded.
    """
    if now is None:
        ref = datetime.now(timezone.utc).date()
    elif isinstance(now, datetime):
        ref = now.date()
    else:
        ref = now  # assume a date

    d = _to_date(trade_date) or _to_date(filed_date)
    if d is None:
        return 1.0, "Trade age unknown"

    age = (ref - d).days
    if age < 0:
        return 1.0, "Trade age unknown"
    for max_days, factor in RECENCY_TIERS:
        if age <= max_days:
            if factor >= 1.0:
                return factor, f"Recent trade, {age}d old"
            return factor, f"Trade {age}d old (x{factor:.2f})"
    return RECENCY_STALE_FACTOR, f"Trade {age}d old, stale (x{RECENCY_STALE_FACTOR:.2f})"


def _apply_recency(base_score: int, trade_date: str, filed_date: str, now,
                   reasons: list) -> int:
    """Cap the base score, scale it by the recency multiplier, append the reason."""
    base = min(base_score, 100)
    factor, reason = recency_factor(trade_date, filed_date, now)
    reasons.append(reason)
    return max(0, min(round(base * factor), 100))


def _normalize_chamber(base: int, chamber: str, reasons: list) -> int:
    """
    Scale a chamber's base score to the common ceiling POL_TARGET_BASE so chambers
    whose free feed omits fields aren't permanently capped below richer ones. Senate
    (dollar-only, ceiling 35) is scaled up toward 55; House (ceiling 55 == target) and
    any unknown source are returned unchanged, so House scores never regress. The
    result is capped at POL_TARGET_BASE so a normalized signal can't exceed the ceiling.
    Does not fabricate disclosure/committee data — only removes the structural penalty.
    """
    ceiling = CHAMBER_MAX_BASE.get(chamber, POL_TARGET_BASE)
    if ceiling >= POL_TARGET_BASE:
        return base
    factor = POL_TARGET_BASE / ceiling
    scaled = min(round(base * factor), POL_TARGET_BASE)
    if scaled != base:
        reasons.append(f"{chamber} feed lacks disclosure data, normalized (x{factor:.2f})")
    return scaled


def score_insider(role: str, value_text: str, filed_date: str, trade_date: str,
                  now=None) -> tuple[int, str]:
    score = 0
    reasons = []

    role_u = (role or "").upper()
    if any(t in role_u for t in TOP_TITLES):
        score += 30
        reasons.append("C-suite/officer buy (+30)")
    elif "DIRECTOR" in role_u:
        score += 15
        reasons.append("Director buy (+15)")
    else:
        score += 5
        reasons.append("Other insider (+5)")

    dollar = parse_dollar_high(value_text)
    if dollar >= 1_000_000:
        score += 30
        reasons.append(">$1M position (+30)")
    elif dollar >= 250_000:
        score += 20
        reasons.append(">$250K position (+20)")
    elif dollar >= 50_000:
        score += 10
        reasons.append(">$50K position (+10)")

    # Filing speed confirmation — fast Form 4 filings (within 2 days, as required)
    # read as "clean" signal quality. Normalize both dates first so a tz-aware atom
    # timestamp and a bare ISO trade date don't blow up on subtraction.
    f = _to_date(filed_date)
    t = _to_date(trade_date) or f
    if f is not None and t is not None:
        delay_days = (f - t).days
        if delay_days <= 2:
            score += 10
            reasons.append("Filed promptly (+10)")

    score = _apply_recency(score, trade_date, filed_date, now, reasons)
    return score, "; ".join(reasons)


def score_politician(person: str, committees: str, value_text: str, trade_date: str,
                     filed_date: str, chamber: str = "", now=None) -> tuple[int, str]:
    score = 0
    reasons = []

    # Be robust whether the caller passed raw feed dates (MM/DD/YYYY) or already-ISO
    # ones; normalize_date() is idempotent on ISO input.
    trade_date = normalize_date(trade_date)
    filed_date = normalize_date(filed_date)

    dollar = parse_dollar_high(value_text)
    if dollar >= 500_000:
        score += 35
        reasons.append(">$500K reported range (+35)")
    elif dollar >= 100_000:
        score += 25
        reasons.append(">$100K reported range (+25)")
    elif dollar >= 15_000:
        score += 10
        reasons.append(">$15K reported range (+10)")

    if committees and any(h in committees.upper() for h in COMMITTEE_SECTOR_HINTS):
        score += 15
        reasons.append("Committee/sector overlap (+15)")

    # STOCK Act allows up to 45 days to disclose — reward faster-than-typical filings.
    # Dates arrive normalized to ISO by normalize_date(). The Senate feed carries no
    # disclosure date at all, so we say so rather than guessing at a delay.
    if trade_date and filed_date:
        try:
            t = datetime.fromisoformat(trade_date)
            f = datetime.fromisoformat(filed_date)
            delay_days = (f - t).days
            if delay_days <= 0:
                # ~16% of House rows report disclosure_date == transaction_date. A
                # same-day STOCK Act disclosure is not credible; it's a transcription
                # placeholder. Awarding a speed bonus here would inflate the score of
                # precisely the least trustworthy rows, so we award nothing and say so.
                reasons.append("Disclosure date not reliable (+0)")
            elif delay_days <= 14:
                score += 20
                reasons.append(f"Fast disclosure, {delay_days}d (+20)")
            elif delay_days <= 30:
                score += 10
                reasons.append(f"Disclosure in {delay_days}d (+10)")
            else:
                reasons.append(f"Slow disclosure, {delay_days}d (+0)")
        except ValueError:
            reasons.append("Disclosure date unparseable (+0)")
    else:
        reasons.append("No disclosure date in feed (+0)")

    # Level the playing field across chambers before recency (see CHAMBER_MAX_BASE):
    # a Senate signal, which can only earn the dollar component, is scaled so a large
    # fresh Senate buy is competitive with a large fresh House buy.
    score = _normalize_chamber(score, chamber, reasons)
    score = _apply_recency(score, trade_date, filed_date, now, reasons)
    return score, "; ".join(reasons)


# ------------------------------------------------------------------------
# SOURCE 1: SEC Form 4 — free "current filings" feed, no API key
# ------------------------------------------------------------------------

def fetch_sec_form4() -> list[Signal]:
    """
    Pulls the most recent Form 4 filings market-wide from SEC's free atom feed.
    This feed gives you WHO filed and a link to the filing, but not parsed
    transaction-code/dollar detail (that requires opening each filing's XML,
    which the deeper `resolve_form4_detail()` helper below does on demand).
    """
    headers = {"User-Agent": SEC_USER_AGENT}
    resp = requests.get(SEC_CURRENT_FORM4_URL, headers=headers, timeout=20)
    resp.raise_for_status()

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(resp.content)

    signals = []
    for entry in root.findall("atom:entry", ns):
        title = entry.findtext("atom:title", default="", namespaces=ns)
        link_el = entry.find("atom:link", ns)
        link = link_el.get("href") if link_el is not None else ""
        updated = entry.findtext("atom:updated", default="", namespaces=ns)

        # Title format is typically "4 - Issuer Name (Filer Name) (Filer)"
        m = re.match(r"4\s*-\s*(.+)", title)
        company_and_person = m.group(1) if m else title

        sig_id = f"SEC-{link}"
        signals.append(Signal(
            id=sig_id,
            source="SEC Form 4",
            ticker="",  # not in this feed; resolve_form4_detail() can fill this in
            person=company_and_person,
            role="Insider (see filing)",
            action="Form 4 filed — verify transaction code P (buy) in filing",
            value="See filing",
            trade_date="",
            filed_date=updated,
            score=0,
            reasons="",
            url=link,
        ))
    return signals


def resolve_form4_detail(signal: Signal) -> Signal:
    """
    Optional deep-dive: fetch the actual Form 4 XML behind a filing link and pull
    out transaction code, ticker, price, and shares so scoring is accurate rather
    than a placeholder. Call this only on filings you're about to alert on, since
    it's one extra HTTP request per filing.
    """
    try:
        headers = {"User-Agent": SEC_USER_AGENT}
        index_resp = requests.get(signal.url, headers=headers, timeout=20)
        index_resp.raise_for_status()
        xml_links = re.findall(r'href="([^"]+\.xml)"', index_resp.text)
        form_xml_link = next((l for l in xml_links if "form4" in l.lower() or "primary_doc" not in l.lower()), None)
        if not form_xml_link:
            return signal
        if form_xml_link.startswith("/"):
            form_xml_link = "https://www.sec.gov" + form_xml_link

        xml_resp = requests.get(form_xml_link, headers=headers, timeout=20)
        xml_resp.raise_for_status()
        root = ET.fromstring(xml_resp.content)

        ticker = root.findtext(".//issuerTradingSymbol") or ""
        role_flags = []
        if root.findtext(".//isOfficer") == "1":
            role_flags.append(root.findtext(".//officerTitle") or "Officer")
        if root.findtext(".//isDirector") == "1":
            role_flags.append("Director")
        role = ", ".join(role_flags) or "Insider"

        code = root.findtext(".//transactionCode") or ""
        shares = root.findtext(".//transactionShares/value") or "0"
        price = root.findtext(".//transactionPricePerShare/value") or "0"
        trade_date = root.findtext(".//transactionDate/value") or ""

        try:
            dollar_value = float(shares) * float(price)
        except ValueError:
            dollar_value = 0.0

        signal.ticker = ticker
        signal.role = role
        signal.trade_date = trade_date
        signal.value = f"${dollar_value:,.0f}"
        signal.action = "BUY (code P)" if code == "P" else f"code {code} (not open-market buy)"
    except Exception:
        pass
    return signal


# ------------------------------------------------------------------------
# SOURCE 2 & 3: Congressional trades — free House/Senate Stock Watcher datasets
# ------------------------------------------------------------------------

def fetch_congress_trades(url: str, chamber: str) -> list[Signal]:
    """
    Pull congressional purchases from a free House/Senate JSON feed.

    Handles two shapes so the House and Senate sources (which differ) share one path:
      * a flat list of rows (House Stock Watcher mirror), or
      * an object wrapping the rows under "trades" / "transactions" (the
        efdsearch-scraping Senate feed, see SENATE_TRADES_URL).

    Field names also vary between feeds, so each is read through a set of aliases
    (e.g. disclosure_date | report_date | filed_date; amount | range | amount_range).

    Schema notes (verified against the live feeds):
      * Dates arrive as MM/DD/YYYY, not ISO -> normalize_date() before scoring.
      * A row with no usable disclosure date still scores on size + recency;
        score_politician() says so rather than inventing a delay it can't know.
      * ticker is often "--" for non-stock assets (bonds, funds); those are skipped.
    """
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):  # {"trades": [...]} / {"transactions": [...]} feeds
        data = data.get("trades") or data.get("transactions") or []

    signals = []
    for row in data:
        txn_type = (row.get("type") or row.get("transaction_type") or "").lower()
        if "purchase" not in txn_type and "buy" not in txn_type:
            continue

        ticker = (row.get("ticker") or row.get("symbol") or "").strip()
        if not ticker or ticker in ("--", "-", "N/A"):
            continue  # non-stock asset (bond/fund) or untranscribed filing

        person = row.get("representative") or row.get("senator") or row.get("name") or "Unknown"
        trade_date = normalize_date(row.get("transaction_date") or row.get("trade_date") or "")
        filed_date = normalize_date(
            row.get("disclosure_date") or row.get("report_date") or row.get("filed_date") or ""
        )
        amount = row.get("amount") or row.get("range") or row.get("amount_range") or ""
        committees = row.get("committees") or ""

        sig_id = f"CONGRESS-{chamber}-{person}-{ticker}-{trade_date}-{amount}"

        score, reasons = score_politician(person, committees, amount, trade_date,
                                          filed_date, chamber=chamber)

        signals.append(Signal(
            id=sig_id,
            source=f"Congress ({chamber})",
            ticker=ticker.upper(),
            person=person,
            role=chamber,
            action="BUY",
            value=amount,
            trade_date=trade_date,
            filed_date=filed_date,
            score=score,
            reasons=reasons,
            url=row.get("source_url") or row.get("ptr_link") or row.get("report_url") or "",
        ))
    return signals


# ------------------------------------------------------------------------
# ALERTING
# ------------------------------------------------------------------------

def format_alert(sig: Signal) -> str:
    bar_len = sig.score // 5
    bar = "█" * bar_len + "░" * (20 - bar_len)
    return (
        f"[{sig.score:>3}/100] {bar}  {sig.source}\n"
        f"  Ticker: {sig.ticker or '—'}   Person: {sig.person}\n"
        f"  Role: {sig.role}   Action: {sig.action}\n"
        f"  Value: {sig.value}   Trade: {sig.trade_date or '—'}   Filed: {sig.filed_date or '—'}\n"
        f"  Why: {sig.reasons or '—'}\n"
        f"  {sig.url}"
    )


def send_pushover(message: str):
    if not (PUSHOVER_TOKEN and PUSHOVER_USER):
        return
    requests.post("https://api.pushover.net/1/messages.json", data={
        "token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
        "title": "Insider / Politician Buy Alert", "message": message,
    }, timeout=10)


def send_discord(message: str):
    if not DISCORD_WEBHOOK_URL:
        return
    requests.post(DISCORD_WEBHOOK_URL, json={"content": f"```{message}```"}, timeout=10)


def send_telegram(message: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)


def dispatch_alert(sig: Signal):
    message = format_alert(sig)
    print(message, "\n" + "-" * 60)
    send_pushover(message)
    send_discord(message)
    send_telegram(message)
    if push is not None:
        # Concise notification for the phone (the ASCII bar above is console-only).
        title = f"{sig.score}/100 · {sig.ticker or sig.source}"
        body = f"{sig.person} — {sig.action}" + (f"  {sig.value}" if sig.value else "")
        push.send_push(title=title, body=body, url=sig.url or "/")


# ------------------------------------------------------------------------
# MAIN SCAN PASS
# ------------------------------------------------------------------------

def run_once(deep_resolve_top_n: int = 15):
    init_db()
    all_signals: list[Signal] = []

    # --- SEC insider buys ---
    try:
        form4_hits = fetch_sec_form4()
        # Only deep-resolve a capped number per pass to keep this free-tier friendly
        for sig in form4_hits[:deep_resolve_top_n]:
            if already_seen(sig.id):
                continue
            sig = resolve_form4_detail(sig)
            if "BUY" not in sig.action:
                continue  # skip sales / grants / other codes
            score, reasons = score_insider(sig.role, sig.value, sig.filed_date, sig.trade_date)
            sig.score, sig.reasons = score, reasons
            all_signals.append(sig)
    except Exception as e:
        print(f"[WARN] SEC Form 4 fetch failed: {e}")

    # --- Congressional buys ---
    try:
        all_signals.extend(fetch_congress_trades(HOUSE_TRADES_URL, "House"))
    except Exception as e:
        print(f"[WARN] House trades fetch failed: {e}")

    try:
        all_signals.extend(fetch_congress_trades(SENATE_TRADES_URL, "Senate"))
    except Exception as e:
        print(f"[WARN] Senate trades fetch failed: {e}")

    # --- Dedup, store, alert ---
    new_count = 0
    for sig in all_signals:
        if already_seen(sig.id):
            continue
        save_signal(sig)
        new_count += 1
        if sig.score >= MIN_ALERT_SCORE:
            dispatch_alert(sig)

    print(f"[{datetime.now().isoformat(timespec='seconds')}] scan complete — "
          f"{len(all_signals)} candidates, {new_count} new, threshold {MIN_ALERT_SCORE}")


def main():
    parser = argparse.ArgumentParser(description="Insider / Politician Buy Scanner")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=INTERVAL_SECONDS, help="Seconds between scans")
    parser.add_argument("--purge-stale-senate", action="store_true",
                        help="Delete Senate signals with trade_date before --before, then exit "
                             "(one-off cleanup of retired-mirror rows)")
    parser.add_argument("--before", default="2025-01-01",
                        help="Cutoff date YYYY-MM-DD for --purge-stale-senate (default 2025-01-01)")
    args = parser.parse_args()

    if args.purge_stale_senate:
        n = purge_stale_senate(args.before)
        print(f"Purged {n} stale Senate signal(s) with trade_date before {args.before}")
        return

    # One-off cleanup hook for hosts where a separate shell session can't reach the
    # service's persistent DB (e.g. Render): set PURGE_STALE_SENATE_BEFORE and the
    # purge runs HERE, inside the long-running process that actually owns the DB the
    # API reads. Idempotent — a second run deletes 0 — so it's safe to leave set, but
    # you can unset it once the count is confirmed.
    purge_before = os.getenv("PURGE_STALE_SENATE_BEFORE")
    if purge_before:
        try:
            n = purge_stale_senate(purge_before)
            print(f"[startup] purged {n} stale Senate signal(s) with trade_date before {purge_before}")
        except Exception as e:
            print(f"[WARN] startup purge failed: {e}")

    if args.loop:
        while True:
            try:
                run_once()
            except Exception as e:
                print(f"[ERROR] {datetime.now()}: {e}")
            time.sleep(args.interval)
    else:
        run_once()


if __name__ == "__main__":
    main()