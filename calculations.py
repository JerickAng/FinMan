"""
calculations.py
All financial calculation logic for FinMan.
Covers: cash flow metrics, liquidity ratios, efficiency ratios,
        cash flow quality ratios.
"""

def compute_summary(records: list, total_invoices: float = 0) -> dict:
    """
    Takes a list of financial record dicts from Firestore and returns a
    flat dict of all computed financial metrics.

    total_invoices should be the real outstanding-invoices total, sourced
    from the invoices collection by the caller -- financial records no
    longer carry an 'invoices' field (invoices moved to their own
    dedicated feature), so this must be passed in explicitly rather than
    summed from records.
    """
    if not records and not total_invoices:
        return _empty_summary()

    total_income   = sum(float(r.get("income",   0) or 0) for r in records)
    total_expenses = sum(float(r.get("expenses", 0) or 0) for r in records)

    # ── Cash Flow & Health Metrics ────────────────────────────────────────────
    net_cash_flow       = total_income - total_expenses
    operating_cash_flow = net_cash_flow * 0.85           # simplified estimate
    free_cash_flow      = operating_cash_flow - (total_expenses * 0.1)

    # ── Liquidity Ratios ──────────────────────────────────────────────────────
    # Simplified: treat income as current assets, expenses as current liabilities
    current_assets      = total_income
    current_liabilities = total_expenses if total_expenses > 0 else 1
    inventory           = total_income * 0.1             # simplified

    current_ratio       = round(current_assets / current_liabilities, 2)
    quick_ratio         = round((current_assets - inventory) / current_liabilities, 2)
    working_capital     = current_assets - current_liabilities

    # ── Efficiency Ratios ─────────────────────────────────────────────────────
    # DSO = (Accounts Receivable / Total Revenue) * Days in period
    days_in_period      = 30
    accounts_receivable = total_invoices
    revenue             = total_income if total_income > 0 else 1

    dso                 = round((accounts_receivable / revenue) * days_in_period, 1)
    ar_turnover         = round(revenue / accounts_receivable, 2) if accounts_receivable > 0 else 0
    # Cash Conversion Cycle simplified
    dpo                 = round((total_expenses / revenue) * days_in_period, 1)
    cash_conversion     = round(dso - dpo, 1)

    # ── Cash Flow Quality Ratios ──────────────────────────────────────────────
    net_income          = net_cash_flow
    cf_quality          = round(operating_cash_flow / net_income, 2) if net_income != 0 else 0

    return {
        # Raw totals
        "total_income":        round(total_income, 2),
        "total_expenses":      round(total_expenses, 2),
        "total_invoices":      round(total_invoices, 2),
        # Cash flow
        "net_cash_flow":       round(net_cash_flow, 2),
        "operating_cash_flow": round(operating_cash_flow, 2),
        "free_cash_flow":      round(free_cash_flow, 2),
        # Liquidity
        "current_ratio":       current_ratio,
        "quick_ratio":         quick_ratio,
        "working_capital":     round(working_capital, 2),
        # Efficiency
        "dso":                 dso,
        "ar_turnover":         ar_turnover,
        "cash_conversion":     cash_conversion,
        # Quality
        "cf_quality":          cf_quality,
        # Helpers for colour coding in templates
        "net_cf_positive":     net_cash_flow >= 0,
        "current_ratio_ok":    1.5 <= current_ratio <= 3.0,
        "dso_ok":              dso <= 45,
    }


def compute_monthly_series(records: list) -> list:
    """
    Groups records by their `period` field (expected format YYYY-MM) and
    sums income/expenses/invoices within each period, returning a
    chronologically sorted list of real data points for charting.
    Records with a missing or malformed period are grouped under "Unknown"
    and sorted last, rather than silently dropped or guessed at.
    """
    buckets = {}
    for r in records:
        period = (r.get("period") or "").strip() or "Unknown"
        b = buckets.setdefault(period, {"income": 0.0, "expenses": 0.0, "invoices": 0.0})
        b["income"]   += float(r.get("income",   0) or 0)
        b["expenses"] += float(r.get("expenses", 0) or 0)
        b["invoices"] += float(r.get("invoices", 0) or 0)

    def sort_key(period):
        # Real YYYY-MM periods sort chronologically; "Unknown" sorts last.
        return (1, "") if period == "Unknown" else (0, period)

    series = []
    for period in sorted(buckets.keys(), key=sort_key):
        b = buckets[period]
        series.append({
            "period":         period,
            "income":         round(b["income"], 2),
            "expenses":       round(b["expenses"], 2),
            "invoices":       round(b["invoices"], 2),
            "net_cash_flow":  round(b["income"] - b["expenses"], 2),
        })
    return series


def _empty_summary() -> dict:
    keys = [
        "total_income", "total_expenses", "total_invoices",
        "net_cash_flow", "operating_cash_flow", "free_cash_flow",
        "current_ratio", "quick_ratio", "working_capital",
        "dso", "ar_turnover", "cash_conversion", "cf_quality",
    ]
    result = {k: 0 for k in keys}
    result.update({"net_cf_positive": False, "current_ratio_ok": False, "dso_ok": True})
    return result