"""Common helper functions for QuantConnect strategies.

This module is *optional*; feel free to extend with reusable code such as:
    - Technical indicator factory methods
    - Position sizing helpers
    - Logging utilities

Import example:
    from algorithms.base_template import atr_stop
"""

from AlgorithmImports import *


def atr_stop(algo: QCAlgorithm, symbol: Symbol, atr_period: int = 14, multiplier: float = 3.0):
    """Return an ATR-based stop-loss current price level.

    Parameters
    ----------
    algo : QCAlgorithm
        The running algorithm instance.
    symbol : Symbol
        Target symbol.
    atr_period : int, optional
        Period of ATR calculation, by default 14.
    multiplier : float, optional
        Multiplier for stop distance, by default 3.0.

    Note
    ----
    This function **requires** that the algorithm has already registered an ATR
    indicator elsewhere, otherwise it will create one on-the-fly.
    """

    # Reuse existing indicator if present
    name = f"ATR_{symbol}_{atr_period}"
    atr = algo.Indicator(name) if algo.Indicator(name) is not None else None

    if atr is None:
        atr = algo.ATR(symbol, atr_period, MovingAverageType.Simple, Resolution.Daily, name=name)

    if not atr.IsReady:
        return None

    last_price = algo.Securities[symbol].Price
    return last_price - atr.Current.Value * multiplier

