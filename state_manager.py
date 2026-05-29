
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config
import runtime
from models import Position
from utils import load_json, save_json, log_event, parse_trade_timestamp, today_str


def default_state() -> Dict[str, Any]:
    return {
        "cash_balance": config.STARTING_BALANCE,
        "starting_balance": config.STARTING_BALANCE,
        "closed_pnl_usd": 0.0,
        "wins": 0,
        "losses": 0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "best_trade_usd": None,
        "worst_trade_usd": None,
        "best_trade_pct": None,
        "worst_trade_pct": None,
        "equity_peak": config.STARTING_BALANCE,
        "max_drawdown_pct": 0.0,
        "trades_closed": 0,
        "consecutive_losses": 0,
        "last_exit_time": None,
        "daily_realized_pnl": 0.0,
        "daily_pnl_date": today_str(),
        "trading_halted_for_day": False,
        "pending_order": False,
        "positions": [],
        "position": None,
        "symbol_cooldowns": {},
        "last_traded_symbol": None,
        "recent_symbols": [],
    }


def state_positions(state: Dict[str, Any]) -> List[Position]:
    raw_positions = state.get("positions", [])
    return [Position(**p) for p in raw_positions]


def save_positions(state: Dict[str, Any], positions: List[Position]) -> None:
    state["positions"] = [asdict(p) for p in positions]
    state["position"] = state["positions"][0] if state["positions"] else None


def daily_loss_limit_hit(state: Dict[str, Any]) -> bool:
    return state["daily_realized_pnl"] <= -(state["starting_balance"] * config.MAX_DAILY_LOSS_PCT)


def rebuild_stats_from_trade_history(state: Dict[str, Any]) -> Dict[str, Any]:
    if not os.path.exists(config.TRADE_CSV):
        return state

    closed_rows: List[Dict[str, Any]] = []
    try:
        with open(config.TRADE_CSV, "r", encoding="utf-8") as f:
            import csv
            reader = csv.DictReader(f)
            for row in reader:
                if str(row.get("side", "")).upper() != "SELL":
                    continue
                try:
                    pnl_usd = float(row.get("pnl_usd") or 0.0)
                    pnl_pct = float(row.get("pnl_pct") or 0.0)
                except Exception:
                    continue
                ts = parse_trade_timestamp(str(row.get("timestamp", "")).strip())
                closed_rows.append({
                    "timestamp": ts,
                    "pnl_usd": pnl_usd,
                    "pnl_pct": pnl_pct,
                })
    except Exception as e:
        log_event(f"Trade history rebuild skipped: {e}")
        return state

    if not closed_rows:
        return state

    closed_rows.sort(key=lambda x: x["timestamp"] or datetime.min.replace(tzinfo=timezone.utc))

    wins = sum(1 for row in closed_rows if row["pnl_usd"] >= 0)
    losses = sum(1 for row in closed_rows if row["pnl_usd"] < 0)
    gross_profit = sum(row["pnl_usd"] for row in closed_rows if row["pnl_usd"] >= 0)
    gross_loss = sum(abs(row["pnl_usd"]) for row in closed_rows if row["pnl_usd"] < 0)
    closed_pnl_usd = sum(row["pnl_usd"] for row in closed_rows)

    best_trade_usd = max(row["pnl_usd"] for row in closed_rows)
    worst_trade_usd = min(row["pnl_usd"] for row in closed_rows)
    best_trade_pct = max(row["pnl_pct"] for row in closed_rows)
    worst_trade_pct = min(row["pnl_pct"] for row in closed_rows)

    running_equity = float(state.get("starting_balance", config.STARTING_BALANCE))
    equity_peak = running_equity
    max_drawdown_pct = 0.0
    for row in closed_rows:
        running_equity += row["pnl_usd"]
        if running_equity > equity_peak:
            equity_peak = running_equity
        if equity_peak > 0:
            drawdown_pct = ((equity_peak - running_equity) / equity_peak) * 100.0
            if drawdown_pct > max_drawdown_pct:
                max_drawdown_pct = drawdown_pct

    consecutive_losses = 0
    for row in reversed(closed_rows):
        if row["pnl_usd"] < 0:
            consecutive_losses += 1
        else:
            break

    today = today_str()
    daily_realized_pnl = 0.0
    last_exit_time = None
    for row in closed_rows:
        ts = row["timestamp"]
        if ts is not None:
            last_exit_time = ts.isoformat()
            if ts.astimezone(timezone.utc).strftime("%Y-%m-%d") == today:
                daily_realized_pnl += row["pnl_usd"]

    state["closed_pnl_usd"] = round(closed_pnl_usd, 2)
    state["wins"] = wins
    state["losses"] = losses
    state["gross_profit"] = round(gross_profit, 2)
    state["gross_loss"] = round(gross_loss, 2)
    state["best_trade_usd"] = best_trade_usd
    state["worst_trade_usd"] = worst_trade_usd
    state["best_trade_pct"] = best_trade_pct
    state["worst_trade_pct"] = worst_trade_pct
    state["equity_peak"] = max(float(state.get("equity_peak", config.STARTING_BALANCE)), equity_peak)
    state["max_drawdown_pct"] = max(float(state.get("max_drawdown_pct", 0.0)), max_drawdown_pct)
    state["trades_closed"] = len(closed_rows)
    state["consecutive_losses"] = consecutive_losses
    state["last_exit_time"] = last_exit_time
    state["daily_realized_pnl"] = round(daily_realized_pnl, 2)
    state["trading_halted_for_day"] = daily_loss_limit_hit(state)

    if not state_positions(state):
        state["cash_balance"] = round(float(state.get("starting_balance", config.STARTING_BALANCE)) + closed_pnl_usd, 2)

    return state


def load_state() -> Dict[str, Any]:
    state = load_json(config.STATE_FILE, default_state())
    defaults = default_state()

    for k, v in defaults.items():
        state.setdefault(k, v)

    if not state.get("positions") and state.get("position") is not None:
        state["positions"] = [state["position"]]

    if state.get("positions"):
        state["position"] = state["positions"][0]
    else:
        state["position"] = None

    if state["daily_pnl_date"] != today_str():
        state["daily_pnl_date"] = today_str()
        state["daily_realized_pnl"] = 0.0
        state["trading_halted_for_day"] = False
        state["consecutive_losses"] = 0

    state = rebuild_stats_from_trade_history(state)

    runtime.last_good_state = json.loads(json.dumps(state))
    return state


def save_state(state: Dict[str, Any]) -> None:
    save_json(config.STATE_FILE, state)
    runtime.last_good_state = json.loads(json.dumps(state))


def get_recent_closed_trades(limit: int = config.ADAPTIVE_LOOKBACK_TRADES) -> List[Dict[str, Any]]:
    if not os.path.exists(config.TRADE_CSV):
        return []

    rows: List[Dict[str, Any]] = []
    try:
        with open(config.TRADE_CSV, "r", encoding="utf-8") as f:
            import csv
            reader = csv.DictReader(f)
            for row in reader:
                if str(row.get("side", "")).upper() != "SELL":
                    continue
                try:
                    pnl_usd = float(row.get("pnl_usd") or 0.0)
                    pnl_pct = float(row.get("pnl_pct") or 0.0)
                except Exception:
                    continue
                rows.append({
                    "symbol": str(row.get("symbol", "")).strip(),
                    "reason": str(row.get("reason", "")).strip(),
                    "pnl_usd": pnl_usd,
                    "pnl_pct": pnl_pct,
                    "timestamp": parse_trade_timestamp(str(row.get("timestamp", "")).strip()),
                })
    except Exception as e:
        log_event(f"Adaptive history read skipped: {e}")
        return []

    rows.sort(key=lambda x: x["timestamp"] or datetime.min.replace(tzinfo=timezone.utc))
    return rows[-limit:] if limit > 0 else rows


def build_adaptive_settings(state: Dict[str, Any]) -> Dict[str, Any]:
    recent = get_recent_closed_trades(config.ADAPTIVE_LOOKBACK_TRADES)
    settings = {
        "min_score": config.ADAPTIVE_MIN_SCORE_FLOOR,
        "position_size": config.POSITION_SIZE,
        "underperform_penalties": {},
        "market_mode": "neutral",
        "recent_win_rate": None,
        "recent_pnl_usd": 0.0,
    }

    symbol_rows = get_recent_closed_trades(config.UNDERPERFORM_SYMBOL_LOOKBACK)
    symbol_stats: Dict[str, Dict[str, float]] = {}
    for row in symbol_rows:
        symbol = row["symbol"]
        if not symbol:
            continue
        entry = symbol_stats.setdefault(symbol, {"count": 0, "wins": 0, "pnl": 0.0})
        entry["count"] += 1
        if row["pnl_usd"] > 0:
            entry["wins"] += 1
        entry["pnl"] += row["pnl_usd"]

    for symbol, stats in symbol_stats.items():
        count = int(stats["count"])
        win_rate = (stats["wins"] / count) if count else 0.0
        if count >= config.UNDERPERFORM_SYMBOL_MIN_TRADES and win_rate <= config.UNDERPERFORM_SYMBOL_MAX_WINRATE and stats["pnl"] < 0:
            settings["underperform_penalties"][symbol] = config.UNDERPERFORM_SYMBOL_PENALTY

    if not recent:
        return settings

    wins = sum(1 for row in recent if row["pnl_usd"] > 0)
    recent_win_rate = wins / len(recent)
    recent_pnl = sum(row["pnl_usd"] for row in recent)
    recent_avg_pct = sum(row["pnl_pct"] for row in recent) / len(recent)

    settings["recent_win_rate"] = recent_win_rate
    settings["recent_pnl_usd"] = recent_pnl

    if recent_win_rate < 0.50 or recent_pnl < 0 or recent_avg_pct < 0:
        settings["min_score"] = min(config.ADAPTIVE_MIN_SCORE_CAP, config.ADAPTIVE_MIN_SCORE_FLOOR + config.ADAPTIVE_MIN_SCORE_STEP)
        settings["position_size"] = max(config.ADAPTIVE_MIN_POSITION_SIZE, config.POSITION_SIZE * config.ADAPTIVE_POSITION_DOWNSHIFT)
        settings["market_mode"] = "defensive"
    elif recent_win_rate >= 0.67 and recent_pnl > 0:
        settings["min_score"] = config.ADAPTIVE_MIN_SCORE_FLOOR
        settings["position_size"] = min(config.ADAPTIVE_MAX_POSITION_SIZE, config.POSITION_SIZE * config.ADAPTIVE_POSITION_UPSHIFT)
        settings["market_mode"] = "aggressive"
    else:
        settings["min_score"] = min(config.ADAPTIVE_MIN_SCORE_CAP, config.ADAPTIVE_MIN_SCORE_FLOOR + 1)

    settings["min_score"] = max(config.ADAPTIVE_MIN_SCORE_FLOOR, min(config.ADAPTIVE_MIN_SCORE_CAP, int(settings["min_score"])))
    return settings
