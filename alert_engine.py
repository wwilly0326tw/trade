from __future__ import annotations

import datetime
import logging
import os
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Dict, Optional

import pytz
import requests
from ibapi.contract import Contract

from IBApp import IBApp


# ─────────────────────────── 日誌設定 ────────────────────────────
def configure_logging(
    level: str = "INFO", noisy_loggers: list[str] | None = None
) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)

    console = logging.StreamHandler()
    console.setLevel(numeric)

    class DedupFilter(logging.Filter):
        _cache: dict[str, float] = {}

        def filter(self, record: logging.LogRecord) -> bool:  # noqa: N802
            msg, now = record.getMessage(), record.created
            last = self._cache.get(msg, 0.0)
            if now - last < 30:
                return False
            self._cache[msg] = now
            return True

    console.addFilter(DedupFilter())

    file_hdl = RotatingFileHandler(
        "alert_engine.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_hdl.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console.setFormatter(fmt)
    file_hdl.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # 避免重複 addHandler（重覆呼叫 configure_logging 時）
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        root.addHandler(console)
        root.addHandler(file_hdl)

    for name in noisy_loggers or []:
        logging.getLogger(name).setLevel(logging.WARNING)


configure_logging("INFO", noisy_loggers=["ibapi", "ibapi.utils", "ibapi.client"])
log = logging.getLogger(__name__)

# ─────────────────────────── LINE Push ──────────────────────────

_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "") or (
    "jpDKXxch8e/m30Ll4irnKE5Rcwv8bNslQ0f4H4DpyMmQ4dWJNOuWDN/VUC29C7iD/"
    "XWjDFrlRMZHAXgbdNwaUTGzpoO2sUSwSpwUonpIRTZ6TDZdsIfyz/G6Xf3RaqAsDbYti"
    "+NKTkFPR6XHDTL5jwdB04t89/1O/w1cDnyilFU="
)
_LINE_EP = "https://api.line.me/v2/bot/message/broadcast"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "Content-Type": "application/json"}
CHECK_INTERVAL = 60  # 行為不變：每 60 秒檢查一次


def line_push(msg: str) -> None:
    if not _TOKEN:
        log.warning("未設定 LINE TOKEN，警報僅寫入日誌")
        return
    try:
        r = requests.post(
            _LINE_EP,
            headers=_HEADERS,
            json={"messages": [{"type": "text", "text": msg[:1000]}]},
            timeout=5,
        )
        if r.status_code != 200:
            log.error("LINE API %s: %s", r.status_code, r.text[:200])
    except Exception as exc:  # noqa: BLE001
        log.error("LINE Broadcast 例外: %s", exc)


# ──────────────────────────── 資料類別 ────────────────────────────


@dataclass(slots=True)
class StrategyConfig:
    profit_target: float = 0.50
    min_dte: int = 21
    sell_delta_threshold: float = 0.30  # 只對 SELL 生效的上限
    buy_delta_floor: float = 0.65  # 只對 BUY 生效的下限


@dataclass(slots=True)
class ContractConfig:
    symbol: str
    expiry: str
    strike: float
    right: str  # "PUT" / "CALL"
    exchange: str = "SMART"
    currency: str = "USD"
    delta: float = 0.0
    premium: float = 0.0
    action: str = "SELL"  # "SELL" / "BUY"
    con_id: int = 0
    trading_class: str = ""
    multiplier: str = "100"

    def to_ib(self) -> Contract:
        """
        與原行為等價：
        - 若有 con_id：以 conId 指定，並設定 exchange=SMART, secType=OPT, currency=USD
        - 若無 con_id：用 symbol/expiry/strike/right 等欄位組合
        """
        c = Contract()
        if self.con_id:
            c.conId = self.con_id
            c.exchange = "SMART"  # 維持你原本行為：避免 321
            c.secType = "OPT"
            c.currency = "USD"
            return c

        # 無 conId → 以欄位組合
        c.symbol = self.symbol
        c.secType = "OPT"
        c.currency = self.currency
        c.right = self.right
        c.strike = self.strike
        c.lastTradeDateOrContractMonth = self.expiry
        c.exchange = self.exchange or "SMART"
        if self.trading_class:
            c.tradingClass = self.trading_class
        if self.multiplier:
            c.multiplier = self.multiplier
        return c


# ──────────────────────────── AlertEngine ────────────────────────────


class AlertEngine:
    def __init__(self, app: IBApp, rule: StrategyConfig) -> None:
        self.app = app
        self.rule = rule

        # 動態資料
        self.cfgs: Dict[str, ContractConfig] = {}
        self.init_price: Dict[str, float] = {}
        self.prev_closes: Dict[str, float] = {}
        self.sent_alerts: Dict[str, datetime.date] = {}

        self.trading_date = datetime.date.today()
        self.market_closed_notified = False
        self.last_market_status_check = datetime.datetime.min
        self.last_positions_update = 0.0

        # 啟動即載入持倉與訂閱行情
        self.refresh_positions(force=True)
        self._validate_positions_loaded()

    # ─────────── 啟動驗證 ────────────
    def _validate_positions_loaded(self) -> None:
        if not self.cfgs:
            msg = "⚠️ AlertEngine 啟動失敗：未偵測到任何期權持倉"
            log.error(msg)
            line_push(msg)
        else:
            log.info("啟動成功，目前期權持倉 %d 檔", len(self.cfgs))

    # ─────────── Streaming helpers ────────────
    def _subscribe_market_data(self) -> None:
        underlying_symbols = {cfg.symbol for cfg in self.cfgs.values()}
        for sym in underlying_symbols:
            stk = Contract()
            stk.symbol, stk.secType, stk.exchange, stk.currency = (
                sym,
                "STK",
                "SMART",
                "USD",
            )
            self.app.subscribe(stk, False, sym)
        for key, cfg in self.cfgs.items():
            self.app.subscribe(cfg.to_ib(), True, key)
        log.info("已訂閱 %d 標的與 %d 期權", len(underlying_symbols), len(self.cfgs))

    # ─────────── Positions 載入 ────────────
    def _load_from_positions(self) -> Dict[str, ContractConfig]:
        positions = self.app.getPositions(timeout=5.0)  # type: ignore[attr-defined]
        out: Dict[str, ContractConfig] = {}
        for p in positions or []:
            if p["secType"] != "OPT" or p["position"] == 0:
                continue
            key = f"{p['symbol']}_{p['right']}_{p['strike']}_{p['lastTradeDateOrContractMonth']}"
            multiplier_val = float(p.get("multiplier") or 100)
            out[key] = ContractConfig(
                symbol=p["symbol"],
                expiry=p["lastTradeDateOrContractMonth"],
                strike=p["strike"],
                right=p["right"],
                exchange=p["exchange"],
                currency=p["currency"],
                action="SELL" if p["position"] < 0 else "BUY",
                con_id=p["conId"],
                trading_class=p.get("tradingClass", ""),
                multiplier=p.get("multiplier", "100"),
                premium=p["avgCost"] / multiplier_val,
            )
        return out

    # 舊介面保留（不破壞外部相容性）：委派到 _load_from_positions
    def load_contracts_from_positions(self) -> Dict[str, ContractConfig]:
        log.info("從 IBKR 艙位資料載入合約 ...")
        contracts = self._load_from_positions()
        log.info("成功載入 %d 筆合約", len(contracts))
        return contracts

    # ─────────── 工具函式 ────────────
    @staticmethod
    def _dte(expiry: str) -> int:
        expire = datetime.datetime.strptime(expiry, "%Y%m%d").date()
        return (expire - datetime.date.today()).days

    # ─────────── Snapshot ───────────
    def first_snap(self) -> None:
        log.info("獲取首次快照資料 ...")
        self._wait_for_market_open()

        # premium（以目前持倉平均成本做為初始）
        for k, c in self.cfgs.items():
            self.init_price[k] = c.premium
            log.debug("%s premium = %.4f", k, c.premium)

        # 昨收（僅 underlying）
        self.prev_closes.clear()
        for symbol in {cfg.symbol for cfg in self.cfgs.values()}:
            prev_close = self._get_underlying_prev_close(symbol)
            if prev_close:
                self.prev_closes[symbol] = prev_close
                log.debug("%s 昨收 %.2f", symbol, prev_close)
            else:
                log.warning("無法獲取 %s 昨收價格", symbol)

    def _wait_for_market_open(self) -> None:
        while not self.app.is_regular_market_open():
            next_open = self._next_regular_open_time()
            if not next_open:
                log.info("無法計算下一次開盤時間，5 分鐘後重新檢查 ...")
                time.sleep(300)
                continue

            now_et = datetime.datetime.now(pytz.UTC).astimezone(self.app.us_eastern)  # type: ignore[attr-defined]
            wait_seconds = (next_open - now_et).total_seconds()

            if wait_seconds <= 60:
                log.info("市場即將開盤，30 秒後再次確認 ...")
                time.sleep(30)
            else:
                log.info(
                    "市場尚未開盤，預計開盤時間: %s",
                    next_open.strftime("%Y-%m-%d %H:%M:%S %Z"),
                )
                time.sleep(min(wait_seconds, 300))
        log.info("市場已開盤 (正規時段)，開始監控")

    def _next_regular_open_time(self) -> datetime.datetime:
        server_time = self.app.get_server_time()
        et_now = (
            server_time.astimezone(self.app.us_eastern)  # type: ignore[attr-defined]
            if server_time
            else datetime.datetime.now(pytz.UTC).astimezone(self.app.us_eastern)  # type: ignore[attr-defined]
        )
        today_open = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
        if et_now.weekday() < 5 and et_now < today_open:
            return today_open
        next_day = et_now + datetime.timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += datetime.timedelta(days=1)
        return next_day.replace(hour=9, minute=30, second=0, microsecond=0)

    # ─────────── 市場狀態 ────────────
    def _check_market_status(self) -> bool:
        now = datetime.datetime.now()
        if (now - self.last_market_status_check).total_seconds() < 300:
            return self.app.market_status["is_open"]  # type: ignore[attr-defined]

        self.last_market_status_check = now
        market_status = self.app.is_market_open()
        regular_open = self.app.is_regular_market_open()
        market_status["is_open"] = regular_open  # 保持原行為：僅採 RTH

        if not regular_open:
            market_status["next_open"] = self._next_regular_open_time()

        if market_status["is_open"]:
            current_date = datetime.datetime.now().date()
            if current_date != self.trading_date:
                log.info("交易日變更: %s → %s", self.trading_date, current_date)
                self.sent_alerts.clear()
                self.trading_date = current_date
                self.market_closed_notified = False

        if not market_status["is_open"] and not self.market_closed_notified:
            next_open = market_status.get("next_open")
            if next_open:
                log.info(
                    "市場已休市，下次開盤時間: %s",
                    next_open.strftime("%Y-%m-%d %H:%M:%S %Z"),
                )
            else:
                log.info("市場已休市，無法確定下次開盤時間")
            self.market_closed_notified = True

        return market_status["is_open"]

    # ─────────── 其他輔助 ────────────
    def get_positions_summary(self) -> str:
        positions = self.app.getPositions(refresh=False)  # type: ignore[attr-defined]
        if not positions:
            return "無持倉數據"
        summary = []
        for pos in positions:
            if pos["position"] == 0:
                continue
            if pos["secType"] == "OPT":
                summary.append(
                    f"{pos['symbol']} {pos['right']} {pos['strike']} "
                    f"{pos['lastTradeDateOrContractMonth']}: "
                    f"{pos['position']} @ {pos['avgCost']:.2f}"
                )
            else:
                summary.append(
                    f"{pos['symbol']}: {pos['position']} @ {pos['avgCost']:.2f}"
                )
        return "\n".join(summary) if summary else "無有效持倉"

    def refresh_positions(self, force: bool = False):
        if force or time.time() - self.last_positions_update > 600:
            new_cfgs = self._load_from_positions()
            if new_cfgs:
                self.cfgs = new_cfgs
                for cfg in self.cfgs.values():
                    if cfg.right in ("CALL", "PUT"):
                        self.enrich_option_contract(cfg)
                self.last_positions_update = time.time()
                self._subscribe_market_data()
                self._update_initial_prices()
                summary = self.get_positions_summary()
                log.info("持倉摘要:\n%s", summary)

    def _update_initial_prices(self) -> None:
        for k, c in self.cfgs.items():
            self.init_price[k] = c.premium
        for symbol in {cfg.symbol for cfg in self.cfgs.values()}:
            if symbol not in self.prev_closes:
                prev_close = self._get_underlying_prev_close(symbol)
                if prev_close:
                    self.prev_closes[symbol] = prev_close
                    log.debug("更新 %s 昨收價格: %.2f", symbol, prev_close)

    def _get_underlying_prev_close(
        self, symbol: str, timeout: float = 10.0
    ) -> Optional[float]:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            data = self.app.get_stream_data(symbol)
            close_val = data.get("prev_close") or data.get("close")
            if close_val:
                return close_val
            time.sleep(0.1)
        return None

    # ─────────── 警報文字 ───────────
    def generate_detailed_alert(
        self,
        key: str,
        alert_type: str,
        value: float,
        contract: ContractConfig,
        extra_info: dict | None = None,
    ) -> tuple[str, str]:
        extra_info = extra_info or {}
        today = datetime.datetime.now().strftime("%Y-%m-%d")

        if alert_type == "delta":
            emoji = "🚨"
            mode = extra_info.get("mode", contract.action.upper())  # "SELL" or "BUY"
            th = extra_info.get("threshold", 0.30)
            if mode == "SELL":
                detail = f"{key} Δ={value:.3f}（SELL）已超過閾值 {th:.2f}"
                action = f"建議關注 {contract.symbol} {contract.strike}{'P' if contract.right=='PUT' else 'C'} 風險增加"
            else:
                detail = f"{key} Δ={value:.3f}（BUY）已低於門檻 {th:.2f}"
                action = f"留意部位敏感度下降（可評估調整或加值）"
        elif alert_type == "profit":
            emoji = "💰"
            detail = (
                f"{key} 收益={value:.1%} 已達目標 {extra_info.get('target', 0.5):.1%} "
                f"({contract.action} {contract.premium:.2f}→{extra_info.get('price', 0):.2f})"
            )
            action = f"可考慮{'買回' if contract.action=='SELL' else '賣出'}平倉獲利"
        elif alert_type == "dte":
            emoji = "📅"
            detail = f"{key} 剩餘天數={value} 低於設定 {extra_info.get('min_dte', 36)}"
            action = "注意時間價值加速衰減，評估是否調整部位"
        else:  # gap
            emoji = "⚡"
            direction = "上漲" if value > 0 else "下跌"
            detail = f"{key} {direction} {abs(value):.1%}，大幅變動"
            action = f"請密切關注市場波動，{'PUT' if value > 0 else 'CALL'}選擇權可能受影響較大"

        full_message = f"{emoji} {today}\n{detail}\n{action}"
        unique_id = f"{alert_type}_{key}_{self.trading_date:%Y%m%d}"
        return full_message, unique_id

    def enrich_option_contract(self, cfg: ContractConfig):
        """用 conId 與 tradingClass 補完合約，提高行情成功率"""
        if cfg.con_id:
            return
        det = self.app.req_contract_details_blocking(cfg.to_ib())
        if det:
            c = det[0].contract
            cfg.con_id, cfg.trading_class, cfg.multiplier = (
                c.conId,
                c.tradingClass,
                c.multiplier,
            )

    def _pick_price(self, d: dict):
        # 依序嘗試：標準 last/bid/ask → 延遲 p68/p66/p67 → Mark Price p37
        for k in ("last", "bid", "ask", "p68", "p66", "p67", "p37"):
            v = d.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return v
        return None

    # ─────────── 主迴圈 ────────────
    def loop(self) -> None:
        self.refresh_positions(force=True)

        while True:
            try:
                self.refresh_positions()
                if not self._check_market_status():
                    time.sleep(CHECK_INTERVAL * 5)
                    continue

                self.market_closed_notified = False
                log.debug(
                    "[%s] 開始檢查合約狀態",
                    datetime.datetime.now().strftime("%H:%M:%S"),
                )
                alerts: list[tuple[str, str]] = []

                # 股票行情 / 跳空
                for symbol in {cfg.symbol for cfg in self.cfgs.values()}:
                    data = self.app.get_stream_data(symbol)
                    stock_px = data.get("last") or data.get("bid") or data.get("ask")
                    prev_close = self.prev_closes.get(symbol)
                    if stock_px and prev_close:
                        gap = (stock_px - prev_close) / prev_close
                        log.debug("%s Px=%.2f", symbol, stock_px)
                        if abs(gap) >= 0.03:
                            msg, aid = self.generate_detailed_alert(
                                symbol, "gap", gap, ContractConfig(symbol, "", 0, "")
                            )
                            alerts.append((msg, aid))
                            log.warning("偵測到 %s 跳空: %.1f%%", symbol, gap * 100)
                    else:
                        log.debug("%s Px=NA", symbol)

                # 選擇權逐檔
                for key, c in self.cfgs.items():
                    data = self.app.get_stream_data(key)
                    price = self._pick_price(data)
                    delta = data.get("delta")
                    iv = data.get("iv")
                    if price is None or delta is None:
                        log.warning("%s: 無法取得完整資料, data: %s", key, data)
                        continue

                    dte = self._dte(c.expiry)
                    delta_abs = abs(delta)
                    is_sell = c.action.upper() == "SELL"
                    sell_thr = getattr(self.rule, "sell_delta_threshold", 0.30)
                    buy_floor = getattr(self.rule, "buy_delta_floor", 0.65)

                    # Δ 門檻
                    # SELL：|Δ| >= 0.30 才警報
                    if is_sell and delta_abs >= sell_thr:
                        msg, aid = self.generate_detailed_alert(
                            key,
                            "delta",
                            delta_abs,
                            c,
                            {"threshold": sell_thr, "mode": "SELL"},
                        )
                        alerts.append((msg, aid))
                        log.warning(
                            "%s Δ=%.3f (SELL) 超過 %.2f", key, delta_abs, sell_thr
                        )

                    # BUY：|Δ| <= 0.65 才警報
                    elif (not is_sell) and delta_abs <= buy_floor and delta_abs > 0:
                        msg, aid = self.generate_detailed_alert(
                            key,
                            "delta",
                            delta_abs,
                            c,
                            {"threshold": buy_floor, "mode": "BUY"},
                        )
                        alerts.append((msg, aid))
                        log.warning(
                            "%s Δ=%.3f (BUY) 低於 %.2f", key, delta_abs, buy_floor
                        )

                    # 收益率（僅針對賣方部位觸發）
                    base = abs(c.premium) or 1e-9
                    if c.action.upper() == "SELL":
                        pct = (base - price) / base
                    else:
                        pct = (price - base) / base

                    # 僅當為賣方部位（SELL）且達到獲利目標時才發出警報
                    if is_sell and pct >= self.rule.profit_target:
                        msg, aid = self.generate_detailed_alert(
                            key,
                            "profit",
                            pct,
                            c,
                            {"target": self.rule.profit_target, "price": price},
                        )
                        alerts.append((msg, aid))
                        log.warning("%s 收益=%.1f%% 已達目標", key, pct * 100)

                    # DTE
                    if dte <= self.rule.min_dte:
                        msg, aid = self.generate_detailed_alert(
                            key, "dte", dte, c, {"min_dte": self.rule.min_dte}
                        )
                        alerts.append((msg, aid))
                        log.warning("%s DTE=%d 低於閾值", key, dte)

                    # 詳細行情（維持原先 debug 資訊格式）
                    pct_str = f"{pct:+.1%}"
                    delta_diff = f"{delta_abs - abs(c.delta):+.3f}"
                    iv_str = f"{iv:.4f}" if iv else "NA"
                    log.debug(
                        "%s: Px=%.2f (%s) Δ=%.3f (ΔΔ=%s) IV=%s DTE=%d",
                        key,
                        price,
                        pct_str,
                        delta_abs,
                        delta_diff,
                        iv_str,
                        dte,
                    )

                # 推播警報（去重）
                if alerts:
                    unique_alerts = []
                    for msg, aid in alerts:
                        if aid not in self.sent_alerts:
                            unique_alerts.append(msg)
                            self.sent_alerts[aid] = self.trading_date
                            line_push(msg)
                        else:
                            log.debug("[重複警報已忽略] %s", aid)
                    if unique_alerts:
                        log.info("已發送 %d 則新警報", len(unique_alerts))
                else:
                    log.debug("✓ 無警報")

                time.sleep(CHECK_INTERVAL)
            except Exception:
                log.exception("主循環發生未處理例外，60 秒後重試")
                time.sleep(60)
