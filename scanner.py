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
    python scanner.py                 # one pass
    python scanner.py --loop          # continuous, polls every INTERVAL_SECONDS
    streamlit run dashboard.py        # visual scanner table (separate file)

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

# Free, no-key congressional trade datasets (community-maintained mirrors of official
# House/Senate financial disclosures). Swap for Quiver/FMP/Finnhub later if you want
# faster refresh or more fields — see README.md.
# NOTE (2026): the original House/Senate Stock Watcher S3 buckets now return 403 and
# are no longer publicly served. These GitHub raw mirrors carry the same JSON schema
# and are still maintained. Verified reachable and parsing correctly.
HOUSE_TRADES_URL = os.getenv(
    "HOUSE_TRADES_URL",
    "https://raw.githubusercontent.com/TattooedHead/house-stock-watcher-data/master/data/all_transactions.json",
)
SENATE_TRADES_URL = os.getenv(
    "SENATE_TRADES_URL",
    "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json",
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


# ------------------------------------------------------------------------
# SCORING  (0-100, breakout-scanner style: several weighted "confirmations" stack up)
# ------------------------------------------------------------------------

def parse_dollar_high(value_text: str) -> float:
    """Congressional disclosures report ranges like '$100,001 - $250,000'. Take the high end."""
    nums = re.findall(r"[\d,]+", value_text or "")
    nums = [float(n.replace(",", "")) for n in nums if n]
    return max(nums) if nums else 0.0


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


def score_insider(role: str, value_text: str, filed_date: str, trade_date: str) -> tuple[int, str]:
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
    # read as "clean" signal quality
    try:
        f = datetime.fromisoformat(filed_date.replace("Z", "+00:00"))
        t = datetime.fromisoformat(trade_date.replace("Z", "+00:00")) if trade_date else f
        delay_days = (f - t).days
        if delay_days <= 2:
            score += 10
            reasons.append("Filed promptly (+10)")
    except Exception:
        pass

    return min(score, 100), "; ".join(reasons)


def score_politician(person: str, committees: str, value_text: str, trade_date: str, filed_date: str) -> tuple[int, str]:
    score = 0
    reasons = []

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

    return min(score, 100), "; ".join(reasons)


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
    Pull congressional purchases from the House/Senate Stock Watcher JSON mirrors.

    Schema notes (verified against the live feeds):
      * Dates arrive as MM/DD/YYYY, not ISO -> normalize_date() before scoring.
      * The Senate feed has NO disclosure_date field; only transaction_date. We fall
        back to the trade date, which means the disclosure-speed bonus can't be
        computed for Senate rows -- score_politician handles that without inventing
        a delay it can't actually know.
      * ticker is often "--" for non-stock assets (bonds, funds); those are skipped.
    """
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    data = resp.json()

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
        filed_date = normalize_date(row.get("disclosure_date") or row.get("report_date") or "")
        amount = row.get("amount") or row.get("range") or ""
        committees = row.get("committees") or ""

        sig_id = f"CONGRESS-{chamber}-{person}-{ticker}-{trade_date}-{amount}"

        score, reasons = score_politician(person, committees, amount, trade_date, filed_date)

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
            url=row.get("source_url") or row.get("ptr_link") or "",
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
    args = parser.parse_args()

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