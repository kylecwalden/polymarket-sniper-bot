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
            # Skip sports markets — no edge vs Vegas-efficient pricing
            question = (m.get("question", "") + " " + m.get("groupItemTitle", "")).lower()
            sports_keywords = [
                "vs.", "vs ", " ml", "spread:", "o/u ", "over/under",
                "moneyline", "nba", "nfl", "mlb", "nhl", "ufc", "mma",
                "ncaa", "premier league", "la liga", "serie a", "bundesliga",
                "champions league", "copa", "world cup", "tennis", "atp", "wta",
                "boxing", "bellator", "pfl", "wnba", "college football",
                "college basketball", "march madness", "super bowl",
                "stanley cup", "world series", "playoffs",
                "lakers", "celtics", "warriors", "yankees", "dodgers",
                "chiefs", "eagles", "cowboys", "49ers",
                "fight night", "bantamweight", "middleweight", "welterweight",
                "heavyweight", "lightweight", "featherweight", "flyweight",
            ]
            if any(kw in question for kw in sports_keywords):
                continue

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
        "query": prompt[:1500],  # Cap query length
        "type": "deep-reasoning",
        "numResults": 5,
    }).encode("utf-8")

    req = urllib.request.Request(
        EXA_API,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": EXA_API_KEY,
            "User-Agent": "polymarket-bot/1.0",
        },
    )

    # Retry with backoff
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode())
            break
        except Exception as e:
            last_err = e
            console.print(f"[yellow]Exa attempt {attempt+1}/3: {e}[/yellow]")
            time.sleep(5 * (attempt + 1))  # 5s, 10s, 15s backoff
    else:
        console.print(f"[red]Exa API failed after 3 attempts: {last_err}[/red]")
        return {"probability": None, "thesis": f"Exa error: {last_err}", "sources": []}

    # Extract the reasoning summary — deep-reasoning uses output.content
    output = data.get("output", {})
    summary = ""
    if isinstance(output, dict):
        summary = output.get("content", "") or ""
    if not summary:
        summary = data.get("summary", "") or data.get("searchSummary", "") or ""

    # Sources: from output.grounding citations + results
    sources = []
    if isinstance(output, dict):
        for g in output.get("grounding", []):
            for c in g.get("citations", []):
                url = c.get("url", "")
                if url and url not in sources:
                    sources.append(url)
    results = data.get("results", [])
    for r in results[:5]:
        url = r.get("url", "")
        if url and url not in sources:
            sources.append(url)

    # Parse probability from Exa's summary
    probability = _extract_probability(summary)

    return {
        "probability": probability,
        "thesis": summary[:500] if summary else "No analysis available",
        "sources": sources[:5],
    }


def _extract_probability(text: str) -> Optional[float]:
    """Extract a probability value from Exa's reasoning text."""
    if not text:
        return None

    # Priority patterns — look for explicit probability statements first
    priority_patterns = [
        r'(\d{1,2}(?:\.\d+)?)\s*%\s*probability',           # 35% probability
        r'probability\s*(?:of\s*)?(?:approximately\s*)?(\d{1,2}(?:\.\d+)?)\s*%',  # probability of 35%
        r'probability[:\s]+(\d*\.\d+)',                       # probability: 0.35
        r'estimate[ds]?\s*(?:at\s*)?(\d{1,2}(?:\.\d+)?)\s*%', # estimated at 35%
        r'likelihood\s*(?:of\s*)?(\d{1,2}(?:\.\d+)?)\s*%',    # likelihood of 35%
        r'(\d{1,2}(?:\.\d+)?)\s*percent\s*(?:probability|chance|likelihood)',  # 35 percent probability
    ]

    for pattern in priority_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for m in matches:
            try:
                val = float(m)
                if val > 1 and val <= 99:
                    return val / 100
                elif 0 < val <= 1:
                    return val
            except ValueError:
                continue

    # Fallback: find all percentages, filter out prices/dollar amounts
    # Avoid matching "$100,000" or "100K" — only match standalone percentages
    pct_matches = re.finditer(r'(?<!\$)(?<!\d[,.])\b(\d{1,2}(?:\.\d+)?)\s*%', text)
    probabilities = []
    for m in pct_matches:
        try:
            val = float(m.group(1))
            # Only valid probability percentages (1-99%)
            if 1 <= val <= 99:
                probabilities.append(val / 100)
        except ValueError:
            continue

    if probabilities:
        # Take the last one — Exa tends to give final answer at end
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

    # Track which market_ids we've already flagged in THIS scan
    # to prevent taking both sides of the same market
    scanned_market_ids = set()

    for i, m in enumerate(markets):
        # Skip markets we already have trades or pending on
        if m["market_id"] in already_placed or m["market_id"] in existing_pending_ids:
            console.print(f"  [{i+1}/{len(markets)}] SKIP (already tracked): {m['question'][:50]}")
            continue

        # LEARNING: Never take both sides of the same market
        if m["market_id"] in scanned_market_ids:
            console.print(f"  [{i+1}/{len(markets)}] SKIP (other side already flagged): {m['outcome']}")
            continue

        console.print(f"  [{i+1}/{len(markets)}] Analyzing: {m['question'][:60]}...")

        # Call Exa deep reasoning — FIRST CALL
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
            f"    Call 1: Market: {market_price:.0%} | AI: {ai_prob:.0%} | "
            f"Edge: {edge:.0%} {'✅' if edge >= AI_MIN_EDGE else '❌'}"
        )

        if edge < AI_MIN_EDGE:
            continue

        # LEARNING: Only BUY direction (AI thinks it's worth MORE than market)
        # Can't short on Polymarket, so AVOID signals are useless
        if ai_prob <= market_price:
            console.print(f"    [yellow]AVOID direction (can't short) — skip[/yellow]")
            continue

        # LEARNING: Double-call verification — Exa can give wildly different
        # answers on the same question. Run a second call and require both
        # calls to agree within 15 points to confirm the signal.
        console.print(f"    Verifying with second Exa call...")
        time.sleep(3)  # Rate limit between calls

        result2 = exa_deep_reasoning(
            question=m["question"],
            resolution_criteria=m.get("description", "")[:500],
        )

        ai_prob2 = result2["probability"]
        if ai_prob2 is None:
            console.print(f"    [yellow]Verification call failed — skip[/yellow]")
            continue

        # Check consistency between two calls
        prob_diff = abs(ai_prob - ai_prob2)
        avg_prob = (ai_prob + ai_prob2) / 2
        avg_edge = abs(avg_prob - market_price)

        console.print(
            f"    Call 2: AI: {ai_prob2:.0%} | Diff: {prob_diff:.0%} | "
            f"Avg: {avg_prob:.0%} | Avg Edge: {avg_edge:.0%}"
        )

        if prob_diff > 0.15:
            console.print(f"    [red]❌ INCONSISTENT — calls disagree by {prob_diff:.0%} (>15%) — skip[/red]")
            continue

        if avg_edge < AI_MIN_EDGE:
            console.print(f"    [yellow]Average edge {avg_edge:.0%} below threshold — skip[/yellow]")
            continue

        if avg_prob <= market_price:
            console.print(f"    [yellow]Average says AVOID — skip[/yellow]")
            continue

        # Use averaged values for the final opportunity
        ai_prob = avg_prob
        edge = avg_edge
        result["thesis"] = f"[Verified 2x: {result['probability']:.0%} & {ai_prob2:.0%}] " + result["thesis"]

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
# Resolution Checker — Track wins/losses and alert via Telegram
# ══════════════════════════════════════════════════════════════════════

def check_resolutions():
    """Check if any open AI mispricing trades have resolved. Update P&L + alert."""
    trades = load_trades()
    if not trades:
        return

    open_trades = [t for t in trades if t.get("status") in ("placed", "paper_placed") and not t.get("resolved")]
    if not open_trades:
        console.print("  [dim]No open AI trades to check[/dim]")
        return

    console.print(f"  Checking {len(open_trades)} open AI mispricing trades...")
    updated = False

    for t in open_trades:
        condition_id = t.get("condition_id", "")
        if not condition_id:
            continue

        # Check market status via Gamma API
        try:
            req = urllib.request.Request(
                f"{GAMMA_API}/markets?conditionId={condition_id}",
                headers={"User-Agent": "polymarket-bot/1.0"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                markets = json.loads(r.read())
        except Exception as e:
            console.print(f"    [yellow]API error checking {t.get('outcome', '?')[:30]}: {e}[/yellow]")
            continue

        if not markets:
            continue

        market = markets[0]

        # STRICT resolution check — must have explicit resolved=True
        # AND end date must have passed AND prices must be 0 or 1 (not mid-range)
        is_resolved = False

        # Check if Gamma says it's resolved (not just closed)
        if market.get("resolved") == True:
            is_resolved = True

        # Double-check: end date must have passed
        if is_resolved:
            end_str = market.get("endDateIso") or market.get("endDate", "")
            if end_str:
                try:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    if end_dt > datetime.now(timezone.utc):
                        is_resolved = False  # End date hasn't passed yet
                except Exception:
                    pass

        # Triple-check: outcome prices should be 0 or 1 if truly resolved
        outcomes = market.get("outcomes", [])
        prices = market.get("outcomePrices", [])
        if isinstance(outcomes, str):
            try: outcomes = json.loads(outcomes)
            except: outcomes = []
        if isinstance(prices, str):
            try: prices = json.loads(prices)
            except: prices = []

        if is_resolved and prices:
            # All prices should be very close to 0.0 or 1.0
            all_terminal = True
            for p_str in prices:
                try:
                    p_val = float(p_str)
                    if 0.02 < p_val < 0.98:
                        all_terminal = False
                        break
                except (ValueError, TypeError):
                    pass
            if not all_terminal:
                console.print(f"    [yellow]Skipping {t.get('outcome', '?')[:30]} — prices not terminal yet[/yellow]")
                is_resolved = False

        if not is_resolved:
            continue

        # Market truly resolved — figure out if we won
        our_outcome = t.get("outcome", "")
        won = False
        for i, outcome_name in enumerate(outcomes):
            if outcome_name == our_outcome and i < len(prices):
                try:
                    final_price = float(prices[i])
                    # If final price is >= 0.98, it resolved YES (we won)
                    won = final_price >= 0.98
                except (ValueError, TypeError):
                    pass
                break

        # Update trade record
        t["resolved"] = True
        t["won"] = won
        t["resolved_at"] = datetime.now(timezone.utc).isoformat()

        bet_size = t.get("bet_size", AI_BET_SIZE_MIN)
        shares = t.get("shares", 0)

        if won:
            payout = shares  # Each winning share pays $1
            t["payout"] = payout
            profit = payout - bet_size
            console.print(f"    [green]✅ WIN: {our_outcome[:40]} → +${profit:.2f}[/green]")
            _send_tg(
                f"🎉 *AI MISPRICING WIN!*\n\n"
                f"{t.get('question', '')[:60]}\n"
                f"✅ {our_outcome} resolved YES\n"
                f"Bought @ {t.get('market_price', 0):.0%} | AI said: {t.get('ai_probability', 0):.0%}\n"
                f"💰 Profit: +${profit:.2f} ({shares:.1f} shares × $1)\n\n"
                f"{pnl_summary()}"
            )
        else:
            t["payout"] = 0
            console.print(f"    [red]❌ LOSS: {our_outcome[:40]} → -${bet_size:.2f}[/red]")
            _send_tg(
                f"❌ *AI MISPRICING LOSS*\n\n"
                f"{t.get('question', '')[:60]}\n"
                f"❌ {our_outcome} resolved NO\n"
                f"Lost: -${bet_size:.2f}\n"
                f"AI said {t.get('ai_probability', 0):.0%} but market was right\n\n"
                f"{pnl_summary()}"
            )

        updated = True

    if updated:
        save_trades(trades)
        console.print(f"  {pnl_summary()}")


# ══════════════════════════════════════════════════════════════════════
# Auto-run loop (for systemd / cron)
# ══════════════════════════════════════════════════════════════════════

RESOLUTION_CHECK_INTERVAL = 3600  # Check resolutions every hour

async def run_auto():
    """Run scan automatically every AI_SCAN_INTERVAL hours, check resolutions hourly."""
    console.print("[bold]🤖 AI Mispricing Scanner — Auto Mode[/bold]")
    console.print(f"  Scan interval: {AI_SCAN_INTERVAL}h")
    console.print(f"  Min edge: {AI_MIN_EDGE:.0%}")
    console.print(f"  Bet size: ${AI_BET_SIZE_MIN}-${AI_BET_SIZE_MAX}")
    console.print(f"  Max markets per scan: {AI_MAX_MARKETS_PER_SCAN}")
    console.print(f"  Resolution check: every 1h")
    console.print()

    _send_tg(
        f"🤖 AI Mispricing Scanner started\n"
        f"Scanning every {AI_SCAN_INTERVAL}h | Min edge: {AI_MIN_EDGE:.0%}\n"
        f"Resolution checks: every 1h"
    )

    last_scan = 0  # Force first scan immediately
    last_resolution_check = 0

    while True:
        now = time.time()

        # Resolution check every hour
        if now - last_resolution_check >= RESOLUTION_CHECK_INTERVAL:
            try:
                console.print(f"\n[bold]🔄 Resolution Check — {datetime.now(timezone.utc).strftime('%H:%M UTC')}[/bold]")
                check_resolutions()
                last_resolution_check = now
            except Exception as e:
                console.print(f"[red]Resolution check error: {e}[/red]")

        # Full scan every AI_SCAN_INTERVAL hours
        if now - last_scan >= AI_SCAN_INTERVAL * 3600:
            try:
                console.print(f"\n[bold]🔍 Full Scan — {datetime.now(timezone.utc).strftime('%H:%M UTC')}[/bold]")
                run_scan()
                last_scan = now
            except Exception as e:
                console.print(f"[red]Scan error: {e}[/red]")
                _send_tg(f"❌ AI scan error: {e}")

        # Sleep 5 minutes between checks
        await asyncio.sleep(300)


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
