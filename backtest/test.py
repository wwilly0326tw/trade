# spy_option_alert_ibapi.py
"""
SPY 選擇權警報系統（官方 ibapi 版）
------------------------------------------------
* 在 7496 連線 (TWS Live 預設埠)
* 支援兩種合約來源：
  1. 透過 spy_contracts_config.json 指定 PUT / CALL 合約 (SELL 或 BUY)
  2. 依 Delta & DTE 動態搜尋符合條件的合約
* 每隔 N 秒比對一次 (預設 60s) ：
  - Delta >= 閾值
  - 收益率 >= 50 %
  - DTE <= 21 天
  若觸發條件即列印警報
"""
from __future__ import annotations

import json, os, sys, time, datetime, threading, random, signal
from dataclasses import dataclass
from typing import Dict, Any, Optional, Tuple, List

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

HOST = "127.0.0.1"
PORT = 7496  # TWS Live 預設，可自行調整
CID = random.randint(1000, 9999)
CHECK_INTERVAL = 60  # 秒


# ------------------------------------------------------------------
# 資料類別
# ------------------------------------------------------------------
@dataclass
class StrategyConfig:
    put_delta: float
    put_dte: int
    put_premium: float
    call_delta: float
    call_dte: int
    call_premium: float
    # 警報閾值
    delta_threshold: float = 0.30
    profit_target: float = 0.50
    ivrank_change: float = 20.0
    min_dte: int = 21


@dataclass
class ContractConfig:
    symbol: str
    expiry: str
    strike: float
    right: str  # "PUT" or "CALL"
    exchange: str = "SMART"
    currency: str = "USD"
    delta: float = 0.0
    premium: float = 0.0
    action: str = "SELL"  # "SELL" or "BUY"

    def to_ib_contract(self) -> Contract:
        c = Contract()
        c.symbol = self.symbol
        c.secType = "OPT"
        c.exchange = self.exchange
        c.currency = self.currency
        c.lastTradeDateOrContractMonth = self.expiry
        c.right = self.right
        c.strike = self.strike
        return c


# ------------------------------------------------------------------
# 配置檔案工具
# ------------------------------------------------------------------
class ConfigManager:
    def __init__(self, path: str = "spy_contracts_config.json"):
        self.path = path

    def load(self) -> Dict[str, ContractConfig]:
        if not os.path.exists(self.path):
            self._create_sample()
            return {}
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {k: ContractConfig(**v) for k, v in raw.items()}

    def _create_sample(self):
        sample = {
            "spy_put_sample": {
                "symbol": "SPY",
                "expiry": "20251219",
                "strike": 580,
                "right": "PUT",
                "delta": -0.15,
                "premium": 2.5,
                "action": "SELL",
            },
            "spy_call_sample": {
                "symbol": "SPY",
                "expiry": "20251219",
                "strike": 620,
                "right": "CALL",
                "delta": 0.15,
                "premium": 1.8,
                "action": "SELL",
            },
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(sample, f, indent=2, ensure_ascii=False)
        print(f"已建立範例 {self.path}，請編輯後重新執行。")


# ------------------------------------------------------------------
# IB 主應用
# ------------------------------------------------------------------
class IBApp(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.next_req_id = 1
        self.tick_store: Dict[int, Dict[str, Any]] = {}
        self.contract_map: Dict[int, Contract] = {}
        self.connected_ok = threading.Event()

    # -------------------------------- events
    def nextValidId(self, oid: int):
        self.next_req_id = max(self.next_req_id, oid)
        self.connected_ok.set()

    def tickPrice(self, reqId, field, price, attrib):
        self.tick_store.setdefault(reqId, {})[f"field_{field}"] = price

    def tickOptionComputation(self, reqId, tickType, impliedVol, delta, *_):
        self.tick_store.setdefault(reqId, {})["delta"] = delta
        self.tick_store[reqId]["iv"] = impliedVol

    def error(self, reqId, code, msg, advancedOrderRejectJson=""):
        if code in (2104, 2106, 2158):
            return  # 資料農場通知
        print(f"ERR {code}: {msg}")

    # -------------------------------- helper
    def _req_id(self) -> int:
        rid = self.next_req_id
        self.next_req_id += 1
        return rid

    def snapshot(
        self, contract: Contract, greeks: bool = True, timeout: float = 3.0
    ) -> Dict[str, Any]:
        rid = self._req_id()
        self.contract_map[rid] = contract
        self.reqMktData(rid, contract, "", True, False, [])  # snapshot=True
        t0 = time.time()
        while time.time() - t0 < timeout:
            if (
                rid in self.tick_store and "field_4" in self.tick_store[rid]
            ):  # LAST price
                break
            time.sleep(0.05)
        self.cancelMktData(rid)
        data = self.tick_store.pop(rid, {})
        price = data.get("field_4") or data.get("field_1") or data.get("field_2")
        return {"price": price, "delta": data.get("delta"), "iv": data.get("iv")}


# ------------------------------------------------------------------
# 策略核心：警報判斷
# ------------------------------------------------------------------
class AlertEngine:
    def __init__(
        self,
        app: IBApp,
        cfg: StrategyConfig,
        put_conf: ContractConfig,
        call_conf: ContractConfig,
    ):
        self.app = app
        self.cfg = cfg
        self.put_conf = put_conf
        self.call_conf = call_conf
        self.init_prices: Dict[str, float] = {}

    # --------- util
    @staticmethod
    def _dte(expiry: str) -> int:
        d = datetime.datetime.strptime(expiry, "%Y%m%d").date()
        return (d - datetime.date.today()).days

    # --------- main loop
    def start(self):
        # 首次 snapshot，記錄進場價格
        self.init_prices["PUT"] = self.app.snapshot(self.put_conf.to_ib_contract())[
            "price"
        ]
        self.init_prices["CALL"] = self.app.snapshot(self.call_conf.to_ib_contract())[
            "price"
        ]
        print(
            f"初始 PUT 價格 = {self.init_prices['PUT']}, CALL 價格 = {self.init_prices['CALL']}"
        )
        while True:
            self._check_once()
            time.sleep(CHECK_INTERVAL)

    def _check_once(self):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"[{now}] 檢查中…")
        alerts: List[str] = []

        # --- PUT ---
        put_snap = self.app.snapshot(self.put_conf.to_ib_contract())
        if put_snap["price"] is None:
            print("PUT 無價格資料，跳過 …")
        else:
            self._evaluate_leg("PUT", put_snap, self.put_conf, alerts)

        # --- CALL ---
        call_snap = self.app.snapshot(self.call_conf.to_ib_contract())
        if call_snap["price"] is None:
            print("CALL 無價格資料，跳過 …")
        else:
            self._evaluate_leg("CALL", call_snap, self.call_conf, alerts)

        if alerts:
            print("\n==========  警報  ==========")
            for a in alerts:
                print(a)
            print("===========================\n")
        else:
            print("✓ 無警報\n")

    # --------- single leg logic
    def _evaluate_leg(
        self, tag: str, snap: Dict[str, Any], conf: ContractConfig, alerts: List[str]
    ):
        dte = self._dte(conf.expiry)
        delta_now = abs(snap.get("delta") or 0.0)
        px_now = snap["price"]
        px_init = self.init_prices[tag]

        print(f"{tag}: 價格={px_now:.2f}, Δ={delta_now:.3f}, DTE={dte}")

        # Delta 警報
        if delta_now >= self.cfg.delta_threshold:
            alerts.append(
                f"🚨 {tag} Delta 警報 {delta_now:.3f} >= {self.cfg.delta_threshold}"
            )

        # 收益警報
        if px_init and px_now:
            if conf.action.upper() == "SELL":
                profit_pct = (px_init - px_now) / px_init
            else:
                profit_pct = (px_now - px_init) / px_init
            if profit_pct >= self.cfg.profit_target:
                alerts.append(
                    f"💰 {tag} 收益 {profit_pct:.1%} >= {self.cfg.profit_target:.1%}"
                )

        # DTE 警報
        if dte <= self.cfg.min_dte:
            alerts.append(f"📅 {tag} DTE {dte} <= {self.cfg.min_dte}")


# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------
def main():
    print("=== SPY 選擇權警報系統 (ibapi 版) ===")

    # 1. 讀取/建立合約設定
    cmgr = ConfigManager()
    cfgs = cmgr.load()
    print(cfgs)
    if not cfgs:
        print("請編輯範例配置後重新執行 …")
        return

    # 強制要求 user 指定 PUT 與 CALL
    put_key = input("輸入 PUT key: ").strip()
    call_key = input("輸入 CALL key: ").strip()
    if put_key not in cfgs or call_key not in cfgs:
        print("Key 不存在")
        return

    put_conf = cfgs[put_key]
    call_conf = cfgs[call_key]

    # 2. 輸入策略 (若需動態調整)
    sc = StrategyConfig(
        put_delta=put_conf.delta or 0.15,
        put_dte=AlertEngine._dte(put_conf.expiry),
        put_premium=put_conf.premium,
        call_delta=call_conf.delta or 0.15,
        call_dte=AlertEngine._dte(call_conf.expiry),
        call_premium=call_conf.premium,
    )

    # 3. 啟動 IB 連線
    app = IBApp()
    print(f"連線至 {HOST}:{PORT} (cid={CID}) …")
    app.connect(HOST, PORT, CID)
    threading.Thread(target=app.run, daemon=True).start()
    if not app.connected_ok.wait(5):
        print("連線逾時，請檢查 TWS/IBG")
        return
    print("✓ 已連線 TWS/Gateway\n")

    # 4. Ctrl+C 安全釋放
    def safe_close(sig, _):
        print("\n收到中斷, 正在斷線 …")
        app.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT, safe_close)
    signal.signal(signal.SIGTERM, safe_close)

    # 5. 進入警報引擎
    engine = AlertEngine(app, sc, put_conf, call_conf)
    try:
        engine.start()
    finally:
        app.disconnect()


if __name__ == "__main__":
    main()
