"""
Market scanner - finds cheap outcomes on Polymarket via Gamma API.
"""

import json
import requests
import time
from dataclasses import dataclass

GAMMA_URL = "https://gamma-api.polymarket.com"


@dataclass
class CheapOutcome:
    event_title: str
    market_question: str
    outcome: str
    price: float
    token_id: str
    condition_id: str
    market_slug: str


def fetch_active_events() -> list[dict]:
    """Fetch all active, non-closed events from Gamma API."""
    events = []
    offset = 0
    limit = 100

    while True:
        try:
            resp = requests.get(
                f"{GAMMA_URL}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "offset": offset,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"[scanner] Error fetching events at offset {offset}: {e}")
            break

        if not data:
            break

        events.extend(data)

        if len(data) < limit:
            break

        offset += limit
        time.sleep(0.5)  # rate limit courtesy

    return events


def find_cheap_outcomes(events: list[dict], min_price: float = 0.005, max_price: float = 0.03) -> list[CheapOutcome]:
    """
    Scan events for outcomes priced between min_price and max_price.
    min_price filters out dead/illiquid markets and respects Polymarket minimums.
    """
    cheap = []

    for event in events:
        event_title = event.get("title", "Unknown")

        for market in event.get("markets", []):
            # These fields come as JSON strings from the API, not lists
            raw_outcomes = market.get("outcomes", "[]")
            raw_prices = market.get("outcomePrices", "[]")
            raw_token_ids = market.get("clobTokenIds", "[]")

            outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
            prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
            clob_token_ids = json.loads(raw_token_ids) if isinstance(raw_token_ids, str) else raw_token_ids

            condition_id = market.get("conditionId", "")
            market_slug = market.get("slug", "")
            question = market.get("question", event_title)

            if not outcomes or not prices:
                continue

            for i, (outcome, price_str) in enumerate(zip(outcomes, prices)):
                try:
                    price = float(price_str)
                except (ValueError, TypeError):
                    continue

                if price < min_price or price > max_price:
                    continue

                token_id = clob_token_ids[i] if i < len(clob_token_ids) else None
                if not token_id:
                    continue

                cheap.append(
                    CheapOutcome(
                        event_title=event_title,
                        market_question=question,
                        outcome=outcome,
                        price=price,
                        token_id=token_id,
                        condition_id=condition_id,
                        market_slug=market_slug,
                    )
                )

    # Sort by price ascending (cheapest first)
    cheap.sort(key=lambda x: x.price)
    return cheap


def scan(min_price: float = 0.005, max_price: float = 0.03) -> list[CheapOutcome]:
    """Full scan: fetch events, find cheap outcomes."""
    print(f"[scanner] Fetching active events from Polymarket...")
    events = fetch_active_events()
    print(f"[scanner] Found {len(events)} active events")

    cheap = find_cheap_outcomes(events, min_price=min_price, max_price=max_price)
    print(f"[scanner] Found {len(cheap)} outcomes priced ${min_price}-${max_price}")

    return cheap


if __name__ == "__main__":
    results = scan(max_price=0.03)
    for r in results[:20]:
        print(f"  ${r.price:.4f} | {r.outcome} | {r.market_question}")
