"""Tests for the depth join.

The join always produces a file; whether it produced a dataset is the
question, so that is what these check.
"""

from __future__ import annotations

import csv

import pytest

from merge_depth import main, normalise


def _write(path, rows, fields):
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _features(path, stamps, spelling="{}+00:00"):
    rows = [{"timestamp": spelling.format(s), "mid": "3850.0",
             "l2_imbalance_5": "0.0", "l2_imbalance_3": "0.0"} for s in stamps]
    _write(path, rows, ["timestamp", "mid", "l2_imbalance_5",
                        "l2_imbalance_3"])


def _depth(path, stamps, imb=0.25):
    rows = [{"timestamp": f"{s}Z", "l2_levels": "20",
             "l2_bid_vol_total": "100", "l2_ask_vol_total": "60",
             "l2_imbalance_5": str(imb), "l2_imbalance_3": str(imb)}
            for s in stamps]
    _write(path, rows, ["timestamp", "l2_levels", "l2_bid_vol_total",
                        "l2_ask_vol_total", "l2_imbalance_5",
                        "l2_imbalance_3"])


STAMPS = ["2026-08-26T14:00:00", "2026-08-26T14:05:00",
          "2026-08-26T14:10:00", "2026-08-26T14:15:00"]


def test_z_and_offset_spellings_are_the_same_instant():
    assert (normalise("2026-08-26T14:00:00+00:00")
            == normalise("2026-08-26T14:00:00Z")
            == "2026-08-26T14:00:00")


def test_a_full_match_fills_the_dead_features(tmp_path, capsys):
    f, d = tmp_path / "f.csv", tmp_path / "d.csv"
    _features(f, STAMPS)
    _depth(d, STAMPS)
    assert main(["--features", str(f), "--depth", str(d)]) == 0
    assert "(100.0%)" in capsys.readouterr().out

    out = list(csv.DictReader((tmp_path / "f_depth.csv").open(),
                              skipinitialspace=True))
    # The header comment line is skipped by DictReader? No - check explicitly.
    rows = [r for r in out if r.get("timestamp")]
    assert all(r["l2_imbalance_5"] == "0.25" for r in rows)


def test_a_misaligned_clock_fails_instead_of_writing_empty_columns(tmp_path):
    f, d = tmp_path / "f.csv", tmp_path / "d.csv"
    _features(f, STAMPS)
    _depth(d, ["2026-08-26T17:00:00", "2026-08-26T17:05:00",
               "2026-08-26T17:10:00", "2026-08-26T17:15:00"])
    with pytest.raises(SystemExit) as caught:
        main(["--features", str(f), "--depth", str(d)])
    assert "below --min-match" in str(caught.value)
    assert not (tmp_path / "f_depth.csv").exists()


def test_a_partial_match_is_allowed_but_reported(tmp_path, capsys):
    f, d = tmp_path / "f.csv", tmp_path / "d.csv"
    _features(f, STAMPS)
    _depth(d, STAMPS[:3])
    assert main(["--features", str(f), "--depth", str(d)]) == 0
    report = capsys.readouterr().out
    assert "(75.0%)" in report
    assert "first unmatched feature timestamps" in report


def test_rows_without_depth_keep_their_original_values(tmp_path):
    f, d = tmp_path / "f.csv", tmp_path / "d.csv"
    _features(f, STAMPS)
    _depth(d, STAMPS[:3])
    main(["--features", str(f), "--depth", str(d)])
    rows = [r for r in csv.DictReader((tmp_path / "f_depth.csv").open())
            if r.get("timestamp")]
    assert rows[-1]["l2_imbalance_5"] == "0.0"
    assert rows[-1]["l2_levels"] == ""


def test_a_bom_in_either_file_does_not_break_the_join(tmp_path):
    """Windows writes them. The .env lesson, applied before it costs a run."""
    f, d = tmp_path / "f.csv", tmp_path / "d.csv"
    _features(f, STAMPS)
    _depth(d, STAMPS)
    f.write_bytes(b"\xef\xbb\xbf" + f.read_bytes())
    d.write_bytes(b"\xef\xbb\xbf" + d.read_bytes())
    assert main(["--features", str(f), "--depth", str(d)]) == 0


def test_the_csv_is_readable_by_a_plain_csv_reader(tmp_path):
    """No comment line, no BOM, nothing clever. The first version put a `#`
    provenance line at the top and every DictReader downstream read it as the
    header."""
    f, d = tmp_path / "f.csv", tmp_path / "d.csv"
    _features(f, STAMPS)
    _depth(d, STAMPS)
    main(["--features", str(f), "--depth", str(d)])

    rows = list(csv.DictReader((tmp_path / "f_depth.csv").open()))
    assert len(rows) == len(STAMPS)
    assert set(rows[0]) >= {"timestamp", "mid", "l2_imbalance_5", "l2_levels"}


def test_the_cross_instrument_warning_is_recorded_in_the_sidecar(tmp_path):
    import json
    f, d = tmp_path / "f.csv", tmp_path / "d.csv"
    _features(f, STAMPS)
    _depth(d, STAMPS)
    main(["--features", str(f), "--depth", str(d)])

    meta = json.loads((tmp_path / "f_depth.meta.json").read_text())
    assert "CROSS-INSTRUMENT" in meta["warning"]
    assert meta["match_share"] == 1.0
    assert meta["columns_filled"] == ["l2_imbalance_5", "l2_imbalance_3"]
