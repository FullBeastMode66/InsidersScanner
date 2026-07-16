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
# cross-chamber normalization (Senate feed has no disclosure_date/committees)
# ------------------------------------------------------------------------

def test_house_scores_unchanged_by_normalization():
    # House ceiling == target, so passing chamber="House" must not alter the score.
    plain, _ = score_politician(
        "Rep X", "", "$1,000,001 - $5,000,000", "2026-06-01", "2026-06-10",
        now=date(2026, 6, 15),
    )
    house, reasons = score_politician(
        "Rep X", "", "$1,000,001 - $5,000,000", "2026-06-01", "2026-06-10",
        chamber="House", now=date(2026, 6, 15),
    )
    assert house == plain           # >$500K (+35) + fast disclosure (+20) = 55
    assert house == 55
    assert "normalized" not in reasons.lower()


def test_senate_large_fresh_buy_is_competitive():
    # Senate feed has no disclosure date -> only the dollar component is earnable.
    # Normalization must lift a large, fresh Senate buy to the common ceiling so it
    # clears the default alert threshold (50), matching a top-tier House buy.
    score, reasons = score_politician(
        "Sen Y", "", "$1,000,001 - $5,000,000", "2026-06-10", "",
        chamber="Senate", now=date(2026, 6, 15),
    )
    assert score == 55              # >$500K (+35) x (55/35) -> 55, fresh -> x1.0
    assert score >= 50              # now alert-eligible
    assert "No disclosure date in feed" in reasons
    assert "normalized" in reasons.lower()


def test_senate_normalization_capped_at_ceiling():
    # Normalized Senate score must never exceed the common ceiling.
    score, _ = score_politician(
        "Sen Y", "", "$5,000,000", "2026-06-10", "", chamber="Senate",
        now=date(2026, 6, 15),
    )
    assert score <= 55


def test_senate_small_buy_stays_below_threshold():
    # Normalization lifts the ceiling, not the noise floor: a small fresh Senate buy
    # must not become alert-eligible.
    score, _ = score_politician(
        "Sen Y", "", "$15,001 - $50,000", "2026-06-10", "", chamber="Senate",
        now=date(2026, 6, 15),
    )
    assert score < 50               # >$15K (+10) x1.57 -> ~16


def test_senate_stale_large_buy_still_suppressed():
    # Recency still applies after normalization: an old large Senate buy stays low.
    score, reasons = score_politician(
        "Sen Y", "", "$1,000,001 - $5,000,000", "2022-06-10", "",
        chamber="Senate", now=date(2026, 6, 15),
    )
    assert score < 50
    assert "stale" in reasons


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
