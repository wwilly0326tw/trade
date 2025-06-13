from ib_insync import *
import pandas as pd

# 連接到 TWS
ib = IB()
ib.connect("127.0.0.1", 7496, clientId=1, readonly=True)  # 確保端口與 TWS 設定一致

if ib.isConnected():
    print("成功連接到 TWS。")
else:
    print("連接失敗，請檢查網絡或 TWS 設置。")

# 設置選擇權合約
option_contract = Option(
    symbol="SPX",  # 標的
    lastTradeDateOrContractMonth="20250117",  # 到期日 (YYYYMMDD)
    strike=4500,  # 行權價
    right="P",  # 選擇權類型: Put
    multiplier="100",  # 合約單位
    exchange="CBOE",  # 使用 SMART 路由
    currency="USD",  # 基礎貨幣
)

contract_details = ib.reqContractDetails(option_contract)
if not contract_details:
    print("無法找到選擇權合約，請檢查參數設置。")
else:
    print("選擇權合約已成功解析。")


# 請求歷史數據
bars = ib.reqHistoricalData(
    option_contract,
    endDateTime="",
    durationStr="1 D",  # 減至 1 天
    barSizeSetting="15 mins",  # 更高頻率
    whatToShow="MIDPOINT",
    useRTH=True,
)


# 轉換數據為 DataFrame
if bars:
    df = util.df(bars)
    df.to_csv("spx_option_data.csv", index=False)
    print("數據已保存至 spx_option_data.csv")
else:
    print("無法獲取歷史數據，請檢查數據權限或合約定義。")

# 斷開連接
ib.disconnect()
