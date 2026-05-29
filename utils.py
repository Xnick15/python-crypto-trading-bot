
import csv
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import requests

import config
import runtime


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_str() -> str:
    return now_utc().strftime("%Y-%m-%d")


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log_event(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(config.EVENT_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def send_discord_message(message: str) -> None:
    if not config.DISCORD_ENABLED or not config.DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(
            config.DISCORD_WEBHOOK_URL,
            json={"content": message},
            timeout=5,
        )
    except Exception as e:
        if config.DEBUG_MODE:
            log_event(f"Discord error: {e}")


def acquire_single_instance_lock() -> None:
    try:
        runtime.lock_handle = open(config.LOCK_FILE, "w", encoding="utf-8")
        runtime.lock_handle.write(str(os.getpid()))
        runtime.lock_handle.flush()

        import msvcrt
        runtime.lock_handle.seek(0)
        msvcrt.locking(runtime.lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        if runtime.lock_handle:
            try:
                runtime.lock_handle.close()
            except Exception:
                pass
            runtime.lock_handle = None
        raise RuntimeError("Another bot instance is already running. Close the other bot first.")


def release_single_instance_lock() -> None:
    if runtime.lock_handle is None:
        return
    try:
        try:
            import msvcrt
            runtime.lock_handle.seek(0)
            msvcrt.locking(runtime.lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        runtime.lock_handle.close()
    except Exception:
        pass
    finally:
        runtime.lock_handle = None

    try:
        if os.path.exists(config.LOCK_FILE):
            os.remove(config.LOCK_FILE)
    except Exception:
        pass


def load_json(path: str, default: Any) -> Any:
    last_error: Optional[Exception] = None

    if not os.path.exists(path):
        return default

    for _ in range(10):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()

            if not content:
                time.sleep(0.05)
                continue

            data = json.loads(content)

            if path == config.STATE_FILE and isinstance(data, dict):
                runtime.last_good_state = json.loads(json.dumps(data))

            return data

        except Exception as e:
            last_error = e
            time.sleep(0.05)

    if path == config.STATE_FILE and runtime.last_good_state is not None:
        log_event(f"Warning: using last known good state for {path}")
        return json.loads(json.dumps(runtime.last_good_state))

    if last_error is not None:
        log_event(f"Error loading {path}: {last_error}")

    return default


def save_json(path: str, data: Any) -> None:
    temp_path = f"{path}.tmp"

    for _ in range(10):
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_path, path)
            return

        except PermissionError:
            time.sleep(0.1)

        except Exception:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            raise

    raise PermissionError(f"Could not safely write {path} after multiple retries.")


def ensure_trade_csv() -> None:
    if not os.path.exists(config.TRADE_CSV):
        with open(config.TRADE_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "mode", "symbol", "side", "entry_price", "exit_price",
                "qty", "cost_basis", "sale_value", "pnl_usd", "pnl_pct", "reason",
                "client_order_id", "exchange_order_id"
            ])


def append_trade_csv(
    mode: str,
    symbol: str,
    side: str,
    entry_price: float,
    exit_price: float,
    qty: float,
    cost_basis: float,
    sale_value: float,
    pnl_usd: float,
    pnl_pct: float,
    reason: str,
    client_order_id: Optional[str],
    exchange_order_id: Optional[str],
) -> None:
    with open(config.TRADE_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            mode,
            symbol,
            side,
            f"{entry_price:.8f}",
            f"{exit_price:.8f}",
            f"{qty:.8f}",
            f"{cost_basis:.2f}",
            f"{sale_value:.2f}",
            f"{pnl_usd:.2f}",
            f"{pnl_pct:.4f}",
            reason,
            client_order_id or "",
            exchange_order_id or "",
        ])


def parse_trade_timestamp(value: str) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None
