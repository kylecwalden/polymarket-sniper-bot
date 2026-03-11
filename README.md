# Polymarket Sniper Bot

Scans Polymarket for prediction outcomes priced under 3 cents and places small bets with asymmetric upside. High volume, low cost, lottery-ticket math.

## How It Works

1. **Scans** all active Polymarket events via Gamma API
2. **Filters** for outcomes priced under your threshold (default: $0.03)
3. **Places** GTC limit buy orders via the CLOB API
4. **Tracks** positions and P&L over time
5. **Repeats** on a configurable interval

## Setup

### 1. Install dependencies

```bash
cd polymarket-sniper-bot
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and add your credentials:

- **PRIVATE_KEY**: Export from polymarket.com > Cash > ⋯ > Export Private Key
- **WALLET_ADDRESS**: Your Polygon wallet address
- **SIGNATURE_TYPE**: `1` for Polymarket email login, `0` for EOA wallet

### 3. Token Allowances (EOA wallets only)

If using a standard wallet (not Polymarket email login), you need to approve USDC spending. The bot will prompt you if this is needed.

### 4. Connect ProtonVPN

Connect ProtonVPN to a **non-US server** before running. The bot checks your IP geolocation before each scan cycle.

### 5. Run

```bash
# Preview mode — see cheap outcomes without buying
python bot.py scan

# Live mode — scan and place orders on loop
python bot.py run

# Check positions and P&L
python bot.py positions
```

## Configuration (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| MAX_PRICE | 0.03 | Max price to buy (0.03 = 3 cents) |
| BET_SIZE_USDC | 10 | USDC per bet |
| MAX_DAILY_SPEND | 100 | Daily spending cap |
| SCAN_INTERVAL_MINUTES | 30 | Minutes between scans |
| PROTON_VPN_REQUIRED | true | Require VPN to trade |

## The Math

- Buy 100 outcomes at $0.02 each, spending $10 per bet = $1,000 total
- 99 resolve to $0 → lose $990
- 1 resolves YES → 500 shares × $1.00 = $500
- 2 hit? You break even. 3+? You profit.

## Files

```
polymarket-sniper-bot/
├── bot.py          # Main entry point + CLI
├── scanner.py      # Gamma API market scanner
├── trader.py       # CLOB order placement
├── tracker.py      # Position + P&L tracking
├── vpn.py          # ProtonVPN connection gate
├── .env.example    # Config template
└── data/           # Order history (auto-created)
    └── orders.json
```

## Disclaimer

This is experimental software. Prediction markets carry risk of total loss. This is not financial advice. Use at your own risk.
