# Starting QQQ backtest with put income strategy...
# Initial Capital: $10,000.00
# Period: 2000-01-01 to 2025-06-09
# QQQ Entry: Weekly drop < -6.00%
# QQQ Exit: Weekly rebound > 10.00%
# Weekly Income Rate (when not in QQQ): 0.500%

# Backtest Finished.
# Total Trades: 7
# Initial Capital: $10,000.00
# Final Capital: $110,412.77
# Net Profit: $100,412.77
# Time-Weighted Rate of Return (TWRR): 1004.13%

import yfinance as yf
import pandas as pd
import math


def backtest_qqq_put_income_strategy(
    initial_capital=10000.0,
    start_date="2000-01-01",
    end_date="2025-06-09",
    entry_drop_threshold=-0.06,
    exit_rebound_threshold=0.10,
    premium_per_put_contract=303.0,  # Premium received for selling one put contract
    put_strike_percentage_below_spot=0.06,  # e.g., 0.041 for 4.1% below spot
    days_to_expiration=31,  # Days until the sold put options expire
):
    """
    Backtests a QQQ trading strategy that includes generating income from selling puts
    when not invested in QQQ. If puts are assigned, QQQ is bought at the strike price.

    Args:
        initial_capital (float): Initial investment capital.
        start_date (str): Backtest start date (YYYY-MM-DD).
        end_date (str): Backtest end date (YYYY-MM-DD).
        entry_drop_threshold (float): Weekly return threshold to enter QQQ (e.g., -0.06).
        exit_rebound_threshold (float): Weekly return threshold to exit QQQ (e.g., 0.10).
        premium_per_put_contract (float): Premium received per put contract sold.
        put_strike_percentage_below_spot (float): Percentage below current price for put strike.
        days_to_expiration (int): Number of days until sold put options expire.

    Returns:
        tuple: (twrr, final_capital, trade_log, put_sell_log)
    """

    print(f"Starting QQQ backtest with put income strategy...")
    print(f"Initial Capital: ${initial_capital:,.2f}")
    print(f"Period: {start_date} to {end_date}")
    print(f"QQQ Entry (Market Drop): Weekly drop < {entry_drop_threshold:.2%}")
    print(f"QQQ Exit (Market Rebound): Weekly rebound > {exit_rebound_threshold:.2%}")
    print(
        f"Premium per Put Contract (when not in QQQ): ${premium_per_put_contract:.2f}"
    )
    print(f"Put Strike Percentage Below Spot: {put_strike_percentage_below_spot:.3%}")
    print(f"Days to Expiration for Puts: {days_to_expiration} days")
    print("-" * 60)

    # Fetch QQQ weekly data
    qqq_ticker = yf.Ticker("QQQ")
    qqq_data = qqq_ticker.history(start=start_date, end=end_date, interval="1wk")

    if qqq_data.empty:
        print("Error: No QQQ data fetched. Check date range or ticker.")
        return 0.0, initial_capital, [], []

    qqq_data["WeeklyReturn"] = qqq_data["Close"].pct_change()
    qqq_data = qqq_data.dropna()  # Remove rows with NaN returns (first row)

    if qqq_data.empty:
        print("Error: Not enough QQQ data after processing for backtest.")
        return 0.0, initial_capital, [], []

    # Prepare month columns for monthly settlement
    qqq_data["Month"] = qqq_data.index.to_series().dt.month
    # prev_month = qqq_data["Month"].iloc[0] if not qqq_data.empty else None # Removed
    open_put_positions = []

    # Initialize backtest variables
    capital = initial_capital
    shares = 0.0
    in_qqq_position = False
    weeks_in_trade = 0
    trade_count = 0
    all_events_log = []
    month_last_option_sell_attempt = (
        0  # Added for correct monthly logic    # Loop through each week
    )
    for i in range(len(qqq_data)):
        current_week_data = qqq_data.iloc[i]
        current_week_date = qqq_data.index[i]
        current_month = qqq_data["Month"].iloc[i]
        newly_assigned_this_week = False
        put_settled_this_week = False  # 任何PUT結算（到期或執行）都會設為True

        can_trade_next_week = i + 1 < len(qqq_data)
        next_week_open_price = (
            qqq_data["Open"].iloc[i + 1] if can_trade_next_week else None
        )  # --- DTE-Based Put Settlement ---
        remaining_open_positions = []
        put_just_expired = False
        expired_put_date = None
        for position in open_put_positions:
            # PUT在到期日當週或之後才結算
            if position["expiration_date"] <= current_week_date:
                event_date_for_log = position["expiration_date"]
                put_settled_this_week = True  # 任何PUT結算都設為True
                if current_week_data["Close"] < position["strike_price"]:
                    shares_assigned = position["contracts"] * 100
                    cost = shares_assigned * position["strike_price"]
                    capital_before_assignment = capital
                    capital -= cost
                    shares += shares_assigned
                    in_qqq_position = True
                    newly_assigned_this_week = True
                    trade_count += 1
                    all_events_log.append(
                        {
                            "date": event_date_for_log,
                            "log": f"[{event_date_for_log.strftime('%Y-%m-%d')}]: PUT ASSIGNED. Bought {shares_assigned} QQQ at strike ${position['strike_price']:.2f} (Sold on {position['sell_date'].strftime('%Y-%m-%d')}, Originally Expired {event_date_for_log.strftime('%Y-%m-%d')}). Cost: ${cost:.2f}. Capital: ${capital:.2f}. Shares: {shares:.2f}",
                        }
                    )
                else:
                    put_just_expired = True
                    expired_put_date = event_date_for_log
                    all_events_log.append(
                        {
                            "date": event_date_for_log,
                            "log": f"[{event_date_for_log.strftime('%Y-%m-%d')}]: PUTS EXPIRED WORTHLESS. Strike: ${position['strike_price']:.2f} (Sold on {position['sell_date'].strftime('%Y-%m-%d')}, Originally Expired {event_date_for_log.strftime('%Y-%m-%d')})",
                        }
                    )
            else:
                remaining_open_positions.append(position)
        open_put_positions = remaining_open_positions

        if newly_assigned_this_week:
            weeks_in_trade = 0

        # --- Put Selling (PUT到期失效當天立即賣新PUT) ---
        if put_just_expired and not in_qqq_position and expired_put_date:
            current_spot_price = current_week_data["Close"]
            if current_spot_price > 0.01:
                strike_price_for_new_puts = current_spot_price * (
                    1 - put_strike_percentage_below_spot
                )
                if strike_price_for_new_puts > 0.01:
                    max_contracts = math.floor(
                        capital / (strike_price_for_new_puts * 100.0)
                    )
                    if max_contracts > 0:
                        premium_received = max_contracts * premium_per_put_contract
                        capital += premium_received
                        expiration_date_calc = expired_put_date + pd.Timedelta(
                            days=days_to_expiration
                        )
                        open_put_positions.append(
                            {
                                "sell_date": expired_put_date,
                                "expiration_date": expiration_date_calc,
                                "strike_price": strike_price_for_new_puts,
                                "contracts": max_contracts,
                                "premium_total": premium_received,
                            }
                        )
                        all_events_log.append(
                            {
                                "date": expired_put_date,
                                "log": f"[{expired_put_date.strftime('%Y-%m-%d')}]: SOLD {max_contracts} Puts (SAME DAY after expiry). Strike: ${strike_price_for_new_puts:.2f}, Premium: ${premium_received:.2f}, Expires: {expiration_date_calc.strftime('%Y-%m-%d')}. Capital: ${capital:.2f}",
                            }
                        )

        # --- 正常PUT賣出邏輯（沒有未到期PUT且沒持有QQQ時）---
        elif (
            not in_qqq_position
            and len(open_put_positions) == 0
            and not newly_assigned_this_week  # 只有PUT被執行時才阻止賣新PUT
        ):
            current_spot_price = current_week_data["Open"]
            if current_spot_price > 0.01:
                strike_price_for_new_puts = current_spot_price * (
                    1 - put_strike_percentage_below_spot
                )
                if strike_price_for_new_puts > 0.01:
                    max_contracts = math.floor(
                        capital / (strike_price_for_new_puts * 100.0)
                    )
                    if max_contracts > 0:
                        premium_received = max_contracts * premium_per_put_contract
                        capital += premium_received
                        expiration_date_calc = current_week_date + pd.Timedelta(
                            days=days_to_expiration
                        )
                        open_put_positions.append(
                            {
                                "sell_date": current_week_date,
                                "expiration_date": expiration_date_calc,
                                "strike_price": strike_price_for_new_puts,
                                "contracts": max_contracts,
                                "premium_total": premium_received,
                            }
                        )
                        all_events_log.append(
                            {
                                "date": current_week_date,
                                "log": f"[{current_week_date.strftime('%Y-%m-%d')}]: SOLD {max_contracts} Puts. Strike: ${strike_price_for_new_puts:.2f}, Premium: ${premium_received:.2f}, Expires: {expiration_date_calc.strftime('%Y-%m-%d')}. Capital: ${capital:.2f}",
                            }
                        )
        # ...existing code...

        # --- Trading Logic (Modified for same-day execution) ---
        if not in_qqq_position:
            if current_week_data["WeeklyReturn"] < entry_drop_threshold:
                # Enter QQQ position at current week's close price
                current_close_price = current_week_data["Close"]
                if current_close_price > 0 and capital > 0:
                    shares_to_buy = capital / current_close_price
                    if shares_to_buy > 0:
                        shares = shares_to_buy
                        capital_before_buy = capital
                        capital = 0.0  # All capital invested
                        in_qqq_position = True
                        trade_count += 1
                        weeks_in_trade = 0
                        log_entry = (
                            f"[{current_week_date.strftime('%Y-%m-%d')} Market Drop]: ENTER QQQ. Buy {shares:,.2f} shares at ${current_close_price:,.2f}. "
                            f"Capital before: ${capital_before_buy:,.2f}, Capital after: ${capital:.2f}"
                        )
                        all_events_log.append(
                            {"date": current_week_date, "log": log_entry}
                        )
        elif in_qqq_position:  # If in QQQ
            weeks_in_trade += 1
            if current_week_data["WeeklyReturn"] > exit_rebound_threshold:
                # Exit QQQ position at current week's close price
                current_close_price = current_week_data["Close"]
                if current_close_price > 0:
                    capital_before_sell = capital  # Should be 0 if all in
                    proceeds = shares * current_close_price
                    capital += proceeds
                    shares_sold = shares
                    shares = 0.0
                    in_qqq_position = False
                    log_entry = (
                        f"[{current_week_date.strftime('%Y-%m-%d')} Market Rebound]: EXIT QQQ. Sell {shares_sold:,.2f} shares at ${current_close_price:,.2f}. "
                        f"Proceeds: ${proceeds:,.2f}. Capital after: ${capital:,.2f}. Weeks in trade: {weeks_in_trade}"
                    )
                    all_events_log.append({"date": current_week_date, "log": log_entry})
            # else: Hold QQQ position or cannot trade next week

        # Log portfolio value at the end of each week for analysis (optional)
        current_portfolio_value = capital + (shares * current_week_data["Close"])
        # portfolio_value_over_time.append(current_portfolio_value) # If you want to track this

    # If still in position at the end of the backtest, liquidate
    if in_qqq_position and not qqq_data.empty:
        final_close_price = qqq_data["Close"].iloc[-1]
        final_event_date = qqq_data.index[-1]
        if final_close_price > 0:
            capital += shares * final_close_price
            log_entry = (
                f"[{final_event_date.strftime('%Y-%m-%d')} End of Backtest]: LIQUIDATE. Sold {shares:,.2f} QQQ at ${final_close_price:,.2f}. "
                f"Capital: ${capital:,.2f}"
            )
            all_events_log.append({"date": final_event_date, "log": log_entry})
            shares = 0.0
            in_qqq_position = False

    # Final TWRR Calculation
    if initial_capital == 0:
        twrr = 0.0 if capital == 0 else float("inf")
    else:
        twrr = (capital - initial_capital) / initial_capital

    # Sort all events by date
    all_events_log.sort(key=lambda x: x["date"])

    return twrr, capital, all_events_log


if __name__ == "__main__":
    # Run the backtest with specified parameters
    start_date = "2024-01-01"
    end_date = "2025-12-31"
    test_data = [
        {
            "initial_capital": 100000.0,
            "start_date": start_date,
            "end_date": end_date,
            "entry_drop_threshold": -0.16,
            "exit_rebound_threshold": 0.10,
            "premium_per_put_contract": 292.0,
            "put_strike_percentage_below_spot": 0.06,
            "days_to_expiration": 30,
        },
        # {
        #     "initial_capital": 100000.0,
        #     "start_date": start_date,
        #     "end_date": end_date,
        #     "entry_drop_threshold": -0.07,
        #     "exit_rebound_threshold": 0.09,
        #     "premium_per_put_contract": 246.0,
        #     "put_strike_percentage_below_spot": 0.07,
        #     "days_to_expiration": 30,
        # },
        # {
        #     "initial_capital": 100000.0,
        #     "start_date": start_date,
        #     "end_date": end_date,
        #     "entry_drop_threshold": -0.05,
        #     "exit_rebound_threshold": 0.09,
        #     "premium_per_put_contract": 312.0,
        #     "put_strike_percentage_below_spot": 0.05,
        #     "days_to_expiration": 30,
        # },
        # {
        #     "initial_capital": 100000.0,
        #     "start_date": start_date,
        #     "end_date": end_date,
        #     "entry_drop_threshold": -0.06,
        #     "exit_rebound_threshold": 0.09,
        #     "premium_per_put_contract": 303.0,
        #     "put_strike_percentage_below_spot": 0.06,
        #     "days_to_expiration": 7,
        # },
        # {
        #     "initial_capital": 100000.0,
        #     "start_date": start_date,
        #     "end_date": end_date,
        #     "entry_drop_threshold": -0.0295,
        #     "exit_rebound_threshold": 0.09,
        #     "premium_per_put_contract": 128.0,
        #     "put_strike_percentage_below_spot": 0.0295,
        #     "days_to_expiration": 7,
        # },
    ]
    for test in test_data:
        final_twrr, final_capital_value, chronological_log = (
            backtest_qqq_put_income_strategy(
                initial_capital=test["initial_capital"],
                start_date=test["start_date"],
                end_date=test["end_date"],
                entry_drop_threshold=test["entry_drop_threshold"],
                exit_rebound_threshold=test["exit_rebound_threshold"],
                premium_per_put_contract=test["premium_per_put_contract"],
                put_strike_percentage_below_spot=test[
                    "put_strike_percentage_below_spot"
                ],
                days_to_expiration=test["days_to_expiration"],
            )
        )

        print("\n--- Chronological Event Log ---")
        for event in chronological_log:
            print(event["log"])

        # Print summary statistics after the chronological log
        print("-" * 60)
        print(f"Backtest Finished.")
        print(test)
        print(
            f"Total Trades: {len(chronological_log)}"
        )  # If trade_count isn't returned, use len of log
        print(f"Final Capital: ${final_capital_value:,.2f}")
        print(f"Total Return (TWRR): {final_twrr:.2%}")
        print("-" * 60)


# initial_capital=100000.0,
# start_date="2000-01-01",
# end_date="2000-01-01",
# entry_drop_threshold=-0.06,
# exit_rebound_threshold=0.10,
# premium_per_put_contract=18.0,
# put_strike_percentage_below_spot=0.06,
# Backtest Finished.
# Total Trades: 1
# Final Capital: $194,696.14
# Total Return (TWRR): 94.70%
