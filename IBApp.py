import math
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

log = logging.getLogger(__name__)


class IBApp(EWrapper, EClient):
    """
    封裝 IB API：
    - 維持與 TWS/Gateway 的連線
    - 提供 snapshot() 與長駐 subscribe() 快取
    - 提供市場狀態檢查（交易時間 / 最近成交）
    - 提供 Positions 取得
    """

    # -------------- tick 欄位對映（與原版一致）--------------
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
        66: "bid",  # Delayed Bid
        67: "ask",  # Delayed Ask
        68: "last",  # Delayed Last
        69: "bid_size",  # Delayed Bid Size
        70: "ask_size",  # Delayed Ask Size
        71: "last_size",  # Delayed Last Size
        72: "high",  # Delayed High
        73: "low",  # Delayed Low
        74: "volume",  # Delayed Volume
        75: "prev_close",  # Delayed Close（昨收）
    }

    # -------------- 生命週期 --------------
    def __init__(self):
        EClient.__init__(self, self)

        # 握手/ID
        self.ready = threading.Event()
        self._id_lock = threading.Lock()
        self.req_id = 1

        # 市場資料/狀態
        self.tickers: Dict[int, Dict[str, Any]] = {}
        self._stream_key_map: Dict[int, str] = {}
        self._stream_data: Dict[str, Dict[str, Any]] = {}
        self.market_status = {"is_open": False, "next_open": None, "last_check": None}

        # 回傳資料隊列
        self.contract_details: List[ContractDetails] = []
        self.contract_details_queue = deque()
        self.contract_details_available = threading.Event()
        self.current_time_queue = deque()
        self.current_time_available = threading.Event()
        self.historical_data_queue = deque()
        self.historical_data_available = threading.Event()
        self.historical_data_end_available = threading.Event()

        # 時區
        self.us_eastern = pytz.timezone("US/Eastern")

        # 持倉
        self._positions: List[Dict[str, Any]] = []
        self._positions_completed = threading.Event()

        # 維持原行為：啟動即要求 delayed data 類型（3）
        # （若換到有即時行情的帳戶，可改 1；這裡不改動行為）
        self.reqMarketDataType(3)

    # -------------- 工具：安全取得下一個 reqId --------------
    def _next_rid(self) -> int:
        with self._id_lock:
            rid = self.req_id
            self.req_id += 1
            return rid

    # -------------- 握手完成 --------------
    def nextValidId(self, oid: int):
        # 維持既有行為：更新 req_id、設 ready
        with self._id_lock:
            if oid > self.req_id:
                self.req_id = oid
        self.ready.set()

    # -------------- 錯誤處理 --------------
    def error(
        self,
        reqId: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson: str = "",
        *args,
    ):
        # 與原有邏輯一致：特定代碼降噪，其餘 Warning
        if errorCode in (2104, 2106, 2158, 321):
            log.debug("IB ERR %s(%s): %s", errorCode, reqId, errorString)
            return
        log.warning("IB ERR %s(%s): %s", errorCode, reqId, errorString)

    # -------------- 合約細節回調 --------------
    def contractDetails(self, reqId: int, details: ContractDetails):
        self.contract_details.append(details)
        self.contract_details_queue.append(details)

    def contractDetailsEnd(self, reqId):
        self.contract_details_available.set()

    # -------------- 伺服器時間回調 --------------
    def currentTime(self, server_time):
        self.current_time_queue.append(server_time)
        self.current_time_available.set()

    # -------------- 歷史資料回調 --------------
    def historicalData(self, reqId, bar: BarData):
        self.historical_data_queue.append(bar)

    def historicalDataEnd(self, reqId, start, end):
        self.historical_data_available.set()
        self.historical_data_end_available.set()

    # -------------- Streaming market-data --------------
    def subscribe(self, con: Contract, is_opt: bool, key: str) -> int:
        """長駐訂閱一檔合約，最新值會寫入 _stream_data[key]"""
        rid = self._next_rid()
        tick_list = TICK_LIST_OPT if is_opt else ""
        self._stream_key_map[rid] = key
        self.reqMktData(rid, con, tick_list, False, False, [])
        return rid

    def unsubscribe(self, rid: int):
        if rid in self._stream_key_map:
            self.cancelMktData(rid)
            key = self._stream_key_map.pop(rid)
            self._stream_data.pop(key, None)

    def get_stream_data(self, key: str) -> Dict[str, Any]:
        return self._stream_data.get(key, {})

    # ---- Tick handlers（維持原始鍵值結構與行為）----
    def tickPrice(self, reqId, field, price, _):
        if field in (1, 2, 4, 6, 7, 9, 14, 37, 66, 67, 68, 72, 73, 75, 76):
            if price is None or price < 0:
                return
        key = self.FIELD_MAP.get(field, f"p{field}")
        self.tickers.setdefault(reqId, {})[key] = price
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

    def tickOptionComputation(self, reqId, *args):
        # ---- 解析 API 參數（Python 原生 API 9.7+ 的順序）----
        # reqId, field, tickAttrib, iv, delta, optPrice, pvDiv, gamma, vega, theta, undPx
        field = args[0] if len(args) > 0 else None
        # tickAttrib = args[1]  # 如需可用，但目前不影響你邏輯
        iv = args[2] if len(args) > 2 else None
        delta = args[3] if len(args) > 3 else None
        gamma = args[6] if len(args) > 6 else None
        vega = args[7] if len(args) > 7 else None
        theta = args[8] if len(args) > 8 else None
        undPx = args[9] if len(args) > 9 else None

        # ---- 將 -1 / 非數值 視為「無效」不覆蓋舊值 ----
        def _clean(x):
            if x is None:
                return None
            if isinstance(x, (int, float)):
                if x == -1 or not math.isfinite(x):
                    return None
            return x

        iv = _clean(iv)
        delta = _clean(delta)
        gamma = _clean(gamma)
        vega = _clean(vega)
        theta = _clean(theta)
        undPx = _clean(undPx)

        # ---- live 與 delayed 的 tickType 合併（10-13 與 80-83）----
        # 10/11/12/13 = Bid/Ask/Last/Model Option Computation（即時）
        # 80/81/82/83 = Delayed Bid/Ask/Last/Model Option Computation（延遲）
        # 官方 tick types 對照表：66–76（延遲價量）與 80–83（延遲 Greeks）。:contentReference[oaicite:0]{index=0}
        side = {
            10: "bid",
            11: "ask",
            12: "last",
            13: "model",
            80: "bid",
            81: "ask",
            82: "last",
            83: "model",
        }.get(field)

        # ---- 只在新值有效時才覆蓋，避免被 -1 蓋掉 ----
        bucket = self.tickers.setdefault(reqId, {})
        if iv is not None:
            bucket["iv"] = iv
        if delta is not None:
            bucket["delta"] = delta
        if gamma is not None:
            bucket["gamma"] = gamma
        if vega is not None:
            bucket["vega"] = vega
        if theta is not None:
            bucket["theta"] = theta
        if undPx is not None:
            bucket["undPx"] = undPx

        # （可選）保留 side 明細，若你之後要比較 bid/ask/last 計算版本
        if side:
            if iv is not None:
                bucket[f"{side}_iv"] = iv
            if delta is not None:
                bucket[f"{side}_delta"] = delta
            if gamma is not None:
                bucket[f"{side}_gamma"] = gamma
            if vega is not None:
                bucket[f"{side}_vega"] = vega
            if theta is not None:
                bucket[f"{side}_theta"] = theta

        # ---- 同步到 stream 快取（維持你原本行為）----
        if reqId in self._stream_key_map:
            k = self._stream_key_map[reqId]
            out = self._stream_data.setdefault(k, {})
            for key in ("iv", "delta", "gamma", "vega", "theta", "undPx"):
                if key in bucket:
                    out[key] = bucket[key]
            if side:
                for key in (
                    f"{side}_iv",
                    f"{side}_delta",
                    f"{side}_gamma",
                    f"{side}_vega",
                    f"{side}_theta",
                ):
                    if key in bucket:
                        out[key] = bucket[key]

    # -------------- 單檔 Snapshot（行為不變）--------------
    def snapshot(self, con: Contract, is_opt: bool) -> Dict[str, Any]:
        rid = self._next_rid()
        tick_list = TICK_LIST_OPT if is_opt else ""
        self.reqMktData(rid, con, tick_list, False, False, [])
        t0 = time.monotonic()
        while time.monotonic() - t0 < TIMEOUT:
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

    # -------------- 市場狀態 --------------
    def _calculate_next_trading_day(self, et_now: datetime.datetime) -> None:
        tz = self.us_eastern
        d = et_now + datetime.timedelta(days=1)
        while d.weekday() >= 5:
            d += datetime.timedelta(days=1)
        next_open = tz.localize(datetime.datetime(d.year, d.month, d.day, 9, 30))
        self.market_status["next_open"] = next_open

    def is_regular_market_open(self) -> bool:
        server_time = self.get_server_time()
        if not server_time:
            return self.market_status.get("is_open", True)
        et = server_time.astimezone(self.us_eastern)
        if et.weekday() >= 5:
            return False
        t = et.time()
        return datetime.time(9, 30) <= t < datetime.time(16, 0)

    _last_server_time: Optional[datetime.datetime] = None
    _server_time_ts: float = 0.0  # monotonic 秒
    _server_time_extrapolate_max = 6 * 3600  # 允許外推的最長秒數（例如 6 小時）

    def get_server_time(
        self,
        retry: int = 3,
        timeout: float = 5.0,
        extrapolate: bool = True,
        soft_cache_age: float = 90.0,
        hard_cache_age: Optional[float] = None,
    ) -> Optional[datetime.datetime]:
        """
        取得伺服器時間：
        - 嘗試 N 次；成功則更新 _last_server_time 與 _server_time_ts。
        - 失敗時：
          a) 若距上次成功 < soft_cache_age（預設 90s），直接回舊值；
          b) 若 extrapolate=True 且距上次成功 < _server_time_extrapolate_max，用 monotonic 外推；
          c) 若指定 hard_cache_age 且未超過，也可回舊值（可當額外保險）。
        """
        for _ in range(retry):
            self.current_time_available.clear()
            self.current_time_queue.clear()
            self.reqCurrentTime()
            if self.current_time_available.wait(timeout):
                ts = self.current_time_queue.popleft()
                dt = datetime.datetime.fromtimestamp(ts, tz=pytz.UTC)
                self._last_server_time = dt
                self._server_time_ts = time.monotonic()
                return dt
            time.sleep(0.2)

        if self._last_server_time:
            age = time.monotonic() - self._server_time_ts
            # a) 短期快取：直接回舊值
            if age < soft_cache_age:
                return self._last_server_time
            # b) 外推（關鍵修補）：用單調時鐘推進伺服器時間
            if extrapolate and age < self._server_time_extrapolate_max:
                return self._last_server_time + datetime.timedelta(seconds=age)
            # c) 可選：硬快取上限
            if hard_cache_age is not None and age < hard_cache_age:
                return self._last_server_time

        return None
    
    def check_recent_trades(self, symbol: str = "SPY") -> bool:
        self.historical_data_available.clear()
        self.historical_data_end_available.clear()
        self.historical_data_queue.clear()

        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"

        req_id = self._next_rid()
        end_time = ""
        duration = "300 S"
        bar_size = "1 min"
        self.reqHistoricalData(
            req_id, contract, end_time, duration, bar_size, "TRADES", 1, 1, False, []
        )

        if not self.historical_data_end_available.wait(10):
            return False
        if not self.historical_data_queue:
            return False

        recent_bar = self.historical_data_queue[-1]
        return recent_bar.volume > 0

    def is_market_open(self) -> Dict[str, Any]:
        now = datetime.datetime.now()
        if (
            self.market_status["last_check"]
            and (now - self.market_status["last_check"]).total_seconds() < 60
        ):
            return self.market_status

        self.market_status["last_check"] = now
        server_time = self.get_server_time(extrapolate=True, soft_cache_age=90.0)

        # 允許短期失聯時維持開市（sticky-open）
        STICKY_GRACE = 15 * 60  # 15 分鐘
        was_open = self.market_status.get("is_open", False)

        if not server_time:
            if was_open:
                # 若上一狀態為開市，且距離上次成功取時不超過 STICKY_GRACE，維持開市
                age = time.monotonic() - self._server_time_ts if self._server_time_ts else 1e9
                if age < STICKY_GRACE:
                    log.warning("無法獲取伺服器時間 - sticky-open 生效，暫時視為仍在交易")
                    return self.market_status
            log.warning("無法獲取伺服器時間且超過黏著寬限 → 視為休市")
            self.market_status["is_open"] = False
            if self._last_server_time:
                self._calculate_next_trading_day(
                    self._last_server_time.astimezone(self.us_eastern)
                )
            return self.market_status

        et_time = server_time.astimezone(self.us_eastern)

        # 週末直接休市
        if et_time.weekday() >= 5:
            self.market_status["is_open"] = False
            self._calculate_next_trading_day(et_time)
            return self.market_status

        # 優先用 SPY 的交易時間（快取/查詢）
        spy_contract = Contract()
        spy_contract.symbol = "SPY"
        spy_contract.secType = "STK"
        spy_contract.exchange = "SMART"
        spy_contract.currency = "USD"

        trading_hours = self.get_contract_trading_hours(spy_contract)
        if not trading_hours:
            log.warning("無法獲取交易時間信息，改用最近成交偵測")
            has_recent_trades = self.check_recent_trades()
            self.market_status["is_open"] = has_recent_trades
            if not has_recent_trades:
                self._calculate_next_trading_day(et_time)
            return self.market_status

        is_open_now = self._parse_trading_hours(trading_hours, et_time)

        # 黏著邏輯：若剛好在一般收盤臨界（例如 16:00 附近）避免抖動
        if was_open and not is_open_now:
            # 收盤後 5 分鐘內，仍用「有無成交」確認一次，避免誤判
            if et_time.time() <= datetime.time(16, 5):
                if self.check_recent_trades():
                    is_open_now = True

        self.market_status["is_open"] = is_open_now
        if not is_open_now:
            self._calculate_next_trading_day(et_time)
        return self.market_status

    def get_contract_trading_hours(self, contract: Contract) -> Optional[str]:
        self.contract_details_available.clear()
        self.contract_details_queue.clear()
        rid = self._next_rid()
        self.reqContractDetails(rid, contract)
        if not self.contract_details_available.wait(10):
            return None
        if not self.contract_details_queue:
            return None
        details = self.contract_details_queue[0]
        return details.tradingHours

    def _parse_trading_hours(
        self, trading_hours: str, current_time: datetime.datetime
    ) -> bool:
        def _split_ranges(s: str):
            for seg in s.split(";"):
                for rng in seg.split(","):
                    yield rng.strip()

        _RANGE_RE = re.compile(
            r"^(?P<sdate>\d{8}):(?P<stime>\d{4})-(?:(?P<edate>\d{8}):)?(?P<etime>\d{4})$"
        )
        today = current_time.strftime("%Y%m%d")
        tz = self.us_eastern

        for rng in _split_ranges(trading_hours):
            if rng.endswith("CLOSED"):
                continue
            m = _RANGE_RE.match(rng)
            if not m or m["sdate"] != today:
                continue
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

    # -------------- Positions （行為不變）--------------
    def reqPositions(self):
        if not self.isConnected():
            log.warning("無法請求艙位數據 - 未連接")
            return False
        self._positions = []
        self._positions_completed.clear()
        super().reqPositions()
        return True

    def position(self, account: str, contract: Contract, pos: float, avgCost: float):
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
                "tradingClass": contract.tradingClass,
                "multiplier": contract.multiplier,
            }
        )
        log.debug(f"收到艙位更新: {contract.symbol} {pos}")

    def positionEnd(self):
        log.info(f"艙位數據接收完畢，共 {len(self._positions)} 筆")
        self._positions_completed.set()

    def getPositions(
        self, timeout: float = 10.0, refresh: bool = True
    ) -> List[Dict[str, Any]]:
        if not self.isConnected():
            log.warning("無法獲取艙位數據 - 未連接")
            return []
        if refresh:
            self._positions = []
            self._positions_completed.clear()
            super().reqPositions()
            self._positions_completed.wait(timeout)
            if not self._positions_completed.is_set():
                log.warning(f"獲取艙位數據超時 ({timeout}秒)")
        return self._positions

    def cancelPositions(self):
        if not self.isConnected():
            log.warning("無法取消艙位訂閱 - 未連接")
            return False
        super().cancelPositions()
        return True

    # -------------- Contract details（同步封裝，行為等價）--------------
    def req_contract_details_blocking(self, contract: Contract, timeout: float = 5.0):
        self.contract_details.clear()
        rid = self._next_rid()
        self.reqContractDetails(rid, contract)
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout and not self.contract_details:
            time.sleep(0.05)
        return self.contract_details
