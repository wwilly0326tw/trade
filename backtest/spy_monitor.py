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
from typing import Dict, Any, List
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
import logging
import logging.handlers
from pathlib import Path

# ---------------- 全域設定 ----------------
HOST = "127.0.0.1"
PORT = 4002  # Paper: 7497 / 4002
CID = random.randint(1000, 9999)
TICK_LIST_OPT = "106"  # 要求 Option Greeks (IV / Δ)
TIMEOUT = 5.0  # 單檔行情等待秒數
CHECK_INTERVAL = 10  # 監控輪詢秒數
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
    min_dte: int = 36


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

    # --- 握手完成
    def nextValidId(self, oid: int):
        self.req_id = max(self.req_id, oid)
        self.ready.set()

    def error(self, reqId, code, msg, _=""):
        if code in (2104, 2106, 2158):  # 市場資料伺服器通知
            return
        print(f"ERR {code}: {msg}")

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
        self.current_date = datetime.date.today()

    def _dte(self, expiry: str) -> int:
        expire = datetime.datetime.strptime(expiry, "%Y%m%d").date()
        return (expire - datetime.date.today()).days

    def first_snap(self):
        print("首次快照 …")
        # 1️⃣ 記錄每檔 premium
        for k, c in self.cfgs.items():
            self.init_price[k] = c.premium
            print(f"{k} premium = {c.premium}")
        # 2️⃣ 取得昨日收盤
        snap = self.app.snapshot(self.spy_con, is_opt=False)
        self.spy_prev_close = snap.get("close") or snap.get("price")
        print(f"SPY 昨收 {self.spy_prev_close}")

    def _check_alert_deduplication(self):
        """檢查是否需要重置今日警報紀錄（日期變更時）"""
        today = datetime.date.today()
        if today > self.current_date:
            print(f"日期變更: {self.current_date} → {today}，重置警報紀錄")
            self.sent_alerts.clear()
            self.current_date = today

    def loop(self):
        while True:
            # 檢查是否需要重置警報紀錄（新的一天）
            self._check_alert_deduplication()

            now = datetime.datetime.now().strftime("%H:%M:%S")
            log.info(f"[{now}] 開始檢查合約狀態")
            alerts = []

            # --- SPY 價格 / 跳空警報
            spy_snap = self.app.snapshot(self.spy_con, is_opt=False)
            spy_px = spy_snap.get("price")
            if spy_px and self.spy_prev_close:
                gap = (spy_px - self.spy_prev_close) / self.spy_prev_close
                if abs(gap) >= 0.03:
                    alert_msg = generate_detailed_alert(
                        "SPY", "gap", gap, ContractConfig("SPY", "", 0, "")
                    )
                    alerts.append(alert_msg)
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
                    alert_msg = generate_detailed_alert(
                        key,
                        "profit",
                        pct,
                        c,
                        {"target": self.rule.profit_target, "price": price},
                    )
                    alerts.append(alert_msg)
                    log.warning(f"{key} 收益={pct:.1%} 已達目標")

                # DTE
                if dte <= self.rule.min_dte:
                    alert_msg = generate_detailed_alert(
                        key, "dte", dte, c, {"min_dte": self.rule.min_dte}
                    )
                    alerts.append(alert_msg)
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
                        self.sent_alerts[alert_id] = self.current_date
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


# 設置日誌系統（在 main 函數開頭）
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

    # 設置全域變數，方便其他模組使用
    global log
    log = logger

    log.info("日誌系統已初始化，檔案將每兩天輪換一次")
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

    # Create a unique identifier for this specific alert
    unique_id = f"{alert_type}_{key}_{datetime.datetime.now().strftime('%Y%m%d')}"

    return full_message, unique_id


if __name__ == "__main__":
    main()
