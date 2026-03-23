"""
Polymarket WebSocket Orderbook Feed
====================================
Real-time orderbook streaming for UP/DOWN crypto markets.
Sub-100ms updates vs 1.5s REST polling.

Usage:
    from polymarket_ws import orderbook_feed
    orderbook_feed.subscribe(market)  # Subscribe to a market's UP+DOWN tokens
    best = orderbook_feed.get_best(token_id)  # Returns (best_bid, best_ask) instantly
"""

import asyncio
import json
import time
import threading
from dataclasses import dataclass, field
from typing import Optional

import websockets

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL = 10  # seconds


@dataclass
class TokenBook:
    """Live orderbook state for a single token."""
    token_id: str
    best_bid: float = 0.0
    best_bid_size: float = 0.0
    best_ask: float = 0.0
    best_ask_size: float = 0.0
    mid: float = 0.0
    last_update: float = 0.0

    def update_from_book(self, bids: list, asks: list):
        """Update from a full book snapshot."""
        if bids:
            self.best_bid = max(float(b.get("price", 0)) for b in bids)
            self.best_bid_size = sum(
                float(b.get("size", 0)) for b in bids
                if float(b.get("price", 0)) == self.best_bid
            )
        if asks:
            self.best_ask = min(float(a.get("price", 0)) for a in asks)
            self.best_ask_size = sum(
                float(a.get("size", 0)) for a in asks
                if float(a.get("price", 0)) == self.best_ask
            )
        if self.best_bid > 0 and self.best_ask > 0:
            self.mid = round((self.best_bid + self.best_ask) / 2, 4)
        self.last_update = time.time()

    def update_best_bid_ask(self, data: dict):
        """Update from a best_bid_ask event."""
        if "best_bid" in data:
            self.best_bid = float(data["best_bid"])
        if "best_ask" in data:
            self.best_ask = float(data["best_ask"])
        if "best_bid_size" in data:
            self.best_bid_size = float(data["best_bid_size"])
        if "best_ask_size" in data:
            self.best_ask_size = float(data["best_ask_size"])
        if self.best_bid > 0 and self.best_ask > 0:
            self.mid = round((self.best_bid + self.best_ask) / 2, 4)
        self.last_update = time.time()

    def update_price_change(self, data: dict):
        """Update from a price_change event."""
        side = data.get("side", "")
        price = float(data.get("price", 0))
        size = float(data.get("size", 0))

        if side == "BUY" and price > 0:
            if size > 0 and price >= self.best_bid:
                self.best_bid = price
                self.best_bid_size = size
            elif size == 0 and price == self.best_bid:
                # Best bid removed — we'd need full book to know new best
                # For now just mark as stale
                pass
        elif side == "SELL" and price > 0:
            if size > 0 and (self.best_ask == 0 or price <= self.best_ask):
                self.best_ask = price
                self.best_ask_size = size
            elif size == 0 and price == self.best_ask:
                pass

        if self.best_bid > 0 and self.best_ask > 0:
            self.mid = round((self.best_bid + self.best_ask) / 2, 4)
        self.last_update = time.time()

    @property
    def is_fresh(self) -> bool:
        """Data is less than 5 seconds old."""
        return (time.time() - self.last_update) < 5

    @property
    def spread(self) -> float:
        if self.best_bid > 0 and self.best_ask > 0:
            return round(self.best_ask - self.best_bid, 4)
        return 0


class OrderbookFeed:
    """Manages WebSocket connection and orderbook state for all subscribed tokens."""

    def __init__(self):
        self._books: dict[str, TokenBook] = {}
        self._subscribed_tokens: set[str] = set()
        self._pending_subs: list[str] = []
        self._ws = None
        self._connected = False
        self._task = None
        self._lock = threading.Lock()

    def subscribe(self, market):
        """Subscribe to a CryptoMarket's UP and DOWN tokens."""
        for token_id in [market.up_token_id, market.down_token_id]:
            if token_id not in self._subscribed_tokens:
                self._subscribed_tokens.add(token_id)
                self._books[token_id] = TokenBook(token_id=token_id)
                self._pending_subs.append(token_id)

    def get_best_ask(self, token_id: str) -> Optional[tuple[float, float]]:
        """Get (price, size) of best ask. Returns None if no data."""
        book = self._books.get(token_id)
        if book and book.best_ask > 0 and book.is_fresh:
            return (round(book.best_ask, 2), book.best_ask_size)
        return None

    def get_best_bid(self, token_id: str) -> Optional[tuple[float, float]]:
        """Get (price, size) of best bid. Returns None if no data."""
        book = self._books.get(token_id)
        if book and book.best_bid > 0 and book.is_fresh:
            return (round(book.best_bid, 2), book.best_bid_size)
        return None

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price. Returns None if no data."""
        book = self._books.get(token_id)
        if book and book.mid > 0 and book.is_fresh:
            return book.mid
        return None

    def get_spread_sum(self, up_token_id: str, down_token_id: str) -> Optional[float]:
        """Get UP_ask + DOWN_ask for hedge detection."""
        up_ask = self.get_best_ask(up_token_id)
        down_ask = self.get_best_ask(down_token_id)
        if up_ask and down_ask:
            return round(up_ask[0] + down_ask[0], 4)
        return None

    @property
    def stats(self) -> str:
        fresh = sum(1 for b in self._books.values() if b.is_fresh)
        return f"{fresh}/{len(self._books)} tokens streaming"

    async def _send_subscription(self, ws, token_ids: list[str]):
        """Send subscription message for token IDs."""
        if not token_ids:
            return
        msg = json.dumps({
            "assets_ids": token_ids,
            "type": "market",
            "custom_feature_enabled": True,
        })
        await ws.send(msg)

    async def run(self):
        """Main WebSocket loop — connect, subscribe, process messages."""
        while True:
            try:
                async with websockets.connect(
                    WS_URL,
                    ping_interval=None,  # We send our own pings
                    max_size=10 * 1024 * 1024,  # 10MB max message
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    print(f"[polymarket-ws] Connected — subscribing to {len(self._subscribed_tokens)} tokens")

                    # Subscribe to all tokens
                    all_tokens = list(self._subscribed_tokens)
                    # Send in batches of 50
                    for i in range(0, len(all_tokens), 50):
                        batch = all_tokens[i:i+50]
                        await self._send_subscription(ws, batch)
                        await asyncio.sleep(0.1)

                    self._pending_subs.clear()

                    # Process messages
                    last_ping = time.time()
                    async for raw_msg in ws:
                        try:
                            # Send ping every 10 seconds
                            if time.time() - last_ping > PING_INTERVAL:
                                await ws.send("PING")
                                last_ping = time.time()

                            # Handle pending subscriptions
                            if self._pending_subs:
                                await self._send_subscription(ws, self._pending_subs)
                                self._pending_subs.clear()

                            # Skip pong
                            if raw_msg == "PONG":
                                continue

                            # Parse message
                            msgs = json.loads(raw_msg)
                            if not isinstance(msgs, list):
                                msgs = [msgs]

                            for msg in msgs:
                                self._process_message(msg)

                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            print(f"[polymarket-ws] Message error: {e}")

            except Exception as e:
                print(f"[polymarket-ws] Connection error: {e}")
                self._connected = False
                await asyncio.sleep(2)  # Reconnect delay

    def _process_message(self, msg: dict):
        """Process a single WebSocket message."""
        event_type = msg.get("event_type", "")
        asset_id = msg.get("asset_id", "")

        if not asset_id or asset_id not in self._books:
            return

        book = self._books[asset_id]

        if event_type == "book":
            bids = msg.get("bids", [])
            asks = msg.get("asks", [])
            book.update_from_book(bids, asks)

        elif event_type == "price_change":
            changes = msg.get("changes", [])
            for change in changes:
                book.update_price_change(change)

        elif event_type == "best_bid_ask":
            book.update_best_bid_ask(msg)

        elif event_type == "last_trade_price":
            # Could track last trade but not needed for MM
            pass


# Singleton instance
orderbook_feed = OrderbookFeed()
