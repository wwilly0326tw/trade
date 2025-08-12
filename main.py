"""
啟動 IB 連線並交給 AlertEngine 監控選擇權。
"""

import random
import sys
import signal
import threading
import logging
import logging.handlers
from pathlib import Path
from ibapi.client import MarketDataTypeEnum

from alert_engine import AlertEngine, StrategyConfig
from IBApp import IBApp

HOST, PORT = "127.0.0.1", 4001
CID = random.randint(1000, 9999)


def setup_logging():
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(),
            logging.handlers.TimedRotatingFileHandler(
                log_dir / "monitor.log",
                when="D",
                interval=2,
                backupCount=10,
                encoding="utf-8",
            ),
        ],
    )
    return logging.getLogger("monitor")


log = setup_logging()
log.info("監控主程式啟動")

# ---------- 連線 IBKR（與原行為一致） ---------- #
app = IBApp()
app.connect(HOST, PORT, CID)
threading.Thread(target=app.run, daemon=True).start()
if not app.ready.wait(5):
    log.error("與 TWS/Gateway 握手逾時")
    sys.exit(1)


app.reqMarketDataType(MarketDataTypeEnum.DELAYED)  # 等同 app.reqMarketDataType(3)

# ---------- 啟動警報引擎（行為不變） ---------- #
rule = StrategyConfig()
engine = AlertEngine(app, rule)
engine.first_snap()


def shutdown(sig, _):
    log.info("收到終止信號，斷線中 ...")
    app.disconnect()
    sys.exit(0)


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

try:
    engine.loop()
finally:
    app.disconnect()
