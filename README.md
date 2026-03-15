# Polymarket Sniper Bot

Automated trading bot for Polymarket prediction markets. Three strategies, one codebase:

1. **Weather Bracket Bot (v5)** — Trades daily weather temperature brackets using the GFS 31-member ensemble forecast. Counts how many ensemble members land in each bracket to compute probability. 8% edge threshold. FOK orders with real orderbook pricing.
2. **Crypto Maker Bot (v5)** — Trades 15-min BTC/ETH up/down markets using a maker strategy. Posts GTC limit orders at $0.88-0.95 on the likely winning side ~10 seconds before window close. Zero taker fees + maker rebates.
3. **Sniper (v1)** — Buys outcomes priced under 3 cents. High volume, low cost, lottery-ticket math.

## Quick Start

```bash
git clone https://github.com/kylecwalden/polymarket-sniper-bot.git
cd polymarket-sniper-bot
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials (see Setup below)

# Connect VPN to non-US server, then:
python bot.py bracket     # Weather bot (GFS ensemble)
python bot.py maker       # Crypto maker (15-min markets)
python bot.py dual        # Both in parallel
python bot.py scan        # Preview cheap outcomes
python bot.py positions   # Check P&L
```

## How It Works

### Weather Bracket Bot (`python bot.py bracket`)

Trades daily temperature bracket markets across 20+ global cities (Dallas, Seoul, Tokyo, London, etc.)

**The edge:** We use the GFS 31-member ensemble from Open-Meteo's free API. Each ensemble member runs a slightly different simulation of the atmosphere. If 28/31 members predict a high above 70°F, that's a 90.3% probability — far more accurate than a single-forecast guess. When Polymarket prices a bracket at 50% but our ensemble says 90%, we buy.

**Flow:**
1. Fetches GFS 31-member ensemble from Open-Meteo (`ensemble-api.open-meteo.com`)
2. Counts how many members land in each temperature bracket
3. Compares ensemble probability vs Polymarket price
4. When edge > 8%, places FOK order at the real orderbook best ask
5. Falls back to NOAA/Open-Meteo single forecast + normal distribution if ensemble unavailable
6. Skips single-degree brackets (too noisy for ensemble resolution)
7. Skips cities past 4 PM local time (observation window closed)

**Why this works:** The [top weather bots on Polymarket ($24K+ profit)](https://blog.devgenius.io/found-the-weather-trading-bots-quietly-making-24-000-on-polymarket-and-built-one-myself-for-free-120bd34d6f09) all use GFS ensemble counting. Single-forecast models can't compete.

### Crypto Maker Bot (`python bot.py maker`)

Trades 15-minute BTC/ETH "Up or Down" markets using a maker (limit order) strategy.

**Why taker arbitrage is dead:** In Feb 2026, Polymarket introduced [dynamic taker fees up to 3.15%](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/) and removed the 500ms taker delay. The old strategy of FOK-ing the spread no longer works.

**The new strategy:**
1. Connects to Binance WebSocket for real-time BTC/ETH prices
2. Tracks price from the start of each 15-min window
3. At T-10 seconds before window close: checks if price has moved >0.1% in one direction
4. If direction is clear → posts GTC maker bid at $0.88-0.95 on the likely winning side
5. If ambiguous (< 0.1% move) → skips (don't bet on coin flips)
6. After window close: if order didn't fill, cancel it. If filled, collect $1.00/share on win.

**Why this works:** ~85% of 15-min price direction is determined by T-10 seconds. Polymarket odds update slowly near close. Maker orders = zero fees + rebates.

### Sniper Mode (`python bot.py scan` / `run`)

Scans all active Polymarket events for outcomes priced under 3 cents. Places small bets on extreme long shots with asymmetric upside.

**The math:** Buy 100 outcomes at $0.02 each = $200 total. If 1 wins = $500 payout.

## Prerequisites

- **Python 3.11+**
- **Polymarket account** with USDC deposited (on Polygon network)
- **ProtonVPN** (or any VPN) — Polymarket blocks US IPs for trading
- **macOS or Linux** (untested on Windows)

## Setup (5 minutes)

### Step 1: Clone and install

```bash
git clone https://github.com/kylecwalden/polymarket-sniper-bot.git
cd polymarket-sniper-bot
pip install -r requirements.txt
```

### Step 2: Configure

```bash
cp .env.example .env
```

Open `.env` in any editor and fill in your credentials.

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

### Step 5: Connect VPN and run

```bash
# Connect ProtonVPN to any non-US server first, then:

# Weather bot only (GFS ensemble — recommended to start)
python bot.py bracket

# Crypto maker only (15-min BTC/ETH)
python bot.py maker

# Both strategies in parallel
python bot.py dual

# Preview mode — see cheap outcomes without buying
python bot.py scan

# Check your positions and P&L
python bot.py positions
```

## Commands

| Command | Description |
|---------|-------------|
| `python bot.py bracket` | Weather bracket bot (GFS ensemble, FOK orders) |
| `python bot.py maker` | Crypto maker bot (15-min BTC/ETH, GTC orders) |
| `python bot.py dual` | Run weather + crypto maker in parallel |
| `python bot.py scan` | Preview cheap outcomes (no buying) |
| `python bot.py run` | Legacy v1 sniper bot |
| `python bot.py positions` | Show positions and P&L |

## Configuration Reference

### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `PRIVATE_KEY` | required | Polymarket wallet private key |
| `WALLET_ADDRESS` | required | Your Polygon wallet address |
| `SIGNATURE_TYPE` | `1` | `1` = email login, `0` = EOA wallet |
| `PROTON_VPN_REQUIRED` | `true` | Require non-US IP before trading |

### Weather Bot (v5)

| Variable | Default | Description |
|----------|---------|-------------|
| `V4_MIN_EDGE_WEATHER` | `0.08` | Min edge to trade (8%) |
| `V4_SCAN_INTERVAL` | `300` | Seconds between scans |
| `V4_MAX_BET` | `10.0` | Max bet size (USDC) |
| `V4_MIN_BET` | `2.0` | Min bet size (USDC) |
| `V4_DAILY_BANKROLL` | `50.0` | Daily budget (USDC) |
| `V4_KELLY_FRACTION` | `0.10` | Kelly criterion fraction (10%) |
| `V4_MAX_ENTRY_PRICE` | `0.80` | Won't buy above 80 cents |
| `V4_MIN_WIN_PROB` | `0.65` | Only bet when model says 65%+ win chance |
| `V4_MAX_BUY_PRICE` | `0.50` | Max share price (cheap = better upside) |
| `V4_MAX_WEATHER_PER_CYCLE` | `6` | Max weather bets per scan cycle |

### Crypto Maker Bot (v5)

| Variable | Default | Description |
|----------|---------|-------------|
| `MAKER_COINS` | `BTC,ETH` | Coins to trade |
| `MAKER_BET_SIZE` | `5.0` | Default bet per trade (USDC) |
| `MAKER_MAX_BET` | `10.0` | Max bet per trade (USDC) |
| `MAKER_DAILY_BANKROLL` | `50.0` | Daily budget (USDC) |
| `MAKER_DAILY_LOSS_LIMIT` | `25.0` | Stop trading after $25 in losses |
| `MAKER_MIN_MOVE_PCT` | `0.10` | Min price move to bet (0.1%) |
| `MAKER_BID_PRICE_LOW` | `0.88` | Bid price for low-confidence trades |
| `MAKER_BID_PRICE_HIGH` | `0.95` | Bid price for high-confidence trades |
| `MAKER_ENTRY_SECONDS` | `10` | Enter at T-10 seconds before close |
| `MAKER_LOSS_STREAK_LIMIT` | `3` | Pause after 3 consecutive losses |
| `MAKER_LOSS_COOLDOWN` | `3600` | Cooldown after loss streak (seconds) |

### Sniper Bot (v1)

| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_PRICE` | `0.005` | Min outcome price to buy (0.5 cents) |
| `MAX_PRICE` | `0.03` | Max outcome price to buy (3 cents) |
| `BET_SIZE_USDC` | `10` | USDC per bet |
| `MAX_DAILY_SPEND` | `100` | Daily spending cap |
| `SCAN_INTERVAL_MINUTES` | `30` | Minutes between scans |

### Optional Services

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | empty | Telegram bot token for alerts |
| `TELEGRAM_CHAT_ID` | empty | Your Telegram chat ID |
| `ALLIUM_API_KEY` | empty | Allium on-chain data API key |

## Project Structure

```
polymarket-sniper-bot/
├── bot.py                  # CLI entry point (bracket/maker/dual/scan/positions)
├── .env.example            # Config template — copy to .env
├── requirements.txt        # Python dependencies
│
├── # ── Weather Bracket Bot (v5) ──
├── arb_engine_v4.py        # Weather scoring + trading loop (GFS ensemble)
├── bracket_markets.py      # Discovers weather bracket events from Gamma API
├── bracket_model.py        # Probability models (ensemble counting + normal dist)
├── noaa_feed.py            # Weather data (GFS ensemble + NOAA + Open-Meteo)
│
├── # ── Crypto Maker Bot (v5) ──
├── arb_engine_v5_maker.py  # 15-min crypto maker strategy
├── crypto_markets.py       # 15-min up/down market discovery
├── binance_feed.py         # Real-time BTC/ETH/SOL prices (WebSocket)
│
├── # ── Shared Infrastructure ──
├── trader.py               # CLOB order placement + tracking
├── tracker.py              # Position monitoring + P&L
├── scanner.py              # Gamma API market scanner (v1)
├── vpn.py                  # VPN connection verification
├── telegram_alerts.py      # Trade alerts via Telegram
├── allium_feed.py          # On-chain smart money signals
│
├── # ── Legacy ──
├── arb_engine.py           # v3.5 latency arb engine (deprecated)
├── analyzer.py             # Performance analysis
│
└── data/                   # Auto-created: orders, trades, logs
```

## Safety Features

Both bots have multiple layers of protection:

| Guard | Weather Bot | Crypto Maker |
|-------|------------|--------------|
| Daily bankroll cap | $50 | $50 |
| Daily loss limit | — | $25 |
| Max drawdown | 35% (circuit breaker) | — |
| Loss streak pause | 5 losses → 30 min | 3 losses → 60 min |
| Win rate floor | Halts if <30% after 10 trades | — |
| Model sanity check | Skip if model vs market >40% apart | Skip if <0.1% price move |
| Telegram alerts | Every trade/win/loss/halt | Every trade/win/loss/halt |

## Research & References

This bot was built using research from the most profitable weather and crypto bots on Polymarket:

- [GFS Ensemble Weather Bot ($1,325 profit)](https://github.com/suislanchez/polymarket-kalshi-weather-bot)
- [$24K weather bot teardown](https://blog.devgenius.io/found-the-weather-trading-bots-quietly-making-24-000-on-polymarket-and-built-one-myself-for-free-120bd34d6f09)
- [Degen Doppler — 13-model weather edge finder](https://degendoppler.com/)
- [Polymarket dynamic fees killed latency arb](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/)
- [Polymarket CLOB docs](https://docs.polymarket.com/developers/CLOB/orders/create-order)
- [Open-Meteo GFS Ensemble API](https://open-meteo.com/en/docs/ensemble-api)

## Support This Project

If this bot makes you money, consider tipping the developer:

**Polygon/Ethereum (ERC-20):** `0x75A895ab14E58Af90e6CD9609EaACdfB5Ef07a36`

## Helpful Links

- [Sign up for Polymarket](https://polymarket.com)
- [Get ProtonVPN](https://pr.tn/ref/WMF7NFH4) — Free VPN required for trading
- [Open-Meteo Ensemble API](https://open-meteo.com/en/docs/ensemble-api) — Free GFS ensemble data
- [Allium Data Platform](https://app.allium.so) — On-chain intelligence (optional)

## Disclaimer

This is experimental software for educational purposes. Prediction markets carry risk of total loss. Past performance does not guarantee future results. This is not financial advice. Use at your own risk. You are responsible for compliance with all applicable laws in your jurisdiction.
