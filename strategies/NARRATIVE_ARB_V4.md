# Strategy: Narrative Arbitrage (v4 Candidate)

**Source:** https://x.com/0xTengen_/status/2031017960664633344
**Saved:** 2026-03-09
**Status:** BACKLOG — deploy after v3.3 latency arb proves profitable

---

## Core Concept

Polymarket prices **attention**, not just probability. When two markets are logically linked (cointegrated) but one gets all the hype and the other sits quiet, a spread opens up. That spread is free money.

Retail doomscrolls, catches a vibe, and slams the "main character" market. The structurally linked market lags in the shadows, starved of liquidity.

**You buy the overlooked side. Wait for the crowd to connect the dots.**

---

## Example Trade

| Market | Crowd Behavior | Smart Play |
|--------|---------------|------------|
| "Will BTC drop below $65k?" | Retail panic-buys YES on red candles | Overpriced — skip |
| "Will MicroStrategy buy BTC this month?" | Nobody watching, underpriced | Buy YES — Saylor historically buys dips |

The two markets are logically linked: BTC bleeds → Saylor buys → MSTR market should reprice. But retail attention hasn't connected the dots yet.

---

## Why It Works

- Traditional markets price cash flows; prediction markets price attention
- A divergence between cointegrated markets is an **attention gap**, not a math error
- Human psychology guarantees the spread will close — it's just a matter of when
- The math tells you the spread exists; crowd behavior tells you when it'll snap back

---

## What We'd Need to Build

### 1. Market Pair Scanner
- NLP/semantic analysis to identify logically linked markets
- Historical price correlation tracking between market pairs
- Alert when spread between cointegrated pairs exceeds N standard deviations

### 2. Attention Flow Tracker
- Monitor volume/liquidity imbalance between paired markets
- Detect when one side gets "main character" attention (volume spike) while the other lags
- Allium smart money data: are whales already loading the quiet side?

### 3. Entry/Exit Logic
- Entry: spread exceeds threshold + smart money confirms the quiet side
- Position size: based on spread magnitude and historical reversion speed
- Exit: spread closes to within 1 std dev, or time-based stop (market expiry approaching)

### 4. Risk Controls
- Max exposure per pair
- Correlation breakdown detection (pairs can decouple)
- Liquidity check — illiquid markets mean slippage on exit

---

## How It Differs From Our Latency Arb (v3.3)

| | Latency Arb (Current) | Narrative Arb (Future) |
|---|---|---|
| Edge source | Speed — Binance price leads Polymarket | Attention gap between linked markets |
| Timeframe | 15 minutes | Days to weeks |
| Markets | Crypto up/down only | Any correlated market pairs |
| Automation | Fully automated | Needs market pairing + NLP |
| Bet frequency | 6-8 per session | Fewer, larger, longer holds |
| Capital needed | $3-10 per trade | $25-100+ per position |
| Complexity | Moderate | High — requires understanding market relationships |

---

## Existing Infrastructure We Can Reuse

- **py-clob-client**: Same order placement code
- **Allium MCP**: Smart money tracking on the "quiet" side of pairs
- **Bankroll management**: Kelly criterion adapts to any strategy
- **Wallet + VPN setup**: Already configured

---

## Prerequisites Before Building

1. v3.3 latency arb running profitably for 1+ week (50+ trades)
2. Prove we can sustain 80%+ win rate with Kelly sizing
3. Bankroll grown enough to support longer-hold positions
4. Research: catalog 20+ cointegrated market pairs manually to validate the concept

---

## Related Research

- @0xTengen_ thread on narrative arb (source above)
- @0xPhantomDefi: whales doing 100+ latency trades/day (validates current strategy first)
- LMSR pricing math: softmax probabilities apply to both strategies
- RN1 wallet ($5.3M): sports value betting — different strategy but same "mispricing" principle
