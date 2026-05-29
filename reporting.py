
import time
from typing import Any, Dict, Optional

import config
import runtime
from execution import in_cooldown, total_equity
from market_data import get_live_price
from state_manager import build_adaptive_settings, state_positions
from utils import send_discord_message


def maybe_send_discord_summary(state: Dict[str, Any], adaptive: Optional[Dict[str, Any]] = None, force: bool = False) -> None:
    now_ts = time.time()
    if not force and (now_ts - runtime.last_discord_summary) < config.DISCORD_SUMMARY_INTERVAL:
        return

    runtime.last_discord_summary = now_ts
    win_rate = (state["wins"] / state["trades_closed"] * 100.0) if state["trades_closed"] > 0 else 0.0
    adaptive = adaptive or build_adaptive_settings(state)

    send_discord_message(
        "📊 BOT SUMMARY\n"
        f"Mode: {'LIVE' if config.LIVE_TRADING else 'PAPER'}\n"
        f"Trades Closed: {state['trades_closed']}\n"
        f"Wins/Losses: {state['wins']}/{state['losses']}\n"
        f"Win Rate: {win_rate:.2f}%\n"
        f"Closed PnL: ${state['closed_pnl_usd']:.2f}\n"
        f"Max Drawdown: {state['max_drawdown_pct']:.2f}%\n"
        f"Adaptive Min Score: {adaptive.get('min_score', config.MIN_SCORE_TO_BUY)}\n"
        f"Adaptive Position Size: {adaptive.get('position_size', config.POSITION_SIZE):.3f}"
    )


def print_status(state: Dict[str, Any], adaptive: Optional[Dict[str, Any]] = None) -> None:
    positions = state_positions(state)
    equity = total_equity(state)

    total_pnl = equity - state["starting_balance"]
    win_rate = 0.0
    total_closed = state["wins"] + state["losses"]
    if total_closed > 0:
        win_rate = (state["wins"] / total_closed) * 100.0

    print("\n==================== STATUS ====================")
    print(f"Mode: {'LIVE' if config.LIVE_TRADING else 'PAPER'}")

    if not positions:
        print("No open positions")
    else:
        print(f"Open Positions: {len(positions)}")
        for pos in positions:
            current = get_live_price(pos.symbol)
            position_value = 0.0 if current is None else pos.quantity * current
            unrealized = position_value - pos.cost_basis
            unrealized_pct = 0.0 if current is None else ((current - pos.entry_price) / pos.entry_price) * 100.0

            print("------------------------------------------------")
            print(f"{pos.symbol}")
            print(f"Entry: {pos.entry_price:.8f}")
            print(f"Now: {current:.8f}" if current is not None else "Now: unavailable")
            print(f"Qty: {pos.quantity:.8f}")
            print(f"Position Value: ${position_value:.2f}")
            print(f"Unrealized P/L: ${unrealized:.2f} ({unrealized_pct:.2f}%)")
            print(f"Break-even Active: {pos.break_even_active}")
            print(f"Trailing Active: {pos.trailing_active}")
            print(f"Trailing Stop: {pos.trailing_stop_price}")
            print(f"Entry Score: {pos.entry_score}")

    print("------------------------------------------------")
    print(f"Cash Balance: ${state['cash_balance']:.2f}")
    print(f"Closed P/L: ${state['closed_pnl_usd']:.2f}")
    print(f"Daily Realized P/L: ${state['daily_realized_pnl']:.2f}")
    print(f"Total Equity: ${equity:.2f}")
    print(f"TOTAL P/L: ${total_pnl:.2f}")
    print("------------------------------------------------")
    print(f"Trades Closed: {state['trades_closed']}")
    print(f"Wins/Losses: {state['wins']}/{state['losses']}")
    print(f"Win Rate: {win_rate:.2f}%")
    print(f"Consecutive Losses: {state['consecutive_losses']}")
    best_trade_pct = state.get("best_trade_pct")
    best_trade_usd = state.get("best_trade_usd")
    worst_trade_pct = state.get("worst_trade_pct")
    worst_trade_usd = state.get("worst_trade_usd")

    best_trade_text = "None" if best_trade_pct is None or best_trade_usd is None else f"{best_trade_pct:.2f}% (${best_trade_usd:.2f})"
    worst_trade_text = "None" if worst_trade_pct is None or worst_trade_usd is None else f"{worst_trade_pct:.2f}% (${worst_trade_usd:.2f})"

    print(f"Best Trade: {best_trade_text}")
    print(f"Worst Trade: {worst_trade_text}")
    print(f"Equity Peak: ${state['equity_peak']:.2f}")
    print(f"Max Drawdown: {state['max_drawdown_pct']:.2f}%")
    print(f"Trading Halted Today: {state['trading_halted_for_day']}")
    print(f"In Cooldown: {in_cooldown(state)}")
    if adaptive is not None:
        recent_wr = adaptive.get("recent_win_rate")
        recent_wr_text = "n/a" if recent_wr is None else f"{recent_wr * 100.0:.2f}%"
        print(f"Adaptive Mode: {adaptive.get('market_mode', 'neutral')}")
        print(f"Adaptive Min Score: {adaptive.get('min_score', config.MIN_SCORE_TO_BUY)}")
        print(f"Adaptive Position Size: {adaptive.get('position_size', config.POSITION_SIZE):.3f}")
        print(f"Recent Win Rate (adaptive): {recent_wr_text}")
        underperform = adaptive.get("underperform_penalties", {})
        print(f"Underperform Penalties: {underperform if underperform else 'None'}")
    print(f"Last Traded Symbol: {state.get('last_traded_symbol')}")
    print(f"Recent Symbols: {state.get('recent_symbols')}")
    print("================================================")
