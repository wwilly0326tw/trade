"""ibkr_positions_ibapi.py
Retrieve current IBKR account positions using the *official* TWS API package
(`ibapi`).  The helper hides the asynchronous nature of the API and returns the
complete snapshot as a simple Python object – optionally as
`pandas.DataFrame`.

The function requires a running TWS or IB Gateway instance that listens on the
specified host/port (default *localhost:7496*).  No external dependencies are
needed beyond `ibapi`; `pandas` is optional.

Example
-------
>>> from ibkr_positions_ibapi import fetch_ibkr_positions
>>> df = fetch_ibkr_positions()
>>> print(df.head())
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

try:
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.contract import Contract

    _HAS_IBAPI = True
except ImportError:  # pragma: no cover – ibapi not installed in CI
    _HAS_IBAPI = False


try:
    import pandas as pd  # type: ignore

    _HAS_PANDAS = True
except ImportError:  # pragma: no cover – optional
    _HAS_PANDAS = False


# ---------------------------------------------------------------------------
# Low-level wrapper to capture *all* position callbacks
# ---------------------------------------------------------------------------


if _HAS_IBAPI:

    class _PositionCollector(EWrapper, EClient):  # type: ignore[misc]
        """Combine *EWrapper* and *EClient* to collect position updates."""

        def __init__(self):
            EWrapper.__init__(self)
            EClient.__init__(self, wrapper=self)

            self._positions: List[Dict[str, Any]] = []
            self._completed = threading.Event()

        # EWrapper overrides -------------------------------------------------

        def position(self, account: str, contract: "Contract", pos: float, avgCost: float):  # noqa: N802
            self._positions.append(
                {
                    "account": account,
                    "conId": contract.conId,
                    "secType": contract.secType,
                    "symbol": contract.symbol,
                    "lastTradeDateOrContractMonth": contract.lastTradeDateOrContractMonth,
                    "strike": contract.strike,
                    "right": contract.right,
                    "exchange": contract.exchange,
                    "currency": contract.currency,
                    "position": pos,
                    "avgCost": avgCost,
                }
            )

        def positionEnd(self):  # noqa: N802
            self._completed.set()

        # Error callback so that we don't print unwanted spam ---------------

        def error(self, reqId, errorCode, errorString):  # noqa: N802
            # Override only to suppress excessive default logging; users can
            # still inspect _positions even when some errors occurred.
            if errorCode in (2104, 2106):  # market data farm connect msgs
                return
            print(f"[ibkr_positions_ibapi] error {errorCode}: {errorString}")

# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------


def fetch_ibkr_positions(
    *,
    host: str = "127.0.0.1",
    port: int = 7496,
    client_id: int = 119,
    account: Optional[str] = None,
    as_dataframe: bool | None = None,
    timeout: float = 10.0,
) -> "List[Dict[str, Any]] | pd.DataFrame":
    """Return a snapshot of current account positions via the *official* IB API.

    Parameters
    ----------
    host, port, client_id
        Connection parameters for TWS / IB Gateway.
    account
        Filter results to the given account id (if you are subscribed to more
        than one).  When *None* (default) all received records are returned.
    as_dataframe
        Control output format.  *None* (default) – return DataFrame if pandas
        is available; otherwise a list of dicts.  *True* / *False* enforce a
        specific format.
    timeout
        Maximum time in **seconds** to wait for the *positionEnd* callback
        before the helper returns whatever has been collected so far.
    """

    if not _HAS_IBAPI:
        raise RuntimeError("ibapi is not installed; cannot use official API")

    collector = _PositionCollector()

    # ------------------------------------------------------------------
    # Launch the API's reader thread – required to receive callbacks
    # ------------------------------------------------------------------
    collector.connect(host, port, client_id)

    # EClient.run() is a *blocking* method; we therefore start it in a daemon
    # thread so that Python can exit even when something goes wrong.
    thread = threading.Thread(target=collector.run, daemon=True)
    thread.start()

    # Request positions (asynchronous)
    collector.reqPositions()

    # Wait for completion or timeout
    collector._completed.wait(timeout)

    # Always disconnect to free socket resources
    try:
        collector.disconnect()
    except Exception:
        pass  # pragma: no cover – already closed / never opened

    records = (
        [pos for pos in collector._positions if (account is None or pos["account"] == account)]
        if account is not None
        else collector._positions
    )

    # Decide on output format
    if as_dataframe is None:
        as_dataframe = _HAS_PANDAS

    if as_dataframe:
        if not _HAS_PANDAS:
            raise RuntimeError("pandas is not installed but as_dataframe=True was requested")
        return pd.DataFrame(records)  # type: ignore[return-value]

    return records  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# If executed as a script: simple smoke test (will return empty on CI)
# ---------------------------------------------------------------------------


if __name__ == "__main__":  # pragma: no cover
    try:
        data = fetch_ibkr_positions(as_dataframe=False, timeout=3)
        print(f"Received {len(data)} positions")
    except Exception as exc:
        print("Cannot fetch positions:", exc)

