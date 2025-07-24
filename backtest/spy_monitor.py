# spy_option_alert_loop_ibapi.py
"""
SPY 選擇權警報系統（循環版 ‑ ibapi）
------------------------------------------------
* 讀取 **spy_contracts_config.json** 內所有 PUT / CALL 合約；每 `CHECK_INTERVAL` 秒更新行情。
* 監控條件：Delta、收益率（相對 premium；SELL 價格愈低愈正）、剩餘 DTE、以及 SPY 跳空 ±3 %。
* `DEBUG=True` 會完整列印回傳 Tick 字典，便於檢查缺失欄位／IV 為 0 的原因。
* 兼容 `tickOptionComputation` 多版本參數（API ≥ v10.19）。
"""
from __future__ import annotations

import json, os, sys, time, threading, random, signal, datetime, requests
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Tuple
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract, ContractDetails
from ibapi.common import BarData
import logging
import logging.handlers
from pathlib import Path
from collections import deque
import pytz

# ---------------- 全域設定 ----------------
HOST = "127.0.0.1"
PORT = 4002  # Paper: 7497 / 4002
CID = random.randint(1000, 9999)
TICK_LIST_OPT = "106"  # 要求 Option Greeks (IV / Δ)
TIMEOUT = 5.0  # 單檔行情等待秒數
CHECK_INTERVAL = 60  # 監控輪詢秒數
DEBUG = False  # True 時打印完整 Tick

# LINE Messaging API ── 使用者提供的長期權杖（若環境變數未設則採用此值）
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or (
    "jpDKXxch8e/m30Ll4irnKE5Rcwv8bNslQ0f4H4DpyMmQ4dWJNOuWDN/VUC29C7iD/"
    "XWjDFrlRMZHAXgbdNwaUTGzpoO2sUSwSpwUonpIRTZ6TDZdsIfyz/G6Xf3RaqAsDbYti"
    "+NKTkFPR6XHDTL5jwdB04t89/1O/w1cDnyilFU="
)
LINE_ENDPOINT = "https://api.line.me/v2/bot/message/broadcast"
LINE_HEADERS = {
    "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}


def line_push(msg: str):
    """以 Broadcast 方式推播文字訊息到所有已加入 Bot 的聊天室。"""
    if not CHANNEL_ACCESS_TOKEN:
        print("[WARN] 未設定 LINE CHANNEL TOKEN，警報只會顯示在終端機。")
        return
    payload = {"messages": [{"type": "text", "text": msg[:1000]}]}
    try:
        r = requests.post(LINE_ENDPOINT, headers=LINE_HEADERS, json=payload, timeout=5)
        if r.status_code != 200:
            print(f"[ERR] LINE Broadcast {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        print(f"[ERR] LINE Broadcast 例外: {exc}")


# ---------------- 資料類別 ----------------
@dataclass
class StrategyConfig:
    delta_threshold: float = 0.30
    profit_target: float = 0.50  # 50%
    min_dte: int = 21


@dataclass
class ContractConfig:
    symbol: str
    expiry: str
    strike: float
    right: str  # PUT / CALL
    exchange: str = "SMART"
    currency: str = "USD"
    delta: float = 0.0
    premium: float = 0.0
    action: str = "SELL"  # SELL / BUY

    def to_ib(self) -> Contract:
        c = Contract()
        c.symbol = self.symbol
        c.secType = "OPT"
        c.exchange = self.exchange
        c.currency = self.currency
        c.right = self.right
        c.strike = self.strike
        c.lastTradeDateOrContractMonth = self.expiry
        return c


# ---------------- 配置讀取 ----------------
class ConfigManager:
    def __init__(self, path: str = "spy_contracts_config.json"):
        self.path = path

    def load(self) -> Dict[str, ContractConfig]:
        if not os.path.exists(self.path):
            raise FileNotFoundError(self.path)
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {k: ContractConfig(**v) for k, v in raw.items()}


# ---------------- IB 客戶端 ---------------
class IBApp(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.ready = threading.Event()
        self.req_id = 1
        self.tickers: Dict[int, Dict[str, Any]] = {}

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

    # --- 握手完成
    def nextValidId(self, oid: int):
        self.req_id = max(self.req_id, oid)
        self.ready.set()

    def error(self, reqId, code, msg, _=""):
        if code in (2104, 2106, 2158):  # 市場資料伺服器通知
            return
        print(f"ERR {code}: {msg}")

    # --- 市場狀態相關回調
    def contractDetails(self, reqId, details: ContractDetails):
        self.contract_details_queue.append(details)

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

    def tickSize(self, reqId, field, size):
        self.tickers.setdefault(reqId, {})[f"size_{field}"] = size

    def tickGeneric(self, reqId, field, value):
        self.tickers.setdefault(reqId, {})[f"g{field}"] = value

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

    def get_server_time(self) -> Optional[datetime.datetime]:
        """取得 IB 伺服器時間"""
        self.current_time_available.clear()
        self.current_time_queue.clear()

        self.reqCurrentTime()

        if not self.current_time_available.wait(5):
            return None

        if not self.current_time_queue:
            return None

        # IB 返回的是 UTC 時間戳
        timestamp = self.current_time_queue.popleft()
        server_time = datetime.datetime.fromtimestamp(timestamp, tz=pytz.UTC)
        return server_time

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
        if not server_time:
            log.warning("無法獲取伺服器時間")
            self.market_status["is_open"] = False
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
        """解析 IB 返回的交易時間字符串，判斷當前是否在交易時段"""
        try:
            # 範例：20250724:0930-1600;20250725:0930-1600
            today_str = current_time.strftime("%Y%m%d")

            # 尋找今天的交易時段
            segments = trading_hours.split(";")
            for segment in segments:
                if not segment:
                    continue

                parts = segment.split(":")
                if len(parts) != 2:
                    continue

                date_str, hours = parts
                if date_str != today_str:
                    continue

                # 找到今天的時段
                time_ranges = hours.split(",")
                for time_range in time_ranges:
                    if "-" not in time_range:
                        continue

                    start_str, end_str = time_range.split("-")

                    # 解析時間
                    start_hour = int(start_str[0:2])
                    start_minute = int(start_str[2:4])
                    end_hour = int(end_str[0:2])
                    end_minute = int(end_str[2:4])

                    # 創建時間對象
                    start_time = current_time.replace(
                        hour=start_hour, minute=start_minute, second=0, microsecond=0
                    )
                    end_time = current_time.replace(
                        hour=end_hour, minute=end_minute, second=0, microsecond=0
                    )

                    # 檢查當前時間是否在範圍內
                    if start_time <= current_time < end_time:
                        return True

            return False
        except Exception as e:
            log.error(f"解析交易時間時發生錯誤: {e}")
            return False

    def _calculate_next_trading_day(self, current_time: datetime.datetime) -> None:
        """計算下一個交易日的開盤時間"""
        try:
            # 以當前時間為基礎，向後找 10 天，找到第一個有交易時段的日期
            test_date = current_time
            for _ in range(10):  # 往後查找 10 天
                test_date = test_date + datetime.timedelta(days=1)

                # 跳過週末
                if test_date.weekday() >= 5:
                    continue

                test_contract = Contract()
                test_contract.symbol = "SPY"
                test_contract.secType = "STK"
                test_contract.exchange = "SMART"
                test_contract.currency = "USD"

                trading_hours = self.get_contract_trading_hours(test_contract)
                if not trading_hours:
                    continue

                # 查找該日期的開盤時間
                test_date_str = test_date.strftime("%Y%m%d")
                segments = trading_hours.split(";")

                for segment in segments:
                    if not segment:
                        continue

                    parts = segment.split(":")
                    if len(parts) != 2:
                        continue

                    date_str, hours = parts
                    if date_str != test_date_str:
                        continue

                    # 找到下一個交易日
                    time_ranges = hours.split(",")
                    for time_range in time_ranges:
                        if "-" not in time_range:
                            continue

                        start_str = time_range.split("-")[0]

                        # 解析時間
                        start_hour = int(start_str[0:2])
                        start_minute = int(start_str[2:4])

                        # 設定開盤時間
                        market_open = test_date.replace(
                            hour=start_hour,
                            minute=start_minute,
                            second=0,
                            microsecond=0,
                        )
                        self.market_status["next_open"] = market_open
                        return

            # 如果找不到，設置為 None
            self.market_status["next_open"] = None

        except Exception as e:
            log.error(f"計算下一個交易日時發生錯誤: {e}")
            self.market_status["next_open"] = None


# ---------------- 警報引擎 ----------------
class AlertEngine:
    def __init__(
        self, app: IBApp, cfgs: Dict[str, ContractConfig], rule: StrategyConfig
    ):
        self.app = app
        self.cfgs = cfgs
        self.rule = rule
        self.init_price: Dict[str, float] = {}

        # SPY 股票合約
        self.spy_con = Contract()
        self.spy_con.symbol = "SPY"
        self.spy_con.secType = "STK"
        self.spy_con.exchange = "SMART"
        self.spy_con.currency = "USD"
        self.spy_prev_close: float | None = None

        # 記錄已發送的警報及發送日期
        self.sent_alerts: Dict[str, datetime.date] = {}
        self.trading_date = datetime.date.today()
        self.market_closed_notified = False
        self.last_market_status_check = datetime.datetime.min

    def _dte(self, expiry: str) -> int:
        expire = datetime.datetime.strptime(expiry, "%Y%m%d").date()
        return (expire - datetime.date.today()).days

    def first_snap(self):
        log.info("獲取首次快照資料 ...")

        # 等待市場開盤
        self._wait_for_market_open()

        # 1️⃣ 記錄每檔 premium
        for k, c in self.cfgs.items():
            self.init_price[k] = c.premium
            log.info(f"{k} premium = {c.premium}")

        # 2️⃣ 取得昨日收盤
        snap = self.app.snapshot(self.spy_con, is_opt=False)
        self.spy_prev_close = snap.get("close") or snap.get("price")
        log.info(f"SPY 昨收 {self.spy_prev_close}")

    def _wait_for_market_open(self) -> None:
        """在系統啟動時如果市場尚未開盤，等待開盤"""
        market_status = self.app.is_market_open()

        if market_status["is_open"]:
            log.info("市場已開盤，開始監控")
            return

        next_open = market_status["next_open"]
        if next_open:
            wait_seconds = (
                next_open
                - datetime.datetime.now(pytz.UTC).astimezone(self.app.us_eastern)
            ).total_seconds()
            if wait_seconds > 0:
                log.info(
                    f"市場尚未開盤，等待開盤時間: {next_open.strftime('%Y-%m-%d %H:%M:%S %Z')}"
                )
                log.info(f"等待約 {wait_seconds/60:.1f} 分鐘...")

                # 如果開盤時間很遠，先休眠一段時間再檢查
                sleep_time = min(wait_seconds, 300)  # 最多休眠 5 分鐘
                time.sleep(sleep_time)
                self._wait_for_market_open()  # 遞迴檢查
            else:
                log.info("市場即將開盤，等待 30 秒確認開盤狀態...")
                time.sleep(30)  # 等待 30 秒確認開盤
                self._wait_for_market_open()  # 遞迴檢查
        else:
            log.warning("無法確定下次開盤時間，假設市場已開盤")

    def _check_market_status(self) -> bool:
        """檢查市場狀態，返回市場是否開盤"""
        # 限制檢查頻率
        now = datetime.datetime.now()
        if (
            now - self.last_market_status_check
        ).total_seconds() < 300:  # 5分鐘內不重複檢查
            return self.app.market_status["is_open"]

        self.last_market_status_check = now
        market_status = self.app.is_market_open()

        # 檢查交易日是否改變
        if market_status["is_open"]:
            current_date = datetime.datetime.now().date()
            if current_date != self.trading_date:
                log.info(f"交易日變更: {self.trading_date} → {current_date}")
                self.sent_alerts.clear()  # 清空已發送的警報
                self.trading_date = current_date
                self.market_closed_notified = False

        if not market_status["is_open"] and not self.market_closed_notified:
            next_open = market_status["next_open"]
            if next_open:
                log.info(
                    f"市場已休市，下次開盤時間: {next_open.strftime('%Y-%m-%d %H:%M:%S %Z')}"
                )
            else:
                log.info("市場已休市，無法確定下次開盤時間")
            self.market_closed_notified = True

        return market_status["is_open"]

    def loop(self):
        while True:
            # 檢查市場狀態
            is_market_open = self._check_market_status()

            if not is_market_open:
                # 休市時降低檢查頻率
                time.sleep(CHECK_INTERVAL * 5)
                continue

            # 重置通知標記，因為已進入交易時段
            self.market_closed_notified = False

            now = datetime.datetime.now().strftime("%H:%M:%S")
            log.info(f"[{now}] 開始檢查合約狀態")
            alerts = []

            # --- SPY 價格 / 跳空警報
            spy_snap = self.app.snapshot(self.spy_con, is_opt=False)
            spy_px = spy_snap.get("price")
            if spy_px and self.spy_prev_close:
                gap = (spy_px - self.spy_prev_close) / self.spy_prev_close
                if abs(gap) >= 0.03:
                    alert_msg, alert_id = generate_detailed_alert(
                        "SPY", "gap", gap, ContractConfig("SPY", "", 0, "")
                    )
                    alerts.append((alert_msg, alert_id))
                    log.warning(f"偵測到 SPY 跳空: {gap:.1%}")
            log.info(f"SPY Px={(f'{spy_px:.2f}' if spy_px else 'NA')}")

            # --- 逐檔選擇權
            for key, c in self.cfgs.items():
                snap = self.app.snapshot(c.to_ib(), True)
                price = snap["price"]
                delta = snap.get("delta")
                iv = snap.get("iv")
                if price is None or delta is None:
                    log.warning(f"{key}: 無法取得完整資料")
                    continue

                dte = self._dte(c.expiry)
                delta_abs = abs(delta)

                # Δ 警報
                if delta_abs >= self.rule.delta_threshold:
                    alert_msg, alert_id = generate_detailed_alert(
                        key,
                        "delta",
                        delta_abs,
                        c,
                        {"threshold": self.rule.delta_threshold},
                    )
                    alerts.append((alert_msg, alert_id))
                    log.warning(f"{key} Delta={delta_abs:.3f} 超過閾值")

                # 收益率
                base = c.premium
                pct = (
                    (base - price) / base
                    if c.action.upper() == "SELL"
                    else (price - base) / base
                )
                if pct >= self.rule.profit_target:
                    alert_msg, alert_id = generate_detailed_alert(
                        key,
                        "profit",
                        pct,
                        c,
                        {"target": self.rule.profit_target, "price": price},
                    )
                    alerts.append((alert_msg, alert_id))
                    log.warning(f"{key} 收益={pct:.1%} 已達目標")

                # DTE
                if dte <= self.rule.min_dte:
                    alert_msg, alert_id = generate_detailed_alert(
                        key, "dte", dte, c, {"min_dte": self.rule.min_dte}
                    )
                    alerts.append((alert_msg, alert_id))
                    log.warning(f"{key} DTE={dte} 低於閾值")

                # 記錄詳細資訊
                pct_str = f"{pct:+.1%}"
                delta_diff = f"{delta_abs - abs(c.delta):+.3f}"
                iv_str = f"{iv:.4f}" if iv else "NA"
                log.info(
                    f"{key}: Px={price:.2f} ({pct_str}) Δ={delta_abs:.3f} (ΔΔ={delta_diff}) IV={iv_str} DTE={dte}"
                )

            if alerts:
                log.info("== 觸發警報 ==")
                unique_alerts = []
                for alert_msg, alert_id in alerts:
                    if alert_id not in self.sent_alerts:
                        unique_alerts.append(alert_msg)
                        self.sent_alerts[alert_id] = self.trading_date
                        log.info(f"發送警報: {alert_msg[:50]}...")
                        line_push(alert_msg)
                    else:
                        log.info(f"[重複警報，已忽略] {alert_msg[:50]}...")

                if unique_alerts:
                    log.info(f"已發送 {len(unique_alerts)} 則新警報")
                else:
                    log.info("所有警報今日均已發送過")
            else:
                log.info("✓ 無警報")

            time.sleep(CHECK_INTERVAL)


# ---------------- Main ----------------
def main():
    # 設置日誌系統
    global log
    log = setup_logging()

    log.info("SPY 選擇權監控系統啟動")
    cfgs = ConfigManager().load()
    log.info(f"讀取 {len(cfgs)} 檔合約 → 連線 {HOST}:{PORT}")

    # 連線並開啟事件迴圈
    app = IBApp()
    app.connect(HOST, PORT, CID)
    threading.Thread(target=app.run, daemon=True).start()
    if not app.ready.wait(5):
        log.error("握手逾時，請確認 TWS/Gateway")
        return

    # 建立警報引擎
    rule = StrategyConfig()
    eng = AlertEngine(app, cfgs, rule)
    eng.first_snap()

    # 安全中斷
    def shutdown(sig, _):
        log.info("收到終止信號，正在斷線...")
        app.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # 進入監控迴圈
    try:
        eng.loop()
    finally:
        app.disconnect()


# 設置日誌系統
def setup_logging():
    """設置日誌系統，每兩天輪換一次檔案。"""
    # 確保日誌目錄存在
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    # 主要日誌設置
    logger = logging.getLogger("spy_monitor")
    logger.setLevel(logging.INFO)

    # 檔案處理器 - 每兩天輪換一次
    log_file = log_dir / "spy_monitor.log"
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file, when="D", interval=2, backupCount=10, encoding="utf-8"
    )

    # 終端機處理器
    console_handler = logging.StreamHandler()

    # 日誌格式
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    # 添加處理器
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    logger.info("日誌系統已初始化，檔案將每兩天輪換一次")
    return logger


# Function to generate more detailed alert messages
def generate_detailed_alert(
    key: str,
    alert_type: str,
    value: float,
    contract: ContractConfig,
    extra_info: dict = None,
) -> tuple[str, str]:  # Return both message and a unique ID
    """生成詳細的警報訊息，包含觸發原因和建議動作。"""
    extra_info = extra_info or {}
    current_date = datetime.datetime.now().strftime("%Y-%m-%d")

    # 基本訊息格式
    if alert_type == "delta":
        emoji = "🚨"
        detail = (
            f"{key} Delta={value:.3f} 已超過閾值 {extra_info.get('threshold', 0.3):.2f}"
        )
        action = f"建議關注 {contract.symbol} {contract.strike}{'P' if contract.right=='PUT' else 'C'} 風險增加"

    elif alert_type == "profit":
        emoji = "💰"
        detail = (
            f"{key} 收益={value:.1%} 已達目標 {extra_info.get('target', 0.5):.1%}"
            f" ({contract.action} {contract.premium:.2f}→{extra_info.get('price', 0):.2f})"
        )
        action = f"可考慮{'買回' if contract.action=='SELL' else '賣出'}平倉獲利"

    elif alert_type == "dte":
        emoji = "📅"
        detail = f"{key} 剩餘天數={value}天 低於設定 {extra_info.get('min_dte', 36)}天"
        action = "注意時間價值加速衰減，評估是否調整部位"

    elif alert_type == "gap":
        emoji = "⚡"
        direction = "上漲" if value > 0 else "下跌"
        detail = f"SPY {direction} {abs(value):.1%}，大幅跳空"
        action = (
            f"請密切關注市場波動，{'PUT' if value > 0 else 'CALL'}選擇權可能受影響較大"
        )

    # 組合完整訊息
    full_message = f"{emoji} {current_date}\n" f"{detail}\n" f"{action}"

    # 創建每日唯一的警報識別碼
    trading_date = datetime.datetime.now().strftime("%Y%m%d")
    unique_id = f"{alert_type}_{key}_{trading_date}"

    return full_message, unique_id


if __name__ == "__main__":
    main()
