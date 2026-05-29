
import time
from datetime import timedelta
from typing import List, Optional, Tuple

import requests

import config
import runtime
from utils import iso_z, log_event, now_utc


def get_live_price(symbol: str) -> Optional[float]:
    price = runtime.price_cache.get(symbol)
    if price is not None:
        return price

    try:
        r = requests.get(
            f"https://api.exchange.coinbase.com/products/{symbol}/ticker",
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        return float(data["price"])
    except Exception:
        return None


def get_candles(symbol: str, granularity: int, limit: int = 60) -> List[List[float]]:
    end = now_utc()
    start = end - timedelta(seconds=granularity * limit)
    try:
        r = requests.get(
            f"https://api.exchange.coinbase.com/products/{symbol}/candles",
            params={
                "start": iso_z(start),
                "end": iso_z(end),
                "granularity": granularity,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            return []
        data.sort(key=lambda x: x[0])
        return data
    except Exception as e:
        if config.DEBUG_MODE:
            log_event(f"Candle fetch failed for {symbol} [{granularity}]: {e}")
        return []


def get_top_gainers(limit: int = config.TOP_GAINERS_LIMIT) -> List[str]:
    try:
        r = requests.get("https://api.exchange.coinbase.com/products", timeout=15)
        r.raise_for_status()
        products = r.json()

        usd_pairs = []
        for product in products:
            product_id = product.get("id")
            quote_currency = product.get("quote_currency")
            status = str(product.get("status", "")).lower()
            trading_disabled = product.get("trading_disabled", False)

            if not product_id or quote_currency != "USD":
                continue
            if trading_disabled:
                continue
            if status and status != "online":
                continue

            usd_pairs.append(product_id)

        gainers: List[Tuple[str, float, float]] = []
        for symbol in usd_pairs:
            try:
                ticker_resp = requests.get(
                    f"https://api.exchange.coinbase.com/products/{symbol}/ticker", timeout=8
                )
                ticker_resp.raise_for_status()
                ticker = ticker_resp.json()

                price = float(ticker.get("price") or 0.0)
                volume = float(ticker.get("volume") or 0.0)
                if price <= 0 or volume <= 0:
                    continue

                quote_volume_usd = price * volume
                if quote_volume_usd < config.TOP_GAINERS_MIN_24H_VOLUME_USD:
                    continue

                open_24h = float(ticker.get("open") or 0.0)
                if open_24h <= 0:
                    continue

                change_pct = ((price - open_24h) / open_24h) * 100.0
                if change_pct < config.TOP_GAINERS_MIN_PRICE_CHANGE_PCT:
                    continue

                gainers.append((symbol, change_pct, quote_volume_usd))
            except Exception:
                continue

        gainers.sort(key=lambda x: (x[1], x[2]), reverse=True)
        top = [symbol for symbol, _change, _volume in gainers[:limit]]

        if top:
            log_event(f"Scanner selected symbols: {top}")
            return top

        log_event("Scanner returned no symbols, using fallback watchlist")
        return config.WATCHLIST.copy()

    except Exception as e:
        log_event(f"Gainer scan failed: {e}")
        return config.WATCHLIST.copy()


def get_scan_symbols() -> List[str]:
    if not config.TOP_GAINERS_ENABLED:
        return config.WATCHLIST.copy()

    now_ts = time.time()
    cached_symbols = runtime.scanner_cache.get("symbols", [])
    last_refresh = float(runtime.scanner_cache.get("last_refresh", 0.0))

    if cached_symbols and (now_ts - last_refresh) < config.TOP_GAINERS_REFRESH_SECONDS:
        return list(cached_symbols)

    symbols = get_top_gainers(config.TOP_GAINERS_LIMIT)
    runtime.scanner_cache["symbols"] = list(symbols)
    runtime.scanner_cache["last_refresh"] = now_ts
    return list(symbols)
