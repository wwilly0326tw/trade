import yfinance as yf
import pandas as pd
import math


def backtest_qqq_put_income_monthly(
    initial_capital=100000.0,
    start_date="2000-01-01",
    end_date="2025-06-09",
    put_strike_pct_below=0.041,
    premium_per_put_contract=50.0,
):
    """
    Monthly put-selling income strategy:
    - Sell cash-secured puts at start of each month
    - Settlement at month end: exercise or expire
    - If exercised, buy QQQ at strike price using capital
    - Final liquidation of any shares held
    """
    # Header
    print(f"Starting Monthly Put Income Backtest...")
    print(f"Initial Capital: ${initial_capital:,.2f}")
    print(f"Period: {start_date} to {end_date}")
    print(f"Put Strike: Spot - {put_strike_pct_below:.3%}")
    print(f"Premium per Put Contract: ${premium_per_put_contract:.2f}")
    print("-" * 60)

    # Fetch weekly data
    df = yf.Ticker("QQQ").history(start=start_date, end=end_date, interval="1wk")
    if df.empty:
        print("No data fetched.")
        return 0.0, initial_capital
    data = df[["Open", "Close"]].dropna().copy()

    # Month and Year
    data["Month"] = data.index.to_series().dt.month
    data["Year"] = data.index.to_series().dt.year

    capital = initial_capital
    shares = 0.0
    all_events = []
    current_month = data["Month"].iloc[0]
    put_strike = None
    puts_sold = 0

    for i in range(len(data)):
        date = data.index[i]
        row = data.iloc[i]
        month = row["Month"]
        open_price = row["Open"]
        close_price = row["Close"]
        # Month start: sell puts
        if month != current_month:
            current_month = month
            if open_price > 0:
                put_strike = open_price * (1 - put_strike_pct_below)
                puts_sold = math.floor((capital / put_strike) / 100.0)
                if puts_sold > 0:
                    premium = puts_sold * premium_per_put_contract
                    capital += premium
                    all_events.append(
                        (
                            date,
                            f"[{date.strftime('%Y-%m-%d')}]: SOLD {puts_sold} Puts. "
                            f"Strike: ${put_strike:.2f}, Premium: ${premium:.2f}, Capital: ${capital:.2f}",
                        )
                    )
        # Month end settlement
        next_month = data["Month"].iloc[i + 1] if i + 1 < len(data) else None
        if put_strike and (next_month != month):
            if close_price < put_strike:
                # Assigned: buy shares
                shares_assigned = puts_sold * 100
                cost = shares_assigned * put_strike
                capital_before = capital
                capital -= cost
                shares += shares_assigned
                all_events.append(
                    (
                        date,
                        f"[{date.strftime('%Y-%m-%d')}]: PUTS ASSIGNED. "
                        f"Bought {shares_assigned} QQQ at strike ${put_strike:.2f}. "
                        f"Cost: ${cost:.2f}, Capital before: ${capital_before:.2f}, Capital after: ${capital:.2f}, Shares: {shares:.2f}",
                    )
                )
            else:
                all_events.append(
                    (date, f"[{date.strftime('%Y-%m-%d')}]: PUTS EXPIRE WORTHLESS")
                )
            # reset
            put_strike = None
            puts_sold = 0

    # Final liquidation of shares
    last_date = data.index[-1]
    last_close = data["Close"].iloc[-1]
    if shares > 0 and last_close > 0:
        capital_before = capital
        capital += shares * last_close
        all_events.append(
            (
                last_date,
                f"[{last_date.strftime('%Y-%m-%d')}]: LIQUIDATED {shares:.2f} shares at ${last_close:.2f}. "
                f"Capital before: ${capital_before:.2f}, Capital after: ${capital:.2f}",
            )
        )
        shares = 0

    # Calculate TWRR
    twrr = (capital - initial_capital) / initial_capital

    # Print chronological events
    all_events.sort(key=lambda x: x[0])
    for _, log in all_events:
        print(log)
    print("-" * 60)
    print(f"Final Capital: ${capital:.2f}")
    print(f"Time-Weighted Return (TWRR): {twrr:.2%}")
    return twrr, capital


if __name__ == "__main__":
    backtest_qqq_put_income_monthly()
