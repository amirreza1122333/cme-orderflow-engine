"""Tests for the market journalist.

The two that matter are the number guard and the honesty of a comparison
made from too little history. Everything else is plumbing.
"""

from __future__ import annotations

import json

import pytest

from reporter import (
    MIN_HISTORY,
    Ledger,
    UnsourcedNumber,
    archive,
    build,
    load_rows,
    observe,
    verify,
)


def _row(**over):
    row = {
        "timestamp": "2026-09-02T12:00:00Z",
        "bid": "3849.70", "ask": "3850.30", "mid": "3850.00",
        "session": "3", "session_progress": "0.5",
        "asian_range_width": "12.0", "fvg_count_unfilled": "2",
        "asian_swept_high": "0", "asian_swept_low": "0",
        "asian_returned_high": "0", "asian_returned_low": "0",
        "swept_prev_high": "0", "swept_prev_low": "0",
        "l2_imbalance_5": "0", "l2_imbalance_3": "0",
        "l2_imbalance_at_asian_high": "0", "l2_imbalance_at_asian_low": "0",
    }
    row.update({k: str(v) for k, v in over.items()})
    return row


def _history(days: int):
    rows = []
    for d in range(days):
        for m in range(3):
            rows.append(_row(timestamp=f"2026-08-{d + 1:02d}T{10 + m}:00:00Z",
                             asian_range_width=8.0 + d * 0.5,
                             fvg_count_unfilled=d % 4))
    return rows


# ---------------------------------------------------------- the number guard

def test_a_number_the_ledger_never_issued_is_rejected():
    ledger = Ledger()
    ledger.num(3850.0)
    with pytest.raises(UnsourcedNumber):
        verify("Mid 3850.00, and support at 3820.", ledger)


def test_a_number_the_ledger_issued_passes():
    ledger = Ledger()
    ledger.num(3850.0)
    verify("Mid 3850.00 and nothing else.", ledger)


def test_the_real_report_contains_no_invented_number():
    """The guard running on the actual renderer, not a fixture."""
    report = build(_history(40) + [_row()], "XAUUSD")
    assert report.text          # verify() ran inside build and did not raise


def test_a_price_written_into_a_template_would_fail_the_build(monkeypatch):
    """Prove the guard bites, by making the renderer misbehave.

    A test that only checks the good path cannot tell a working guard from a
    guard that is never consulted.
    """
    import reporter

    original = reporter._phrase_rank
    monkeypatch.setattr(
        reporter, "_phrase_rank",
        lambda obs, ledger, unit, lead=True: original(obs, ledger, unit, lead)
        + " Resistance sits at 3899."
    )
    with pytest.raises(UnsourcedNumber):
        build(_history(40) + [_row()], "XAUUSD")


# -------------------------------------------------------- honest comparisons

def test_too_little_history_refuses_to_give_a_percentile():
    obs = observe("width", 12.0, [8.0, 9.0, 10.0])
    assert obs.percentile is None
    assert not obs.comparable


def test_enough_history_gives_a_percentile_and_its_n():
    obs = observe("width", 12.0, [float(i) for i in range(MIN_HISTORY + 10)])
    assert obs.comparable
    assert obs.n == MIN_HISTORY + 10
    assert obs.percentile == pytest.approx(100 * 12 / (MIN_HISTORY + 10))


def test_a_short_history_says_so_in_the_report():
    report = build(_history(3) + [_row()], "XAUUSD")
    assert "no comparison yet" in report.text
    assert "percentile" not in report.text


def test_a_long_history_reports_a_percentile_with_its_n():
    report = build(_history(MIN_HISTORY + 5) + [_row()], "XAUUSD")
    assert "percentile" in report.text
    assert "no comparison yet" not in report.text


def test_nan_values_are_not_counted_as_history():
    obs = observe("width", 5.0, [1.0, float("nan"), 9.0])
    assert obs.n == 2


# ------------------------------------------------------------------ content

def test_a_dead_order_book_is_named_not_reported_as_zero():
    """All four l2 features are constant on the .scid feed.

    Printing "imbalance 0.00" would read as a measurement. It is the absence
    of one, and the report has to say which.
    """
    report = build(_history(40) + [_row()], "XAUUSD")
    assert "Order-book imbalance is unavailable" in report.text


def test_structure_flags_are_listed_when_they_happened():
    report = build(_history(40) + [_row(asian_swept_high=1,
                                        swept_prev_low=1)], "XAUUSD")
    assert "swept the Asian high" in report.text
    assert "swept yesterday's low" in report.text
    assert "No sweep or return" not in report.text


def test_a_quiet_day_says_nothing_happened_rather_than_listing_nothing():
    report = build(_history(40) + [_row()], "XAUUSD")
    assert "No sweep or return has been recorded today." in report.text


# ------------------------------------------------------------------ archive

def test_the_state_is_archived_beside_the_report(tmp_path):
    report = build(_history(40) + [_row()], "XAUUSD")
    md, js = archive(report, tmp_path, "2026-09-02T12:00:00Z")
    assert md.read_text(encoding="utf-8") == report.text

    saved = json.loads(js.read_text(encoding="utf-8"))
    # The state must be the row the report was written from, not a summary of
    # it: next month's question is "what did it see", not "what did it say".
    assert saved["state"]["mid"] == "3850.00"
    assert saved["state"]["asian_range_width"] == "12.0"
    assert any(o["name"] == "Asian range width" for o in saved["observations"])


def test_reports_from_different_timestamps_do_not_overwrite(tmp_path):
    report = build(_history(40) + [_row()], "XAUUSD")
    first, _ = archive(report, tmp_path, "2026-09-02T12:00:00Z")
    second, _ = archive(report, tmp_path, "2026-09-02T16:00:00Z")
    assert first != second
    assert first.exists() and second.exists()


def test_rows_are_read_in_timestamp_order_across_files(tmp_path):
    (tmp_path / "ict_XAUUSD_20260902.csv").write_text(
        "timestamp,mid\n2026-09-02T01:00:00Z,2\n", encoding="utf-8")
    (tmp_path / "ict_XAUUSD_20260901.csv").write_text(
        "timestamp,mid\n2026-09-01T01:00:00Z,1\n", encoding="utf-8")
    rows = load_rows(tmp_path, "XAUUSD")
    assert [r["mid"] for r in rows] == ["1", "2"]


def test_another_symbols_files_are_not_read(tmp_path):
    (tmp_path / "ict_XAUUSD_20260902.csv").write_text(
        "timestamp,mid\n2026-09-02T01:00:00Z,1\n", encoding="utf-8")
    (tmp_path / "ict_BTCUSD_20260902.csv").write_text(
        "timestamp,mid\n2026-09-02T01:00:00Z,2\n", encoding="utf-8")
    assert len(load_rows(tmp_path, "XAUUSD")) == 1
