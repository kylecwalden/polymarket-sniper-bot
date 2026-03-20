"""
Crypto Maker Strategy v5.5 — 15-min Market Maker + Hedge + Hybrid

Three strategy modes:
- "directional": Bet on predicted direction (UP/DOWN) using Binance + Allium signals
- "hedge": Buy BOTH sides when combined ask < $0.98 (guaranteed profit)
- "hybrid" (default): Try hedge first, fall back to directional

Why hedge works:
- Each 15-min market has UP and DOWN tokens. One always resolves to $1.00.
- If you buy UP at $0.49 and DOWN at $0.48 ($0.97 total), you profit $0.03 guaranteed.
- Zero directional risk. The $313→$438K bot on Polymarket did exactly this.
- Combined with maker rebates (zero fees + daily rebate pool), even small edges stack.

Flow:
1. Connect to Binance WebSocket for real-time BTC/ETH prices
2. At each 15-min window, discover market and fetch orderbooks
3. HYBRID MODE:
   a. Check if UP ask + DOWN ask < HEDGE_SUM_TARGET ($0.98)
   b. If yes: buy both sides → guaranteed profit at resolution
   c. If no: fall back to directional (Binance direction + Allium confirmation)
4. At T-0: cancel unfilled orders, resolve wins/losses
5. Track maker rebate estimates (1% of volume)
"""

import asyncio
import json
import math
import os
import time
import uuid
from dataclasses import dataclass, field
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
from allium_feed import allium

load_dotenv()
console = Console()

# --- Config ---
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
VPN_REQUIRED = os.getenv("PROTON_VPN_REQUIRED", "true").lower() == "true"

# Maker strategy config
MAKER_COINS = os.getenv("MAKER_COINS", "BTC,ETH").split(",")  # SOL dropped — 53% win rate
# Low-liquidity hours (UTC) — skip trading when reversals are common
MAKER_QUIET_HOURS_START = int(os.getenv("MAKER_QUIET_HOURS_START", "0"))   # midnight UTC
MAKER_QUIET_HOURS_END = int(os.getenv("MAKER_QUIET_HOURS_END", "0"))      # 0 = disabled
MAKER_BET_SIZE = float(os.getenv("MAKER_BET_SIZE", "3.0"))        # $3 per trade
MAKER_MAX_BET = float(os.getenv("MAKER_MAX_BET", "5.0"))          # $5 max
MAKER_DAILY_BANKROLL = float(os.getenv("MAKER_DAILY_BANKROLL", "50.0"))
MAKER_DAILY_LOSS_LIMIT = float(os.getenv("MAKER_DAILY_LOSS_LIMIT", "25.0"))
MAKER_MIN_MOVE_PCT = float(os.getenv("MAKER_MIN_MOVE_PCT", "0.10"))   # 0.1% min price move
MAKER_BID_PRICE_LOW = float(os.getenv("MAKER_BID_PRICE_LOW", "0.88"))  # Bid range low
MAKER_BID_PRICE_HIGH = float(os.getenv("MAKER_BID_PRICE_HIGH", "0.95"))  # Bid range high
MAKER_ENTRY_SECONDS = int(os.getenv("MAKER_ENTRY_SECONDS", "480"))    # Enter at T-480s (8 min)
MAKER_LOSS_STREAK_LIMIT = int(os.getenv("MAKER_LOSS_STREAK_LIMIT", "3"))
MAKER_LOSS_COOLDOWN = int(os.getenv("MAKER_LOSS_COOLDOWN", "3600"))    # 1 hour
PAPER_TRADE = os.getenv("PAPER_TRADE", "false").lower() == "true"

# Strategy mode: directional / hedge / hybrid
MAKER_STRATEGY = os.getenv("MAKER_STRATEGY", "hybrid")
HEDGE_SUM_TARGET = float(os.getenv("HEDGE_SUM_TARGET", "0.98"))  # Max combined ask to hedge

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
    pending_orders: list = None
    # Hedge tracking
    hedge_wins: int = 0
    hedge_count: int = 0
    hedge_pnl: float = 0.0
    # Rebate tracking
    estimated_rebates: float = 0.0
    total_volume: float = 0.0

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
        self.total_volume += bet_amount
        self.estimated_rebates += bet_amount * 0.01
        log_trade(f"WIN: +${payout - bet_amount:.2f} | Bankroll: ${self.balance:.2f}")

    def record_loss(self, bet_amount: float):
        self.losses += 1
        self.loss_streak += 1
        self.daily_losses += bet_amount
        self.total_volume += bet_amount
        self.estimated_rebates += bet_amount * 0.01
        if self.loss_streak >= MAKER_LOSS_STREAK_LIMIT:
            self.paused_until = time.time() + MAKER_LOSS_COOLDOWN
            console.print(f"[red]🚨 {self.loss_streak} consecutive losses — pausing {MAKER_LOSS_COOLDOWN // 60} min[/red]")
            tg.send_message(
                f"🚨 MAKER LOSS STREAK: {self.loss_streak}\n"
                f"Pausing {MAKER_LOSS_COOLDOWN // 60} min\n"
                f"Bankroll: ${self.balance:.2f}"
            )
        log_trade(f"LOSS: -${bet_amount:.2f} | Bankroll: ${self.balance:.2f} | Streak: {self.loss_streak}")

    def record_hedge_result(self, total_cost: float, payout: float):
        """Record hedge trade result. Does NOT affect loss streak."""
        profit = payout - total_cost
        self.hedge_count += 1
        self.hedge_pnl += profit
        self.total_volume += total_cost
        self.estimated_rebates += total_cost * 0.01
        if profit >= 0:
            self.hedge_wins += 1
            self.balance += payout
            log_trade(f"HEDGE WIN: +${profit:.2f} | Bankroll: ${self.balance:.2f}")
        else:
            # Hedge loss (rare — only if one leg didn't fill)
            self.balance += payout
            self.daily_losses += abs(profit)
            log_trade(f"HEDGE LOSS: -${abs(profit):.2f} | Bankroll: ${self.balance:.2f}")

    def status_line(self) -> str:
        pnl = self.pnl
        color = "green" if pnl >= 0 else "red"
        parts = [
            f"Bankroll: ${self.balance:.2f}",
            f"P&L: [{color}]${pnl:+.2f}[/{color}]",
            f"W/L: {self.wins}/{self.losses} ({self.win_rate:.0%})",
            f"Pending: {len(self.pending_orders)}",
        ]
        if self.hedge_count > 0:
            parts.append(f"Hedges: {self.hedge_wins}/{self.hedge_count} (${self.hedge_pnl:+.2f})")
        if self.estimated_rebates > 0:
            parts.append(f"Rebates: ~${self.estimated_rebates:.2f}")
        return " | ".join(parts)


# --- Direction Detection ---

def detect_direction(coin: str, window_start_price: float) -> tuple[str | None, float, float]:
    """Detect price direction at near end of 15-min window."""
    current = feed.get_price(coin)
    if current is None or window_start_price <= 0:
        return None, 0.0, 0.0

    pct_move = (current - window_start_price) / window_start_price * 100

    if abs(pct_move) < MAKER_MIN_MOVE_PCT:
        return None, abs(pct_move), current

    direction = "up" if pct_move > 0 else "down"
    return direction, abs(pct_move), current


def calculate_bid_price(confidence_pct: float) -> float:
    """Calculate maker bid price based on confidence (linear interpolation)."""
    t = min(1.0, max(0.0, (confidence_pct - MAKER_MIN_MOVE_PCT) / 0.4))
    bid = MAKER_BID_PRICE_LOW + t * (MAKER_BID_PRICE_HIGH - MAKER_BID_PRICE_LOW)
    return round(bid, 2)


# --- Orderbook Helpers ---

def get_best_ask(client, token_id: str) -> tuple[float, float] | None:
    """Get best (lowest) ask price and available size from CLOB orderbook."""
    try:
        book = client.get_order_book(token_id)
        asks = book.get("asks", []) if isinstance(book, dict) else getattr(book, "asks", [])
        if not asks:
            return None
        parsed = []
        for a in asks:
            if isinstance(a, dict):
                p, s = float(a.get("price", 0)), float(a.get("size", 0))
            else:
                p, s = float(a.price), float(a.size)
            if p > 0:
                parsed.append((p, s))
        if not parsed:
            return None
        best_price = min(p for p, s in parsed)
        best_size = sum(s for p, s in parsed if p == best_price)
        return (round(best_price, 2), best_size)
    except Exception:
        return None


def check_hedge_opportunity(client, market: CryptoMarket) -> dict | None:
    """Check if both sides can be bought for < HEDGE_SUM_TARGET (guaranteed profit)."""
    up_ask = get_best_ask(client, market.up_token_id)
    down_ask = get_best_ask(client, market.down_token_id)

    if up_ask is None or down_ask is None:
        return None

    up_price, up_size = up_ask
    down_price, down_size = down_ask
    total_cost = up_price + down_price

    if total_cost < HEDGE_SUM_TARGET and 0.01 < up_price < 0.99 and 0.01 < down_price < 0.99:
        projected_profit = 1.00 - total_cost
        return {
            "up_ask_price": up_price,
            "up_ask_size": up_size,
            "down_ask_price": down_price,
            "down_ask_size": down_size,
            "total_cost": total_cost,
            "projected_profit": projected_profit,
        }

    return None


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

    if direction == "up":
        token_id = market.up_token_id
        side_label = "UP"
    else:
        token_id = market.down_token_id
        side_label = "DOWN"

    # Ensure maker status: bid must be below best ask
    best = get_best_ask(client, token_id)
    if best and bid_price >= best[0]:
        bid_price = round(best[0] - 0.01, 2)
        if bid_price < 0.01:
            return None

    size = bet_amount / bid_price
    size = max(5, math.floor(size * 100) / 100)
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


async def place_hedge_orders(
    client,
    market: CryptoMarket,
    hedge_info: dict,
    bet_amount: float,
    bankroll: "MakerBankroll",
    is_paper: bool = False,
) -> bool:
    """Place both UP and DOWN orders to lock in guaranteed profit."""
    hedge_id = str(uuid.uuid4())[:8]

    # Bid 1 cent below each ask to ensure maker status
    up_bid = round(hedge_info["up_ask_price"] - 0.01, 2)
    down_bid = round(hedge_info["down_ask_price"] - 0.01, 2)

    if up_bid < 0.01 or down_bid < 0.01:
        console.print(f"  [yellow]{market.coin}: Hedge bid too low — SKIP[/yellow]")
        return False

    up_size = max(5, math.floor((bet_amount / up_bid) * 100) / 100)
    down_size = max(5, math.floor((bet_amount / down_bid) * 100) / 100)
    up_cost = round(up_bid * up_size, 2)
    down_cost = round(down_bid * down_size, 2)
    total_cost = up_cost + down_cost

    if total_cost > bankroll.balance:
        console.print(f"  [yellow]{market.coin}: Not enough bankroll for hedge (${total_cost:.2f} > ${bankroll.balance:.2f})[/yellow]")
        return False

    ptag = "📝 PAPER " if is_paper else ""

    if is_paper:
        # Simulate both orders
        up_order = {
            "order_id": f"paper_hedge_up_{market.coin}_{int(time.time())}",
            "token_id": market.up_token_id,
            "coin": market.coin,
            "direction": "up",
            "bid_price": up_bid,
            "size": up_size,
            "cost": up_cost,
            "placed_at": time.time(),
            "paper": True,
            "trade_type": "hedge",
            "hedge_id": hedge_id,
        }
        down_order = {
            "order_id": f"paper_hedge_down_{market.coin}_{int(time.time())}",
            "token_id": market.down_token_id,
            "coin": market.coin,
            "direction": "down",
            "bid_price": down_bid,
            "size": down_size,
            "cost": down_cost,
            "placed_at": time.time(),
            "paper": True,
            "trade_type": "hedge",
            "hedge_id": hedge_id,
        }
    else:
        # Place real UP order
        up_order_info = await place_maker_order(client, market, "up", up_bid, bet_amount)
        if up_order_info is None:
            console.print(f"  [red]{market.coin}: Hedge UP leg failed — aborting[/red]")
            return False
        up_order_info["trade_type"] = "hedge"
        up_order_info["hedge_id"] = hedge_id
        up_order = up_order_info

        # Place real DOWN order
        down_order_info = await place_maker_order(client, market, "down", down_bid, bet_amount)
        if down_order_info is None:
            # Cancel the UP order — don't leave one-sided exposure
            console.print(f"  [red]{market.coin}: Hedge DOWN leg failed — cancelling UP[/red]")
            await cancel_order(client, up_order["order_id"])
            return False
        down_order_info["trade_type"] = "hedge"
        down_order_info["hedge_id"] = hedge_id
        down_order = down_order_info

    projected = 1.00 - (up_bid + down_bid)
    msg = (
        f"🔄 {ptag}HEDGE: {market.coin} UP ${up_bid:.2f} + DOWN ${down_bid:.2f} "
        f"= ${up_bid + down_bid:.2f} → +${projected:.3f}/share profit"
    )
    console.print(f"  [bold green]{msg}[/bold green]")
    log_trade(msg)
    tg.send_message(
        f"🔄 {ptag}HEDGE\n{market.coin}\n"
        f"UP @ ${up_bid:.2f} ({up_size:.0f} shares)\n"
        f"DOWN @ ${down_bid:.2f} ({down_size:.0f} shares)\n"
        f"Total: ${total_cost:.2f}\n"
        f"Projected profit: +${projected * min(up_size, down_size):.2f}"
    )

    bankroll.balance -= total_cost
    bankroll.pending_orders.append(up_order)
    bankroll.pending_orders.append(down_order)

    return True


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
        return False


# --- Window Tracking ---

@dataclass
class WindowState:
    """Track the state of a 15-min trading window for a coin."""
    coin: str
    window_start_ts: int
    window_end_ts: int
    start_price: float
    order_placed: bool = False
    order_info: dict = None
    hedge_orders: list = None  # [up_order, down_order] for hedge trades
    filled: bool = False

    @property
    def seconds_remaining(self) -> int:
        return self.window_end_ts - int(time.time())

    @property
    def is_active(self) -> bool:
        return self.seconds_remaining > 0

    @property
    def needs_resolution(self) -> bool:
        """Window closed and has unresolved order(s)."""
        has_orders = self.order_info is not None or (self.hedge_orders is not None and len(self.hedge_orders) > 0)
        return self.order_placed and has_orders and self.seconds_remaining <= 0


# --- Main Loop ---

async def run_maker_bot():
    """Main crypto maker bot loop."""
    console.print("[bold magenta]" + "=" * 60 + "[/bold magenta]")
    console.print("[bold magenta]  Polymarket Crypto Maker v5.5[/bold magenta]")
    console.print("[bold magenta]  15-min Market Maker + Hedge + Hybrid[/bold magenta]")
    console.print("[bold magenta]" + "=" * 60 + "[/bold magenta]")
    console.print()

    console.print(f"  Strategy:        {MAKER_STRATEGY.upper()}")
    if MAKER_STRATEGY in ("hedge", "hybrid"):
        console.print(f"  Hedge target:    ${HEDGE_SUM_TARGET:.2f} (buy both sides if sum < this)")
    console.print(f"  Coins:           {', '.join(MAKER_COINS)}")
    console.print(f"  Bet size:        ${MAKER_BET_SIZE:.0f}-${MAKER_MAX_BET:.0f}")
    console.print(f"  Daily bankroll:  ${MAKER_DAILY_BANKROLL:.0f}")
    console.print(f"  Daily loss cap:  ${MAKER_DAILY_LOSS_LIMIT:.0f}")
    console.print(f"  Min move:        {MAKER_MIN_MOVE_PCT:.2f}%")
    console.print(f"  Bid range:       ${MAKER_BID_PRICE_LOW:.2f}-${MAKER_BID_PRICE_HIGH:.2f}")
    console.print(f"  Entry at:        T-{MAKER_ENTRY_SECONDS}s")
    console.print(f"  Loss streak cap: {MAKER_LOSS_STREAK_LIMIT} (then {MAKER_LOSS_COOLDOWN // 60}m cooldown)")
    console.print(f"  Order type:      GTC (maker, zero fees + rebates)")
    if PAPER_TRADE:
        console.print(f"  [bold yellow]📝 MODE:           PAPER TRADE (no real orders)[/bold yellow]")
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

    # Test Allium smart money connection
    allium_ok = allium.test_connection()
    if allium_ok:
        console.print("[green]  Allium: Connected (smart money signals active)[/green]")
    else:
        console.print("[yellow]  Allium: Unavailable (trading without smart money)[/yellow]")
    console.print()

    # Start Binance WebSocket in background
    ws_task = asyncio.create_task(connect_binance())
    await asyncio.sleep(3)

    # Track windows per coin
    windows: dict[str, WindowState] = {}
    prev_windows: dict[str, WindowState] = {}

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

                window = windows.get(coin)

                # ── Resolve previous window ──
                prev = prev_windows.get(coin)
                if prev and prev.needs_resolution:
                    await _resolve_window(client, prev, coin, bankroll)
                    del prev_windows[coin]

                # ── New window detection ──
                if window is None or window.window_start_ts != current_window_start:
                    if window and (window.order_info or window.hedge_orders):
                        prev_windows[coin] = window

                    start_price = feed.get_price(coin) or 0
                    if start_price > 0:
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
                        f"{max(0, window.seconds_remaining)}s remaining ──[/bold]"
                    )

                # ── Entry decision ──
                secs_left = window.seconds_remaining

                if (
                    not window.order_placed
                    and 0 < secs_left <= MAKER_ENTRY_SECONDS
                    and window.start_price > 0
                    and bankroll.can_trade
                ):
                    # Skip quiet hours
                    current_hour = datetime.now(timezone.utc).hour
                    if MAKER_QUIET_HOURS_START < MAKER_QUIET_HOURS_END and MAKER_QUIET_HOURS_START <= current_hour < MAKER_QUIET_HOURS_END:
                        console.print(
                            f"  [yellow]{coin}: Quiet hours ({MAKER_QUIET_HOURS_START}:00-"
                            f"{MAKER_QUIET_HOURS_END}:00 UTC) — SKIP[/yellow]"
                        )
                        window.order_placed = True
                        continue

                    # Discover the market (needed for both hedge and directional)
                    market = discover_market(coin)
                    if market is None or not market.is_active:
                        console.print(f"  [yellow]{coin}: No active 15-min market found[/yellow]")
                        window.order_placed = True
                        continue

                    # ── HEDGE CHECK (hybrid or hedge mode) ──
                    if MAKER_STRATEGY in ("hedge", "hybrid"):
                        hedge = check_hedge_opportunity(client, market)
                        if hedge:
                            console.print(
                                f"  [bold green]{coin}: 🔄 HEDGE OPPORTUNITY! "
                                f"UP ${hedge['up_ask_price']:.2f} + DOWN ${hedge['down_ask_price']:.2f} "
                                f"= ${hedge['total_cost']:.2f} (profit: +${hedge['projected_profit']:.3f}/share)[/bold green]"
                            )
                            success = await place_hedge_orders(
                                client, market, hedge, MAKER_BET_SIZE, bankroll,
                                is_paper=PAPER_TRADE
                            )
                            window.order_placed = True
                            if success:
                                # Store both orders on the window for resolution
                                hedge_orders = [
                                    o for o in bankroll.pending_orders
                                    if o.get("trade_type") == "hedge" and o.get("coin") == coin
                                ][-2:]  # Last 2 added
                                window.hedge_orders = hedge_orders
                            continue
                        else:
                            if MAKER_STRATEGY == "hedge":
                                console.print(f"  [dim]{coin}: No hedge (sum ≥ ${HEDGE_SUM_TARGET:.2f}) — SKIP[/dim]")
                                window.order_placed = True
                                continue
                            # hybrid: fall through to directional

                    # ── DIRECTIONAL (directional mode or hybrid fallback) ──
                    direction, confidence, current_price = detect_direction(
                        coin, window.start_price
                    )

                    if direction is None:
                        console.print(
                            f"  [yellow]{coin}: Ambiguous ({confidence:.3f}% move) — SKIP[/yellow]"
                        )
                        window.order_placed = True
                        continue

                    console.print(
                        f"  [green]{coin}: {direction.upper()} detected ({confidence:.3f}% move) "
                        f"| ${window.start_price:,.2f} → ${current_price:,.2f}[/green]"
                    )

                    # Check Allium smart money confirmation
                    allium_signal = allium.get_signal(coin, current_window_start)
                    allium_tag = ""
                    if allium_signal.has_flow_data or allium_signal.has_smart_data:
                        console.print(f"  [cyan]{coin} Allium: {allium_signal.summary()}[/cyan]")
                        allium_tag = f" | Allium: {allium_signal.summary()}"

                        if allium_signal.contradicts_side(direction):
                            console.print(
                                f"  [yellow]{coin}: Smart money CONTRADICTS "
                                f"{direction.upper()} — SKIP[/yellow]"
                            )
                            window.order_placed = True
                            continue

                        if allium_signal.confirms_side(direction):
                            console.print(
                                f"  [green]{coin}: Smart money CONFIRMS "
                                f"{direction.upper()} — boosting confidence[/green]"
                            )
                            confidence = min(confidence * 1.3, 0.6)
                    else:
                        console.print(f"  [dim]{coin} Allium: No data (trading on Binance alone)[/dim]")

                    bid_price = calculate_bid_price(confidence)

                    if confidence >= 0.3:
                        bet_amount = MAKER_MAX_BET
                    elif confidence >= 0.2:
                        bet_amount = (MAKER_BET_SIZE + MAKER_MAX_BET) / 2
                    else:
                        bet_amount = MAKER_BET_SIZE

                    if PAPER_TRADE:
                        paper_order = {
                            "order_id": f"paper_{coin}_{int(time.time())}",
                            "token_id": market.up_token_id if direction == "up" else market.down_token_id,
                            "direction": direction,
                            "bid_price": bid_price,
                            "size": bet_amount / bid_price,
                            "cost": bet_amount,
                            "coin": coin,
                            "placed_at": time.time(),
                            "paper": True,
                            "trade_type": "directional",
                        }
                        console.print(
                            f"  [bold yellow]📝 PAPER BID: {coin} {direction.upper()} "
                            f"@ ${bid_price:.2f} | {paper_order['size']:.1f} shares | ${bet_amount:.2f}[/bold yellow]"
                        )
                        tg.send_message(
                            f"📝 PAPER BID\n{coin} {direction.upper()} @ ${bid_price:.2f}\n"
                            f"${bet_amount:.2f} ({paper_order['size']:.1f} shares)"
                        )
                        window.order_placed = True
                        window.order_info = paper_order
                        if allium_tag:
                            tg.send_message(f"🧠 Smart Money{allium_tag}")
                        bankroll.balance -= paper_order["cost"]
                        bankroll.pending_orders.append(paper_order)
                    else:
                        order_info = await place_maker_order(
                            client, market, direction, bid_price, bet_amount
                        )
                        window.order_placed = True
                        if order_info:
                            order_info["trade_type"] = "directional"
                            window.order_info = order_info
                            if allium_tag:
                                tg.send_message(f"🧠 Smart Money{allium_tag}")
                            bankroll.balance -= order_info["cost"]
                            bankroll.pending_orders.append(order_info)


            # ── Clean up stale pending orders (older than 20 minutes) ──
            stale_cutoff = time.time() - 1200
            stale_orders = [
                o for o in bankroll.pending_orders
                if o.get("placed_at", time.time()) < stale_cutoff
            ]
            for stale in stale_orders:
                if not stale.get("paper", False):
                    try:
                        await cancel_order(client, stale["order_id"])
                    except Exception:
                        pass
                bankroll.balance += stale.get("cost", 0)
                console.print(f"  [yellow]🧹 Cleaned up stale order: {stale.get('coin', '?')} — refunded ${stale.get('cost', 0):.2f}[/yellow]")
            if stale_orders:
                bankroll.pending_orders = [
                    o for o in bankroll.pending_orders
                    if o not in stale_orders
                ]

            # Print status periodically
            if now % 60 == 0:
                console.print(f"  {bankroll.status_line()}")

            await asyncio.sleep(1)

    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelling open orders...[/yellow]")
        for order in bankroll.pending_orders:
            if order.get("order_id") and not order.get("paper", False):
                await cancel_order(client, order["order_id"])
        console.print("[yellow]Maker bot stopped.[/yellow]")
        console.print(f"\n  {bankroll.status_line()}")

    finally:
        ws_task.cancel()


async def _resolve_window(client, prev: "WindowState", coin: str, bankroll: "MakerBankroll"):
    """Resolve a closed window's orders (directional or hedge)."""
    from tracker import get_current_price

    if prev.hedge_orders and len(prev.hedge_orders) >= 2:
        # ── Hedge resolution ──
        up_order = prev.hedge_orders[0]
        down_order = prev.hedge_orders[1]
        is_paper = up_order.get("paper", False)

        # Check fills
        up_filled = True if is_paper else await check_if_filled(client, up_order["order_id"])
        down_filled = True if is_paper else await check_if_filled(client, down_order["order_id"])

        ptag = "📝 PAPER " if is_paper else ""

        if up_filled and down_filled:
            # Both filled — check which side won
            total_cost = up_order["cost"] + down_order["cost"]
            try:
                up_price = get_current_price(up_order["token_id"])
                down_price = get_current_price(down_order["token_id"])

                if up_price is not None and up_price >= 0.90:
                    # UP won — payout on UP shares
                    payout = up_order["size"] * 1.0
                    profit = payout - total_cost
                    bankroll.record_hedge_result(total_cost, payout)
                    console.print(f"  [bold green]🔄 {ptag}{coin} HEDGE WIN! UP won → +${profit:.2f}[/bold green]")
                    tg.send_message(f"🔄 {ptag}HEDGE WIN\n{coin} UP won\n+${profit:.2f}\nBankroll: ${bankroll.balance:.2f}")
                elif down_price is not None and down_price >= 0.90:
                    # DOWN won — payout on DOWN shares
                    payout = down_order["size"] * 1.0
                    profit = payout - total_cost
                    bankroll.record_hedge_result(total_cost, payout)
                    console.print(f"  [bold green]🔄 {ptag}{coin} HEDGE WIN! DOWN won → +${profit:.2f}[/bold green]")
                    tg.send_message(f"🔄 {ptag}HEDGE WIN\n{coin} DOWN won\n+${profit:.2f}\nBankroll: ${bankroll.balance:.2f}")
                else:
                    # Not resolved yet — refund
                    console.print(f"  [yellow]{coin}: Hedge not resolved yet — refunding[/yellow]")
                    bankroll.balance += total_cost
            except Exception as e:
                console.print(f"  [yellow]{coin}: Hedge resolution failed ({e}) — refunding[/yellow]")
                bankroll.balance += total_cost
        else:
            # One or both not filled — cancel and refund
            for order in [up_order, down_order]:
                if not is_paper:
                    await cancel_order(client, order["order_id"])
                bankroll.balance += order["cost"]
            console.print(f"  [dim]{coin}: Hedge not fully filled — cancelled (refunded)[/dim]")

        # Clean up pending
        hedge_ids = {up_order["order_id"], down_order["order_id"]}
        bankroll.pending_orders = [o for o in bankroll.pending_orders if o.get("order_id") not in hedge_ids]
        prev.hedge_orders = None

    elif prev.order_info:
        # ── Directional resolution (existing logic) ──
        order = prev.order_info
        is_paper = order.get("paper", False)
        filled = True if is_paper else await check_if_filled(client, order["order_id"])

        if filled:
            fill_tag = "📝 PAPER " if is_paper else ""
            console.print(f"  [green]✅ {fill_tag}{coin} maker order FILLED![/green]")
            save_trade_record({
                "type": "maker_fill", "coin": coin,
                "direction": order["direction"], "bid_price": order["bid_price"],
                "size": order["size"], "cost": order["cost"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            try:
                token_price = get_current_price(order["token_id"])
                if token_price is not None and token_price >= 0.90:
                    payout = order["size"] * 1.0
                    bankroll.record_win(order["cost"], payout)
                    ptag = "📝 PAPER " if is_paper else ""
                    console.print(f"  [bold green]🎯 {ptag}{coin} WIN! +${payout - order['cost']:.2f}[/bold green]")
                    tg.send_message(f"🎯 {ptag}MAKER WIN\n{coin} {order['direction'].upper()}\n+${payout - order['cost']:.2f}\nBankroll: ${bankroll.balance:.2f}")
                elif token_price is not None and token_price <= 0.10:
                    bankroll.record_loss(order["cost"])
                    ptag = "📝 PAPER " if is_paper else ""
                    console.print(f"  [red]❌ {ptag}{coin} LOSS: -${order['cost']:.2f}[/red]")
                    tg.send_message(f"❌ {ptag}MAKER LOSS\n{coin} {order['direction'].upper()}\n-${order['cost']:.2f}\nBankroll: ${bankroll.balance:.2f}")
                else:
                    console.print(f"  [yellow]{coin}: Resolution unclear (price: {token_price}) — refunding[/yellow]")
                    bankroll.balance += order["cost"]
            except Exception as e:
                console.print(f"  [yellow]{coin}: Resolution check failed ({e}) — refunding[/yellow]")
                bankroll.balance += order["cost"]
            bankroll.pending_orders = [o for o in bankroll.pending_orders if o.get("order_id") != order["order_id"]]
        else:
            if not is_paper:
                await cancel_order(client, order["order_id"])
            bankroll.balance += order["cost"]
            console.print(f"  [dim]{coin}: Order not filled — cancelled (refunded ${order['cost']:.2f})[/dim]")
            bankroll.pending_orders = [o for o in bankroll.pending_orders if o.get("order_id") != order["order_id"]]

        prev.order_info = None


def main():
    asyncio.run(run_maker_bot())


if __name__ == "__main__":
    main()
