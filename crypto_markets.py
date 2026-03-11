"""
Polymarket crypto 15-min market discovery and management.
Auto-discovers current BTC/ETH/SOL up/down markets and extracts token IDs.
"""

import json
import time
from dataclasses import dataclass

import requests

GAMMA_URL = "https://gamma-api.polymarket.com"

# Supported coins and their slug prefixes
COIN_SLUGS = {
    "BTC": "btc-updown-15m",
    "ETH": "eth-updown-15m",
    "SOL": "sol-updown-15m",
}

WINDOW_SECONDS = 900  # 15 minutes


@dataclass
class CryptoMarket:
    coin: str
    slug: str
    question: str
    up_token_id: str
    down_token_id: str
    up_price: float
    down_price: float
    end_timestamp: int
    accepting_orders: bool

    @property
    def seconds_remaining(self) -> int:
        return max(0, self.end_timestamp - int(time.time()))

    @property
    def is_active(self) -> bool:
        return self.accepting_orders and self.seconds_remaining > 0


def get_current_window_timestamp() -> int:
    """Get the Unix timestamp for the current 15-minute window start."""
    now = int(time.time())
    return (now // WINDOW_SECONDS) * WINDOW_SECONDS


def get_next_window_timestamp() -> int:
    """Get the Unix timestamp for the next 15-minute window start."""
    return get_current_window_timestamp() + WINDOW_SECONDS


def discover_market(coin: str) -> CryptoMarket | None:
    """
    Discover the current active 15-min market for a coin.
    Tries current window first, then next window.
    """
    prefix = COIN_SLUGS.get(coin)
    if not prefix:
        print(f"[markets] Unknown coin: {coin}")
        return None

    # Try current window, then next
    for ts in [get_current_window_timestamp(), get_next_window_timestamp()]:
        slug = f"{prefix}-{ts}"
        market = fetch_market_by_slug(slug, coin)
        if market and market.is_active:
            return market

    # Fallback: search via Gamma API
    return search_active_market(coin)


def fetch_market_by_slug(slug: str, coin: str) -> CryptoMarket | None:
    """Fetch a specific market by its slug."""
    try:
        resp = requests.get(
            f"{GAMMA_URL}/markets",
            params={"slug": slug},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if not data:
            return None

        # API returns a list
        market = data[0] if isinstance(data, list) else data
        return parse_market(market, coin)

    except (requests.RequestException, ValueError, IndexError) as e:
        return None


def search_active_market(coin: str) -> CryptoMarket | None:
    """Search for the active market via Gamma API text search."""
    prefix = COIN_SLUGS.get(coin, "")
    try:
        resp = requests.get(
            f"{GAMMA_URL}/markets",
            params={
                "slug_contains": prefix,
                "active": "true",
                "closed": "false",
                "order": "endDate",
                "ascending": "true",
                "limit": 5,
            },
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json()

        if not markets:
            return None

        for m in markets:
            parsed = parse_market(m, coin)
            if parsed and parsed.is_active:
                return parsed

        return None

    except (requests.RequestException, ValueError) as e:
        print(f"[markets] Search failed for {coin}: {e}")
        return None


def parse_market(market: dict, coin: str) -> CryptoMarket | None:
    """Parse a Gamma API market response into a CryptoMarket."""
    try:
        raw_outcomes = market.get("outcomes", "[]")
        raw_prices = market.get("outcomePrices", "[]")
        raw_token_ids = market.get("clobTokenIds", "[]")

        outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
        token_ids = json.loads(raw_token_ids) if isinstance(raw_token_ids, str) else raw_token_ids

        if len(outcomes) < 2 or len(token_ids) < 2:
            return None

        # Map outcomes to up/down
        up_idx = None
        down_idx = None
        for i, o in enumerate(outcomes):
            if o.lower() == "up":
                up_idx = i
            elif o.lower() == "down":
                down_idx = i

        if up_idx is None or down_idx is None:
            return None

        # Parse end timestamp from slug or endDate
        slug = market.get("slug", "")
        end_ts = 0
        try:
            # Slug format: btc-updown-15m-1768824000
            parts = slug.split("-")
            end_ts = int(parts[-1]) + WINDOW_SECONDS
        except (ValueError, IndexError):
            pass

        if not end_ts:
            end_date = market.get("endDate", "")
            if end_date:
                from datetime import datetime
                try:
                    dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    end_ts = int(dt.timestamp())
                except ValueError:
                    end_ts = int(time.time()) + 900

        return CryptoMarket(
            coin=coin,
            slug=slug,
            question=market.get("question", ""),
            up_token_id=token_ids[up_idx],
            down_token_id=token_ids[down_idx],
            up_price=float(prices[up_idx]) if up_idx < len(prices) else 0.5,
            down_price=float(prices[down_idx]) if down_idx < len(prices) else 0.5,
            end_timestamp=end_ts,
            accepting_orders=market.get("acceptingOrders", True),
        )

    except Exception as e:
        print(f"[markets] Failed to parse market: {e}")
        return None


def discover_all_markets() -> dict[str, CryptoMarket]:
    """Discover current active markets for all supported coins."""
    markets = {}
    for coin in COIN_SLUGS:
        market = discover_market(coin)
        if market:
            markets[coin] = market
            print(
                f"[markets] {coin}: {market.slug} | "
                f"Up: ${market.up_price:.2f} Down: ${market.down_price:.2f} | "
                f"{market.seconds_remaining}s remaining"
            )
        else:
            print(f"[markets] {coin}: No active market found")
    return markets


if __name__ == "__main__":
    print("Discovering active 15-min crypto markets...")
    markets = discover_all_markets()
    print(f"\nFound {len(markets)} active markets")
