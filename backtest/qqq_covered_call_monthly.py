import yfinance as yf
import pandas as pd
import math


def backtest_qqq_covered_call_monthly(
    initial_capital=10000.0,
    start_date="2000-01-01",
    end_date="2025-06-09",
    call_strike_pct_above=0.0378,
    premium_per_call_contract=27.0,
):
    """
    Covered call strategy with monthly expirations:
    - Invest all cash into QQQ at first week open.
    - At start of each month, sell call contracts (ceil(shares/100)) at monthly open.
    - Expire/settle at month end based on monthly close.
    Returns TWRR and final portfolio value.
    """
    # Print header
    print(f"Starting Monthly Covered Call Backtest...")
    print(f"Initial Capital: ${initial_capital:,.2f}")
    print(f"Period: {start_date} to {end_date}")
    print(f"Call Strike: Spot + {call_strike_pct_above:.3%}")
    print(f"Premium per Call Contract: ${premium_per_call_contract:.2f}")
    print("-" * 60)

    # Fetch weekly data
    df = yf.Ticker("QQQ").history(start=start_date, end=end_date, interval="1wk")
    if df.empty:
        print("No data fetched.")
        return 0.0, initial_capital
    data = df[["Open", "Close"]].dropna()

    # Determine month boundaries
    data["Month"] = data.index.to_series().dt.month
    data["Year"] = data.index.to_series().dt.year

    # Initialize
    cash = initial_capital
    shares = 0.0
    all_events = []
    current_month = None
    call_strike = None
    calls_sold = 0

    # Buy initial QQQ with all cash on first available open
    first_open = data["Open"].iloc[0]
    if first_open > 0:
        shares = cash / first_open
        cash = 0.0
        all_events.append(
            (
                data.index[0],
                f"[{data.index[0].strftime('%Y-%m-%d')}]: BUY QQQ with cash at ${first_open:.2f}, shares={shares:.2f}, cash=0",
            )
        )
        current_month = data["Month"].iloc[0]

    # Loop through weekly data
    for i in range(len(data)):
        row = data.iloc[i]
        date = data.index[i]
        month = row["Month"]
        year = row["Year"]
        # Month start: sell calls
        if month != current_month:
            # new month begins
            current_month = month
            spot = row["Open"]
            calls_sold = math.ceil(shares / 100)
            premium = calls_sold * premium_per_call_contract
            cash += premium
            call_strike = spot * (1 + call_strike_pct_above)
            all_events.append(
                (
                    date,
                    f"[{date.strftime('%Y-%m-%d')}]: SOLD {calls_sold} Calls at strike ${call_strike:.2f}, premium ${premium:.2f}, cash=${cash:.2f}",
                )
            )
        # Month end detection: look ahead to next row or end
        next_month = data["Month"].iloc[i + 1] if i + 1 < len(data) else None
        if call_strike and (next_month != month):
            # settle at this week's close
            close = row["Close"]
            if close > call_strike:
                # exercised
                intrinsic = (close - call_strike) * calls_sold * 100
                cash -= intrinsic
                # deliver shares
                deliver = min(shares, calls_sold * 100)
                shares -= deliver
                all_events.append(
                    (
                        date,
                        f"[{date.strftime('%Y-%m-%d')}]: CALLS EXERCISED, intrinsic=${intrinsic:.2f}, delivered={deliver:.2f}, remaining_shares={shares:.2f}, cash=${cash:.2f}",
                    )
                )
            else:
                all_events.append(
                    (date, f"[{date.strftime('%Y-%m-%d')}]: CALLS EXPIRE WORTHLESS")
                )
            # reset for next month
            call_strike = None
            calls_sold = 0

    # Final liquidation: sell remaining shares at last close
    last_close = data["Close"].iloc[-1]
    cash += shares * last_close
    all_events.append(
        (
            data.index[-1],
            f"[{data.index[-1].strftime('%Y-%m-%d')}]: LIQUIDATE {shares:.2f} shares at ${last_close:.2f}, cash=${cash:.2f}",
        )
    )
    shares = 0

    # Compute TWRR
    twrr = (cash - initial_capital) / initial_capital

    # Sort events
    all_events.sort(key=lambda x: x[0])
    for _, log in all_events:
        print(log)
    print("-" * 60)
    print(f"Final Capital: ${cash:.2f}")
    print(f"Time-Weighted Return (TWRR): {twrr:.2%}")
    return twrr, cash


if __name__ == "__main__":
    backtest_qqq_covered_call_monthly()
