import sys
from unittest.mock import patch
import pytest
from flask import Flask
import forecasting as fc


# ---------- helpers ----------
def recs(values, start_year=2026, start_month=1):
    """One income-only record per month."""
    out = []
    for i, v in enumerate(values):
        m = start_month - 1 + i
        out.append({"period": f"{start_year + m // 12}-{m % 12 + 1:02d}",
                    "income": v, "expenses": 0})
    return out

def call(records, horizon=30, invoices=None):
    """Runs get_forecast inside a Flask app context; returns (json, status)."""
    with Flask(__name__).app_context():
        res = fc.get_forecast(records, horizon, invoices)
        resp, status = res if isinstance(res, tuple) else (res, 200)
        return resp.get_json(), status


# ---------- Month helpers ----------
def test_ym_index_valid_and_roundtrip():
    assert fc._idx_to_period(fc._ym_index("2026-03")) == "2026-03"

@pytest.mark.parametrize("bad", ["", None, "2026-13", "March", "2026/03"])
def test_ym_index_invalid_returns_none(bad):
    assert fc._ym_index(bad) is None

def test_idx_to_period_december_boundary():
    assert fc._idx_to_period(fc._ym_index("2026-12") + 1) == "2027-01"


# ---------- Monthly aggregation ----------
def test_monthly_series_aggregates_net_cash_flow():
    data = [{"period": "2026-01", "income": 1000, "expenses": 400},
            {"period": "2026-01", "income": 500,  "expenses": 100},
            {"period": "2026-02", "income": 200,  "expenses": 300}]
    series, skipped = fc._monthly_series(data)
    assert [v for _, v in series] == [1000.0, -100.0] and skipped == 0

def test_monthly_series_skips_invalid_periods():
    data = [{"period": "2026-01", "income": 100}, {"period": "bad", "income": 5},
            {"period": None, "income": 5}]
    series, skipped = fc._monthly_series(data)
    assert len(series) == 1 and skipped == 2


# ---------- Forecasting methods ----------
def test_trend_extrapolates_perfect_line():
    out = fc._trend([(0, 100), (1, 200), (2, 300)], [3])
    yhat, lower, upper = out[0]
    assert yhat == pytest.approx(400) and lower == pytest.approx(upper)

def test_trend_flat_series_gives_flat_forecast():
    out = fc._trend([(0, 50), (1, 50), (2, 50)], [3, 4])
    assert [round(p[0], 2) for p in out] == [50.0, 50.0]

def test_trend_interval_widens_with_noisy_data():
    yhat, lower, upper = fc._trend([(0, 100), (1, 300), (2, 150), (3, 350)], [4])[0]
    assert lower < yhat < upper

def test_moving_average_uses_last_three_months():
    out = fc._moving_average([(0, 10), (1, 20), (2, 30), (3, 40)], [4])
    assert out[0][0] == pytest.approx(30)      # (20+30+40)/3


# ---------- Method selection ----------
def test_choose_method_few_months_uses_trend():
    method, notes = fc._choose_method(4)
    assert method == "trend" and notes

def test_choose_method_falls_back_when_prophet_missing():
    with patch.dict(sys.modules, {"prophet": None}):
        method, notes = fc._choose_method(8)
    assert method == "trend" and "not installed" in notes[0]

def test_choose_method_uses_prophet_when_available():
    pytest.importorskip("prophet")
    assert fc._choose_method(8)[0] == "prophet"


# ---------- get_forecast: validation ----------
def test_insufficient_data_rejected():
    body, status = call(recs([100, 200]))
    assert status == 400 and body["error"] == "Not enough data"

def test_no_records_rejected():
    assert call([])[1] == 400


# ---------- get_forecast: output ----------
def test_three_months_returns_trend_forecast():
    body, status = call(recs([100, 200, 300]))
    assert status == 200 and body["method"] == "trend"
    assert body["months_of_data"] == 3
    assert body["projected"][0]["date"] == "2026-04"
    assert body["projected"][0]["value"] == pytest.approx(400)

@pytest.mark.parametrize("horizon, points", [(30, 1), (60, 2), (90, 3), (45, 2), (365, 3), (1, 1)])
def test_horizon_maps_to_projection_points(horizon, points):
    body, _ = call(recs([100, 200, 300, 400]), horizon)
    assert len(body["projected"]) == points

def test_total_projected_matches_sum():
    body, _ = call(recs([100, 200, 300, 400]), 60)
    assert body["total_projected"] == pytest.approx(sum(p["value"] for p in body["projected"]))

def test_invalid_period_records_noted_not_invented():
    data = recs([100, 200, 300]) + [{"period": "oops", "income": 999, "expenses": 0}]
    body, status = call(data)
    assert status == 200 and body["months_of_data"] == 3
    assert any("left out" in n for n in body["notes"])

def test_prophet_forecast_when_six_plus_months():
    pytest.importorskip("prophet")
    body, status = call(recs([1000, 1100, 1250, 1300, 1450, 1500, 1620, 1700]), 90)
    assert status == 200 and body["method"] == "prophet"
    assert len(body["projected"]) == 3
    for p in body["projected"]:
        assert p["lower"] <= p["value"] <= p["upper"]


# ---------- Back-test ----------
def test_backtest_skipped_with_short_history():
    body, _ = call(recs([100, 200, 300, 400, 500]))
    assert body["backtest"] is None

def test_backtest_trend_beats_baseline_on_linear_data():
    series, _ = fc._monthly_series(recs([100, 200, 300, 400, 500, 600]))
    bt = fc._backtest(series, "trend")
    assert bt["months_held_out"] == 2
    assert bt["model"]["mae"] == pytest.approx(0, abs=0.01)
    assert bt["beats_baseline"] is True


# ---------- Known receivables ----------
def test_receivables_none_without_invoices():
    assert fc._receivables_due([], [100]) is None
    assert fc._receivables_due(None, [100]) is None

def test_receivables_grouped_by_due_month():
    t1 = fc._ym_index("2026-05")
    invs = [{"amount": 100, "due_date": "2026-05-10"},
            {"amount": 50,  "due_date": "2026-05-20"},
            {"total": 70,   "due_date": "2026-06-01"},
            {"amount": 30,  "due_date": "2026-03-01"},     # before first target
            {"amount": 99,  "due_date": "garbage"}]         # ignored
    out = fc._receivables_due(invs, [t1, t1 + 1])
    assert out["earlier"] == 30
    assert out["by_month"][0] == {"date": "2026-05", "amount": 150.0, "count": 2}
    assert out["by_month"][1]["amount"] == 70.0