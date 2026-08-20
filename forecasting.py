"""
forecasting.py
Cash flow forecasting using Prophet.
Called by the /api/forecast route in app.py.
"""
from flask import jsonify
from datetime import datetime, timedelta
import json

def get_forecast(records: list, horizon: int = 30):
    """
    Takes financial records and a forecast horizon (30/60/90 days).
    Returns a JSON response with actual and projected cash flow values.
    """
    try:
        from prophet import Prophet
        import pandas as pd

        if len(records) < 3:
            return jsonify({
                "error": "Not enough data",
                "message": "Please enter at least 3 months of financial data to generate a forecast."
            }), 400

        # Build dataframe Prophet expects: ds (date), y (value)
        rows = []
        for i, r in enumerate(records):
            net = float(r.get("income", 0) or 0) - float(r.get("expenses", 0) or 0)
            # Use period field if available, otherwise generate monthly dates
            period = r.get("period", "")
            try:
                date = datetime.strptime(period, "%Y-%m")
            except Exception:
                date = datetime.today().replace(day=1) - timedelta(days=30 * (len(records) - i))
            rows.append({"ds": date, "y": net})

        df = pd.DataFrame(rows).sort_values("ds").reset_index(drop=True)

        model = Prophet(
            yearly_seasonality=False,
            weekly_seasonality=False,
            daily_seasonality=False,
            changepoint_prior_scale=0.05,
        )
        model.fit(df)

        future   = model.make_future_dataframe(periods=horizon, freq="D")
        forecast = model.predict(future)

        # Actual data points
        actual = [
            {"date": row["ds"].strftime("%Y-%m-%d"), "value": round(row["y"], 2)}
            for _, row in df.iterrows()
        ]

        # Projected data points (future only)
        cutoff    = df["ds"].max()
        projected = [
            {
                "date":  row["ds"].strftime("%Y-%m-%d"),
                "value": round(row["yhat"], 2),
                "upper": round(row["yhat_upper"], 2),
                "lower": round(row["yhat_lower"], 2),
            }
            for _, row in forecast.iterrows()
            if row["ds"] > cutoff
        ]

        return jsonify({"actual": actual, "projected": projected, "horizon": horizon})

    except ImportError:
        # Prophet not installed — return mock data so UI still works during dev
        return _mock_forecast(horizon)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _mock_forecast(horizon: int):
    """Returns mock forecast data when Prophet is not yet installed."""
    today  = datetime.today()
    actual = [
        {"date": (today - timedelta(days=30*i)).strftime("%Y-%m-%d"),
         "value": 15000 + (i * 3000)}
        for i in range(5, 0, -1)
    ]
    projected = [
        {"date": (today + timedelta(days=i*10)).strftime("%Y-%m-%d"),
         "value": 34000 + (i * 2000),
         "upper": 36000 + (i * 2000),
         "lower": 32000 + (i * 2000)}
        for i in range(1, (horizon // 10) + 2)
    ]
    return jsonify({"actual": actual, "projected": projected,
                    "horizon": horizon, "mock": True})
