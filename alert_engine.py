# alert_engine.py
"""
封裝選擇權監控 & 警報邏輯。
可由任何主程式 import 使用，不依賴 CLI。
"""
from __future__ import annotations
import os, json, time, datetime, requests, logging, pytz
from dataclasses import dataclass
from typing import Dict, Any, List, Optional
from ibapi.contract import Contract
from IBApp import IBApp  # ← 直接重用您自訂的 IBApp

log = logging.getLogger(__name__)

# ---------- LINE Push ---------- #
_CHANNEL_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
_LINE_ENDPOINT = "https://api.line.me/v2/bot/message/broadcast"
_LINE_HEADERS = {
    "Authorization": f"Bearer {_CHANNEL_TOKEN}",
    "Content-Type": "application/json",
}


def line_push(msg: str):
    """Broadcast 文字訊息到所有已加入 Bot 的聊天室。"""
    if not _CHANNEL_TOKEN:
        log.warning("未設定 LINE TOKEN，警報僅寫入日誌")
        return
    payload = {"messages": [{"type": "text", "text": msg[:1000]}]}
    try:
        r = requests.post(
            _LINE_ENDPOINT, headers=_LINE_HEADERS, json=payload, timeout=5
        )
        if r.status_code != 200:
            log.error("LINE API %s: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.error("LINE Broadcast 例外: %s", exc)


# ---------- 資料類別 ---------- #
@dataclass
class StrategyConfig:
    delta_threshold: float = 0.30
    profit_target: float = 0.50  # 50 %
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


# ---------- 配置讀取 ---------- #
class ConfigManager:
    def __init__(self, path: str = "spy_contracts_config.json"):
        self.path = path

    def load(self) -> Dict[str, ContractConfig]:
        if not os.path.exists(self.path):
            raise FileNotFoundError(self.path)
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {k: ContractConfig(**v) for k, v in raw.items()}


# ---------------- 警報引擎 ----------------
class AlertEngine:
    def __init__(
        self, app: IBApp, cfgs: Dict[str, ContractConfig], rule: StrategyConfig
    ):
        self.app = app
        self.cfgs = cfgs
        self.rule = rule
        self.init_price: Dict[str, float] = {}
        self.prev_closes: Dict[str, float] = {}  # 存儲各標的物昨收

        # SPY 股票合約 (兼容性保留)
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

        # For position tracking
        self.last_positions_update = time.time()
        self.use_positions = True  # Flag to use positions or config

        # 建立 streaming 訂閱
        self._subscribe_market_data()

    # ---------- Streaming helpers ----------
    def _subscribe_market_data(self):
        """根據合約配置訂閱市場數據，包括標的股票和選擇權"""
        # 清理之前訂閱的數據 (可選，視 IBApp 實現而定)
        # 需要先獲取所有唯一的標的股票
        underlying_symbols = set()

        # 從合約配置中獲取所有唯一的標的股票符號
        for key, cfg in self.cfgs.items():
            underlying_symbols.add(cfg.symbol)

        # 訂閱所有標的股票
        for symbol in underlying_symbols:
            stk_con = Contract()
            stk_con.symbol = symbol
            stk_con.secType = "STK"
            stk_con.exchange = "SMART"
            stk_con.currency = "USD"
            self.app.subscribe(stk_con, False, symbol)
            log.info(f"已訂閱 {symbol} 股票行情")

        # 訂閱所有選擇權合約
        option_count = 0
        for key, cfg in self.cfgs.items():
            self.app.subscribe(cfg.to_ib(), True, key)
            option_count += 1

        log.info(
            f"已訂閱 {len(underlying_symbols)} 個股票和 {option_count} 個選擇權合約的市場數據"
        )

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

        # 2️⃣ 取得所有標的物的昨日收盤
        self.prev_closes = {}  # 用字典存儲各標的物的昨收

        underlying_symbols = set(cfg.symbol for cfg in self.cfgs.values())

        for symbol in underlying_symbols:
            prev_close = self._get_underlying_prev_close(symbol)
            if prev_close:
                self.prev_closes[symbol] = prev_close
                log.info(f"{symbol} 昨收 {prev_close}")
            else:
                log.warning(f"無法獲取 {symbol} 昨收價格")

        # 向下兼容，保留 spy_prev_close 變數
        self.spy_prev_close = self.prev_closes.get("SPY")

    def _wait_for_prev_close(self, timeout: float = 10.0) -> Optional[float]:
        """等待 streaming 資料填入昨日收盤價，最多 *timeout* 秒。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            data = self.app.get_stream_data("SPY")
            close_val = data.get("prev_close") or data.get("close")
            if close_val:
                return close_val
            time.sleep(0.1)
        return None

    def _wait_for_market_open(self) -> None:
        """在系統啟動時如果市場尚未開盤，等待開盤"""
        # 以迴圈替代遞迴，避免長時間等待導致遞迴層數過深
        while not self.app.is_regular_market_open():
            next_open = self._next_regular_open_time()

            # 若無法取得下一次開盤時間，預設 5 分鐘後再次檢查
            if not next_open:
                log.info("無法計算下一次開盤時間，5 分鐘後重新檢查 ...")
                time.sleep(300)
                continue

            # 估算距離開盤的秒數
            now_et = datetime.datetime.now(pytz.UTC).astimezone(self.app.us_eastern)
            wait_seconds = (next_open - now_et).total_seconds()

            if wait_seconds <= 60:
                # 開盤在即，縮短檢查間隔
                log.info("市場即將開盤，30 秒後再次確認 ...")
                time.sleep(30)
            else:
                log.info(
                    f"市場尚未開盤，預計開盤時間: {next_open.strftime('%Y-%m-%d %H:%M:%S %Z')}"
                )
                log.info(f"將在 {min(wait_seconds/60, 5):.1f} 分鐘後重新檢查 ...")
                # 最多休眠 5 分鐘，避免長時間阻塞
                time.sleep(min(wait_seconds, 300))

        log.info("市場已開盤 (正規時段)，開始監控")

    def _next_regular_open_time(self) -> datetime.datetime:
        """返回下一個正規交易日 09:30 ET 的 datetime (帶時區)。"""
        server_time = self.app.get_server_time()
        if server_time:
            et_now = server_time.astimezone(self.app.us_eastern)
        else:
            # 退而取本地 UTC → ET，較不精準但足矣等待
            et_now = datetime.datetime.now(pytz.UTC).astimezone(self.app.us_eastern)

        # 若今日尚未開盤且為平日
        today_open = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
        if et_now.weekday() < 5 and et_now < today_open:
            return today_open

        # 否則尋找下一個平日
        next_day = et_now + datetime.timedelta(days=1)
        while next_day.weekday() >= 5:  # 跳過週末
            next_day += datetime.timedelta(days=1)

        next_open = next_day.replace(hour=9, minute=30, second=0, microsecond=0)
        return next_open

    def _check_market_status(self) -> bool:
        """檢查市場狀態，返回市場是否開盤"""
        # 限制檢查頻率
        now = datetime.datetime.now()
        if (
            now - self.last_market_status_check
        ).total_seconds() < 300:  # 5分鐘內不重複檢查
            return self.app.market_status["is_open"]

        self.last_market_status_check = now

        # 先取得 IB 判斷的市場狀態 (可能包含盤前/盤後)
        market_status = self.app.is_market_open()

        # 只取正規時段 09:30–16:00 的開盤狀態
        regular_open = self.app.is_regular_market_open()
        market_status["is_open"] = regular_open
        # 重新計算下一次正規開盤時間，便於日誌輸出
        if not regular_open:
            market_status["next_open"] = self._next_regular_open_time()

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

    def load_contracts_from_positions(self) -> Dict[str, ContractConfig]:
        """從實際艙位中載入合約配置"""
        log.info("從 IBKR 艙位資料載入合約...")

        # 獲取艙位數據
        positions = self.app.getPositions(timeout=5.0)

        if not positions:
            log.warning("無法獲取艙位數據或沒有持倉")
            return {}

        # 轉換為 ContractConfig 格式
        contracts = {}
        for pos in positions:
            # 只處理期權類型和非零艙位
            if pos["secType"] != "OPT" or pos["position"] == 0:
                continue

            symbol = pos["symbol"]
            expiry = pos["lastTradeDateOrContractMonth"]
            strike = pos["strike"]
            right = pos["right"]

            # 生成唯一 key
            key = f"{symbol}_{right}_{strike}_{expiry}"

            # 預設值 - 後續會由市場數據補充
            delta = 0.0
            premium = 0.0

            # 根據艙位方向判斷 action
            action = "SELL" if pos["position"] < 0 else "BUY"

            contracts[key] = ContractConfig(
                symbol=symbol,
                expiry=expiry,
                strike=strike,
                right=right,
                exchange=pos["exchange"],
                currency=pos["currency"],
                delta=delta,
                premium=premium,
                action=action,
            )

        log.info(f"成功載入 {len(contracts)} 筆合約")
        return contracts

    def get_positions_summary(self) -> str:
        """獲取當前持倉摘要"""
        positions = self.app.getPositions(refresh=False)  # Use cached positions

        if not positions:
            return "無持倉數據"

        summary = []
        for pos in positions:
            if pos["position"] == 0:
                continue

            if pos["secType"] == "OPT":
                summary.append(
                    f"{pos['symbol']} {pos['right']} {pos['strike']} {pos['lastTradeDateOrContractMonth']}: {pos['position']} @ {pos['avgCost']:.2f}"
                )
            else:
                summary.append(
                    f"{pos['symbol']}: {pos['position']} @ {pos['avgCost']:.2f}"
                )

        return "\n".join(summary) if summary else "無有效持倉"

    def refresh_positions(self, force: bool = False):
        """定期刷新艙位數據"""
        # 每 10 分鐘或強制刷新
        if force or time.time() - self.last_positions_update > 600:  # 10 minutes
            log.info("刷新艙位數據...")
            positions_contracts = self.load_contracts_from_positions()

            if positions_contracts:
                # 更新合約配置
                self.cfgs = positions_contracts
                self.last_positions_update = time.time()
                log.info(f"已更新艙位數據，共 {len(positions_contracts)} 筆合約")

                # 重新訂閱所有合約的市場數據
                self._subscribe_market_data()
                log.info("已為所有艙位重新訂閱市場數據")

                # 更新初始價格及昨收價格
                self._update_initial_prices()
            else:
                log.warning("艙位數據為空，保留原有配置")

    def _update_initial_prices(self):
        """更新初始價格和前收盤價格"""
        # 更新選擇權初始價格
        for k, c in self.cfgs.items():
            self.init_price[k] = c.premium

        # 更新標的物昨收價格
        underlying_symbols = set(cfg.symbol for cfg in self.cfgs.values())
        for symbol in underlying_symbols:
            if symbol not in self.prev_closes:
                prev_close = self._get_underlying_prev_close(symbol)
                if prev_close:
                    self.prev_closes[symbol] = prev_close
                    log.info(f"更新 {symbol} 昨收價格: {prev_close}")

    def _get_underlying_prev_close(
        self, symbol: str, timeout: float = 10.0
    ) -> Optional[float]:
        """等待 streaming 資料填入指定標的物的昨日收盤價，最多 *timeout* 秒。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            data = self.app.get_stream_data(symbol)
            close_val = data.get("prev_close") or data.get("close")
            if close_val:
                return close_val
            time.sleep(0.1)
        return None

    def loop(self):
        # Initial load from positions
        self.refresh_positions(force=True)

        # If no positions found, fall back to config
        if not self.cfgs:
            log.warning("未找到有效艙位，將使用配置文件")
            config_manager = ConfigManager(self.path)
            self.cfgs = config_manager.load()

        while True:
            try:
                # 定期刷新艙位數據
                self.refresh_positions()

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

                # --- 所有標的股票價格 / 跳空警報
                underlying_symbols = set(cfg.symbol for cfg in self.cfgs.values())
                for symbol in underlying_symbols:
                    stock_data = self.app.get_stream_data(symbol)
                    stock_px = (
                        stock_data.get("last")
                        or stock_data.get("bid")
                        or stock_data.get("ask")
                    )
                    prev_close = self.prev_closes.get(symbol)

                    if stock_px and prev_close:
                        gap = (stock_px - prev_close) / prev_close
                        log.info(
                            f"{symbol} Px={(f'{stock_px:.2f}' if stock_px else 'NA')}"
                        )

                        if abs(gap) >= 0.03:  # 3% 跳空閾值
                            alert_msg, alert_id = generate_detailed_alert(
                                symbol, "gap", gap, ContractConfig(symbol, "", 0, "")
                            )
                            alerts.append((alert_msg, alert_id))
                            log.warning(f"偵測到 {symbol} 跳空: {gap:.1%}")
                    else:
                        log.info(f"{symbol} Px=NA")

                # --- 保留原來的 SPY 檢查代碼以保持兼容性
                spy_data = self.app.get_stream_data("SPY")
                spy_px = (
                    spy_data.get("last") or spy_data.get("bid") or spy_data.get("ask")
                )
                if spy_px:
                    log.info(f"SPY Px={spy_px:.2f}")

                # --- 逐檔選擇權
                for key, c in self.cfgs.items():
                    data = self.app.get_stream_data(key)
                    price = data.get("last") or data.get("bid") or data.get("ask")
                    delta = data.get("delta")
                    iv = data.get("iv")
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
            except:
                log.exception("主循環發生未處理例外，60 秒後重試")
                time.sleep(60)
