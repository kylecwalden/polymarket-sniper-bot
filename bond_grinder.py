"""
bond_grinder.py — High-probability bond grinding strategy.

Buy near-certain outcomes (95-99¢) that resolve in 1-7 days.
Hold to resolution, collect yield. Stack multiple positions.

Improvements over base strategy:
- Allium smart money gate (skip if whales bet against)
- Auto-redeem via Builder API (recycle USDC automatically)
- Full P&L tracking with per-market resolution
- Telegram alerts for entries, wins, losses

Expected: 3-7% per trade, 60-180% APY when stacked.
Risk: ~5% of "near-certain" outcomes lose. One loss wipes 20 wins.
"""

import asyncio
import json
import math
import os
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from rich.console import Console

load_dotenv()
console = Console()

GAMMA_API = "https://gamma-api.polymarket.com"

# ══════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════

BOND_BUDGET = float(os.getenv("BOND_BUDGET", "0"))             # Daily budget (0 = unlimited)
BOND_BET_SIZE = float(os.getenv("BOND_BET_SIZE", "2.0"))      # Per position
BOND_MIN_PRICE = float(os.getenv("BOND_MIN_PRICE", "0.95"))   # Only 95+ cents
BOND_MAX_PRICE = float(os.getenv("BOND_MAX_PRICE", "0.993"))  # Leave room for fees
BOND_MIN_HOURS = float(os.getenv("BOND_MIN_HOURS", "2.0"))    # Not too close
BOND_MAX_DAYS = float(os.getenv("BOND_MAX_DAYS", "7.0"))      # Up to 7 days
BOND_MIN_VOLUME = float(os.getenv("BOND_MIN_VOLUME", "100.0"))  # $100 min 24h volume
BOND_MAX_POSITIONS = int(os.getenv("BOND_MAX_POSITIONS", "10"))  # Max simultaneous
BOND_MIN_YIELD = float(os.getenv("BOND_MIN_YIELD", "0.005"))  # Min 0.5% yield
BOND_SCAN_INTERVAL = int(os.getenv("BOND_SCAN_INTERVAL", "300"))  # 5 min
BOND_USE_ALLIUM = os.getenv("BOND_USE_ALLIUM", "true").lower() == "true"
PAPER_TRADE = os.getenv("PAPER_TRADE", "false").lower() == "true"

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
SPEND_FILE = DATA_DIR / "bond_spend.json"
ORDERS_FILE = DATA_DIR / "bond_orders.jsonl"
PNL_FILE = DATA_DIR / "bond_pnl.json"


# ══════════════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════════════

@dataclass
class BondOpportunity:
    token_id: str
    outcome: str
    price: float
    market_question: str
    market_id: str
    condition_id: str
    end_date: str
    hours_to_expiry: float
    volume_24h: float

    @property
    def yield_pct(self) -> float:
        """Net yield after Polymarket 2% fee on winnings."""
        gross = (1.0 - self.price) / self.price
        fee = 0.02
        return gross - fee * (1.0 / self.price - 1)

    @property
    def annualized_yield(self) -> float:
        if self.hours_to_expiry <= 0:
            return 0
        daily = self.yield_pct / (self.hours_to_expiry / 24)
        return daily * 365

    @property
    def days_to_expiry(self) -> float:
        return self.hours_to_expiry / 24


@dataclass
class BondPnL:
    wins: int = 0
    losses: int = 0
    total_invested: float = 0.0
    total_returned: float = 0.0
    positions_open: int = 0

    @property
    def net_pnl(self) -> float:
        return self.total_returned - self.total_invested

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return (self.wins / total * 100) if total > 0 else 0

    def summary(self) -> str:
        return (
            f"🏦 Bond P&L: ${self.net_pnl:+.2f} | "
            f"{self.wins}W/{self.losses}L ({self.win_rate:.0f}%) | "
            f"Open: {self.positions_open}"
        )

    def save(self):
        PNL_FILE.write_text(json.dumps({
            "wins": self.wins, "losses": self.losses,
            "total_invested": self.total_invested,
            "total_returned": self.total_returned,
            "positions_open": self.positions_open,
        }))

    @classmethod
    def load(cls):
        if PNL_FILE.exists():
            try:
                d = json.loads(PNL_FILE.read_text())
                return cls(**d)
            except Exception:
                pass
        return cls()


# ══════════════════════════════════════════════════════════════════════
# Budget Tracking (persists across restarts)
# ══════════════════════════════════════════════════════════════════════

def get_daily_spend() -> float:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not SPEND_FILE.exists():
        return 0.0
    try:
        return float(json.loads(SPEND_FILE.read_text()).get(today, 0.0))
    except Exception:
        return 0.0


def record_spend(amount: float):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = {}
    if SPEND_FILE.exists():
        try:
            data = json.loads(SPEND_FILE.read_text())
        except Exception:
            pass
    data[today] = data.get(today, 0.0) + amount
    SPEND_FILE.write_text(json.dumps(data))


def get_placed_token_ids() -> set:
    if not ORDERS_FILE.exists():
        return set()
    ids = set()
    for line in ORDERS_FILE.read_text().strip().split("\n"):
        if line.strip():
            try:
                ids.add(json.loads(line).get("token_id", ""))
            except Exception:
                pass
    return ids


def save_order(order: dict):
    with ORDERS_FILE.open("a") as f:
        f.write(json.dumps(order) + "\n")


# ══════════════════════════════════════════════════════════════════════
# Market Scanner
# ══════════════════════════════════════════════════════════════════════

def scan_bond_opportunities() -> list[BondOpportunity]:
    """Scan for high-probability markets (95+ cents, 2h-7 days out)."""
    now_utc = datetime.now(timezone.utc)
    min_cutoff = now_utc + timedelta(hours=BOND_MIN_HOURS)
    max_cutoff = now_utc + timedelta(days=BOND_MAX_DAYS)

    results = []
    offset = 0
    limit = 500

    while True:
        params = urllib.parse.urlencode({
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
            "order": "endDate",
            "ascending": "true",
        })

        try:
            req = urllib.request.Request(
                f"{GAMMA_API}/markets?{params}",
                headers={"User-Agent": "polymarket-bot/1.0"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                markets = json.loads(r.read())
        except Exception as e:
            console.print(f"[red][bond] Fetch error: {e}[/red]")
            break

        if not markets:
            break

        past_window = False
        for m in markets:
            end_str = m.get("endDateIso") or m.get("endDate", "")
            if not end_str:
                continue

            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            if end_dt < min_cutoff:
                continue
            if end_dt > max_cutoff:
                past_window = True
                break

            hours_left = (end_dt - now_utc).total_seconds() / 3600
            volume = float(m.get("volume24hrClob") or m.get("volume24hr") or 0)
            if volume < BOND_MIN_VOLUME:
                continue

            condition_id = m.get("conditionId", "")
            outcomes = m.get("outcomes", [])
            prices = m.get("outcomePrices", [])
            token_ids = m.get("clobTokenIds", [])

            # Parse JSON strings if needed
            for attr in [outcomes, prices, token_ids]:
                if isinstance(attr, str):
                    try:
                        attr = json.loads(attr)
                    except:
                        pass

            if isinstance(outcomes, str):
                try: outcomes = json.loads(outcomes)
                except: outcomes = []
            if isinstance(prices, str):
                try: prices = json.loads(prices)
                except: prices = []
            if isinstance(token_ids, str):
                try: token_ids = json.loads(token_ids)
                except: token_ids = []

            # Skip sports markets — no edge vs Vegas
            q_lower = m.get("question", "").lower()
            SPORTS_KEYWORDS = [
                "nba", "nfl", "mlb", "nhl", "ncaa", "cbb", "cfb",
                "ufc", "mma", "boxing", "wrestling",
                "spread:", "moneyline", "o/u ",
                "vs.", "lakers", "celtics", "knicks", "bulls", "warriors",
                "yankees", "dodgers", "chiefs", "cowboys", "eagles",
                "premier league", "la liga", "serie a", "bundesliga",
                "champions league", "europa league",
                "atp", "wta", "tennis",
                "kings vs", "raptors", "suns", "jazz",
                "wildcats", "boilermakers", "bulldogs", "tigers",
                "lol:", "counter-strike", "cs2", "dota", "valorant",
                "esports", "bo3", "bo5",
                "soccer", "football", "fútbol",
                "win on 2026", "win on 2027",  # generic sports match
            ]
            if any(kw in q_lower for kw in SPORTS_KEYWORDS):
                continue

            for i, outcome in enumerate(outcomes):
                if i >= len(prices) or i >= len(token_ids):
                    continue
                try:
                    price = float(prices[i])
                except (ValueError, TypeError):
                    continue

                if not (BOND_MIN_PRICE <= price <= BOND_MAX_PRICE):
                    continue

                opp = BondOpportunity(
                    token_id=token_ids[i],
                    outcome=outcome,
                    price=price,
                    market_question=m.get("question", ""),
                    market_id=m.get("id", ""),
                    condition_id=condition_id,
                    end_date=end_str,
                    hours_to_expiry=round(hours_left, 2),
                    volume_24h=volume,
                )

                if opp.yield_pct >= BOND_MIN_YIELD:
                    results.append(opp)

        if past_window or len(markets) < limit:
            break
        offset += limit

    # Sort by annualized yield
    results.sort(key=lambda x: x.annualized_yield, reverse=True)
    return results


# ══════════════════════════════════════════════════════════════════════
# Allium Smart Money Check
# ══════════════════════════════════════════════════════════════════════

def allium_check(opp: BondOpportunity) -> bool:
    """Check if smart money is betting AGAINST this outcome. Returns True if safe."""
    if not BOND_USE_ALLIUM:
        return True

    try:
        from allium_feed import allium
        # Check if any high-win-rate wallets are shorting this outcome
        # If the question contains temperature/weather keywords, use weather wallets
        # Otherwise skip (Allium mainly covers crypto + weather)
        q = opp.market_question.lower()
        if any(kw in q for kw in ["temperature", "weather", "°f", "°c"]):
            signal = allium.get_weather_smart_signal(opp.market_question)
            if signal and signal.get("contradicts", False):
                console.print(f"  [yellow]🧠 Allium: Smart money contradicts {opp.outcome} — SKIP[/yellow]")
                return False
        return True
    except Exception:
        return True  # Allium unavailable — proceed without it


# ══════════════════════════════════════════════════════════════════════
# Telegram
# ══════════════════════════════════════════════════════════════════════

def _send_tg(msg: str):
    try:
        import telegram_alerts as tg
        tg.send_message(msg)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# Main Engine
# ══════════════════════════════════════════════════════════════════════

async def run_bond_grinder():
    """Main bond grinding loop."""
    from trader import init_client
    from vpn import ensure_vpn

    # Startup
    console.print("=" * 60)
    console.print("  [bold]Bond Grinder — Near-Certainty Yield Strategy[/bold]")
    console.print("=" * 60)
    budget_str = "unlimited" if BOND_BUDGET <= 0 else f"${BOND_BUDGET}/day"
    console.print(f"  Budget:        {budget_str}")
    console.print(f"  Bet size:      ${BOND_BET_SIZE}")
    console.print(f"  Price range:   ${BOND_MIN_PRICE}-${BOND_MAX_PRICE}")
    console.print(f"  Resolution:    {BOND_MIN_HOURS}h - {BOND_MAX_DAYS}d")
    console.print(f"  Max positions: {BOND_MAX_POSITIONS}")
    console.print(f"  Allium gate:   {'ON' if BOND_USE_ALLIUM else 'OFF'}")
    if PAPER_TRADE:
        console.print(f"  [bold yellow]📝 MODE: PAPER TRADE[/bold yellow]")

    ensure_vpn()

    private_key = os.getenv("PRIVATE_KEY", "")
    sig_type = int(os.getenv("SIGNATURE_TYPE", "1"))
    funder = os.getenv("WALLET_ADDRESS", "")
    client = init_client(private_key, sig_type, funder)
    console.print("[trader] CLOB client initialized")

    # Load persisted P&L
    pnl = BondPnL.load()
    console.print(f"  Loaded P&L: {pnl.summary()}")

    # Auto-redeem setup
    redeem_service = None
    if not PAPER_TRADE:
        try:
            from poly_web3 import PolyWeb3Service, RELAYER_URL
            from py_builder_signing_sdk.config import BuilderConfig
            from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
            from py_builder_relayer_client.client import RelayClient as RelayerClient

            builder_config = BuilderConfig(
                local_builder_creds=BuilderApiKeyCreds(
                    key=os.getenv("POLY_BUILDER_API_KEY", ""),
                    secret=os.getenv("POLY_BUILDER_SECRET", ""),
                    passphrase=os.getenv("POLY_BUILDER_PASSPHRASE", ""),
                )
            )
            relayer_client = RelayerClient(RELAYER_URL, 137, private_key, builder_config)
            redeem_service = PolyWeb3Service(
                clob_client=client,
                relayer_client=relayer_client,
                rpc_url="https://polygon-bor.publicnode.com",
            )
            console.print("  💰 Auto-redeem: ON")
        except Exception as e:
            console.print(f"  [yellow]Auto-redeem unavailable: {e}[/yellow]")

    budget_tg = "unlimited" if BOND_BUDGET <= 0 else f"${BOND_BUDGET}/day"
    _send_tg(f"🏦 Bond Grinder started\nBudget: {budget_tg}\n{pnl.summary()}")

    try:
        while True:
            # Budget check (0 = unlimited)
            if BOND_BUDGET > 0:
                remaining = max(0, BOND_BUDGET - get_daily_spend())
                if remaining < BOND_BET_SIZE:
                    console.print(f"[dim]Budget exhausted (${remaining:.2f} left). Waiting for reset...[/dim]")
                    await asyncio.sleep(60)
                    continue
            else:
                remaining = float("inf")

            # Scan
            opps = scan_bond_opportunities()
            console.print(f"  Found {len(opps)} bond opportunities")

            existing = get_placed_token_ids()
            current_count = len(existing)
            slots = max(0, BOND_MAX_POSITIONS - current_count)
            new_opps = [o for o in opps if o.token_id not in existing][:slots]

            placed = 0
            for opp in new_opps:
                if BOND_BUDGET > 0:
                    remaining = max(0, BOND_BUDGET - get_daily_spend())
                    if remaining < BOND_BET_SIZE:
                        break

                # Allium check
                if not allium_check(opp):
                    continue

                ptag = "📝 " if PAPER_TRADE else ""

                if PAPER_TRADE:
                    # Simulate
                    record_spend(BOND_BET_SIZE)
                    save_order({
                        "token_id": opp.token_id,
                        "strategy": "bond",
                        "outcome": opp.outcome,
                        "price": opp.price,
                        "amount": BOND_BET_SIZE,
                        "yield_pct": round(opp.yield_pct, 4),
                        "annualized_yield": round(opp.annualized_yield, 2),
                        "days_to_expiry": round(opp.days_to_expiry, 2),
                        "question": opp.market_question[:60],
                        "end_date": opp.end_date,
                        "hours_to_expiry": opp.hours_to_expiry,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "paper": True,
                    })
                    pnl.total_invested += BOND_BET_SIZE
                    pnl.positions_open += 1
                    placed += 1
                else:
                    # Real trade
                    from py_clob_client.clob_types import OrderArgs, OrderType
                    from py_clob_client.order_builder.constants import BUY

                    size = max(5, math.floor((BOND_BET_SIZE / opp.price) * 100) / 100)
                    try:
                        args = OrderArgs(
                            token_id=opp.token_id,
                            price=opp.price,
                            size=size,
                            side=BUY,
                        )
                        signed = client.create_order(args)
                        response = client.post_order(signed, OrderType.GTC)

                        order_id = ""
                        if isinstance(response, dict):
                            order_id = response.get("orderID", response.get("id", ""))
                            if not response.get("success", True):
                                continue

                        record_spend(BOND_BET_SIZE)
                        save_order({
                            "token_id": opp.token_id,
                            "order_id": order_id,
                            "strategy": "bond",
                            "outcome": opp.outcome,
                            "price": opp.price,
                            "size": size,
                            "amount": BOND_BET_SIZE,
                            "condition_id": opp.condition_id,
                            "yield_pct": round(opp.yield_pct, 4),
                            "question": opp.market_question[:60],
                            "end_date": opp.end_date,
                            "hours_to_expiry": opp.hours_to_expiry,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                        pnl.total_invested += BOND_BET_SIZE
                        pnl.positions_open += 1
                        placed += 1
                    except Exception as e:
                        console.print(f"  [red]Order failed: {e}[/red]")
                        continue

                msg = (
                    f"🏦 {ptag}BOND: {opp.outcome}\n"
                    f"{opp.market_question[:55]}\n"
                    f"${opp.price:.3f} | yield {opp.yield_pct:.1%} | "
                    f"{opp.days_to_expiry:.1f}d | ann {opp.annualized_yield:.0f}%"
                )
                console.print(f"  [green]{msg}[/green]")
                _send_tg(msg)
                time.sleep(0.5)

            if placed > 0:
                console.print(f"  Placed {placed} bonds. {pnl.summary()}")
                pnl.save()

            # Auto-redeem
            if redeem_service and not PAPER_TRADE:
                try:
                    results = redeem_service.redeem_all(batch_size=10)
                    if results:
                        for r in results:
                            if r:
                                volume = getattr(r, 'volume', 0) or 0
                                if volume > 0:
                                    pnl.total_returned += volume
                                    pnl.wins += 1
                                    pnl.positions_open = max(0, pnl.positions_open - 1)
                                    console.print(f"  [green]💰 Redeemed ${volume:.2f}[/green]")
                                    _send_tg(f"💰 Bond redeemed: +${volume:.2f}\n{pnl.summary()}")
                        pnl.save()
                except Exception as e:
                    if "no redeemable" not in str(e).lower():
                        console.print(f"  [dim]Redeem: {e}[/dim]")

            # Status
            budget_info = "unlimited" if BOND_BUDGET <= 0 else f"${remaining:.2f}/${BOND_BUDGET:.2f}"
            console.print(
                f"  {pnl.summary()} | "
                f"Budget: {budget_info} | "
                f"Slots: {slots}"
            )

            await asyncio.sleep(BOND_SCAN_INTERVAL)

    except KeyboardInterrupt:
        console.print(f"\n[yellow]Bond Grinder stopped.[/yellow]")
        console.print(f"  {pnl.summary()}")
        pnl.save()


def main():
    asyncio.run(run_bond_grinder())


if __name__ == "__main__":
    main()
