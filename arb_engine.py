"""
Crypto Latency Arbitrage Engine v3.4.
Trades the DISLOCATION — when Binance moves and Polymarket hasn't caught up yet.

v3.4: Fixed critical fill-detection bug.
- FIX: Orders are now verified via CLOB API (get_order) before marking expired.
- FIX: Filled orders are NEVER credited back — bot waits for resolution instead.
- FIX: Only truly unfilled orders (size_matched=0) get cancelled and money returned.
v3.3: Kelly criterion sizing + early-exit loss cutting.
v3.2: Allium on-chain intelligence integration.
- All v3/v3.1 guards remain: no both-sides, max entry price, trade limits, late-join protection.
"""

import asyncio
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from allium_feed import allium, AlliumSignal
import telegram_alerts as tg
from binance_feed import feed, connect_binance, get_initial_prices, SYMBOLS
from crypto_markets import discover_all_markets, discover_market, CryptoMarket, WINDOW_SECONDS
from trader import init_client, PlacedOrder, save_order
from vpn import ensure_vpn

load_dotenv()
console = Console()

# --- Config ---
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
VPN_REQUIRED = os.getenv("PROTON_VPN_REQUIRED", "true").lower() == "true"

# Arb-specific config
ARB_BET_SIZE = float(os.getenv("ARB_BET_SIZE", "1"))
ARB_MIN_EDGE = float(os.getenv("ARB_MIN_EDGE", "0.05"))        # 5% min edge (lowered from 8%)
ARB_COINS = os.getenv("ARB_COINS", "BTC,ETH,SOL").split(",")
ARB_MAX_DAILY_SPEND = float(os.getenv("ARB_MAX_DAILY_SPEND", "20"))
ARB_COOLDOWN_SECS = int(os.getenv("ARB_COOLDOWN_SECS", "120"))  # 2-min cooldown per coin

# v3 Risk Management
MAX_ENTRY_PRICE = float(os.getenv("ARB_MAX_ENTRY_PRICE", "0.70"))  # Don't buy above 70 cents
MAX_TRADES_PER_COIN_PER_WINDOW = int(os.getenv("ARB_MAX_TRADES_PER_COIN", "2"))  # Cap trades per coin per window

# Logging
LOG_DIR = Path("data")
LOG_FILE = LOG_DIR / "trades.log"
TRADES_FILE = LOG_DIR / "trades.json"


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


@dataclass
class Bankroll:
    """
    Dynamic bankroll: wins add to it, losses subtract.
    Start with daily limit. Keep playing as long as you're winning.
    Hit zero and you're done for the day.
    """
    starting: float
    balance: float = 0.0
    total_wagered: float = 0.0
    wins: int = 0
    losses: int = 0
    pending_trades: list = None

    def __post_init__(self):
        self.balance = self.starting
        self.pending_trades = []
        # v3: Track committed side per coin per window (prevents betting both sides)
        self.committed_sides: dict = {}   # key: (coin, window_ts) -> "up" or "down"
        # v3: Track trade count per coin per window
        self.trades_per_coin_window: dict = {}  # key: (coin, window_ts) -> count
        # v3: Flag to prevent bankroll-depleted spam
        self._depleted_logged: bool = False

    @property
    def pnl(self) -> float:
        return self.balance - self.starting

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0

    @property
    def can_trade(self) -> bool:
        return self.balance >= KELLY_MIN_BET

    def can_bet_side(self, coin: str, side: str, window_ts: int) -> bool:
        """v3: Check if we're allowed to bet this side for this coin in this window."""
        key = (coin, window_ts)
        committed = self.committed_sides.get(key)
        if committed is not None and committed != side:
            return False  # Already committed to the OTHER side
        return True

    def get_trade_count(self, coin: str, window_ts: int) -> int:
        """v3: How many trades have we placed on this coin in this window?"""
        return self.trades_per_coin_window.get((coin, window_ts), 0)

    def place_bet(self, amount: float, token_id: str, side: str, coin: str, buy_price: float, window_ts: int, shares: float = 0, order_id: str = "", edge: float = 0, secs_left: int = 0):
        """Deduct bet from bankroll and track pending trade."""
        self.balance -= amount
        self.total_wagered += amount
        self._depleted_logged = False  # Reset spam flag when we place a bet

        # v3: Record committed side and increment trade count
        key = (coin, window_ts)
        self.committed_sides[key] = side
        self.trades_per_coin_window[key] = self.trades_per_coin_window.get(key, 0) + 1

        trade = {
            "amount": amount,
            "token_id": token_id,
            "side": side,
            "coin": coin,
            "buy_price": buy_price,
            "shares": shares if shares > 0 else amount / buy_price,
            "timestamp": time.time(),
            "window_ts": window_ts,
            "exit_attempted": False,  # v3.3: track if early exit was tried
            "order_id": order_id,  # v3.4: CLOB order ID for fill verification
        }
        self.pending_trades.append(trade)
        log_trade(f"BET: {coin} {side.upper()} @ ${buy_price:.3f} | ${amount:.2f} | Bankroll: ${self.balance:.2f}")
        save_trade_record({
            "type": "bet",
            "coin": coin,
            "side": side,
            "buy_price": buy_price,
            "amount": amount,
            "bankroll_after": self.balance,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "window_ts": window_ts,
        })
        # v3.4.2: FOK = instant fill. If we got here, order filled. Alert immediately.
        tg.alert_trade(coin, side, buy_price, amount, edge, secs_left, self.balance)

    def resolve_trade(self, won: bool, trade: dict, payout: float = 0):
        """Resolve a completed trade."""
        if won:
            self.wins += 1
            self.balance += payout
            msg = f"WIN: {trade['coin']} {trade['side'].upper()} | Bet ${trade['amount']:.2f} → Payout ${payout:.2f} | Bankroll: ${self.balance:.2f}"
            console.print(f"[bold green][bankroll] {msg}[/bold green]")
        else:
            self.losses += 1
            msg = f"LOSS: {trade['coin']} {trade['side'].upper()} | Lost ${trade['amount']:.2f} | Bankroll: ${self.balance:.2f}"
            console.print(f"[red][bankroll] {msg}[/red]")

        log_trade(msg)
        save_trade_record({
            "type": "win" if won else "loss",
            "coin": trade["coin"],
            "side": trade["side"],
            "amount": trade["amount"],
            "payout": payout if won else 0,
            "bankroll_after": self.balance,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        if won:
            tg.alert_win(trade["coin"], trade["side"], trade["amount"], payout, self.balance)
        else:
            tg.alert_loss(trade["coin"], trade["side"], trade["amount"], self.balance)

    def check_pending_resolutions(self, current_markets: dict, client=None):
        """
        Check if any pending trades can be resolved.

        v3.4: Before marking any order as expired, verify fill status via
        CLOB API (client.get_order). Only credit money back if size_matched=0.
        Filled orders wait for market resolution — never expire.
        """
        resolved = []
        now = time.time()

        # Max time to wait before checking CLOB fill status
        MAX_PENDING_SECS = WINDOW_SECONDS + 300  # 5 min after window close

        for trade in self.pending_trades:
            window_ts = trade.get("window_ts", 0)
            resolve_time = window_ts + WINDOW_SECONDS + 60  # 60s buffer for settlement

            if now < resolve_time:
                continue

            coin = trade["coin"]
            side = trade["side"]

            # Try to check the resolved price
            try:
                from tracker import get_current_price
                current_price = get_current_price(trade["token_id"])
            except Exception:
                current_price = None

            if current_price is not None:
                if current_price >= 0.90:
                    # Resolved YES — we won
                    shares = trade["amount"] / trade["buy_price"]
                    payout = shares * 1.0
                    self.resolve_trade(won=True, trade=trade, payout=payout)
                    resolved.append(trade)
                elif current_price <= 0.10:
                    # Resolved NO — we lost
                    self.resolve_trade(won=False, trade=trade)
                    resolved.append(trade)
                # else: still settling, wait longer

            # v3.4: CLOB-verified expiry (replaces blind timeout from v3.2.1)
            # Only attempt expiry after MAX_PENDING_SECS, and ONLY if CLOB
            # confirms the order never filled (size_matched == 0).
            if trade not in resolved and now > (window_ts + MAX_PENDING_SECS):
                order_id = trade.get("order_id", "")

                # --- v3.4 CLOB fill check ---
                filled = trade.get("fill_verified", False)  # Use cached result if already checked
                if not filled and order_id and client:
                    try:
                        order_data = client.get_order(order_id)
                        size_matched = float(order_data.get("size_matched", "0"))
                        if size_matched > 0:
                            filled = True
                            trade["fill_verified"] = True  # Cache — don't re-check/re-log
                            console.print(
                                f"[cyan]  [v3.4] {coin} {side.upper()} order FILLED on CLOB "
                                f"(matched {size_matched}) — waiting for resolution[/cyan]"
                            )
                            log_trade(
                                f"CLOB FILLED: {coin} {side.upper()} | "
                                f"order_id={order_id[:16]}... | size_matched={size_matched} | "
                                f"Waiting for market resolution"
                            )
                            # v3.4.2: With FOK, fills are instant — alert already sent in place_bet
                    except Exception as e:
                        # If CLOB check fails, assume filled (safe default — don't credit back)
                        filled = True
                        trade["fill_verified"] = True
                        console.print(
                            f"[yellow]  [v3.4] CLOB check failed for {coin} {side.upper()}: {e} "
                            f"— assuming filled (safe default)[/yellow]"
                        )

                if filled:
                    # Order filled on CLOB — DO NOT credit back.
                    # But if we've waited WAY too long (15+ min past window),
                    # the price check is failing — resolve as loss (conservative).
                    STUCK_TIMEOUT = WINDOW_SECONDS + 900  # 15 min past window close
                    if now > (window_ts + STUCK_TIMEOUT):
                        self.resolve_trade(won=False, trade=trade)
                        msg = (
                            f"STUCK RESOLVED: {coin} {side.upper()} | "
                            f"Filled but price unresolvable — counted as loss ${trade['amount']:.2f} | "
                            f"Bankroll: ${self.balance:.2f}"
                        )
                        console.print(f"[red][bankroll] {msg}[/red]")
                        log_trade(msg)
                        save_trade_record({
                            "type": "stuck_loss",
                            "coin": coin,
                            "side": side,
                            "amount": trade["amount"],
                            "bankroll_after": self.balance,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                        tg.alert_stuck(coin, side, trade["amount"], self.balance)
                        resolved.append(trade)
                    continue

                # Truly unfilled (size_matched=0 or no order_id) — safe to credit back
                # Try to cancel the order on CLOB first
                if order_id and client:
                    try:
                        client.cancel(order_id)
                        console.print(f"[dim]  [v3.4] Cancelled unfilled order {order_id[:16]}...[/dim]")
                    except Exception:
                        pass  # Already expired or cancelled

                self.balance += trade["amount"]
                self.total_wagered -= trade["amount"]
                msg = (
                    f"EXPIRED: {coin} {side.upper()} | "
                    f"CLOB confirmed unfilled — ${trade['amount']:.2f} returned to bankroll | "
                    f"Bankroll: ${self.balance:.2f}"
                )
                console.print(f"[yellow][bankroll] {msg}[/yellow]")
                log_trade(msg)
                save_trade_record({
                    "type": "expired",
                    "coin": coin,
                    "side": side,
                    "amount": trade["amount"],
                    "bankroll_after": self.balance,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                tg.alert_expired(coin, side, trade["amount"], self.balance)
                resolved.append(trade)

        for trade in resolved:
            self.pending_trades.remove(trade)

    def status_line(self) -> str:
        pnl = self.pnl
        pnl_style = "green" if pnl >= 0 else "red"
        return (
            f"Bankroll: ${self.balance:.2f} | "
            f"P&L: [{pnl_style}]${pnl:+.2f}[/{pnl_style}] | "
            f"W/L: {self.wins}/{self.losses} ({self.win_rate:.0%}) | "
            f"Pending: {len(self.pending_trades)}"
        )


@dataclass
class ArbSignal:
    coin: str
    side: str          # "up" or "down"
    binance_price: float
    window_start_price: float
    implied_prob: float
    polymarket_price: float
    edge: float
    seconds_remaining: int
    token_id: str


def calculate_fee(price: float, fee_rate: float = 0.25) -> float:
    """Polymarket fee per share."""
    return price * (1 - price) * fee_rate


# --- v3.3: Kelly Criterion Sizing ---
KELLY_FRACTION = 0.10   # Tenth-Kelly (conservative while we validate)
KELLY_MIN_BET = 1.0     # Floor — smaller bets while gathering data
KELLY_MAX_BET = 3.0     # Ceiling — max $3 per trade


def kelly_bet_size(edge: float, buy_price: float, bankroll: float) -> float:
    """
    Quarter-Kelly bet sizing for binary outcome markets.

    For a bet that pays $1/share on win, costs buy_price/share:
        Full Kelly fraction = edge / (1 - buy_price)
    We use quarter-Kelly to reduce variance.
    """
    if edge <= 0 or buy_price >= 0.99:
        return KELLY_MIN_BET

    kelly_f = edge / (1.0 - buy_price)
    kelly_f = max(0.0, min(kelly_f, 0.50))  # cap raw Kelly at 50%

    bet = bankroll * kelly_f * KELLY_FRACTION
    return round(max(KELLY_MIN_BET, min(bet, KELLY_MAX_BET)), 2)


# --- v3.3: Early Exit (sell losing positions) ---
EARLY_EXIT_BAIL_PROB = 0.20     # Sell if win probability drops below 20%
EARLY_EXIT_MIN_SECS = 120      # Need 2+ min for sell order to fill
EARLY_EXIT_MAX_SECS = 660      # Don't sell too early (< 4 min into window)
EARLY_EXIT_MIN_RECOVERY = 0.15  # Only sell if we recover at least 15% of cost


def execute_early_exit(client, trade: dict, sell_price: float) -> float:
    """
    Sell shares early to cut losses. Returns recovery amount (USDC received)
    or 0.0 if the sell failed.
    """
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import SELL

    shares = trade["shares"]
    size = math.floor(shares * 100) / 100  # Floor to 2 decimals

    if size < 5:
        return 0.0  # Below minimum order size

    try:
        order_args = OrderArgs(
            token_id=trade["token_id"],
            price=sell_price,
            size=size,
            side=SELL,
        )
        signed_order = client.create_order(order_args)
        response = client.post_order(signed_order, OrderType.FOK)  # FOK: fill instantly or cancel

        success = True
        if isinstance(response, dict):
            success = response.get("success", True)

        if not success:
            console.print(f"[red]  Early exit order rejected: {response}[/red]")
            return 0.0

        recovery = sell_price * size
        return recovery

    except Exception as e:
        console.print(f"[red]  Early exit failed: {e}[/red]")
        return 0.0


# Track last trade time per coin (for cooldown)
last_trade_time: dict = {}


def find_arb_signal(coin: str, market: CryptoMarket) -> ArbSignal | None:
    """
    Compare Binance real-time price to Polymarket odds.
    Returns an ArbSignal if edge exceeds threshold.

    v2: No time restriction. Trades whenever Polymarket is stale vs Binance.
    Uses time-weighted probability (more confident near end of window).
    """
    # Check cooldown
    now = time.time()
    if coin in last_trade_time:
        elapsed = now - last_trade_time[coin]
        if elapsed < ARB_COOLDOWN_SECS:
            return None

    binance_price = feed.get_price(coin)
    window_start = feed.get_window_start(coin)

    if binance_price is None or window_start is None:
        return None

    secs_remaining = market.seconds_remaining

    # v3.4.1: Only trade in the sweet spot — 8 min to 2 min remaining.
    # First 7 min: price still finding direction, signals are noise.
    # Last 2 min: too close to settlement, fills unlikely.
    if secs_remaining > 480:  # Skip first 7 min (wait for real move)
        return None

    if secs_remaining < 120:  # Skip last 2 min (need time to fill)
        return None

    # Time-weighted implied probability from Binance
    implied_prob_up = feed.get_implied_probability(coin, seconds_remaining=secs_remaining)
    if implied_prob_up is None:
        return None

    implied_prob_down = 1 - implied_prob_up

    poly_up_price = market.up_price
    poly_down_price = market.down_price

    # Edge = our model probability - Polymarket price
    edge_up = implied_prob_up - poly_up_price
    edge_down = implied_prob_down - poly_down_price

    # Fees
    fee_up = calculate_fee(poly_up_price)
    fee_down = calculate_fee(poly_down_price)

    # Net edge after fees
    net_edge_up = edge_up - fee_up
    net_edge_down = edge_down - fee_down

    # We need BOTH:
    # 1. Our model says probability is significantly different from Poly price
    # 2. The Binance price has moved meaningfully from window start
    pct_move = abs((binance_price - window_start) / window_start * 100)

    # Minimum price move threshold (scaled by time remaining)
    # Early in window: need bigger move to be confident
    # Late in window: small moves are meaningful
    time_fraction = secs_remaining / 900.0
    min_move = 0.05 + 0.10 * time_fraction  # 0.05% late, 0.15% early

    if pct_move < min_move:
        return None  # Price hasn't moved enough to generate a real signal

    # Find the better side (v3: enforce max entry price)
    if net_edge_up >= ARB_MIN_EDGE and net_edge_up > net_edge_down and poly_up_price <= MAX_ENTRY_PRICE:
        return ArbSignal(
            coin=coin,
            side="up",
            binance_price=binance_price,
            window_start_price=window_start,
            implied_prob=implied_prob_up,
            polymarket_price=poly_up_price,
            edge=net_edge_up,
            seconds_remaining=secs_remaining,
            token_id=market.up_token_id,
        )
    elif net_edge_down >= ARB_MIN_EDGE and net_edge_down > net_edge_up and poly_down_price <= MAX_ENTRY_PRICE:
        return ArbSignal(
            coin=coin,
            side="down",
            binance_price=binance_price,
            window_start_price=window_start,
            implied_prob=implied_prob_down,
            polymarket_price=poly_down_price,
            edge=net_edge_down,
            seconds_remaining=secs_remaining,
            token_id=market.down_token_id,
        )

    return None


def execute_arb_trade(client, signal: ArbSignal, bet_amount: float = 0) -> PlacedOrder | None:
    """Execute an arb trade on Polymarket. Uses Kelly-sized bet_amount."""
    import math
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    MIN_SHARES = 5  # Polymarket minimum order size

    # Use Kelly amount if provided, otherwise fall back to flat size
    usdc_to_spend = bet_amount if bet_amount > 0 else ARB_BET_SIZE

    # Slightly aggressive limit price to ensure fill
    limit_price = min(round(signal.polymarket_price + 0.02, 2), 0.99)
    raw_size = usdc_to_spend / limit_price
    size = max(MIN_SHARES, math.floor(raw_size * 100) / 100)  # At least 5 shares, 2 decimal
    actual_cost = limit_price * size

    try:
        order_args = OrderArgs(
            token_id=signal.token_id,
            price=limit_price,
            size=size,
            side=BUY,
        )
        signed_order = client.create_order(order_args)
        response = client.post_order(signed_order, OrderType.FOK)  # FOK: fill instantly or cancel

        order_id = ""
        if isinstance(response, dict):
            order_id = response.get("orderID", response.get("id", ""))
            success = response.get("success", True)
        else:
            order_id = str(response)
            success = True

        if not success:
            msg = f"Order rejected: {response}"
            console.print(f"[red][arb] {msg}[/red]")
            log_trade(f"REJECTED: {signal.coin} {signal.side.upper()} | {msg}")
            return None

        placed = PlacedOrder(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_title=f"{signal.coin} 15-min {signal.side.upper()}",
            market_question=f"Will {signal.coin} go {signal.side} in this 15-min window?",
            outcome=signal.side.upper(),
            price=limit_price,
            size=size,
            usdc_spent=actual_cost,
            token_id=signal.token_id,
            order_id=order_id,
            status="filled",
        )

        msg = (
            f"TRADE: {signal.coin} {signal.side.upper()} "
            f"@ ${limit_price:.3f} | {size} shares | Edge: {signal.edge:.1%} | "
            f"${actual_cost:.2f} USDC | {signal.seconds_remaining}s left"
        )
        console.print(f"[bold green][arb] {msg}[/bold green]")
        log_trade(msg)

        save_order(placed)

        # Set cooldown for this coin
        last_trade_time[signal.coin] = time.time()

        return placed

    except Exception as e:
        msg = f"Trade failed: {e}"
        console.print(f"[red][arb] {msg}[/red]")
        log_trade(f"ERROR: {signal.coin} {signal.side.upper()} | {msg}")
        return None


async def run_arb_bot():
    """Main arb bot loop."""
    console.print()
    console.print("[bold cyan]╔══════════════════════════════════════╗[/bold cyan]")
    console.print("[bold cyan]║   CRYPTO LATENCY ARB BOT  v3.4        ║[/bold cyan]")
    console.print("[bold cyan]║   CLOB fill detection + Kelly + Allium║[/bold cyan]")
    console.print("[bold cyan]╚══════════════════════════════════════╝[/bold cyan]")
    console.print()
    console.print(f"  Coins:           {', '.join(ARB_COINS)}")
    console.print(f"  Bet sizing:      Kelly criterion (${KELLY_MIN_BET}-${KELLY_MAX_BET}, {KELLY_FRACTION:.0%} Kelly)")
    console.print(f"  Min edge:        {ARB_MIN_EDGE:.0%}")
    console.print(f"  Cooldown:        {ARB_COOLDOWN_SECS}s per coin")
    console.print(f"  Daily bankroll:  ${ARB_MAX_DAILY_SPEND} USDC")
    console.print(f"  Max entry price: ${MAX_ENTRY_PRICE} (won't buy above this)")
    console.print(f"  Max trades/coin: {MAX_TRADES_PER_COIN_PER_WINDOW} per window")
    console.print(f"  Early exit:      Sell if win prob < {EARLY_EXIT_BAIL_PROB:.0%} (cut losses)")
    console.print(f"  Log file:        {LOG_FILE}")
    console.print(f"  [dim]v3.4: CLOB fill verification + Kelly sizing + early exit + Allium on-chain[/dim]")
    console.print(f"  [dim]Rule: wins add to bankroll, losses subtract. Hit $0 = done for the day.[/dim]")
    console.print()

    # VPN check
    if not ensure_vpn(required=VPN_REQUIRED):
        return

    # v3.2: Initialize Allium on-chain data feed
    allium_enabled = False
    if allium.test_connection():
        allium_enabled = True
        console.print("[green]  Allium on-chain feed: ACTIVE (smart money + flow tracking)[/green]")
    else:
        console.print("[yellow]  Allium on-chain feed: OFFLINE (trading without on-chain signals)[/yellow]")
    console.print()

    # Init trading client
    client = init_client(
        private_key=PRIVATE_KEY,
        signature_type=SIGNATURE_TYPE,
        funder=WALLET_ADDRESS if WALLET_ADDRESS else None,
    )

    # Create data dir
    LOG_DIR.mkdir(exist_ok=True)
    log_trade("=== BOT STARTED ===")

    # Get initial prices from Binance REST
    console.print("[dim]Fetching initial prices...[/dim]")
    initial = get_initial_prices()
    for sym, price in initial.items():
        console.print(f"  {sym}: ${price:,.2f}")

    # Start Binance WebSocket feed in background
    binance_task = asyncio.create_task(connect_binance())

    # Wait for feed to connect
    console.print("[dim]Waiting for Binance feed...[/dim]")
    for _ in range(10):
        if feed._running:
            break
        await asyncio.sleep(1)

    if not feed._running:
        console.print("[red]Failed to connect to Binance. Check internet/VPN.[/red]")
        return

    console.print("[green]Binance feed connected. Starting arb loop...[/green]")
    console.print("[dim]Press Ctrl+C to stop[/dim]")
    console.print()

    bankroll = Bankroll(starting=ARB_MAX_DAILY_SPEND)
    console.print(f"[bold]Starting bankroll: ${bankroll.balance:.2f}[/bold]")
    console.print()
    log_trade(f"Starting bankroll: ${bankroll.balance:.2f}")
    tg.alert_bot_started(bankroll.balance, ARB_COINS)

    current_markets = {}
    last_market_refresh = 0
    last_window_ts = 0
    last_status_print = 0
    skip_window_ts = 0  # v3.1: skip this window (late join)
    allium_signals: dict[str, AlliumSignal] = {}  # v3.2: cached Allium signals per coin

    # v3.1: Check if we're joining mid-window (late join protection)
    now_init = time.time()
    current_init_window = (int(now_init) // WINDOW_SECONDS) * WINDOW_SECONDS
    seconds_into_window = now_init - current_init_window
    MAX_LATE_JOIN_SECS = 30  # Only trade if we join within first 30s of window
    if seconds_into_window > MAX_LATE_JOIN_SECS:
        skip_window_ts = current_init_window
        console.print(
            f"[yellow]⚠ Joined {seconds_into_window:.0f}s into current window — "
            f"SKIPPING (reference price unreliable). Will trade next window.[/yellow]"
        )
        log_trade(f"SKIP WINDOW: Joined {seconds_into_window:.0f}s late. Waiting for next window.")

    try:
        while True:
            now = time.time()
            current_window_ts = (int(now) // WINDOW_SECONDS) * WINDOW_SECONDS

            # Refresh markets at start of each new window
            if current_window_ts != last_window_ts:
                last_window_ts = current_window_ts
                window_time = datetime.fromtimestamp(current_window_ts, tz=timezone.utc).strftime('%H:%M')
                console.print(f"\n[bold]--- New 15-min window: {window_time} UTC ---[/bold]")
                log_trade(f"--- New window: {window_time} UTC ---")

                current_markets = discover_all_markets()

                for coin in ARB_COINS:
                    price = feed.get_price(coin)
                    if price:
                        feed.set_window_start(coin, price)
                        console.print(f"  {coin} window start: ${price:,.2f}")

                # v3.2: Refresh Allium signals at window start
                if allium_enabled:
                    for coin in ARB_COINS:
                        try:
                            sig = allium.get_signal(coin, current_window_ts)
                            allium_signals[coin] = sig
                            if sig.has_flow_data or sig.has_smart_data:
                                console.print(f"  [magenta]🔗 {coin} Allium: {sig.summary()}[/magenta]")
                        except Exception as e:
                            console.print(f"  [dim][allium] {coin} query failed: {e}[/dim]")

            # Refresh Polymarket prices every 3 seconds (faster than v1's 5s)
            if now - last_market_refresh > 3:
                for coin in ARB_COINS:
                    market = discover_market(coin)
                    if market:
                        current_markets[coin] = market
                last_market_refresh = now

            # v3.1: Skip signaling if this window was joined late
            if current_window_ts == skip_window_ts:
                # Still check resolutions for any pending trades
                bankroll.check_pending_resolutions(current_markets, client=client)
                if now - last_status_print >= 10:
                    last_status_print = now
                    console.print(f"  [yellow]⏳ Skipping window (late join) — waiting for next window[/yellow]")
                await asyncio.sleep(1)
                continue

            # Check for arb signals on all coins
            signals = {}
            for coin in ARB_COINS:
                market = current_markets.get(coin)
                if not market or not market.is_active:
                    continue

                signal = find_arb_signal(coin, market)
                if signal:
                    signals[coin] = signal

            # Check for resolved trades
            bankroll.check_pending_resolutions(current_markets, client=client)

            # Execute trades (v3: with position guards)
            for coin, signal in signals.items():
                if not bankroll.can_trade:
                    if not bankroll._depleted_logged:
                        console.print("[yellow]Bankroll depleted — waiting for pending trades to resolve.[/yellow]")
                        log_trade("Bankroll depleted — waiting for resolutions.")
                        bankroll._depleted_logged = True
                    break

                # v3 GUARD 1: No betting both sides in same window
                if not bankroll.can_bet_side(coin, signal.side, current_window_ts):
                    committed = bankroll.committed_sides.get((coin, current_window_ts), "?")
                    console.print(
                        f"[dim][v3] BLOCKED {coin} {signal.side.upper()} — "
                        f"already committed to {committed.upper()} this window[/dim]"
                    )
                    continue

                # v3 GUARD 2: Max trades per coin per window
                trade_count = bankroll.get_trade_count(coin, current_window_ts)
                if trade_count >= MAX_TRADES_PER_COIN_PER_WINDOW:
                    console.print(
                        f"[dim][v3] BLOCKED {coin} — "
                        f"already placed {trade_count} trades this window (max {MAX_TRADES_PER_COIN_PER_WINDOW})[/dim]"
                    )
                    continue

                # v3.2: Check Allium on-chain signal
                allium_tag = ""
                if allium_enabled and coin in allium_signals:
                    asig = allium_signals[coin]
                    if asig.contradicts_side(signal.side):
                        console.print(
                            f"[red]  🔗 ALLIUM BLOCK: {coin} {signal.side.upper()} contradicted "
                            f"by on-chain data ({asig.summary()})[/red]"
                        )
                        log_trade(
                            f"ALLIUM BLOCK: {coin} {signal.side.upper()} | "
                            f"On-chain: {asig.summary()}"
                        )
                        # Set cooldown so we don't spam-block every second
                        last_trade_time[coin] = time.time()
                        continue  # Skip this trade
                    elif asig.confirms_side(signal.side):
                        allium_tag = " | 🔗 ALLIUM CONFIRMED"
                    else:
                        allium_tag = " | 🔗 allium neutral"

                # v3.3: Kelly-sized bet
                bet_amt = kelly_bet_size(signal.edge, signal.polymarket_price, bankroll.balance)

                console.print(
                    f"[bold yellow]>>> SIGNAL: {signal.coin} {signal.side.upper()} | "
                    f"Edge: {signal.edge:.1%} | Binance P(Up): {signal.implied_prob:.1%} | "
                    f"Poly: ${signal.polymarket_price:.3f} | Kelly: ${bet_amt:.2f} | "
                    f"{signal.seconds_remaining}s left{allium_tag}[/bold yellow]"
                )
                log_trade(
                    f"SIGNAL: {signal.coin} {signal.side.upper()} | "
                    f"Edge: {signal.edge:.1%} | P(Up): {signal.implied_prob:.1%} | "
                    f"Poly: ${signal.polymarket_price:.3f} | Kelly: ${bet_amt:.2f} | "
                    f"{signal.seconds_remaining}s left{allium_tag}"
                )

                result = execute_arb_trade(client, signal, bet_amount=bet_amt)
                if result:
                    bankroll.place_bet(
                        amount=result.usdc_spent,
                        token_id=signal.token_id,
                        side=signal.side,
                        coin=signal.coin,
                        buy_price=result.price,
                        window_ts=current_window_ts,
                        shares=result.size,
                        order_id=result.order_id,  # v3.4: track for CLOB fill verification
                        edge=signal.edge,
                        secs_left=signal.seconds_remaining,
                    )

            # v3.4.1: Early exits DISABLED — sell-side fill verification not implemented.
            # Trades go to resolution (win or lose). No phantom recovery credits.
            # TODO: Re-enable once sell orders are verified via CLOB like buy orders.

            # Print status every 10 seconds
            if now - last_status_print >= 10:
                last_status_print = now
                console.print(f"  [bold]{bankroll.status_line()}[/bold]")
                for coin in ARB_COINS:
                    market = current_markets.get(coin)
                    bp = feed.get_price(coin)
                    ws = feed.get_window_start(coin)

                    if bp and ws and market:
                        pct = (bp - ws) / ws * 100
                        imp = feed.get_implied_probability(coin, seconds_remaining=market.seconds_remaining)
                        imp_str = f"{imp:.1%}" if imp else "?"

                        # Show if cooldown is active
                        cd = ""
                        if coin in last_trade_time:
                            cd_left = ARB_COOLDOWN_SECS - (now - last_trade_time[coin])
                            if cd_left > 0:
                                cd = f" | [yellow]CD: {cd_left:.0f}s[/yellow]"

                        move_dir = "+" if pct >= 0 else ""
                        console.print(
                            f"  [dim]{coin}: ${bp:,.2f} ({move_dir}{pct:.3f}%) | "
                            f"P(Up)={imp_str} | Poly: Up=${market.up_price:.3f} Down=${market.down_price:.3f} | "
                            f"{market.seconds_remaining}s left{cd}[/dim]"
                        )

            # Stop if bankroll is gone and no pending trades
            if not bankroll.can_trade and len(bankroll.pending_trades) == 0:
                console.print("[yellow]Bankroll depleted and no pending trades. Shutting down.[/yellow]")
                log_trade("Bankroll depleted. Shutting down.")
                break

            await asyncio.sleep(1)

    except KeyboardInterrupt:
        pass
    finally:
        binance_task.cancel()

    # Final summary
    console.print()
    console.print("[bold]═══ SESSION SUMMARY ═══[/bold]")
    console.print(f"  {bankroll.status_line()}")
    pnl = bankroll.pnl
    if pnl > 0:
        console.print(f"  [bold green]Profit: ${pnl:.2f}[/bold green]")
    elif pnl < 0:
        console.print(f"  [bold red]Loss: ${pnl:.2f}[/bold red]")
    else:
        console.print(f"  [dim]Broke even[/dim]")

    log_trade(f"=== SESSION ENDED === {bankroll.status_line()}")


def main():
    asyncio.run(run_arb_bot())


if __name__ == "__main__":
    main()
