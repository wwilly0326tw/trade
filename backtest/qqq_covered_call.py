# 回測期間2000至今起始資金為一萬美金
# 直接把所有的資金一次性買入QQQ 然後一併賣出同等數量 delta =0.05的 call 權利金大約是24元一張(根據持有的股數/100計算當週有幾張) strike price會是當前股價的+3.7% 意思就是把當週超過3.7%的漲幅都捨棄掉了 每周結算損益
# 週漲幅10%就all-out賣出 連同該call一起平倉
# Initial Capital: $10,000.00
# Final Portfolio Value: $87,629.21
# Net Profit: $77,629.21
# Time-Weighted Rate of Return (TWRR): 776.29%
import yfinance as yf
import pandas as pd
import numpy as np
import time
import math  # Added math for ceil function


def backtest_qqq_covered_call_strategy(
    initial_capital=10000.0,
    start_date="2025-01-01",
    end_date="2025-06-09",  # Today's date or last available data
    qqq_exit_threshold=0.10,  # Weekly QQQ stock gain > 10% to sell QQQ
    call_strike_percentage_above_spot=0.0378,  # Call strike 4.1% above current QQQ price
    premium_per_call_contract=27.0,  # Weekly premium per call contract
    days_to_expiration=30,  # Days until the sold call options expire
):
    """
    Backtests a QQQ covered call strategy.

    Args:
        initial_capital (float): Initial investment capital.
        start_date (str): Backtest start date (YYYY-MM-DD).
        end_date (str): Backtest end date (YYYY-MM-DD).
        qqq_exit_threshold (float): Weekly QQQ stock return to trigger selling all QQQ.
        call_strike_percentage_above_spot (float): Percentage above current QQQ price to set call strike.
        premium_per_call_contract (float): Fixed premium received per call contract sold.
        days_to_expiration (int): Number of days until sold call options expire.

    Returns:
        tuple: (twrr, final_portfolio_value)
    """

    print(f"Starting QQQ Covered Call Strategy Backtest...")
    print(f"Initial Capital: ${initial_capital:,.2f}")
    print(f"Period: {start_date} to {end_date}")
    print(f"QQQ Exit Condition: Weekly QQQ stock gain > {qqq_exit_threshold:.2%}")
    print(f"Call Strike: Spot + {call_strike_percentage_above_spot:.3%}")
    print(f"Weekly Premium per Call Contract: ${premium_per_call_contract:,.2f}")
    print(f"Days to Expiration for Calls: {days_to_expiration} days")
    print("-" * 70)
    time.sleep(1)
    # Fetch QQQ weekly data
    qqq_hist = yf.Ticker("QQQ").history(start=start_date, end=end_date, interval="1wk")

    if qqq_hist.empty:
        print("Error: Could not fetch QQQ historical data.")
        return 0.0, initial_capital

    # Prepare data: Use Open for transactions, Close for return/settlement calculations
    qqq_data = qqq_hist[["Open", "Close"]].copy()
    qqq_data.dropna(inplace=True)

    if len(qqq_data) < 2:
        print("Error: Not enough data for the backtest.")
        return 0.0, initial_capital

    # Calculate QQQ's own weekly stock return (Close-to-Close)
    # This is used for the QQQ exit condition
    qqq_data["QQQ_Weekly_Return"] = qqq_data["Close"].pct_change()

    # Prepare month column for monthly option selling
    qqq_data["Month"] = qqq_data.index.to_series().dt.month
    # prev_month = qqq_data["Month"].iloc[0] if not qqq_data.empty else None # Old logic
    month_last_call_sell_attempt = 0  # Initialize to ensure first month is processed

    # Initialize portfolio variables
    cash = initial_capital
    qqq_shares_held = 0.0
    portfolio_value_over_time = []  # For potential future plotting or detailed analysis
    trade_log = []
    open_call_positions = []  # To store details of open call options

    # Loop through each week, starting from the first week where we can make a decision
    for i in range(len(qqq_data)):
        current_week_date = qqq_data.index[i]
        current_week_open = qqq_data["Open"].iloc[i]
        current_week_close = qqq_data["Close"].iloc[i]
        qqq_stock_return_this_week = qqq_data["QQQ_Weekly_Return"].iloc[i]
        current_month = qqq_data["Month"].iloc[i]

        # --- 0. DTE-Based Call Settlement ---
        remaining_open_calls = []
        for position in open_call_positions:
            # Corrected condition: Settle if expiration occurs before the next week's data point
            if position["expiration_date"] < (
                current_week_date + pd.Timedelta(weeks=1)
            ):
                # Position is expiring or has expired this week
                if current_week_close > position["strike_price"]:
                    intrinsic_value_per_share = (
                        current_week_close - position["strike_price"]
                    )
                    shares_obligated_by_contracts = position["contracts"] * 100

                    actual_shares_physically_removed = min(
                        qqq_shares_held, shares_obligated_by_contracts
                    )
                    qqq_shares_held_before_assignment = qqq_shares_held

                    cash_from_delivered_shares = (
                        actual_shares_physically_removed * position["strike_price"]
                    )
                    cash += cash_from_delivered_shares

                    naked_shares_count = (
                        shares_obligated_by_contracts - actual_shares_physically_removed
                    )
                    cost_naked_settlement = 0.0
                    if naked_shares_count > 0:
                        cost_naked_settlement = (
                            naked_shares_count * intrinsic_value_per_share
                        )
                        cash -= (
                            cost_naked_settlement  # Buy shares at market or cash settle
                        )

                    qqq_shares_held -= actual_shares_physically_removed

                    trade_log.append(
                        f"[{current_week_date.strftime('%Y-%m-%d')}]: CALLS EXERCISED (Expired {position['expiration_date'].strftime('%Y-%m-%d')}, Sold {position['sell_date'].strftime('%Y-%m-%d')}). "
                        f"Strike: ${position['strike_price']:.2f}. "
                        f"{actual_shares_physically_removed:.2f} QQQ delivered (was {qqq_shares_held_before_assignment:.2f}). "
                        f"Cash from delivered: ${cash_from_delivered_shares:,.2f}. "
                        f"Naked settled: {naked_shares_count:.2f} shares, Cost: ${cost_naked_settlement:,.2f}. "
                        f"New QQQ: {qqq_shares_held:,.2f}. Cash: ${cash:,.2f}"
                    )
                else:
                    trade_log.append(
                        f"[{current_week_date.strftime('%Y-%m-%d')}]: CALLS EXPIRED WORTHLESS. Strike: ${position['strike_price']:.2f} (Expired {position['expiration_date'].strftime('%Y-%m-%d')}, Sold {position['sell_date'].strftime('%Y-%m-%d')})"
                    )
                # This position is now settled
            else:
                remaining_open_calls.append(position)  # Keep non-expired positions
        open_call_positions = remaining_open_calls

        # --- 1. Continuously Reinvest cash into QQQ ---
        if cash > 0 and current_week_open > 0:
            cash_before_buy = cash
            shares_to_buy = cash / current_week_open
            qqq_shares_held += shares_to_buy
            cash = 0  # All cash invested
            trade_log.append(
                f"[{current_week_date.strftime('%Y-%m-%d')}]: BUY QQQ with cash at ${current_week_open:,.2f}. "
                f"Shares bought: {shares_to_buy:,.2f}, New total QQQ shares: {qqq_shares_held:,.2f}. "
                f"Cash before buy: ${cash_before_buy:,.2f}, Cash after buy: ${cash:,.2f}"
            )

        # --- 2. Covered Call Logic (Monthly Selling, if QQQ shares are held) ---
        if (
            qqq_shares_held > 0
            and current_week_open > 0
            # and prev_month is not None # Old logic
            # and current_month != prev_month # Old logic
            and current_month != month_last_call_sell_attempt
        ):
            # prev_month = current_month # Mark that we've processed this month's start for call selling # Old logic

            spot_price_for_call_sell = (
                current_week_open  # Use current week's open to decide strike
            )
            num_calls_to_sell = math.ceil(qqq_shares_held / 100.0)

            if num_calls_to_sell > 0:
                premium_for_new_calls = num_calls_to_sell * premium_per_call_contract
                cash += premium_for_new_calls

                strike_price_for_new_calls = spot_price_for_call_sell * (
                    1 + call_strike_percentage_above_spot
                )
                expiration_date_calc = current_week_date + pd.Timedelta(
                    days=days_to_expiration
                )

                open_call_positions.append(
                    {
                        "sell_date": current_week_date,
                        "expiration_date": expiration_date_calc,
                        "strike_price": strike_price_for_new_calls,
                        "contracts": num_calls_to_sell,
                        "premium_total": premium_for_new_calls,
                    }
                )

                trade_log.append(
                    f"[{current_week_date.strftime('%Y-%m-%d')}]: SOLD {num_calls_to_sell:.0f} Calls. Strike: ${strike_price_for_new_calls:,.2f}, Premium: ${premium_for_new_calls:,.2f}, Expires: {expiration_date_calc.strftime('%Y-%m-%d')}. Cash: ${cash:,.2f}"
                )
            # Update the month we last attempted to sell calls, regardless of success.
            month_last_call_sell_attempt = current_month
        # elif prev_month is not None and current_month != prev_month: # This entire elif block is no longer needed
        #     prev_month = current_month

        # --- Old Weekly Call Logic Removed ---
        # premium_received_this_week = 0
        # active_call_strike = None
        # total_payout = 0
        # num_calls_sold_this_week = 0
        # ... (original weekly call selling and settlement logic deleted here) ...

        # --- 3. QQQ Exit Condition (based on QQQ's own stock return for the week) ---
        # This check happens *after* call settlement for the current week.
        # The decision to sell QQQ is for the *next* week's open.
        if (
            qqq_shares_held > 0
            and not pd.isna(qqq_stock_return_this_week)
            and qqq_stock_return_this_week > qqq_exit_threshold
        ):
            # Determine next week's open price for selling QQQ
            if (i + 1) < len(qqq_data):
                sell_price_qqq = qqq_data["Open"].iloc[i + 1]
                if sell_price_qqq > 0:
                    cash += qqq_shares_held * sell_price_qqq
                    trade_log.append(
                        f"[{qqq_data.index[i+1].strftime('%Y-%m-%d')}]: ALL OUT QQQ at ${sell_price_qqq:,.2f}. Shares: {qqq_shares_held:,.2f}, Cash: ${cash:,.2f}"
                    )
                    qqq_shares_held = 0
                    # Strategy concludes here as per prompt (no re-entry specified)
                    # To make it continue (e.g. go back to selling puts or other strategy), add logic here.
                    # For now, we break the loop as the primary QQQ holding phase is over.
                    # Update portfolio value for the week of exit before breaking
                    portfolio_value = cash  # Since shares are now 0
                    portfolio_value_over_time.append(
                        {
                            "Date": qqq_data.index[i + 1],
                            "PortfolioValue": portfolio_value,
                        }
                    )
                    break  # Exit the main loop
                else:
                    trade_log.append(
                        f"[{qqq_data.index[i+1].strftime('%Y-%m-%d')}]: QQQ EXIT signal, but next week open is invalid. Holding QQQ."
                    )
            else:
                # Last week of data, QQQ exit triggered, sell at current week's close
                cash += qqq_shares_held * current_week_close
                trade_log.append(
                    f"[{current_week_date.strftime('%Y-%m-%d')}]: ALL OUT QQQ (End of Data) at ${current_week_close:,.2f}. Shares: {qqq_shares_held:,.2f}, Cash: ${cash:,.2f}"
                )
                qqq_shares_held = 0
                # Update portfolio value for this final week
                portfolio_value = cash
                portfolio_value_over_time.append(
                    {"Date": current_week_date, "PortfolioValue": portfolio_value}
                )
                break  # Exit the main loop

        # --- 4. Calculate Portfolio Value at the end of the week ---
        portfolio_value = (qqq_shares_held * current_week_close) + cash
        portfolio_value_over_time.append(
            {"Date": current_week_date, "PortfolioValue": portfolio_value}
        )

    # --- 5. Final Results ---
    final_portfolio_value = (
        portfolio_value_over_time[-1]["PortfolioValue"]
        if portfolio_value_over_time
        else initial_capital
    )

    if initial_capital == 0:
        twrr = 0.0 if final_portfolio_value == 0 else float("inf")
    else:
        twrr = (final_portfolio_value - initial_capital) / initial_capital

    print("-" * 70)
    print("Backtest Finished.")
    for log_entry in trade_log:
        print(log_entry)
    print("-" * 70)
    print(f"Initial Capital: ${initial_capital:,.2f}")
    print(f"Final Portfolio Value: ${final_portfolio_value:,.2f}")
    print(f"Net Profit: ${(final_portfolio_value - initial_capital):,.2f}")
    print(
        f"Time-Weighted Rate of Return (TWRR): {twrr:.2%}"
    )  # More accurately, this is a simple total return.
    print("-" * 70)

    return twrr, final_portfolio_value


if __name__ == "__main__":
    # Run the backtest
    final_twrr, final_value = backtest_qqq_covered_call_strategy(
        initial_capital=100000.0,
        start_date="2025-01-01",
        end_date="2025-06-09",
        qqq_exit_threshold=0.90,
        call_strike_percentage_above_spot=0.037,
        premium_per_call_contract=23.0,
        days_to_expiration=45,  # Example: 45 DTE
    )

# initial_capital=100000.0,
# start_date="2020-01-01",
# end_date="2025-06-09",
# qqq_exit_threshold=0.90,
# call_strike_percentage_above_spot=0.037,
# premium_per_call_contract=23.0,
# Initial Capital: $100,000.00
# Final Portfolio Value: $191,191.81
# Net Profit: $91,191.81
# Time-Weighted Rate of Return (TWRR): 91.19%
