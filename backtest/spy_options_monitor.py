"""
SPY 選擇權警報系統
基於穩定的IB連線，監控PUT和CALL SPREAD策略
"""

from ib_insync import *
import time
import datetime
import math
import json
import os
from dataclasses import dataclass
from typing import Optional, Dict, Any


@dataclass
class StrategyConfig:
    """策略配置"""

    # PUT 策略
    put_delta: float
    put_dte: int
    put_premium: float

    # CALL SPREAD 策略
    call_delta: float
    call_dte: int
    call_premium: float

    # 警報閾值
    delta_threshold: float = 0.30
    profit_target: float = 0.50  # 50%收益
    ivrank_change: float = 20.0  # ±20點
    min_dte: int = 21


@dataclass
class ContractConfig:
    """合約配置"""

    symbol: str
    expiry: str
    strike: float
    right: str  # 'PUT' or 'CALL'
    exchange: str = "SMART"
    currency: str = "USD"
    delta: float = 0.0  # 預期Delta值
    premium: float = 0.0  # 預期Premium價格
    action: str = "SELL"  # 'SELL' or 'BUY'

    def to_option_contract(self):
        """轉換為IB Option合約"""
        return Option(
            symbol=self.symbol,
            lastTradeDateOrContractMonth=self.expiry,
            strike=self.strike,
            right=self.right,
            exchange=self.exchange,
            currency=self.currency,
        )


class ConfigManager:
    """配置管理器"""

    def __init__(self, config_file="spy_contracts_config.json"):
        self.config_file = config_file

    def load_contracts_config(self) -> Dict[str, ContractConfig]:
        """載入合約配置"""
        if not os.path.exists(self.config_file):
            print(f"配置文件 {self.config_file} 不存在，將創建範例配置")
            self.create_example_config()
            return {}

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            contracts = {}
            for key, contract_data in data.items():
                contracts[key] = ContractConfig(**contract_data)

            print(f"✓ 已載入 {len(contracts)} 個合約配置")
            return contracts

        except Exception as e:
            print(f"載入配置失敗: {e}")
            return {}

    def save_contracts_config(self, contracts: Dict[str, ContractConfig]):
        """保存合約配置"""
        try:
            data = {}
            for key, contract in contracts.items():
                data[key] = {
                    "symbol": contract.symbol,
                    "expiry": contract.expiry,
                    "strike": contract.strike,
                    "right": contract.right,
                    "exchange": contract.exchange,
                    "currency": contract.currency,
                    "delta": contract.delta,
                    "premium": contract.premium,
                    "action": contract.action,
                }

            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            print(f"✓ 配置已保存到 {self.config_file}")

        except Exception as e:
            print(f"保存配置失敗: {e}")

    def create_example_config(self):
        """創建範例配置文件"""
        example_contracts = {
            "spy_put_30dte": {
                "symbol": "SPY",
                "expiry": "20250821",  # 範例到期日
                "strike": 580.0,
                "right": "PUT",
                "exchange": "SMART",
                "currency": "USD",
                "delta": -0.15,
                "premium": 2.50,
                "action": "SELL",
            },
            "spy_call_30dte": {
                "symbol": "SPY",
                "expiry": "20250821",  # 範例到期日
                "strike": 620.0,
                "right": "CALL",
                "exchange": "SMART",
                "currency": "USD",
                "delta": 0.15,
                "premium": 1.80,
                "action": "SELL",
            },
        }

        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(example_contracts, f, indent=2, ensure_ascii=False)
            print(f"✓ 已創建範例配置文件: {self.config_file}")
            print("請編輯此文件以設定您的實際合約")
        except Exception as e:
            print(f"創建範例配置失敗: {e}")


class SPYOptionsMonitor:
    def __init__(self, host="127.0.0.1", port=None, client_id=789):
        self.ib = IB()
        self.host = host
        self.port = port  # 如果為None，會自動檢測
        self.client_id = client_id
        self.connected = False

        # 儲存合約和初始數據
        self.spy_stock = None
        self.put_contract = None
        self.call_short_contract = None
        self.call_long_contract = None

        # 儲存初始狀態
        self.initial_put_price = None
        self.initial_call_price = None
        self.initial_ivrank = None

        # 儲存操作類型
        self.put_action = "SELL"  # PUT操作類型
        self.call_action = "SELL"  # CALL操作類型

        # 配置管理器
        self.config_manager = ConfigManager()

        # 啟用 ib_insync 的詳細日誌
        util.logToConsole()

    def find_available_port(self):
        """自動檢測可用的IB端口"""
        common_ports = [
            (4002, "IB Gateway (Live)"),
            (7496, "IB Gateway (Paper)"),
            (4001, "TWS (Live)"),
            (7497, "TWS (Paper)"),
        ]

        print("正在檢測可用的IB端口...")

        for port, description in common_ports:
            print(f"  測試 {port} ({description})...", end="")

            # 檢查端口連通性
            import socket

            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3)
                result = sock.connect_ex((self.host, port))
                sock.close()

                if result == 0:
                    print(" ✓ 端口可用")

                    # 嘗試API連接
                    test_ib = IB()
                    try:
                        test_ib.connect(
                            self.host,
                            port,
                            clientId=self.client_id + 1000,
                            timeout=60,  # 增加超時時間
                            readonly=True,
                        )
                        test_ib.disconnect()
                        print(f"    ✓ API連接成功 - 將使用端口 {port}")
                        return port
                    except Exception as api_error:
                        print(f"    ✗ API連接失敗: {api_error}")

                else:
                    print(" ✗ 端口無法連接")

            except Exception as e:
                print(f" ✗ 端口檢查失敗: {e}")

        print("✗ 未找到可用的IB端口")
        return None

    def connect(self):
        """連接到IB - 增強版本帶自動端口檢測"""
        # 如果沒有指定端口，自動檢測
        if self.port is None:
            self.port = self.find_available_port()
            if self.port is None:
                print("✗ 無法找到可用的IB端口")
                print("\n請檢查:")
                print("1. IB Gateway 或 TWS 是否已啟動")
                print("2. API 設置是否已啟用")
                print("3. 如果使用Paper Trading，確認使用Paper Trading模式")
                return False

        max_retries = 3
        retry_delay = 5

        for attempt in range(max_retries):
            try:
                if self.ib.isConnected():
                    print("Already connected")
                    return True

                print(
                    f"正在連接 IB (端口: {self.port}, 客戶端ID: {self.client_id}) - 嘗試 {attempt + 1}/{max_retries}..."
                )

                # 診斷連接前的狀態
                print(f"  主機: {self.host}:{self.port}")
                print(f"  客戶端ID: {self.client_id}")

                # 嘗試連接 (增加 readonly=True)
                self.ib.connect(
                    self.host,
                    self.port,
                    clientId=self.client_id,
                    timeout=60,
                    readonly=True,
                )
                self.connected = True
                print("✓ 連接成功!")

                # 等待連接穩定
                self.ib.sleep(2)

                # 驗證連接
                try:
                    server_time = self.ib.reqCurrentTime()
                    print(f"✓ 連接驗證成功 - 伺服器時間: {server_time}")
                    return True
                except Exception as verify_error:
                    print(f"⚠️ 連接驗證失敗: {verify_error}")
                    self.ib.disconnect()
                    self.connected = False

            except Exception as e:
                error_msg = str(e)
                print(f"✗ 連接嘗試 {attempt + 1} 失敗: {error_msg}")

                # 提供具體的錯誤診斷
                if "TimeoutError" in error_msg or "timeout" in error_msg.lower():
                    print("  診斷: 連接超時")
                    print("  可能原因:")
                    if self.port in [7496, 7497]:
                        print("    1. IB Gateway/TWS 沒有設為 Paper Trading 模式")
                        print("    2. Paper Trading API 設置沒有啟用")
                    else:
                        print("    1. IB Gateway/TWS 沒有啟動")
                        print("    2. Live Trading API 設置沒有啟用")
                elif "refused" in error_msg.lower():
                    print("  診斷: 連接被拒絕")
                    print("  請檢查 API 設置是否正確啟用")
                elif "already in use" in error_msg.lower():
                    print("  診斷: 客戶端ID衝突，嘗試新ID...")
                    import random

                    self.client_id = random.randint(1000, 9999)
                    print(f"  新客戶端ID: {self.client_id}")

                self.connected = False

                if attempt < max_retries - 1:
                    print(f"  等待 {retry_delay} 秒後重試...")
                    time.sleep(retry_delay)
                    retry_delay += 5  # 增加重試間隔

        print("✗ 所有連接嘗試都失敗")
        return False

    def disconnect(self):
        """安全斷開連接 - 改進版"""
        try:
            if self.ib.isConnected():
                print("正在安全斷開IB連接...")

                # 取消所有市場數據訂閱
                try:
                    self.ib.cancelMktData(self.spy_stock)
                    if self.put_contract:
                        self.ib.cancelMktData(self.put_contract)
                    if self.call_short_contract:
                        self.ib.cancelMktData(self.call_short_contract)
                except:
                    pass  # 可能沒有訂閱，忽略錯誤

                # 等待一下讓取消生效
                time.sleep(1)

                # 斷開連接
                self.ib.disconnect()
                print("✓ 已安全斷開連接")

            self.connected = False

        except Exception as e:
            print(f"斷開連接時發生錯誤: {e}")
            # 強制設為False，確保狀態正確
            self.connected = False

    def __enter__(self):
        """支持 with 語句的上下文管理"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """自動清理資源"""
        self.disconnect()

    def setup_signal_handlers(self):
        """設置信號處理器，確保程式中斷時正常斷開連接"""
        import signal
        import sys

        def signal_handler(signum, frame):
            print(f"\n收到中斷信號 ({signum})，正在安全關閉...")
            self.disconnect()
            sys.exit(0)

        # 註冊信號處理器
        signal.signal(signal.SIGINT, signal_handler)  # Ctrl+C
        signal.signal(signal.SIGTERM, signal_handler)  # 終止信號

        print("✓ 已設置安全關閉處理器")

    def setup_spy_stock(self):
        """設置SPY股票合約"""
        try:
            spy_contract = Stock("SPY", "SMART", "USD")
            qualified = self.ib.qualifyContracts(spy_contract)
            if not qualified:
                raise Exception("無法確認SPY合約")

            self.spy_stock = qualified[0]
            print(
                f"✓ SPY股票合約確認: {self.spy_stock.symbol} ({self.spy_stock.conId})"
            )
            return True

        except Exception as e:
            print(f"✗ SPY股票設置失敗: {e}")
            return False

    def get_spy_price(self):
        """獲取SPY現價"""
        try:
            ticker = self.ib.reqMktData(self.spy_stock, "", False, False)
            self.ib.sleep(2)

            price = ticker.last if ticker.last else ticker.close
            self.ib.cancelMktData(self.spy_stock)

            return price
        except Exception as e:
            print(f"獲取SPY價格失敗: {e}")
            return None

    def find_option_by_delta(
        self, option_type: str, target_delta: float, dte: int, spy_price: float
    ):
        """根據delta找到對應的選擇權合約"""
        try:
            # 計算到期日
            expiry_date = datetime.date.today() + datetime.timedelta(days=dte)
            expiry_str = expiry_date.strftime("%Y%m%d")

            # 根據delta估算履約價
            if option_type.upper() == "PUT":
                # PUT的delta是負值，我們用絕對值
                estimated_strike = spy_price * (1 - abs(target_delta) * 0.1)  # 粗略估算
            else:  # CALL
                estimated_strike = spy_price * (1 + target_delta * 0.1)  # 粗略估算

            # 四捨五入到最近的整數履約價
            strike = round(estimated_strike)

            # 創建選擇權合約
            option = Option(
                "SPY", expiry_str, strike, option_type.upper(), "SMART", currency="USD"
            )

            # 確認合約
            qualified = self.ib.qualifyContracts(option)
            if not qualified:
                print(f"無法找到 {option_type} strike={strike} expiry={expiry_str}")
                return None

            contract = qualified[0]
            print(
                f"✓ 找到選擇權: {contract.right} ${contract.strike} {contract.lastTradeDateOrContractMonth}"
            )

            return contract

        except Exception as e:
            print(f"尋找選擇權合約失敗: {e}")
            return None

    def get_option_greeks_and_price(self, contract):
        """獲取選擇權的Greeks和價格"""
        try:
            ticker = self.ib.reqMktData(contract, "", False, False)
            self.ib.sleep(3)  # 等待數據

            # 獲取價格
            mark_price = None
            if ticker.last:
                mark_price = ticker.last
            elif ticker.bid and ticker.ask:
                mark_price = (ticker.bid + ticker.ask) / 2

            # 獲取Greeks
            delta = getattr(ticker, "delta", None)
            gamma = getattr(ticker, "gamma", None)
            theta = getattr(ticker, "theta", None)
            vega = getattr(ticker, "vega", None)
            iv = getattr(ticker, "impliedVolatility", None)

            self.ib.cancelMktData(contract)

            return {
                "mark_price": mark_price,
                "delta": delta,
                "gamma": gamma,
                "theta": theta,
                "vega": vega,
                "iv": iv,
            }

        except Exception as e:
            print(f"獲取選擇權數據失敗: {e}")
            return None

    def calculate_dte(self, contract):
        """計算剩餘DTE"""
        try:
            expiry_str = contract.lastTradeDateOrContractMonth
            expiry_date = datetime.datetime.strptime(expiry_str, "%Y%m%d").date()
            today = datetime.date.today()
            dte = (expiry_date - today).days
            return dte
        except Exception as e:
            print(f"計算DTE失敗: {e}")
            return None

    def setup_contracts_from_config(self):
        """從配置文件載入合約"""
        print("\n=== 從配置文件載入合約 ===")

        # 載入配置
        contracts_config = self.config_manager.load_contracts_config()
        if not contracts_config:
            print("✗ 無可用的合約配置")
            return False

        print("可用的合約配置:")
        for key, contract_config in contracts_config.items():
            print(
                f"  {key}: {contract_config.action} {contract_config.symbol} {contract_config.right} ${contract_config.strike} {contract_config.expiry} (Delta:{contract_config.delta}, Premium:${contract_config.premium})"
            )

        # 讓用戶選擇PUT合約
        put_key = input("\n請選擇PUT合約 (輸入key): ").strip()
        if put_key not in contracts_config:
            print(f"✗ 找不到PUT合約配置: {put_key}")
            return False

        put_config = contracts_config[put_key]
        if put_config.right.upper() != "PUT":
            print(f"✗ 選擇的合約不是PUT: {put_config.right}")
            return False

        # 讓用戶選擇CALL合約
        call_key = input("請選擇CALL合約 (輸入key): ").strip()
        if call_key not in contracts_config:
            print(f"✗ 找不到CALL合約配置: {call_key}")
            return False

        call_config = contracts_config[call_key]
        if call_config.right.upper() != "CALL":
            print(f"✗ 選擇的合約不是CALL: {call_config.right}")
            return False

        # 創建並確認合約
        try:
            # 創建PUT合約
            put_option = put_config.to_option_contract()
            put_qualified = self.ib.qualifyContracts(put_option)
            if not put_qualified:
                print(f"✗ 無法確認PUT合約: {put_config}")
                return False
            self.put_contract = put_qualified[0]
            self.put_action = put_config.action  # 記錄PUT操作類型
            print(
                f"✓ PUT合約確認: {self.put_action} {self.put_contract.right} ${self.put_contract.strike} {self.put_contract.lastTradeDateOrContractMonth}"
            )

            # 創建CALL合約
            call_option = call_config.to_option_contract()
            call_qualified = self.ib.qualifyContracts(call_option)
            if not call_qualified:
                print(f"✗ 無法確認CALL合約: {call_config}")
                return False
            self.call_short_contract = call_qualified[0]
            self.call_action = call_config.action  # 記錄CALL操作類型
            print(
                f"✓ CALL合約確認: {self.call_action} {self.call_short_contract.right} ${self.call_short_contract.strike} {self.call_short_contract.lastTradeDateOrContractMonth}"
            )

            # 獲取初始價格
            print("\n獲取初始價格...")
            put_data = self.get_option_greeks_and_price(self.put_contract)
            call_data = self.get_option_greeks_and_price(self.call_short_contract)

            if put_data and call_data:
                self.initial_put_price = put_data["mark_price"]
                self.initial_call_price = call_data["mark_price"]

                print(f"✓ PUT初始價格: ${self.initial_put_price:.2f}")
                print(f"✓ CALL初始價格: ${self.initial_call_price:.2f}")

                return True
            else:
                print("✗ 無法獲取初始價格")
                return False

        except Exception as e:
            print(f"✗ 設置合約失敗: {e}")
            return False

    def setup_contracts(self, config: StrategyConfig):
        """設置監控的合約 (原有的動態搜尋方法)"""
        print("\n=== 動態搜尋合約 ===")

        # 獲取SPY現價
        spy_price = self.get_spy_price()
        if not spy_price:
            print("✗ 無法獲取SPY價格")
            return False

        print(f"✓ SPY現價: ${spy_price:.2f}")

        # 設置PUT合約
        print(f"\n尋找PUT合約 (delta={config.put_delta}, DTE={config.put_dte})...")
        self.put_contract = self.find_option_by_delta(
            "PUT", config.put_delta, config.put_dte, spy_price
        )
        if not self.put_contract:
            print("✗ 無法找到PUT合約")
            return False

        # 設置CALL合約 (賣出較近的履約價)
        print(f"\n尋找CALL合約 (delta={config.call_delta}, DTE={config.call_dte})...")
        self.call_short_contract = self.find_option_by_delta(
            "CALL", config.call_delta, config.call_dte, spy_price
        )
        if not self.call_short_contract:
            print("✗ 無法找到CALL合約")
            return False

        # 獲取初始價格
        print("\n獲取初始價格...")
        put_data = self.get_option_greeks_and_price(self.put_contract)
        call_data = self.get_option_greeks_and_price(self.call_short_contract)

        if put_data and call_data:
            self.initial_put_price = put_data["mark_price"]
            self.initial_call_price = call_data["mark_price"]

            print(f"✓ PUT初始價格: ${self.initial_put_price:.2f}")
            print(f"✓ CALL初始價格: ${self.initial_call_price:.2f}")

            return True

        return False

    def check_alerts(self, config: StrategyConfig):
        """檢查警報條件"""
        print("\n=== 檢查警報條件 ===")
        alerts = []

        # 檢查PUT合約
        if self.put_contract:
            print(f"檢查PUT合約: {self.put_contract.right} ${self.put_contract.strike}")
            put_data = self.get_option_greeks_and_price(self.put_contract)
            dte = self.calculate_dte(self.put_contract)

            if put_data:
                current_delta = abs(put_data["delta"]) if put_data["delta"] else 0
                current_price = put_data["mark_price"]

                print(f"  當前Delta: {current_delta:.3f}")
                print(f"  當前價格: ${current_price:.2f}")
                print(f"  剩餘DTE: {dte}")

                # 檢查Delta警報
                if current_delta >= config.delta_threshold:
                    alerts.append(
                        f"🚨 PUT Delta警報: {current_delta:.3f} >= {config.delta_threshold}"
                    )

                # 檢查收益警報
                if self.initial_put_price and current_price:
                    if self.put_action == "SELL":
                        # SELL PUT: 價格下跌是獲利 (收權利金，希望價格下跌)
                        profit_pct = (
                            self.initial_put_price - current_price
                        ) / self.initial_put_price
                    else:  # BUY PUT
                        # BUY PUT: 價格上漲是獲利 (付權利金，希望價格上漲)
                        profit_pct = (
                            current_price - self.initial_put_price
                        ) / self.initial_put_price

                    if profit_pct >= config.profit_target:
                        alerts.append(
                            f"💰 PUT收益警報: {profit_pct:.1%} >= {config.profit_target:.1%} ({self.put_action})"
                        )

                # 檢查DTE警報
                if dte and dte <= config.min_dte:
                    alerts.append(f"📅 PUT DTE警報: 剩餘{dte}天 <= {config.min_dte}天")

        # 檢查CALL合約
        if self.call_short_contract:
            print(
                f"檢查CALL合約: {self.call_short_contract.right} ${self.call_short_contract.strike}"
            )
            call_data = self.get_option_greeks_and_price(self.call_short_contract)
            dte = self.calculate_dte(self.call_short_contract)

            if call_data:
                current_delta = abs(call_data["delta"]) if call_data["delta"] else 0
                current_price = call_data["mark_price"]

                print(f"  當前Delta: {current_delta:.3f}")
                print(f"  當前價格: ${current_price:.2f}")
                print(f"  剩餘DTE: {dte}")

                # 檢查Delta警報
                if current_delta >= config.delta_threshold:
                    alerts.append(
                        f"🚨 CALL Delta警報: {current_delta:.3f} >= {config.delta_threshold}"
                    )

                # 檢查收益警報
                if self.initial_call_price and current_price:
                    if self.call_action == "SELL":
                        # SELL CALL: 價格下跌是獲利 (收權利金，希望價格下跌)
                        profit_pct = (
                            self.initial_call_price - current_price
                        ) / self.initial_call_price
                    else:  # BUY CALL
                        # BUY CALL: 價格上漲是獲利 (付權利金，希望價格上漲)
                        profit_pct = (
                            current_price - self.initial_call_price
                        ) / self.initial_call_price

                    if profit_pct >= config.profit_target:
                        alerts.append(
                            f"💰 CALL收益警報: {profit_pct:.1%} >= {config.profit_target:.1%} ({self.call_action})"
                        )

                # 檢查DTE警報
                if dte and dte <= config.min_dte:
                    alerts.append(f"📅 CALL DTE警報: 剩餘{dte}天 <= {config.min_dte}天")

        return alerts

    def run_monitor(self, config: StrategyConfig, check_interval=60):
        """運行監控"""
        print(f"\n=== 開始監控 (每{check_interval}秒檢查一次) ===")

        try:
            while True:
                current_time = datetime.datetime.now().strftime("%H:%M:%S")
                print(f"\n[{current_time}] 執行檢查...")

                # 檢查連線
                if not self.ib.isConnected():
                    print("⚠️ 連線中斷，嘗試重新連接...")
                    if not self.connect():
                        print("重新連接失敗，等待下次檢查...")
                        time.sleep(check_interval)
                        continue

                # 檢查警報
                alerts = self.check_alerts(config)

                if alerts:
                    print("\n" + "=" * 50)
                    print("🔔 發現警報:")
                    for alert in alerts:
                        print(f"  {alert}")
                    print("=" * 50)
                else:
                    print("✓ 無警報")

                # 等待下次檢查
                time.sleep(check_interval)

        except KeyboardInterrupt:
            print("\n監控已停止")
        except Exception as e:
            print(f"監控錯誤: {e}")


def main():
    print("=== SPY 選擇權警報系統 ===")

    # 預先檢查IB連接 - 自動檢測可用端口
    print("\n執行連接預檢...")
    import socket

    # 檢查常用端口
    common_ports = [
        (4002, "IB Gateway (Live)"),
        (7496, "IB Gateway (Paper)"),
        (4001, "TWS (Live)"),
        (7497, "TWS (Paper)"),
    ]

    available_port = None
    for port, description in common_ports:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            result = sock.connect_ex(("127.0.0.1", port))
            sock.close()

            if result == 0:
                print(f"✓ 發現可用端口 {port} ({description})")
                available_port = port
                break
        except Exception as e:
            continue

    if not available_port:
        print("✗ 未找到可用的IB端口")
        print("請確認:")
        print("1. IB Gateway 或 TWS 已啟動")
        print("2. API 設置已啟用")
        print("3. 軟體已完全載入並登入")
        return

    print(f"將使用端口 {available_port} 進行連接")

    # 選擇設置方式
    print("\n請選擇合約設置方式:")
    print("1. 從配置文件載入現有合約")
    print("2. 動態搜尋新合約")
    print("3. 僅測試連接")

    choice = input("請選擇 (1, 2, 或 3): ").strip()

    # 創建監控器 - 使用檢測到的端口和隨機客戶端ID避免衝突
    import random

    client_id = random.randint(1000, 9999)
    print(f"\n使用客戶端ID: {client_id}")
    print(f"使用端口: {available_port}")

    # 使用上下文管理器確保連接正常關閉
    with SPYOptionsMonitor(port=available_port, client_id=client_id) as monitor:
        # 設置安全關閉處理器
        monitor.setup_signal_handlers()

        try:
            # 連接
            if not monitor.connect():
                print("\n🔧 連接失敗的解決方案:")
                print("1. 運行清理工具: python clean_ib_connections.py")
                print("2. 重啟 IB Gateway/TWS")
                print("3. 檢查 API 設定 (Configure -> API -> Settings)")
                print("4. 確認 'Enable ActiveX and Socket Clients' 已勾選")
                print("5. 確認端口設定正確:")
                print("   - Live Trading: 4002 (IB Gateway) 或 4001 (TWS)")
                print("   - Paper Trading: 7496 (IB Gateway) 或 7497 (TWS)")
                return

            if choice == "3":
                print("✅ 連接測試成功！")
                print("系統可以正常使用")
                return

            # 設置SPY股票
            if not monitor.setup_spy_stock():
                return

            if choice == "1":
                # 從配置文件載入
                if not monitor.setup_contracts_from_config():
                    print("從配置文件載入失敗，請檢查配置文件或選擇動態搜尋")
                    return
            else:
                # 動態搜尋方式
                print("\n請輸入策略參數:")

                try:
                    # PUT策略參數
                    put_delta = float(input("PUT Delta (例如: 0.15): ") or "0.15")
                    put_dte = int(input("PUT DTE (例如: 30): ") or "30")
                    put_premium = float(input("PUT Premium (例如: 2.0): ") or "2.0")

                    # CALL策略參數
                    call_delta = float(input("CALL Delta (例如: 0.15): ") or "0.15")
                    call_dte = int(input("CALL DTE (例如: 30): ") or "30")
                    call_premium = float(input("CALL Premium (例如: 1.0): ") or "1.0")

                    # 創建配置
                    config = StrategyConfig(
                        put_delta=put_delta,
                        put_dte=put_dte,
                        put_premium=put_premium,
                        call_delta=call_delta,
                        call_dte=call_dte,
                        call_premium=call_premium,
                    )

                    print(f"\n配置完成:")
                    print(
                        f"  PUT: Delta={config.put_delta}, DTE={config.put_dte}, Premium=${config.put_premium}"
                    )
                    print(
                        f"  CALL: Delta={config.call_delta}, DTE={config.call_dte}, Premium=${config.call_premium}"
                    )

                except ValueError:
                    print("輸入格式錯誤，使用預設值")
                    config = StrategyConfig()

                # 設置合約
                if not monitor.setup_contracts(config):
                    return

            # 開始監控
            print("\n按 Ctrl+C 停止監控")
            monitor.run_monitor(config if choice == "2" else StrategyConfig())

        except Exception as e:
            print(f"\n程式執行錯誤: {e}")
            import traceback

            traceback.print_exc()

        finally:
            print("\n正在清理資源...")
            # 上下文管理器會自動調用 disconnect()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n程式已被用戶中斷")
    except Exception as e:
        print(f"\n主程式錯誤: {e}")
        import traceback

        traceback.print_exc()

    print("\n💡 提示: 如果經常遇到連接問題，可以運行清理工具:")
    print("python clean_ib_connections.py")
