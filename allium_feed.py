"""
Allium on-chain data feed for Polymarket intelligence.

Provides supplementary trading signals from blockchain data:
1. Volume Flow Imbalance  — net buying pressure on UP vs DOWN
2. Smart Money Tracking   — follow wallets with >70% win rate
3. Combined confidence score that modifies trade decisions

Uses Allium's MCP endpoint (HTTP transport) to query polygon.predictions tables.
Graceful degradation: if Allium is down, bot trades without on-chain signals.

Schema reference (polygon.predictions.trades_enriched):
  - question: "Bitcoin Up or Down - March 9, 5:30PM-5:45PM ET"
  - token_outcome: "Up" or "Down"
  - usd_collateral_amount: float (USDC volume)
  - trade_price: float (0.00 - 1.00)
  - maker: address (passive order)
  - taker: address (aggressive fill)
  - is_winning_outcome: boolean
  - block_timestamp: timestamp
  - category: "crypto"
"""

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# --- Config ---
ALLIUM_API_KEY = os.getenv("ALLIUM_API_KEY", "")
ALLIUM_MCP_URL = "https://mcp.allium.so"  # No /mcp suffix — stateless MCP endpoint

# Cache settings
FLOW_CACHE_TTL = 90         # Re-query flow data every 90 seconds
SMART_CACHE_TTL = 3600      # Re-query smart wallet list every hour
SIGNAL_CACHE_TTL = 60       # Cache combined signal for 60 seconds

# Thresholds
SMART_MONEY_MIN_TRADES = 10       # Minimum trades to qualify as "smart"
SMART_MONEY_MIN_WIN_RATE = 0.70   # 70%+ win rate
SMART_MONEY_MIN_VOLUME = 500      # $500+ volume
SMART_MONEY_LOOKBACK_DAYS = 7     # Look at last 7 days

# Coin names as they appear in Polymarket questions
COIN_NAMES = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
}

# Signal weights for combined score
FLOW_WEIGHT = 0.40
SMART_MONEY_WEIGHT = 0.60


@dataclass
class AlliumSignal:
    """Supplementary signal from Allium on-chain data."""
    coin: str
    window_ts: int
    timestamp: float = 0.0

    # Flow imbalance: positive = more UP buying, negative = more DOWN buying
    flow_imbalance: float = 0.0      # -1.0 to 1.0
    flow_up_volume: float = 0.0
    flow_down_volume: float = 0.0
    flow_total_trades: int = 0

    # Smart money: what side are winning wallets betting?
    smart_money_side: Optional[str] = None  # "up", "down", or None
    smart_money_volume: float = 0.0
    smart_money_count: int = 0

    # Data quality
    has_flow_data: bool = False
    has_smart_data: bool = False
    error: Optional[str] = None

    @property
    def confidence_boost(self) -> float:
        """
        Combined confidence modifier from all Allium signals.
        Returns: -1.0 to 1.0
          Positive = favors UP side
          Negative = favors DOWN side
          Near 0 = no strong signal
        """
        score = 0.0
        total_weight = 0.0

        # Flow imbalance signal
        if self.has_flow_data and self.flow_total_trades >= 3:
            score += self.flow_imbalance * FLOW_WEIGHT
            total_weight += FLOW_WEIGHT

        # Smart money signal
        if self.has_smart_data and self.smart_money_side:
            smart_dir = 1.0 if self.smart_money_side == "up" else -1.0
            # Scale by number of smart wallets (1 wallet = partial, 3+ = full weight)
            wallet_scale = min(self.smart_money_count / 3.0, 1.0)
            score += smart_dir * wallet_scale * SMART_MONEY_WEIGHT
            total_weight += SMART_MONEY_WEIGHT

        # Normalize to actual weight used
        if total_weight > 0:
            score = score / total_weight * max(total_weight, 0.3)

        return max(-1.0, min(1.0, score))

    def confirms_side(self, side: str) -> bool:
        """Check if Allium data confirms the proposed trade side."""
        boost = self.confidence_boost
        if side == "up":
            return boost > 0.15
        else:  # down
            return boost < -0.15

    def contradicts_side(self, side: str) -> bool:
        """Check if Allium data actively contradicts the proposed trade side."""
        boost = self.confidence_boost
        if side == "up":
            return boost < -0.30  # Strong DOWN signal
        else:  # down
            return boost > 0.30   # Strong UP signal

    def summary(self) -> str:
        """Human-readable summary of the signal."""
        parts = []
        if self.has_flow_data:
            direction = "UP" if self.flow_imbalance > 0 else "DOWN"
            parts.append(f"Flow: {direction} ({self.flow_imbalance:+.2f}, {self.flow_total_trades} trades)")
        if self.has_smart_data and self.smart_money_side:
            parts.append(
                f"Smart$: {self.smart_money_side.upper()} "
                f"({self.smart_money_count} wallets, ${self.smart_money_volume:,.0f})"
            )
        if not parts:
            return "No Allium data"
        boost = self.confidence_boost
        direction = "UP" if boost > 0 else "DOWN" if boost < 0 else "NEUTRAL"
        return f"[{direction} {boost:+.2f}] " + " | ".join(parts)


class AlliumFeed:
    """
    On-chain data feed from Allium's MCP endpoint.

    Queries polygon.predictions.trades_enriched for:
    - Trade flow data (volume per side per coin per window)
    - Smart money wallet activity (high win-rate wallets)

    All queries are cached and the feed degrades gracefully if Allium is unreachable.
    """

    def __init__(self):
        self._signal_cache: dict[tuple, AlliumSignal] = {}
        self._query_cache: dict[int, tuple[float, list]] = {}
        self._smart_wallets_cache: tuple[float, list] = (0, [])
        self._initialized: bool = False
        self._available: bool = True
        self._last_error_ts: float = 0
        self._error_backoff: float = 60  # Seconds to wait after error

    # ═══════════════════════════════════════════════
    #  PUBLIC API
    # ═══════════════════════════════════════════════

    def test_connection(self) -> bool:
        """Test Allium connectivity. Returns True if working."""
        if not ALLIUM_API_KEY:
            print("[allium] No API key configured (set ALLIUM_API_KEY in .env)")
            return False

        try:
            rows = self._run_sql("SELECT 1 as test_value")
            if rows:
                print("[allium] Connection OK")
                self._available = True
                return True
            else:
                print("[allium] Connection failed: empty response")
                self._available = False
                return False
        except Exception as e:
            print(f"[allium] Connection failed: {e}")
            self._available = False
            return False

    def get_signal(self, coin: str, window_ts: int) -> AlliumSignal:
        """
        Get combined Allium signal for a coin's current window.
        Returns cached signal if fresh, otherwise queries Allium.
        Always returns an AlliumSignal (never raises).
        """
        if not self._available:
            if time.time() - self._last_error_ts < self._error_backoff:
                return AlliumSignal(coin=coin, window_ts=window_ts, error="Allium unavailable")
            self._available = True  # Try again

        if not ALLIUM_API_KEY:
            return AlliumSignal(coin=coin, window_ts=window_ts, error="No API key")

        # Check signal cache
        cache_key = (coin, window_ts)
        if cache_key in self._signal_cache:
            cached = self._signal_cache[cache_key]
            if time.time() - cached.timestamp < SIGNAL_CACHE_TTL:
                return cached

        # Build fresh signal
        signal = AlliumSignal(coin=coin, window_ts=window_ts, timestamp=time.time())

        # Query flow imbalance
        try:
            imbalance, up_vol, down_vol, trade_count = self._get_flow_imbalance(coin, window_ts)
            signal.flow_imbalance = imbalance
            signal.flow_up_volume = up_vol
            signal.flow_down_volume = down_vol
            signal.flow_total_trades = trade_count
            signal.has_flow_data = True
        except Exception as e:
            signal.error = f"Flow: {e}"

        # Query smart money
        try:
            side, vol, count = self._get_smart_money_activity(coin, window_ts)
            signal.smart_money_side = side
            signal.smart_money_volume = vol
            signal.smart_money_count = count
            signal.has_smart_data = True
        except Exception as e:
            err = f"Smart$: {e}"
            signal.error = f"{signal.error}; {err}" if signal.error else err

        # Cache it
        self._signal_cache[cache_key] = signal
        return signal

    # ═══════════════════════════════════════════════
    #  SIGNAL QUERIES
    # ═══════════════════════════════════════════════

    def _get_flow_imbalance(self, coin: str, window_ts: int) -> tuple[float, float, float, int]:
        """
        Get volume flow imbalance from RECENT completed windows.

        Allium has ~1h indexing lag, so we can't query the current window.
        Instead, we look at the last 3 hours of completed crypto updown trades
        to gauge the prevailing direction (trend signal).

        Returns: (imbalance_ratio, up_volume, down_volume, total_trades)
        imbalance_ratio: -1 (all DOWN buying) to +1 (all UP buying)
        """
        coin_name = COIN_NAMES.get(coin, coin)

        sql = f"""
        SELECT
            token_outcome,
            SUM(usd_collateral_amount) as total_volume,
            COUNT(*) as trade_count
        FROM polygon.predictions.trades_enriched
        WHERE category = 'crypto'
          AND LOWER(question) LIKE '%{coin_name.lower()} up or down%'
          AND block_timestamp >= DATEADD(hour, -3, CURRENT_TIMESTAMP())
        GROUP BY token_outcome
        """

        rows = self._run_sql(sql, cache_ttl=FLOW_CACHE_TTL)

        up_vol = 0.0
        down_vol = 0.0
        total_trades = 0

        for row in rows:
            outcome = str(row.get("token_outcome", row.get("TOKEN_OUTCOME", ""))).lower()
            vol = float(row.get("total_volume", row.get("TOTAL_VOLUME", 0)) or 0)
            trades = int(row.get("trade_count", row.get("TRADE_COUNT", 0)) or 0)

            if outcome == "up":
                up_vol = vol
            elif outcome == "down":
                down_vol = vol
            total_trades += trades

        # Calculate imbalance
        total = up_vol + down_vol
        if total == 0:
            return 0.0, 0.0, 0.0, total_trades

        imbalance = (up_vol - down_vol) / total  # -1 to +1
        return imbalance, up_vol, down_vol, total_trades

    def _get_smart_money_activity(self, coin: str, window_ts: int) -> tuple[Optional[str], float, int]:
        """
        Check what smart money wallets have been doing recently (last 3 hours).

        Since Allium has ~1h lag, we look at the recent trend of smart wallets
        rather than their activity in the current window.

        Returns: (dominant_side, total_volume, wallet_count)
        """
        smart_wallets = self._get_smart_wallet_list()
        if not smart_wallets:
            return None, 0.0, 0

        coin_name = COIN_NAMES.get(coin, coin)

        # Use first 20 wallets to keep query fast
        wallet_list = ", ".join(f"'{w}'" for w in smart_wallets[:20])

        sql = f"""
        SELECT
            token_outcome,
            SUM(usd_collateral_amount) as volume,
            COUNT(DISTINCT taker) as wallet_count
        FROM polygon.predictions.trades_enriched
        WHERE category = 'crypto'
          AND LOWER(question) LIKE '%{coin_name.lower()} up or down%'
          AND block_timestamp >= DATEADD(hour, -3, CURRENT_TIMESTAMP())
          AND taker IN ({wallet_list})
        GROUP BY token_outcome
        """

        rows = self._run_sql(sql, cache_ttl=FLOW_CACHE_TTL)

        up_vol = 0.0
        down_vol = 0.0
        up_wallets = 0
        down_wallets = 0

        for row in rows:
            outcome = str(row.get("token_outcome", row.get("TOKEN_OUTCOME", ""))).lower()
            vol = float(row.get("volume", row.get("VOLUME", 0)) or 0)
            wallets = int(row.get("wallet_count", row.get("WALLET_COUNT", 0)) or 0)

            if outcome == "up":
                up_vol += vol
                up_wallets += wallets
            elif outcome == "down":
                down_vol += vol
                down_wallets += wallets

        total_vol = up_vol + down_vol
        total_wallets = up_wallets + down_wallets

        if total_vol == 0:
            return None, 0.0, 0

        if up_vol > down_vol * 1.5:
            return "up", total_vol, total_wallets
        elif down_vol > up_vol * 1.5:
            return "down", total_vol, total_wallets

        return None, total_vol, total_wallets

    def _get_smart_wallet_list(self) -> list[str]:
        """
        Get list of smart money wallet addresses (takers with >70% win rate).
        Cached for SMART_CACHE_TTL (1 hour).
        """
        cache_ts, cached_wallets = self._smart_wallets_cache
        if cached_wallets and time.time() - cache_ts < SMART_CACHE_TTL:
            return cached_wallets

        sql = f"""
        SELECT
            taker as wallet_address,
            COUNT(*) as total_trades,
            SUM(CASE WHEN is_winning_outcome = true THEN 1 ELSE 0 END) as wins,
            SUM(usd_collateral_amount) as total_volume
        FROM polygon.predictions.trades_enriched
        WHERE category = 'crypto'
          AND LOWER(question) LIKE '%up or down%'
          AND block_timestamp >= DATEADD(day, -{SMART_MONEY_LOOKBACK_DAYS}, CURRENT_TIMESTAMP())
        GROUP BY taker
        HAVING COUNT(*) >= {SMART_MONEY_MIN_TRADES}
          AND SUM(CASE WHEN is_winning_outcome = true THEN 1 ELSE 0 END)::FLOAT / NULLIF(COUNT(*), 0) >= {SMART_MONEY_MIN_WIN_RATE}
          AND SUM(usd_collateral_amount) >= {SMART_MONEY_MIN_VOLUME}
        ORDER BY total_volume DESC
        LIMIT 50
        """

        try:
            rows = self._run_sql(sql, cache_ttl=SMART_CACHE_TTL)
        except Exception:
            return cached_wallets if cached_wallets else []

        wallets = []
        for row in rows:
            addr = row.get("wallet_address", row.get("WALLET_ADDRESS", ""))
            if addr:
                wallets.append(addr)

        if wallets:
            self._smart_wallets_cache = (time.time(), wallets)
            print(f"[allium] Loaded {len(wallets)} smart money wallets")

        return wallets

    # ═══════════════════════════════════════════════
    #  BRACKET MARKET QUERIES (v4)
    # ═══════════════════════════════════════════════

    def get_bracket_signal(self, coin: str, slug: str) -> AlliumSignal:
        """
        Get Allium signal for a daily bracket market (v4).

        Queries recent trades on "Bitcoin above" / "Ethereum above" markets
        instead of the 15-min "up or down" markets used by v3.5.

        Args:
            coin: "BTC" or "ETH"
            slug: Event slug for cache keying

        Returns:
            AlliumSignal with flow and smart money data for bracket markets.
        """
        if not self._available:
            if time.time() - self._last_error_ts < self._error_backoff:
                return AlliumSignal(coin=coin, window_ts=0, error="Allium unavailable")
            self._available = True

        if not ALLIUM_API_KEY:
            return AlliumSignal(coin=coin, window_ts=0, error="No API key")

        # Check signal cache (keyed by slug)
        cache_key = ("bracket", coin, slug)
        if cache_key in self._signal_cache:
            cached = self._signal_cache[cache_key]
            if time.time() - cached.timestamp < SIGNAL_CACHE_TTL:
                return cached

        signal = AlliumSignal(coin=coin, window_ts=0, timestamp=time.time())

        # Query bracket flow imbalance
        try:
            imbalance, yes_vol, no_vol, trade_count = self._get_bracket_flow(coin)
            signal.flow_imbalance = imbalance
            signal.flow_up_volume = yes_vol      # Reuse up/down fields for yes/no
            signal.flow_down_volume = no_vol
            signal.flow_total_trades = trade_count
            signal.has_flow_data = True
        except Exception as e:
            signal.error = f"Bracket flow: {e}"

        # Query smart money on bracket markets
        try:
            side, vol, count = self._get_bracket_smart_money(coin)
            signal.smart_money_side = side
            signal.smart_money_volume = vol
            signal.smart_money_count = count
            signal.has_smart_data = True
        except Exception as e:
            err = f"Bracket smart$: {e}"
            signal.error = f"{signal.error}; {err}" if signal.error else err

        self._signal_cache[cache_key] = signal
        return signal

    def _get_bracket_flow(self, coin: str) -> tuple[float, float, float, int]:
        """
        Get volume flow on daily bracket markets (last 6 hours).

        For bracket markets, "Yes" = bullish (price will be above threshold),
        "No" = bearish. We aggregate across all thresholds for the coin.

        Returns: (imbalance, yes_volume, no_volume, total_trades)
        """
        coin_name = COIN_NAMES.get(coin, coin)

        sql = f"""
        SELECT
            token_outcome,
            SUM(usd_collateral_amount) as total_volume,
            COUNT(*) as trade_count
        FROM polygon.predictions.trades_enriched
        WHERE category = 'crypto'
          AND LOWER(question) LIKE '%{coin_name.lower()} % above%'
          AND block_timestamp >= DATEADD(hour, -6, CURRENT_TIMESTAMP())
        GROUP BY token_outcome
        """

        rows = self._run_sql(sql, cache_ttl=FLOW_CACHE_TTL)

        yes_vol = 0.0
        no_vol = 0.0
        total_trades = 0

        for row in rows:
            outcome = str(row.get("token_outcome", row.get("TOKEN_OUTCOME", ""))).lower()
            vol = float(row.get("total_volume", row.get("TOTAL_VOLUME", 0)) or 0)
            trades = int(row.get("trade_count", row.get("TRADE_COUNT", 0)) or 0)

            if outcome == "yes":
                yes_vol = vol
            elif outcome == "no":
                no_vol = vol
            total_trades += trades

        total = yes_vol + no_vol
        if total == 0:
            return 0.0, 0.0, 0.0, total_trades

        imbalance = (yes_vol - no_vol) / total
        return imbalance, yes_vol, no_vol, total_trades

    def _get_bracket_smart_money(self, coin: str) -> tuple[Optional[str], float, int]:
        """
        Check smart money activity on daily bracket markets (last 6 hours).

        Returns: (dominant_side, total_volume, wallet_count)
        dominant_side is "up" (buying YES = bullish) or "down" (buying NO = bearish)
        """
        smart_wallets = self._get_smart_wallet_list()
        if not smart_wallets:
            return None, 0.0, 0

        coin_name = COIN_NAMES.get(coin, coin)
        wallet_list = ", ".join(f"'{w}'" for w in smart_wallets[:20])

        sql = f"""
        SELECT
            token_outcome,
            SUM(usd_collateral_amount) as volume,
            COUNT(DISTINCT taker) as wallet_count
        FROM polygon.predictions.trades_enriched
        WHERE category = 'crypto'
          AND LOWER(question) LIKE '%{coin_name.lower()} % above%'
          AND block_timestamp >= DATEADD(hour, -6, CURRENT_TIMESTAMP())
          AND taker IN ({wallet_list})
        GROUP BY token_outcome
        """

        rows = self._run_sql(sql, cache_ttl=FLOW_CACHE_TTL)

        yes_vol = 0.0
        no_vol = 0.0
        yes_wallets = 0
        no_wallets = 0

        for row in rows:
            outcome = str(row.get("token_outcome", row.get("TOKEN_OUTCOME", ""))).lower()
            vol = float(row.get("volume", row.get("VOLUME", 0)) or 0)
            wallets = int(row.get("wallet_count", row.get("WALLET_COUNT", 0)) or 0)

            if outcome == "yes":
                yes_vol += vol
                yes_wallets += wallets
            elif outcome == "no":
                no_vol += vol
                no_wallets += wallets

        total_vol = yes_vol + no_vol
        total_wallets = yes_wallets + no_wallets

        if total_vol == 0:
            return None, 0.0, 0

        if yes_vol > no_vol * 1.5:
            return "up", total_vol, total_wallets
        elif no_vol > yes_vol * 1.5:
            return "down", total_vol, total_wallets

        return None, total_vol, total_wallets

    # ═══════════════════════════════════════════════
    #  MCP TRANSPORT LAYER
    # ═══════════════════════════════════════════════

    def _run_sql(self, sql: str, cache_ttl: float = FLOW_CACHE_TTL) -> list[dict]:
        """Execute SQL query against Allium via MCP endpoint."""
        cache_key = hash(sql)
        if cache_key in self._query_cache:
            ts, result = self._query_cache[cache_key]
            if time.time() - ts < cache_ttl:
                return result

        try:
            if not self._initialized:
                self._init_mcp()

            payload = {
                "jsonrpc": "2.0",
                "id": int(time.time() * 1000) % 1000000,
                "method": "tools/call",
                "params": {
                    "name": "explorer_run_sql",
                    "arguments": {"sql": sql}
                }
            }

            resp = requests.post(
                ALLIUM_MCP_URL,
                json=payload,
                headers=self._headers(),
                timeout=45,
            )

            if resp.status_code != 200:
                raise Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")

            result = self._parse_response(resp.text)

            self._query_cache[cache_key] = (time.time(), result)
            return result

        except Exception as e:
            self._available = False
            self._last_error_ts = time.time()
            raise

    def _init_mcp(self):
        """Initialize MCP session (stateless — Allium doesn't require sessions)."""
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "polymarket-sniper-bot", "version": "3.2"}
                }
            }
            requests.post(
                ALLIUM_MCP_URL,
                json=payload,
                headers=self._headers(),
                timeout=15,
            )
            self._initialized = True
        except Exception as e:
            print(f"[allium] MCP init: {e}")
            self._initialized = True  # Try anyway

    def _headers(self) -> dict:
        return {
            "X-API-KEY": ALLIUM_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

    def _parse_response(self, text: str) -> list[dict]:
        """Parse MCP response (SSE format from Allium)."""
        for line in text.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            json_str = line[5:].strip()
            if not json_str:
                continue

            data = json.loads(json_str)

            # Check for error
            is_error = data.get("result", {}).get("isError", False)
            if is_error:
                content = data.get("result", {}).get("content", [])
                err_msg = content[0].get("text", "Unknown error") if content else "Unknown error"
                raise Exception(f"SQL error: {err_msg}")

            # Try structuredContent first (cleaner)
            structured = data.get("result", {}).get("structuredContent", {}).get("result", {})
            if structured and "data" in structured:
                return structured["data"]

            # Fall back to content text parsing
            content = data.get("result", {}).get("content", [])
            for item in content:
                if item.get("type") != "text":
                    continue
                text_val = item.get("text", "")
                if not text_val:
                    continue
                try:
                    parsed = json.loads(text_val)
                    if isinstance(parsed, dict) and "data" in parsed:
                        return parsed["data"]
                    if isinstance(parsed, list):
                        return parsed
                except json.JSONDecodeError:
                    continue

        return []


    # ═══════════════════════════════════════════════
    #  WEATHER MARKET QUERIES
    # ═══════════════════════════════════════════════

    def get_weather_signal(self, city: str, question: str) -> AlliumSignal:
        """
        Get Allium signal for a weather bracket market.

        Checks what smart money wallets (70%+ win rate on weather) are betting
        on the same city's temperature brackets today.

        Args:
            city: City name (e.g., "Chicago", "Dallas")
            question: Full Polymarket question for cache keying

        Returns:
            AlliumSignal with smart money data for weather markets.
        """
        if not self._available:
            if time.time() - self._last_error_ts < self._error_backoff:
                return AlliumSignal(coin=city, window_ts=0, error="Allium unavailable")
            self._available = True

        if not ALLIUM_API_KEY:
            return AlliumSignal(coin=city, window_ts=0, error="No API key")

        # Check signal cache
        cache_key = ("weather", city, question[:60])
        if cache_key in self._signal_cache:
            cached = self._signal_cache[cache_key]
            if time.time() - cached.timestamp < SIGNAL_CACHE_TTL:
                return cached

        signal = AlliumSignal(coin=city, window_ts=0, timestamp=time.time())

        # Query smart money activity on this city's weather markets
        try:
            side, vol, count = self._get_weather_smart_money(city, question)
            signal.smart_money_side = side
            signal.smart_money_volume = vol
            signal.smart_money_count = count
            signal.has_smart_data = True
        except Exception as e:
            signal.error = f"Weather smart$: {e}"

        # Query flow imbalance on this specific question
        try:
            imbalance, yes_vol, no_vol, trade_count = self._get_weather_flow(question)
            signal.flow_imbalance = imbalance
            signal.flow_up_volume = yes_vol
            signal.flow_down_volume = no_vol
            signal.flow_total_trades = trade_count
            signal.has_flow_data = True
        except Exception as e:
            err = f"Weather flow: {e}"
            signal.error = f"{signal.error}; {err}" if signal.error else err

        self._signal_cache[cache_key] = signal
        return signal

    def _get_weather_smart_money(self, city: str, question: str) -> tuple[Optional[str], float, int]:
        """
        Check what smart money wallets are betting on this city's weather markets.

        Uses the weather-specific smart wallet list (cached separately).

        Returns: (dominant_side, total_volume, wallet_count)
        dominant_side is "up" (buying YES = temperature will be in this range)
                     or "down" (buying NO)
        """
        smart_wallets = self._get_weather_smart_wallet_list()
        if not smart_wallets:
            return None, 0.0, 0

        city_lower = city.lower()
        wallet_list = ", ".join(f"'{w}'" for w in smart_wallets[:20])

        sql = f"""
        SELECT
            token_outcome,
            SUM(usd_collateral_amount) as volume,
            COUNT(DISTINCT taker) as wallet_count
        FROM polygon.predictions.trades_enriched
        WHERE LOWER(question) LIKE '%temperature%{city_lower}%'
          AND block_timestamp >= DATEADD(hour, -12, CURRENT_TIMESTAMP())
          AND taker IN ({wallet_list})
        GROUP BY token_outcome
        """

        rows = self._run_sql(sql, cache_ttl=FLOW_CACHE_TTL)

        yes_vol = 0.0
        no_vol = 0.0
        yes_wallets = 0
        no_wallets = 0

        for row in rows:
            outcome = str(row.get("token_outcome", row.get("TOKEN_OUTCOME", ""))).lower()
            vol = float(row.get("volume", row.get("VOLUME", 0)) or 0)
            wallets = int(row.get("wallet_count", row.get("WALLET_COUNT", 0)) or 0)

            if outcome == "yes":
                yes_vol += vol
                yes_wallets += wallets
            elif outcome == "no":
                no_vol += vol
                no_wallets += wallets

        total_vol = yes_vol + no_vol
        total_wallets = yes_wallets + no_wallets

        if total_vol == 0:
            return None, 0.0, 0

        if yes_vol > no_vol * 1.5:
            return "up", total_vol, total_wallets  # "up" = YES side
        elif no_vol > yes_vol * 1.5:
            return "down", total_vol, total_wallets  # "down" = NO side

        return None, total_vol, total_wallets

    def _get_weather_flow(self, question: str) -> tuple[float, float, float, int]:
        """
        Get volume flow on a specific weather question (last 12 hours).

        Returns: (imbalance, yes_volume, no_volume, total_trades)
        """
        # Escape single quotes in question text for SQL
        q_escaped = question.replace("'", "''")[:100]

        sql = f"""
        SELECT
            token_outcome,
            SUM(usd_collateral_amount) as total_volume,
            COUNT(*) as trade_count
        FROM polygon.predictions.trades_enriched
        WHERE LOWER(question) LIKE '%{q_escaped.lower()[:60]}%'
          AND block_timestamp >= DATEADD(hour, -12, CURRENT_TIMESTAMP())
        GROUP BY token_outcome
        """

        rows = self._run_sql(sql, cache_ttl=FLOW_CACHE_TTL)

        yes_vol = 0.0
        no_vol = 0.0
        total_trades = 0

        for row in rows:
            outcome = str(row.get("token_outcome", row.get("TOKEN_OUTCOME", ""))).lower()
            vol = float(row.get("total_volume", row.get("TOTAL_VOLUME", 0)) or 0)
            trades = int(row.get("trade_count", row.get("TRADE_COUNT", 0)) or 0)

            if outcome == "yes":
                yes_vol = vol
            elif outcome == "no":
                no_vol = vol
            total_trades += trades

        total = yes_vol + no_vol
        if total == 0:
            return 0.0, 0.0, 0.0, total_trades

        imbalance = (yes_vol - no_vol) / total
        return imbalance, yes_vol, no_vol, total_trades

    def _get_weather_smart_wallet_list(self) -> list[str]:
        """
        Get list of smart money wallet addresses for weather markets.
        Wallets with >70% win rate on temperature markets, last 7 days.
        Cached for SMART_CACHE_TTL (1 hour).
        """
        cache_key = "weather_smart_wallets"
        # Use a separate cache slot for weather wallets
        if hasattr(self, '_weather_wallets_cache'):
            cache_ts, cached_wallets = self._weather_wallets_cache
            if cached_wallets and time.time() - cache_ts < SMART_CACHE_TTL:
                return cached_wallets

        sql = f"""
        SELECT
            taker as wallet_address,
            COUNT(*) as total_trades,
            SUM(CASE WHEN is_winning_outcome = true THEN 1 ELSE 0 END) as wins,
            SUM(usd_collateral_amount) as total_volume
        FROM polygon.predictions.trades_enriched
        WHERE LOWER(question) LIKE '%temperature%'
          AND block_timestamp >= DATEADD(day, -{SMART_MONEY_LOOKBACK_DAYS}, CURRENT_TIMESTAMP())
        GROUP BY taker
        HAVING COUNT(*) >= {SMART_MONEY_MIN_TRADES}
          AND SUM(CASE WHEN is_winning_outcome = true THEN 1 ELSE 0 END)::FLOAT / NULLIF(COUNT(*), 0) >= {SMART_MONEY_MIN_WIN_RATE}
          AND SUM(usd_collateral_amount) >= {SMART_MONEY_MIN_VOLUME}
        ORDER BY total_volume DESC
        LIMIT 50
        """

        try:
            rows = self._run_sql(sql, cache_ttl=SMART_CACHE_TTL)
        except Exception:
            if hasattr(self, '_weather_wallets_cache'):
                return self._weather_wallets_cache[1]
            return []

        wallets = []
        for row in rows:
            addr = row.get("wallet_address", row.get("WALLET_ADDRESS", ""))
            if addr:
                wallets.append(addr)

        if wallets:
            self._weather_wallets_cache = (time.time(), wallets)
            print(f"[allium] Loaded {len(wallets)} weather smart money wallets")

        return wallets


# ═══════════════════════════════════════════════
#  GLOBAL INSTANCE
# ═══════════════════════════════════════════════

allium = AlliumFeed()


# ═══════════════════════════════════════════════
#  STANDALONE TEST
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    from rich.console import Console

    console = Console()
    console.print("\n[bold cyan]═══ Allium Feed Test ═══[/bold cyan]\n")

    # Test connection
    console.print("Testing connection...")
    if not allium.test_connection():
        console.print("[red]Connection failed. Check ALLIUM_API_KEY in .env[/red]")
        sys.exit(1)

    console.print("[green]Connected![/green]\n")

    # Get current window
    now = int(time.time())
    window_ts = (now // 900) * 900
    window_time = datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime('%H:%M UTC')

    console.print(f"Current window: {window_time} (ts={window_ts})")
    console.print(f"Querying signals for BTC, ETH, SOL...\n")

    for coin in ["BTC", "ETH", "SOL"]:
        console.print(f"[bold]{coin}:[/bold]")
        signal = allium.get_signal(coin, window_ts)
        console.print(f"  {signal.summary()}")
        if signal.error:
            console.print(f"  [yellow]Error: {signal.error}[/yellow]")
        console.print()

    # Show smart wallet list
    console.print("[bold]Smart Money Wallets:[/bold]")
    wallets = allium._get_smart_wallet_list()
    if wallets:
        for w in wallets[:10]:
            console.print(f"  {w}")
        if len(wallets) > 10:
            console.print(f"  ... and {len(wallets) - 10} more")
    else:
        console.print("  [dim]None found (or query failed)[/dim]")
