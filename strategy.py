
from typing import Any, Dict, List, Optional

import config
from indicators import closes_from_candles, ema, pct_change, rsi, sma
from market_data import get_candles
from state_manager import build_adaptive_settings, state_positions
from utils import log_event, now_utc


def analyze_timeframe(symbol: str, tf_name: str, granularity: int) -> Optional[Dict[str, Any]]:
    candles = get_candles(symbol, granularity, config.CANDLES_PER_TIMEFRAME)
    if len(candles) < 35:
        return None

    closes = closes_from_candles(candles)
    latest = closes[-1]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    rsi14 = rsi(closes, 14)
    vol_sma20 = sma([float(c[5]) for c in candles], 20)

    if not ema9 or not ema21 or not rsi14 or not vol_sma20:
        return None

    latest_ema9 = ema9[-1]
    latest_ema21 = ema21[-1]
    latest_rsi = rsi14[-1]
    latest_volume = float(candles[-1][5])
    avg_volume = vol_sma20[-1]
    recent_high = max(float(c[2]) for c in candles[-10:-1])
    momentum_3 = pct_change(closes[-1], closes[-4]) if len(closes) >= 4 else 0.0

    bullish = latest > latest_ema9 and latest_ema9 > latest_ema21

    score = 0
    reasons = []

    if latest > latest_ema9:
        score += 1
        reasons.append("price>ema9")
    if latest_ema9 > latest_ema21:
        score += 2
        reasons.append("ema9>ema21")
    if 50 <= latest_rsi <= 68:
        score += 2
        reasons.append("rsi_good")
    elif 68 < latest_rsi <= 75:
        score += 1
        reasons.append("rsi_hot")
    elif latest_rsi < 45:
        score -= 1
        reasons.append("rsi_weak")
    if latest_volume > avg_volume * 1.10:
        score += 1
        reasons.append("volume_up")
    if latest > recent_high:
        score += 1
        reasons.append("breakout")

    if momentum_3 > 0.05:
        score += 1
        reasons.append("momentum_up")
    elif momentum_3 < -0.05:
        score -= 1
        reasons.append("momentum_down")

    return {
        "symbol": symbol,
        "tf": tf_name,
        "score": score,
        "bullish": bullish,
        "price": latest,
        "rsi": latest_rsi,
        "reasons": reasons,
    }


def score_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    results = {}
    all_bullish = True

    for tf_name, granularity in config.TIMEFRAMES.items():
        res = analyze_timeframe(symbol, tf_name, granularity)
        if res is None:
            return None
        results[tf_name] = res
        if not res["bullish"]:
            all_bullish = False

    weighted = (
        results["5m"]["score"] * 1 +
        results["15m"]["score"] * 2 +
        results["1h"]["score"] * 2
    )

    if config.REQUIRE_ALL_TIMEFRAMES_BULLISH and not all_bullish:
        weighted -= 3

    return {
        "symbol": symbol,
        "weighted_score": weighted,
        "all_bullish": all_bullish,
        "latest_price": results["5m"]["price"],
        "timeframes": results,
    }


def in_symbol_cooldown(state: Dict[str, Any], symbol: str) -> bool:
    cooldowns = state.get("symbol_cooldowns", {})
    if symbol not in cooldowns:
        return False

    try:
        from datetime import datetime, timedelta
        last_time = datetime.fromisoformat(cooldowns[symbol])
    except Exception:
        return False

    return now_utc() < last_time + timedelta(minutes=config.SYMBOL_COOLDOWN_MINUTES)


def remember_symbol(state: Dict[str, Any], symbol: str) -> None:
    recent = state.get("recent_symbols", [])
    recent.append(symbol)
    state["recent_symbols"] = recent[-config.RECENT_SYMBOL_MEMORY:]
    state["last_traded_symbol"] = symbol


def has_open_symbol(state: Dict[str, Any], symbol: str) -> bool:
    return any(p.symbol == symbol for p in state_positions(state))


def passes_entry_filters(result: Dict[str, Any], adaptive: Dict[str, Any]) -> bool:
    score = int(result["weighted_score"])
    tf5 = result["timeframes"]["5m"]
    tf15 = result["timeframes"]["15m"]
    tf1h = result["timeframes"]["1h"]
    min_score = int(adaptive.get("min_score", config.MIN_SCORE_TO_BUY))

    if score < min_score:
        return False
    if tf15["score"] < 2 or tf1h["score"] < 1:
        return False
    if not result["all_bullish"] and score < (min_score + 4):
        return False
    if tf5["rsi"] > 74 and "breakout" not in tf5["reasons"]:
        return False
    if "momentum_down" in tf5["reasons"] and "momentum_up" not in tf15["reasons"]:
        return False
    if "rsi_weak" in tf15["reasons"] or "rsi_weak" in tf1h["reasons"]:
        return False
    return True


def apply_rotation_penalty(state: Dict[str, Any], symbol: str, score: int, adaptive: Optional[Dict[str, Any]] = None) -> int:
    adjusted = score

    if state.get("last_traded_symbol") == symbol:
        adjusted -= config.LAST_TRADED_SYMBOL_PENALTY

    recent_symbols = state.get("recent_symbols", [])
    if symbol in recent_symbols:
        adjusted -= config.RECENT_SYMBOL_PENALTY

    if adaptive is not None:
        adjusted -= int(adaptive.get("underperform_penalties", {}).get(symbol, 0))

    return adjusted


def choose_best_symbols(
    state: Dict[str, Any],
    max_picks: int,
    symbol_pool: List[str],
    adaptive: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    candidates = []
    adaptive = adaptive or build_adaptive_settings(state)

    for symbol in symbol_pool:
        if in_symbol_cooldown(state, symbol):
            if config.DEBUG_MODE:
                log_event(f"DEBUG {symbol} skipped (symbol cooldown active)")
            continue

        if has_open_symbol(state, symbol):
            if config.DEBUG_MODE:
                log_event(f"DEBUG {symbol} skipped (already open)")
            continue

        result = score_symbol(symbol)
        if result is None:
            continue

        base_score = result["weighted_score"]
        adjusted_score = apply_rotation_penalty(state, symbol, base_score, adaptive)
        result["base_weighted_score"] = base_score
        result["weighted_score"] = adjusted_score

        if config.DEBUG_MODE:
            tf5 = result["timeframes"]["5m"]
            tf15 = result["timeframes"]["15m"]
            tf1h = result["timeframes"]["1h"]
            log_event(
                f"DEBUG {symbol} base={base_score} adjusted={adjusted_score} "
                f"bullish={result['all_bullish']} "
                f"5m={tf5['score']} {tf5['reasons']} "
                f"15m={tf15['score']} {tf15['reasons']} "
                f"1h={tf1h['score']} {tf1h['reasons']}"
            )

        if passes_entry_filters(result, adaptive):
            candidates.append(result)
        elif config.DEBUG_MODE:
            log_event(f"DEBUG {symbol} filtered out by stricter entry rules")

    candidates.sort(key=lambda x: x["weighted_score"], reverse=True)
    return candidates[:max_picks]
