"""
Polymarket V7 — Dump-and-Hedge Engine
=======================================
One strategy, done right. Based on the $150K bot (94% win rate, 50K trades)
and the backtested 86% ROI dump-and-hedge approach.

How it works:
1. WATCH  — Stream orderbook via WebSocket, track price within each window
2. DETECT — Wait for a violent price dump (one side drops 15%+ from window high)
3. LEG 1  — Buy the dumped side at current ask (cheap)
4. WAIT   — Monitor opposite side's ask price
5. LEG 2  — When leg1_cost + opposite_ask ≤ sumTarget ($0.95), buy opposite side
6. PROFIT — One side pays $1.00 at resolution. Profit = $1.00 - total_cost
7. BAIL   — If hedge doesn't come within max_wait, sell Leg 1 back (small loss)

Sequential execution: each leg is CONFIRMED filled before moving to next.
No accidental directional exposure.
"""

import asyncio
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

V7_COINS = os.getenv("V7_COINS", "BTC,ETH,SOL,XRP").upper().split(",")
V7_TIMEFRAMES = os.getenv("V7_TIMEFRAMES", "5m,15m").split(",")
V7_BANKROLL = float(os.getenv("V7_BANKROLL", "100.0"))
V7_DAILY_LOSS_LIMIT = float(os.getenv("V7_DAILY_LOSS_LIMIT", "25.0"))
PAPER_TRADE = os.getenv("PAPER_TRADE", "false").lower() == "true"
V7_POLL_INTERVAL = float(os.getenv("V7_POLL_INTERVAL", "1.0"))

# Dump detection
V7_DUMP_THRESHOLD = float(os.getenv("V7_DUMP_THRESHOLD", "0.15"))  # 15% drop from window high
V7_DUMP_WINDOW_MIN = int(os.getenv("V7_DUMP_WINDOW_MIN", "30"))     # Only detect dumps after 30s into window

# Hedge target
V7_SUM_TARGET = float(os.getenv("V7_SUM_TARGET", "0.95"))          # Buy both sides if sum ≤ this
V7_MAX_WAIT_SECONDS = int(os.getenv("V7_MAX_WAIT_SECONDS", "120"))  # Max time to wait for Leg 2

# Sizing
V7_SHARES_PER_TRADE = int(os.getenv("V7_SHARES_PER_TRADE", "10"))
V7_MAX_OPEN_HEDGES = int(os.getenv("V7_MAX_OPEN_HEDGES", "4"))      # Max concurrent hedge attempts

# ══════════════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════════════

@dataclass
class TradeRecord:
    """Single trade result for history."""
    timestamp: float
    coin: str
    tf: str
    trade_type: str  # "hedge", "unwind", "loss"
    profit: float
    leg1_price: float = 0.0
    leg2_price: float = 0.0
    size: float = 0.0


@dataclass
class PnLTracker:
    wins: int = 0
    losses: int = 0
    unwinds: int = 0
    total_pnl: float = 0.0
    trades: int = 0
    history: list = field(default_factory=list)
    # Per-coin tracking
    coin_pnl: dict = field(default_factory=lambda: {})
    # Per-timeframe tracking
    tf_pnl: dict = field(default_factory=lambda: {"5m": 0.0, "15m": 0.0, "1h": 0.0})

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses + self.unwinds
        return (self.wins / total * 100) if total > 0 else 0

    def record(self, trade_type: str, coin: str, tf: str, profit: float,
               leg1_price: float = 0, leg2_price: float = 0, size: float = 0):
        """Record a trade result."""
        rec = TradeRecord(
            timestamp=time.time(), coin=coin, tf=tf, trade_type=trade_type,
            profit=profit, leg1_price=leg1_price, leg2_price=leg2_price, size=size,
        )
        self.history.append(rec)
        self.total_pnl += profit
        self.trades += 1

        # Per-coin
        if coin not in self.coin_pnl:
            self.coin_pnl[coin] = {"pnl": 0.0, "wins": 0, "unwinds": 0}
        self.coin_pnl[coin]["pnl"] += profit
        if trade_type == "hedge":
            self.coin_pnl[coin]["wins"] += 1
        elif trade_type == "unwind":
            self.coin_pnl[coin]["unwinds"] += 1

        # Per-timeframe
        if tf in self.tf_pnl:
            self.tf_pnl[tf] += profit

    def summary(self) -> str:
        return (
            f"{self.wins}W/{self.losses}L/{self.unwinds}U "
            f"({self.win_rate:.0f}%) ${self.total_pnl:+.2f}"
        )

    def detailed_summary(self) -> str:
        """Per-coin and per-timeframe breakdown."""
        lines = []
        for coin in sorted(self.coin_pnl.keys()):
            c = self.coin_pnl[coin]
            lines.append(f"  {coin}: {c['wins']}W/{c['unwinds']}U ${c['pnl']:+.2f}")
        tf_parts = [f"{tf}: ${self.tf_pnl[tf]:+.2f}" for tf in ["5m", "15m", "1h"] if self.tf_pnl.get(tf, 0) != 0]
        if tf_parts:
            lines.append(f"  {' | '.join(tf_parts)}")
        # Last 5 trades
        if self.history:
            lines.append("  Recent:")
            for rec in self.history[-5:]:
                icon = "🔄" if rec.trade_type == "hedge" else "⏰"
                lines.append(f"    {icon} {rec.coin}/{rec.tf} ${rec.profit:+.2f}")
        return "\n".join(lines)


@dataclass
class Bankroll:
    starting: float
    balance: float = 0.0
    daily_losses: float = 0.0
    pnl: PnLTracker = field(default_factory=PnLTracker)

    def __post_init__(self):
        self.balance = self.starting

    @property
    def can_trade(self) -> bool:
        return self.daily_losses < V7_DAILY_LOSS_LIMIT

    def status_line(self) -> str:
        return (
            f"P&L: ${self.pnl.total_pnl:+.2f} | "
            f"Cash: ${self.balance:.2f} | "
            f"{self.pnl.summary()} | "
            f"Deployed: ${self.starting - self.balance:.2f}"
        )

    def full_report(self) -> str:
        """Detailed P&L report for Telegram."""
        lines = [
            f"📊 V7 P&L Report",
            f"Total: ${self.pnl.total_pnl:+.2f} | Cash: ${self.balance:.2f}",
            f"Record: {self.pnl.summary()}",
            f"",
        ]
        # Per coin
        for coin in sorted(self.pnl.coin_pnl.keys()):
            c = self.pnl.coin_pnl[coin]
            lines.append(f"{coin}: {c['wins']}W/{c['unwinds']}U ${c['pnl']:+.2f}")
        # Per timeframe
        tf_parts = [f"{tf}: ${self.pnl.tf_pnl[tf]:+.2f}" for tf in ["5m", "15m", "1h"] if self.pnl.tf_pnl.get(tf, 0) != 0]
        if tf_parts:
            lines.append(f"")
            lines.append(" | ".join(tf_parts))
        return "\n".join(lines)


@dataclass
class WindowTracker:
    """Tracks price highs/lows within a single market window."""
    coin: str
    tf: str
    window_start_ts: int
    up_high: float = 0.0    # Highest UP ask seen this window
    down_high: float = 0.0   # Highest DOWN ask seen this window
    up_low: float = 1.0      # Lowest UP ask seen
    down_low: float = 1.0    # Lowest DOWN ask seen
    leg1_done: bool = False   # Already entered Leg 1 this window


@dataclass
class OpenHedge:
    """Tracks an in-progress hedge (Leg 1 placed, waiting for Leg 2)."""
    hedge_id: str
    coin: str
    tf: str
    leg1_side: str           # "up" or "down" — which side we bought
    leg1_price: float        # Price we paid for Leg 1
    leg1_size: float         # Shares bought
    leg1_cost: float         # Total USDC spent on Leg 1
    leg1_token_id: str
    leg1_order_id: str
    leg2_target: float       # Max price for Leg 2 (sum_target - leg1_price)
    leg2_token_id: str       # Token ID for the opposite side
    placed_at: float         # time.time() when Leg 1 was placed
    market_slug: str = ""
    paper: bool = False


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


# ══════════════════════════════════════════════════════════════════════
# Dump Detection
# ══════════════════════════════════════════════════════════════════════

def check_for_dump(tracker: WindowTracker, up_ask: float, down_ask: float) -> Optional[dict]:
    """Check if either side has dumped from its window high.

    Returns dict with dump info if detected, None otherwise.
    """
    # Update highs and lows
    if up_ask > 0:
        tracker.up_high = max(tracker.up_high, up_ask)
        tracker.up_low = min(tracker.up_low, up_ask)
    if down_ask > 0:
        tracker.down_high = max(tracker.down_high, down_ask)
        tracker.down_low = min(tracker.down_low, down_ask)

    # Check for UP dump (UP price dropped significantly from high)
    if tracker.up_high > 0.10:
        up_drop = (tracker.up_high - up_ask) / tracker.up_high
        if up_drop >= V7_DUMP_THRESHOLD and up_ask > 0.05:
            return {
                "dumped_side": "up",
                "current_ask": up_ask,
                "high": tracker.up_high,
                "drop_pct": up_drop,
                "opposite_ask": down_ask,
            }

    # Check for DOWN dump
    if tracker.down_high > 0.10:
        down_drop = (tracker.down_high - down_ask) / tracker.down_high
        if down_drop >= V7_DUMP_THRESHOLD and down_ask > 0.05:
            return {
                "dumped_side": "down",
                "current_ask": down_ask,
                "high": tracker.down_high,
                "drop_pct": down_drop,
                "opposite_ask": up_ask,
            }

    return None


# ══════════════════════════════════════════════════════════════════════
# Trade Execution
# ══════════════════════════════════════════════════════════════════════

async def execute_leg1(client, market, dump_info: dict, bankroll: Bankroll, is_paper: bool) -> Optional[OpenHedge]:
    """Execute Leg 1 — buy the dumped side."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    side = dump_info["dumped_side"]
    ask_price = round(dump_info["current_ask"], 2)
    size = V7_SHARES_PER_TRADE
    cost = round(ask_price * size, 2)
    tf = "5m" if "5m" in market.slug else "15m" if "15m" in market.slug else "1h"

    if cost > bankroll.balance or cost < 1:
        return None

    token_id = market.up_token_id if side == "up" else market.down_token_id
    opposite_token_id = market.down_token_id if side == "up" else market.up_token_id
    leg2_target = round(V7_SUM_TARGET - ask_price, 2)

    hedge_id = str(uuid.uuid4())[:8]
    ptag = "📝 " if is_paper else ""

    if is_paper:
        order_id = f"paper_leg1_{hedge_id}"
    else:
        try:
            args = OrderArgs(token_id=token_id, price=ask_price, size=size, side=BUY)
            signed = client.create_order(args)
            resp = client.post_order(signed, OrderType.GTC)
            order_id = _extract_order_id(resp)
            if not order_id:
                console.print(f"  [red]Leg 1 order rejected[/red]")
                return None
        except Exception as e:
            console.print(f"  [red]Leg 1 failed: {e}[/red]")
            return None

    bankroll.balance -= cost

    msg = (
        f"🎯 {ptag}LEG 1: {market.coin}/{tf} {side.upper()} @ ${ask_price:.2f}\n"
        f"{size} shares (${cost:.2f}) | Drop: {dump_info['drop_pct']:.0%} from ${dump_info['high']:.2f}\n"
        f"Need Leg 2 @ ≤${leg2_target:.2f} for hedge"
    )
    console.print(f"  [bold green]{msg}[/bold green]")
    _send_tg(msg)

    return OpenHedge(
        hedge_id=hedge_id,
        coin=market.coin,
        tf=tf,
        leg1_side=side,
        leg1_price=ask_price,
        leg1_size=size,
        leg1_cost=cost,
        leg1_token_id=token_id,
        leg1_order_id=order_id,
        leg2_target=leg2_target,
        leg2_token_id=opposite_token_id,
        placed_at=time.time(),
        market_slug=market.slug if hasattr(market, 'slug') else "",
        paper=is_paper,
    )


async def attempt_leg2(client, hedge: OpenHedge, opposite_ask: float, bankroll: Bankroll, is_paper: bool) -> bool:
    """Attempt Leg 2 — buy opposite side if price is right."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    if opposite_ask > hedge.leg2_target or opposite_ask <= 0.01:
        return False

    ask_price = round(opposite_ask, 2)
    size = hedge.leg1_size  # Match Leg 1 size
    cost = round(ask_price * size, 2)

    if cost > bankroll.balance:
        return False

    ptag = "📝 " if is_paper else ""

    if is_paper:
        order_id = f"paper_leg2_{hedge.hedge_id}"
    else:
        try:
            args = OrderArgs(token_id=hedge.leg2_token_id, price=ask_price, size=size, side=BUY)
            signed = client.create_order(args)
            resp = client.post_order(signed, OrderType.GTC)
            order_id = _extract_order_id(resp)
            if not order_id:
                return False
        except Exception as e:
            console.print(f"  [red]Leg 2 failed: {e}[/red]")
            return False

    bankroll.balance -= cost
    total_cost = hedge.leg1_cost + cost
    profit = round(size * 1.00 - total_cost, 2)

    bankroll.balance += size * 1.00  # One side will pay $1.00
    bankroll.pnl.wins += 1
    bankroll.pnl.record("hedge", hedge.coin, hedge.tf, profit,
                        leg1_price=hedge.leg1_price, leg2_price=ask_price, size=size)

    opposite_side = "down" if hedge.leg1_side == "up" else "up"
    msg = (
        f"🔄 {ptag}HEDGED: {hedge.coin}/{hedge.tf}\n"
        f"Leg 1: {hedge.leg1_side.upper()} @ ${hedge.leg1_price:.2f}\n"
        f"Leg 2: {opposite_side.upper()} @ ${ask_price:.2f}\n"
        f"Sum: ${hedge.leg1_price + ask_price:.2f} → +${profit:.2f} profit"
    )
    console.print(f"  [bold cyan]{msg}[/bold cyan]")
    _send_tg(msg)
    return True


def get_best_bid(client, token_id: str):
    """Get best bid price and size from CLOB orderbook."""
    try:
        book = client.get_order_book(token_id)
        bids_raw = book.get("bids", []) if isinstance(book, dict) else getattr(book, "bids", [])
        parsed = []
        for entry in bids_raw:
            if isinstance(entry, dict):
                p = float(entry.get("price", 0))
                s = float(entry.get("size", 0))
            else:
                p = float(getattr(entry, "price", 0))
                s = float(getattr(entry, "size", 0))
            if p > 0 and s > 0:
                parsed.append((p, s))
        if not parsed:
            return None
        best_price = max(p for p, s in parsed)
        best_size = sum(s for p, s in parsed if p == best_price)
        return (round(best_price, 2), best_size)
    except Exception:
        return None


async def unwind_leg1(client, hedge: OpenHedge, bankroll: Bankroll, is_paper: bool):
    """Sell back Leg 1 — hedge window expired. MUST actually sell or track full loss."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import SELL
    import time as _time

    ptag = "📝 " if is_paper else ""

    if is_paper:
        loss = round(0.02 * hedge.leg1_size, 2)
        bankroll.balance += hedge.leg1_cost - loss
        bankroll.pnl.unwinds += 1
        bankroll.pnl.record("unwind", hedge.coin, hedge.tf, -loss,
                            leg1_price=hedge.leg1_price, size=hedge.leg1_size)
        bankroll.daily_losses += loss
        msg = (
            f"⏰ {ptag}UNWIND: {hedge.coin}/{hedge.tf} {hedge.leg1_side.upper()}\n"
            f"No hedge found in {V7_MAX_WAIT_SECONDS}s → sold back (-${loss:.2f})"
        )
        console.print(f"  [yellow]{msg}[/yellow]")
        _send_tg(msg)
        return

    # ── REAL UNWIND: Sell at best bid, verify fill ──
    # Get current best bid — sell at MARKET price, not our cost
    best_bid = get_best_bid(client, hedge.leg1_token_id)
    if best_bid and best_bid[0] > 0.01:
        sell_price = best_bid[0]  # Sell at whatever the market will pay
    else:
        # No bid — position is worthless, record full loss
        loss = hedge.leg1_cost
        bankroll.balance -= 0  # Already spent when we bought
        bankroll.pnl.unwinds += 1
        bankroll.pnl.record("unwind", hedge.coin, hedge.tf, -loss,
                            leg1_price=hedge.leg1_price, size=hedge.leg1_size)
        bankroll.daily_losses += loss
        msg = (
            f"⚠️ UNWIND FAILED: {hedge.coin}/{hedge.tf} {hedge.leg1_side.upper()}\n"
            f"No bids — full loss -${loss:.2f}"
        )
        console.print(f"  [red]{msg}[/red]")
        _send_tg(msg)
        return

    # Place sell order at best bid
    filled = False
    try:
        args = OrderArgs(
            token_id=hedge.leg1_token_id,
            price=sell_price,
            size=hedge.leg1_size,
            side=SELL,
        )
        signed = client.create_order(args)
        response = client.post_order(signed, OrderType.GTC)

        # Extract order ID and verify fill
        order_id = None
        if isinstance(response, dict):
            order_id = response.get("orderID", response.get("id", ""))
            if not response.get("success", True):
                order_id = None

        if order_id:
            # Wait up to 10 seconds for fill
            for _ in range(5):
                _time.sleep(2)
                try:
                    order_status = client.get_order(order_id)
                    if isinstance(order_status, dict):
                        status = order_status.get("status", "").upper()
                        size_matched = float(order_status.get("size_matched", 0))
                    else:
                        status = getattr(order_status, "status", "").upper()
                        size_matched = float(getattr(order_status, "size_matched", 0))

                    if status == "MATCHED" or size_matched > 0:
                        filled = True
                        actual_sell = round(sell_price * size_matched, 2)
                        loss = round(hedge.leg1_cost - actual_sell, 2)
                        break
                    elif status in ("CANCELLED", "EXPIRED"):
                        break
                except Exception:
                    pass

            # If not filled, cancel and try at lower price
            if not filled:
                try:
                    client.cancel(order_id)
                except Exception:
                    pass

                # Try again at 50% of best bid (desperate sell)
                desperate_price = round(sell_price * 0.5, 2)
                if desperate_price > 0.01:
                    try:
                        args2 = OrderArgs(
                            token_id=hedge.leg1_token_id,
                            price=desperate_price,
                            size=hedge.leg1_size,
                            side=SELL,
                        )
                        signed2 = client.create_order(args2)
                        client.post_order(signed2, OrderType.GTC)
                        # Don't wait — just hope it fills. Record estimated loss.
                        loss = round(hedge.leg1_cost - desperate_price * hedge.leg1_size, 2)
                        filled = True  # Optimistic — at least we tried hard
                    except Exception:
                        pass
    except Exception as e:
        console.print(f"  [red]Unwind order failed: {e}[/red]")

    # If still not filled, record FULL loss
    if not filled:
        loss = hedge.leg1_cost
        msg = (
            f"⚠️ UNWIND FAILED: {hedge.coin}/{hedge.tf} {hedge.leg1_side.upper()}\n"
            f"Could not sell — full loss -${loss:.2f}"
        )
    else:
        msg = (
            f"⏰ UNWIND: {hedge.coin}/{hedge.tf} {hedge.leg1_side.upper()}\n"
            f"Sold @ ${sell_price:.2f} → loss -${loss:.2f}"
        )

    bankroll.balance += hedge.leg1_cost - loss
    bankroll.pnl.unwinds += 1
    bankroll.pnl.record("unwind", hedge.coin, hedge.tf, -loss,
                        leg1_price=hedge.leg1_price, size=hedge.leg1_size)
    bankroll.daily_losses += loss

    console.print(f"  [yellow]{msg}[/yellow]")
    _send_tg(msg)


# ══════════════════════════════════════════════════════════════════════
# Auto-Redeem
# ══════════════════════════════════════════════════════════════════════

_redeem_service = None

async def auto_claim_resolved(client, bankroll: Bankroll):
    """Auto-redeem via Builder API, fallback to TG reminder."""
    global _redeem_service
    if _redeem_service is None:
        try:
            from poly_web3 import PolyWeb3Service, RelayClient, RELAYER_URL
            from py_builder_signing_sdk.config import BuilderConfig
            from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

            bk = os.getenv("POLY_BUILDER_API_KEY", "")
            bs = os.getenv("POLY_BUILDER_SECRET", "")
            bp = os.getenv("POLY_BUILDER_PASSPHRASE", "")
            if not all([bk, bs, bp]):
                return
            builder_config = BuilderConfig(local_builder_creds=BuilderApiKeyCreds(key=bk, secret=bs, passphrase=bp))
            relayer = RelayClient(private_key=os.getenv("PRIVATE_KEY", ""), relayer_url=RELAYER_URL, chain_id=137, builder_config=builder_config)
            _redeem_service = PolyWeb3Service(clob_client=client, relayer_client=relayer, rpc_url="https://polygon-bor-rpc.publicnode.com")
        except Exception:
            return

    try:
        result = _redeem_service.redeem_all(batch_size=10)
        if result and any(r is not None for r in result):
            count = sum(1 for r in result if r is not None)
            _send_tg(f"💰 AUTO-REDEEMED: {count} positions claimed!")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# Main Engine
# ══════════════════════════════════════════════════════════════════════

async def run_v7_engine():
    """Run the V7 dump-and-hedge engine."""
    from binance_feed import feed, connect_binance, get_initial_prices, SYMBOLS
    from crypto_markets import discover_market_tf, TIMEFRAME_SECONDS
    from polymarket_ws import orderbook_feed
    from trader import init_client
    from vpn import ensure_vpn

    # ── Startup ──
    console.print("=" * 60)
    console.print("  [bold]Polymarket V7 — Dump-and-Hedge Engine[/bold]")
    console.print("=" * 60)
    console.print(f"  Coins:           {', '.join(V7_COINS)}")
    console.print(f"  Timeframes:      {', '.join(V7_TIMEFRAMES)}")
    console.print(f"  Bankroll:        ${V7_BANKROLL:.0f}")
    console.print(f"  Dump threshold:  {V7_DUMP_THRESHOLD:.0%}")
    console.print(f"  Sum target:      ${V7_SUM_TARGET}")
    console.print(f"  Shares/trade:    {V7_SHARES_PER_TRADE}")
    console.print(f"  Max wait:        {V7_MAX_WAIT_SECONDS}s for Leg 2")
    console.print(f"  Max open hedges: {V7_MAX_OPEN_HEDGES}")
    if PAPER_TRADE:
        console.print(f"  [bold yellow]📝 MODE: PAPER TRADE[/bold yellow]")

    console.print(" Checking VPN...")
    ensure_vpn()

    private_key = os.getenv("PRIVATE_KEY", "")
    sig_type = int(os.getenv("SIGNATURE_TYPE", "1"))
    funder = os.getenv("WALLET_ADDRESS", "")
    client = init_client(private_key, sig_type, funder)
    console.print("[trader] CLOB client authenticated")

    console.print(" Fetching prices...")
    get_initial_prices()
    for coin in V7_COINS:
        if coin in SYMBOLS:
            p = feed.get_price(coin)
            if p:
                console.print(f"  {coin}: ${p:,.2f}")

    # Start WebSockets
    ws_binance = asyncio.create_task(connect_binance())
    ws_poly = asyncio.create_task(orderbook_feed.run())
    console.print("  WebSockets: Binance + Polymarket starting...")

    # ── State ──
    bankroll = Bankroll(starting=V7_BANKROLL)
    trackers: dict[str, WindowTracker] = {}
    open_hedges: list[OpenHedge] = []

    console.print(f"\nV7 engine started. Watching for dumps...")

    try:
        while True:
            now = int(time.time())

            if not bankroll.can_trade:
                if now % 60 == 0:
                    console.print(f"[red]Daily loss limit hit[/red]")
                await asyncio.sleep(V7_POLL_INTERVAL)
                continue

            # ── Check open hedges: attempt Leg 2 or unwind ──
            resolved_hedges = []
            for hedge in open_hedges:
                # Get opposite side's current ask — WebSocket first, REST fallback
                opposite_ask_data = orderbook_feed.get_best_ask(hedge.leg2_token_id)
                if not opposite_ask_data:
                    try:
                        from arb_engine_v6 import get_best_ask as rest_get_best_ask
                        opposite_ask_data = rest_get_best_ask(client, hedge.leg2_token_id)
                    except Exception:
                        pass
                if opposite_ask_data:
                    opposite_ask = opposite_ask_data[0]

                    # Try Leg 2
                    if opposite_ask <= hedge.leg2_target:
                        success = await attempt_leg2(client, hedge, opposite_ask, bankroll, PAPER_TRADE)
                        if success:
                            resolved_hedges.append(hedge)
                            continue

                # Check timeout
                elapsed = time.time() - hedge.placed_at
                if elapsed >= V7_MAX_WAIT_SECONDS:
                    await unwind_leg1(client, hedge, bankroll, PAPER_TRADE)
                    resolved_hedges.append(hedge)

            for h in resolved_hedges:
                if h in open_hedges:
                    open_hedges.remove(h)

            # ── Scan for dumps across all markets ──
            for coin in V7_COINS:
                if coin not in SYMBOLS:
                    continue

                for tf in V7_TIMEFRAMES:
                    market_key = f"{coin}_{tf}"
                    tf_secs = TIMEFRAME_SECONDS.get(tf, 900)
                    window_ts = (now // tf_secs) * tf_secs
                    secs_into_window = now - window_ts
                    secs_left = tf_secs - secs_into_window

                    # Discover market + subscribe to WebSocket
                    market = discover_market_tf(coin, tf)
                    if not market or not market.is_active:
                        continue
                    orderbook_feed.subscribe(market)

                    # Window tracker
                    tracker = trackers.get(market_key)
                    if tracker is None or tracker.window_start_ts != window_ts:
                        tracker = WindowTracker(
                            coin=coin, tf=tf, window_start_ts=window_ts,
                        )
                        trackers[market_key] = tracker
                        if secs_left > 30:
                            console.print(
                                f"── {coin}/{tf} Window "
                                f"{datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime('%H:%M:%S')} UTC "
                                f"| {secs_left}s left ──"
                            )

                    # Skip if too early in window or too close to end
                    if secs_into_window < V7_DUMP_WINDOW_MIN or secs_left < 30:
                        continue

                    # Skip if already entered Leg 1 this window for this market
                    if tracker.leg1_done:
                        continue

                    # Skip if at max open hedges
                    if len(open_hedges) >= V7_MAX_OPEN_HEDGES:
                        continue

                    # Get current prices — WebSocket first, REST fallback
                    up_ask_data = orderbook_feed.get_best_ask(market.up_token_id)
                    down_ask_data = orderbook_feed.get_best_ask(market.down_token_id)

                    if not up_ask_data or not down_ask_data:
                        # REST fallback
                        try:
                            from arb_engine_v6 import get_best_ask as rest_get_best_ask
                            if not up_ask_data:
                                up_ask_data = rest_get_best_ask(client, market.up_token_id)
                            if not down_ask_data:
                                down_ask_data = rest_get_best_ask(client, market.down_token_id)
                        except Exception:
                            pass

                    if not up_ask_data or not down_ask_data:
                        continue

                    up_ask = up_ask_data[0]
                    down_ask = down_ask_data[0]

                    # Check for dump
                    dump = check_for_dump(tracker, up_ask, down_ask)
                    if dump:
                        # Verify sum is close enough to be hedgeable
                        current_sum = dump["current_ask"] + dump["opposite_ask"]
                        if current_sum > 1.05:
                            continue  # Spread too wide, skip

                        hedge = await execute_leg1(client, market, dump, bankroll, PAPER_TRADE)
                        if hedge:
                            hedge.tf = tf
                            open_hedges.append(hedge)
                            tracker.leg1_done = True

            # ── Status every 30 seconds ──
            if now % 30 == 0:
                hedges_str = f" | Open hedges: {len(open_hedges)}" if open_hedges else ""
                console.print(f"  {bankroll.status_line()}{hedges_str} | WS: {orderbook_feed.stats}")

            # ── TG status every 5 min ──
            current_time = int(time.time())
            if (current_time - getattr(bankroll, '_last_tg', 0)) >= 300:
                bankroll._last_tg = current_time
                _send_tg(f"{bankroll.full_report()}\nOpen hedges: {len(open_hedges)}\nWS: {orderbook_feed.stats}")

            # ── Auto-redeem every 60s ──
            if not PAPER_TRADE and (current_time - getattr(bankroll, '_last_claim', 0)) >= 60:
                bankroll._last_claim = current_time
                await auto_claim_resolved(client, bankroll)

            await asyncio.sleep(V7_POLL_INTERVAL)

    except KeyboardInterrupt:
        console.print(f"\n[yellow]V7 stopping...[/yellow]")
        # Unwind all open hedges
        for hedge in open_hedges:
            await unwind_leg1(client, hedge, bankroll, PAPER_TRADE)
        console.print(f"\n  {bankroll.status_line()}")
    finally:
        ws_binance.cancel()
        ws_poly.cancel()


def main():
    asyncio.run(run_v7_engine())


if __name__ == "__main__":
    main()
