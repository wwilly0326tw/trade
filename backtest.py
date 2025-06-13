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

option_contract = Option(
    symbol="SPX",
    lastTradeDateOrContractMonth="20250116",
    strike=5700,
    right="P",
    multiplier="100",
    exchange="CBOE",  # 或 SMART
    currency="USD",
)

contract_details = ib.reqContractDetails(option_contract)
if not contract_details:
    print("合約不存在或權限不足。")

# 嘗試獲取該行權價的歷史價格
test_bars = ib.reqHistoricalData(
    option_contract,
    endDateTime=f"20240101 23:59:59",
    durationStr="1 D",
    barSizeSetting="1 day",
    whatToShow="TRADES",
    useRTH=True,
)
if test_bars:
    print(f"行權價有效。")
