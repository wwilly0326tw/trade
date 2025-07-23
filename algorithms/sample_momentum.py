"""Sample Momentum Strategy for QuantConnect platform.

To use in QuantConnect cloud IDE:
1. Create a new Python algorithm.
2. Replace the default content with this file's content.

This file follows the project coding conventions defined in codex.md.
"""

from AlgorithmImports import *  # QuantConnect LEAN aggregated imports


class SampleMomentum(QCAlgorithm):
    """A simplistic 50/200-day SMA momentum cross strategy on SPY.

    Buy when the 50-day simple moving average crosses *above* the 200-day SMA.
    Exit (liquidate) when it crosses *below* again.
    """

    def Initialize(self) -> None:
        # Backtest parameters
        self.SetStartDate(2010, 1, 1)
        self.SetEndDate(2023, 12, 31)
        self.SetCash(100_000)

        # Universe
        self.symbol = self.AddEquity("SPY", Resolution.Daily).Symbol

        # Indicators
        self.fast = self.SMA(self.symbol, 50, Resolution.Daily)
        self.slow = self.SMA(self.symbol, 200, Resolution.Daily)

        # Schedule a daily check 10 minutes after NYSE open
        self.Schedule.On(
            self.DateRules.EveryDay(self.symbol),
            self.TimeRules.AfterMarketOpen(self.symbol, 10),
            self.Rebalance,
        )

        # Warm-up period so that both SMAs are ready before trading
        self.SetWarmUp(200, Resolution.Daily)

    def Rebalance(self) -> None:
        if self.IsWarmingUp:
            return

        invested = self.Portfolio.Invested

        # Golden cross: fast SMA > slow SMA -> long
        if not invested and self.fast.Current.Value > self.slow.Current.Value:
            self.SetHoldings(self.symbol, 1.0)
        # Death cross: fast SMA < slow SMA -> exit
        elif invested and self.fast.Current.Value < self.slow.Current.Value:
            self.Liquidate(self.symbol)

    def OnEndOfAlgorithm(self) -> None:
        self.Debug(f"Final portfolio value: {self.Portfolio.TotalPortfolioValue:,.2f}")

