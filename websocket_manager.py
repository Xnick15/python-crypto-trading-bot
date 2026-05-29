
import json
import os
import threading
import time
from typing import Optional

from coinbase.websocket import (
    WSClient,
    WSUserClient,
    WSClientConnectionClosedException,
    WSClientException,
)

import config
import runtime
from utils import log_event


def on_market_message(msg) -> None:
    try:
        if isinstance(msg, str):
            payload = json.loads(msg)
        else:
            payload = msg if isinstance(msg, dict) else json.loads(str(msg))

        channel = payload.get("channel")
        events = payload.get("events", [])

        if channel == "ticker":
            for event in events:
                tickers = event.get("tickers", [])
                for ticker in tickers:
                    product_id = ticker.get("product_id")
                    price = ticker.get("price")
                    if product_id and price:
                        runtime.price_cache[product_id] = float(price)

    except Exception as e:
        log_event(f"Market WS parse error: {e}")


def on_user_message(msg) -> None:
    try:
        runtime.event_queue.put({"type": "user_msg", "payload": msg})
    except Exception as e:
        log_event(f"User WS parse error: {e}")


def start_market_ws() -> threading.Thread:
    def runner():
        while True:
            try:
                ws = WSClient(on_message=on_market_message, retry=True)
                ws.open()
                ws.ticker(product_ids=config.WATCHLIST)
                ws.heartbeats()
                ws.run_forever_with_exception_check()
            except (WSClientConnectionClosedException, WSClientException) as e:
                log_event(f"Market WS reconnect loop: {e}")
                time.sleep(2)
            except Exception as e:
                log_event(f"Market WS fatal-ish error: {e}")
                time.sleep(2)

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    return t


def start_user_ws() -> Optional[threading.Thread]:
    if not config.LIVE_TRADING:
        return None

    def runner():
        while True:
            try:
                ws = WSUserClient(
                    api_key=os.getenv("COINBASE_API_KEY"),
                    api_secret=os.getenv("COINBASE_API_SECRET"),
                    on_message=on_user_message,
                    retry=True,
                )
                ws.open()
                ws.user(product_ids=config.WATCHLIST)
                ws.heartbeats()
                ws.run_forever_with_exception_check()
            except (WSClientConnectionClosedException, WSClientException) as e:
                log_event(f"User WS reconnect loop: {e}")
                time.sleep(2)
            except Exception as e:
                log_event(f"User WS fatal-ish error: {e}")
                time.sleep(2)

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    return t
