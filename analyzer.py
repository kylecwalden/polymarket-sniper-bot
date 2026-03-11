"""
Performance Analyzer & Learning Module v1.
Runs periodically to analyze bot performance and generate parameter recommendations.

Called by scheduled cron job every time we cross a 100-trade milestone.
Can also be run standalone: python3 analyzer.py
"""

import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# --- Paths ---
DATA_DIR = Path("data")
TRADES_FILE = DATA_DIR / "trades.json"
ANALYSIS_FILE = DATA_DIR / "analysis.json"
LEARNING_FILE = DATA_DIR / "learning_state.json"
RECOMMENDATIONS_FILE = DATA_DIR / "recommendations.json"

# Minimum trades needed before generating recommendations
MIN_TRADES_FOR_ANALYSIS = 10
MILESTONE_INTERVAL = 100  # Run full analysis every 100 trades


@dataclass
class TradeAnalysis:
    """Comprehensive analysis of bot performance."""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_wagered: float = 0.0
    total_pnl: float = 0.0
    roi: float = 0.0
    avg_win_payout: float = 0.0
    avg_loss_amount: float = 0.0
    expected_value_per_trade: float = 0.0

    # Per-coin stats
    coin_stats: dict = None

    # Per-price-bucket stats
    price_bucket_stats: dict = None

    # Per-edge-bucket stats
    edge_bucket_stats: dict = None

    # Time-of-window stats (early vs late trades)
    timing_stats: dict = None

    # Per-side stats
    side_stats: dict = None

    # Streak analysis
    max_win_streak: int = 0
    max_loss_streak: int = 0
    current_streak: int = 0
    current_streak_type: str = ""

    def __post_init__(self):
        if self.coin_stats is None:
            self.coin_stats = {}
        if self.price_bucket_stats is None:
            self.price_bucket_stats = {}
        if self.edge_bucket_stats is None:
            self.edge_bucket_stats = {}
        if self.timing_stats is None:
            self.timing_stats = {}
        if self.side_stats is None:
            self.side_stats = {}


def load_trades() -> list[dict]:
    """Load all trades from JSON file."""
    if not TRADES_FILE.exists():
        return []
    try:
        data = json.loads(TRADES_FILE.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def load_learning_state() -> dict:
    """Load learning state (last milestone, parameter history)."""
    if LEARNING_FILE.exists():
        try:
            return json.loads(LEARNING_FILE.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    return {
        "last_milestone": 0,
        "analysis_runs": 0,
        "parameter_history": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def save_learning_state(state: dict):
    """Save learning state to disk."""
    DATA_DIR.mkdir(exist_ok=True)
    LEARNING_FILE.write_text(json.dumps(state, indent=2))


def pair_bets_with_outcomes(trades: list[dict]) -> list[dict]:
    """
    Pair bet records with their win/loss outcomes.
    Returns list of paired trades with all relevant data.
    """
    paired = []
    pending_bets = []

    for t in trades:
        if t.get("type") == "bet":
            pending_bets.append(t)
        elif t.get("type") in ("win", "loss"):
            # Try to match with pending bet (same coin + side)
            coin = t.get("coin")
            side = t.get("side")
            matched = None
            for i, bet in enumerate(pending_bets):
                if bet.get("coin") == coin and bet.get("side") == side:
                    matched = pending_bets.pop(i)
                    break

            if matched:
                paired.append({
                    "coin": coin,
                    "side": side,
                    "buy_price": matched.get("buy_price", 0),
                    "amount": matched.get("amount", 0),
                    "window_ts": matched.get("window_ts", 0),
                    "bet_time": matched.get("timestamp", ""),
                    "result": t["type"],  # "win" or "loss"
                    "payout": t.get("payout", 0),
                })
            else:
                # Outcome without matching bet — still count it
                paired.append({
                    "coin": coin,
                    "side": side,
                    "buy_price": 0,
                    "amount": t.get("amount", 0),
                    "window_ts": 0,
                    "bet_time": t.get("timestamp", ""),
                    "result": t["type"],
                    "payout": t.get("payout", 0),
                })

    return paired


def analyze_performance(paired_trades: list[dict]) -> TradeAnalysis:
    """Run comprehensive analysis on paired trades."""
    analysis = TradeAnalysis()

    if not paired_trades:
        return analysis

    analysis.total_trades = len(paired_trades)
    analysis.wins = sum(1 for t in paired_trades if t["result"] == "win")
    analysis.losses = sum(1 for t in paired_trades if t["result"] == "loss")
    analysis.win_rate = analysis.wins / analysis.total_trades if analysis.total_trades > 0 else 0

    analysis.total_wagered = sum(t["amount"] for t in paired_trades)

    total_payouts = sum(t["payout"] for t in paired_trades if t["result"] == "win")
    total_losses = sum(t["amount"] for t in paired_trades if t["result"] == "loss")
    analysis.total_pnl = total_payouts - analysis.total_wagered
    analysis.roi = analysis.total_pnl / analysis.total_wagered if analysis.total_wagered > 0 else 0

    win_payouts = [t["payout"] for t in paired_trades if t["result"] == "win"]
    loss_amounts = [t["amount"] for t in paired_trades if t["result"] == "loss"]
    analysis.avg_win_payout = sum(win_payouts) / len(win_payouts) if win_payouts else 0
    analysis.avg_loss_amount = sum(loss_amounts) / len(loss_amounts) if loss_amounts else 0

    # Expected value per trade
    ev_win = analysis.win_rate * analysis.avg_win_payout
    ev_loss = (1 - analysis.win_rate) * analysis.avg_loss_amount
    analysis.expected_value_per_trade = ev_win - ev_loss

    # --- Per-coin stats ---
    coin_groups = defaultdict(list)
    for t in paired_trades:
        coin_groups[t["coin"]].append(t)

    for coin, trades in coin_groups.items():
        wins = sum(1 for t in trades if t["result"] == "win")
        total = len(trades)
        wagered = sum(t["amount"] for t in trades)
        payouts = sum(t["payout"] for t in trades if t["result"] == "win")
        analysis.coin_stats[coin] = {
            "total": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate": wins / total if total > 0 else 0,
            "pnl": payouts - wagered,
            "avg_buy_price": sum(t["buy_price"] for t in trades) / total if total > 0 else 0,
        }

    # --- Per-price-bucket stats ---
    price_buckets = {"$0.00-0.10": [], "$0.10-0.20": [], "$0.20-0.30": [],
                     "$0.30-0.50": [], "$0.50-0.70": []}
    for t in paired_trades:
        p = t["buy_price"]
        if p < 0.10:
            price_buckets["$0.00-0.10"].append(t)
        elif p < 0.20:
            price_buckets["$0.10-0.20"].append(t)
        elif p < 0.30:
            price_buckets["$0.20-0.30"].append(t)
        elif p < 0.50:
            price_buckets["$0.30-0.50"].append(t)
        else:
            price_buckets["$0.50-0.70"].append(t)

    for bucket, trades in price_buckets.items():
        if trades:
            wins = sum(1 for t in trades if t["result"] == "win")
            total = len(trades)
            wagered = sum(t["amount"] for t in trades)
            payouts = sum(t["payout"] for t in trades if t["result"] == "win")
            analysis.price_bucket_stats[bucket] = {
                "total": total,
                "wins": wins,
                "win_rate": wins / total if total > 0 else 0,
                "pnl": payouts - wagered,
                "avg_payout_ratio": (payouts / wagered) if wagered > 0 else 0,
            }

    # --- Per-side stats ---
    side_groups = defaultdict(list)
    for t in paired_trades:
        side_groups[t["side"]].append(t)

    for side, trades in side_groups.items():
        wins = sum(1 for t in trades if t["result"] == "win")
        total = len(trades)
        wagered = sum(t["amount"] for t in trades)
        payouts = sum(t["payout"] for t in trades if t["result"] == "win")
        analysis.side_stats[side] = {
            "total": total,
            "wins": wins,
            "win_rate": wins / total if total > 0 else 0,
            "pnl": payouts - wagered,
        }

    # --- Streak analysis ---
    win_streak = 0
    loss_streak = 0
    current_streak = 0
    current_type = ""

    for t in paired_trades:
        if t["result"] == "win":
            if current_type == "win":
                current_streak += 1
            else:
                current_streak = 1
                current_type = "win"
            win_streak = max(win_streak, current_streak)
        else:
            if current_type == "loss":
                current_streak += 1
            else:
                current_streak = 1
                current_type = "loss"
            loss_streak = max(loss_streak, current_streak)

    analysis.max_win_streak = win_streak
    analysis.max_loss_streak = loss_streak
    analysis.current_streak = current_streak
    analysis.current_streak_type = current_type

    return analysis


def generate_recommendations(analysis: TradeAnalysis) -> list[dict]:
    """
    Generate parameter adjustment recommendations based on performance.
    This is the LEARNING component — the bot improves over time.
    """
    recommendations = []

    if analysis.total_trades < MIN_TRADES_FOR_ANALYSIS:
        recommendations.append({
            "type": "info",
            "message": f"Need {MIN_TRADES_FOR_ANALYSIS - analysis.total_trades} more trades for meaningful analysis.",
            "action": "none",
        })
        return recommendations

    # --- Rule 1: Adjust min edge based on win rate ---
    if analysis.win_rate < 0.50:
        recommendations.append({
            "type": "critical",
            "param": "ARB_MIN_EDGE",
            "current": float(os.getenv("ARB_MIN_EDGE", "0.05")),
            "recommended": 0.15,
            "reason": f"Win rate is {analysis.win_rate:.1%} (below 50%). Increase min edge to only take higher-confidence trades.",
            "action": "increase_min_edge",
        })
    elif analysis.win_rate > 0.85 and analysis.total_trades >= 50:
        recommendations.append({
            "type": "optimization",
            "param": "ARB_MIN_EDGE",
            "current": float(os.getenv("ARB_MIN_EDGE", "0.05")),
            "recommended": 0.03,
            "reason": f"Win rate is {analysis.win_rate:.1%} (excellent). Can lower min edge to capture more trades.",
            "action": "decrease_min_edge",
        })

    # --- Rule 2: Adjust max entry price based on price bucket performance ---
    for bucket, stats in analysis.price_bucket_stats.items():
        if stats["total"] >= 5 and stats["win_rate"] < 0.40:
            recommendations.append({
                "type": "warning",
                "param": "ARB_MAX_ENTRY_PRICE",
                "bucket": bucket,
                "win_rate": stats["win_rate"],
                "reason": f"Price bucket {bucket} has {stats['win_rate']:.0%} win rate ({stats['total']} trades). Consider lowering max entry price.",
                "action": "adjust_max_entry_price",
            })

    # --- Rule 3: Per-coin performance adjustments ---
    for coin, stats in analysis.coin_stats.items():
        if stats["total"] >= 10 and stats["win_rate"] < 0.40:
            recommendations.append({
                "type": "warning",
                "param": "coin_weight",
                "coin": coin,
                "win_rate": stats["win_rate"],
                "reason": f"{coin} has {stats['win_rate']:.0%} win rate over {stats['total']} trades. Consider reducing bet size or removing coin.",
                "action": "reduce_coin_weight",
            })
        elif stats["total"] >= 10 and stats["win_rate"] > 0.85:
            recommendations.append({
                "type": "optimization",
                "param": "coin_weight",
                "coin": coin,
                "win_rate": stats["win_rate"],
                "reason": f"{coin} has {stats['win_rate']:.0%} win rate over {stats['total']} trades. Consider increasing bet size.",
                "action": "increase_coin_weight",
            })

    # --- Rule 4: Side bias detection ---
    for side, stats in analysis.side_stats.items():
        if stats["total"] >= 10 and stats["win_rate"] < 0.35:
            other_side = "down" if side == "up" else "up"
            recommendations.append({
                "type": "critical",
                "param": "side_bias",
                "side": side,
                "win_rate": stats["win_rate"],
                "reason": f"{side.upper()} bets have {stats['win_rate']:.0%} win rate ({stats['total']} trades). Possible systematic bias. Check oracle reference price alignment.",
                "action": "investigate_side_bias",
            })

    # --- Rule 5: Kelly Criterion bet sizing ---
    if analysis.total_trades >= 30 and analysis.win_rate > 0:
        # Kelly formula: f* = (bp - q) / b
        # Where b = avg win payout / avg bet size - 1, p = win rate, q = 1 - win rate
        avg_bet = analysis.total_wagered / analysis.total_trades
        if avg_bet > 0 and analysis.avg_win_payout > 0:
            b = (analysis.avg_win_payout / avg_bet) - 1
            if b > 0:
                p = analysis.win_rate
                q = 1 - p
                kelly_fraction = (b * p - q) / b
                kelly_fraction = max(0, min(kelly_fraction, 0.25))  # Cap at 25% of bankroll

                kelly_bet = kelly_fraction * float(os.getenv("ARB_MAX_DAILY_SPEND", "20"))
                current_bet = float(os.getenv("ARB_BET_SIZE", "3"))

                if kelly_bet > 0 and abs(kelly_bet - current_bet) / current_bet > 0.3:
                    recommendations.append({
                        "type": "optimization",
                        "param": "ARB_BET_SIZE",
                        "current": current_bet,
                        "recommended": round(kelly_bet, 2),
                        "kelly_fraction": round(kelly_fraction, 4),
                        "reason": f"Kelly Criterion suggests ${kelly_bet:.2f} bet size ({kelly_fraction:.1%} of bankroll). Currently at ${current_bet:.2f}.",
                        "action": "adjust_bet_size",
                    })

    # --- Rule 6: Loss streak circuit breaker ---
    if analysis.max_loss_streak >= 6:
        recommendations.append({
            "type": "critical",
            "param": "circuit_breaker",
            "max_loss_streak": analysis.max_loss_streak,
            "reason": f"Hit {analysis.max_loss_streak}-trade loss streak. Consider adding circuit breaker: pause trading after 5 consecutive losses.",
            "action": "add_circuit_breaker",
        })

    # --- Rule 7: Cooldown adjustment ---
    if analysis.total_trades >= 30:
        trades_per_window = analysis.total_trades  # rough estimate
        if analysis.win_rate > 0.70 and trades_per_window > 4:
            recommendations.append({
                "type": "optimization",
                "param": "ARB_COOLDOWN_SECS",
                "current": int(os.getenv("ARB_COOLDOWN_SECS", "120")),
                "recommended": 90,
                "reason": "High win rate suggests shorter cooldown could capture more profitable trades.",
                "action": "decrease_cooldown",
            })

    return recommendations


def apply_auto_learning(recommendations: list[dict], learning_state: dict) -> list[dict]:
    """
    Apply safe automatic parameter adjustments from recommendations.
    Only adjusts parameters that have clear data-driven evidence.
    Returns list of changes actually applied.
    """
    applied = []

    for rec in recommendations:
        action = rec.get("action", "")

        # AUTO-APPLY: Increase min edge when win rate is critically low
        if action == "increase_min_edge" and rec.get("type") == "critical":
            new_val = rec["recommended"]
            # Write to .env or a config override file
            save_config_override("ARB_MIN_EDGE", str(new_val))
            applied.append({
                "param": "ARB_MIN_EDGE",
                "old": rec["current"],
                "new": new_val,
                "reason": rec["reason"],
            })

    return applied


def save_config_override(key: str, value: str):
    """Save a config override that the bot will read on next restart."""
    override_file = DATA_DIR / "config_overrides.json"
    overrides = {}
    if override_file.exists():
        try:
            overrides = json.loads(override_file.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    overrides[key] = value
    overrides["_updated_at"] = datetime.now(timezone.utc).isoformat()
    override_file.write_text(json.dumps(overrides, indent=2))


def run_analysis(force: bool = False) -> dict:
    """
    Main analysis entry point.
    Called by cron job or manually.
    Returns analysis results dict.
    """
    trades = load_trades()
    learning_state = load_learning_state()

    # Count resolved trades (wins + losses)
    resolved_count = sum(1 for t in trades if t.get("type") in ("win", "loss"))

    # Check if we've hit a milestone
    last_milestone = learning_state.get("last_milestone", 0)
    current_milestone = (resolved_count // MILESTONE_INTERVAL) * MILESTONE_INTERVAL

    if not force and current_milestone <= last_milestone and resolved_count >= MIN_TRADES_FOR_ANALYSIS:
        return {
            "status": "skipped",
            "reason": f"No new milestone. Resolved: {resolved_count}, Last milestone: {last_milestone}, Next: {last_milestone + MILESTONE_INTERVAL}",
            "resolved_trades": resolved_count,
        }

    # Pair bets with outcomes
    paired = pair_bets_with_outcomes(trades)

    if not paired:
        return {
            "status": "no_data",
            "reason": "No paired trades found.",
            "total_raw_trades": len(trades),
        }

    # Run analysis
    analysis = analyze_performance(paired)

    # Generate recommendations
    recommendations = generate_recommendations(analysis)

    # Apply safe auto-learning
    applied_changes = apply_auto_learning(recommendations, learning_state)

    # Update learning state
    learning_state["last_milestone"] = max(current_milestone, last_milestone)
    learning_state["analysis_runs"] = learning_state.get("analysis_runs", 0) + 1
    learning_state["last_analysis_at"] = datetime.now(timezone.utc).isoformat()
    learning_state["last_resolved_count"] = resolved_count

    if applied_changes:
        learning_state.setdefault("parameter_history", []).append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "resolved_trades": resolved_count,
            "changes": applied_changes,
        })

    save_learning_state(learning_state)

    # Build results
    results = {
        "status": "completed",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "resolved_trades": resolved_count,
        "milestone": current_milestone,
        "analysis": {
            "total_trades": analysis.total_trades,
            "wins": analysis.wins,
            "losses": analysis.losses,
            "win_rate": round(analysis.win_rate, 4),
            "total_wagered": round(analysis.total_wagered, 2),
            "total_pnl": round(analysis.total_pnl, 2),
            "roi": round(analysis.roi, 4),
            "expected_value_per_trade": round(analysis.expected_value_per_trade, 4),
            "avg_win_payout": round(analysis.avg_win_payout, 2),
            "avg_loss_amount": round(analysis.avg_loss_amount, 2),
            "max_win_streak": analysis.max_win_streak,
            "max_loss_streak": analysis.max_loss_streak,
            "current_streak": analysis.current_streak,
            "current_streak_type": analysis.current_streak_type,
        },
        "coin_stats": analysis.coin_stats,
        "price_bucket_stats": analysis.price_bucket_stats,
        "side_stats": analysis.side_stats,
        "recommendations": recommendations,
        "applied_changes": applied_changes,
    }

    # Save analysis results
    DATA_DIR.mkdir(exist_ok=True)
    ANALYSIS_FILE.write_text(json.dumps(results, indent=2))
    RECOMMENDATIONS_FILE.write_text(json.dumps(recommendations, indent=2))

    return results


def print_report(results: dict):
    """Pretty-print analysis results."""
    if results["status"] == "skipped":
        print(f"⏭ Analysis skipped: {results['reason']}")
        return

    if results["status"] == "no_data":
        print(f"📭 No data: {results['reason']}")
        return

    a = results["analysis"]
    print()
    print("=" * 60)
    print(f"  📊 PERFORMANCE ANALYSIS — {a['total_trades']} Resolved Trades")
    print(f"  🕐 {results['timestamp']}")
    print("=" * 60)

    # Overall
    pnl_emoji = "🟢" if a["total_pnl"] >= 0 else "🔴"
    print(f"\n  {pnl_emoji} Overall: {a['wins']}W / {a['losses']}L ({a['win_rate']:.1%})")
    print(f"  💰 P&L: ${a['total_pnl']:+.2f} | ROI: {a['roi']:.1%}")
    print(f"  📈 EV/trade: ${a['expected_value_per_trade']:+.4f}")
    print(f"  🏆 Streaks: Best {a['max_win_streak']}W | Worst {a['max_loss_streak']}L")

    # Per-coin
    if results.get("coin_stats"):
        print(f"\n  📊 Per-Coin Breakdown:")
        for coin, stats in results["coin_stats"].items():
            emoji = "✅" if stats["win_rate"] >= 0.60 else "⚠️" if stats["win_rate"] >= 0.40 else "❌"
            print(f"    {emoji} {coin}: {stats['wins']}W/{stats['losses']}L ({stats['win_rate']:.0%}) | P&L: ${stats['pnl']:+.2f} | Avg price: ${stats['avg_buy_price']:.3f}")

    # Per-side
    if results.get("side_stats"):
        print(f"\n  📊 Per-Side Breakdown:")
        for side, stats in results["side_stats"].items():
            emoji = "✅" if stats["win_rate"] >= 0.60 else "⚠️" if stats["win_rate"] >= 0.40 else "❌"
            print(f"    {emoji} {side.upper()}: {stats['wins']}W/{stats['total'] - stats['wins']}L ({stats['win_rate']:.0%}) | P&L: ${stats['pnl']:+.2f}")

    # Price buckets
    if results.get("price_bucket_stats"):
        print(f"\n  📊 Performance by Entry Price:")
        for bucket, stats in sorted(results["price_bucket_stats"].items()):
            emoji = "✅" if stats["win_rate"] >= 0.60 else "⚠️" if stats["win_rate"] >= 0.40 else "❌"
            print(f"    {emoji} {bucket}: {stats['wins']}W/{stats['total'] - stats['wins']}L ({stats['win_rate']:.0%}) | P&L: ${stats['pnl']:+.2f}")

    # Recommendations
    if results.get("recommendations"):
        print(f"\n  🧠 LEARNING RECOMMENDATIONS:")
        for i, rec in enumerate(results["recommendations"], 1):
            icon = "🚨" if rec["type"] == "critical" else "⚡" if rec["type"] == "optimization" else "⚠️"
            msg = rec.get("reason", rec.get("message", "No details"))
            print(f"    {icon} #{i}: {msg}")
            if "recommended" in rec:
                print(f"       → Suggested: {rec['param']} = {rec['recommended']}")

    # Applied changes
    if results.get("applied_changes"):
        print(f"\n  🔧 AUTO-APPLIED CHANGES:")
        for change in results["applied_changes"]:
            print(f"    ✅ {change['param']}: {change['old']} → {change['new']}")
            print(f"       Reason: {change['reason']}")
    else:
        print(f"\n  ℹ️  No auto-adjustments applied this run.")

    print()
    print("=" * 60)
    print(f"  Next milestone analysis at {results.get('milestone', 0) + MILESTONE_INTERVAL} trades")
    print("=" * 60)
    print()


if __name__ == "__main__":
    import sys

    force = "--force" in sys.argv

    print("🔍 Running performance analysis...")
    results = run_analysis(force=force)
    print_report(results)
