"""
forecasting.py
Cash flow forecasting for FinMan. Called by the /api/forecast route in app.py.

Method
------
1. Records are aggregated into MONTHLY net cash flow (income - expenses).
   Records with a missing/invalid `period` are skipped and counted, never
   given an invented date.
2. Model choice depends on how much history exists:
     < 3 months  -> no forecast (not enough data)
     3-5 months  -> linear trend (least squares) with an 80% interval
     6+ months   -> Prophet (80% interval), falling back to linear trend
                    if Prophet is not installed or fails
3. Back-test: the most recent months are held out, forecast from the
   earlier months, and compared with what actually happened. The model is
   scored against a naive baseline (mean of the last 3 months) using MAE
   and WAPE, so the CFO can see whether the model beats a simple average.
4. Known receivables: unpaid invoices are summed by due month and returned
   as supporting information. They are NOT added to the statistical
   forecast, because historical income already contains past collections
   and adding them would double count.

Horizon: 30 / 60 / 90 days -> 1 / 2 / 3 monthly projection points.
"""
from datetime import datetime
from flask import jsonify

MIN_MONTHS = 3
PROPHET_MIN_MONTHS = 6
Z_80 = 1.2816          # z-score for an 80% interval (matches Prophet's interval_width=0.8)


# ── month helpers ─────────────────────────────────────────────────────────────
def _ym_index(period):
    """'YYYY-MM' -> integer month index, or None if malformed."""
    try:
        d = datetime.strptime(str(period or "").strip(), "%Y-%m")
    except ValueError:
        return None
    return d.year * 12 + d.month - 1


def _idx_to_period(idx):
    y, m0 = divmod(idx, 12)
    return f"{y:04d}-{m0 + 1:02d}"


def _monthly_series(records):
    """Aggregate records into [(month_index, net_cash_flow)], plus skipped count."""
    buckets, skipped = {}, 0
    for r in records:
        idx = _ym_index(r.get("period"))
        if idx is None:
            skipped += 1
            continue
        net = float(r.get("income", 0) or 0) - float(r.get("expenses", 0) or 0)
        buckets[idx] = buckets.get(idx, 0.0) + net
    return sorted(buckets.items()), skipped


# ── forecasting methods: each returns [(yhat, lower, upper), ...] per target ──
def _trend(train, targets):
    xs = [i for i, _ in train]
    ys = [y for _, y in train]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0.0
    intercept = my - slope * mx
    resid = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    sd = (sum(e * e for e in resid) / max(n - 2, 1)) ** 0.5
    out = []
    for t in targets:
        yhat = intercept + slope * t
        out.append((yhat, yhat - Z_80 * sd, yhat + Z_80 * sd))
    return out


def _moving_average(train, targets):
    """Baseline: flat forecast at the mean of the last 3 observed months."""
    recent = [y for _, y in train][-3:]
    m = sum(recent) / len(recent)
    return [(m, m, m) for _ in targets]


def _prophet(train, targets):
    import logging
    import pandas as pd
    from prophet import Prophet
    logging.getLogger("cmdstanpy").setLevel(logging.WARNING)
    logging.getLogger("prophet").setLevel(logging.WARNING)

    def ts(i):
        return pd.Timestamp(year=i // 12, month=i % 12 + 1, day=1)

    df = pd.DataFrame({"ds": [ts(i) for i, _ in train], "y": [y for _, y in train]})
    model = Prophet(
        yearly_seasonality=False, weekly_seasonality=False, daily_seasonality=False,
        changepoint_prior_scale=0.05, interval_width=0.8,
    )
    model.fit(df)
    pred = model.predict(pd.DataFrame({"ds": [ts(t) for t in targets]}))
    return list(zip(pred["yhat"], pred["yhat_lower"], pred["yhat_upper"]))


def _predict(method, train, targets):
    return _prophet(train, targets) if method == "prophet" else _trend(train, targets)


def _choose_method(n_months):
    notes = []
    if n_months < PROPHET_MIN_MONTHS:
        notes.append(f"Only {n_months} months of data: a linear trend was used. "
                     f"Prophet is used once {PROPHET_MIN_MONTHS}+ months are available.")
        return "trend", notes
    try:
        import prophet  # noqa: F401
        return "prophet", notes
    except ImportError:
        notes.append("Prophet is not installed on the server, so a linear trend was used.")
        return "trend", notes


# ── back-test ─────────────────────────────────────────────────────────────────
def _backtest(series, method):
    n = len(series)
    k = min(3, n // 3)
    if n < PROPHET_MIN_MONTHS or k < 1:
        return None
    train, test = series[:-k], series[-k:]
    targets = [i for i, _ in test]
    actual = [y for _, y in test]

    def score(preds):
        errs = [abs(a - p) for a, p in zip(actual, preds)]
        denom = sum(abs(a) for a in actual)
        return {"mae": round(sum(errs) / k, 2),
                "wape": round(sum(errs) / denom * 100, 1) if denom else None}

    try:
        model_preds = [p[0] for p in _predict(method, train, targets)]
    except Exception:
        return None
    base_preds = [p[0] for p in _moving_average(train, targets)]
    model, base = score(model_preds), score(base_preds)
    return {"months_held_out": k, "model": model, "baseline": base,
            "beats_baseline": model["mae"] < base["mae"]}


# ── known receivables ─────────────────────────────────────────────────────────
def _receivables_due(invoices, targets):
    if not invoices:
        return None
    first = targets[0]
    earlier = 0.0
    buckets = {t: [0.0, 0] for t in targets}
    for inv in invoices:
        amt = float(inv.get("amount") or inv.get("total") or 0)
        raw, idx = str(inv.get("due_date") or ""), None
        for fmt in ("%Y-%m-%d", "%Y-%m"):
            try:
                d = datetime.strptime(raw, fmt)
                idx = d.year * 12 + d.month - 1
                break
            except ValueError:
                continue
        if idx is None:
            continue
        if idx < first:
            earlier += amt
        elif idx in buckets:
            buckets[idx][0] += amt
            buckets[idx][1] += 1
    return {
        "earlier": round(earlier, 2),
        "by_month": [{"date": _idx_to_period(t), "amount": round(a, 2), "count": c}
                     for t, (a, c) in buckets.items()],
    }


# ── public entry point ────────────────────────────────────────────────────────
def get_forecast(records: list, horizon: int = 30, invoices: list = None):
    """
    records : financialRecords dicts (income, expenses, period)
    horizon : 30 / 60 / 90 (days) -> 1 / 2 / 3 monthly points
    invoices: optional unpaid invoices (amount/total, due_date) for the
              known-receivables panel
    """
    try:
        series, skipped = _monthly_series(records)
        n = len(series)
        if n < MIN_MONTHS:
            return jsonify({
                "error": "Not enough data",
                "message": f"Forecasting needs at least {MIN_MONTHS} different months of data "
                           f"(you have {n}). Please enter more records.",
            }), 400

        horizon_months = max(1, min(3, -(-int(horizon) // 30)))
        last_idx = series[-1][0]
        targets = [last_idx + i for i in range(1, horizon_months + 1)]

        method, notes = _choose_method(n)
        try:
            preds = _predict(method, series, targets)
        except Exception:
            if method != "prophet":
                raise
            method = "trend"
            notes.append("Prophet could not fit this data, so a linear trend was used.")
            preds = _predict(method, series, targets)

        if skipped:
            notes.append(f"{skipped} record(s) without a valid month were left out.")

        projected = [{"date": _idx_to_period(t), "value": round(p[0], 2),
                      "lower": round(p[1], 2), "upper": round(p[2], 2)}
                     for t, p in zip(targets, preds)]

        return jsonify({
            "method": method,
            "method_label": "Prophet time-series model" if method == "prophet"
                            else "Linear trend",
            "interval": "80%",
            "months_of_data": n,
            "horizon": horizon,
            "actual": [{"date": _idx_to_period(i), "value": round(y, 2)} for i, y in series],
            "projected": projected,
            "total_projected": round(sum(p["value"] for p in projected), 2),
            "backtest": _backtest(series, method),
            "receivables_due": _receivables_due(invoices, targets),
            "notes": notes,
        })

    except Exception as e:
        return jsonify({"error": "forecast_failed", "message": f"Forecast failed: {e}"}), 500