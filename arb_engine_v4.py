"""
Bracket Market Engine v4.2
Trades daily BTC/ETH price brackets and weather temperature brackets.

v4.1 changes (limit order hybrid):
- Posts GTC limit orders at model-fair price instead of FOK
- Cancels and refreshes all open orders every scan cycle
- Detects fills between cycles and records them in bankroll
- Eliminates the "edge too thin at ask" problem — we make the market

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
V4_MIN_EDGE = float(os.getenv("V4_MIN_EDGE", "0.15"))          # 15% minimum edge (raised from 5%)
V4_SCAN_INTERVAL = int(os.getenv("V4_SCAN_INTERVAL", "300"))    # 5 min between scans
V4_MAX_BET = float(os.getenv("V4_MAX_BET", "10.0"))             # Higher max for daily markets
V4_MIN_BET = float(os.getenv("V4_MIN_BET", "2.0"))              # Higher floor
V4_KELLY_FRACTION = float(os.getenv("V4_KELLY_FRACTION", "0.10"))
V4_DAILY_BANKROLL = float(os.getenv("V4_DAILY_BANKROLL", "50.0"))
V4_MAX_ENTRY_PRICE = float(os.getenv("V4_MAX_ENTRY_PRICE", "0.80"))
V4_MAX_TRADES_PER_EVENT = int(os.getenv("V4_MAX_TRADES_PER_EVENT", "1"))
V4_MAX_WEATHER_PER_CYCLE = int(os.getenv("V4_MAX_WEATHER_PER_CYCLE", "3"))
V4_MAX_CRYPTO_PER_CYCLE = int(os.getenv("V4_MAX_CRYPTO_PER_CYCLE", "3"))
V4_MAX_OPEN_POSITIONS = int(os.getenv("V4_MAX_OPEN_POSITIONS", "6"))
V4_MIN_HOURS_TO_RESOLUTION = float(os.getenv("V4_MIN_HOURS_TO_RESOLUTION", "6.0"))

# Per-category edge thresholds — weather and crypto are different beasts
V4_MIN_EDGE_WEATHER = float(os.getenv("V4_MIN_EDGE_WEATHER", "0.20"))  # 20% for weather (model uncertainty)
V4_MIN_EDGE_CRYPTO = float(os.getenv("V4_MIN_EDGE_CRYPTO", "0.10"))    # 10% for crypto (vol model is tighter)

# Win-rate optimization — favor bets we're likely to WIN
V4_MIN_WIN_PROB = float(os.getenv("V4_MIN_WIN_PROB", "0.65"))          # Only bet if model says ≥65% chance of winning
V4_MAX_BUY_PRICE = float(os.getenv("V4_MAX_BUY_PRICE", "0.50"))       # Don't pay >$0.50 per share (better risk/reward)

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
    MAX_DRAWDOWN_PCT = 0.35       # Stop trading if 35% of starting bankroll lost
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

def kelly_bet_size(edge: float, buy_price: float, bankroll: float, win_prob: float = 0.5) -> float:
    """Kelly criterion bet sizing for binary bracket markets.

    Scales up for high-conviction bets: if win_prob ≥ 80%, use 2x Kelly fraction.
    More wins → bigger bankroll → bigger bets → compounding machine.
    """
    if edge <= 0 or buy_price >= 0.99:
        return V4_MIN_BET

    kelly_f = edge / (1.0 - buy_price)
    kelly_f = max(0.0, min(kelly_f, 0.50))

    # Scale Kelly fraction based on conviction
    # High win probability = more confident = bet more
    fraction = V4_KELLY_FRACTION
    if win_prob >= 0.85:
        fraction = V4_KELLY_FRACTION * 2.5    # Very high conviction → 25% Kelly
    elif win_prob >= 0.75:
        fraction = V4_KELLY_FRACTION * 1.5    # High conviction → 15% Kelly

    bet = bankroll * kelly_f * fraction
    return round(max(V4_MIN_BET, min(bet, V4_MAX_BET)), 2)


# --- Order Manager (v4.1 — hybrid limit orders) ---

@dataclass
class OpenOrder:
    """Tracks a live GTC limit order on the CLOB."""
    order_id: str
    token_id: str
    score: BracketScore
    limit_price: float
    size: float
    cost: float
    event_slug: str
    market_type: str
    placed_at: float  # time.time()


class OrderManager:
    """
    Manages GTC limit orders with cancel-and-refresh each scan cycle.

    Flow each cycle:
    1. Check which old orders filled (via get_orders — if missing, it filled)
    2. Record fills in bankroll
    3. Cancel all remaining open orders (stale prices)
    4. Post fresh orders at updated model prices
    """

    def __init__(self):
        self.open_orders: list[OpenOrder] = []

    def check_fills_and_cancel(self, client, bankroll: Bankroll):
        """
        Check for fills since last cycle, then cancel all remaining open orders.
        Returns number of fills detected.
        """
        if not self.open_orders:
            return 0

        fills = 0

        # Get all currently open orders from CLOB
        try:
            live_orders = client.get_orders()
            live_ids = {o.get("id", o.get("orderID", "")) for o in live_orders}
        except Exception as e:
            console.print(f"[yellow][v4] Could not fetch open orders: {e}[/yellow]")
            # If we can't check, cancel everything to be safe
            self._cancel_all_safe(client)
            self.open_orders.clear()
            return 0

        # Check each tracked order — if not in live_ids, it filled (or was cancelled externally)
        still_open = []
        for order in self.open_orders:
            if order.order_id not in live_ids:
                # Order is gone from the book → filled!
                fills += 1
                msg = (
                    f"✅ FILLED: {order.score.question[:50]} {order.score.best_side.upper()} "
                    f"@ ${order.limit_price:.3f} | {order.size:.1f} shares | "
                    f"${order.cost:.2f} USDC"
                )
                console.print(f"[bold green][v4] {msg}[/bold green]")
                log_trade(msg)

                # Record in bankroll
                bankroll.place_bet(
                    amount=order.cost,
                    score=order.score,
                    order_id=order.order_id,
                    shares=order.size,
                    event_slug=order.event_slug,
                    market_type=order.market_type,
                )

                # Save to order history
                placed = PlacedOrder(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    event_title=order.event_slug,
                    market_question=order.score.question,
                    outcome=order.score.best_side.upper(),
                    price=order.limit_price,
                    size=order.size,
                    usdc_spent=order.cost,
                    token_id=order.token_id,
                    order_id=order.order_id,
                    status="filled",
                )
                save_order(placed)
            else:
                still_open.append(order)

        # Cancel all remaining open orders (stale prices)
        if still_open:
            cancel_ids = [o.order_id for o in still_open]
            try:
                client.cancel_orders(cancel_ids)
                console.print(f"[dim][v4] Cancelled {len(cancel_ids)} stale orders[/dim]")
            except Exception as e:
                console.print(f"[yellow][v4] Cancel failed, trying cancel_all: {e}[/yellow]")
                self._cancel_all_safe(client)

        # Telegram notification on cycle reset
        if fills or still_open:
            parts = []
            if fills:
                parts.append(f"🎯 {fills} filled")
            if still_open:
                parts.append(f"🔄 {len(still_open)} cancelled")
            tg.send_message(f"📋 Order cycle reset: {' | '.join(parts)}\nBankroll: ${bankroll.balance:.2f}")

        self.open_orders.clear()
        return fills

    def post_limit_order(self, client, score: BracketScore, bankroll: Bankroll,
                         event_slug: str = "", market_type: str = "") -> OpenOrder | None:
        """Post a GTC limit order at our model-fair bid price."""
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        MIN_SHARES = 5

        # Our bid price = model_prob - fee - small buffer
        # We want to buy at a price where we still have edge
        model_prob = score.model_prob_yes if score.best_side == "yes" else (1 - score.model_prob_yes)

        # Bid at model_prob minus fee minus 2% buffer (keeps ~2% edge minimum)
        bid_price = round(model_prob - POLYMARKET_FEE - 0.02, 2)

        # Clamp to valid range — also enforce max buy price for risk/reward
        bid_price = max(0.01, min(bid_price, V4_MAX_ENTRY_PRICE, V4_MAX_BUY_PRICE))

        # Don't bid above the current mid-price (don't overpay)
        mid_price = score.buy_price
        if bid_price > mid_price and mid_price > 0.01:
            # Bid at mid-price — still has edge since model_prob > mid + fee
            bid_price = round(mid_price, 2)

        # Verify we still have edge at our bid
        edge_at_bid = model_prob - bid_price - POLYMARKET_FEE
        if edge_at_bid < V4_MIN_EDGE:
            return None

        # Kelly sizing at our bid price — scales up for high-conviction bets
        bet_amt = kelly_bet_size(edge_at_bid, bid_price, bankroll.balance, win_prob=model_prob)

        raw_size = bet_amt / bid_price
        size = max(MIN_SHARES, math.floor(raw_size * 100) / 100)
        actual_cost = bid_price * size

        # Don't exceed remaining bankroll
        if actual_cost > bankroll.balance:
            size = math.floor((bankroll.balance / bid_price) * 100) / 100
            if size < MIN_SHARES:
                return None
            actual_cost = bid_price * size

        try:
            order_args = OrderArgs(
                token_id=score.token_id,
                price=bid_price,
                size=size,
                side=BUY,
            )
            signed_order = client.create_order(order_args)
            response = client.post_order(signed_order, OrderType.GTC)

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

            open_order = OpenOrder(
                order_id=order_id,
                token_id=score.token_id,
                score=score,
                limit_price=bid_price,
                size=size,
                cost=actual_cost,
                event_slug=event_slug,
                market_type=market_type,
                placed_at=time.time(),
            )
            self.open_orders.append(open_order)

            msg = (
                f"📋 BID: {score.question[:50]} {score.best_side.upper()} "
                f"@ ${bid_price:.3f} | {size:.1f} shares | "
                f"Edge: {edge_at_bid:.1%} | ${actual_cost:.2f}"
            )
            console.print(f"[cyan][v4] {msg}[/cyan]")
            log_trade(msg)

            return open_order

        except Exception as e:
            console.print(f"[red][v4] Limit order failed: {e}[/red]")
            log_trade(f"ERROR: {score.question[:50]} | {e}")
            return None

    def _cancel_all_safe(self, client):
        """Fallback: cancel all orders for this account."""
        try:
            client.cancel_all()
        except Exception as e:
            console.print(f"[red][v4] cancel_all failed: {e}[/red]")

    @property
    def total_locked(self) -> float:
        """Total USDC locked in open orders."""
        return sum(o.cost for o in self.open_orders)

    @property
    def count(self) -> int:
        return len(self.open_orders)


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
    console.print("[bold cyan]  Polymarket Bracket Bot v4.2[/bold cyan]")
    console.print("[bold cyan]  Weather + BTC/ETH Daily Brackets[/bold cyan]")
    console.print("[bold cyan]" + "=" * 60 + "[/bold cyan]")
    console.print()

    # Config display
    console.print(f"  Coins:           {', '.join(V4_COINS)}")
    console.print(f"  Min edge:        {V4_MIN_EDGE_CRYPTO:.0%} crypto / {V4_MIN_EDGE_WEATHER:.0%} weather")
    console.print(f"  Scan interval:   {V4_SCAN_INTERVAL}s")
    console.print(f"  Bet range:       ${V4_MIN_BET:.0f}-${V4_MAX_BET:.0f}")
    console.print(f"  Daily bankroll:  ${V4_DAILY_BANKROLL:.0f}")
    console.print(f"  Kelly fraction:  {V4_KELLY_FRACTION:.0%}")
    console.print(f"  Max entry:       ${V4_MAX_ENTRY_PRICE}")
    console.print(f"  Max positions:   {V4_MAX_OPEN_POSITIONS} total ({V4_MAX_WEATHER_PER_CYCLE}W/{V4_MAX_CRYPTO_PER_CYCLE}C per cycle)")
    console.print(f"  Skip if <{V4_MIN_HOURS_TO_RESOLUTION:.0f}h to resolution")
    console.print(f"  Drawdown limit:  {Bankroll.MAX_DRAWDOWN_PCT:.0%}")
    console.print(f"  Min win prob:    {V4_MIN_WIN_PROB:.0%} (only bet when likely to win)")
    console.print(f"  Max buy price:   ${V4_MAX_BUY_PRICE:.2f} (cheap shares = better upside)")
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

    # Order manager (v4.1 — hybrid limit orders)
    order_mgr = OrderManager()

    # Main loop
    scan_count = 0

    while True:
        try:
            scan_count += 1
            now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            console.print(f"[bold]── Scan #{scan_count} @ {now_str} ──[/bold]")

            # ── 0. Check fills + cancel stale orders ──
            fills = order_mgr.check_fills_and_cancel(client, bankroll)
            if fills:
                console.print(f"  [bold green]🎯 {fills} order(s) filled since last scan![/bold green]")

            # ── 1. Discover events ──
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
            # Filter by: edge threshold, win probability, and max buy price
            tradeable = []
            for s, e in all_scores:
                min_edge = V4_MIN_EDGE_WEATHER if e.market_type == "weather" else V4_MIN_EDGE_CRYPTO
                if s.best_edge < min_edge:
                    continue

                # Win probability = model prob of the side we're betting on
                win_prob = s.model_prob_yes if s.best_side == "yes" else (1 - s.model_prob_yes)

                # Only bet when we're LIKELY to win (≥65%)
                if win_prob < V4_MIN_WIN_PROB:
                    continue

                # Don't overpay — cheap shares have better risk/reward
                # Paying $0.80 for NO risks $0.80 to make $0.20 (terrible)
                # Paying $0.30 for YES risks $0.30 to make $0.70 (great)
                if s.buy_price > V4_MAX_BUY_PRICE:
                    continue

                tradeable.append((s, e))

            # Sort by expected value (edge × win_prob), not just edge
            # This favors high-probability bets that compound
            tradeable.sort(key=lambda x: x[0].best_edge * (
                x[0].model_prob_yes if x[0].best_side == "yes" else (1 - x[0].model_prob_yes)
            ), reverse=True)

            if tradeable:
                console.print(f"\n  [green]📊 {len(tradeable)} high-conviction opportunities:[/green]")
                for s, e in tradeable[:10]:
                    icon = "₿" if e.market_type == "crypto" else "🌡️"
                    win_p = s.model_prob_yes if s.best_side == "yes" else (1 - s.model_prob_yes)
                    already = " [traded]" if bankroll.already_traded(s.slug) else ""
                    console.print(
                        f"    {icon} {s.question[:50]:50s} "
                        f"{s.best_side.upper():3s} @ ${s.buy_price:.3f} | "
                        f"Win: {win_p:.0%} Edge: {s.best_edge:+.1%}{already}"
                    )
            else:
                console.print(f"  [dim]No bets pass filters (edge + win prob ≥{V4_MIN_WIN_PROB:.0%} + price ≤${V4_MAX_BUY_PRICE:.2f})[/dim]")

            # ── 4. Post fresh limit orders ──
            orders_posted = 0
            if bankroll.can_trade and tradeable:
                # Budget for this cycle: don't lock more than remaining bankroll
                cycle_budget = bankroll.balance
                cycle_spent = 0.0

                # Category counters — enforce diversification
                weather_count = 0
                crypto_count = 0
                total_open = len(bankroll.pending_trades)  # Already-filled positions

                for score, event in tradeable:
                    if not bankroll.can_trade:
                        break
                    if cycle_spent >= cycle_budget * 0.80:  # Keep 20% reserve
                        break

                    # ── Position limits ──
                    if total_open + orders_posted >= V4_MAX_OPEN_POSITIONS:
                        console.print(f"  [yellow]Max {V4_MAX_OPEN_POSITIONS} open positions reached[/yellow]")
                        break

                    # Per-category limits
                    if event.market_type == "weather" and weather_count >= V4_MAX_WEATHER_PER_CYCLE:
                        continue
                    if event.market_type == "crypto" and crypto_count >= V4_MAX_CRYPTO_PER_CYCLE:
                        continue

                    # ── Skip markets resolving too soon (adverse selection risk) ──
                    market_hours = min(
                        (m.hours_remaining for m in event.markets if m.is_active),
                        default=24
                    )
                    if market_hours < V4_MIN_HOURS_TO_RESOLUTION:
                        continue

                    # Skip if already traded this market (filled in a previous cycle)
                    if bankroll.already_traded(score.slug):
                        continue

                    # Skip if already traded this event (max per event)
                    event_traded = sum(1 for s in bankroll.traded_slugs
                                       if any(m.slug == s for m in event.markets))
                    if event_traded >= V4_MAX_TRADES_PER_EVENT:
                        continue

                    # Post limit order
                    order = order_mgr.post_limit_order(
                        client, score, bankroll,
                        event_slug=event.slug,
                        market_type=event.market_type,
                    )

                    if order:
                        orders_posted += 1
                        cycle_spent += order.cost
                        if event.market_type == "weather":
                            weather_count += 1
                        else:
                            crypto_count += 1

                if orders_posted:
                    console.print(
                        f"  [cyan]📋 {orders_posted} limit orders posted "
                        f"({weather_count}W/{crypto_count}C) | "
                        f"${order_mgr.total_locked:.2f} USDC on book[/cyan]"
                    )

            # ── 5. Check resolutions ──
            if bankroll.pending_trades:
                bankroll.check_pending_resolutions(client)

            # ── 6. Status ──
            open_str = f" | Open: {order_mgr.count}" if order_mgr.count else ""
            console.print(f"\n  {bankroll.status_line()}{open_str}")
            console.print()

            # ── 7. Sleep until next scan ──
            await asyncio.sleep(V4_SCAN_INTERVAL)

        except KeyboardInterrupt:
            # Clean up: cancel all open orders on exit
            console.print("\n[yellow]Cancelling open orders...[/yellow]")
            order_mgr._cancel_all_safe(client)
            console.print("[yellow]Bot stopped by user[/yellow]")
            break
        except Exception as e:
            console.print(f"[red]Error in main loop: {e}[/red]")
            log_trade(f"ERROR: Main loop: {e}")
            await asyncio.sleep(60)


def main():
    asyncio.run(run_bracket_bot())


if __name__ == "__main__":
    main()
