import pytest
from calculations import (compute_summary, compute_profit_loss,
                          compute_monthly_series, _div)


# ---------- helpers ----------
def rec(period, income=0, expenses=0, cat="Sales", exp_cat="General"):
    return {"period": period, "income": income, "expenses": expenses,
            "category": cat, "expense_category": exp_cat}


# ---------- UT-01 / UT-02: net profit ----------
def test_net_profit_positive():
    r = compute_profit_loss([rec("2026-01", 50000, 32000)])
    assert r["net_profit"] == 18000
    assert r["net_profit_positive"] is True

def test_net_profit_loss():
    r = compute_profit_loss([rec("2026-01", 10000, 15000)])
    assert r["net_profit"] == -5000
    assert r["net_profit_positive"] is False


# ---------- UT-03: division by zero / missing data ----------
def test_div_by_zero_returns_none():
    assert _div(100, 0) is None
    assert _div(None, 5) is None

def test_net_margin_zero_revenue():
    r = compute_profit_loss([rec("2026-01", 0, 500)])
    assert r["net_margin"] == 0


# ---------- UT-04: P&L period filtering ----------
def test_profit_loss_period_filter():
    data = [rec("2026-01", 1000, 0), rec("2026-02", 2000, 0), rec("2026-03", 4000, 0)]
    r = compute_profit_loss(data, "2026-02", "2026-03")
    assert r["total_revenue"] == 6000
    assert r["record_count"] == 2


# ---------- UT-05: category grouping ----------
def test_revenue_grouped_by_category():
    data = [rec("2026-01", 300, 0, cat="Product"), rec("2026-01", 200, 0, cat="Service"),
            rec("2026-02", 100, 0, cat="Product")]
    r = compute_profit_loss(data)
    assert r["revenue_by_category"] == [("Product", 400.0), ("Service", 200.0)]


# ---------- UT-06: invalid / non-numeric amounts ----------
def test_non_numeric_amounts_treated_as_zero():
    r = compute_profit_loss([{"period": "2026-01", "income": "abc", "expenses": None}])
    assert r["total_revenue"] == 0 and r["total_expenses"] == 0


# ---------- UT-07: monthly series ----------
def test_monthly_series_sorted_and_unknown_last():
    data = [rec("2026-02", 200, 50), rec("", 10, 0), rec("2026-01", 100, 40)]
    s = compute_monthly_series(data)
    assert [m["period"] for m in s] == ["2026-01", "2026-02", "Unknown"]
    assert s[0]["net_cash_flow"] == 60


# ---------- UT-08: cash flow summary ----------
def test_summary_net_cash_flow():
    s = compute_summary([rec("2026-01", 50000, 32000)])
    assert s["net_cash_flow"] == 18000
    assert s["net_cf_positive"] is True

def test_summary_operating_and_free_cash_flow():
    s = compute_summary([rec("2026-01", 50000, 32000)],
                        balance_inputs={"non_cash_expenses": 2000,
                                        "change_in_working_capital": 1000,
                                        "capex": 5000})
    assert s["operating_cash_flow"] == 19000   # 18000 + 2000 - 1000
    assert s["free_cash_flow"] == 14000        # 19000 - 5000


# ---------- UT-09: liquidity ratios ----------
def test_liquidity_ratios():
    s = compute_summary([rec("2026-01", 10000, 5000)], total_invoices=5000,
                        balance_inputs={"cash": 10000, "inventory": 2000,
                                        "accounts_payable": 6000,
                                        "other_current_liabilities": 2000})
    # current assets = 10000 + 5000 + 2000 = 17000; CL = 8000
    assert s["working_capital"] == 9000
    assert s["current_ratio"] == 2.12
    assert s["quick_ratio"] == 1.88
    assert s["cash_ratio"] == 1.25

def test_ratios_unavailable_without_balance_inputs():
    s = compute_summary([rec("2026-01", 10000, 5000)])
    assert s["current_ratio"] is None
    assert s["liquidity_available"] is False


# ---------- UT-10: DSO ----------
def test_dso():
    s = compute_summary([rec("2026-01", 10000, 0)], total_invoices=5000)
    assert s["dso"] == 15.0          # 5000/10000 * 30 days


# ---------- UT-11: empty input ----------
def test_empty_summary():
    s = compute_summary([])
    assert s["total_income"] == 0
    assert s["current_ratio"] is None
    assert s["liquidity_available"] is False