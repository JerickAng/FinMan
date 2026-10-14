"""
calculations.py
All financial calculation logic for FinMan.

Formulas follow Chapter 2 (Tables 2.2 - 2.5):
  Cash flow & health : OCF, FCF, Net Cash Flow
  Liquidity          : Working Capital, Current, Quick, Cash ratio
  Efficiency         : DSO, AR Turnover, Inventory Turnover, AP Turnover, CCC
  Cash flow quality  : CFO/Net Income, CFO/Current Liabilities,
                       CFO/Total Debt, CFO/Capex

Design rule: a metric is NEVER fabricated. If an input it needs is missing,
the metric is returned as None (templates should render None as "—") and the
matching *_available flag is False.

Income/expenses come from financialRecords (cash-basis). Balance-sheet style
inputs (cash, inventory, payables ...) are supplied by the caller through
`balance_inputs`, a dict with any of these optional keys:
    cash, inventory, accounts_payable, other_current_liabilities,
    total_debt, capex, cogs, purchases, non_cash_expenses,
    change_in_working_capital
Values not supplied fall back to figures derivable from records
(expense_category "Depreciation"/"Amortization" -> non-cash,
 "COGS" -> cogs, "Capex" -> capex) or stay None.
"""

NON_CASH_CATEGORIES = {"depreciation", "amortization", "amortisation"}
DAYS_PER_PERIOD = 30


# ── helpers ───────────────────────────────────────────────────────────────────
def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _num(v):
    """Return float, or None when the input is missing/blank."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _div(a, b, nd=2):
    if a is None or b is None or b == 0:
        return None
    return round(a / b, nd)


def _r(v, nd=2):
    return None if v is None else round(v, nd)


def _sum_expense_category(records, names):
    return sum(_f(r.get("expenses")) for r in records
               if (r.get("expense_category") or "").strip().lower() in names)


# ── main summary ──────────────────────────────────────────────────────────────
def compute_summary(records: list, total_invoices: float = 0,
                    balance_inputs: dict = None) -> dict:
    """
    records         : financialRecords dicts (income, expenses, period, ...)
    total_invoices  : outstanding receivables, sourced from the invoices
                      collection by the caller (= Accounts Receivable)
    balance_inputs  : optional dict, see module docstring
    """
    if not records and not total_invoices:
        return _empty_summary()

    b = balance_inputs or {}

    total_income   = sum(_f(r.get("income"))   for r in records)
    total_expenses = sum(_f(r.get("expenses")) for r in records)

    periods        = {r.get("period") for r in records if r.get("period")}
    days_in_period = max(len(periods), 1) * DAYS_PER_PERIOD

    # ── Inputs (explicit input > derived from records > None) ────────────────
    cash      = _num(b.get("cash"))
    inventory = _num(b.get("inventory"))
    ap        = _num(b.get("accounts_payable"))
    other_cl  = _num(b.get("other_current_liabilities"))
    debt      = _num(b.get("total_debt"))

    capex = _num(b.get("capex"))
    if capex is None:
        capex = _sum_expense_category(records, {"capex"}) or 0.0

    non_cash = _num(b.get("non_cash_expenses"))
    if non_cash is None:
        non_cash = _sum_expense_category(records, NON_CASH_CATEGORIES)

    d_wc = _num(b.get("change_in_working_capital")) or 0.0

    cogs = _num(b.get("cogs"))
    if cogs is None:
        cogs = _sum_expense_category(records, {"cogs", "cost of goods sold"}) or None

    purchases = _num(b.get("purchases"))
    if purchases is None:
        purchases = cogs

    ar = float(total_invoices or 0)

    current_liabilities = None
    if ap is not None or other_cl is not None:
        current_liabilities = (ap or 0) + (other_cl or 0)

    # ── Table 2.2  Cash flow & health ────────────────────────────────────────
    net_cash_flow = total_income - total_expenses
    net_income    = net_cash_flow                       # cash-basis proxy
    # OCF = Net Income + Non-Cash Expenses - Change in Working Capital
    operating_cash_flow = net_income + non_cash - d_wc
    # FCF = OCF - Capex
    free_cash_flow = operating_cash_flow - capex

    # ── Table 2.3  Liquidity ─────────────────────────────────────────────────
    liquidity_available = cash is not None and current_liabilities not in (None, 0)
    current_assets = None
    if cash is not None:
        current_assets = cash + ar + (inventory or 0)

    working_capital = (current_assets - current_liabilities
                       if current_assets is not None and current_liabilities is not None
                       else None)
    current_ratio = _div(current_assets, current_liabilities)
    quick_ratio   = _div(None if current_assets is None
                         else current_assets - (inventory or 0), current_liabilities)
    cash_ratio    = _div(cash, current_liabilities)

    # ── Table 2.4  Efficiency ────────────────────────────────────────────────
    efficiency_available = total_income > 0
    dso         = _r(_div(ar, total_income, 6) * days_in_period, 1) \
                  if efficiency_available else None
    ar_turnover = _div(total_income, ar) if efficiency_available else None

    inventory_turnover = _div(cogs, inventory)
    ap_turnover        = _div(purchases, ap)

    dio = _r(_div(inventory, cogs, 6) * days_in_period, 1) \
          if _div(inventory, cogs, 6) is not None else None
    dpo = _r(_div(ap, purchases, 6) * days_in_period, 1) \
          if _div(ap, purchases, 6) is not None else None

    # CCC = DIO + DSO - DPO  (service firms with no inventory: DIO = 0)
    ccc_available = dso is not None and dpo is not None
    if ccc_available:
        cash_conversion = round((dio or 0) + dso - dpo, 1)
    else:
        cash_conversion = None

    # ── Table 2.5  Cash flow quality ─────────────────────────────────────────
    cfo_to_net_income  = _div(operating_cash_flow, net_income) if net_income > 0 else None
    cfo_to_current_liab = _div(operating_cash_flow, current_liabilities)
    cfo_to_debt        = _div(operating_cash_flow, debt)
    cfo_to_capex       = _div(operating_cash_flow, capex)

    return {
        # Raw totals
        "total_income":   round(total_income, 2),
        "total_expenses": round(total_expenses, 2),
        "total_invoices": round(ar, 2),
        "days_in_period": days_in_period,
        # Cash flow
        "net_cash_flow":       round(net_cash_flow, 2),
        "operating_cash_flow": round(operating_cash_flow, 2),
        "free_cash_flow":      round(free_cash_flow, 2),
        # Liquidity
        "working_capital": _r(working_capital),
        "current_ratio":   current_ratio,
        "quick_ratio":     quick_ratio,
        "cash_ratio":      cash_ratio,
        "liquidity_available": liquidity_available,
        # Efficiency
        "dso":                dso,
        "ar_turnover":        ar_turnover,
        "inventory_turnover": inventory_turnover,
        "ap_turnover":        ap_turnover,
        "cash_conversion":    cash_conversion,
        "efficiency_available": efficiency_available,
        "ccc_available":        ccc_available,
        # Quality
        "cf_quality":          cfo_to_net_income,      # kept for template compat
        "cfo_to_net_income":   cfo_to_net_income,
        "cfo_to_current_liabilities": cfo_to_current_liab,
        "cfo_to_total_debt":   cfo_to_debt,
        "cfo_to_capex":        cfo_to_capex,
        # Colour-coding helpers (thresholds from Tables 2.3 - 2.5)
        "net_cf_positive":  net_cash_flow >= 0,
        "current_ratio_ok": current_ratio is not None and 1.5 <= current_ratio <= 3.0,
        "quick_ratio_ok":   quick_ratio is not None and quick_ratio >= 1.0,
        "cash_ratio_ok":    cash_ratio is not None and 0.5 <= cash_ratio <= 1.0,
        "dso_ok":           dso is not None and dso <= 45,
        "cf_quality_ok":    cfo_to_net_income is not None and cfo_to_net_income >= 1.0,
        "cf_quality_warning": cfo_to_net_income is not None and cfo_to_net_income < 1.0,
    }


# ── monthly series ────────────────────────────────────────────────────────────
def compute_monthly_series(records: list) -> list:
    """Groups records by `period` (YYYY-MM); malformed periods -> "Unknown" (last)."""
    buckets = {}
    for r in records:
        period = (r.get("period") or "").strip() or "Unknown"
        bk = buckets.setdefault(period, {"income": 0.0, "expenses": 0.0})
        bk["income"]   += _f(r.get("income"))
        bk["expenses"] += _f(r.get("expenses"))

    def sort_key(p):
        return (1, "") if p == "Unknown" else (0, p)

    return [{
        "period":        p,
        "income":        round(buckets[p]["income"], 2),
        "expenses":      round(buckets[p]["expenses"], 2),
        "net_cash_flow": round(buckets[p]["income"] - buckets[p]["expenses"], 2),
    } for p in sorted(buckets, key=sort_key)]


# ── profit & loss ─────────────────────────────────────────────────────────────
def compute_profit_loss(records, period_start=None, period_end=None):
    """Profit & loss grouped by category, optional inclusive YYYY-MM range.
    Cash-basis: uses income received and expenses paid."""
    filtered = records
    if period_start:
        filtered = [r for r in filtered if (r.get("period") or "") >= period_start]
    if period_end:
        filtered = [r for r in filtered if (r.get("period") or "") <= period_end]

    rev, exp = {}, {}
    for r in filtered:
        inc = _f(r.get("income"))
        if inc:
            c = r.get("category") or "Uncategorized"
            rev[c] = rev.get(c, 0) + inc
        ex = _f(r.get("expenses"))
        if ex:
            c = r.get("expense_category") or "Uncategorized"
            exp[c] = exp.get(c, 0) + ex

    total_revenue  = sum(rev.values())
    total_expenses = sum(exp.values())
    net_profit     = total_revenue - total_expenses

    return {
        "period_start": period_start,
        "period_end":   period_end,
        "revenue_by_category": sorted([(k, round(v, 2)) for k, v in rev.items()],
                                      key=lambda x: -x[1]),
        "expense_by_category": sorted([(k, round(v, 2)) for k, v in exp.items()],
                                      key=lambda x: -x[1]),
        "total_revenue":       round(total_revenue, 2),
        "total_expenses":      round(total_expenses, 2),
        "net_profit":          round(net_profit, 2),
        "net_profit_positive": net_profit >= 0,
        "net_margin": round(net_profit / total_revenue * 100, 1) if total_revenue else 0,
        "record_count": len(filtered),
    }


def _empty_summary() -> dict:
    zero = ["total_income", "total_expenses", "total_invoices",
            "net_cash_flow", "operating_cash_flow", "free_cash_flow"]
    none = ["working_capital", "current_ratio", "quick_ratio", "cash_ratio",
            "dso", "ar_turnover", "inventory_turnover", "ap_turnover",
            "cash_conversion", "cf_quality", "cfo_to_net_income",
            "cfo_to_current_liabilities", "cfo_to_total_debt", "cfo_to_capex"]
    result = {k: 0 for k in zero}
    result.update({k: None for k in none})
    result.update({
        "days_in_period": DAYS_PER_PERIOD,
        "liquidity_available": False, "efficiency_available": False,
        "ccc_available": False,
        "net_cf_positive": False, "current_ratio_ok": False,
        "quick_ratio_ok": False, "cash_ratio_ok": False,
        "dso_ok": False, "cf_quality_ok": False, "cf_quality_warning": False,
    })
    return result