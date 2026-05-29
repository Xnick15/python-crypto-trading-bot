# Crypto Bot Refactor

This is a cleaned multi-file version of my Coinbase trading bot. The goal of this refactor is to improve readability and make the project easier to maintain without changing the core trading behavior.

## Files

- `main.py` - main loop
- `config.py` - settings and constants
- `models.py` - position dataclass
- `runtime.py` - shared runtime globals
- `utils.py` - logging, Discord, file helpers, lock handling
- `state_manager.py` - state persistence and adaptive settings
- `market_data.py` - live price, candles, scanner
- `indicators.py` - EMA, SMA, RSI, pct change
- `strategy.py` - scoring, filters, symbol selection
- `execution.py` - trading, position management, equity stats
- `websocket_manager.py` - Coinbase websocket handlers
- `reporting.py` - console status and Discord summary

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Copy `.env.example` values into your environment or set them in your system.

3. Review `config.py` before running.

4. Start the bot:
   ```bash
   python main.py
   ```

## Important

- Your old hard-coded Discord webhook was removed. Set `DISCORD_WEBHOOK_URL` through an environment variable.
- This refactor is meant to preserve behavior as closely as possible, but you should still test in paper mode first.
