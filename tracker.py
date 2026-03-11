"""
Position tracker - monitors open positions and calculates P&L.
"""

import json
from datetime import datetime
from pathlib import Path

import requests
from rich.console import Console
from rich.table import Table

from trader import ORDERS_FILE, load_order_history

GAMMA_URL = "https://gamma-api.polymarket.com"
console = Console()


def get_current_price(token_id: str) -> float | None:
    """Get current midpoint price for a token."""
    try:
        resp = requests.get(
            "https://clob.polymarket.com/midpoint",
            params={"token_id": token_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return float(data.get("mid", 0))
    except Exception:
        return None


def show_positions():
    """Display all positions with current prices and P&L."""
    history = load_order_history()

    if not history:
        console.print("[yellow]No orders placed yet.[/yellow]")
        return

    table = Table(title="Polymarket Sniper Bot - Positions")
    table.add_column("Date", style="dim")
    table.add_column("Outcome", style="cyan", max_width=30)
    table.add_column("Market", max_width=40)
    table.add_column("Buy Price", justify="right")
    table.add_column("Current", justify="right")
    table.add_column("Shares", justify="right")
    table.add_column("Spent", justify="right")
    table.add_column("Value", justify="right")
    table.add_column("P&L", justify="right")

    total_spent = 0.0
    total_value = 0.0
    total_pnl = 0.0

    for order in history:
        buy_price = order["price"]
        shares = order["size"]
        spent = order["usdc_spent"]
        token_id = order["token_id"]

        current_price = get_current_price(token_id)
        if current_price is None:
            current_price = 0.0

        current_value = shares * current_price
        pnl = current_value - spent
        pnl_pct = (pnl / spent * 100) if spent > 0 else 0

        total_spent += spent
        total_value += current_value
        total_pnl += pnl

        pnl_style = "green" if pnl >= 0 else "red"
        date_str = order["timestamp"][:10]

        table.add_row(
            date_str,
            order["outcome"],
            order["market_question"][:40],
            f"${buy_price:.4f}",
            f"${current_price:.4f}",
            f"{shares:.0f}",
            f"${spent:.2f}",
            f"${current_value:.2f}",
            f"[{pnl_style}]${pnl:+.2f} ({pnl_pct:+.0f}%)[/{pnl_style}]",
        )

    console.print(table)
    console.print()

    pnl_style = "green" if total_pnl >= 0 else "red"
    pnl_pct = (total_pnl / total_spent * 100) if total_spent > 0 else 0
    console.print(f"  Total Spent:  ${total_spent:.2f}")
    console.print(f"  Total Value:  ${total_value:.2f}")
    console.print(f"  Total P&L:    [{pnl_style}]${total_pnl:+.2f} ({pnl_pct:+.0f}%)[/{pnl_style}]")
    console.print(f"  Positions:    {len(history)}")


def show_summary():
    """Quick summary stats."""
    history = load_order_history()
    if not history:
        return {"total_orders": 0, "total_spent": 0, "unique_markets": 0}

    return {
        "total_orders": len(history),
        "total_spent": sum(o["usdc_spent"] for o in history),
        "unique_markets": len(set(o["token_id"] for o in history)),
        "first_order": history[0]["timestamp"][:10] if history else None,
        "last_order": history[-1]["timestamp"][:10] if history else None,
    }


if __name__ == "__main__":
    show_positions()
