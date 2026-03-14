"""
Bracket Market Engine v4.0
Trades daily BTC/ETH price brackets and weather temperature brackets.

Unlike v3.5 (15-min latency arb), v4 uses:
- Log-normal volatility model for crypto brackets
- NOAA/Open-Meteo forecast model for weather brackets
- Allium on-chain data for crypto signal boost
- Scans every 5 minutes instead of every second
"""

import asyncio
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone, date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from bracket_markets import (
    BracketEvent, BracketMarket,
    discover_all_events, refresh_event_prices,
)
from bracket_model import (
    crypto_bracket_prob, weather_bracket_prob,
    estimate_volatility, score_bracket, BracketScore,
    POLYMARKET_FEE,
)
from noaa_feed import get_forecast, CityForecast
from trader import init_client, PlacedOrder, save_order
from vpn import ensure_vpn
import telegram_alerts as tg

load_dotenv()
console = Console()

# --- Config ---
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
VPN_REQUIRED = os.getenv("PROTON_VPN_REQUIRED", "true").lower() == "true"

# v4 Config
V4_COINS = os.getenv("V4_COINS", "BTC,ETH").split(",")
V4_MIN_EDGE = float(os.getenv("V4_MIN_EDGE", "0.05"))          # 5% minimum edge
V4_SCAN_INTERVAL = int(os.getenv("V4_SCAN_INTERVAL", "300"))    # 5 min between scans
V4_MAX_BET = float(os.getenv("V4_MAX_BET", "10.0"))             # Higher max for daily markets
V4_MIN_BET = float(os.getenv("V4_MIN_BET", "2.0"))              # Higher floor
V4_KELLY_FRACTION = float(os.getenv("V4_KELLY_FRACTION", "0.10"))
V4_DAILY_BANKROLL = float(os.getenv("V4_DAILY_BANKROLL", "50.0"))
V4_MAX_ENTRY_PRICE = float(os.getenv("V4_MAX_ENTRY_PRICE", "0.80"))
V4_MAX_TRADES_PER_EVENT = int(os.getenv("V4_MAX_TRADES_PER_EVENT", "1"))

# Logging
LOG_DIR = Path("data")
LOG_FILE = LOG_DIR / "v4_trades.log"
TRADES_FILE = LOG_DIR / "v4_trades.json"


def log_trade(message: str):
    """Append a timestamped trade message to disk."""
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {message}\n"
    with open(LOG_FILE, "a") as f:
        f.write(line)


def save_trade_record(trade: dict):
    """Save structured trade record to JSON file."""
    LOG_DIR.mkdir(exist_ok=True)
    trades = []
    if TRADES_FILE.exists():
        try:
            trades = json.loads(TRADES_FILE.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            trades = []
    trades.append(trade)
    TRADES_FILE.write_text(json.dumps(trades, indent=2))


# --- Bankroll (adapted from v3.5) ---

class Bankroll:
    """Dynamic bankroll for daily bracket trading with downside protection."""

    # ── Circuit breaker thresholds ──
    MAX_DRAWDOWN_PCT = 0.50       # Stop trading if 50% of starting bankroll lost
    LOSS_STREAK_LIMIT = 5         # Pause after 5 consecutive losses
    LOSS_STREAK_COOLDOWN = 1800   # 30 min cooldown after loss streak (seconds)
    MIN_WIN_RATE_TRADES = 10      # Start checking win rate after 10 trades
    MIN_WIN_RATE = 0.30           # If win rate drops below 30% after 10+ trades, halt

    def __init__(self, starting: float):
        self.starting = starting
        self.balance = starting
        self.high_water_mark = starting  # Track peak balance
        self.total_wagered = 0.0
        self.wins = 0
        self.losses = 0
        self.loss_streak = 0             # Consecutive losses
        self.max_loss_streak = 0         # Worst streak ever
        self.loss_streak_paused_until = 0.0  # Timestamp when cooldown ends
        self.pending_trades: list[dict] = []
        self.traded_slugs: set[str] = set()
        self._circuit_broken = False
        self._circuit_reason = ""

    @property
    def pnl(self) -> float:
        return self.balance - self.starting

    @property
    def drawdown(self) -> float:
        """Current drawdown from high water mark (0.0 to 1.0)."""
        if self.high_water_mark <= 0:
            return 0.0
        return max(0, (self.high_water_mark - self.balance) / self.high_water_mark)

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0

    @property
    def can_trade(self) -> bool:
        """Check all bankroll guards before allowing a trade."""
        # Basic: enough balance
        if self.balance < V4_MIN_BET:
            return False

        # Circuit breaker tripped
        if self._circuit_broken:
            return False

        # Max drawdown guard
        if self.drawdown >= self.MAX_DRAWDOWN_PCT:
            self._trip_circuit(f"Max drawdown hit ({self.drawdown:.0%} from ${self.high_water_mark:.2f})")
            return False

        # Loss streak cooldown
        if self.loss_streak >= self.LOSS_STREAK_LIMIT:
            if time.time() < self.loss_streak_paused_until:
                return False
            else:
                # Cooldown expired, reset streak and continue
                console.print(f"[yellow][v4] Loss streak cooldown expired. Resuming trading.[/yellow]")
                self.loss_streak = 0

        # Win rate guard (only after enough trades)
        total = self.wins + self.losses
        if total >= self.MIN_WIN_RATE_TRADES and self.win_rate < self.MIN_WIN_RATE:
            self._trip_circuit(f"Win rate too low ({self.win_rate:.0%} after {total} trades)")
            return False

        return True

    def _trip_circuit(self, reason: str):
        """Trip the circuit breaker — stops all trading."""
        if not self._circuit_broken:
            self._circuit_broken = True
            self._circuit_reason = reason
            console.print(f"[bold red]🚨 CIRCUIT BREAKER: {reason}[/bold red]")
            console.print(f"[red]   Trading halted. Review performance before restarting.[/red]")
            tg.send_message(f"🚨 V4 CIRCUIT BREAKER\n{reason}\nBankroll: ${self.balance:.2f}\nP&L: {self.pnl:+.2f}\nWin rate: {self.win_rate:.0%}")
            log_trade(f"CIRCUIT BREAKER: {reason} | Balance: ${self.balance:.2f} | P&L: ${self.pnl:+.2f}")

    def already_traded(self, slug: str) -> bool:
        return slug in self.traded_slugs

    def place_bet(self, amount: float, score: BracketScore, order_id: str = "",
                  shares: float = 0, event_slug: str = "", market_type: str = ""):
        """Deduct bet from bankroll and track pending trade."""
        self.balance -= amount
        self.total_wagered += amount
        self.traded_slugs.add(score.slug)

        trade = {
            "amount": amount,
            "token_id": score.token_id,
            "side": score.best_side,
            "coin": score.question[:30],
            "buy_price": score.buy_price,
            "shares": shares if shares > 0 else amount / max(score.buy_price, 0.01),
            "timestamp": time.time(),
            "event_slug": event_slug,
            "market_slug": score.slug,
            "market_type": market_type,
            "order_id": order_id,
            "edge": score.best_edge,
            "model_prob": score.model_prob_yes if score.best_side == "yes" else (1 - score.model_prob_yes),
        }
        self.pending_trades.append(trade)

        label = score.question[:60]
        log_trade(
            f"BET: {label} {score.best_side.upper()} @ ${score.buy_price:.3f} | "
            f"${amount:.2f} | Edge: {score.best_edge:.1%} | Bankroll: ${self.balance:.2f}"
        )
        save_trade_record({
            "type": "bet",
            "question": score.question,
            "side": score.best_side,
            "buy_price": score.buy_price,
            "amount": amount,
            "edge": score.best_edge,
            "model_prob": trade["model_prob"],
            "bankroll_after": self.balance,
            "market_type": market_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # Telegram alert
        tg.alert_trade(
            label[:20], score.best_side, score.buy_price,
            amount, score.best_edge, 0, self.balance,
        )

    def resolve_trade(self, won: bool, trade: dict, payout: float = 0):
        """Resolve a completed trade with streak tracking."""
        label = trade.get("coin", "?")[:30]
        if won:
            self.wins += 1
            self.balance += payout
            self.loss_streak = 0  # Reset loss streak on any win
            self.high_water_mark = max(self.high_water_mark, self.balance)
            msg = f"WIN: {label} {trade['side'].upper()} | Bet ${trade['amount']:.2f} → Payout ${payout:.2f} | Bankroll: ${self.balance:.2f}"
            console.print(f"[bold green][v4] {msg}[/bold green]")
            tg.alert_win(label, trade["side"], trade["amount"], payout, self.balance)
        else:
            self.losses += 1
            self.loss_streak += 1
            self.max_loss_streak = max(self.max_loss_streak, self.loss_streak)
            msg = f"LOSS: {label} {trade['side'].upper()} | Lost ${trade['amount']:.2f} | Bankroll: ${self.balance:.2f} | Streak: {self.loss_streak}"
            console.print(f"[red][v4] {msg}[/red]")
            tg.alert_loss(label, trade["side"], trade["amount"], self.balance)

            # Trigger loss streak cooldown
            if self.loss_streak >= self.LOSS_STREAK_LIMIT:
                self.loss_streak_paused_until = time.time() + self.LOSS_STREAK_COOLDOWN
                console.print(f"[yellow]⚠️ {self.loss_streak} consecutive losses — pausing for {self.LOSS_STREAK_COOLDOWN // 60} min cooldown[/yellow]")
                tg.send_message(f"⚠️ V4 LOSS STREAK: {self.loss_streak} in a row\nPausing {self.LOSS_STREAK_COOLDOWN // 60} min\nBankroll: ${self.balance:.2f}")

        log_trade(msg)
        save_trade_record({
            "type": "win" if won else "loss",
            "question": trade.get("coin", ""),
            "side": trade["side"],
            "amount": trade["amount"],
            "payout": payout if won else 0,
            "bankroll_after": self.balance,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def check_pending_resolutions(self, client=None):
        """Check if any pending trades have resolved."""
        from tracker import get_current_price

        resolved = []
        for trade in self.pending_trades:
            try:
                current_price = get_current_price(trade["token_id"])
            except Exception:
                current_price = None

            if current_price is not None:
                if current_price >= 0.90:
                    # Resolved YES — winning side
                    shares = trade["amount"] / max(trade["buy_price"], 0.01)
                    payout = shares * 1.0
                    self.resolve_trade(won=True, trade=trade, payout=payout)
                    resolved.append(trade)
                elif current_price <= 0.10:
                    # Resolved NO — losing side
                    self.resolve_trade(won=False, trade=trade)
                    resolved.append(trade)

        for trade in resolved:
            self.pending_trades.remove(trade)

    def status_line(self) -> str:
        pnl = self.pnl
        pnl_color = "green" if pnl >= 0 else "red"
        dd_str = f" | DD: {self.drawdown:.0%}" if self.drawdown > 0.05 else ""
        streak_str = f" | L-streak: {self.loss_streak}" if self.loss_streak >= 2 else ""
        circuit_str = " | 🚨 HALTED" if self._circuit_broken else ""
        return (
            f"Bankroll: ${self.balance:.2f} | "
            f"P&L: [{pnl_color}]${pnl:+.2f}[/{pnl_color}] | "
            f"W/L: {self.wins}/{self.losses} ({self.win_rate:.0%}) | "
            f"Pending: {len(self.pending_trades)}"
            f"{dd_str}{streak_str}{circuit_str}"
        )


# --- Kelly Sizing ---

def kelly_bet_size(edge: float, buy_price: float, bankroll: float) -> float:
    """Kelly criterion bet sizing for binary bracket markets."""
    if edge <= 0 or buy_price >= 0.99:
        return V4_MIN_BET

    kelly_f = edge / (1.0 - buy_price)
    kelly_f = max(0.0, min(kelly_f, 0.50))

    bet = bankroll * kelly_f * V4_KELLY_FRACTION
    return round(max(V4_MIN_BET, min(bet, V4_MAX_BET)), 2)


# --- Orderbook + Trade Execution ---

def get_best_ask(client, token_id: str) -> tuple[float, float] | None:
    """Get best ask from CLOB orderbook."""
    try:
        book = client.get_order_book(token_id)
        if not book.asks:
            return None
        best = book.asks[0]
        return (float(best.price), float(best.size))
    except Exception:
        return None


def execute_bracket_trade(client, score: BracketScore, bankroll: Bankroll,
                          event_slug: str = "", market_type: str = "") -> PlacedOrder | None:
    """Execute a bracket trade on Polymarket CLOB."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    MIN_SHARES = 5

    # Get real best ask
    best = get_best_ask(client, score.token_id)
    if best is None:
        console.print(f"[dim][v4] No asks on book for {score.question[:50]}[/dim]")
        return None

    best_ask_price, best_ask_size = best

    # Recalculate edge at real ask
    model_prob = score.model_prob_yes if score.best_side == "yes" else (1 - score.model_prob_yes)
    real_edge = model_prob - best_ask_price - POLYMARKET_FEE

    if real_edge < V4_MIN_EDGE:
        console.print(
            f"[dim][v4] Edge too thin at ask: {real_edge:.1%} "
            f"(mid: {score.best_edge:.1%}) | Ask: ${best_ask_price:.3f}[/dim]"
        )
        return None

    if best_ask_price > V4_MAX_ENTRY_PRICE:
        console.print(f"[dim][v4] Ask ${best_ask_price:.3f} > max entry ${V4_MAX_ENTRY_PRICE}[/dim]")
        return None

    # Kelly sizing
    bet_amt = kelly_bet_size(real_edge, best_ask_price, bankroll.balance)

    # Limit price: 1¢ above best ask
    limit_price = min(round(best_ask_price + 0.01, 2), 0.99)
    raw_size = bet_amt / limit_price
    size = max(MIN_SHARES, math.floor(raw_size * 100) / 100)
    actual_cost = limit_price * size

    try:
        order_args = OrderArgs(
            token_id=score.token_id,
            price=limit_price,
            size=size,
            side=BUY,
        )
        signed_order = client.create_order(order_args)
        response = client.post_order(signed_order, OrderType.FOK)

        order_id = ""
        if isinstance(response, dict):
            order_id = response.get("orderID", response.get("id", ""))
            success = response.get("success", True)
        else:
            order_id = str(response)
            success = True

        if not success:
            console.print(f"[red][v4] Order rejected: {response}[/red]")
            log_trade(f"REJECTED: {score.question[:50]} | {response}")
            return None

        placed = PlacedOrder(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_title=event_slug,
            market_question=score.question,
            outcome=score.best_side.upper(),
            price=limit_price,
            size=size,
            usdc_spent=actual_cost,
            token_id=score.token_id,
            order_id=order_id,
            status="filled",
        )

        msg = (
            f"TRADE: {score.question[:50]} {score.best_side.upper()} "
            f"@ ${limit_price:.3f} | {size} shares | "
            f"Edge: {real_edge:.1%} | ${actual_cost:.2f} USDC"
        )
        console.print(f"[bold green][v4] {msg}[/bold green]")
        log_trade(msg)
        save_order(placed)

        # Record in bankroll
        bankroll.place_bet(
            amount=actual_cost,
            score=score,
            order_id=order_id,
            shares=size,
            event_slug=event_slug,
            market_type=market_type,
        )

        return placed

    except Exception as e:
        console.print(f"[red][v4] Trade failed: {e}[/red]")
        log_trade(f"ERROR: {score.question[:50]} | {e}")
        return None


# --- Scoring Engine ---

def score_crypto_event(event: BracketEvent, current_price: float, volatility: float) -> list[BracketScore]:
    """Score all brackets in a crypto event."""
    scores = []
    for market in event.markets:
        if not market.is_active:
            continue

        model_prob = crypto_bracket_prob(
            current_price=current_price,
            threshold=market.threshold,
            hours_remaining=market.hours_remaining,
            volatility=volatility,
        )

        s = score_bracket(
            question=market.question,
            threshold=market.threshold,
            model_prob_yes=model_prob,
            poly_yes_price=market.yes_price,
            poly_no_price=market.no_price,
            yes_token_id=market.yes_token_id,
            no_token_id=market.no_token_id,
            slug=market.slug,
        )
        scores.append(s)

    return scores


def score_weather_event(event: BracketEvent, forecast: CityForecast) -> list[BracketScore]:
    """Score all brackets in a weather event."""
    scores = []
    for market in event.markets:
        if not market.is_active:
            continue

        model_prob = weather_bracket_prob(
            forecast_temp=forecast.high_temp if forecast.unit == market.threshold_unit
                else (forecast.high_temp_f if market.threshold_unit == "°F" else forecast.high_temp_c),
            bracket_low=market.threshold,
            bracket_high=market.threshold_high,
            bracket_type=market.bracket_type,
            hours_remaining=market.hours_remaining,
            source=forecast.source,
            unit=market.threshold_unit,
        )

        s = score_bracket(
            question=market.question,
            threshold=market.threshold,
            model_prob_yes=model_prob,
            poly_yes_price=market.yes_price,
            poly_no_price=market.no_price,
            yes_token_id=market.yes_token_id,
            no_token_id=market.no_token_id,
            slug=market.slug,
        )
        scores.append(s)

    return scores


# --- Main Bot ---

async def run_bracket_bot():
    """Main v4 bracket bot loop."""
    console.print("[bold cyan]" + "=" * 60 + "[/bold cyan]")
    console.print("[bold cyan]  Polymarket Bracket Bot v4.0[/bold cyan]")
    console.print("[bold cyan]  Weather + BTC/ETH Daily Brackets[/bold cyan]")
    console.print("[bold cyan]" + "=" * 60 + "[/bold cyan]")
    console.print()

    # Config display
    console.print(f"  Coins:           {', '.join(V4_COINS)}")
    console.print(f"  Min edge:        {V4_MIN_EDGE:.0%}")
    console.print(f"  Scan interval:   {V4_SCAN_INTERVAL}s")
    console.print(f"  Bet range:       ${V4_MIN_BET:.0f}-${V4_MAX_BET:.0f}")
    console.print(f"  Daily bankroll:  ${V4_DAILY_BANKROLL:.0f}")
    console.print(f"  Kelly fraction:  {V4_KELLY_FRACTION:.0%}")
    console.print(f"  Max entry:       ${V4_MAX_ENTRY_PRICE}")
    console.print()

    # VPN check
    console.print("[vpn] Checking VPN connection...")
    if VPN_REQUIRED and not ensure_vpn():
        console.print("[red]VPN required but not connected. Exiting.[/red]")
        return
    console.print()

    # Init CLOB client
    client = init_client(PRIVATE_KEY, SIGNATURE_TYPE, WALLET_ADDRESS)

    # Init Bankroll
    bankroll = Bankroll(V4_DAILY_BANKROLL)

    # Get Binance prices for crypto
    from binance_feed import feed, connect_binance, get_initial_prices
    binance_task = asyncio.create_task(connect_binance())
    console.print("Fetching initial Binance prices...")
    await asyncio.sleep(3)
    prices = get_initial_prices()
    for coin, price in prices.items():
        console.print(f"  {coin}: ${price:,.2f}")
    console.print()

    # Allium init (optional — for crypto markets)
    allium_enabled = False
    try:
        from allium_feed import allium
        allium_ok = allium.test_connection()
        if allium_ok:
            allium_enabled = True
            console.print("[allium] Connection OK — on-chain signals active for crypto")
        else:
            console.print("[yellow][allium] Connection failed — proceeding without on-chain data[/yellow]")
    except Exception:
        console.print("[yellow][allium] Not available — proceeding without on-chain data[/yellow]")

    console.print()

    # Volatility cache
    vol_cache: dict[str, tuple[float, float]] = {}  # coin -> (vol, timestamp)
    VOL_CACHE_TTL = 3600  # Refresh every hour

    def get_volatility(coin: str) -> float:
        cached = vol_cache.get(coin)
        if cached and (time.time() - cached[1]) < VOL_CACHE_TTL:
            return cached[0]
        vol = estimate_volatility(coin)
        vol_cache[coin] = (vol, time.time())
        return vol

    # Estimate initial volatility
    for coin in V4_COINS:
        vol = get_volatility(coin)
        console.print(f"  {coin} volatility: {vol:.1%} annualized")
    console.print()

    # Main loop
    scan_count = 0

    while True:
        try:
            scan_count += 1
            now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

            # ── 1. Discover events ──
            console.print(f"[bold]── Scan #{scan_count} @ {now_str} ──[/bold]")
            events = discover_all_events(V4_COINS)

            crypto_events = [e for e in events if e.market_type == "crypto"]
            weather_events = [e for e in events if e.market_type == "weather"]
            console.print(
                f"  Events: {len(crypto_events)} crypto, {len(weather_events)} weather"
            )

            if not events:
                console.print("[yellow]  No active events found. Waiting...[/yellow]")
                await asyncio.sleep(V4_SCAN_INTERVAL)
                continue

            # ── 2. Score all brackets ──
            all_scores: list[tuple[BracketScore, BracketEvent]] = []

            # Crypto scoring
            for event in crypto_events:
                coin = event.coin
                binance_price = feed.get_price(coin)
                if binance_price is None:
                    continue

                vol = get_volatility(coin)
                scores = score_crypto_event(event, binance_price, vol)

                # Apply Allium signal as edge boost/penalty
                if allium_enabled:
                    try:
                        allium_sig = allium.get_bracket_signal(coin, event.slug)
                        if allium_sig.has_flow_data or allium_sig.has_smart_data:
                            console.print(f"  [dim][allium] {coin}: {allium_sig.summary()}[/dim]")
                    except Exception:
                        allium_sig = None
                else:
                    allium_sig = None

                for s in scores:
                    all_scores.append((s, event))

            # Weather scoring
            for event in weather_events:
                city = event.coin
                target_date = event.resolution_date.isoformat()
                forecast = get_forecast(city, target_date)

                if forecast is None:
                    continue

                scores = score_weather_event(event, forecast)
                for s in scores:
                    all_scores.append((s, event))

            # ── 3. Find best opportunities ──
            # Sort by edge descending
            all_scores.sort(key=lambda x: x[0].best_edge, reverse=True)

            # Show top opportunities
            tradeable = [(s, e) for s, e in all_scores if s.best_edge >= V4_MIN_EDGE]
            if tradeable:
                console.print(f"\n  [green]📊 {len(tradeable)} opportunities with edge ≥ {V4_MIN_EDGE:.0%}:[/green]")
                for s, e in tradeable[:10]:
                    icon = "₿" if e.market_type == "crypto" else "🌡️"
                    already = " [traded]" if bankroll.already_traded(s.slug) else ""
                    console.print(
                        f"    {icon} {s.question[:55]:55s} "
                        f"{s.best_side.upper():3s} @ ${s.buy_price:.3f} | "
                        f"Model: {s.model_prob_yes:.1%} Poly: {s.poly_yes_price:.3f} | "
                        f"Edge: {s.best_edge:+.1%}{already}"
                    )
            else:
                console.print(f"  [dim]No edges ≥ {V4_MIN_EDGE:.0%} found[/dim]")

            # ── 4. Execute trades ──
            if bankroll.can_trade and tradeable:
                for score, event in tradeable:
                    if not bankroll.can_trade:
                        break

                    # Skip if already traded this market
                    if bankroll.already_traded(score.slug):
                        continue

                    # Skip if already traded this event (max 1 per event)
                    event_traded = sum(1 for s in bankroll.traded_slugs
                                       if any(m.slug == s for m in event.markets))
                    if event_traded >= V4_MAX_TRADES_PER_EVENT:
                        continue

                    # Execute!
                    result = execute_bracket_trade(
                        client, score, bankroll,
                        event_slug=event.slug,
                        market_type=event.market_type,
                    )

                    if result:
                        console.print(f"  [bold green]✅ Trade placed![/bold green]")

            # ── 5. Check resolutions ──
            if bankroll.pending_trades:
                bankroll.check_pending_resolutions(client)

            # ── 6. Status ──
            console.print(f"\n  {bankroll.status_line()}")
            console.print()

            # ── 7. Sleep until next scan ──
            await asyncio.sleep(V4_SCAN_INTERVAL)

        except KeyboardInterrupt:
            console.print("\n[yellow]Bot stopped by user[/yellow]")
            break
        except Exception as e:
            console.print(f"[red]Error in main loop: {e}[/red]")
            log_trade(f"ERROR: Main loop: {e}")
            await asyncio.sleep(60)  # Wait a minute on error


def main():
    asyncio.run(run_bracket_bot())


if __name__ == "__main__":
    main()
