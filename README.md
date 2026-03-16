# Polymarket Sniper Bot

Automated trading bot for Polymarket prediction markets. Three strategies, one codebase. Run it locally in 5 minutes, or go full autopilot with cloud deployment and AI-powered remote control.

**Start simple. Add complexity when you're ready.**

| Level | What You Get | Time to Set Up |
|-------|-------------|----------------|
| **Basic** | Bot runs on your laptop, you watch the terminal | 5 minutes |
| **+ Telegram Alerts** | Get trade notifications on your phone | +2 minutes |
| **+ Cloud Deployment** | Bot runs 24/7 even when your laptop is closed | +20 minutes |
| **+ AI Control Bot** | Message your bot from your phone, Claude AI diagnoses issues and makes fixes | +5 minutes |

## The Three Strategies

1. **Weather Bracket Bot** — Trades daily weather temperature brackets using the GFS 31-member ensemble forecast. Counts how many ensemble members land in each bracket to compute probability. Buys when edge > 8%.
2. **Crypto Maker Bot** — Trades 15-min BTC/ETH/SOL up/down markets. Posts GTC limit orders at $0.88-0.95 on the likely winning side ~10 seconds before window close. Zero taker fees + maker rebates.
3. **Sniper** — Buys outcomes priced under 3 cents. High volume, low cost, lottery-ticket math.

---

# Level 1: Run Locally (5 minutes)

Everything you need to get trading. No cloud, no Telegram, no API keys beyond Polymarket.

### Step 1: Clone and install

```bash
git clone https://github.com/kylecwalden/polymarket-sniper-bot.git
cd polymarket-sniper-bot
pip install -r requirements.txt
cp .env.example .env
```

### Step 2: Get your Polymarket credentials

1. Go to [polymarket.com](https://polymarket.com) and create an account (deposit at least $20 USDC)
2. Click your profile icon (top right) > **Cash** > **...** (three dots) > **Export Private Key**
3. Open `.env` in any editor and fill in:
   ```
   PRIVATE_KEY=your_private_key_here
   WALLET_ADDRESS=your_wallet_address_here
   SIGNATURE_TYPE=1    # 1 = email login, 0 = MetaMask/EOA wallet
   ```

### Step 3: Connect VPN and run

Polymarket blocks US IPs. Connect [ProtonVPN](https://pr.tn/ref/WMF7NFH4) (or any VPN) to a non-US server, then:

```bash
# Pick your strategy:
python bot.py bracket     # Weather bot (recommended to start)
python bot.py maker       # Crypto maker (15-min BTC/ETH/SOL)
python bot.py dual        # Both in parallel

# Utilities:
python bot.py scan        # Preview cheap outcomes without buying
python bot.py positions   # Check your P&L
```

**That's it.** The bot scans Polymarket, finds edges, and places trades. You'll see everything in your terminal. Keep it running as long as you want — close it anytime with `Ctrl+C`.

---

# Level 2: Add Telegram Alerts (+2 minutes)

Get notifications on your phone for every trade, win, loss, and status update. No more watching the terminal.

1. Open Telegram, search for **@BotFather**, send `/newbot`, follow the prompts, copy the **bot token**
2. Search for **@userinfobot**, send any message, copy your **chat ID**
3. Add to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=your_bot_token
   TELEGRAM_CHAT_ID=your_chat_id
   ```
4. Restart the bot. You'll now get messages like:

   > 🎯 **NEW TRADE PLACED**
   > *Dallas High Temp Above 75°F*
   > Betting: YES @ $0.42/share
   > Cost: $5.00 (11 shares)
   > Edge: 12.3% over market
   > If we win: +$6.38 profit

---

# Level 3: Deploy to the Cloud (+20 minutes)

Run the bot 24/7 on a cloud server so it keeps trading when your laptop is closed, sleeping, or off.

**Recommended:** [AWS Free Tier](https://aws.amazon.com/free/) — 12 months free on a t3.micro instance. Pick a European region (like Ireland) and you get a non-US IP automatically, so you don't even need a VPN.

The setup is the same as Level 1 — just do it on a cloud server instead of your laptop:

1. Spin up a Linux instance (Ubuntu 22.04) on AWS, DigitalOcean, Linode, etc.
2. SSH in, clone the repo, install dependencies, create `.env`
3. Run with `nohup python bot.py dual &` or set up a systemd service for auto-restart on crashes

**Pro tip:** If you pick an EU region, set `PROTON_VPN_REQUIRED=false` in your `.env` — the server's IP is already non-US.

---

# Level 4: AI-Powered Remote Control (+5 minutes)

This is the fun one. Add a Claude AI brain to your Telegram bot so you can control everything from your phone using natural language.

**What it does:** You message your Telegram bot, Claude reads your code and logs, diagnoses issues, edits files, restarts services — all from your phone. It's like having a DevOps engineer on call 24/7.

**Requires:** An [Anthropic API key](https://console.anthropic.com) (~$0.01-0.05 per message)

1. Sign up at [console.anthropic.com](https://console.anthropic.com) and create an API key
2. Add to `.env`:
   ```
   ANTHROPIC_API_KEY=your_anthropic_api_key
   ```
3. Run the control bot (in addition to your trading bot):
   ```bash
   python telegram_control.py
   ```
4. Message your Telegram bot:

   | Message | What Happens |
   |---------|-------------|
   | `/status` | Bot health, bankroll, P&L at a glance |
   | `/logs` | Recent activity from both bots |
   | `/restart` | Restart the trading bots |
   | `/pause` / `/resume` | Pause or resume trading |
   | *"Why aren't any trades going through?"* | Claude reads logs and code, tells you what's wrong |
   | *"Lower the edge threshold to 5%"* | Claude edits the config and restarts the bot |
   | *"How much have we made today?"* | Claude checks P&L and gives you a summary |

---

## How the Strategies Work

### Weather Bracket Bot (`python bot.py bracket`)

Trades daily temperature bracket markets across 20+ global cities (Dallas, Seoul, Tokyo, London, etc.)

**The edge:** We use the GFS 31-member ensemble from Open-Meteo's free API. Each ensemble member runs a slightly different simulation of the atmosphere. If 28/31 members predict a high above 70°F, that's a 90.3% probability — far more accurate than a single-forecast guess. When Polymarket prices a bracket at 50% but our ensemble says 90%, we buy.

**Flow:**
1. Fetches GFS 31-member ensemble from Open-Meteo (`ensemble-api.open-meteo.com`)
2. Counts how many members land in each temperature bracket
3. Compares ensemble probability vs Polymarket price
4. When edge > 8%, places GTC limit order at the real orderbook best ask (auto-cancels after 20s if unfilled)
5. Falls back to NOAA/Open-Meteo single forecast + normal distribution if ensemble unavailable
6. Skips single-degree brackets (too noisy for ensemble resolution)
7. Skips cities past 4 PM local time (observation window closed)

**Why this works:** The [top weather bots on Polymarket ($24K+ profit)](https://blog.devgenius.io/found-the-weather-trading-bots-quietly-making-24-000-on-polymarket-and-built-one-myself-for-free-120bd34d6f09) all use GFS ensemble counting. Single-forecast models can't compete.

### Crypto Maker Bot (`python bot.py maker`)

Trades 15-minute BTC/ETH/SOL "Up or Down" markets using a maker (limit order) strategy.

**Why taker arbitrage is dead:** In Feb 2026, Polymarket introduced [dynamic taker fees up to 3.15%](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/) and removed the 500ms taker delay. The old strategy of FOK-ing the spread no longer works.

**The new strategy:**
1. Connects to Binance WebSocket for real-time BTC/ETH/SOL prices
2. Tracks price from the start of each 15-min window
3. At T-10 seconds before window close: checks if price has moved >0.1% in one direction
4. If direction is clear → posts GTC maker bid at $0.88-0.95 on the likely winning side
5. If ambiguous (< 0.1% move) → skips (don't bet on coin flips)
6. After window close: if order didn't fill, cancel it. If filled, collect $1.00/share on win.

**Why this works:** ~85% of 15-min price direction is determined by T-10 seconds. Polymarket odds update slowly near close. Maker orders = zero fees + rebates.

### Sniper Mode (`python bot.py scan` / `run`)

Scans all active Polymarket events for outcomes priced under 3 cents. Places small bets on extreme long shots with asymmetric upside.

**The math:** Buy 100 outcomes at $0.02 each = $200 total. If 1 wins = $500 payout.

---

## Configuration Reference

All settings are in `.env`. Defaults work out of the box — only `PRIVATE_KEY` and `WALLET_ADDRESS` are required.

### Core (Required)

| Variable | Default | Description |
|----------|---------|-------------|
| `PRIVATE_KEY` | required | Polymarket wallet private key |
| `WALLET_ADDRESS` | required | Your Polygon wallet address |
| `SIGNATURE_TYPE` | `1` | `1` = email login, `0` = EOA wallet |
| `PROTON_VPN_REQUIRED` | `true` | Require non-US IP before trading |

### Weather Bot

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

### Crypto Maker Bot

| Variable | Default | Description |
|----------|---------|-------------|
| `MAKER_COINS` | `BTC,ETH,SOL` | Coins to trade (comma-separated) |
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

### Sniper Bot

| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_PRICE` | `0.005` | Min outcome price to buy (0.5 cents) |
| `MAX_PRICE` | `0.03` | Max outcome price to buy (3 cents) |
| `BET_SIZE_USDC` | `10` | USDC per bet |
| `MAX_DAILY_SPEND` | `100` | Daily spending cap |
| `SCAN_INTERVAL_MINUTES` | `30` | Minutes between scans |

### Optional Services

| Variable | Level | Description |
|----------|-------|-------------|
| `TELEGRAM_BOT_TOKEN` | Level 2 | Telegram bot token for alerts |
| `TELEGRAM_CHAT_ID` | Level 2 | Your Telegram chat ID |
| `ANTHROPIC_API_KEY` | Level 4 | Anthropic API key (for AI control bot) |
| `ALLIUM_API_KEY` | Optional | Allium on-chain data API key |

## All Commands

| Command | Description |
|---------|-------------|
| `python bot.py bracket` | Weather bracket bot (GFS ensemble) |
| `python bot.py maker` | Crypto maker bot (15-min BTC/ETH/SOL) |
| `python bot.py dual` | Run weather + crypto maker in parallel |
| `python bot.py scan` | Preview cheap outcomes (no buying) |
| `python bot.py run` | Legacy v1 sniper bot |
| `python bot.py positions` | Show positions and P&L |
| `python telegram_control.py` | AI-powered Telegram control bot (Level 4) |

## Project Structure

```
polymarket-sniper-bot/
├── bot.py                  # CLI entry point (bracket/maker/dual/scan/positions)
├── .env.example            # Config template — copy to .env
├── requirements.txt        # Python dependencies
│
├── # ── Weather Bracket Bot ──
├── arb_engine_v4.py        # Weather scoring + trading loop (GFS ensemble)
├── bracket_markets.py      # Discovers weather bracket events from Gamma API
├── bracket_model.py        # Probability models (ensemble counting + normal dist)
├── noaa_feed.py            # Weather data (GFS ensemble + NOAA + Open-Meteo)
│
├── # ── Crypto Maker Bot ──
├── arb_engine_v5_maker.py  # 15-min crypto maker strategy
├── crypto_markets.py       # 15-min up/down market discovery
├── binance_feed.py         # Real-time BTC/ETH/SOL prices (WebSocket)
│
├── # ── Remote Control (Level 4) ──
├── telegram_control.py     # Claude AI Telegram bot (control from phone)
├── telegram_alerts.py      # Trade alerts via Telegram (Level 2)
│
├── # ── Shared Infrastructure ──
├── trader.py               # CLOB order placement + tracking
├── tracker.py              # Position monitoring + P&L
├── scanner.py              # Gamma API market scanner
├── vpn.py                  # VPN connection verification
├── allium_feed.py          # On-chain smart money signals
│
└── data/                   # Auto-created: orders, trades, logs
```

## Safety Features

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
- [Get ProtonVPN](https://pr.tn/ref/WMF7NFH4) — Free VPN required for trading from the US
- [Open-Meteo Ensemble API](https://open-meteo.com/en/docs/ensemble-api) — Free GFS ensemble data
- [Allium Data Platform](https://app.allium.so) — On-chain intelligence (optional)
- [Anthropic Console](https://console.anthropic.com) — API key for the AI control bot (Level 4)
- [AWS Free Tier](https://aws.amazon.com/free/) — Run 24/7 in the cloud for free (Level 3)

## Disclaimer

This is experimental software for educational purposes. Prediction markets carry risk of total loss. Past performance does not guarantee future results. This is not financial advice. Use at your own risk. You are responsible for compliance with all applicable laws in your jurisdiction.
