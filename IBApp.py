import re
import time
import threading
import datetime
import logging
from collections import deque
from typing import List, Dict, Optional, Any

import pytz
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract, ContractDetails
from ibapi.common import BarData

# 常量定義
TICK_LIST_OPT = "101,106,165,221,225,233,236,258,293,294,411,456"
TIMEOUT = 5.0  # 單檔行情等待秒數
DEBUG = False

# 日誌設定
log = logging.getLogger(__name__)


# ---------------- IB 客戶端 ---------------
class IBApp(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.ready = threading.Event()
        self.req_id = 1
        self.contract_details: List[ContractDetails] = []
        # ───────── Market-data cache ─────────
        # original snapshot() 以 rid 為 key 暫存一次性資料；
        # 併發 / 長駐訂閱需要一份全域快取讓外部直接存取最新 tick。
        # 使用兩層 mapping：  rid → tmp dict (沿用原本)，以及 key → dict 供 AlertEngine 查詢。
        self.tickers: Dict[int, Dict[str, Any]] = {}
        self._stream_key_map: Dict[int, str] = {}
        self._stream_data: Dict[str, Dict[str, Any]] = {}

        # 用於儲存市場狀態信息
        self.market_status = {"is_open": False, "next_open": None, "last_check": None}
        self.contract_details_queue = deque()
        self.contract_details_available = threading.Event()
        self.current_time_queue = deque()
        self.current_time_available = threading.Event()
        self.historical_data_queue = deque()
        self.historical_data_available = threading.Event()
        self.historical_data_end_available = threading.Event()

        # 美東時區
        self.us_eastern = pytz.timezone("US/Eastern")

        # For positions tracking
        self._positions = []
        self._positions_completed = threading.Event()

    def _calculate_next_trading_day(self, et_now: datetime.datetime) -> None:
        """
        計算下一個正規交易日 09:30 ET，寫回 self.market_status['next_open']。
        若要更精確可再解析 tradingHours/liquidHours。
        """
        tz = self.us_eastern
        # 從明天開始往後找第一個非週末
        d = et_now + datetime.timedelta(days=1)
        while d.weekday() >= 5:  # 5=Sat, 6=Sun
            d += datetime.timedelta(days=1)

        next_open = tz.localize(datetime.datetime(d.year, d.month, d.day, 9, 30))
        self.market_status["next_open"] = next_open

    # ---------------- 盤中判斷 ----------------
    def is_regular_market_open(self) -> bool:
        """
        僅判斷 RTH (09:30–16:00 ET, Mon–Fri)。
        若無法取得伺服器時間，沿用快取狀態，避免誤判休市。
        """
        server_time = self.get_server_time()
        if not server_time:
            # 沿用前次判斷結果，預設 True → 把「斷線」和「休市」分開處理
            return self.market_status.get("is_open", True)

        et = server_time.astimezone(self.us_eastern)
        if et.weekday() >= 5:  # 週六週日
            return False
        t = et.time()
        return datetime.time(9, 30) <= t < datetime.time(16, 0)

    # --- 握手完成
    def nextValidId(self, oid: int):
        self.req_id = max(self.req_id, oid)
        self.ready.set()

    def error(
        self,
        reqId: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson: str = "",
        *args,
    ):
        if errorCode in (2104, 2106, 2158, 321):  # exchange 未填
            log.debug("IB ERR 321(%s): %s", reqId, errorString)
            return  # 不跳 WARNING
        # 其他錯誤維持 WARNING
        log.warning("IB ERR %s(%s): %s", errorCode, reqId, errorString)

    # --- 市場狀態相關回調
    def contractDetails(self, reqId: int, details: ContractDetails):
        # 同步給 list 與 deque
        self.contract_details.append(details)  ### <--
        self.contract_details_queue.append(details)  ### <--

    def contractDetailsEnd(self, reqId):
        self.contract_details_available.set()

    def currentTime(self, server_time):
        self.current_time_queue.append(server_time)
        self.current_time_available.set()

    def historicalData(self, reqId, bar: BarData):
        self.historical_data_queue.append(bar)

    def historicalDataEnd(self, reqId, start, end):
        self.historical_data_available.set()
        self.historical_data_end_available.set()

    # ---------------- Streaming market-data ----------------
    def subscribe(self, con: Contract, is_opt: bool, key: str) -> int:
        """Subscribe to streaming market data for *con*, identify by *key*.

        Parameters
        ----------
        con : Contract
            IB contract object.
        is_opt : bool
            True if option contract (will request greeks via TICK_LIST_OPT).
        key : str
            Application-level identifier (e.g. contract symbol or dict key).
        Returns
        -------
        int
            IB ticker id assigned to this subscription.
        """
        rid = self.req_id
        self.req_id += 1
        tick_list = TICK_LIST_OPT if is_opt else ""

        self._stream_key_map[rid] = key
        self.reqMktData(rid, con, tick_list, False, False, [])

        return rid

    def unsubscribe(self, rid: int):
        """Cancel an existing streaming subscription by ticker id."""
        if rid in self._stream_key_map:
            self.cancelMktData(rid)
            key = self._stream_key_map.pop(rid)
            self._stream_data.pop(key, None)

    def get_stream_data(self, key: str) -> Dict[str, Any]:
        """Return the latest cached tick dictionary for *key*."""
        return self._stream_data.get(key, {})

    # --- Tick 處理
    FIELD_MAP = {
        0: "bid_size",
        1: "bid",
        2: "ask",
        3: "ask_size",
        4: "last",
        5: "last_size",
        6: "high",
        7: "low",
        8: "close",
        9: "prev_close",
        14: "open",
        27: "bid_iv",
        28: "ask_iv",
        31: "last_iv",
        49: "call_oi",
        50: "put_oi",
        55: "call_vol",
        56: "put_vol",
    }

    def tickPrice(self, reqId, field, price, _):
        key = self.FIELD_MAP.get(field, f"p{field}")
        self.tickers.setdefault(reqId, {})[key] = price

        # 更新 streaming 快取 (若此 reqId 為長駐訂閱)
        if reqId in self._stream_key_map:
            k = self._stream_key_map[reqId]
            self._stream_data.setdefault(k, {})[key] = price

    def tickSize(self, reqId, field, size):
        self.tickers.setdefault(reqId, {})[f"size_{field}"] = size

        if reqId in self._stream_key_map:
            k = self._stream_key_map[reqId]
            self._stream_data.setdefault(k, {})[f"size_{field}"] = size

    def tickGeneric(self, reqId, field, value):
        self.tickers.setdefault(reqId, {})[f"g{field}"] = value

        if reqId in self._stream_key_map:
            k = self._stream_key_map[reqId]
            self._stream_data.setdefault(k, {})[f"g{field}"] = value

    # 兼容不同版本 (>= v10.19 參數增多)
    def tickOptionComputation(self, reqId, *args):
        iv = args[2] if len(args) > 2 else None
        delta = args[3] if len(args) > 3 else None
        gamma = args[6] if len(args) > 6 else None
        vega = args[7] if len(args) > 7 else None
        theta = args[8] if len(args) > 8 else None
        undPx = args[9] if len(args) > 9 else None
        self.tickers.setdefault(reqId, {}).update(
            {
                "iv": iv,
                "delta": delta,
                "gamma": gamma,
                "vega": vega,
                "theta": theta,
                "undPx": undPx,
            }
        )

        if reqId in self._stream_key_map:
            k = self._stream_key_map[reqId]
            self._stream_data.setdefault(k, {}).update(
                {
                    "iv": iv,
                    "delta": delta,
                    "gamma": gamma,
                    "vega": vega,
                    "theta": theta,
                    "undPx": undPx,
                }
            )

    # --- 單檔 Snapshot
    def snapshot(self, con: Contract, is_opt: bool) -> Dict[str, Any]:
        """從 IB 取得單一合約市場快照"""
        rid = self.req_id
        self.req_id += 1
        tick_list = TICK_LIST_OPT if is_opt else ""
        self.reqMktData(rid, con, tick_list, False, False, [])
        t0 = time.time()
        while time.time() - t0 < TIMEOUT:
            d = self.tickers.get(rid, {})
            price_ready = any(k in d for k in ("last", "bid", "ask"))
            greeks_ready = (not is_opt) or ("delta" in d and d["delta"] is not None)
            if price_ready and greeks_ready:
                break
            time.sleep(0.05)
        self.cancelMktData(rid)
        data = self.tickers.pop(rid, {})
        if DEBUG:
            print("DEBUG tick", con.symbol, con.right if is_opt else "STK", data)
        price = data.get("last") or data.get("bid") or data.get("ask")
        close = data.get("prev_close") or data.get("close")
        return {
            "price": price,
            "delta": data.get("delta"),
            "iv": data.get("iv"),
            "close": close,
        }

    # --- 市場狀態檢查
    def get_contract_trading_hours(self, contract: Contract) -> Optional[str]:
        """取得合約的交易時間"""
        self.contract_details_available.clear()
        self.contract_details_queue.clear()

        req_id = self.req_id
        self.req_id += 1
        self.reqContractDetails(req_id, contract)

        if not self.contract_details_available.wait(10):
            return None

        if not self.contract_details_queue:
            return None

        details = self.contract_details_queue[0]
        return details.tradingHours

    _last_server_time: Optional[datetime.datetime] = None
    _server_time_ts: float = 0.0  # epoch 秒

    def get_server_time(
        self, retry: int = 3, timeout: float = 5.0
    ) -> Optional[datetime.datetime]:
        """
        取 IB 伺服器時間；若短暫失敗，保留最近一次成功結果，
        並在 retry 次內重試，避免把市場誤判為休市。
        """
        for _ in range(retry):
            self.current_time_available.clear()
            self.current_time_queue.clear()
            self.reqCurrentTime()
            if self.current_time_available.wait(timeout):
                ts = self.current_time_queue.popleft()
                # 轉成 aware UTC datetime
                dt = datetime.datetime.fromtimestamp(ts, tz=pytz.UTC)
                # 快取
                self._last_server_time = dt
                self._server_time_ts = time.time()
                return dt
            time.sleep(0.2)  # 短暫讓出 GIL 以接收封包

        # 若連續失敗 → 回傳最近一次成功的結果（若有且 <90 秒）
        if self._last_server_time and (time.time() - self._server_time_ts) < 90:
            return self._last_server_time
        return None  # 真的要回 None，讓外部決定 fallback

    def check_recent_trades(self, symbol: str = "SPY") -> bool:
        """檢查是否有最近的交易資料來確定市場是否開盤"""
        self.historical_data_available.clear()
        self.historical_data_end_available.clear()
        self.historical_data_queue.clear()

        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"

        req_id = self.req_id
        self.req_id += 1

        # 請求最近 5 分鐘的一分鐘 K 線
        end_time = ""  # 空字符串表示當前時間
        duration = "300 S"  # 5 分鐘
        bar_size = "1 min"
        self.reqHistoricalData(
            req_id, contract, end_time, duration, bar_size, "TRADES", 1, 1, False, []
        )

        if not self.historical_data_end_available.wait(10):
            return False

        # 檢查是否有資料
        if not self.historical_data_queue:
            return False

        # 取最近一筆資料看成交量
        recent_bar = self.historical_data_queue[-1]
        return recent_bar.volume > 0

    def is_market_open(self) -> Dict[str, Any]:
        """綜合判斷市場是否開盤，並返回市場狀態信息"""
        # 最多每分鐘檢查一次，避免過度 API 請求
        now = datetime.datetime.now()
        if (
            self.market_status["last_check"]
            and (now - self.market_status["last_check"]).total_seconds() < 60
        ):
            return self.market_status

        self.market_status["last_check"] = now

        # 1. 獲取伺服器時間
        server_time = self.get_server_time()

        # -------------------------------------------------------------
        # 若無法即時取得伺服器時間 (可能只是連線瞬斷)，採用「寬限期」策略，
        # 以降低誤判市場休市的機率。
        # -------------------------------------------------------------
        GRACE_PERIOD = 600  # seconds; 10 minutes

        if not server_time:
            # 若最近一次成功取得的 server_time 距今尚在寬限期內，
            # 且先前市場判定為開市，則沿用原判定。
            if (
                self._last_server_time
                and (time.time() - self._server_time_ts) < GRACE_PERIOD
                and self.market_status.get("is_open", False)
            ):
                log.warning("無法獲取伺服器時間 ‑ 使用緩存判斷市場仍在交易 (grace)")
                # 不更新其他狀態，直接回傳沿用結果。
                return self.market_status

            # 超過寬限期 → 視為休市
            log.warning("無法獲取伺服器時間，超過寬限期 → 視為休市")
            self.market_status["is_open"] = False

            # 嘗試推算下一個開盤日，避免外部取得 None
            if self._last_server_time:
                self._calculate_next_trading_day(
                    self._last_server_time.astimezone(self.us_eastern)
                )
            return self.market_status

        # 轉換到美東時間
        et_time = server_time.astimezone(self.us_eastern)

        # 2. 檢查是否為週末
        if et_time.weekday() >= 5:  # 5=週六, 6=週日
            log.info(f"今天是週{et_time.weekday()+1}，市場休市")
            self.market_status["is_open"] = False
            self._calculate_next_trading_day(et_time)
            return self.market_status

        # 3. 獲取 SPY 交易時間
        spy_contract = Contract()
        spy_contract.symbol = "SPY"
        spy_contract.secType = "STK"
        spy_contract.exchange = "SMART"
        spy_contract.currency = "USD"

        trading_hours = self.get_contract_trading_hours(spy_contract)
        if not trading_hours:
            log.warning("無法獲取交易時間信息")
            # 4. 退而求其次，檢查是否有最近成交
            has_recent_trades = self.check_recent_trades()
            self.market_status["is_open"] = has_recent_trades
            if not has_recent_trades:
                self._calculate_next_trading_day(et_time)
            return self.market_status

        # 解析交易時間
        self.market_status["is_open"] = self._parse_trading_hours(
            trading_hours, et_time
        )
        if not self.market_status["is_open"]:
            self._calculate_next_trading_day(et_time)

        return self.market_status

    def _parse_trading_hours(
        self, trading_hours: str, current_time: datetime.datetime
    ) -> bool:
        def _split_ranges(trading_hours: str):
            """把 ';' 與 ',' 皆視為分段分隔，不直接丟棄含 CLOSED 的 segment"""

            for seg in trading_hours.split(";"):
                for rng in seg.split(","):
                    yield rng.strip()

        _RANGE_RE = re.compile(
            r"^(?P<sdate>\d{8}):(?P<stime>\d{4})"
            r"-(?:(?P<edate>\d{8}):)?(?P<etime>\d{4})$"
        )
        today = current_time.strftime("%Y%m%d")
        tz = self.us_eastern  # 已在 __init__ 建立的 pytz 時區

        for rng in _split_ranges(trading_hours):
            if rng.endswith("CLOSED"):
                continue  # 只跳過這一小段

            m = _RANGE_RE.match(rng)
            if not m or m["sdate"] != today:
                continue

            # 正確建立 aware datetime，避免 –04:56 偏移
            start_dt = tz.localize(
                datetime.datetime.strptime(m["sdate"] + m["stime"], "%Y%m%d%H%M")
            )
            end_dt = tz.localize(
                datetime.datetime.strptime(
                    (m["edate"] or m["sdate"]) + m["etime"], "%Y%m%d%H%M"
                )
            )

            if start_dt <= current_time < end_dt:
                return True
        return False

    # Add these new methods for position handling
    def reqPositions(self):
        """請求所有帳戶持倉數據"""
        if not self.isConnected():
            log.warning("無法請求艙位數據 - 未連接")
            return False

        # Reset positions collection
        self._positions = []
        self._positions_completed.clear()

        # Request positions
        super().reqPositions()
        return True

    def position(self, account: str, contract: Contract, pos: float, avgCost: float):
        """處理持倉更新回調"""
        # Store all position data
        position_data = {
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
            "tradingClass": contract.tradingClass,
            "multiplier": contract.multiplier,
        }

        self._positions.append(position_data)
        log.debug(f"收到艙位更新: {contract.symbol} {pos}")

    def positionEnd(self):
        """艙位數據接收完成信號"""
        log.info(f"艙位數據接收完畢，共 {len(self._positions)} 筆")
        self._positions_completed.set()

    def getPositions(
        self, timeout: float = 10.0, refresh: bool = True
    ) -> List[Dict[str, Any]]:
        """獲取所有持倉數據

        Parameters
        ----------
        timeout : float
            最長等待時間（秒）
        refresh : bool
            是否請求新數據，否則返回緩存數據

        Returns
        -------
        List[Dict[str, Any]]
            持倉數據列表
        """
        if not self.isConnected():
            log.warning("無法獲取艙位數據 - 未連接")
            return []

        if refresh:
            # Reset and request new data
            self._positions = []
            self._positions_completed.clear()
            super().reqPositions()

            # Wait with timeout
            self._positions_completed.wait(timeout)

            if not self._positions_completed.is_set():
                log.warning(f"獲取艙位數據超時 ({timeout}秒)")

        return self._positions

    def cancelPositions(self):
        """取消持倉數據訂閱"""
        if not self.isConnected():
            log.warning("無法取消艙位訂閱 - 未連接")
            return False

        super().cancelPositions()
        return True

    def req_contract_details_blocking(self, contract: Contract, timeout: float = 5.0):
        """
        同步封裝：回傳 list[ContractDetails]。
        需要先在 __init__() 裡 self.contract_details = []
        """
        self.contract_details.clear()
        self.reqContractDetails(9001, contract)
        t0 = time.time()
        while time.time() - t0 < timeout and not self.contract_details:
            time.sleep(0.05)
        return self.contract_details

    def contractDetails(self, reqId, details):
        self.contract_details.append(details)
