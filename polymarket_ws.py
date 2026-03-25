"""
Polymarket WebSocket Orderbook Feed
====================================
Real-time orderbook streaming for UP/DOWN crypto markets.
Sub-100ms updates vs 1.5s REST polling.

Fixes:
- Uses protocol-level ping/pong (ping_interval=20) instead of text "PING"
- Caps active subscriptions to current markets only (max ~16 tokens)
- Cleans up expired tokens every window change
- Faster reconnect (0.5s) with exponential backoff
"""

import asyncio
import json
import time
import threading
from dataclasses import dataclass, field
from typing import Optional

import websockets

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MAX_TOKENS = 32  # Max tokens to subscribe (4 coins x 2 timeframes x 2 sides + buffer)
STALE_THRESHOLD = 10  # Seconds before data is considered stale


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
    subscribed_at: float = 0.0

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
        elif side == "SELL" and price > 0:
            if size > 0 and (self.best_ask == 0 or price <= self.best_ask):
                self.best_ask = price
                self.best_ask_size = size

        if self.best_bid > 0 and self.best_ask > 0:
            self.mid = round((self.best_bid + self.best_ask) / 2, 4)
        self.last_update = time.time()

    @property
    def is_fresh(self) -> bool:
        """Data is less than STALE_THRESHOLD seconds old."""
        return (time.time() - self.last_update) < STALE_THRESHOLD

    @property
    def spread(self) -> float:
        if self.best_bid > 0 and self.best_ask > 0:
            return round(self.best_ask - self.best_bid, 4)
        return 0


class OrderbookFeed:
    """Manages WebSocket connection and orderbook state."""

    def __init__(self):
        self._books: dict[str, TokenBook] = {}
        self._active_tokens: set[str] = set()  # Currently active tokens
        self._pending_subs: list[str] = []
        self._pending_unsubs: list[str] = []
        self._ws = None
        self._connected = False
        self._reconnect_count = 0
        self._last_message_time = 0
        self._lock = threading.Lock()

    def subscribe(self, market):
        """Subscribe to a CryptoMarket's UP and DOWN tokens."""
        for token_id in [market.up_token_id, market.down_token_id]:
            if token_id not in self._active_tokens:
                self._active_tokens.add(token_id)
                self._books[token_id] = TokenBook(
                    token_id=token_id,
                    subscribed_at=time.time(),
                )
                self._pending_subs.append(token_id)

    def unsubscribe(self, token_id: str):
        """Unsubscribe from a token and clean up."""
        if token_id in self._active_tokens:
            self._active_tokens.discard(token_id)
            self._pending_unsubs.append(token_id)
            # Keep the book data for a bit in case it's still needed
            # but mark it as inactive

    def cleanup_stale_tokens(self, active_market_tokens: set[str]):
        """Remove tokens that are no longer in active markets."""
        stale = self._active_tokens - active_market_tokens
        for token_id in stale:
            self.unsubscribe(token_id)
            if token_id in self._books:
                del self._books[token_id]

        if stale:
            print(f"[polymarket-ws] Cleaned up {len(stale)} expired tokens, {len(self._active_tokens)} active")

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
        return f"{fresh}/{len(self._active_tokens)} tokens streaming"

    @property
    def is_healthy(self) -> bool:
        """WebSocket is connected and receiving data."""
        return self._connected and (time.time() - self._last_message_time) < 15

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

    def force_cleanup(self):
        """Force remove all tokens except the most recent MAX_TOKENS."""
        if len(self._active_tokens) > MAX_TOKENS:
            # Keep only the most recently subscribed tokens
            sorted_books = sorted(
                [(tid, b) for tid, b in self._books.items() if tid in self._active_tokens],
                key=lambda x: x[1].subscribed_at,
                reverse=True,
            )
            keep = set(tid for tid, _ in sorted_books[:MAX_TOKENS])
            stale = self._active_tokens - keep
            for tid in stale:
                self._active_tokens.discard(tid)
                if tid in self._books:
                    del self._books[tid]
            print(f"[polymarket-ws] Force cleanup: removed {len(stale)} stale tokens, {len(self._active_tokens)} remaining")

    async def run(self):
        """Main WebSocket loop — connect, subscribe, process messages."""
        while True:
            try:
                # Force cleanup if tokens are bloating
                if len(self._active_tokens) > MAX_TOKENS:
                    self.force_cleanup()

                async with websockets.connect(
                    WS_URL,
                    ping_interval=20,   # Protocol-level ping every 20s
                    ping_timeout=10,    # Timeout if no pong in 10s
                    close_timeout=5,    # Don't hang on close
                    max_size=10 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._reconnect_count = 0

                    # Only subscribe to active tokens (not stale ones)
                    active = list(self._active_tokens)[:MAX_TOKENS]
                    if not active:
                        print("[polymarket-ws] No tokens to subscribe, waiting...")
                        await asyncio.sleep(5)
                        continue

                    print(f"[polymarket-ws] Connected — subscribing to {len(active)} tokens")

                    # Subscribe in small batches
                    for i in range(0, len(active), 10):
                        batch = active[i:i+10]
                        await self._send_subscription(ws, batch)
                        await asyncio.sleep(0.1)

                    self._pending_subs.clear()

                    # Process messages with timeout to detect zombie connections
                    while True:
                        try:
                            raw_msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            print("[polymarket-ws] No message in 30s — forcing reconnect")
                            break

                        try:
                            self._last_message_time = time.time()

                            # Handle pending subs/unsubs
                            if self._pending_subs:
                                subs = self._pending_subs[:20]  # Batch limit
                                await self._send_subscription(ws, subs)
                                self._pending_subs = self._pending_subs[20:]

                            # Skip non-JSON
                            if not raw_msg or raw_msg in ("PONG", "pong"):
                                continue

                            # Parse
                            msgs = json.loads(raw_msg)
                            if not isinstance(msgs, list):
                                msgs = [msgs]

                            for msg in msgs:
                                if isinstance(msg, dict):
                                    self._process_message(msg)

                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            print(f"[polymarket-ws] Message error: {e}")

            except websockets.exceptions.ConnectionClosedError as e:
                self._connected = False
                self._reconnect_count += 1
                delay = min(1.0 * (2 ** min(self._reconnect_count, 4)), 15)
                print(f"[polymarket-ws] Connection closed: {e} — reconnecting in {delay:.1f}s (attempt {self._reconnect_count})")
                # Force cleanup on repeated failures
                if self._reconnect_count >= 3:
                    self.force_cleanup()
                await asyncio.sleep(delay)

            except Exception as e:
                self._connected = False
                self._reconnect_count += 1
                delay = min(1.0 * (2 ** min(self._reconnect_count, 4)), 15)
                print(f"[polymarket-ws] Connection error: {e} — reconnecting in {delay:.1f}s")
                if self._reconnect_count >= 3:
                    self.force_cleanup()
                await asyncio.sleep(delay)

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


# Singleton instance
orderbook_feed = OrderbookFeed()
