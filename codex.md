# Backtesting Strategies Project Codex

> 專案協作指南（請持續更新）

本文件統整本專案所有與 **QuantConnect / Lean** 策略回測相關的約定，目的是降低未來溝通成本。

## 目標

1. 提供一個可直接在 **QuantConnect** 平台（或本機 **Lean CLI**）執行的 Python 策略集合。
2. 每個策略放在獨立檔案，方便在雲端複製貼上或在 `lean backtest` 本機執行。
3. 所有策略遵循統一的 **命名規則**、**資料夾結構** 與 **程式格式**。

## 專案結構（草案）

```
.
├── algorithms/            # 放置 QuantConnect 策略（每檔一策略）
│   ├── __init__.py        # 空白即可，讓 python 視為 package
│   ├── base_template.py   # 建議繼承用的共用函式
│   ├── sample_momentum.py # 範例策略
│   └── ...
├── research/              # Jupyter / Notebook / 原型驗證用
├── codex.md               # 本文件
└── README.md              # 專案說明（可連結到 codex）
```

## 命名規則

* 檔名：`<策略英文名稱>.py`，全部小寫，單字以底線分隔。  
  例：`mean_reversion_etf.py`
* 類別名稱：`<策略英文名稱CamelCase>` 且必須繼承 `QCAlgorithm`。  
  例：`MeanReversionEtf`

## 策略檔案範本

```python
from AlgorithmImports import *  # QC 提供的統一匯入


class MyStrategy(QCAlgorithm):
    def Initialize(self):
        self.SetStartDate(2020, 1, 1)
        self.SetEndDate(2020, 12, 31)
        self.SetCash(100_000)

        self.symbol = self.AddEquity("SPY", Resolution.Daily).Symbol

        # 範例：每天開盤 30 分執行交易邏輯
        self.Schedule.On(
            self.DateRules.EveryDay(self.symbol),
            self.TimeRules.AfterMarketOpen(self.symbol, 30),
            self.TradeLogic,
        )

    def TradeLogic(self):
        # TODO: 你的策略
        pass

    def OnEndOfAlgorithm(self):
        self.Debug(f"Final Portfolio Value：{self.Portfolio.TotalPortfolioValue}")
```

## 測試與驗證

1. **語法檢查**：開發階段可直接 `python -m py_compile algorithms/<file>.py`，確認無語法錯誤。
2. **本機回測**：若已安裝 [Lean CLI](https://github.com/QuantConnect/lean)，可在專案根目錄執行  
   `lean backtest '<file name>' -c <config>`
3. **雲端回測**：登入 QuantConnect > Create Algorithm > 複製檔案內容 > Backtest。

## Contributing Checklist

* [ ] 新增策略檔前，確認檔案命名符合規則。
* [ ] 內部註解一律使用英文。
* [ ] 提交 PR 時請簡要說明 **策略概念** 與 **驗證結果**（圖片或連結）。

## TODO

1. 建立 `base_template.py` 以共用常用工具（如 ATR、SMA 快捷函式）。
2. 整合自動化測試（CI）確保基本語法無誤。

---

> 如果之後有新的約定或常見問題，請隨時於此補充！

