"""
Polymarket V6 Multi-Strategy Engine
====================================
Three concurrent strategies optimized for maximum P&L:

1. HEDGE  — Buy both UP+DOWN when asks sum < $0.98 (guaranteed profit)
2. MM     — Market making: capture $0.02 spread + maker rebates
3. LATE   — Late entry at T-120s with whale confirmation ($0.93-0.95 bids)

Paper trade mode simulates all fills from real orderbook data.
"""

import asyncio
import json
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from rich.console import Console

load_dotenv()
console = Console()

# ══════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════

V6_COINS = os.getenv("V6_COINS", "BTC,ETH,SOL,XRP").upper().split(",")
V6_TIMEFRAMES = os.getenv("V6_TIMEFRAMES", "5m,15m").split(",")
V6_BANKROLL = float(os.getenv("V6_BANKROLL", "100.0"))
V6_DAILY_LOSS_LIMIT = float(os.getenv("V6_DAILY_LOSS_LIMIT", "40.0"))
PAPER_TRADE = os.getenv("PAPER_TRADE", "false").lower() == "true"

# Strategy toggles
V6_HEDGE_ENABLED = os.getenv("V6_HEDGE_ENABLED", "true").lower() == "true"
V6_MM_ENABLED = os.getenv("V6_MM_ENABLED", "true").lower() == "true"
V6_LATE_ENABLED = os.getenv("V6_LATE_ENABLED", "true").lower() == "true"

# Bankroll allocation
V6_HEDGE_ALLOC = float(os.getenv("V6_HEDGE_ALLOC", "0.50"))
V6_MM_ALLOC = float(os.getenv("V6_MM_ALLOC", "0.30"))
V6_LATE_ALLOC = float(os.getenv("V6_LATE_ALLOC", "0.20"))

# Hedge config
V6_HEDGE_THRESHOLD = float(os.getenv("V6_HEDGE_THRESHOLD", "0.98"))
V6_HEDGE_BET_SIZE = float(os.getenv("V6_HEDGE_BET_SIZE", "5.0"))
V6_HEDGE_POLL_INTERVAL = float(os.getenv("V6_HEDGE_POLL_INTERVAL", "1.5"))

# Market making config
V6_MM_SPREAD = float(os.getenv("V6_MM_SPREAD", "0.02"))
V6_MM_SIZE = float(os.getenv("V6_MM_SIZE", "5.0"))
V6_MM_MAX_INVENTORY = float(os.getenv("V6_MM_MAX_INVENTORY", "20.0"))
V6_MM_REQUOTE_THRESHOLD = float(os.getenv("V6_MM_REQUOTE_THRESHOLD", "0.02"))

# Position limits — prevent over-exposure
V6_MAX_TOTAL_DEPLOYED = float(os.getenv("V6_MAX_TOTAL_DEPLOYED", "300.0"))  # Hard cap across all positions
V6_MAX_PER_COIN = float(os.getenv("V6_MAX_PER_COIN", "50.0"))              # Max deployed per coin

# Late entry config
V6_LATE_ENTRY_SECONDS = int(os.getenv("V6_LATE_ENTRY_SECONDS", "120"))
V6_LATE_MIN_MOVE_PCT = float(os.getenv("V6_LATE_MIN_MOVE_PCT", "0.30"))
V6_LATE_BID_LOW = float(os.getenv("V6_LATE_BID_LOW", "0.93"))
V6_LATE_BID_HIGH = float(os.getenv("V6_LATE_BID_HIGH", "0.95"))
V6_LATE_BET_SIZE = float(os.getenv("V6_LATE_BET_SIZE", "5.0"))
V6_LATE_REQUIRE_ALLIUM = os.getenv("V6_LATE_REQUIRE_ALLIUM", "true").lower() == "true"

# ══════════════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════════════

@dataclass
class StrategyPnL:
    name: str
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_volume: float = 0.0
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
    hedge_pool: float = 0.0
    mm_pool: float = 0.0
    late_pool: float = 0.0
    daily_losses: float = 0.0
    hedge_pnl: StrategyPnL = field(default_factory=lambda: StrategyPnL("hedge"))
    mm_pnl: StrategyPnL = field(default_factory=lambda: StrategyPnL("mm"))
    late_pnl: StrategyPnL = field(default_factory=lambda: StrategyPnL("late"))
    estimated_rebates: float = 0.0

    def __post_init__(self):
        self.hedge_pool = self.starting * V6_HEDGE_ALLOC
        self.mm_pool = self.starting * V6_MM_ALLOC
        self.late_pool = self.starting * V6_LATE_ALLOC

    @property
    def total_balance(self) -> float:
        return self.hedge_pool + self.mm_pool + self.late_pool

    @property
    def total_pnl(self) -> float:
        return self.total_balance - self.starting

    @property
    def can_trade(self) -> bool:
        return self.daily_losses < V6_DAILY_LOSS_LIMIT

    def status_line(self) -> str:
        return (
            f"Bankroll: ${self.total_balance:.2f} | P&L: ${self.total_pnl:+.2f} | "
            f"Hedge: {self.hedge_pnl.summary()} | "
            f"MM: {self.mm_pnl.summary()} | "
            f"Late: {self.late_pnl.summary()} | "
            f"Rebates: ~${self.estimated_rebates:.2f}"
        )


@dataclass
class WindowState:
    coin: str
    window_start_ts: int
    start_price: float
    # Strategy flags per window
    late_entry_done: bool = False
    hedge_trades: list = field(default_factory=list)
    mm_quotes: list = field(default_factory=list)
    late_trades: list = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════
# Orderbook Helpers
# ══════════════════════════════════════════════════════════════════════

def _parse_book_side(entries) -> list[tuple[float, float]]:
    """Parse bids or asks into [(price, size), ...]."""
    parsed = []
    for entry in entries:
        if isinstance(entry, dict):
            p = float(entry.get("price", 0))
            s = float(entry.get("size", 0))
        else:
            p = float(getattr(entry, "price", 0))
            s = float(getattr(entry, "size", 0))
        if p > 0 and s > 0:
            parsed.append((p, s))
    return parsed


def get_best_ask(client, token_id: str) -> Optional[tuple[float, float]]:
    """Get (price, size) of best ask — WebSocket first, REST fallback."""
    # Try WebSocket (sub-100ms)
    try:
        from polymarket_ws import orderbook_feed
        ws_result = orderbook_feed.get_best_ask(token_id)
        if ws_result:
            return ws_result
    except Exception:
        pass

    # Fallback to REST polling
    try:
        book = client.get_order_book(token_id)
        if not isinstance(book, (dict,)) and not hasattr(book, 'asks'):
            return None
        asks_raw = book.get("asks", []) if isinstance(book, dict) else getattr(book, "asks", [])
        asks = _parse_book_side(asks_raw)
        if not asks:
            return None
        best_price = min(p for p, s in asks)
        best_size = sum(s for p, s in asks if p == best_price)
        return (round(best_price, 2), best_size)
    except Exception:
        return None


def get_best_bid(client, token_id: str) -> Optional[tuple[float, float]]:
    """Get (price, size) of best bid — WebSocket first, REST fallback."""
    try:
        from polymarket_ws import orderbook_feed
        ws_result = orderbook_feed.get_best_bid(token_id)
        if ws_result:
            return ws_result
    except Exception:
        pass

    try:
        book = client.get_order_book(token_id)
        if not isinstance(book, (dict,)) and not hasattr(book, 'bids'):
            return None
        bids_raw = book.get("bids", []) if isinstance(book, dict) else getattr(book, "bids", [])
        bids = _parse_book_side(bids_raw)
        if not bids:
            return None
        best_price = max(p for p, s in bids)
        best_size = sum(s for p, s in bids if p == best_price)
        return (round(best_price, 2), best_size)
    except Exception:
        return None


def get_midpoint(client, token_id: str) -> Optional[float]:
    """Get midpoint price — WebSocket first, REST fallback."""
    try:
        from polymarket_ws import orderbook_feed
        ws_mid = orderbook_feed.get_midpoint(token_id)
        if ws_mid:
            return ws_mid
    except Exception:
        pass

    bid = get_best_bid(client, token_id)
    ask = get_best_ask(client, token_id)
    if bid and ask:
        return round((bid[0] + ask[0]) / 2, 3)
    return None


# ══════════════════════════════════════════════════════════════════════
# Strategy 1: Dump-and-Hedge Arbitrage
# ══════════════════════════════════════════════════════════════════════

def check_hedge_opportunity(client, market) -> Optional[dict]:
    """Check if UP ask + DOWN ask < threshold."""
    up_ask = get_best_ask(client, market.up_token_id)
    down_ask = get_best_ask(client, market.down_token_id)

    if not up_ask or not down_ask:
        return None

    total = up_ask[0] + down_ask[0]
    if total < V6_HEDGE_THRESHOLD:
        return {
            "up_ask_price": up_ask[0],
            "up_ask_size": up_ask[1],
            "down_ask_price": down_ask[0],
            "down_ask_size": down_ask[1],
            "total_cost": total,
            "projected_profit": 1.00 - total,
        }
    return None


async def execute_hedge(client, market, hedge_info, bankroll: Bankroll, is_paper: bool):
    """Execute a hedge trade — buy both sides."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    hedge_id = str(uuid.uuid4())[:8]
    up_price = hedge_info["up_ask_price"]
    down_price = hedge_info["down_ask_price"]
    bet = min(V6_HEDGE_BET_SIZE, bankroll.hedge_pool / 2)
    if bet < 2:
        return False

    up_size = max(5, math.floor((bet / up_price) * 100) / 100)
    down_size = max(5, math.floor((bet / down_price) * 100) / 100)
    # Use the smaller size so both legs match
    size = min(up_size, down_size)
    up_cost = round(up_price * size, 2)
    down_cost = round(down_price * size, 2)
    total_cost = up_cost + down_cost

    ptag = "📝 " if is_paper else ""

    if is_paper:
        # Paper: simulate both fills
        profit = round(size * 1.00 - total_cost, 2)
        bankroll.hedge_pool += profit
        bankroll.hedge_pnl.wins += 1
        bankroll.hedge_pnl.total_pnl += profit
        bankroll.hedge_pnl.trades += 1
        bankroll.hedge_pnl.total_volume += total_cost
        bankroll.estimated_rebates += total_cost * 0.01

        msg = (
            f"🔄 {ptag}HEDGE: {market.coin}\n"
            f"UP @ ${up_price:.2f} + DOWN @ ${down_price:.2f} = ${up_price + down_price:.2f}\n"
            f"{size:.0f} shares × ${hedge_info['projected_profit']:.3f} = +${profit:.2f}"
        )
        console.print(f"  [bold green]{msg}[/bold green]")
        _send_tg(msg)
        return True
    else:
        # Real trade: place both legs
        try:
            up_args = OrderArgs(token_id=market.up_token_id, price=up_price, size=size, side=BUY)
            up_signed = client.create_order(up_args)
            up_resp = client.post_order(up_signed, OrderType.GTC)
            up_order_id = _extract_order_id(up_resp)
            if not up_order_id:
                return False

            down_args = OrderArgs(token_id=market.down_token_id, price=down_price, size=size, side=BUY)
            down_signed = client.create_order(down_args)
            down_resp = client.post_order(down_signed, OrderType.GTC)
            down_order_id = _extract_order_id(down_resp)
            if not down_order_id:
                # Cancel first leg
                try:
                    client.cancel(up_order_id)
                except Exception:
                    pass
                return False

            bankroll.hedge_pool -= total_cost
            msg = (
                f"🔄 HEDGE: {market.coin}\n"
                f"UP @ ${up_price:.2f} + DOWN @ ${down_price:.2f}\n"
                f"Projected: +${hedge_info['projected_profit'] * size:.2f}"
            )
            console.print(f"  [bold green]{msg}[/bold green]")
            _send_tg(msg)
            return True
        except Exception as e:
            console.print(f"  [red]Hedge order failed: {e}[/red]")
            return False


# ══════════════════════════════════════════════════════════════════════
# Strategy 2: Market Making
# ══════════════════════════════════════════════════════════════════════

@dataclass
class MMState:
    coin: str
    last_midpoint: float = 0.0
    buy_order_id: Optional[str] = None
    sell_order_id: Optional[str] = None
    buy_price: float = 0.0
    sell_price: float = 0.0
    buy_filled: bool = False
    sell_filled: bool = False
    inventory_up: float = 0.0  # Net UP token shares held
    inventory_down: float = 0.0  # Net DOWN token shares held

    @property
    def net_inventory(self) -> float:
        """Positive = long UP, negative = long DOWN."""
        return self.inventory_up - self.inventory_down


async def manage_mm_quotes(client, market, mm_state: MMState, bankroll: Bankroll, is_paper: bool):
    """Manage market making quotes for a coin."""
    mid = get_midpoint(client, market.up_token_id)
    if mid is None or mid <= 0.05 or mid >= 0.95:
        return  # Skip extreme prices

    ptag = "📝 " if is_paper else ""
    half_spread = V6_MM_SPREAD / 2

    # Check if midpoint moved enough to requote
    if mm_state.last_midpoint > 0 and abs(mid - mm_state.last_midpoint) < V6_MM_REQUOTE_THRESHOLD:
        # Check if existing quotes have filled
        if mm_state.buy_price > 0:
            if is_paper:
                # Paper: simulate fills based on price movement
                if mid <= mm_state.buy_price and not mm_state.buy_filled:
                    mm_state.buy_filled = True
                    mm_state.inventory_up += V6_MM_SIZE / mm_state.buy_price
                    console.print(f"  [cyan]📊 {ptag}MM BUY FILL: {market.coin} UP @ ${mm_state.buy_price:.2f}[/cyan]")
                    _send_tg(f"📊 {ptag}MM BUY FILL: {market.coin} UP @ ${mm_state.buy_price:.2f}")

                if mid >= mm_state.sell_price and not mm_state.sell_filled:
                    mm_state.sell_filled = True
                    mm_state.inventory_down += V6_MM_SIZE / (1.0 - mm_state.sell_price)
                    console.print(f"  [cyan]📊 {ptag}MM SELL FILL: {market.coin} UP @ ${mm_state.sell_price:.2f}[/cyan]")
                    _send_tg(f"📊 {ptag}MM SELL FILL: {market.coin} UP @ ${mm_state.sell_price:.2f}")
            else:
                # Real: check order fill status via API
                try:
                    if mm_state.buy_order_id and not mm_state.buy_filled:
                        order = client.get_order(mm_state.buy_order_id)
                        if isinstance(order, dict):
                            matched = float(order.get("size_matched", 0))
                            status = order.get("status", "").upper()
                        else:
                            matched = float(getattr(order, "size_matched", 0))
                            status = getattr(order, "status", "").upper()
                        if matched > 0 or status == "MATCHED":
                            mm_state.buy_filled = True
                            mm_state.inventory_up += matched if matched > 0 else V6_MM_SIZE / mm_state.buy_price
                            cost = round(mm_state.buy_price * (matched if matched > 0 else V6_MM_SIZE / mm_state.buy_price), 2)
                            console.print(f"  [cyan]📊 MM BUY FILL: {market.coin} UP @ ${mm_state.buy_price:.2f} ({matched:.1f} shares)[/cyan]")
                            _send_tg(f"📊 MM BUY FILL: {market.coin} UP @ ${mm_state.buy_price:.2f} ({matched:.1f} shares, ${cost:.2f})")

                    if mm_state.sell_order_id and not mm_state.sell_filled:
                        order = client.get_order(mm_state.sell_order_id)
                        if isinstance(order, dict):
                            matched = float(order.get("size_matched", 0))
                            status = order.get("status", "").upper()
                        else:
                            matched = float(getattr(order, "size_matched", 0))
                            status = getattr(order, "status", "").upper()
                        if matched > 0 or status == "MATCHED":
                            mm_state.sell_filled = True
                            down_price = round(1.0 - mm_state.sell_price, 2)
                            mm_state.inventory_down += matched if matched > 0 else V6_MM_SIZE / down_price
                            cost = round(down_price * (matched if matched > 0 else V6_MM_SIZE / down_price), 2)
                            console.print(f"  [cyan]📊 MM SELL FILL: {market.coin} DOWN @ ${down_price:.2f} ({matched:.1f} shares)[/cyan]")
                            _send_tg(f"📊 MM SELL FILL: {market.coin} DOWN @ ${down_price:.2f} ({matched:.1f} shares, ${cost:.2f})")
                except Exception as e:
                    console.print(f"  [dim]MM fill check error: {e}[/dim]")

            # Round-trip complete?
            if mm_state.buy_filled and mm_state.sell_filled:
                spread_profit = round(V6_MM_SPREAD * min(
                    mm_state.inventory_up,
                    mm_state.inventory_down,
                ), 2)
                bankroll.mm_pool += spread_profit
                bankroll.mm_pnl.wins += 1
                bankroll.mm_pnl.total_pnl += spread_profit
                bankroll.mm_pnl.trades += 1
                bankroll.mm_pnl.total_volume += V6_MM_SIZE * 2
                bankroll.estimated_rebates += V6_MM_SIZE * 2 * 0.01

                msg = f"📊 {ptag}MM ROUND-TRIP: {market.coin} +${spread_profit:.2f} spread"
                console.print(f"  [bold cyan]{msg}[/bold cyan]")
                _send_tg(msg)

                # Reset for next quote
                mm_state.buy_filled = False
                mm_state.sell_filled = False
                mm_state.buy_order_id = None
                mm_state.sell_order_id = None
                mm_state.buy_price = 0
                mm_state.sell_price = 0
                mm_state.inventory_up = 0
                mm_state.inventory_down = 0

            # One-sided fill protection (same as requote path)
            elif mm_state.buy_filled and not mm_state.sell_filled and not is_paper:
                # Wait one more cycle, then unwind
                if not hasattr(mm_state, '_one_sided_since'):
                    mm_state._one_sided_since = time.time()
                elif time.time() - mm_state._one_sided_since > 5:  # 5 sec grace period
                    try:
                        from py_clob_client.order_builder.constants import SELL as SELL_SIDE
                        sell_price_back = round(mm_state.buy_price - 0.01, 2)
                        if sell_price_back > 0.01 and mm_state.inventory_up > 0:
                            args = OrderArgs(token_id=market.up_token_id, price=sell_price_back, size=mm_state.inventory_up, side=SELL_SIDE)
                            signed = client.create_order(args)
                            client.post_order(signed, OrderType.GTC)
                            console.print(f"  [yellow]📊 MM UNWIND: {market.coin} sold UP back[/yellow]")
                    except Exception:
                        pass
                    mm_state.buy_filled = False
                    mm_state.inventory_up = 0
                    mm_state._one_sided_since = None

            elif mm_state.sell_filled and not mm_state.buy_filled and not is_paper:
                if not hasattr(mm_state, '_one_sided_since'):
                    mm_state._one_sided_since = time.time()
                elif time.time() - mm_state._one_sided_since > 5:
                    try:
                        from py_clob_client.order_builder.constants import SELL as SELL_SIDE
                        down_px = round(1.0 - mm_state.sell_price, 2)
                        sell_price_back = round(down_px - 0.01, 2)
                        if sell_price_back > 0.01 and mm_state.inventory_down > 0:
                            args = OrderArgs(token_id=market.down_token_id, price=sell_price_back, size=mm_state.inventory_down, side=SELL_SIDE)
                            signed = client.create_order(args)
                            client.post_order(signed, OrderType.GTC)
                            console.print(f"  [yellow]📊 MM UNWIND: {market.coin} sold DOWN back[/yellow]")
                    except Exception:
                        pass
                    mm_state.sell_filled = False
                    mm_state.inventory_down = 0
                    mm_state._one_sided_since = None
        return

    # Inventory skew — widen the side we're long
    skew = 0.0
    if abs(mm_state.net_inventory) > V6_MM_MAX_INVENTORY:
        skew = 0.01 if mm_state.net_inventory > 0 else -0.01

    buy_price = round(mid - half_spread + skew, 2)
    sell_price = round(mid + half_spread + skew, 2)

    # Ensure valid prices
    if buy_price <= 0.01 or sell_price >= 0.99:
        return

    if is_paper:
        # Paper: just track the quote levels
        mm_state.buy_price = buy_price
        mm_state.sell_price = sell_price
        mm_state.buy_filled = False
        mm_state.sell_filled = False
        mm_state.last_midpoint = mid

        if mm_state.last_midpoint == 0:
            console.print(
                f"  [cyan]📊 {ptag}MM QUOTE: {market.coin} "
                f"BUY ${buy_price:.2f} / SELL ${sell_price:.2f} (mid: ${mid:.3f})[/cyan]"
            )
    else:
        # Real: cancel old orders, post new ones
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        # ── Check fills on existing orders before requoting ──
        buy_matched = 0
        sell_matched = 0

        if mm_state.buy_order_id and not mm_state.buy_filled:
            try:
                order = client.get_order(mm_state.buy_order_id)
                buy_matched = float(order.get("size_matched", 0)) if isinstance(order, dict) else float(getattr(order, "size_matched", 0))
                if buy_matched > 0:
                    mm_state.buy_filled = True
                    mm_state.inventory_up += buy_matched
                    console.print(f"  [cyan]📊 MM BUY FILL: {market.coin} UP @ ${mm_state.buy_price:.2f} ({buy_matched:.1f} shares)[/cyan]")
                else:
                    client.cancel(mm_state.buy_order_id)
            except Exception:
                try: client.cancel(mm_state.buy_order_id)
                except Exception: pass

        if mm_state.sell_order_id and not mm_state.sell_filled:
            try:
                order = client.get_order(mm_state.sell_order_id)
                sell_matched = float(order.get("size_matched", 0)) if isinstance(order, dict) else float(getattr(order, "size_matched", 0))
                if sell_matched > 0:
                    mm_state.sell_filled = True
                    mm_state.inventory_down += sell_matched
                    down_price = round(1.0 - mm_state.sell_price, 2)
                    console.print(f"  [cyan]📊 MM SELL FILL: {market.coin} DOWN @ ${down_price:.2f} ({sell_matched:.1f} shares)[/cyan]")
                else:
                    client.cancel(mm_state.sell_order_id)
            except Exception:
                try: client.cancel(mm_state.sell_order_id)
                except Exception: pass

        # ── Handle outcomes ──
        if mm_state.buy_filled and mm_state.sell_filled:
            # BOTH SIDES FILLED = spread profit (the goal)
            spread_profit = round(V6_MM_SPREAD * min(mm_state.inventory_up, mm_state.inventory_down), 2)
            bankroll.mm_pool += spread_profit
            bankroll.mm_pnl.wins += 1
            bankroll.mm_pnl.total_pnl += spread_profit
            bankroll.mm_pnl.trades += 1
            bankroll.estimated_rebates += V6_MM_SIZE * 2 * 0.01
            msg = f"📊 MM ROUND-TRIP: {market.coin} +${spread_profit:.2f} spread"
            console.print(f"  [bold cyan]{msg}[/bold cyan]")
            _send_tg(msg)
            mm_state.buy_filled = False
            mm_state.sell_filled = False
            mm_state.inventory_up = 0
            mm_state.inventory_down = 0

        elif mm_state.buy_filled and not mm_state.sell_filled:
            # ONE-SIDED: bought UP but DOWN didn't fill
            # SELL BACK the UP position immediately to avoid directional risk
            try:
                from py_clob_client.order_builder.constants import SELL as SELL_SIDE
                sell_back_price = round(mm_state.buy_price - 0.01, 2)  # Sell 1 cent below buy = small loss
                if sell_back_price > 0.01:
                    sell_args = OrderArgs(
                        token_id=market.up_token_id,
                        price=sell_back_price,
                        size=mm_state.inventory_up,
                        side=SELL_SIDE,
                    )
                    signed = client.create_order(sell_args)
                    client.post_order(signed, OrderType.GTC)
                    loss = round(0.01 * mm_state.inventory_up, 2)
                    bankroll.mm_pnl.total_pnl -= loss
                    console.print(f"  [yellow]📊 MM UNWIND: {market.coin} sold UP back (-${loss:.2f} to avoid directional risk)[/yellow]")
                else:
                    console.print(f"  [yellow]📊 MM UNWIND: {market.coin} price too low to sell back[/yellow]")
            except Exception as e:
                console.print(f"  [dim]MM unwind failed: {e}[/dim]")
            mm_state.buy_filled = False
            mm_state.sell_filled = False
            mm_state.inventory_up = 0
            mm_state.inventory_down = 0

        elif mm_state.sell_filled and not mm_state.buy_filled:
            # ONE-SIDED: bought DOWN but UP didn't fill
            # SELL BACK the DOWN position immediately
            try:
                from py_clob_client.order_builder.constants import SELL as SELL_SIDE
                down_buy_px = round(1.0 - mm_state.sell_price, 2)
                sell_back_price = round(down_buy_px - 0.01, 2)
                if sell_back_price > 0.01:
                    sell_args = OrderArgs(
                        token_id=market.down_token_id,
                        price=sell_back_price,
                        size=mm_state.inventory_down,
                        side=SELL_SIDE,
                    )
                    signed = client.create_order(sell_args)
                    client.post_order(signed, OrderType.GTC)
                    loss = round(0.01 * mm_state.inventory_down, 2)
                    bankroll.mm_pnl.total_pnl -= loss
                    console.print(f"  [yellow]📊 MM UNWIND: {market.coin} sold DOWN back (-${loss:.2f} to avoid directional risk)[/yellow]")
                else:
                    console.print(f"  [yellow]📊 MM UNWIND: {market.coin} price too low to sell back[/yellow]")
            except Exception as e:
                console.print(f"  [dim]MM unwind failed: {e}[/dim]")
            mm_state.buy_filled = False
            mm_state.sell_filled = False
            mm_state.inventory_up = 0
            mm_state.inventory_down = 0

        mm_state.buy_order_id = None
        mm_state.sell_order_id = None

        buy_size = max(5, math.floor((V6_MM_SIZE / buy_price) * 100) / 100)
        # "Sell UP" = Buy DOWN at complementary price
        down_buy_price = round(1.0 - sell_price, 2)
        sell_size = max(5, math.floor((V6_MM_SIZE / down_buy_price) * 100) / 100)

        try:
            # Post buy side (UP token)
            buy_args = OrderArgs(token_id=market.up_token_id, price=buy_price, size=buy_size, side=BUY)
            buy_signed = client.create_order(buy_args)
            buy_resp = client.post_order(buy_signed, OrderType.GTC)
            mm_state.buy_order_id = _extract_order_id(buy_resp)

            # Post sell side (= buy DOWN token)
            sell_args = OrderArgs(token_id=market.down_token_id, price=down_buy_price, size=sell_size, side=BUY)
            sell_signed = client.create_order(sell_args)
            sell_resp = client.post_order(sell_signed, OrderType.GTC)
            mm_state.sell_order_id = _extract_order_id(sell_resp)

            mm_state.buy_price = buy_price
            mm_state.sell_price = sell_price
            mm_state.last_midpoint = mid
            console.print(
                f"  [cyan]📊 MM QUOTE: {market.coin} "
                f"BUY ${buy_price:.2f} / SELL ${sell_price:.2f}[/cyan]"
            )
        except Exception as e:
            import traceback
            console.print(f"  [red]MM quote failed: {type(e).__name__}: {e}[/red]")
            console.print(f"  [dim]  buy: token={market.up_token_id[:12]}... price=${buy_price:.2f} size={buy_size}[/dim]")
            console.print(f"  [dim]  sell: token={market.down_token_id[:12]}... price=${down_buy_price:.2f} size={sell_size}[/dim]")

    mm_state.last_midpoint = mid


# ══════════════════════════════════════════════════════════════════════
# Strategy 3: Late Entry with Whale Confirmation
# ══════════════════════════════════════════════════════════════════════

async def attempt_late_entry(client, market, coin: str, bankroll: Bankroll,
                              window: WindowState, feed, is_paper: bool):
    """Place a late entry directional bet with Allium confirmation."""
    from allium_feed import allium

    ptag = "📝 " if is_paper else ""
    current_price = feed.get_price(coin)
    if not current_price or window.start_price <= 0:
        return

    pct_move = (current_price - window.start_price) / window.start_price * 100

    if abs(pct_move) < V6_LATE_MIN_MOVE_PCT:
        console.print(f"  [dim]{coin}: Late entry skip ({pct_move:.3f}% < {V6_LATE_MIN_MOVE_PCT}%)[/dim]")
        return

    direction = "up" if pct_move > 0 else "down"

    # Allium check
    if V6_LATE_REQUIRE_ALLIUM:
        try:
            from crypto_markets import get_current_window_timestamp
            signal = allium.get_signal(coin, get_current_window_timestamp())
            if signal.contradicts_side(direction):
                console.print(f"  [yellow]{coin}: Late entry — Allium CONTRADICTS {direction.upper()} — SKIP[/yellow]")
                return
            if not signal.confirms_side(direction):
                console.print(f"  [yellow]{coin}: Late entry — Allium neutral — SKIP[/yellow]")
                return
            console.print(f"  [green]{coin}: Late entry — Allium CONFIRMS {direction.upper()}[/green]")
        except Exception as e:
            console.print(f"  [dim]{coin}: Allium unavailable ({e}) — skipping late entry[/dim]")
            return

    # Bid price scales with move magnitude
    move_strength = min(abs(pct_move) / 0.5, 1.0)  # 0.3% = 0.6, 0.5%+ = 1.0
    bid_price = round(V6_LATE_BID_LOW + (V6_LATE_BID_HIGH - V6_LATE_BID_LOW) * move_strength, 2)
    bet = min(V6_LATE_BET_SIZE, bankroll.late_pool)
    if bet < 2:
        return

    size = max(5, math.floor((bet / bid_price) * 100) / 100)
    cost = round(bid_price * size, 2)
    token_id = market.up_token_id if direction == "up" else market.down_token_id

    if is_paper:
        trade = {
            "type": "late",
            "coin": coin,
            "direction": direction,
            "bid_price": bid_price,
            "size": size,
            "cost": cost,
            "token_id": token_id,
            "paper": True,
        }
        window.late_trades.append(trade)
        bankroll.late_pool -= cost

        msg = (
            f"⚡ {ptag}LATE ENTRY: {coin} {direction.upper()} @ ${bid_price:.2f}\n"
            f"{size:.0f} shares (${cost:.2f}) | Move: {pct_move:+.2f}%"
        )
        console.print(f"  [bold magenta]{msg}[/bold magenta]")
        _send_tg(msg)
    else:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        try:
            order_args = OrderArgs(token_id=token_id, price=bid_price, size=size, side=BUY)
            signed = client.create_order(order_args)
            resp = client.post_order(signed, OrderType.GTC)
            order_id = _extract_order_id(resp)

            trade = {
                "type": "late",
                "coin": coin,
                "direction": direction,
                "bid_price": bid_price,
                "size": size,
                "cost": cost,
                "order_id": order_id,
                "token_id": token_id,
            }
            window.late_trades.append(trade)
            bankroll.late_pool -= cost

            msg = (
                f"⚡ LATE ENTRY: {coin} {direction.upper()} @ ${bid_price:.2f}\n"
                f"{size:.0f} shares (${cost:.2f})"
            )
            console.print(f"  [bold magenta]{msg}[/bold magenta]")
            _send_tg(msg)
        except Exception as e:
            console.print(f"  [red]Late entry order failed: {e}[/red]")


def resolve_late_entry(trade: dict, bankroll: Bankroll, final_price: float, is_paper: bool):
    """Resolve a late entry trade at window close."""
    ptag = "📝 " if is_paper else ""
    direction = trade["direction"]
    cost = trade["cost"]
    size = trade["size"]
    coin = trade["coin"]

    # Check if direction was correct
    won = (direction == "up" and final_price > 0) or (direction == "down" and final_price > 0)

    # For paper trades, use Binance price to determine win/loss
    if is_paper:
        from binance_feed import feed as binance_feed
        from crypto_markets import get_current_window_timestamp
        current = binance_feed.get_price(coin)
        start = binance_feed.get_window_start(coin)
        if current and start:
            actual_direction = "up" if current > start else "down"
            won = (direction == actual_direction)
        else:
            won = False

    if won:
        payout = round(size * 1.00, 2)
        profit = payout - cost
        bankroll.late_pool += payout
        bankroll.late_pnl.wins += 1
        bankroll.late_pnl.total_pnl += profit
        msg = f"⚡ {ptag}LATE WIN: {coin} {direction.upper()} +${profit:.2f}"
        console.print(f"  [bold green]{msg}[/bold green]")
        _send_tg(msg)
    else:
        bankroll.late_pnl.losses += 1
        bankroll.late_pnl.total_pnl -= cost
        bankroll.daily_losses += cost
        msg = f"⚡ {ptag}LATE LOSS: {coin} {direction.upper()} -${cost:.2f}"
        console.print(f"  [red]{msg}[/red]")
        _send_tg(msg)

    bankroll.late_pnl.trades += 1
    bankroll.late_pnl.total_volume += cost
    bankroll.estimated_rebates += cost * 0.01


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

def _extract_order_id(response) -> Optional[str]:
    """Extract order ID from CLOB response."""
    if isinstance(response, dict):
        oid = response.get("orderID", response.get("id", ""))
        success = response.get("success", True)
        if not success:
            return None
        return oid if oid else None
    return str(response) if response else None


def _send_tg(msg: str):
    """Send Telegram alert (non-blocking)."""
    try:
        import telegram_alerts as tg
        tg.send_message(msg)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# Position Limit Checks
# ══════════════════════════════════════════════════════════════════════

_position_cache = {"data": [], "last_fetch": 0}


def _get_live_positions() -> list[dict]:
    """Fetch current positions from Polymarket, cached for 10 seconds."""
    import requests
    now = time.time()
    if now - _position_cache["last_fetch"] < 10:
        return _position_cache["data"]
    try:
        wallet = os.getenv("WALLET_ADDRESS", "")
        resp = requests.get(
            f"https://data-api.polymarket.com/positions?user={wallet}",
            timeout=10,
        )
        positions = resp.json()
        _position_cache["data"] = positions
        _position_cache["last_fetch"] = now
        return positions
    except Exception:
        return _position_cache["data"]


def get_total_deployed() -> float:
    """Get total USDC deployed across all open positions."""
    positions = _get_live_positions()
    total = 0
    for p in positions:
        size = float(p.get("size", 0))
        price = float(p.get("curPrice", 0) or 0)
        if size > 0 and 0.05 < price < 0.95:  # Only count unresolved
            total += size * min(price, 1 - price)
    return total


def get_coin_deployed(coin: str) -> float:
    """Get USDC deployed for a specific coin."""
    positions = _get_live_positions()
    total = 0
    for p in positions:
        size = float(p.get("size", 0))
        price = float(p.get("curPrice", 0) or 0)
        title = (p.get("title", "") or "").lower()
        coin_names = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "xrp"}
        coin_name = coin_names.get(coin, coin.lower())
        if size > 0 and coin_name in title and 0.05 < price < 0.95:
            total += size * min(price, 1 - price)
    return total


def can_place_order(coin: str, order_cost: float) -> bool:
    """Check if we can place an order without exceeding position limits."""
    total = get_total_deployed()
    coin_total = get_coin_deployed(coin)

    if total + order_cost > V6_MAX_TOTAL_DEPLOYED:
        return False
    if coin_total + order_cost > V6_MAX_PER_COIN:
        return False
    return True


# ══════════════════════════════════════════════════════════════════════
# Auto-Claim: Sell resolved winning positions to recycle USDC
# ══════════════════════════════════════════════════════════════════════

_redeem_service = None


def _get_redeem_service(client):
    """Initialize poly-web3 redeem service with Builder credentials (cached)."""
    global _redeem_service
    if _redeem_service is not None:
        return _redeem_service

    try:
        from poly_web3 import PolyWeb3Service, RelayClient, RELAYER_URL
        from py_builder_signing_sdk.config import BuilderConfig
        from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

        builder_key = os.getenv("POLY_BUILDER_API_KEY", "")
        builder_secret = os.getenv("POLY_BUILDER_SECRET", "")
        builder_pass = os.getenv("POLY_BUILDER_PASSPHRASE", "")

        if not all([builder_key, builder_secret, builder_pass]):
            console.print("  [dim]Builder keys not configured — auto-redeem disabled[/dim]")
            return None

        builder_config = BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(
                key=builder_key,
                secret=builder_secret,
                passphrase=builder_pass,
            )
        )

        relayer = RelayClient(
            private_key=os.getenv("PRIVATE_KEY", ""),
            relayer_url=RELAYER_URL,
            chain_id=137,
            builder_config=builder_config,
        )

        _redeem_service = PolyWeb3Service(
            clob_client=client,
            relayer_client=relayer,
            rpc_url="https://polygon-bor-rpc.publicnode.com",
        )
        console.print(f"  [green]💰 Auto-redeem service initialized (wallet: {_redeem_service.wallet_type})[/green]")
        return _redeem_service

    except Exception as e:
        console.print(f"  [dim]Redeem service init failed: {type(e).__name__}: {e}[/dim]")
        return None


async def auto_claim_resolved(client, bankroll: Bankroll):
    """Auto-redeem resolved winning positions via Builder API + poly-web3.

    Uses Polymarket's Relayer to gaslessly redeem CTF positions.
    Falls back to Telegram reminder if redeem fails.
    """
    service = _get_redeem_service(client)

    if service is not None:
        try:
            result = service.redeem_all(batch_size=10)
            if result and any(r is not None for r in result):
                redeemed_count = sum(1 for r in result if r is not None)
                msg = f"💰 AUTO-REDEEMED: {redeemed_count} positions claimed!"
                console.print(f"  [bold green]{msg}[/bold green]")
                _send_tg(msg)
                return redeemed_count
        except Exception as e:
            console.print(f"  [dim]Auto-redeem failed: {type(e).__name__}: {e}[/dim]")

    # Fallback: check for claimable positions and send reminder
    import requests
    try:
        wallet = os.getenv("WALLET_ADDRESS", "")
        resp = requests.get(
            f"https://data-api.polymarket.com/positions?user={wallet}",
            timeout=10,
        )
        positions = resp.json()

        claimable_total = 0
        claimable_count = 0
        for pos in positions:
            size = float(pos.get("size", 0))
            price = float(pos.get("curPrice", 0) or 0)
            if size >= 1 and price >= 0.95:
                claimable_total += size * 0.99
                claimable_count += 1

        if claimable_count > 0 and claimable_total > 10:
            last_alert = getattr(bankroll, '_last_claim_alert', 0)
            if time.time() - last_alert >= 900:
                bankroll._last_claim_alert = time.time()
                msg = (
                    f"💰 CLAIM REMINDER\n"
                    f"{claimable_count} positions worth ${claimable_total:.2f}\n"
                    f"Auto-redeem failed — claim at polymarket.com"
                )
                console.print(f"  [bold yellow]{msg}[/bold yellow]")
                _send_tg(msg)
    except Exception:
        pass

    return 0


def log_trade(msg: str):
    """Log trade to file."""
    try:
        with open("v6_trades.log", "a") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# Main Engine Loop
# ══════════════════════════════════════════════════════════════════════

async def run_v6_engine():
    """Run the V6 multi-strategy engine."""
    from binance_feed import feed, connect_binance, get_initial_prices, SYMBOLS
    from crypto_markets import (
        discover_market, discover_market_tf, WINDOW_SECONDS,
        TIMEFRAME_SECONDS,
        get_current_window_timestamp,
    )
    from trader import init_client
    from vpn import ensure_vpn

    # ── Startup ──
    console.print("=" * 60)
    console.print("  [bold]Polymarket V6 Multi-Strategy Engine[/bold]")
    console.print("=" * 60)
    console.print(f"  Coins:           {', '.join(V6_COINS)}")
    console.print(f"  Timeframes:      {', '.join(V6_TIMEFRAMES)}")
    console.print(f"  Markets:         {len(V6_COINS) * len(V6_TIMEFRAMES)}")
    console.print(f"  Bankroll:        ${V6_BANKROLL:.0f}")
    console.print(f"  Daily loss cap:  ${V6_DAILY_LOSS_LIMIT:.0f}")
    console.print(f"  Max deployed:    ${V6_MAX_TOTAL_DEPLOYED:.0f} total, ${V6_MAX_PER_COIN:.0f}/coin")
    console.print(f"  Strategies:")
    if V6_HEDGE_ENABLED:
        console.print(f"    🔄 Hedge:    ON  (threshold: ${V6_HEDGE_THRESHOLD}, {V6_HEDGE_ALLOC*100:.0f}% bankroll)")
    if V6_MM_ENABLED:
        console.print(f"    📊 MM:       ON  (spread: ${V6_MM_SPREAD}, {V6_MM_ALLOC*100:.0f}% bankroll)")
    if V6_LATE_ENABLED:
        console.print(f"    ⚡ Late:     ON  (T-{V6_LATE_ENTRY_SECONDS}s, ${V6_LATE_BID_LOW}-{V6_LATE_BID_HIGH}, {V6_LATE_ALLOC*100:.0f}% bankroll)")
    if PAPER_TRADE:
        console.print(f"  [bold yellow]📝 MODE: PAPER TRADE (no real orders)[/bold yellow]")

    # VPN check
    console.print(" Checking VPN connection...")
    ensure_vpn()

    # Init CLOB client
    private_key = os.getenv("PRIVATE_KEY", "")
    sig_type = int(os.getenv("SIGNATURE_TYPE", "1"))
    funder = os.getenv("WALLET_ADDRESS", "")
    client = init_client(private_key, sig_type, funder)
    console.print("[trader] CLOB client initialized and authenticated")

    # Init Binance feed
    console.print(" Fetching initial prices...")
    get_initial_prices()
    for coin in V6_COINS:
        if coin in SYMBOLS:
            p = feed.get_price(coin)
            if p:
                console.print(f"  {coin}: ${p:,.2f}")

    # Allium test
    try:
        from allium_feed import allium
        if allium.test_connection():
            console.print("  Allium: Connected (smart money active)")
        else:
            console.print("  [yellow]Allium: Not available (late entry will skip)[/yellow]")
    except Exception:
        console.print("  [yellow]Allium: Not available[/yellow]")

    # Start Binance WebSocket
    ws_task = asyncio.create_task(connect_binance())

    # Start Polymarket orderbook WebSocket
    from polymarket_ws import orderbook_feed
    poly_ws_task = asyncio.create_task(orderbook_feed.run())
    console.print("  [green]Polymarket WebSocket: Starting...[/green]")

    # ── Init state ──
    bankroll = Bankroll(starting=V6_BANKROLL)
    windows: dict[str, WindowState] = {}
    prev_windows: dict[str, WindowState] = {}
    mm_states: dict[str, MMState] = {}
    for coin in V6_COINS:
        mm_states[coin] = MMState(coin=coin)

    console.print(f"\nV6 engine started. Monitoring {len(V6_COINS)} coins...")
    status_counter = 0

    try:
        while True:
            now = int(time.time())

            if not bankroll.can_trade:
                if now % 60 == 0:
                    console.print(f"[red]Daily loss limit hit (${bankroll.daily_losses:.2f}/${V6_DAILY_LOSS_LIMIT:.2f})[/red]")
                await asyncio.sleep(V6_HEDGE_POLL_INTERVAL)
                continue

            for coin in V6_COINS:
              for tf in V6_TIMEFRAMES:
                market_key = f"{coin}_{tf}"
                tf_secs = TIMEFRAME_SECONDS.get(tf, 900)

                current_window_ts = (now // tf_secs) * tf_secs
                current_window_end = current_window_ts + tf_secs
                secs_left = current_window_end - now

                # Discover market
                market = discover_market_tf(coin, tf)
                if not market or not market.is_active:
                    continue

                # Subscribe to WebSocket orderbook for this market
                orderbook_feed.subscribe(market)

                # ── Window management ──
                window = windows.get(market_key)
                if window is None or window.window_start_ts != current_window_ts:
                    # Resolve previous window
                    if window and window.late_trades:
                        for trade in window.late_trades:
                            resolve_late_entry(trade, bankroll, 0, PAPER_TRADE)
                        window.late_trades = []

                    # DON'T reset MM state — persist inventory tracking across windows
                    # Only reset the quote prices so it requotes at new midpoint
                    if market_key in mm_states:
                        mm_states[market_key].buy_price = 0
                        mm_states[market_key].sell_price = 0
                        mm_states[market_key].last_midpoint = 0
                        mm_states[market_key].buy_order_id = None
                        mm_states[market_key].sell_order_id = None
                    else:
                        mm_states[market_key] = MMState(coin=coin)

                    # New window
                    start_price = feed.get_price(coin) or 0
                    if start_price > 0:
                        feed.set_window_start(coin, start_price)

                    window = WindowState(
                        coin=coin,
                        window_start_ts=current_window_ts,
                        start_price=start_price,
                    )
                    windows[market_key] = window

                    if start_price > 0:
                        console.print(
                            f"── {coin}/{tf} New window: "
                            f"{datetime.fromtimestamp(current_window_ts, tz=timezone.utc).strftime('%H:%M:%S')} UTC "
                            f"| Start: ${start_price:,.2f} | {secs_left}s remaining ──"
                        )

                # ── Priority 1: HEDGE ──
                if V6_HEDGE_ENABLED and bankroll.hedge_pool >= V6_HEDGE_BET_SIZE * 2 and secs_left > 30:
                    hedge = check_hedge_opportunity(client, market)
                    if hedge:
                        await execute_hedge(client, market, hedge, bankroll, PAPER_TRADE)
                    elif now % 10 == 0:  # Log hedge check every 10s to avoid spam
                        up_ask = get_best_ask(client, market.up_token_id)
                        down_ask = get_best_ask(client, market.down_token_id)
                        if up_ask and down_ask:
                            spread = up_ask[0] + down_ask[0]
                            console.print(
                                f"  [dim]{coin}: No hedge "
                                f"(UP ${up_ask[0]:.2f} + DOWN ${down_ask[0]:.2f} "
                                f"= ${spread:.2f} ≥ ${V6_HEDGE_THRESHOLD:.2f})[/dim]"
                            )

                # ── Priority 2: MARKET MAKING ──
                if V6_MM_ENABLED and bankroll.mm_pool >= V6_MM_SIZE and secs_left > 60:
                    if market_key not in mm_states:
                        mm_states[market_key] = MMState(coin=coin)
                    try:
                        await manage_mm_quotes(client, market, mm_states[market_key], bankroll, PAPER_TRADE)
                    except Exception as e:
                        console.print(f"  [dim]MM quote failed: {e}[/dim]")

                # ── Priority 3: LATE ENTRY ──
                if (V6_LATE_ENABLED
                    and not window.late_entry_done
                    and 0 < secs_left <= V6_LATE_ENTRY_SECONDS
                    and bankroll.late_pool >= V6_LATE_BET_SIZE):
                    await attempt_late_entry(client, market, coin, bankroll, window, feed, PAPER_TRADE)
                    window.late_entry_done = True

            # ── Status every 60 seconds ──
            status_counter += 1
            if status_counter >= int(60 / V6_HEDGE_POLL_INTERVAL):
                console.print(f"  {bankroll.status_line()}")
                status_counter = 0

            # ── Auto-claim every 60 seconds (use dedicated timer) ──
            current_time = int(time.time())
            if not PAPER_TRADE and (current_time - getattr(bankroll, '_last_claim', 0)) >= 60:
                bankroll._last_claim = current_time
                await auto_claim_resolved(client, bankroll)

            # ── Telegram status every 5 minutes ──
            if (current_time - getattr(bankroll, '_last_tg', 0)) >= 300:
                bankroll._last_tg = current_time
                _send_tg(
                    f"📈 V6 Status\n"
                    f"{bankroll.status_line()}\n"
                    f"Markets: {len(V6_COINS)} coins × {len(V6_TIMEFRAMES)} timeframes\n"
                    f"WS: {orderbook_feed.stats}"
                )

            await asyncio.sleep(V6_HEDGE_POLL_INTERVAL)

    except KeyboardInterrupt:
        console.print("\n[yellow]V6 engine stopping...[/yellow]")
        console.print(f"\n  {bankroll.status_line()}")
    finally:
        ws_task.cancel()
        poly_ws_task.cancel()


def main():
    asyncio.run(run_v6_engine())


if __name__ == "__main__":
    main()
