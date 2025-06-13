import yfinance as yf
import pandas as pd

# 開始回測 QQQ 策略...
# 初始資金: $10,000.00
# 回測期間: 2000-01-01 至 2025-05-31
# 買入條件: 週跌幅 < -6.00%
# 賣出條件: 週漲幅 > 10.00% 或 持有達到 800 週


# 回測結束。
# 總交易次數: 8
# 初始資金: $10,000.00
# 最終資金: $98,749.59
# 時間加權報酬率 (TWRR): 887.50%
def backtest_qqq_strategy(
    initial_capital=10000.0,
    start_date="2000-01-01",
    end_date="2025-05-31",  # 截至上個月底
    entry_drop_threshold=-0.06,  # 單週下跌超過6%
    exit_rebound_threshold=0.05,  # 單週反彈超過5%
    max_holding_weeks=20,
):
    """
    回測QQQ交易策略並計算TWRR。

    Args:
        initial_capital (float): 初始資金.
        start_date (str): 回測開始日期 (YYYY-MM-DD).
        end_date (str): 回測結束日期 (YYYY-MM-DD).
        entry_drop_threshold (float): 觸發買入的週跌幅閾值 (負數, e.g., -0.06 for -6%).
        exit_rebound_threshold (float): 觸發賣出的週漲幅閾值 (正數, e.g., 0.05 for +5%).
        max_holding_weeks (int): 最大持倉週數.

    Returns:
        tuple: (twrr, final_capital)
    """

    print(f"開始回測 QQQ 策略...")
    print(f"初始資金: ${initial_capital:,.2f}")
    print(f"回測期間: {start_date} 至 {end_date}")
    print(f"買入條件: 週跌幅 < {entry_drop_threshold:.2%}")
    print(
        f"賣出條件: 週漲幅 > {exit_rebound_threshold:.2%} 或 持有達到 {max_holding_weeks} 週"
    )
    print("-" * 50)

    # 獲取數據
    qqq_data = yf.Ticker("QQQ").history(start=start_date, end=end_date, interval="1wk")

    if qqq_data.empty:
        print("錯誤：無法獲取 QQQ 的歷史數據。")
        return 0.0, initial_capital

    # 數據準備
    qqq_data = qqq_data[["Open", "Close"]].copy()
    qqq_data.dropna(inplace=True)  # 移除 Open 或 Close 為 NaN 的行

    if len(qqq_data) < 2:
        print("錯誤：數據不足以計算報酬率。")
        return 0.0, initial_capital

    qqq_data["WeeklyReturn"] = qqq_data["Close"].pct_change()
    # 移除第一行 (因為 WeeklyReturn 會是 NaN)
    qqq_data = qqq_data.iloc[1:].copy()

    if qqq_data.empty:
        print("錯誤：處理報酬率後數據不足。")
        return 0.0, initial_capital

    # 回測變數初始化
    capital = initial_capital
    shares = 0.0
    in_position = False
    entry_price_current_trade = 0.0
    weeks_in_trade = 0
    trade_count = 0

    # 遍歷每週數據進行回測
    for i in range(len(qqq_data)):
        current_week_date = qqq_data.index[i]
        current_week_close_price = qqq_data["Close"].iloc[i]
        current_week_return = qqq_data["WeeklyReturn"].iloc[i]

        # 決定潛在的交易執行價格 (下一週的開盤價) 和日期
        can_trade_next_week = i + 1 < len(qqq_data)
        potential_trade_price = (
            qqq_data["Open"].iloc[i + 1] if can_trade_next_week else 0
        )
        potential_trade_date = qqq_data.index[i + 1] if can_trade_next_week else None

        if in_position:
            weeks_in_trade += 1
            exit_executed_this_iteration = False

            # 檢查是否為數據的最後一週，若是則必須平倉
            if not can_trade_next_week:
                exit_price = current_week_close_price  # 以當週收盤價平倉
                if shares > 0:  # 確保有持倉
                    capital = shares * exit_price
                    trade_count += 1
                    print(
                        f"交易 {trade_count}: 強制平倉 (數據結束) 日期: {current_week_date.strftime('%Y-%m-%d')}, "
                        f"價格: ${exit_price:,.2f}, 持有週數: {weeks_in_trade}, 目前資金: ${capital:,.2f}"
                    )
                in_position = False
                exit_executed_this_iteration = True
            else:
                # 賣出條件1: 週反彈超過閾值
                if current_week_return > exit_rebound_threshold:
                    exit_price = potential_trade_price
                    if shares > 0 and exit_price > 0:  # 確保有持倉且價格有效
                        capital = shares * exit_price
                        trade_count += 1
                        print(
                            f"交易 {trade_count}: 賣出 (反彈) 日期: {potential_trade_date.strftime('%Y-%m-%d')}, "
                            f"價格: ${exit_price:,.2f}, 持有週數: {weeks_in_trade}, 目前資金: ${capital:,.2f}"
                        )
                    elif shares > 0:
                        print(
                            f"警告: 賣出 (反彈) 日期 {potential_trade_date.strftime('%Y-%m-%d')} 時開盤價為零或無效，無法執行交易。"
                        )
                    in_position = False
                    exit_executed_this_iteration = True
                # 賣出條件2: 達到最大持有週數
                elif weeks_in_trade >= max_holding_weeks:
                    exit_price = potential_trade_price
                    if shares > 0 and exit_price > 0:  # 確保有持倉且價格有效
                        capital = shares * exit_price
                        trade_count += 1
                        print(
                            f"交易 {trade_count}: 賣出 (達最大持有週數) 日期: {potential_trade_date.strftime('%Y-%m-%d')}, "
                            f"價格: ${exit_price:,.2f}, 持有週數: {weeks_in_trade}, 目前資金: ${capital:,.2f}"
                        )
                    elif shares > 0:
                        print(
                            f"警告: 賣出 (達最大持有週數) 日期 {potential_trade_date.strftime('%Y-%m-%d')} 時開盤價為零或無效，無法執行交易。"
                        )
                    in_position = False
                    exit_executed_this_iteration = True

            if exit_executed_this_iteration:
                shares = 0.0
                entry_price_current_trade = 0.0
                # weeks_in_trade 會在下次進入新倉位時重置

        # 買入條件 (僅在未持倉且非當前回測週期剛平倉的情況下)
        if not in_position and can_trade_next_week:
            # 使用當週的報酬率作為下一週是否買入的信號
            if current_week_return < entry_drop_threshold:
                entry_price = potential_trade_price
                if capital > 0 and entry_price > 0:  # 確保資金充足且價格有效
                    shares = capital / entry_price
                    entry_price_current_trade = entry_price  # 記錄此次交易的買入價
                    in_position = True
                    weeks_in_trade = 0  # 新倉位開始，持有週數重置 (下一輪迭代會變為1)
                    print(
                        f"買入信號: 日期 {potential_trade_date.strftime('%Y-%m-%d')}, "
                        f"價格: ${entry_price:,.2f}, 投入資金: ${capital:,.2f}, 買入股數: {shares:,.2f}"
                    )
                    # 資金在此刻被視為投入，實際的 capital 變動發生在賣出時
                elif capital > 0:
                    print(
                        f"警告: 買入信號日期 {potential_trade_date.strftime('%Y-%m-%d')} 時開盤價為零或無效，無法執行交易。"
                    )

    # 計算最終的TWRR
    if initial_capital == 0:
        twrr = 0.0 if capital == 0 else float("inf")  # 避免除以零
    else:
        twrr = (capital - initial_capital) / initial_capital

    print("-" * 50)
    print(f"回測結束。")
    print(f"總交易次數: {trade_count}")
    print(f"初始資金: ${initial_capital:,.2f}")
    print(f"最終資金: ${capital:,.2f}")
    print(f"時間加權報酬率 (TWRR): {twrr:.2%}")
    print("-" * 50)

    return twrr, capital


if __name__ == "__main__":
    # 執行回測
    # 您可以調整這裡的參數
    test_data = [
        {
            "initial_capital": 100000.0,
            "start_date": "2000-01-01",
            "end_date": "2025-12-31",
            "entry_drop_threshold": -0.06,
            "exit_rebound_threshold": 0.10,
            "premium_per_put_contract": 303.0,
            "put_strike_percentage_below_spot": 0.06,
            "days_to_expiration": 30,  # Example: 30 DTE
        },
        {
            "initial_capital": 100000.0,
            "start_date": "2000-01-01",
            "end_date": "2025-12-31",
            "entry_drop_threshold": -0.047,
            "exit_rebound_threshold": 0.10,
            "premium_per_put_contract": 390.0,
            "put_strike_percentage_below_spot": 0.047,
            "days_to_expiration": 30,  # Example: 30 DTE
        },
        {
            "initial_capital": 100000.0,
            "start_date": "2000-01-01",
            "end_date": "2025-12-31",
            "entry_drop_threshold": -0.06,
            "exit_rebound_threshold": 0.10,
            "premium_per_put_contract": 19.0,
            "put_strike_percentage_below_spot": 0.06,
            "days_to_expiration": 7,  # Example: 30 DTE
        },
        {
            "initial_capital": 100000.0,
            "start_date": "2000-01-01",
            "end_date": "2025-12-31",
            "entry_drop_threshold": -0.032,
            "exit_rebound_threshold": 0.10,
            "premium_per_put_contract": 77.0,
            "put_strike_percentage_below_spot": 0.032,
            "days_to_expiration": 7,  # Example: 30 DTE
        },
    ]
    for test in test_data:
        final_twrr, final_capital_value = backtest_qqq_strategy(
            initial_capital=test["initial_capital"],
            start_date=test["start_date"],
            end_date=test["end_date"],
            entry_drop_threshold=test["entry_drop_threshold"],
            exit_rebound_threshold=test["exit_rebound_threshold"],
            # premium_per_put_contract=test["premium_per_put_contract"],
            # put_strike_percentage_below_spot=test["put_strike_percentage_below_spot"],
            # days_to_expiration=test["days_to_expiration"],
            max_holding_weeks=800,  # 使用一個較大的持有週數限制
        )
    # 可以在此處添加更多關於結果的分析或圖表繪製
    # 例如: print(f"最終TWRR: {final_twrr:.2%}, 最終資金: ${final_capital_value:,.2f}")
    # 由於函數內部已有詳細打印，此處可省略重複打印


# initial_capital=100000.0,
# start_date="2020-01-01",
# end_date="2025-05-31",  # 使用一個實際的近期日期
# entry_drop_threshold=-0.06,
# exit_rebound_threshold=0.10,
# max_holding_weeks=800,
# 回測結束。
# 總交易次數: 1
# 初始資金: $100,000.00
# 最終資金: $213,967.97
# 時間加權報酬率 (TWRR): 113.97%
