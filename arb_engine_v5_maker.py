"""
Crypto Maker Strategy v5.0 — 15-min Market Maker

The taker arbitrage strategy (v3.5) is dead — Polymarket's Feb 2026 dynamic fees
(up to 3.15%) and removal of the 500ms delay killed it.

NEW STRATEGY: Post GTC maker limit orders on the likely winning side of 15-min
BTC/ETH up/down markets, ~10 seconds before window close.

Why it works:
- ~85% of BTC price direction is determined by T-10 seconds
- Polymarket odds don't fully reflect this (slow to update near close)
- Zero taker fees + maker rebates on GTC orders
- High win rate (target: 80%+) with small per-trade profit ($0.05-0.10/share)

Flow:
1. Connect to Binance WebSocket for real-time BTC/ETH prices
2. At each 15-min window, track price from window start
3. At T-10 seconds before close:
   a. Check Binance price vs window open price
   b. If BTC clearly up (>0.1%): post maker bid for UP at $0.90-0.95
   c. If BTC clearly down (<-0.1%): post maker bid for DOWN at $0.90-0.95
   d. If ambiguous: skip (don't bet on coin flips)
4. At T-0: cancel unfilled orders
5. Wait for resolution, collect $1.00/share on wins

Risk:
- Max 1 order per window per coin
- $5-10 per trade
- Stop after 3 consecutive losses (1h cooldown)
- Daily loss limit: $25
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

from binance_feed import feed, connect_binance, get_initial_prices, SYMBOLS
from crypto_markets import (
    discover_market, CryptoMarket, WINDOW_SECONDS,
    get_current_window_timestamp, get_next_window_timestamp,
)
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

# Maker strategy config
MAKER_COINS = os.getenv("MAKER_COINS", "BTC,ETH").split(",")
MAKER_BET_SIZE = float(os.getenv("MAKER_BET_SIZE", "5.0"))        # $5 per trade
MAKER_MAX_BET = float(os.getenv("MAKER_MAX_BET", "10.0"))         # $10 max
MAKER_DAILY_BANKROLL = float(os.getenv("MAKER_DAILY_BANKROLL", "50.0"))
MAKER_DAILY_LOSS_LIMIT = float(os.getenv("MAKER_DAILY_LOSS_LIMIT", "25.0"))
MAKER_MIN_MOVE_PCT = float(os.getenv("MAKER_MIN_MOVE_PCT", "0.10"))   # 0.1% min price move
MAKER_BID_PRICE_LOW = float(os.getenv("MAKER_BID_PRICE_LOW", "0.88"))  # Bid range low
MAKER_BID_PRICE_HIGH = float(os.getenv("MAKER_BID_PRICE_HIGH", "0.95"))  # Bid range high
MAKER_ENTRY_SECONDS = int(os.getenv("MAKER_ENTRY_SECONDS", "10"))     # Enter at T-10s
MAKER_LOSS_STREAK_LIMIT = int(os.getenv("MAKER_LOSS_STREAK_LIMIT", "3"))
MAKER_LOSS_COOLDOWN = int(os.getenv("MAKER_LOSS_COOLDOWN", "3600"))    # 1 hour

# Logging
LOG_DIR = Path("data")
LOG_FILE = LOG_DIR / "maker_trades.log"
TRADES_FILE = LOG_DIR / "maker_trades.json"


def log_trade(message: str):
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] {message}\n")


def save_trade_record(trade: dict):
    LOG_DIR.mkdir(exist_ok=True)
    trades = []
    if TRADES_FILE.exists():
        try:
            trades = json.loads(TRADES_FILE.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            trades = []
    trades.append(trade)
    TRADES_FILE.write_text(json.dumps(trades, indent=2))


# --- Maker Bankroll ---

@dataclass
class MakerBankroll:
    starting: float
    balance: float = 0.0
    wins: int = 0
    losses: int = 0
    loss_streak: int = 0
    daily_losses: float = 0.0
    paused_until: float = 0.0
    pending_orders: list = None  # list of dicts

    def __post_init__(self):
        self.balance = self.starting
        if self.pending_orders is None:
            self.pending_orders = []

    @property
    def pnl(self) -> float:
        return self.balance - self.starting

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0

    @property
    def can_trade(self) -> bool:
        if self.balance < MAKER_BET_SIZE:
            return False
        if self.daily_losses >= MAKER_DAILY_LOSS_LIMIT:
            console.print(f"[red]Daily loss limit hit (${self.daily_losses:.2f}/${MAKER_DAILY_LOSS_LIMIT:.2f})[/red]")
            return False
        if time.time() < self.paused_until:
            remaining = int(self.paused_until - time.time())
            console.print(f"[yellow]Loss streak cooldown: {remaining}s remaining[/yellow]")
            return False
        return True

    def record_win(self, bet_amount: float, payout: float):
        self.wins += 1
        self.balance += payout
        self.loss_streak = 0
        log_trade(f"WIN: +${payout - bet_amount:.2f} | Bankroll: ${self.balance:.2f}")

    def record_loss(self, bet_amount: float):
        self.losses += 1
        self.loss_streak += 1
        self.daily_losses += bet_amount
        if self.loss_streak >= MAKER_LOSS_STREAK_LIMIT:
            self.paused_until = time.time() + MAKER_LOSS_COOLDOWN
            console.print(f"[red]🚨 {self.loss_streak} consecutive losses — pausing {MAKER_LOSS_COOLDOWN // 60} min[/red]")
            tg.send_message(
                f"🚨 MAKER LOSS STREAK: {self.loss_streak}\n"
                f"Pausing {MAKER_LOSS_COOLDOWN // 60} min\n"
                f"Bankroll: ${self.balance:.2f}"
            )
        log_trade(f"LOSS: -${bet_amount:.2f} | Bankroll: ${self.balance:.2f} | Streak: {self.loss_streak}")

    def status_line(self) -> str:
        pnl = self.pnl
        color = "green" if pnl >= 0 else "red"
        return (
            f"Bankroll: ${self.balance:.2f} | "
            f"P&L: [{color}]${pnl:+.2f}[/{color}] | "
            f"W/L: {self.wins}/{self.losses} ({self.win_rate:.0%}) | "
            f"Pending: {len(self.pending_orders)}"
        )


# --- Direction Detection ---

def detect_direction(coin: str, window_start_price: float) -> tuple[str | None, float, float]:
    """
    Detect price direction at near end of 15-min window.

    Returns: (direction, confidence_pct, current_price)
        direction: "up", "down", or None (ambiguous)
        confidence_pct: absolute % move
        current_price: latest Binance price
    """
    current = feed.get_price(coin)
    if current is None or window_start_price <= 0:
        return None, 0.0, 0.0

    pct_move = (current - window_start_price) / window_start_price * 100

    if abs(pct_move) < MAKER_MIN_MOVE_PCT:
        return None, abs(pct_move), current  # Too close to call

    direction = "up" if pct_move > 0 else "down"
    return direction, abs(pct_move), current


def calculate_bid_price(confidence_pct: float) -> float:
    """
    Calculate maker bid price based on confidence.

    Higher price move = more confident = willing to pay more.
    0.1% move → bid $0.88 (risky)
    0.3% move → bid $0.92 (moderate)
    0.5%+ move → bid $0.95 (high confidence)
    """
    # Linear interpolation: 0.1% → low, 0.5% → high
    t = min(1.0, max(0.0, (confidence_pct - MAKER_MIN_MOVE_PCT) / 0.4))
    bid = MAKER_BID_PRICE_LOW + t * (MAKER_BID_PRICE_HIGH - MAKER_BID_PRICE_LOW)
    # Round to 2 decimal places (Polymarket precision)
    return round(bid, 2)


# --- Order Execution ---

async def place_maker_order(
    client,
    market: CryptoMarket,
    direction: str,
    bid_price: float,
    bet_amount: float,
) -> dict | None:
    """Place a GTC maker limit order on the predicted winning side."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    # Pick token based on direction
    if direction == "up":
        token_id = market.up_token_id
        side_label = "UP"
    else:
        token_id = market.down_token_id
        side_label = "DOWN"

    size = bet_amount / bid_price
    size = max(5, math.floor(size * 100) / 100)  # Min 5 shares, 2 decimal places
    actual_cost = bid_price * size

    try:
        order_args = OrderArgs(
            token_id=token_id,
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
            console.print(f"[red]  Order rejected: {response}[/red]")
            return None

        order_info = {
            "order_id": order_id,
            "token_id": token_id,
            "coin": market.coin,
            "direction": direction,
            "bid_price": bid_price,
            "size": size,
            "cost": actual_cost,
            "slug": market.slug,
            "placed_at": time.time(),
            "window_end": market.end_timestamp,
        }

        msg = (
            f"📋 MAKER BID: {market.coin} {side_label} @ ${bid_price:.2f} | "
            f"{size:.1f} shares | ${actual_cost:.2f}"
        )
        console.print(f"[cyan]  {msg}[/cyan]")
        log_trade(msg)
        tg.send_message(f"📋 MAKER BID\n{market.coin} {side_label}\n${bid_price:.2f} × {size:.0f} shares\n${actual_cost:.2f} USDC")

        return order_info

    except Exception as e:
        console.print(f"[red]  Maker order failed: {e}[/red]")
        log_trade(f"ERROR: Maker order {market.coin} {direction}: {e}")
        return None


async def cancel_order(client, order_id: str) -> bool:
    """Cancel a specific order. Returns True if cancelled."""
    try:
        client.cancel(order_id)
        return True
    except Exception:
        try:
            client.cancel_orders([order_id])
            return True
        except Exception as e:
            console.print(f"[yellow]  Cancel failed: {e}[/yellow]")
            return False


async def check_if_filled(client, order_id: str) -> bool:
    """Check if an order has been filled (no longer on the book)."""
    try:
        live_orders = client.get_orders()
        live_ids = {o.get("id", o.get("orderID", "")) for o in live_orders}
        return order_id not in live_ids
    except Exception:
        return False  # Assume not filled if we can't check


# --- Window Tracking ---

@dataclass
class WindowState:
    """Track the state of a 15-min trading window for a coin."""
    coin: str
    window_start_ts: int       # Unix timestamp of window start
    window_end_ts: int         # Unix timestamp of window end
    start_price: float         # Binance price at window start
    order_placed: bool = False
    order_info: dict = None
    filled: bool = False

    @property
    def seconds_remaining(self) -> int:
        return max(0, self.window_end_ts - int(time.time()))

    @property
    def is_active(self) -> bool:
        return self.seconds_remaining > 0


# --- Main Loop ---

async def run_maker_bot():
    """Main crypto maker bot loop."""
    console.print("[bold magenta]" + "=" * 60 + "[/bold magenta]")
    console.print("[bold magenta]  Polymarket Crypto Maker v5.0[/bold magenta]")
    console.print("[bold magenta]  15-min Market Maker Strategy[/bold magenta]")
    console.print("[bold magenta]" + "=" * 60 + "[/bold magenta]")
    console.print()

    console.print(f"  Coins:           {', '.join(MAKER_COINS)}")
    console.print(f"  Bet size:        ${MAKER_BET_SIZE:.0f}-${MAKER_MAX_BET:.0f}")
    console.print(f"  Daily bankroll:  ${MAKER_DAILY_BANKROLL:.0f}")
    console.print(f"  Daily loss cap:  ${MAKER_DAILY_LOSS_LIMIT:.0f}")
    console.print(f"  Min move:        {MAKER_MIN_MOVE_PCT:.2f}%")
    console.print(f"  Bid range:       ${MAKER_BID_PRICE_LOW:.2f}-${MAKER_BID_PRICE_HIGH:.2f}")
    console.print(f"  Entry at:        T-{MAKER_ENTRY_SECONDS}s")
    console.print(f"  Loss streak cap: {MAKER_LOSS_STREAK_LIMIT} (then {MAKER_LOSS_COOLDOWN // 60}m cooldown)")
    console.print(f"  Order type:      GTC (maker, zero fees + rebates)")
    console.print()

    # VPN check
    console.print("[vpn] Checking VPN connection...")
    if VPN_REQUIRED and not ensure_vpn():
        console.print("[red]VPN required but not connected. Exiting.[/red]")
        return
    console.print()

    # Init CLOB client
    client = init_client(PRIVATE_KEY, SIGNATURE_TYPE, WALLET_ADDRESS)

    # Init bankroll
    bankroll = MakerBankroll(starting=MAKER_DAILY_BANKROLL)

    # Fetch initial Binance prices
    console.print("[binance] Fetching initial prices...")
    prices = get_initial_prices()
    for sym, price in prices.items():
        console.print(f"  {sym}: ${price:,.2f}")
    console.print()

    # Start Binance WebSocket in background
    ws_task = asyncio.create_task(connect_binance())

    # Wait a moment for WebSocket to connect
    await asyncio.sleep(3)

    # Track windows per coin
    windows: dict[str, WindowState] = {}

    console.print("[green]Maker bot started. Monitoring 15-min windows...[/green]")
    console.print()

    try:
        while True:
            now = int(time.time())

            for coin in MAKER_COINS:
                if coin not in SYMBOLS:
                    continue

                current_window_start = get_current_window_timestamp()
                current_window_end = current_window_start + WINDOW_SECONDS

                # Check if we need a new window state
                window = windows.get(coin)
                if window is None or window.window_start_ts != current_window_start:
                    # New window — record start price
                    start_price = feed.get_price(coin) or 0
                    if start_price > 0:
                        # Also set it in the feed for momentum tracking
                        feed.set_window_start(coin, start_price)

                    window = WindowState(
                        coin=coin,
                        window_start_ts=current_window_start,
                        window_end_ts=current_window_end,
                        start_price=start_price,
                    )
                    windows[coin] = window

                    ts = datetime.fromtimestamp(current_window_start, tz=timezone.utc).strftime("%H:%M:%S")
                    console.print(
                        f"[bold]── {coin} New window: {ts} UTC | "
                        f"Start: ${start_price:,.2f} | "
                        f"{window.seconds_remaining}s remaining ──[/bold]"
                    )

                # ── Check if it's time to enter (T-10 seconds) ──
                secs_left = window.seconds_remaining

                if (
                    not window.order_placed
                    and 0 < secs_left <= MAKER_ENTRY_SECONDS
                    and window.start_price > 0
                    and bankroll.can_trade
                ):
                    # Time to make our move!
                    direction, confidence, current_price = detect_direction(
                        coin, window.start_price
                    )

                    if direction is None:
                        console.print(
                            f"  [yellow]{coin}: Ambiguous ({confidence:.3f}% move) — SKIP[/yellow]"
                        )
                        window.order_placed = True  # Don't retry this window
                        continue

                    console.print(
                        f"  [green]{coin}: {direction.upper()} detected ({confidence:.3f}% move) "
                        f"| ${window.start_price:,.2f} → ${current_price:,.2f}[/green]"
                    )

                    # Calculate bid price based on confidence
                    bid_price = calculate_bid_price(confidence)

                    # Scale bet size with confidence
                    if confidence >= 0.3:
                        bet_amount = MAKER_MAX_BET
                    elif confidence >= 0.2:
                        bet_amount = (MAKER_BET_SIZE + MAKER_MAX_BET) / 2
                    else:
                        bet_amount = MAKER_BET_SIZE

                    # Discover the market
                    market = discover_market(coin)
                    if market is None or not market.is_active:
                        console.print(f"  [yellow]{coin}: No active 15-min market found[/yellow]")
                        window.order_placed = True
                        continue

                    # Place the maker order
                    order_info = await place_maker_order(
                        client, market, direction, bid_price, bet_amount
                    )

                    window.order_placed = True
                    if order_info:
                        window.order_info = order_info
                        bankroll.balance -= order_info["cost"]
                        bankroll.pending_orders.append(order_info)

                # ── Check for fills and resolution after window closes ──
                if (
                    window.order_placed
                    and window.order_info
                    and not window.filled
                    and secs_left <= 0
                ):
                    order = window.order_info

                    # Check if the order filled
                    filled = await check_if_filled(client, order["order_id"])

                    if filled:
                        window.filled = True
                        console.print(f"  [green]✅ {coin} maker order FILLED![/green]")

                        # Wait a bit for resolution (15-min markets resolve quickly)
                        # Resolution is usually within 1-2 minutes after window close
                        # We'll check on the next loop iteration

                        save_trade_record({
                            "type": "maker_fill",
                            "coin": coin,
                            "direction": order["direction"],
                            "bid_price": order["bid_price"],
                            "size": order["size"],
                            "cost": order["cost"],
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                    else:
                        # Order didn't fill — cancel it and get money back
                        cancelled = await cancel_order(client, order["order_id"])
                        if cancelled:
                            bankroll.balance += order["cost"]  # Refund
                            console.print(f"  [dim]{coin}: Order not filled — cancelled (refunded ${order['cost']:.2f})[/dim]")
                        window.filled = True  # Mark as handled

                        # Remove from pending
                        bankroll.pending_orders = [
                            o for o in bankroll.pending_orders
                            if o.get("order_id") != order["order_id"]
                        ]

                # ── Check resolution of filled orders (a few minutes after window close) ──
                if (
                    window.filled
                    and window.order_info
                    and secs_left <= -120  # Check 2 minutes after close
                ):
                    order = window.order_info
                    # Check resolution by looking at token price
                    try:
                        from tracker import get_current_price
                        token_price = get_current_price(order["token_id"])

                        if token_price is not None:
                            if token_price >= 0.90:
                                # Won!
                                payout = order["size"] * 1.0
                                bankroll.record_win(order["cost"], payout)
                                console.print(
                                    f"  [bold green]🎯 {coin} WIN! "
                                    f"Bet ${order['cost']:.2f} → Payout ${payout:.2f} "
                                    f"(+${payout - order['cost']:.2f})[/bold green]"
                                )
                                tg.send_message(
                                    f"🎯 MAKER WIN\n{coin} {order['direction'].upper()}\n"
                                    f"+${payout - order['cost']:.2f}\n"
                                    f"Bankroll: ${bankroll.balance:.2f}"
                                )
                                # Remove from pending
                                bankroll.pending_orders = [
                                    o for o in bankroll.pending_orders
                                    if o.get("order_id") != order["order_id"]
                                ]
                                window.order_info = None  # Don't check again

                            elif token_price <= 0.10:
                                # Lost
                                bankroll.record_loss(order["cost"])
                                console.print(
                                    f"  [red]❌ {coin} LOSS: -${order['cost']:.2f}[/red]"
                                )
                                tg.send_message(
                                    f"❌ MAKER LOSS\n{coin} {order['direction'].upper()}\n"
                                    f"-${order['cost']:.2f}\n"
                                    f"Bankroll: ${bankroll.balance:.2f}"
                                )
                                bankroll.pending_orders = [
                                    o for o in bankroll.pending_orders
                                    if o.get("order_id") != order["order_id"]
                                ]
                                window.order_info = None

                    except Exception as e:
                        pass  # Resolution not ready yet, will check next loop

            # Print status periodically
            if now % 60 == 0:
                console.print(f"  {bankroll.status_line()}")

            # Fast poll — check every second near window boundaries
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        # Cancel all open orders
        console.print("\n[yellow]Cancelling open orders...[/yellow]")
        for order in bankroll.pending_orders:
            if order.get("order_id"):
                await cancel_order(client, order["order_id"])
        console.print("[yellow]Maker bot stopped.[/yellow]")
        console.print(f"\n  {bankroll.status_line()}")

    finally:
        ws_task.cancel()


def main():
    asyncio.run(run_maker_bot())


if __name__ == "__main__":
    main()
