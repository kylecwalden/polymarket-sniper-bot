"""
ai_mispricing_scanner.py — Automated AI Mispricing Detection

Scans Polymarket for uncertain markets (30-70% probability), uses Exa deep
reasoning to estimate true probabilities, and alerts via Telegram when
market price diverges from AI estimate by >10 points.

Flow:
  1. Fetch top 20 uncertain markets from Gamma API
  2. For each, call Exa deep reasoning to get AI probability estimate
  3. Compare AI estimate vs market price
  4. If edge > 10 points → Telegram alert with thesis
  5. User approves via Telegram → bot places trade
  6. Track all trades in data/ai_mispricing_trades.json

Run modes:
  python ai_mispricing_scanner.py scan     # One-shot scan + alert
  python ai_mispricing_scanner.py auto     # Run daily at 9am UTC
  python ai_mispricing_scanner.py approve  # Process pending approvals
"""

import asyncio
import json
import math
import os
import re
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from rich.console import Console

load_dotenv()
console = Console()

GAMMA_API = "https://gamma-api.polymarket.com"
EXA_API = "https://api.exa.ai/search"

# ══════════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════════

EXA_API_KEY = os.getenv("EXA_API_KEY", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")

AI_MIN_EDGE = float(os.getenv("AI_MIN_EDGE", "0.10"))           # 10 point minimum edge
AI_BET_SIZE_MIN = float(os.getenv("AI_BET_SIZE_MIN", "2.50"))    # Min USDC per trade
AI_BET_SIZE_MAX = float(os.getenv("AI_BET_SIZE_MAX", "5.00"))    # Max USDC per trade
AI_MAX_MARKETS_PER_SCAN = int(os.getenv("AI_MAX_MARKETS", "20"))  # Markets to analyze per scan
AI_MIN_PROB = float(os.getenv("AI_MIN_PROB", "0.30"))           # Only scan 30-70% markets
AI_MAX_PROB = float(os.getenv("AI_MAX_PROB", "0.70"))
AI_MIN_VOLUME = float(os.getenv("AI_MIN_VOLUME", "1000"))       # $1k+ volume
AI_MAX_DAYS = float(os.getenv("AI_MAX_RESOLUTION_DAYS", "30"))  # Resolve within 30 days
AI_SCAN_INTERVAL = int(os.getenv("AI_SCAN_INTERVAL_HOURS", "24"))  # Scan every 24h

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
TRADES_FILE = DATA_DIR / "ai_mispricing_trades.json"
PENDING_FILE = DATA_DIR / "ai_mispricing_pending.json"

PAPER_TRADE = os.getenv("PAPER_TRADE", "false").lower() == "true"


def calc_bet_size(edge: float) -> float:
    """Scale bet size with edge: 10% edge → $2.50, 30%+ edge → $5.00."""
    # Linear scale between min and max based on edge strength
    edge_floor = AI_MIN_EDGE        # 0.10
    edge_ceil = 0.30                # 30% edge = max bet
    t = min(1.0, max(0.0, (edge - edge_floor) / (edge_ceil - edge_floor)))
    return round(AI_BET_SIZE_MIN + t * (AI_BET_SIZE_MAX - AI_BET_SIZE_MIN), 2)


# ══════════════════════════════════════════════════════════════════════
# Data
# ══════════════════════════════════════════════════════════════════════

@dataclass
class MispricingOpportunity:
    market_id: str
    condition_id: str
    token_id: str
    question: str
    outcome: str
    market_price: float
    ai_probability: float
    edge: float
    thesis: str
    resolution_date: str
    resolution_criteria: str
    sources: list
    days_to_resolution: float

    def to_dict(self):
        return {
            "market_id": self.market_id,
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "question": self.question,
            "outcome": self.outcome,
            "market_price": self.market_price,
            "ai_probability": self.ai_probability,
            "edge": self.edge,
            "thesis": self.thesis,
            "resolution_date": self.resolution_date,
            "resolution_criteria": self.resolution_criteria,
            "sources": self.sources[:3],
            "days_to_resolution": self.days_to_resolution,
        }


def load_trades() -> list:
    if TRADES_FILE.exists():
        try:
            return json.loads(TRADES_FILE.read_text())
        except Exception:
            return []
    return []


def save_trades(trades: list):
    TRADES_FILE.write_text(json.dumps(trades, indent=2))


def load_pending() -> list:
    if PENDING_FILE.exists():
        try:
            return json.loads(PENDING_FILE.read_text())
        except Exception:
            return []
    return []


def save_pending(pending: list):
    PENDING_FILE.write_text(json.dumps(pending, indent=2))


def get_placed_market_ids() -> set:
    trades = load_trades()
    return {t["market_id"] for t in trades}


# ══════════════════════════════════════════════════════════════════════
# Market Scanner — Find uncertain markets
# ══════════════════════════════════════════════════════════════════════

def fetch_uncertain_markets() -> list[dict]:
    """Fetch active markets with 30-70% probability (most uncertain)."""
    now_utc = datetime.now(timezone.utc)
    max_cutoff = now_utc + timedelta(days=AI_MAX_DAYS)
    results = []
    offset = 0
    limit = 100

    while len(results) < AI_MAX_MARKETS_PER_SCAN * 3:  # Fetch extra, filter down
        params = urllib.parse.urlencode({
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
            "order": "volume24hr",
            "ascending": "false",  # Highest volume first
        })

        try:
            req = urllib.request.Request(
                f"{GAMMA_API}/markets?{params}",
                headers={"User-Agent": "polymarket-bot/1.0"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                markets = json.loads(r.read())
        except Exception as e:
            console.print(f"[red]Fetch error: {e}[/red]")
            break

        if not markets:
            break

        for m in markets:
            # Check end date
            end_str = m.get("endDateIso") or m.get("endDate", "")
            if not end_str:
                continue

            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            if end_dt > max_cutoff or end_dt < now_utc:
                continue

            days_left = (end_dt - now_utc).total_seconds() / 86400

            # Check volume
            volume = float(m.get("volume24hrClob") or m.get("volume24hr") or 0)
            if volume < AI_MIN_VOLUME:
                continue

            # Check probability range
            outcomes = m.get("outcomes", [])
            prices = m.get("outcomePrices", [])
            token_ids = m.get("clobTokenIds", [])

            for attr_name in ["outcomes", "prices", "token_ids"]:
                attr = locals().get(attr_name)
                if isinstance(attr, str):
                    try:
                        locals()[attr_name] = json.loads(attr)
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

            for i, outcome in enumerate(outcomes):
                if i >= len(prices) or i >= len(token_ids):
                    continue
                try:
                    price = float(prices[i])
                except (ValueError, TypeError):
                    continue

                # Only uncertain markets
                if not (AI_MIN_PROB <= price <= AI_MAX_PROB):
                    continue

                results.append({
                    "market_id": m.get("id", ""),
                    "condition_id": m.get("conditionId", ""),
                    "token_id": token_ids[i],
                    "question": m.get("question", ""),
                    "outcome": outcome,
                    "price": price,
                    "volume_24h": volume,
                    "end_date": end_str,
                    "days_left": round(days_left, 1),
                    "description": m.get("description", ""),
                    "resolution_source": m.get("resolutionSource", ""),
                })

        offset += limit
        if len(markets) < limit:
            break

    # Sort by volume, take top N
    results.sort(key=lambda x: x["volume_24h"], reverse=True)
    return results[:AI_MAX_MARKETS_PER_SCAN]


# ══════════════════════════════════════════════════════════════════════
# Exa Deep Reasoning — AI Probability Estimation
# ══════════════════════════════════════════════════════════════════════

def exa_deep_reasoning(question: str, resolution_criteria: str = "") -> dict:
    """
    Call Exa deep reasoning to estimate probability of a market question.
    Returns: {"probability": float, "thesis": str, "sources": list}
    """
    if not EXA_API_KEY:
        return {"probability": None, "thesis": "No Exa API key", "sources": []}

    prompt = (
        f"What is the probability that: {question}\n\n"
    )
    if resolution_criteria:
        prompt += f"Resolution criteria: {resolution_criteria}\n\n"
    prompt += (
        "Analyze all available evidence and provide:\n"
        "1. A specific probability estimate (0-100%)\n"
        "2. Key factors supporting your estimate\n"
        "3. What could change this probability\n"
        "Give a clear numerical probability."
    )

    body = json.dumps({
        "query": prompt,
        "type": "deep-reasoning",
        "numResults": 5,
        "useAutoprompt": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        EXA_API,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": EXA_API_KEY,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        console.print(f"[red]Exa API error: {e}[/red]")
        return {"probability": None, "thesis": f"Exa error: {e}", "sources": []}

    # Extract the reasoning summary
    summary = data.get("summary", "") or data.get("searchSummary", "") or ""
    results = data.get("results", [])
    sources = [r.get("url", "") for r in results[:5] if r.get("url")]

    # Parse probability from Exa's summary
    probability = _extract_probability(summary)

    return {
        "probability": probability,
        "thesis": summary[:500] if summary else "No analysis available",
        "sources": sources,
    }


def _extract_probability(text: str) -> Optional[float]:
    """Extract a probability value from Exa's reasoning text."""
    if not text:
        return None

    # Look for patterns like "70%", "probability of 0.65", "65 percent", etc.
    patterns = [
        r'(\d{1,3}(?:\.\d+)?)\s*%',                    # 70%, 65.5%
        r'probability\s*(?:of\s*)?(\d*\.\d+)',          # probability of 0.65
        r'(\d{1,3})\s*percent',                         # 70 percent
        r'estimate[ds]?\s*(?:at\s*)?(\d{1,3}(?:\.\d+)?)\s*%',  # estimated at 70%
        r'likelihood\s*(?:of\s*)?(\d{1,3}(?:\.\d+)?)\s*%',     # likelihood of 70%
    ]

    probabilities = []
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for m in matches:
            try:
                val = float(m)
                if val > 1 and val <= 100:
                    probabilities.append(val / 100)
                elif val <= 1:
                    probabilities.append(val)
            except ValueError:
                continue

    if probabilities:
        # Take the most commonly mentioned probability, or the last one
        # (Exa tends to give the final answer at the end)
        return probabilities[-1]

    return None


# ══════════════════════════════════════════════════════════════════════
# Telegram Alerts
# ══════════════════════════════════════════════════════════════════════

def _send_tg(msg: str):
    try:
        import telegram_alerts as tg
        tg.send_message(msg)
    except Exception:
        pass


def alert_mispricing(opp: MispricingOpportunity, idx: int):
    """Send a mispricing alert to Telegram."""
    direction = "BUY" if opp.ai_probability > opp.market_price else "SELL"
    bet = calc_bet_size(opp.edge)
    _send_tg(
        f"🔍 *AI MISPRICING #{idx}*\n\n"
        f"*{opp.question[:80]}*\n"
        f"Outcome: {opp.outcome}\n\n"
        f"📊 Market: {opp.market_price:.0%} | AI: {opp.ai_probability:.0%}\n"
        f"🎯 Edge: {opp.edge:.0%} → {direction}\n"
        f"📅 Resolves: {opp.days_to_resolution:.0f} days\n\n"
        f"💡 {opp.thesis[:200]}\n\n"
        f"Reply `approve {idx}` to place ${bet:.2f} trade"
    )


def alert_scan_summary(total_scanned: int, opportunities: int, pending: int):
    """Summary after a full scan."""
    _send_tg(
        f"🤖 *AI MISPRICING SCAN COMPLETE*\n\n"
        f"Markets analyzed: {total_scanned}\n"
        f"Mispricing found: {opportunities}\n"
        f"Pending approval: {pending}\n\n"
        f"Reply `approve <#>` to place trade\n"
        f"Reply `approve all` for all"
    )


# ══════════════════════════════════════════════════════════════════════
# Scanner Core
# ══════════════════════════════════════════════════════════════════════

def run_scan() -> list[MispricingOpportunity]:
    """Full scan: fetch markets → Exa deep reasoning → find mispricings."""
    console.print("[bold]🔍 AI Mispricing Scan Starting...[/bold]")

    if not EXA_API_KEY:
        console.print("[red]ERROR: Set EXA_API_KEY in .env[/red]")
        return []

    # Fetch uncertain markets
    markets = fetch_uncertain_markets()
    console.print(f"  Found {len(markets)} uncertain markets to analyze")

    already_placed = get_placed_market_ids()
    opportunities = []
    pending = load_pending()
    existing_pending_ids = {p["market_id"] for p in pending}

    for i, m in enumerate(markets):
        # Skip markets we already have trades or pending on
        if m["market_id"] in already_placed or m["market_id"] in existing_pending_ids:
            console.print(f"  [{i+1}/{len(markets)}] SKIP (already tracked): {m['question'][:50]}")
            continue

        console.print(f"  [{i+1}/{len(markets)}] Analyzing: {m['question'][:60]}...")

        # Call Exa deep reasoning
        result = exa_deep_reasoning(
            question=m["question"],
            resolution_criteria=m.get("description", "")[:500],
        )

        ai_prob = result["probability"]
        if ai_prob is None:
            console.print(f"    [yellow]Could not extract probability — skip[/yellow]")
            continue

        # Calculate edge
        market_price = m["price"]
        edge = abs(ai_prob - market_price)

        console.print(
            f"    Market: {market_price:.0%} | AI: {ai_prob:.0%} | "
            f"Edge: {edge:.0%} {'✅' if edge >= AI_MIN_EDGE else '❌'}"
        )

        if edge < AI_MIN_EDGE:
            continue

        # Build opportunity
        opp = MispricingOpportunity(
            market_id=m["market_id"],
            condition_id=m["condition_id"],
            token_id=m["token_id"],
            question=m["question"],
            outcome=m["outcome"],
            market_price=market_price,
            ai_probability=ai_prob,
            edge=round(edge, 4),
            thesis=result["thesis"],
            resolution_date=m["end_date"],
            resolution_criteria=m.get("description", "")[:300],
            sources=result["sources"],
            days_to_resolution=m["days_left"],
        )
        opportunities.append(opp)

        # Rate limit Exa calls
        time.sleep(2)

    # Add to pending
    for idx, opp in enumerate(opportunities):
        pending_entry = opp.to_dict()
        pending_entry["scan_time"] = datetime.now(timezone.utc).isoformat()
        pending_entry["approved"] = False
        pending_entry["idx"] = len(pending) + 1
        pending.append(pending_entry)

        # Alert each one
        alert_mispricing(opp, pending_entry["idx"])

    save_pending(pending)

    # Summary
    console.print()
    console.print(f"[bold green]Scan complete: {len(opportunities)} mispricings found[/bold green]")
    for opp in opportunities:
        direction = "BUY" if opp.ai_probability > opp.market_price else "AVOID"
        console.print(
            f"  {direction} {opp.outcome} @ {opp.market_price:.0%} "
            f"(AI: {opp.ai_probability:.0%}, edge: {opp.edge:.0%}) | "
            f"{opp.question[:50]}"
        )

    alert_scan_summary(len(markets), len(opportunities), len(pending))

    return opportunities


# ══════════════════════════════════════════════════════════════════════
# Trade Placement
# ══════════════════════════════════════════════════════════════════════

def approve_and_place(indices: list[int] = None, approve_all: bool = False):
    """Approve pending trades and place orders."""
    from trader import init_client
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    pending = load_pending()
    trades = load_trades()

    to_place = []
    if approve_all:
        to_place = [p for p in pending if not p.get("approved")]
    elif indices:
        for p in pending:
            if p.get("idx") in indices and not p.get("approved"):
                to_place.append(p)

    if not to_place:
        console.print("[yellow]No pending trades to approve.[/yellow]")
        return

    client = init_client(PRIVATE_KEY, SIGNATURE_TYPE, WALLET_ADDRESS or None)

    for p in to_place:
        # Determine trade direction
        if p["ai_probability"] > p["market_price"]:
            # AI thinks it's higher → BUY this outcome
            side = BUY
            price = p["market_price"]
            token_id = p["token_id"]
        else:
            # AI thinks it's lower → skip (we can only buy on Polymarket CLOB)
            console.print(f"  [yellow]SKIP {p['outcome']} — AI says lower, can't short[/yellow]")
            p["approved"] = True
            p["status"] = "skipped_no_short"
            continue

        bet_size = calc_bet_size(p.get("edge", AI_MIN_EDGE))
        size = max(5, math.floor((bet_size / price) * 100) / 100)

        if PAPER_TRADE:
            console.print(f"  📝 PAPER: {p['outcome']} @ ${price:.2f} ({size} shares, ${bet_size})")
            trade_entry = {
                **p,
                "approved": True,
                "status": "paper_placed",
                "bet_size": bet_size,
                "shares": size,
                "placed_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            try:
                args = OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=side,
                )
                signed = client.create_order(args)
                response = client.post_order(signed, OrderType.GTC)

                order_id = ""
                if isinstance(response, dict):
                    order_id = response.get("orderID", response.get("id", ""))

                console.print(f"  ✅ PLACED: {p['outcome']} @ ${price:.2f} ({size} shares, ${bet_size})")

                trade_entry = {
                    **p,
                    "approved": True,
                    "status": "placed",
                    "order_id": order_id,
                    "bet_size": bet_size,
                    "shares": size,
                    "placed_at": datetime.now(timezone.utc).isoformat(),
                }

                _send_tg(
                    f"🎯 *AI TRADE PLACED*\n\n"
                    f"{p['question'][:60]}\n"
                    f"BUY {p['outcome']} @ {price:.0%}\n"
                    f"AI says: {p['ai_probability']:.0%} | Edge: {p['edge']:.0%}\n"
                    f"Size: ${bet_size:.2f} ({size} shares)"
                )

            except Exception as e:
                console.print(f"  [red]Order failed: {e}[/red]")
                trade_entry = {
                    **p,
                    "approved": True,
                    "status": f"failed: {e}",
                    "placed_at": datetime.now(timezone.utc).isoformat(),
                }

        trades.append(trade_entry)
        p["approved"] = True
        time.sleep(1)

    save_pending(pending)
    save_trades(trades)

    placed = sum(1 for t in trades if t.get("status") in ("placed", "paper_placed"))
    console.print(f"[green]Approved {len(to_place)} trades. Total: {len(trades)}[/green]")


# ══════════════════════════════════════════════════════════════════════
# P&L Tracking
# ══════════════════════════════════════════════════════════════════════

def get_ai_pnl() -> dict:
    """Get AI mispricing P&L summary."""
    trades = load_trades()
    total_invested = 0
    total_returned = 0
    wins = 0
    losses = 0
    open_positions = 0

    for t in trades:
        status = t.get("status", "")
        bet = t.get("bet_size", AI_BET_SIZE_MIN)

        if status in ("placed", "paper_placed"):
            total_invested += bet
            if t.get("resolved"):
                if t.get("won"):
                    total_returned += t.get("payout", bet / t.get("market_price", 0.5))
                    wins += 1
                else:
                    losses += 1
            else:
                open_positions += 1

    return {
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "open_positions": open_positions,
        "total_invested": total_invested,
        "total_returned": total_returned,
        "net_pnl": total_returned - total_invested,
    }


def pnl_summary() -> str:
    p = get_ai_pnl()
    return (
        f"🔍 AI Mispricing: ${p['net_pnl']:+.2f} | "
        f"{p['wins']}W/{p['losses']}L | "
        f"Open: {p['open_positions']} | "
        f"Invested: ${p['total_invested']:.2f}"
    )


# ══════════════════════════════════════════════════════════════════════
# Auto-run loop (for systemd / cron)
# ══════════════════════════════════════════════════════════════════════

async def run_auto():
    """Run scan automatically every AI_SCAN_INTERVAL hours."""
    console.print("[bold]🤖 AI Mispricing Scanner — Auto Mode[/bold]")
    console.print(f"  Scan interval: {AI_SCAN_INTERVAL}h")
    console.print(f"  Min edge: {AI_MIN_EDGE:.0%}")
    console.print(f"  Bet size: ${AI_BET_SIZE}")
    console.print(f"  Max markets per scan: {AI_MAX_MARKETS_PER_SCAN}")
    console.print()

    _send_tg(
        f"🤖 AI Mispricing Scanner started\n"
        f"Scanning every {AI_SCAN_INTERVAL}h | Min edge: {AI_MIN_EDGE:.0%}"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            console.print(f"[red]Scan error: {e}[/red]")
            _send_tg(f"❌ AI scan error: {e}")

        console.print(f"[dim]Next scan in {AI_SCAN_INTERVAL} hours...[/dim]")
        await asyncio.sleep(AI_SCAN_INTERVAL * 3600)


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

def main():
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"

    if cmd == "scan":
        run_scan()
    elif cmd == "auto":
        asyncio.run(run_auto())
    elif cmd == "approve":
        if len(sys.argv) > 2:
            if sys.argv[2] == "all":
                approve_and_place(approve_all=True)
            else:
                indices = [int(x) for x in sys.argv[2:]]
                approve_and_place(indices=indices)
        else:
            console.print("Usage: python ai_mispricing_scanner.py approve <idx> [idx2...] | all")
    elif cmd == "pnl":
        console.print(pnl_summary())
    elif cmd == "pending":
        pending = load_pending()
        unapproved = [p for p in pending if not p.get("approved")]
        if not unapproved:
            console.print("[yellow]No pending trades.[/yellow]")
        else:
            for p in unapproved:
                console.print(
                    f"  #{p['idx']}: {p['outcome']} | "
                    f"Market: {p['market_price']:.0%} → AI: {p['ai_probability']:.0%} | "
                    f"Edge: {p['edge']:.0%} | {p['question'][:50]}"
                )
    else:
        console.print(f"[red]Unknown command: {cmd}[/red]")
        console.print()
        console.print("Usage:")
        console.print("  python ai_mispricing_scanner.py scan       # Run one scan")
        console.print("  python ai_mispricing_scanner.py auto       # Auto-scan loop")
        console.print("  python ai_mispricing_scanner.py approve 1  # Approve trade #1")
        console.print("  python ai_mispricing_scanner.py approve all")
        console.print("  python ai_mispricing_scanner.py pending    # Show pending")
        console.print("  python ai_mispricing_scanner.py pnl        # Show P&L")


if __name__ == "__main__":
    main()
