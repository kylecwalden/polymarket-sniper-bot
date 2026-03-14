# Polymarket Sniper Bot

Automated trading bot for Polymarket prediction markets. Two strategies:

1. **Sniper (v1)** — Buys outcomes priced under 3 cents. High volume, low cost, lottery-ticket math.
2. **Bracket Bot (v4)** — Trades daily BTC/ETH price brackets and weather temperature brackets using probability models to find mispriced outcomes.

## What It Does

### Sniper Mode (`python bot.py scan` / `run`)

Scans all active Polymarket events for outcomes priced under your threshold (default $0.03). Places small bets on extreme long shots with asymmetric upside.

**The math:** Buy 100 outcomes at $0.02 each = $1,000 total. 99 lose = -$990. 1 wins = $500 payout. 2 wins = breakeven. 3+ = profit.

### Bracket Bot (`python bot.py bracket`)

Finds daily bracket markets and compares Polymarket prices against probability models:

- **Crypto:** "Will Bitcoin be above $72,000 on March 14?" — Uses Black-Scholes log-normal model with Binance volatility data
- **Weather:** "Will the high temperature in Dallas be 82-83F on March 14?" — Uses NOAA/Open-Meteo forecasts with a normal distribution error model

When the model finds an edge > 5% vs Polymarket pricing, it places a bet. Kelly criterion sizing keeps bets conservative.

**Built-in safety:**
- 50% max drawdown circuit breaker
- 5-loss streak cooldown (30 min pause)
- Win rate floor (halts if below 30% after 10+ trades)
- Daily bankroll cap ($50 default)
- Telegram alerts on every trade, win, loss, and circuit breaker trip

## Prerequisites

- **Python 3.11+**
- **Polymarket account** with USDC deposited (on Polygon network)
- **ProtonVPN** (or any VPN) — Polymarket blocks US IPs for trading
- **macOS or Linux** (untested on Windows)

## Setup (5 minutes)

### Step 1: Clone and install

```bash
git clone <repo-url> polymarket-sniper-bot
cd polymarket-sniper-bot
pip install -r requirements.txt
```

### Step 2: Configure

```bash
cp .env.example .env
```

Open `.env` in any editor and fill in your credentials (see below).

### Step 3: Get your Polymarket private key

1. Go to [polymarket.com](https://polymarket.com) and create an account (deposit at least $20 USDC)
2. Click your profile icon (top right) > **Cash** > **...** (three dots) > **Export Private Key**
3. Paste it as `PRIVATE_KEY` in your `.env`
4. Your wallet address is shown on the same page — paste as `WALLET_ADDRESS`
5. If you logged in with email, set `SIGNATURE_TYPE=1`. If using MetaMask/EOA wallet, set `SIGNATURE_TYPE=0`

### Step 4: Set up Telegram alerts (optional but recommended)

1. Open Telegram, search for **@BotFather**
2. Send `/newbot`, follow the prompts, copy the **bot token**
3. Search for **@userinfobot**, send any message, copy your **chat ID**
4. Paste both into `.env`:
   ```
   TELEGRAM_BOT_TOKEN=your_bot_token
   TELEGRAM_CHAT_ID=your_chat_id
   ```

### Step 5: Get Allium API key (optional)

Allium provides on-chain smart money tracking for crypto markets. The bot works without it.

1. Sign up at [app.allium.so](https://app.allium.so)
2. Go to API Keys, create one
3. Paste as `ALLIUM_API_KEY` in `.env`

### Step 6: Connect VPN and run

```bash
# Connect ProtonVPN to any non-US server first, then:

# Preview mode — see opportunities without buying
python bot.py scan

# Run the sniper bot (buys cheap outcomes on loop)
python bot.py run

# Run the bracket bot (daily crypto + weather brackets)
python bot.py bracket

# Check your positions and P&L
python bot.py positions
```

## Configuration Reference

### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `PRIVATE_KEY` | required | Polymarket wallet private key |
| `WALLET_ADDRESS` | required | Your Polygon wallet address |
| `SIGNATURE_TYPE` | `1` | `1` = email login, `0` = EOA wallet |
| `PROTON_VPN_REQUIRED` | `true` | Require non-US IP before trading |

### Sniper Bot (v1)

| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_PRICE` | `0.005` | Min outcome price to buy (0.5 cents) |
| `MAX_PRICE` | `0.03` | Max outcome price to buy (3 cents) |
| `BET_SIZE_USDC` | `10` | USDC per bet |
| `MAX_DAILY_SPEND` | `100` | Daily spending cap |
| `SCAN_INTERVAL_MINUTES` | `30` | Minutes between scans |

### Bracket Bot (v4)

| Variable | Default | Description |
|----------|---------|-------------|
| `V4_COINS` | `BTC,ETH` | Crypto coins to trade |
| `V4_MIN_EDGE` | `0.05` | Min edge to trade (5%) |
| `V4_SCAN_INTERVAL` | `300` | Seconds between scans |
| `V4_MAX_BET` | `10.0` | Max bet size (USDC) |
| `V4_MIN_BET` | `2.0` | Min bet size (USDC) |
| `V4_DAILY_BANKROLL` | `50.0` | Daily budget (USDC) |
| `V4_KELLY_FRACTION` | `0.10` | Kelly criterion fraction (10%) |
| `V4_MAX_ENTRY_PRICE` | `0.80` | Won't buy above 80 cents |

### Optional Services

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | empty | Telegram bot token for alerts |
| `TELEGRAM_CHAT_ID` | empty | Your Telegram chat ID |
| `ALLIUM_API_KEY` | empty | Allium on-chain data API key |

## Project Structure

```
polymarket-sniper-bot/
├── bot.py              # CLI entry point (scan/run/bracket/positions)
├── .env.example        # Config template — copy to .env
├── requirements.txt    # Python dependencies
│
├── # ── Sniper Bot (v1) ──
├── scanner.py          # Gamma API market scanner
├── trader.py           # CLOB order placement + tracking
├── tracker.py          # Position monitoring + P&L
├── analyzer.py         # Performance analysis (auto-runs at milestones)
│
├── # ── Bracket Bot (v4) ──
├── arb_engine_v4.py    # Main v4 engine (scoring + trading loop)
├── bracket_markets.py  # Discovers crypto + weather bracket events
├── bracket_model.py    # Probability models (Black-Scholes + NOAA)
├── noaa_feed.py        # Weather forecasts (NOAA + Open-Meteo)
│
├── # ── Shared Infrastructure ──
├── arb_engine.py       # v3.5 latency arb engine (legacy)
├── crypto_markets.py   # 15-min crypto market discovery
├── binance_feed.py     # Real-time BTC/ETH/SOL prices (WebSocket)
├── allium_feed.py      # On-chain smart money signals
├── vpn.py              # VPN connection verification
├── telegram_alerts.py  # Trade alerts via Telegram
│
└── data/               # Auto-created: orders, trades, logs
```

## How the Bracket Bot Works

1. **Discover** — Fetches all active bracket events from Polymarket's Gamma API (crypto daily brackets + weather temperature brackets for 20+ cities)
2. **Model** — Computes fair probabilities using:
   - Crypto: Log-normal model with Binance hourly volatility
   - Weather: Normal distribution around NOAA/Open-Meteo forecast
3. **Score** — Compares model probability vs Polymarket price for each bracket. Edge = model_prob - poly_price - 2% fee
4. **Trade** — When edge > 5%, verifies with real CLOB orderbook, places Fill-or-Kill order
5. **Protect** — Circuit breakers halt trading on drawdown, loss streaks, or low win rate
6. **Resolve** — Next day, checks if bracket hit or missed, updates bankroll

## Support This Project

If this bot makes you money, consider tipping the developer:

**Polygon/Ethereum (ERC-20):** `0x75A895ab14E58Af90e6CD9609EaACdfB5Ef07a36`

## Helpful Links

- [Sign up for Polymarket](https://polymarket.com) — *replace with your referral link from [partners.dub.co/polymarket](https://partners.dub.co/polymarket)*
- [Get ProtonVPN](https://pr.tn/ref/WMF7NFH4) — Free VPN required for trading
- [Allium Data Platform](https://app.allium.so) — On-chain intelligence (optional)

## Disclaimer

This is experimental software for educational purposes. Prediction markets carry risk of total loss. Past performance does not guarantee future results. This is not financial advice. Use at your own risk. You are responsible for compliance with all applicable laws in your jurisdiction.
