import atexit
import csv
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

import config
import runtime
from execution import (
    Trader,
    can_open_new_trade,
    manage_open_positions,
    open_position,
    update_equity_stats,
)
from market_data import get_scan_symbols
from reporting import maybe_send_discord_summary, print_status
from state_manager import build_adaptive_settings, load_state, save_state, state_positions
from strategy import choose_best_symbols
from utils import (
    acquire_single_instance_lock,
    ensure_trade_csv,
    log_event,
    release_single_instance_lock,
    send_discord_message,
)
from websocket_manager import start_market_ws, start_user_ws


app = FastAPI(title="Crypto Bot API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class StartBotRequest(BaseModel):
    debug: Optional[bool] = None


bot_thread: Optional[threading.Thread] = None
stop_event = threading.Event()
bot_running = False
bot_started_at: Optional[str] = None
last_heartbeat: Optional[str] = None
last_error: Optional[str] = None
last_status: str = "stopped"
ws_started = False
lock_acquired = False

bot_stats = {
    "mode": "LIVE" if config.LIVE_TRADING else "PAPER",
    "status": "stopped",
    "open_positions": 0,
    "equity": None,
    "cash": None,
    "last_action": "idle",
    "last_pick": None,
    "risk_guard": {
        "enabled": True,
        "in_cooldown": False,
        "cooldown_until": None,
        "daily_loss_limit_hit": False,
        "daily_realized_pnl": 0.0,
        "consecutive_losses": 0,
        "last_guard_reason": None,
    },
}

# ---------------------------
# Risk guard settings
# ---------------------------
LOSS_STREAK_COOLDOWN_2_MINUTES = 60
LOSS_STREAK_COOLDOWN_3_MINUTES = 180
MAX_DAILY_LOSS_USD = -25.0

# Try a few likely trade log names/paths
TRADE_LOG_CANDIDATES = [
    getattr(config, "TRADE_CSV_PATH", None),
    getattr(config, "TRADE_LOG_PATH", None),
    "trades.csv",
    "trade_log.csv",
    "trade_history.csv",
]

risk_guard_state = {
    "cooldown_until": None,
    "last_guard_reason": None,
    "last_announced_cooldown_until": None,
    "last_processed_trade_key": None,
}


def cleanup() -> None:
    global lock_acquired
    if lock_acquired:
        try:
            release_single_instance_lock()
        except Exception:
            pass
        lock_acquired = False


atexit.register(cleanup)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def initialize_bot_once() -> None:
    global lock_acquired

    ensure_trade_csv()

    if not lock_acquired:
        acquire_single_instance_lock()
        lock_acquired = True


def start_websockets_once() -> None:
    global ws_started

    if not ws_started:
        start_market_ws()
        start_user_ws()
        ws_started = True
        time.sleep(3)


def find_trade_log_path() -> Optional[str]:
    for candidate in TRADE_LOG_CANDIDATES:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def parse_trade_timestamp(ts: str) -> Optional[datetime]:
    if not ts:
        return None

    ts = ts.strip()

    # Handles "2026-04-18 08:28:05"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue

    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def load_trade_rows() -> list[dict]:
    trade_log_path = find_trade_log_path()
    if not trade_log_path:
        return []

    rows: list[dict] = []
    try:
        with open(trade_log_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row:
                    continue
                if str(row.get("side", "")).upper() != "SELL":
                    continue
                rows.append(row)
    except Exception as e:
        log_event(f"Risk guard could not read trade log: {e}")
        return []

    return rows


def get_trade_key(row: dict) -> str:
    return "|".join(
        [
            str(row.get("timestamp", "")),
            str(row.get("symbol", "")),
            str(row.get("client_order_id", "")),
            str(row.get("exchange_order_id", "")),
            str(row.get("sale_value", "")),
        ]
    )


def get_today_trade_metrics() -> dict:
    """
    Computes:
    - daily realized pnl
    - consecutive losses from the most recent SELL trades
    - latest trade key
    """
    rows = load_trade_rows()
    now = utc_now()
    today_str = now.strftime("%Y-%m-%d")

    daily_realized_pnl = 0.0
    consecutive_losses = 0
    latest_trade_key = None

    parsed_rows: list[tuple[datetime, dict]] = []
    for row in rows:
        ts = parse_trade_timestamp(str(row.get("timestamp", "")))
        if ts is None:
            continue
        parsed_rows.append((ts, row))

    parsed_rows.sort(key=lambda x: x[0])

    # Today's PnL
    for ts, row in parsed_rows:
        if ts.strftime("%Y-%m-%d") == today_str:
            daily_realized_pnl += safe_float(row.get("pnl_usd", 0.0), 0.0)

    # Consecutive losses from latest trades
    for ts, row in reversed(parsed_rows):
        pnl = safe_float(row.get("pnl_usd", 0.0), 0.0)
        if pnl < 0:
            consecutive_losses += 1
        else:
            break

    if parsed_rows:
        latest_trade_key = get_trade_key(parsed_rows[-1][1])

    return {
        "daily_realized_pnl": daily_realized_pnl,
        "consecutive_losses": consecutive_losses,
        "latest_trade_key": latest_trade_key,
        "trade_count": len(parsed_rows),
    }


def maybe_trigger_cooldown(metrics: dict) -> None:
    consecutive_losses = int(metrics.get("consecutive_losses", 0))
    latest_trade_key = metrics.get("latest_trade_key")

    # Only trigger once per newly seen latest trade
    if latest_trade_key == risk_guard_state.get("last_processed_trade_key"):
        return

    risk_guard_state["last_processed_trade_key"] = latest_trade_key

    if consecutive_losses >= 3:
        cooldown_until = utc_now().replace(microsecond=0) + \
            __import__("datetime").timedelta(minutes=LOSS_STREAK_COOLDOWN_3_MINUTES)
        risk_guard_state["cooldown_until"] = cooldown_until
        risk_guard_state["last_guard_reason"] = (
            f"3 straight losses -> cooldown for {LOSS_STREAK_COOLDOWN_3_MINUTES} minutes"
        )
    elif consecutive_losses >= 2:
        cooldown_until = utc_now().replace(microsecond=0) + \
            __import__("datetime").timedelta(minutes=LOSS_STREAK_COOLDOWN_2_MINUTES)
        risk_guard_state["cooldown_until"] = cooldown_until
        risk_guard_state["last_guard_reason"] = (
            f"2 straight losses -> cooldown for {LOSS_STREAK_COOLDOWN_2_MINUTES} minutes"
        )


def in_cooldown() -> bool:
    cooldown_until = risk_guard_state.get("cooldown_until")
    if cooldown_until is None:
        return False
    return utc_now() < cooldown_until


def daily_loss_limit_hit(metrics: dict) -> bool:
    return float(metrics.get("daily_realized_pnl", 0.0)) <= MAX_DAILY_LOSS_USD


def maybe_send_risk_guard_alert(metrics: dict) -> None:
    cooldown_until = risk_guard_state.get("cooldown_until")
    last_announced = risk_guard_state.get("last_announced_cooldown_until")
    reason = risk_guard_state.get("last_guard_reason")

    if cooldown_until is not None and cooldown_until != last_announced and in_cooldown():
        msg = (
            "🛡️ Risk guard activated | "
            f"reason={reason} | "
            f"consecutive_losses={metrics.get('consecutive_losses', 0)} | "
            f"daily_pnl={metrics.get('daily_realized_pnl', 0.0):.2f} | "
            f"cooldown_until={iso_utc(cooldown_until)}"
        )
        log_event(msg)
        send_discord_message(msg)
        risk_guard_state["last_announced_cooldown_until"] = cooldown_until


def update_risk_guard_stats(metrics: dict) -> None:
    bot_stats["risk_guard"] = {
        "enabled": True,
        "in_cooldown": in_cooldown(),
        "cooldown_until": iso_utc(risk_guard_state.get("cooldown_until")),
        "daily_loss_limit_hit": daily_loss_limit_hit(metrics),
        "daily_realized_pnl": round(float(metrics.get("daily_realized_pnl", 0.0)), 2),
        "consecutive_losses": int(metrics.get("consecutive_losses", 0)),
        "last_guard_reason": risk_guard_state.get("last_guard_reason"),
    }


def run_bot_loop() -> None:
    global bot_running, bot_started_at, last_heartbeat, last_error, last_status

    try:
        initialize_bot_once()

        trader = Trader()

        log_event("Starting pro-retail bot...")
        send_discord_message(
            f"🚀 Bot started | mode={'LIVE' if config.LIVE_TRADING else 'PAPER'}"
        )

        start_websockets_once()

        bot_running = True
        last_status = "running"
        bot_started_at = datetime.utcnow().isoformat()
        bot_stats["status"] = "running"
        bot_stats["mode"] = "LIVE" if config.LIVE_TRADING else "PAPER"

        while not stop_event.is_set():
            try:
                last_heartbeat = datetime.utcnow().isoformat()

                with runtime.state_lock:
                    state = load_state()

                    bot_stats["last_action"] = "manage_open_positions"
                    manage_open_positions(state, trader)
                    state = load_state()

                    adaptive = build_adaptive_settings(state)

                    # ---------------------------
                    # Risk guard evaluation
                    # ---------------------------
                    metrics = get_today_trade_metrics()
                    maybe_trigger_cooldown(metrics)
                    maybe_send_risk_guard_alert(metrics)
                    update_risk_guard_stats(metrics)

                    risk_blocked = False
                    if daily_loss_limit_hit(metrics):
                        risk_blocked = True
                        bot_stats["last_action"] = "risk_guard:daily_loss_limit"
                        if bot_stats["risk_guard"]["last_guard_reason"] != "daily loss limit hit":
                            risk_guard_state["last_guard_reason"] = "daily loss limit hit"
                            msg = (
                                "🛑 Daily loss limit hit | "
                                f"daily_pnl={metrics.get('daily_realized_pnl', 0.0):.2f} | "
                                "new entries paused until next UTC day"
                            )
                            log_event(msg)
                            send_discord_message(msg)
                            update_risk_guard_stats(metrics)

                    elif in_cooldown():
                        risk_blocked = True
                        bot_stats["last_action"] = "risk_guard:cooldown"

                    if can_open_new_trade(state) and not risk_blocked:
                        open_slots = config.MAX_OPEN_POSITIONS - len(state_positions(state))
                        scan_symbols = get_scan_symbols()

                        if config.DEBUG_MODE:
                            log_event(f"Scanning {len(scan_symbols)} symbols: {scan_symbols}")

                        bot_stats["last_action"] = "choose_best_symbols"
                        picks = choose_best_symbols(state, open_slots, scan_symbols, adaptive)

                        if picks:
                            for pick in picks:
                                bot_stats["last_pick"] = pick["symbol"]

                                log_event(
                                    f"BEST PICK {pick['symbol']} score={pick['weighted_score']} "
                                    f"base_score={pick.get('base_weighted_score', pick['weighted_score'])} "
                                    f"bullish={pick['all_bullish']} price={pick['latest_price']:.8f}"
                                )

                                bot_stats["last_action"] = f"open_position:{pick['symbol']}"
                                open_position(state, trader, pick, adaptive)

                                state = load_state()
                                if not can_open_new_trade(state):
                                    break
                        else:
                            log_event("No symbol passed buy filter this cycle")
                            bot_stats["last_action"] = "no_valid_pick"
                    elif risk_blocked:
                        if bot_stats["risk_guard"]["daily_loss_limit_hit"]:
                            log_event(
                                f"Risk guard blocking new entries: daily_pnl="
                                f"{bot_stats['risk_guard']['daily_realized_pnl']:.2f}"
                            )
                        elif bot_stats["risk_guard"]["in_cooldown"]:
                            log_event(
                                f"Risk guard blocking new entries: cooldown_until="
                                f"{bot_stats['risk_guard']['cooldown_until']}"
                            )

                    bot_stats["last_action"] = "update_equity"
                    update_equity_stats(state)
                    save_state(state)

                    adaptive = build_adaptive_settings(state)
                    print_status(state, adaptive)
                    maybe_send_discord_summary(state, adaptive)

                    try:
                        positions = state_positions(state)
                        bot_stats["open_positions"] = len(positions)
                    except Exception:
                        bot_stats["open_positions"] = 0

                    try:
                        bot_stats["equity"] = state.get("equity")
                    except Exception:
                        bot_stats["equity"] = None

                    try:
                        bot_stats["cash"] = state.get("cash")
                    except Exception:
                        bot_stats["cash"] = None

                sleep_seconds = max(1, int(config.SCAN_INTERVAL_SECONDS))
                for _ in range(sleep_seconds):
                    if stop_event.is_set():
                        break
                    time.sleep(1)

            except Exception as e:
                last_error = str(e)
                log_event(f"Main loop error: {e}")
                time.sleep(3)

    except Exception as e:
        last_error = str(e)
        last_status = "error"
        bot_stats["status"] = "error"
        log_event(f"Fatal bot error: {e}")

    finally:
        bot_running = False
        last_status = "stopped"
        bot_stats["status"] = "stopped"
        bot_stats["last_action"] = "stopped"
        send_discord_message("🛑 Bot stopped.")


@app.get("/")
def root():
    return {
        "message": "Crypto Bot API is online",
        "docs": "/docs",
        "bot_running": bot_running,
        "mode": "LIVE" if config.LIVE_TRADING else "PAPER",
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "bot_running": bot_running,
        "status": last_status,
        "last_heartbeat": last_heartbeat,
        "last_error": last_error,
    }


@app.get("/status")
def status():
    state_snapshot = None

    try:
        with runtime.state_lock:
            state_snapshot = load_state()
    except Exception:
        state_snapshot = None

    return {
        "bot_running": bot_running,
        "status": last_status,
        "mode": "LIVE" if config.LIVE_TRADING else "PAPER",
        "debug_mode": config.DEBUG_MODE,
        "bot_started_at": bot_started_at,
        "last_heartbeat": last_heartbeat,
        "last_error": last_error,
        "scan_interval_seconds": config.SCAN_INTERVAL_SECONDS,
        "max_open_positions": config.MAX_OPEN_POSITIONS,
        "stats": bot_stats,
        "state_loaded": state_snapshot is not None,
        "state": state_snapshot,
    }


@app.post("/start")
def start_bot(payload: StartBotRequest):
    global bot_thread, bot_running

    if bot_running:
        raise HTTPException(status_code=400, detail="Bot is already running")

    if payload.debug is not None:
        config.DEBUG_MODE = payload.debug

    stop_event.clear()

    bot_thread = threading.Thread(target=run_bot_loop, daemon=True)
    bot_thread.start()

    return {
        "message": "Bot started",
        "mode": "LIVE" if config.LIVE_TRADING else "PAPER",
        "debug_mode": config.DEBUG_MODE,
    }


@app.post("/stop")
def stop_bot():
    if not bot_running:
        raise HTTPException(status_code=400, detail="Bot is not running")

    stop_event.set()

    return {
        "message": "Stop signal sent",
    }


@app.post("/restart")
def restart_bot(payload: StartBotRequest):
    global bot_thread

    if bot_running:
        stop_event.set()
        time.sleep(2)

    if payload.debug is not None:
        config.DEBUG_MODE = payload.debug

    stop_event.clear()

    bot_thread = threading.Thread(target=run_bot_loop, daemon=True)
    bot_thread.start()

    return {
        "message": "Bot restarted",
        "mode": "LIVE" if config.LIVE_TRADING else "PAPER",
        "debug_mode": config.DEBUG_MODE,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)