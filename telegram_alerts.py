"""
Telegram alert module for Polymarket Sniper Bot.
Sends trade notifications, wins, losses, and status updates.
"""

import os
import threading
import urllib.request
import urllib.parse
import json

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

def _send_async(text: str):
    """Send message in background thread (non-blocking)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    def _do_send():
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = json.dumps({
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass  # Never crash the bot over a failed alert

    threading.Thread(target=_do_send, daemon=True).start()


def alert_trade(coin: str, side: str, price: float, amount: float, edge: float, secs_left: int, bankroll: float):
    """Alert when a trade is placed."""
    emoji = "🟢" if side == "up" else "🔴"
    _send_async(
        f"{emoji} *TRADE* {coin} {side.upper()}\n"
        f"Price: ${price:.3f} | ${amount:.2f} USDC\n"
        f"Edge: {edge:.1%} | {secs_left}s left\n"
        f"Bankroll: ${bankroll:.2f}"
    )


def alert_win(coin: str, side: str, amount: float, payout: float, bankroll: float):
    """Alert when a trade wins."""
    profit = payout - amount
    _send_async(
        f"✅ *WIN* {coin} {side.upper()}\n"
        f"Bet ${amount:.2f} → Payout ${payout:.2f} (+${profit:.2f})\n"
        f"Bankroll: ${bankroll:.2f}"
    )


def alert_loss(coin: str, side: str, amount: float, bankroll: float):
    """Alert when a trade loses."""
    _send_async(
        f"❌ *LOSS* {coin} {side.upper()}\n"
        f"Lost ${amount:.2f}\n"
        f"Bankroll: ${bankroll:.2f}"
    )


def alert_expired(coin: str, side: str, amount: float, bankroll: float):
    """Alert when an order is confirmed unfilled."""
    _send_async(
        f"⏳ *EXPIRED* {coin} {side.upper()}\n"
        f"Unfilled — ${amount:.2f} returned\n"
        f"Bankroll: ${bankroll:.2f}"
    )


def alert_stuck(coin: str, side: str, amount: float, bankroll: float):
    """Alert when a filled order can't resolve and is counted as loss."""
    _send_async(
        f"🔒 *STUCK* {coin} {side.upper()}\n"
        f"Filled but unresolvable — counted as loss ${amount:.2f}\n"
        f"Bankroll: ${bankroll:.2f}"
    )


def alert_status(bankroll: float, pnl: float, wins: int, losses: int, pending: int):
    """Periodic status update."""
    pnl_emoji = "📈" if pnl >= 0 else "📉"
    total = wins + losses
    wr = f"{wins/total:.0%}" if total > 0 else "N/A"
    _send_async(
        f"{pnl_emoji} *STATUS UPDATE*\n"
        f"Bankroll: ${bankroll:.2f} | P&L: ${pnl:+.2f}\n"
        f"W/L: {wins}/{losses} ({wr})\n"
        f"Pending: {pending}"
    )


def alert_bot_started(bankroll: float, coins: list):
    """Alert when bot starts."""
    _send_async(
        f"🚀 *BOT STARTED*\n"
        f"Coins: {', '.join(coins)}\n"
        f"Bankroll: ${bankroll:.2f}"
    )
