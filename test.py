from ib_insync import *
import pandas as pd
from datetime import datetime, timedelta


# 計算每月的第三個星期五
def get_third_friday(year: int, month: int) -> str:
    """
    計算每月的第三個星期五，返回格式為 YYYYMMDD。
    """
    first_day = datetime(year, month, 1)
    first_friday = first_day + timedelta(days=(4 - first_day.weekday() + 7) % 7)
    third_friday = first_friday + timedelta(weeks=2)
    return third_friday.strftime("%Y%m%d")


# 連接到 TWS
ib = IB()
ib.connect("127.0.0.1", 7496, clientId=1, readonly=True)

# 檢查連接
if not ib.isConnected():
    print("連接失敗，請檢查網絡或 TWS 設置。")
    exit()

# 查詢 SPX 指數的合約 ID
index_contract = Index(symbol="SPX", exchange="CBOE", currency="USD")
index_details = ib.reqContractDetails(index_contract)
if not index_details:
    print("無法找到 SPX 指數的合約詳細信息。")
    ib.disconnect()
    exit()

underlying_con_id = index_details[0].contract.conId  # 獲取基礎資產的 conId
print(f"SPX 指數的合約 ID 為: {underlying_con_id}")

# 生成回測期間的月份列表
start_date = datetime(2023, 1, 1)
end_date = datetime(2023, 12, 31)
months = pd.date_range(start=start_date, end=end_date, freq="MS")

# 儲存回測結果
backtest_results = []

for month in months:
    # 計算上一個月的第三個星期五 (交易日期)
    trade_year = month.year
    trade_month = month.month - 1 if month.month > 1 else 12
    trade_year -= 1 if trade_month == 12 else 0
    trade_date = get_third_friday(trade_year, trade_month)

    # 計算當月的第三個星期五 (到期日)
    expiry_date = get_third_friday(month.year, month.month)

    # 獲取 SPX 的歷史價格
    spx_bars = ib.reqHistoricalData(
        index_contract,
        endDateTime=f"{trade_date} 23:59:59",
        durationStr="1 D",
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
    )

    if not spx_bars:
        print(f"{trade_date} 無法獲取 SPX 指數歷史價格。")
        continue

    spx_price_on_trade_date = spx_bars[0].close

    # 交易日的歷史價格查詢
    available_strikes = []

    # 假設行權價範圍
    strike_range = range(
        int(spx_price_on_trade_date * 0.8) // 5 * 5,  # 起始行權價，向下取整為5的倍數
        int(spx_price_on_trade_date * 1.2) // 5 * 5
        + 5,  # 結束行權價，向上取整為5的倍數
        5,  # 行權價的步長
    )

    # 遍歷可能的行權價
    for strike in strike_range:
        # 定義選擇權合約
        test_contract = Option(
            symbol="SPX",
            lastTradeDateOrContractMonth=expiry_date,
            strike=strike,
            right="P",
            multiplier="100",
            exchange="CBOE",
            currency="USD",
        )

        # 嘗試獲取該行權價的歷史價格
        test_bars = ib.reqHistoricalData(
            test_contract,
            endDateTime=f"{trade_date} 23:59:59",
            durationStr="1 D",
            barSizeSetting="1 day",
            whatToShow="MIDPOINT",
            useRTH=True,
        )

        # 如果有數據返回，該行權價有效
        if test_bars:
            print(f"行權價 {strike} 有效。")
            available_strikes.append(strike)

    print(f"交易日當時的可用行權價: {available_strikes}")

    # 選擇 Delta 最接近 0.15 的行權價
    delta_target = 0.15
    best_strike = None
    best_delta_diff = float("inf")

    for strike in available_strikes:
        # 模擬計算 Delta（實際應根據選擇權模型或市場數據查詢）
        delta = abs((spx_price_on_trade_date - strike) / spx_price_on_trade_date)
        if abs(delta - delta_target) < best_delta_diff:
            best_delta_diff = abs(delta - delta_target)
            best_strike = strike

    strike_price = best_strike
    print(f"選定行權價: {strike_price}")

    # 定義選擇權合約
    option_contract = Option(
        symbol="SPX",
        lastTradeDateOrContractMonth=expiry_date,
        strike=strike_price,
        right="P",
        multiplier="100",
        exchange="SMART",
        currency="USD",
    )

    # 獲取交易日期的進場價格
    entry_bars = ib.reqHistoricalData(
        option_contract,
        endDateTime=f"{trade_date} 23:59:59",
        durationStr="1 D",
        barSizeSetting="1 day",
        whatToShow="MIDPOINT",
        useRTH=True,
    )

    if not entry_bars:
        print(f"{trade_date} 無法獲取進場歷史數據。")
        continue

    entry_price = entry_bars[0].close

    # 獲取到期日的結算價格
    expiry_bars = ib.reqHistoricalData(
        option_contract,
        endDateTime=f"{expiry_date} 23:59:59",
        durationStr="1 D",
        barSizeSetting="1 day",
        whatToShow="MIDPOINT",
        useRTH=True,
    )

    if not expiry_bars:
        print(f"{expiry_date} 無法獲取到期歷史數據。")
        continue

    expiry_price = expiry_bars[0].close

    # 計算盈虧
    profit = entry_price - expiry_price

    # 保存結果
    backtest_results.append(
        {
            "Month": month.strftime("%Y-%m"),
            "Trade Date": trade_date,
            "Expiry Date": expiry_date,
            "Strike": strike_price,
            "Entry Price": entry_price,
            "Expiry Price": expiry_price,
            "Profit": profit,
        }
    )

# 儲存回測結果到 CSV
results_df = pd.DataFrame(backtest_results)
results_df.to_csv("spx_backtest_results.csv", index=False)
print("回測完成，結果已保存至 spx_backtest_results.csv")

# 斷開連接
ib.disconnect()
