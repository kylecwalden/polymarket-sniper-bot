"""
Order engine - places buy orders on cheap outcomes via Polymarket CLOB.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

from scanner import CheapOutcome

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

DATA_DIR = Path("data")
ORDERS_FILE = DATA_DIR / "orders.json"


@dataclass
class PlacedOrder:
    timestamp: str
    event_title: str
    market_question: str
    outcome: str
    price: float
    size: float
    usdc_spent: float
    token_id: str
    order_id: str
    status: str


def init_client(private_key: str, signature_type: int = 1, funder: str = None) -> ClobClient:
    """Initialize and authenticate the CLOB client."""
    kwargs = {
        "host": HOST,
        "key": private_key,
        "chain_id": CHAIN_ID,
        "signature_type": signature_type,
    }
    if funder:
        kwargs["funder"] = funder

    client = ClobClient(**kwargs)
    client.set_api_creds(client.create_or_derive_api_creds())
    print("[trader] CLOB client initialized and authenticated")
    return client


def calculate_shares(usdc_amount: float, price: float) -> float:
    """
    Calculate number of shares to buy.
    On Polymarket: shares = usdc / price
    If price = 0.02 and you spend $10, you get 500 shares.
    If the outcome resolves YES, each share pays $1.00.
    """
    if price <= 0:
        return 0
    return usdc_amount / price


def place_buy_order(
    client: ClobClient,
    outcome: CheapOutcome,
    usdc_amount: float,
) -> PlacedOrder | None:
    """
    Place a GTC limit buy order for a cheap outcome.
    Returns PlacedOrder on success, None on failure.
    """
    size = calculate_shares(usdc_amount, outcome.price)

    try:
        order_args = OrderArgs(
            token_id=outcome.token_id,
            price=outcome.price,
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
            print(f"[trader] Order rejected: {response}")
            return None

        placed = PlacedOrder(
            timestamp=datetime.utcnow().isoformat(),
            event_title=outcome.event_title,
            market_question=outcome.market_question,
            outcome=outcome.outcome,
            price=outcome.price,
            size=size,
            usdc_spent=usdc_amount,
            token_id=outcome.token_id,
            order_id=order_id,
            status="placed",
        )

        print(
            f"[trader] ORDER PLACED: ${usdc_amount:.2f} on '{outcome.outcome}' "
            f"@ ${outcome.price:.4f} ({size:.0f} shares) | {outcome.market_question}"
        )
        return placed

    except Exception as e:
        print(f"[trader] Failed to place order on '{outcome.outcome}': {e}")
        return None


def load_order_history() -> list[dict]:
    """Load order history from disk."""
    if ORDERS_FILE.exists():
        with open(ORDERS_FILE) as f:
            return json.load(f)
    return []


def save_order(order: PlacedOrder):
    """Append an order to the history file."""
    DATA_DIR.mkdir(exist_ok=True)
    history = load_order_history()
    history.append(order.__dict__)
    with open(ORDERS_FILE, "w") as f:
        json.dump(history, f, indent=2)


def get_daily_spend() -> float:
    """Calculate how much USDC has been spent today."""
    history = load_order_history()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return sum(
        o["usdc_spent"]
        for o in history
        if o["timestamp"].startswith(today)
    )


def get_placed_token_ids() -> set[str]:
    """Get set of token_ids we already have orders on (avoid duplicates)."""
    history = load_order_history()
    return {o["token_id"] for o in history}
