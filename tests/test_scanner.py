"""
Pure-logic tests for scanner.py — no live network calls.

Covers:
  * parse_dollar_high()   range parsing
  * score_insider()       scoring math
  * score_politician()    scoring math
  * dedup round-trip      already_seen()/save_signal() against a temp SQLite file
"""

import os
import sys
from datetime import date

import pytest

# Make scanner importable when tests are run from the repo root or the tests dir.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scanner  # noqa: E402
from scanner import (  # noqa: E402
    parse_dollar_high,
    score_insider,
    score_politician,
    recency_factor,
    already_seen,
    save_signal,
    init_db,
    Signal,
)

# Scoring tests pin `now` so recency (which decays by trade age) stays deterministic
# instead of rotting as the wall clock advances. Each pin sits within 30 days of the
# test's trade_date, so the recency multiplier is x1.0 and these assertions isolate the
# base scoring math; the recency tiers themselves are exercised separately below.


# ------------------------------------------------------------------------
# parse_dollar_high
# ------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("$100,001 - $250,000", 250000.0),
    ("$1,001 - $15,000", 15000.0),
    ("$1,000,000", 1000000.0),
    ("$50,001 - $100,000", 100000.0),
    ("", 0.0),
    (None, 0.0),
    ("no digits here", 0.0),
    ("$500", 500.0),
])
def test_parse_dollar_high(text, expected):
    assert parse_dollar_high(text) == expected


def test_parse_dollar_high_ignores_bare_commas():
    # A stray comma token must not be parsed as a number.
    assert parse_dollar_high("$, $15,000") == 15000.0


# ------------------------------------------------------------------------
# score_insider
# ------------------------------------------------------------------------

def test_score_insider_ceo_large_prompt():
    # C-suite (+30) + >$1M (+30) + filed within 2 days (+10) = 70
    score, reasons = score_insider(
        "CEO", "$2,000,000", "2026-07-03", "2026-07-02", now=date(2026, 7, 5)
    )
    assert score == 70
    assert "C-suite" in reasons
    assert ">$1M" in reasons
    assert "promptly" in reasons


def test_score_insider_director_midsize_no_date():
    # Director (+15) + >$250K (+20), no valid dates => no speed bonus, age unknown = 35
    score, reasons = score_insider("Director", "$300,000", "", "")
    assert score == 35
    assert "Director" in reasons


def test_score_insider_other_small():
    # Other insider (+5), dollar below thresholds, no dates = 5
    score, _ = score_insider("10% Owner", "$10,000", "", "")
    assert score == 5


def test_score_insider_prompt_bonus_with_iso_datetime_filed():
    # filed_date is a full ISO datetime with offset (as SEC's atom feed gives),
    # trade_date is a bare ISO date — must still compute the speed bonus.
    score, reasons = score_insider(
        "CFO", "$60,000", "2026-07-07T22:04:06-04:00", "2026-07-06",
        now=date(2026, 7, 10),
    )
    # C-suite (+30) + >$50K (+10) + prompt (+10) = 50
    assert score == 50
    assert "promptly" in reasons


def test_score_insider_capped_at_100():
    score, _ = score_insider(
        "CEO", "$5,000,000", "2026-07-03", "2026-07-02", now=date(2026, 7, 5)
    )
    assert score <= 100


# ------------------------------------------------------------------------
# score_politician
# ------------------------------------------------------------------------

def test_score_politician_large_fast_with_committee():
    # >$500K (+35) + committee (+15) + fast disclosure <=14d (+20) = 70
    score, reasons = score_politician(
        "Jane Doe", "Armed Services", "$1,000,001 - $5,000,000",
        "2026-06-01", "2026-06-10", now=date(2026, 6, 20),
    )
    assert score == 70
    assert ">$500K" in reasons
    assert "Committee" in reasons
    assert "Fast disclosure" in reasons


def test_score_politician_us_date_format_parses():
    # Legacy MM/DD/YYYY dates must still yield the disclosure-speed bonus.
    score, reasons = score_politician(
        "Jane Doe", "", "$15,001 - $50,000", "06/01/2026", "06/10/2026",
        now=date(2026, 6, 20),
    )
    # >$15K (+10) + fast disclosure 9d (+20) = 30
    assert score == 30
    assert "Fast disclosure, 9d" in reasons


def test_score_politician_slow_disclosure_no_bonus():
    score, reasons = score_politician(
        "Jane Doe", "", "$1,001 - $15,000", "2026-01-01", "2026-03-01",
        now=date(2026, 1, 10),
    )
    # below $15K high-end? high end = 15000 -> qualifies for +10; slow disclosure +0
    assert score == 10
    assert "Slow disclosure" in reasons


def test_score_politician_no_dates_no_committee():
    score, _ = score_politician("Jane Doe", "", "$100,001 - $250,000", "", "")
    # >$100K (+25) only, trade age unknown => no decay
    assert score == 25


# ------------------------------------------------------------------------
# cross-chamber scoring
#
# Both current feeds carry transaction + disclosure dates, so the chambers score
# identically and _normalize_chamber() is a no-op. It is retained only for a
# hypothetical future field-poor feed, unit-tested directly at the end.
# ------------------------------------------------------------------------

def test_house_and_senate_score_identically():
    # Same inputs, different chamber -> identical score (normalization inert), and no
    # spurious "normalized" note now that both feeds carry disclosure dates.
    args = ("Member", "", "$1,000,001 - $5,000,000", "2026-06-10", "2026-06-18")
    house, hr = score_politician(*args, chamber="House", now=date(2026, 6, 25))
    senate, sr = score_politician(*args, chamber="Senate", now=date(2026, 6, 25))
    assert house == senate == 55    # >$500K (+35) + fast disclosure 8d (+20) = 55
    assert "normalized" not in (hr + sr).lower()


def test_senate_large_fresh_buy_is_competitive():
    # With disclosure dates in the feed, a large fresh Senate buy reaches the ceiling
    # on its own merits (size + disclosure speed) and clears the alert threshold.
    score, reasons = score_politician(
        "Sen Y", "", "$1,000,001 - $5,000,000", "2026-06-10", "2026-06-18",
        chamber="Senate", now=date(2026, 6, 25),
    )
    assert score == 55              # >$500K (+35) + fast disclosure 8d (+20)
    assert score >= 50              # alert-eligible
    assert "Fast disclosure" in reasons
    assert "normalized" not in reasons.lower()


def test_senate_no_disclosure_scores_on_size_only():
    # A row without a disclosure date isn't inflated -- it just scores lower (fewer
    # confirmations). Fresh >$500K with no disclosure date = 35, not 55.
    score, reasons = score_politician(
        "Sen Y", "", "$1,000,001 - $5,000,000", "2026-06-10", "",
        chamber="Senate", now=date(2026, 6, 25),
    )
    assert score == 35
    assert "No disclosure date in feed" in reasons
    assert "normalized" not in reasons.lower()


def test_senate_stale_large_buy_still_suppressed():
    # Recency suppresses an old large Senate buy regardless of chamber.
    score, reasons = score_politician(
        "Sen Y", "", "$1,000,001 - $5,000,000", "2022-06-10", "2022-06-18",
        chamber="Senate", now=date(2026, 6, 25),
    )
    assert score < 50
    assert "stale" in reasons


def test_normalize_chamber_still_scales_a_field_poor_source(monkeypatch):
    # Defensive machinery: a hypothetical feed whose chamber can only earn the dollar
    # component (ceiling below target) is scaled up toward the common ceiling.
    monkeypatch.setitem(scanner.CHAMBER_MAX_BASE, "PoorFeed", scanner.POL_MAX_DOLLAR)
    reasons = []
    scaled = scanner._normalize_chamber(scanner.POL_MAX_DOLLAR, "PoorFeed", reasons)
    assert scaled == scanner.POL_TARGET_BASE            # 35 -> 55
    assert any("normalized" in r.lower() for r in reasons)


# ------------------------------------------------------------------------
# recency decay
# ------------------------------------------------------------------------

def test_recency_factor_tiers():
    # trade date fixed; vary "now" to walk each tier boundary.
    trade = "2026-01-01"
    assert recency_factor(trade, now=date(2026, 1, 20))[0] == 1.00   # 19d
    assert recency_factor(trade, now=date(2026, 2, 15))[0] == 0.85   # 45d
    assert recency_factor(trade, now=date(2026, 5, 1))[0] == 0.60    # 120d
    assert recency_factor(trade, now=date(2026, 12, 1))[0] == 0.35   # 334d, stale


def test_recency_factor_unknown_when_undated():
    factor, reason = recency_factor("", "", now=date(2026, 7, 1))
    assert factor == 1.0
    assert "unknown" in reason.lower()


def test_recency_factor_future_trade_not_rewarded():
    # A trade dated in the future is bad data, not a fresh signal.
    factor, _ = recency_factor("2026-08-01", now=date(2026, 7, 1))
    assert factor == 1.0


def test_recency_factor_falls_back_to_filed_date():
    # No trade date, but a disclosure date -> age measured from disclosure.
    factor, _ = recency_factor("", "2026-01-01", now=date(2026, 1, 20))
    assert factor == 1.00


def test_score_politician_stale_trade_is_discounted():
    # Same strong trade, scored fresh vs. stale: the stale one must rank lower and
    # fall below the default alert threshold (50), i.e. it stops alerting on its own.
    fresh, _ = score_politician(
        "Jane Doe", "Armed Services", "$1,000,001 - $5,000,000",
        "2026-06-01", "2026-06-10", now=date(2026, 6, 15),
    )
    stale, reasons = score_politician(
        "Jane Doe", "Armed Services", "$1,000,001 - $5,000,000",
        "2023-06-01", "2023-06-10", now=date(2026, 6, 15),
    )
    assert fresh == 70
    assert stale < fresh
    assert stale < 50
    assert "stale" in reasons


# ------------------------------------------------------------------------
# fetch_congress_trades schema handling (network mocked)
# ------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def test_fetch_congress_trades_parses_wrapped_senate_feed(monkeypatch):
    # The efdsearch-scraped Senate feed wraps rows under "trades" and uses
    # filed_date / amount_range field names -> must still yield scored purchase Signals.
    payload = {"generated_at": "2026-07-16", "trades": [
        {"senator": "Sen Y", "ticker": "msft", "transaction_date": "06/10/2026",
         "filed_date": "06/18/2026", "type": "Purchase",
         "amount_range": "$1,000,001 - $5,000,000", "report_url": "http://efd/x"},
        {"senator": "Sen Y", "ticker": "AAPL", "transaction_date": "06/10/2026",
         "filed_date": "06/18/2026", "type": "Sale (Full)", "amount_range": "$1,001 - $15,000"},
        {"senator": "Sen Z", "ticker": "--", "transaction_date": "06/10/2026",
         "filed_date": "06/18/2026", "type": "Purchase", "amount_range": "$1,001 - $15,000"},
    ]}
    monkeypatch.setattr(scanner.requests, "get", lambda *a, **k: _FakeResp(payload))
    sigs = scanner.fetch_congress_trades("http://x", "Senate")
    # only row 1 survives: purchase + real ticker (sale skipped, "--" skipped)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.ticker == "MSFT" and s.person == "Sen Y" and s.source == "Congress (Senate)"
    assert s.trade_date == "2026-06-10" and s.filed_date == "2026-06-18"
    assert s.url == "http://efd/x"
    assert ">$500K" in s.reasons and "Fast disclosure" in s.reasons
    assert "normalized" not in s.reasons.lower()


def test_fetch_congress_trades_parses_flat_house_feed(monkeypatch):
    # The House mirror is a flat list with disclosure_date / amount fields.
    payload = [
        {"representative": "Rep X", "ticker": "NVDA", "transaction_date": "06/10/2026",
         "disclosure_date": "06/18/2026", "type": "purchase", "amount": "$1,001 - $15,000"},
    ]
    monkeypatch.setattr(scanner.requests, "get", lambda *a, **k: _FakeResp(payload))
    sigs = scanner.fetch_congress_trades("http://x", "House")
    assert len(sigs) == 1
    assert sigs[0].ticker == "NVDA" and sigs[0].source == "Congress (House)"


# ------------------------------------------------------------------------
# dedup round-trip (already_seen / save_signal) against a temp SQLite file
# ------------------------------------------------------------------------

def _make_signal(sig_id="SEC-TEST-1"):
    return Signal(
        id=sig_id, source="SEC Form 4", ticker="ABC", person="Test Person",
        role="CEO", action="BUY (code P)", value="$100,000",
        trade_date="2026-07-01", filed_date="2026-07-02", score=70,
        reasons="test", url="http://example.com",
    )


def test_dedup_roundtrip(tmp_path):
    db = str(tmp_path / "scanner_test.db")
    init_db(db)

    sig = _make_signal()
    assert already_seen(sig.id, db) is False

    save_signal(sig, db)
    assert already_seen(sig.id, db) is True


def test_save_signal_is_idempotent(tmp_path):
    db = str(tmp_path / "scanner_test.db")
    init_db(db)

    sig = _make_signal()
    save_signal(sig, db)
    save_signal(sig, db)  # INSERT OR IGNORE — must not duplicate or raise

    import sqlite3
    conn = sqlite3.connect(db)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE id=?", (sig.id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_distinct_ids_coexist(tmp_path):
    db = str(tmp_path / "scanner_test.db")
    init_db(db)

    save_signal(_make_signal("SEC-A"), db)
    save_signal(_make_signal("SEC-B"), db)

    assert already_seen("SEC-A", db) is True
    assert already_seen("SEC-B", db) is True
    assert already_seen("SEC-C", db) is False
