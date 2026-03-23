"""Thorough P&L analysis of all bot trading activity."""
import os, requests, json
from datetime import datetime, timezone
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv()

wallet = os.getenv("WALLET_ADDRESS")

# Get ALL activities
all_activities = []
for page in range(20):
    url = f"https://data-api.polymarket.com/activity?user={wallet}&limit=500&offset={page*500}"
    resp = requests.get(url, timeout=15)
    activities = resp.json()
    if not activities:
        break
    all_activities.extend(activities)
    if len(activities) < 500:
        break

all_activities = [a for a in all_activities if isinstance(a, dict)]
print(f"Total activities: {len(all_activities)}")
all_activities.sort(key=lambda a: int(a.get("timestamp", 0)))

# Break into 30-min windows
windows = defaultdict(lambda: {"bought": 0, "redeemed": 0, "trades": 0, "redeems": 0, "coins": defaultdict(float)})

for a in all_activities:
    ts = int(a.get("timestamp", 0))
    window = (ts // 1800) * 1800
    t = a.get("type", "")
    usdc = float(a.get("usdcSize", 0))
    title = a.get("title", "")

    coin = "?"
    for c in ["Bitcoin", "Ethereum", "Solana", "XRP"]:
        if c in title:
            coin = c[:3].upper()
            break

    if t == "TRADE" and a.get("side") == "BUY":
        windows[window]["bought"] += usdc
        windows[window]["trades"] += 1
        windows[window]["coins"][coin] += usdc
    elif t == "REDEEM":
        windows[window]["redeemed"] += usdc
        windows[window]["redeems"] += 1

# Print timeline
print("\n=== P&L BY 30-MIN WINDOW ===")
print(f"  Time UTC  |   Bought |  Redeemed |      Net | Running | Trades | Redeems")
print("-" * 85)

running = 0
peak = 0
peak_time = ""
for window in sorted(windows.keys()):
    w = windows[window]
    net = w["redeemed"] - w["bought"]
    running += net
    if running > peak:
        peak = running
        peak_time = datetime.fromtimestamp(window, tz=timezone.utc).strftime("%H:%M")
    time_str = datetime.fromtimestamp(window, tz=timezone.utc).strftime("%H:%M")
    print(f"  {time_str}    | ${w['bought']:>7.2f} | ${w['redeemed']:>8.2f} | ${net:>+7.2f} | ${running:>+7.2f} | {w['trades']:>6d} | {w['redeems']:>7d}")

print("-" * 85)
total_bought = sum(w["bought"] for w in windows.values())
total_redeemed = sum(w["redeemed"] for w in windows.values())
print(f"  TOTAL   | ${total_bought:>7.2f} | ${total_redeemed:>8.2f} | ${total_redeemed - total_bought:>+7.2f} |         | {sum(w['trades'] for w in windows.values()):>6d} | {sum(w['redeems'] for w in windows.values()):>7d}")

print(f"\n  Peak P&L: ${peak:.2f} at {peak_time} UTC")
print(f"  Final: ${running:.2f}")
print(f"  Drawdown from peak: ${peak - running:.2f}")

# Per-coin analysis
print("\n=== P&L BY COIN ===")
coin_totals = defaultdict(lambda: {"bought": 0, "redeemed": 0})
for a in all_activities:
    title = a.get("title", "")
    usdc = float(a.get("usdcSize", 0))
    coin = "?"
    for c in ["Bitcoin", "Ethereum", "Solana", "XRP"]:
        if c in title:
            coin = c[:3].upper()
            break
    if a.get("type") == "TRADE" and a.get("side") == "BUY":
        coin_totals[coin]["bought"] += usdc
    elif a.get("type") == "REDEEM":
        coin_totals[coin]["redeemed"] += usdc

for coin in sorted(coin_totals.keys()):
    c = coin_totals[coin]
    net = c["redeemed"] - c["bought"]
    print(f"  {coin:5s} | Bought: ${c['bought']:>8.2f} | Redeemed: ${c['redeemed']:>8.2f} | Net: ${net:>+8.2f}")

# Timeframe analysis (5m vs 15m)
print("\n=== P&L BY TIMEFRAME ===")
tf_totals = defaultdict(lambda: {"bought": 0, "redeemed": 0, "count": 0})
for a in all_activities:
    slug = a.get("slug", "")
    if "5m" in slug:
        tf = "5min"
    elif "15m" in slug:
        tf = "15min"
    else:
        tf = "other"
    usdc = float(a.get("usdcSize", 0))
    if a.get("type") == "TRADE" and a.get("side") == "BUY":
        tf_totals[tf]["bought"] += usdc
        tf_totals[tf]["count"] += 1
    elif a.get("type") == "REDEEM":
        tf_totals[tf]["redeemed"] += usdc

for tf in sorted(tf_totals.keys()):
    t = tf_totals[tf]
    net = t["redeemed"] - t["bought"]
    print(f"  {tf:6s} | Bought: ${t['bought']:>8.2f} | Redeemed: ${t['redeemed']:>8.2f} | Net: ${net:>+8.2f} | Trades: {t['count']}")

# What went wrong analysis
print("\n=== WHAT WENT WRONG ===")
# Find windows where we lost the most
worst_windows = []
for window in sorted(windows.keys()):
    w = windows[window]
    net = w["redeemed"] - w["bought"]
    if net < -20:
        time_str = datetime.fromtimestamp(window, tz=timezone.utc).strftime("%H:%M")
        worst_windows.append((time_str, net, w["trades"], w["redeems"], w["bought"], w["redeemed"]))

if worst_windows:
    print("  Worst 30-min windows (lost >$20):")
    for time_str, net, trades, redeems, bought, redeemed in sorted(worst_windows, key=lambda x: x[1]):
        print(f"    {time_str} UTC | Net: ${net:+.2f} | Bought: ${bought:.2f} | Redeemed: ${redeemed:.2f} | {trades} trades, {redeems} redeems")
