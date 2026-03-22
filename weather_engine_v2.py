"""
Weather Engine V2 — Three-Strategy Temperature Trading
========================================================
Based on proven strategies from top Polymarket weather bots:

1. LADDER   — Buy 5-7 adjacent brackets at $0.02-0.15 (neobrother style, $77K profit)
2. FORECAST — Buy when market < forecast by 15%+, sell at 45¢ (70-85% win rate)
3. WHALE    — Copy trade 95%+ win rate wallets via Allium on-chain data

Paper trade mode simulates all fills from real market data.
"""

import asyncio
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from typing import Optional

from dotenv import load_dotenv
from rich.console import Console

load_dotenv()
console = Console()

# ══════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════

PAPER_TRADE = os.getenv("PAPER_TRADE", "false").lower() == "true"
W2_BANKROLL = float(os.getenv("W2_BANKROLL", "50.0"))
W2_DAILY_LOSS_LIMIT = float(os.getenv("W2_DAILY_LOSS_LIMIT", "20.0"))
W2_SCAN_INTERVAL = int(os.getenv("W2_SCAN_INTERVAL", "300"))  # 5 min

# Strategy toggles
W2_LADDER_ENABLED = os.getenv("W2_LADDER_ENABLED", "true").lower() == "true"
W2_FORECAST_ENABLED = os.getenv("W2_FORECAST_ENABLED", "true").lower() == "true"
W2_WHALE_ENABLED = os.getenv("W2_WHALE_ENABLED", "true").lower() == "true"

# Ladder config
W2_LADDER_BET_SIZE = float(os.getenv("W2_LADDER_BET_SIZE", "2.0"))     # Per bracket
W2_LADDER_MAX_PRICE = float(os.getenv("W2_LADDER_MAX_PRICE", "0.15"))  # Only buy brackets under 15¢
W2_LADDER_MIN_BRACKETS = int(os.getenv("W2_LADDER_MIN_BRACKETS", "3")) # Min 3 brackets to form ladder
W2_LADDER_MAX_BRACKETS = int(os.getenv("W2_LADDER_MAX_BRACKETS", "7")) # Max 7 brackets per ladder

# Forecast arb config
W2_FORECAST_ENTRY_EDGE = float(os.getenv("W2_FORECAST_ENTRY_EDGE", "0.15"))  # Buy when 15% edge
W2_FORECAST_EXIT_PRICE = float(os.getenv("W2_FORECAST_EXIT_PRICE", "0.45"))  # Sell at 45¢
W2_FORECAST_MAX_PRICE = float(os.getenv("W2_FORECAST_MAX_PRICE", "0.30"))    # Don't buy above 30¢
W2_FORECAST_BET_SIZE = float(os.getenv("W2_FORECAST_BET_SIZE", "3.0"))

# Whale copy config
W2_WHALE_BET_SIZE = float(os.getenv("W2_WHALE_BET_SIZE", "2.0"))
W2_WHALE_MIN_WIN_RATE = float(os.getenv("W2_WHALE_MIN_WIN_RATE", "0.80"))  # Only copy 80%+ wallets
W2_WHALE_MIN_VOLUME = float(os.getenv("W2_WHALE_MIN_VOLUME", "500"))

# ══════════════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════════════

@dataclass
class StrategyPnL:
    name: str
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    trades: int = 0

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return (self.wins / total * 100) if total > 0 else 0

    def summary(self) -> str:
        return f"{self.wins}W/{self.losses}L ({self.win_rate:.0f}%) ${self.total_pnl:+.2f}"


@dataclass
class Bankroll:
    starting: float
    balance: float = 0.0
    daily_losses: float = 0.0
    ladder_pnl: StrategyPnL = field(default_factory=lambda: StrategyPnL("ladder"))
    forecast_pnl: StrategyPnL = field(default_factory=lambda: StrategyPnL("forecast"))
    whale_pnl: StrategyPnL = field(default_factory=lambda: StrategyPnL("whale"))
    open_positions: list = field(default_factory=list)

    def __post_init__(self):
        self.balance = self.starting

    @property
    def total_pnl(self) -> float:
        return self.balance - self.starting

    @property
    def can_trade(self) -> bool:
        return self.daily_losses < W2_DAILY_LOSS_LIMIT

    def status_line(self) -> str:
        return (
            f"Bankroll: ${self.balance:.2f} | P&L: ${self.total_pnl:+.2f} | "
            f"Ladder: {self.ladder_pnl.summary()} | "
            f"Forecast: {self.forecast_pnl.summary()} | "
            f"Whale: {self.whale_pnl.summary()} | "
            f"Open: {len(self.open_positions)}"
        )


@dataclass
class OpenPosition:
    strategy: str  # "ladder", "forecast", "whale"
    city: str
    bracket_question: str
    side: str  # "yes" or "no"
    token_id: str
    buy_price: float
    shares: float
    cost: float
    order_id: str
    placed_at: float
    resolution_date: str
    paper: bool = False
    ladder_id: str = ""  # Groups ladder brackets together


# ══════════════════════════════════════════════════════════════════════
# Strategy 1: Temperature Laddering
# ══════════════════════════════════════════════════════════════════════

def find_ladder_opportunities(events, forecasts):
    """Find temperature ladder opportunities across all events."""
    opportunities = []

    for event in events:
        city = event.coin
        forecast = forecasts.get(city)
        if not forecast:
            continue

        forecast_temp = forecast.high_temp
        unit = forecast.unit

        # Find all YES-side brackets priced under max_price near the forecast
        cheap_brackets = []
        for market in event.markets:
            if not market.is_active or market.yes_price <= 0:
                continue
            if market.yes_price > W2_LADDER_MAX_PRICE:
                continue

            # Is this bracket near the forecast temperature?
            if market.bracket_type == "range" and market.threshold_high:
                bracket_mid = (market.threshold + market.threshold_high) / 2
            elif market.bracket_type == "at_or_below":
                bracket_mid = market.threshold
            elif market.bracket_type == "at_or_above":
                bracket_mid = market.threshold
            else:
                continue

            # Within reasonable range of forecast (±10 degrees)
            distance = abs(bracket_mid - forecast_temp)
            if unit == "°F" and distance <= 10:
                cheap_brackets.append(market)
            elif unit == "°C" and distance <= 6:
                cheap_brackets.append(market)

        # Sort by threshold and check if we have enough for a ladder
        cheap_brackets.sort(key=lambda m: m.threshold)

        if len(cheap_brackets) >= W2_LADDER_MIN_BRACKETS:
            # Take up to max brackets, centered on forecast
            center_idx = 0
            min_dist = float('inf')
            for i, m in enumerate(cheap_brackets):
                mid = m.threshold if not m.threshold_high else (m.threshold + m.threshold_high) / 2
                d = abs(mid - forecast_temp)
                if d < min_dist:
                    min_dist = d
                    center_idx = i

            half = W2_LADDER_MAX_BRACKETS // 2
            start = max(0, center_idx - half)
            end = min(len(cheap_brackets), start + W2_LADDER_MAX_BRACKETS)
            ladder = cheap_brackets[start:end]

            if len(ladder) >= W2_LADDER_MIN_BRACKETS:
                total_cost = sum(m.yes_price * max(5, W2_LADDER_BET_SIZE / m.yes_price) for m in ladder)
                opportunities.append({
                    "city": city,
                    "event": event,
                    "brackets": ladder,
                    "forecast_temp": forecast_temp,
                    "unit": unit,
                    "total_cost": total_cost,
                    "num_brackets": len(ladder),
                })

    # Sort by number of cheap brackets (more coverage = better)
    opportunities.sort(key=lambda x: x["num_brackets"], reverse=True)
    return opportunities


async def execute_ladder(client, opportunity, bankroll: Bankroll, is_paper: bool):
    """Execute a temperature ladder — buy YES on multiple adjacent brackets."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    city = opportunity["city"]
    brackets = opportunity["brackets"]
    forecast = opportunity["forecast_temp"]
    unit = opportunity["unit"]
    ladder_id = str(uuid.uuid4())[:8]
    ptag = "📝 " if is_paper else ""

    # Skip if we already have a ladder on this city+date
    res_date = str(brackets[0].resolution_time.date()) if hasattr(brackets[0], 'resolution_time') else ""
    if _already_has_city_ladder(bankroll, city, res_date):
        console.print(f"  [dim]{city}: Already have ladder for {res_date} — skip[/dim]")
        return False

    filled_count = 0
    total_spent = 0

    for market in brackets:
        price = round(market.yes_price, 2)
        if price <= 0.01 or price > W2_LADDER_MAX_PRICE:
            continue

        size = max(5, math.floor((W2_LADDER_BET_SIZE / price) * 100) / 100)
        cost = round(price * size, 2)

        if cost > bankroll.balance:
            continue

        if is_paper:
            order_id = f"paper_ladder_{ladder_id}_{market.threshold}"
        else:
            try:
                order_args = OrderArgs(
                    token_id=market.yes_token_id,
                    price=price,
                    size=size,
                    side=BUY,
                )
                signed = client.create_order(order_args)
                resp = client.post_order(signed, OrderType.GTC)
                order_id = _extract_order_id(resp)
                if not order_id:
                    continue
            except Exception as e:
                console.print(f"  [red]Ladder order failed: {e}[/red]")
                continue

        bankroll.balance -= cost
        total_spent += cost
        filled_count += 1

        pos = OpenPosition(
            strategy="ladder",
            city=city,
            bracket_question=market.slug[:60] if hasattr(market, 'slug') else f"{city} {market.threshold}{unit}",
            side="yes",
            token_id=market.yes_token_id,
            buy_price=price,
            shares=size,
            cost=cost,
            order_id=order_id,
            placed_at=time.time(),
            resolution_date=str(market.resolution_time.date()) if hasattr(market, 'resolution_time') else "",
            paper=is_paper,
            ladder_id=ladder_id,
        )
        bankroll.open_positions.append(pos)

    if filled_count > 0:
        # Build bracket range string
        thresholds = [f"{m.threshold}" for m in brackets[:filled_count]]
        msg = (
            f"🪜 {ptag}LADDER: {city}\n"
            f"Forecast: {forecast:.0f}{unit}\n"
            f"{filled_count} brackets: {', '.join(thresholds)}{unit}\n"
            f"Total: ${total_spent:.2f}"
        )
        console.print(f"  [bold green]{msg}[/bold green]")
        _send_tg(msg)
        return True
    return False


# ══════════════════════════════════════════════════════════════════════
# Strategy 2: Forecast Arbitrage
# ══════════════════════════════════════════════════════════════════════

def find_forecast_arb_opportunities(events, forecasts, ensemble_probs):
    """Find brackets where market price is significantly below forecast probability."""
    opportunities = []

    for event in events:
        city = event.coin
        probs = ensemble_probs.get(city, {})

        for market in event.markets:
            if not market.is_active or market.yes_price <= 0:
                continue
            if market.yes_price > W2_FORECAST_MAX_PRICE:
                continue

            # Get ensemble probability for this bracket
            key = f"{market.threshold}_{market.bracket_type}"
            model_prob = probs.get(key, 0)

            if model_prob <= 0:
                continue

            # Edge = model probability - market price - 2% fee
            edge = model_prob - market.yes_price - 0.02

            if edge >= W2_FORECAST_ENTRY_EDGE:
                opportunities.append({
                    "city": city,
                    "event": event,
                    "market": market,
                    "model_prob": model_prob,
                    "market_price": market.yes_price,
                    "edge": edge,
                })

    # Sort by edge descending
    opportunities.sort(key=lambda x: x["edge"], reverse=True)
    return opportunities


async def execute_forecast_arb(client, opp, bankroll: Bankroll, is_paper: bool):
    """Buy a bracket where forecast strongly disagrees with market."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    market = opp["market"]
    ptag = "📝 " if is_paper else ""

    # Skip if already have position on this token
    if _already_has_position(bankroll, market.yes_token_id):
        return False

    price = round(market.yes_price, 2)
    size = max(5, math.floor((W2_FORECAST_BET_SIZE / price) * 100) / 100)
    cost = round(price * size, 2)

    if cost > bankroll.balance:
        return False

    if is_paper:
        order_id = f"paper_forecast_{opp['city']}_{int(time.time())}"
    else:
        try:
            order_args = OrderArgs(
                token_id=market.yes_token_id,
                price=price,
                size=size,
                side=BUY,
            )
            signed = client.create_order(order_args)
            resp = client.post_order(signed, OrderType.GTC)
            order_id = _extract_order_id(resp)
            if not order_id:
                return False
        except Exception as e:
            console.print(f"  [red]Forecast arb order failed: {e}[/red]")
            return False

    bankroll.balance -= cost
    pos = OpenPosition(
        strategy="forecast",
        city=opp["city"],
        bracket_question=market.slug[:60] if hasattr(market, 'slug') else f"{opp['city']} {market.threshold}",
        side="yes",
        token_id=market.yes_token_id,
        buy_price=price,
        shares=size,
        cost=cost,
        order_id=order_id,
        placed_at=time.time(),
        resolution_date=str(market.resolution_time.date()) if hasattr(market, 'resolution_time') else "",
        paper=is_paper,
    )
    bankroll.open_positions.append(pos)

    msg = (
        f"🎯 {ptag}FORECAST ARB: {opp['city']}\n"
        f"{market.threshold}{market.threshold_unit} {market.bracket_type}\n"
        f"Model: {opp['model_prob']:.0%} vs Market: ${price:.2f}\n"
        f"Edge: {opp['edge']:.0%} | {size:.0f} shares (${cost:.2f})"
    )
    console.print(f"  [bold cyan]{msg}[/bold cyan]")
    _send_tg(msg)
    return True


# ══════════════════════════════════════════════════════════════════════
# Strategy 3: Whale Copy Trading
# ══════════════════════════════════════════════════════════════════════

def find_whale_trades(events):
    """Find brackets that smart money wallets are buying."""
    try:
        from allium_feed import allium
    except Exception:
        return []

    opportunities = []

    # Get top weather smart wallets
    try:
        rows = allium._run_sql(f"""
            SELECT taker as wallet,
                   question,
                   token_outcome,
                   SUM(usd_collateral_amount) as volume,
                   COUNT(*) as trade_count
            FROM polygon.predictions.trades_enriched
            WHERE LOWER(question) LIKE '%temperature%'
              AND block_timestamp >= DATEADD(hour, -6, CURRENT_TIMESTAMP())
              AND taker IN (
                  SELECT taker FROM polygon.predictions.trades_enriched
                  WHERE LOWER(question) LIKE '%temperature%'
                    AND block_timestamp >= DATEADD(day, -7, CURRENT_TIMESTAMP())
                  GROUP BY taker
                  HAVING COUNT(*) >= 10
                    AND SUM(CASE WHEN is_winning_outcome THEN 1 ELSE 0 END)::FLOAT / COUNT(*) >= {W2_WHALE_MIN_WIN_RATE}
                    AND SUM(usd_collateral_amount) >= {W2_WHALE_MIN_VOLUME}
              )
            GROUP BY taker, question, token_outcome
            HAVING SUM(usd_collateral_amount) >= 10
            ORDER BY volume DESC
            LIMIT 20
        """)
    except Exception as e:
        console.print(f"  [dim]Whale query failed: {e}[/dim]")
        return []

    if not rows:
        return []

    # Match whale trades to our discovered events
    for row in rows:
        question = row.get("question", "")
        outcome = row.get("token_outcome", "").lower()
        volume = float(row.get("volume", 0))

        # Find matching market in our events
        for event in events:
            for market in event.markets:
                if not market.is_active:
                    continue
                # Match by question substring
                market_q = getattr(market, 'question', '') or market.slug
                if question and question[:40].lower() in market_q.lower():
                    side = "yes" if outcome == "yes" else "no"
                    price = market.yes_price if side == "yes" else market.no_price
                    if 0.01 < price < 0.30:  # Only cheap brackets
                        opportunities.append({
                            "city": event.coin,
                            "event": event,
                            "market": market,
                            "side": side,
                            "whale_volume": volume,
                            "price": price,
                        })

    return opportunities


async def execute_whale_copy(client, opp, bankroll: Bankroll, is_paper: bool):
    """Copy a whale's bracket trade."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    market = opp["market"]
    side = opp["side"]
    ptag = "📝 " if is_paper else ""

    token_id = market.yes_token_id if side == "yes" else market.no_token_id
    price = round(opp["price"], 2)
    size = max(5, math.floor((W2_WHALE_BET_SIZE / price) * 100) / 100)
    cost = round(price * size, 2)

    if cost > bankroll.balance:
        return False

    if is_paper:
        order_id = f"paper_whale_{opp['city']}_{int(time.time())}"
    else:
        try:
            order_args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
            signed = client.create_order(order_args)
            resp = client.post_order(signed, OrderType.GTC)
            order_id = _extract_order_id(resp)
            if not order_id:
                return False
        except Exception as e:
            console.print(f"  [red]Whale copy order failed: {e}[/red]")
            return False

    bankroll.balance -= cost
    pos = OpenPosition(
        strategy="whale",
        city=opp["city"],
        bracket_question=market.slug[:60] if hasattr(market, 'slug') else f"{opp['city']} {market.threshold}",
        side=side,
        token_id=token_id,
        buy_price=price,
        shares=size,
        cost=cost,
        order_id=order_id,
        placed_at=time.time(),
        resolution_date=str(market.resolution_time.date()) if hasattr(market, 'resolution_time') else "",
        paper=is_paper,
    )
    bankroll.open_positions.append(pos)

    msg = (
        f"🐋 {ptag}WHALE COPY: {opp['city']}\n"
        f"{side.upper()} @ ${price:.2f} | {size:.0f} shares (${cost:.2f})\n"
        f"Whale volume: ${opp['whale_volume']:.0f}"
    )
    console.print(f"  [bold yellow]{msg}[/bold yellow]")
    _send_tg(msg)
    return True


# ══════════════════════════════════════════════════════════════════════
# Position Resolution
# ══════════════════════════════════════════════════════════════════════

def check_resolutions(bankroll: Bankroll, client, is_paper: bool):
    """Check if any open positions have resolved.

    IMPORTANT: Weather markets resolve at END OF DAY when actual temp is recorded.
    A bracket priced at $0.02 hasn't LOST — it just hasn't resolved yet.
    Only check resolution when:
    - Price jumps to $0.90+ (market resolved YES = WIN)
    - Price drops to $0.01 AND the market's resolution date has PASSED (LOSS)
    - Forecast arb: price rises to exit threshold (early profit-take)
    """
    from tracker import get_current_price
    from datetime import date as date_type

    ptag = "📝 " if is_paper else ""
    today = str(date_type.today())
    resolved = []

    for pos in bankroll.open_positions:
        try:
            price = get_current_price(pos.token_id)
            if price is None:
                continue

            # Check for early exit on forecast arb (sell at 45¢)
            if pos.strategy == "forecast" and price >= W2_FORECAST_EXIT_PRICE:
                payout = round(pos.shares * price, 2)
                profit = payout - pos.cost
                bankroll.balance += payout
                bankroll.forecast_pnl.wins += 1
                bankroll.forecast_pnl.total_pnl += profit
                bankroll.forecast_pnl.trades += 1
                msg = f"🎯 {ptag}FORECAST EXIT: {pos.city} +${profit:.2f} (sold at ${price:.2f})"
                console.print(f"  [bold green]{msg}[/bold green]")
                _send_tg(msg)
                resolved.append(pos)
                continue

            # Win: price near $1 (market resolved in our favor)
            if price >= 0.90:
                payout = round(pos.shares * 1.00, 2)
                profit = payout - pos.cost
                bankroll.balance += payout
                pnl = _get_strategy_pnl(bankroll, pos.strategy)
                pnl.wins += 1
                pnl.total_pnl += profit
                pnl.trades += 1

                emoji = {"ladder": "🪜", "forecast": "🎯", "whale": "🐋"}.get(pos.strategy, "✅")
                msg = f"{emoji} {ptag}WIN: {pos.city} +${profit:.2f}"
                console.print(f"  [bold green]{msg}[/bold green]")
                _send_tg(msg)
                resolved.append(pos)

            # Loss: ONLY if resolution date has PASSED and price is near $0
            elif price <= 0.05 and pos.resolution_date and pos.resolution_date < today:
                bankroll.daily_losses += pos.cost
                pnl = _get_strategy_pnl(bankroll, pos.strategy)
                pnl.losses += 1
                pnl.total_pnl -= pos.cost
                pnl.trades += 1

                emoji = {"ladder": "🪜", "forecast": "🎯", "whale": "🐋"}.get(pos.strategy, "❌")
                msg = f"{emoji} {ptag}LOSS: {pos.city} -${pos.cost:.2f}"
                console.print(f"  [red]{msg}[/red]")
                _send_tg(msg)
                resolved.append(pos)

            # Still pending — don't touch it
            # A bracket at $0.02 that we bought at $0.02 is NOT a loss yet

        except Exception:
            continue

    # Remove resolved positions
    for pos in resolved:
        if pos in bankroll.open_positions:
            bankroll.open_positions.remove(pos)


def _get_strategy_pnl(bankroll: Bankroll, strategy: str) -> StrategyPnL:
    return {"ladder": bankroll.ladder_pnl, "forecast": bankroll.forecast_pnl, "whale": bankroll.whale_pnl}.get(strategy, bankroll.forecast_pnl)


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

def _extract_order_id(response) -> Optional[str]:
    if isinstance(response, dict):
        oid = response.get("orderID", response.get("id", ""))
        if not response.get("success", True):
            return None
        return oid if oid else None
    return str(response) if response else None


def _send_tg(msg: str):
    try:
        import telegram_alerts as tg
        tg.send_message(msg)
    except Exception:
        pass


def _already_has_position(bankroll: Bankroll, token_id: str) -> bool:
    """Check if we already have a position on this token."""
    return any(p.token_id == token_id for p in bankroll.open_positions)


def _already_has_city_ladder(bankroll: Bankroll, city: str, resolution_date: str) -> bool:
    """Check if we already have a ladder on this city+date."""
    return any(
        p.strategy == "ladder" and p.city == city and p.resolution_date == resolution_date
        for p in bankroll.open_positions
    )


# ══════════════════════════════════════════════════════════════════════
# Main Engine
# ══════════════════════════════════════════════════════════════════════

def run_weather_v2():
    """Run the Weather V2 multi-strategy engine."""
    from bracket_markets import discover_weather_events
    from bracket_model import weather_bracket_prob, ensemble_bracket_prob
    from noaa_feed import get_forecast, get_ensemble_forecast
    from trader import init_client
    from vpn import ensure_vpn

    # ── Startup ──
    console.print("=" * 60)
    console.print("  [bold]Weather Engine V2 — Temperature Trading[/bold]")
    console.print("=" * 60)
    console.print(f"  Bankroll:        ${W2_BANKROLL:.0f}")
    console.print(f"  Daily loss cap:  ${W2_DAILY_LOSS_LIMIT:.0f}")
    console.print(f"  Scan interval:   {W2_SCAN_INTERVAL}s")
    console.print(f"  Strategies:")
    if W2_LADDER_ENABLED:
        console.print(f"    🪜 Ladder:   ON  (${W2_LADDER_BET_SIZE}/bracket, max {W2_LADDER_MAX_BRACKETS})")
    if W2_FORECAST_ENABLED:
        console.print(f"    🎯 Forecast: ON  (entry ≥{W2_FORECAST_ENTRY_EDGE:.0%} edge, exit ${W2_FORECAST_EXIT_PRICE})")
    if W2_WHALE_ENABLED:
        console.print(f"    🐋 Whale:    ON  (copy ≥{W2_WHALE_MIN_WIN_RATE:.0%} win rate wallets)")
    if PAPER_TRADE:
        console.print(f"  [bold yellow]📝 MODE: PAPER TRADE (no real orders)[/bold yellow]")

    console.print(" Checking VPN connection...")
    ensure_vpn()

    private_key = os.getenv("PRIVATE_KEY", "")
    sig_type = int(os.getenv("SIGNATURE_TYPE", "1"))
    funder = os.getenv("WALLET_ADDRESS", "")
    client = init_client(private_key, sig_type, funder)
    console.print("[trader] CLOB client initialized and authenticated")

    bankroll = Bankroll(starting=W2_BANKROLL)
    scan_count = 0

    console.print(f"\nWeather V2 started. Scanning every {W2_SCAN_INTERVAL}s...")

    try:
        while True:
            scan_count += 1
            console.print(f"\n── Weather Scan #{scan_count} @ {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC ──")

            if not bankroll.can_trade:
                console.print(f"[red]Daily loss limit hit (${bankroll.daily_losses:.2f}/${W2_DAILY_LOSS_LIMIT:.2f})[/red]")
                time.sleep(W2_SCAN_INTERVAL)
                continue

            # ── Discover weather events ──
            try:
                events = discover_weather_events()
                console.print(f"  Events: {len(events)} weather")
            except Exception as e:
                console.print(f"  [red]Discovery failed: {e}[/red]")
                time.sleep(W2_SCAN_INTERVAL)
                continue

            if not events:
                console.print("  [yellow]No active weather events[/yellow]")
                time.sleep(W2_SCAN_INTERVAL)
                continue

            # ── Fetch forecasts + ensemble for each city ──
            forecasts = {}
            ensemble_probs = {}
            cities_seen = set()

            for event in events:
                city = event.coin
                if city in cities_seen:
                    continue
                cities_seen.add(city)

                try:
                    fc = get_forecast(city, str(event.resolution_date))
                    if fc:
                        forecasts[city] = fc

                    # GFS ensemble probabilities per bracket
                    ensemble = get_ensemble_forecast(city, str(event.resolution_date))
                    if ensemble:
                        probs = {}
                        for market in event.markets:
                            prob = ensemble_bracket_prob(
                                ensemble,
                                market.threshold,
                                market.threshold_high,
                                market.bracket_type,
                                market.threshold_unit,
                            )
                            key = f"{market.threshold}_{market.bracket_type}"
                            probs[key] = prob
                        ensemble_probs[city] = probs
                except Exception as e:
                    console.print(f"  [dim]Forecast for {city} failed: {e}[/dim]")

            console.print(f"  Forecasts: {len(forecasts)} cities | Ensemble: {len(ensemble_probs)} cities")

            # ── Check resolutions on open positions ──
            if bankroll.open_positions:
                check_resolutions(bankroll, client, PAPER_TRADE)

            # ── Strategy 1: LADDER ──
            if W2_LADDER_ENABLED and bankroll.balance >= W2_LADDER_BET_SIZE * W2_LADDER_MIN_BRACKETS:
                ladders = find_ladder_opportunities(events, forecasts)
                if ladders:
                    console.print(f"  🪜 Found {len(ladders)} ladder opportunities")
                    for opp in ladders[:3]:  # Max 3 ladders per scan
                        if bankroll.balance >= W2_LADDER_BET_SIZE * opp["num_brackets"]:
                            asyncio.get_event_loop().run_until_complete(
                                execute_ladder(client, opp, bankroll, PAPER_TRADE)
                            ) if not asyncio.get_event_loop().is_running() else None
                            # Fallback for sync context
                            try:
                                loop = asyncio.get_event_loop()
                                if loop.is_running():
                                    import concurrent.futures
                                    with concurrent.futures.ThreadPoolExecutor() as pool:
                                        pool.submit(asyncio.run, execute_ladder(client, opp, bankroll, PAPER_TRADE)).result()
                                else:
                                    loop.run_until_complete(execute_ladder(client, opp, bankroll, PAPER_TRADE))
                            except Exception:
                                asyncio.run(execute_ladder(client, opp, bankroll, PAPER_TRADE))
                else:
                    console.print("  🪜 No ladder opportunities")

            # ── Strategy 2: FORECAST ARB ──
            if W2_FORECAST_ENABLED and bankroll.balance >= W2_FORECAST_BET_SIZE:
                arbs = find_forecast_arb_opportunities(events, forecasts, ensemble_probs)
                if arbs:
                    console.print(f"  🎯 Found {len(arbs)} forecast arb opportunities")
                    for opp in arbs[:5]:  # Max 5 per scan
                        if bankroll.balance >= W2_FORECAST_BET_SIZE:
                            try:
                                asyncio.run(execute_forecast_arb(client, opp, bankroll, PAPER_TRADE))
                            except Exception:
                                pass
                else:
                    console.print("  🎯 No forecast arb opportunities")

            # ── Strategy 3: WHALE COPY ──
            if W2_WHALE_ENABLED and bankroll.balance >= W2_WHALE_BET_SIZE:
                whales = find_whale_trades(events)
                if whales:
                    console.print(f"  🐋 Found {len(whales)} whale trades to copy")
                    for opp in whales[:3]:  # Max 3 per scan
                        if bankroll.balance >= W2_WHALE_BET_SIZE:
                            try:
                                asyncio.run(execute_whale_copy(client, opp, bankroll, PAPER_TRADE))
                            except Exception:
                                pass
                else:
                    console.print("  🐋 No whale trades found")

            # ── Status ──
            console.print(f"  {bankroll.status_line()}")

            # Telegram status every 6th scan (~30 min)
            if scan_count % 6 == 0:
                _send_tg(
                    f"🌤️ Weather V2 Status\n"
                    f"Scan #{scan_count}\n"
                    f"{bankroll.status_line()}"
                )

            time.sleep(W2_SCAN_INTERVAL)

    except KeyboardInterrupt:
        console.print("\n[yellow]Weather V2 stopping...[/yellow]")
        console.print(f"\n  {bankroll.status_line()}")


def main():
    run_weather_v2()


if __name__ == "__main__":
    main()
