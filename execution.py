import os
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from coinbase.rest import RESTClient

import config
from market_data import get_live_price
from models import Position
from state_manager import (
    daily_loss_limit_hit,
    save_positions,
    save_state,
    state_positions,
)
from strategy import has_open_symbol, in_symbol_cooldown, remember_symbol
from utils import append_trade_csv, log_event, now_utc, send_discord_message


def get_exit_profile(entry_score: int) -> Dict[str, float]:
    # Higher confidence trades get more room to run.
    if entry_score >= 34:
        return {
            "break_even_trigger": 0.025,
            "profit_lock_trigger": 0.055,
            "profit_lock_pct": 0.018,
            "trailing_activate": 0.050,
            "trailing_stop_pct": 0.025,
        }

    if entry_score >= 28:
        return {
            "break_even_trigger": 0.022,
            "profit_lock_trigger": 0.045,
            "profit_lock_pct": 0.015,
            "trailing_activate": 0.040,
            "trailing_stop_pct": 0.022,
        }

    if entry_score >= 20:
        return {
            "break_even_trigger": 0.018,
            "profit_lock_trigger": 0.035,
            "profit_lock_pct": 0.012,
            "trailing_activate": 0.030,
            "trailing_stop_pct": 0.018,
        }

    return {
        "break_even_trigger": 0.015,
        "profit_lock_trigger": 0.025,
        "profit_lock_pct": 0.008,
        "trailing_activate": 0.022,
        "trailing_stop_pct": 0.015,
    }


def ensure_risk_state_defaults(state: Dict[str, Any]) -> None:
    state.setdefault("last_exit_was_loss", False)
    state.setdefault("loss_cluster_pause_until", None)
    state.setdefault("recent_trade_outcomes", [])


def get_loss_streak_cooldown_minutes(state: Dict[str, Any]) -> int:
    base = int(getattr(config, "COOLDOWN_MINUTES", 0) or 0)
    consecutive_losses = int(state.get("consecutive_losses", 0) or 0)

    if consecutive_losses >= 3:
        return max(base, 240)   # 4 hours
    if consecutive_losses == 2:
        return max(base, 120)   # 2 hours
    if consecutive_losses == 1:
        return max(base, 45)    # 45 min
    return base


def get_dynamic_min_score(state: Dict[str, Any]) -> int:
    base_score = int(max(getattr(config, "MIN_SCORE_TO_BUY", 1), 14))
    consecutive_losses = int(state.get("consecutive_losses", 0) or 0)

    if consecutive_losses >= 3:
        return base_score + 6
    if consecutive_losses == 2:
        return base_score + 3
    if consecutive_losses == 1:
        return base_score + 1
    return base_score


def get_dynamic_min_expected_profit_pct(state: Dict[str, Any]) -> float:
    base = float(getattr(config, "MIN_EXPECTED_PROFIT_PCT", 0.025))
    consecutive_losses = int(state.get("consecutive_losses", 0) or 0)

    if consecutive_losses >= 3:
        return max(base, 0.050)
    if consecutive_losses == 2:
        return max(base, 0.040)
    if consecutive_losses == 1:
        return max(base, 0.032)
    return max(base, 0.030)


def record_recent_trade_outcome(state: Dict[str, Any], was_loss: bool) -> None:
    ensure_risk_state_defaults(state)
    outcomes = list(state.get("recent_trade_outcomes", []))
    outcomes.append(1 if was_loss else 0)
    state["recent_trade_outcomes"] = outcomes[-10:]  # last 10 closed trades only


def recent_loss_count(state: Dict[str, Any]) -> int:
    ensure_risk_state_defaults(state)
    return sum(int(x) for x in state.get("recent_trade_outcomes", []))


def activate_loss_cluster_pause(state: Dict[str, Any], hours: int, reason: str) -> None:
    ensure_risk_state_defaults(state)
    pause_until = now_utc() + timedelta(hours=hours)
    state["loss_cluster_pause_until"] = pause_until.isoformat()
    log_event(f"{reason} -> pausing new entries until {pause_until.isoformat()}")
    send_discord_message(
        f"🛡️ {reason}\n"
        f"New entries paused until: {pause_until.isoformat()}"
    )


def total_equity(state: Dict[str, Any]) -> float:
    equity = state["cash_balance"]
    for pos in state_positions(state):
        current = get_live_price(pos.symbol)
        if current is not None:
            equity += pos.quantity * current
        else:
            equity += pos.cost_basis
    return equity


def update_equity_stats(state: Dict[str, Any]) -> None:
    equity = total_equity(state)
    if equity > state["equity_peak"]:
        state["equity_peak"] = equity

    peak = state["equity_peak"]
    if peak > 0:
        dd = ((peak - equity) / peak) * 100.0
        if dd > state["max_drawdown_pct"]:
            state["max_drawdown_pct"] = dd


def in_loss_cluster_pause(state: Dict[str, Any]) -> bool:
    ensure_risk_state_defaults(state)
    pause_until_raw = state.get("loss_cluster_pause_until")
    if not pause_until_raw:
        return False

    try:
        pause_until = datetime.fromisoformat(pause_until_raw)
    except Exception:
        return False

    return now_utc() < pause_until


def in_cooldown(state: Dict[str, Any]) -> bool:
    ensure_risk_state_defaults(state)

    if in_loss_cluster_pause(state):
        return True

    if not state.get("last_exit_time"):
        return False

    if not state.get("last_exit_was_loss", False):
        return False

    try:
        last_exit = datetime.fromisoformat(state["last_exit_time"])
    except Exception:
        return False

    cooldown_minutes = get_loss_streak_cooldown_minutes(state)
    if cooldown_minutes <= 0:
        return False

    return now_utc() < last_exit + timedelta(minutes=cooldown_minutes)


def can_open_new_trade(state: Dict[str, Any]) -> bool:
    ensure_risk_state_defaults(state)

    if state["trading_halted_for_day"]:
        return False
    if state["pending_order"]:
        return False
    if len(state_positions(state)) >= config.MAX_OPEN_POSITIONS:
        return False
    if in_cooldown(state):
        return False
    if daily_loss_limit_hit(state):
        state["trading_halted_for_day"] = True
        save_state(state)
        return False
    return True


class Trader:
    def __init__(self) -> None:
        self.rest = RESTClient(
            api_key=os.getenv("COINBASE_API_KEY"),
            api_secret=os.getenv("COINBASE_API_SECRET"),
            timeout=10,
            verbose=False,
        ) if config.LIVE_TRADING else None

    def market_buy(self, symbol: str, usd_size: float) -> Dict[str, Any]:
        client_order_id = str(uuid.uuid4())
        if not config.LIVE_TRADING:
            return {
                "ok": True,
                "client_order_id": client_order_id,
                "order_id": None,
                "filled": True,
            }

        try:
            resp = self.rest.market_order_buy(
                client_order_id=client_order_id,
                product_id=symbol,
                quote_size=str(round(usd_size, 2)),
            )
            data = resp.to_dict() if hasattr(resp, "to_dict") else dict(resp)
            return {
                "ok": True,
                "client_order_id": client_order_id,
                "order_id": data.get("order_id"),
                "raw": data,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def market_sell(self, symbol: str, base_size: float) -> Dict[str, Any]:
        client_order_id = str(uuid.uuid4())
        if not config.LIVE_TRADING:
            return {
                "ok": True,
                "client_order_id": client_order_id,
                "order_id": None,
                "filled": True,
            }

        try:
            resp = self.rest.market_order_sell(
                client_order_id=client_order_id,
                product_id=symbol,
                base_size=f"{base_size:.8f}",
            )
            data = resp.to_dict() if hasattr(resp, "to_dict") else dict(resp)
            return {
                "ok": True,
                "client_order_id": client_order_id,
                "order_id": data.get("order_id"),
                "raw": data,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}


def open_position(
    state: Dict[str, Any],
    trader: Trader,
    pick: Dict[str, Any],
    adaptive: Optional[Dict[str, Any]] = None,
) -> None:
    ensure_risk_state_defaults(state)

    if not can_open_new_trade(state):
        return

    if in_symbol_cooldown(state, pick["symbol"]):
        log_event(f"Skipping {pick['symbol']} (symbol cooldown active)")
        return

    if has_open_symbol(state, pick["symbol"]):
        log_event(f"Skipping {pick['symbol']} (already open)")
        return

    if sum(1 for p in state_positions(state) if p.symbol == pick["symbol"]) >= config.MAX_TRADES_PER_SYMBOL:
        log_event(f"Skipping {pick['symbol']} (max trades per symbol reached)")
        return

    min_fee_score = get_dynamic_min_score(state)
    weighted_score = int(pick.get("weighted_score", 0) or 0)
    if weighted_score < min_fee_score:
        log_event(
            f"Skipping {pick['symbol']} (score too low: "
            f"{weighted_score} < {min_fee_score}, consecutive_losses={state.get('consecutive_losses', 0)})"
        )
        return

    price = get_live_price(pick["symbol"])
    if price is None:
        log_event(f"Cannot buy {pick['symbol']}: no live price")
        return

    min_expected_profit_pct = get_dynamic_min_expected_profit_pct(state)
    expected_move = float(pick.get("expected_move_pct", min_expected_profit_pct))
    if expected_move < min_expected_profit_pct:
        log_event(
            f"Skipping {pick['symbol']} (expected move too small: "
            f"{expected_move:.4f} < {min_expected_profit_pct:.4f})"
        )
        return

    position_size = float((adaptive or {}).get("position_size", config.POSITION_SIZE))
    usd_to_use = state["cash_balance"] * position_size
    if usd_to_use < config.MIN_NOTIONAL_USD:
        log_event(f"Cannot buy {pick['symbol']}: notional too small (${usd_to_use:.2f})")
        return

    state["pending_order"] = True
    save_state(state)

    result = trader.market_buy(pick["symbol"], usd_to_use)
    if not result["ok"]:
        state["pending_order"] = False
        save_state(state)
        log_event(f"BUY FAILED {pick['symbol']} | {result['error']}")
        return

    qty = usd_to_use / price
    pos = Position(
        symbol=pick["symbol"],
        entry_price=price,
        quantity=qty,
        cost_basis=usd_to_use,
        opened_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        highest_price=price,
        trailing_active=False,
        trailing_stop_price=None,
        break_even_active=False,
        entry_score=weighted_score,
        order_id=result.get("order_id"),
        client_order_id=result.get("client_order_id"),
    )

    positions = state_positions(state)
    positions.append(pos)

    state["cash_balance"] -= usd_to_use
    save_positions(state, positions)
    remember_symbol(state, pos.symbol)
    state["pending_order"] = False
    save_state(state)

    log_event(
        f"BUY {pos.symbol} | mode={'LIVE' if config.LIVE_TRADING else 'PAPER'} | "
        f"entry={pos.entry_price:.8f} qty={pos.quantity:.8f} used=${usd_to_use:.2f} "
        f"cash_left=${state['cash_balance']:.2f} score={pos.entry_score} "
        f"expected_move={expected_move:.4f} pos_size={position_size:.3f} "
        f"loss_streak={state.get('consecutive_losses', 0)} "
        f"recent_loss_count={recent_loss_count(state)}"
    )

    send_discord_message(
        f"🟢 BUY: {pos.symbol}\n"
        f"Mode: {'LIVE' if config.LIVE_TRADING else 'PAPER'}\n"
        f"Entry: {pos.entry_price:.8f}\n"
        f"Size: ${usd_to_use:.2f}\n"
        f"Score: {pos.entry_score}\n"
        f"Expected Move: {expected_move:.2%}\n"
        f"Loss Streak: {state.get('consecutive_losses', 0)}\n"
        f"Recent Losses (last 10): {recent_loss_count(state)}\n"
        f"Cash Left: ${state['cash_balance']:.2f}"
    )


def close_position(state: Dict[str, Any], trader: Trader, pos: Position, reason: str) -> None:
    ensure_risk_state_defaults(state)

    if state["pending_order"]:
        return

    price = get_live_price(pos.symbol)
    if price is None:
        log_event(f"Cannot sell {pos.symbol}: no live price")
        return

    state["pending_order"] = True
    save_state(state)

    result = trader.market_sell(pos.symbol, pos.quantity)
    if not result["ok"]:
        state["pending_order"] = False
        save_state(state)
        log_event(f"SELL FAILED {pos.symbol} | {result['error']}")
        return

    sale_value = pos.quantity * price
    pnl_usd = sale_value - pos.cost_basis
    pnl_pct = ((price - pos.entry_price) / pos.entry_price) * 100.0

    state["cash_balance"] += sale_value
    state["closed_pnl_usd"] += pnl_usd
    state["daily_realized_pnl"] += pnl_usd
    state["trades_closed"] += 1
    state["last_exit_time"] = now_utc().isoformat()

    was_loss = pnl_usd < 0

    if not was_loss:
        state["wins"] += 1
        state["gross_profit"] += pnl_usd
        state["consecutive_losses"] = 0
        state["last_exit_was_loss"] = False
    else:
        state["losses"] += 1
        state["gross_loss"] += abs(pnl_usd)
        state["consecutive_losses"] += 1
        state["last_exit_was_loss"] = True

    record_recent_trade_outcome(state, was_loss)

    if state["best_trade_usd"] is None or pnl_usd > state["best_trade_usd"]:
        state["best_trade_usd"] = pnl_usd
    if state["worst_trade_usd"] is None or pnl_usd < state["worst_trade_usd"]:
        state["worst_trade_usd"] = pnl_usd

    if state.get("best_trade_pct") is None or pnl_pct > state["best_trade_pct"]:
        state["best_trade_pct"] = pnl_pct
    if state.get("worst_trade_pct") is None or pnl_pct < state["worst_trade_pct"]:
        state["worst_trade_pct"] = pnl_pct

    append_trade_csv(
        mode="LIVE" if config.LIVE_TRADING else "PAPER",
        symbol=pos.symbol,
        side="SELL",
        entry_price=pos.entry_price,
        exit_price=price,
        qty=pos.quantity,
        cost_basis=pos.cost_basis,
        sale_value=sale_value,
        pnl_usd=pnl_usd,
        pnl_pct=pnl_pct,
        reason=reason,
        client_order_id=result.get("client_order_id"),
        exchange_order_id=result.get("order_id"),
    )

    log_event(
        f"SELL {pos.symbol} | mode={'LIVE' if config.LIVE_TRADING else 'PAPER'} | "
        f"exit={price:.8f} qty={pos.quantity:.8f} pnl=${pnl_usd:.2f} ({pnl_pct:.2f}%) "
        f"reason={reason} cash_now=${state['cash_balance']:.2f} "
        f"consecutive_losses={state.get('consecutive_losses', 0)} "
        f"recent_loss_count={recent_loss_count(state)}"
    )

    send_discord_message(
        f"🔴 SELL: {pos.symbol}\n"
        f"Mode: {'LIVE' if config.LIVE_TRADING else 'PAPER'}\n"
        f"Exit: {price:.8f}\n"
        f"PnL: {pnl_pct:.2f}% (${pnl_usd:.2f})\n"
        f"Reason: {reason}\n"
        f"Consecutive Losses: {state.get('consecutive_losses', 0)}\n"
        f"Recent Losses (last 10): {recent_loss_count(state)}\n"
        f"Cash Now: ${state['cash_balance']:.2f}"
    )

    if was_loss:
        cooldown_minutes = get_loss_streak_cooldown_minutes(state)
        log_event(
            f"Loss cooldown active for {cooldown_minutes} minutes "
            f"(consecutive_losses={state.get('consecutive_losses', 0)})"
        )
        send_discord_message(
            f"🛡️ Loss cooldown active: {cooldown_minutes} minutes\n"
            f"Consecutive Losses: {state.get('consecutive_losses', 0)}"
        )

    # Hard pause after 3 straight losses
    if int(state.get("consecutive_losses", 0)) >= 3:
        activate_loss_cluster_pause(
            state,
            hours=6,
            reason="3 straight losses detected",
        )

    # Hard pause if 5 of last 10 trades were losses
    if recent_loss_count(state) >= 5 and len(state.get("recent_trade_outcomes", [])) >= 7:
        activate_loss_cluster_pause(
            state,
            hours=8,
            reason="loss cluster detected (5 losses in last 10 trades)",
        )

    state["symbol_cooldowns"][pos.symbol] = now_utc().isoformat()
    remember_symbol(state, pos.symbol)

    positions = [p for p in state_positions(state) if p.symbol != pos.symbol]
    save_positions(state, positions)
    state["pending_order"] = False

    if daily_loss_limit_hit(state):
        state["trading_halted_for_day"] = True
        log_event("DAILY LOSS LIMIT HIT -> trading halted for today")
        send_discord_message("🛑 Daily loss limit hit -> trading halted for today")

    save_state(state)


def manage_open_positions(state: Dict[str, Any], trader: Trader) -> None:
    positions = state_positions(state)
    updated_positions: List[Position] = []
    positions_to_close: List[Tuple[Position, str]] = []

    for pos in positions:
        current = get_live_price(pos.symbol)
        if current is None:
            updated_positions.append(pos)
            continue

        if current > pos.highest_price:
            pos.highest_price = current

        gain_pct = (current - pos.entry_price) / pos.entry_price
        exit_profile = get_exit_profile(pos.entry_score)

        if not pos.break_even_active and gain_pct >= exit_profile["break_even_trigger"]:
            pos.break_even_active = True

        if not pos.trailing_active and gain_pct >= exit_profile["trailing_activate"]:
            pos.trailing_active = True

        if pos.trailing_active:
            candidate_stop = pos.highest_price * (1 - exit_profile["trailing_stop_pct"])
            if pos.trailing_stop_price is None:
                pos.trailing_stop_price = candidate_stop
            else:
                pos.trailing_stop_price = max(pos.trailing_stop_price, candidate_stop)

        hard_stop_price = pos.entry_price * (1 - config.STOP_LOSS_PCT)
        break_even_price = None

        if pos.break_even_active:
            break_even_price = pos.entry_price
            if gain_pct >= exit_profile["profit_lock_trigger"]:
                break_even_price = max(
                    break_even_price,
                    pos.entry_price * (1 + exit_profile["profit_lock_pct"]),
                )

        if current <= hard_stop_price:
            positions_to_close.append((pos, "stop_loss"))
            continue

        if break_even_price is not None and current <= break_even_price and gain_pct > 0:
            positions_to_close.append((pos, "break_even"))
            continue

        if pos.trailing_active and pos.trailing_stop_price is not None and current <= pos.trailing_stop_price:
            positions_to_close.append((pos, "trailing_stop"))
            continue

        updated_positions.append(pos)

    save_positions(state, updated_positions)
    save_state(state)

    for pos, reason in positions_to_close:
        close_position(state, trader, pos, reason)