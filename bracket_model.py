"""
Probability models for daily bracket markets.

Crypto: Log-normal (Black-Scholes digital option) using Binance vol
Weather: Normal distribution around NOAA/Open-Meteo forecast
"""

import math
from dataclasses import dataclass

import requests


# ── Normal CDF (no scipy dependency) ─────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF using error function approximation."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ── Crypto Volatility Estimation ─────────────────────────────────────

def estimate_volatility(coin: str = "BTC", lookback_hours: int = 72) -> float:
    """Estimate annualized volatility from Binance hourly klines.

    Returns annualized volatility (e.g., 0.60 = 60% annual vol).
    """
    symbol_map = {
        "BTC": "BTCUSDT",
        "ETH": "ETHUSDT",
        "SOL": "SOLUSDT",
    }
    symbol = symbol_map.get(coin, f"{coin}USDT")

    try:
        resp = requests.get(
            "https://data-api.binance.vision/api/v3/klines",
            params={
                "symbol": symbol,
                "interval": "1h",
                "limit": lookback_hours,
            },
            timeout=10,
        )
        resp.raise_for_status()
        klines = resp.json()

        if len(klines) < 10:
            print(f"[model] Not enough klines for {coin}: {len(klines)}")
            return 0.60  # Default fallback

        # Extract closing prices
        closes = [float(k[4]) for k in klines]

        # Compute hourly log returns
        log_returns = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0:
                lr = math.log(closes[i] / closes[i - 1])
                log_returns.append(lr)

        if not log_returns:
            return 0.60

        # Standard deviation of hourly returns
        mean = sum(log_returns) / len(log_returns)
        variance = sum((r - mean) ** 2 for r in log_returns) / len(log_returns)
        hourly_vol = math.sqrt(variance)

        # Annualize: hourly_vol × sqrt(hours_per_year)
        annual_vol = hourly_vol * math.sqrt(8760)

        return annual_vol

    except Exception as e:
        print(f"[model] Volatility estimation failed for {coin}: {e}")
        return 0.60  # Reasonable default


# ── Crypto Bracket Probability ────────────────────────────────────────

def crypto_bracket_prob(
    current_price: float,
    threshold: float,
    hours_remaining: float,
    volatility: float,
) -> float:
    """Compute P(price > threshold at resolution) using log-normal model.

    This is essentially a Black-Scholes digital call option price.

    Args:
        current_price: Current Binance price
        threshold: Bracket threshold (e.g., 72000.0)
        hours_remaining: Hours until market resolution
        volatility: Annualized volatility (e.g., 0.60)

    Returns:
        Probability between 0 and 1
    """
    if current_price <= 0 or threshold <= 0:
        return 0.5

    if hours_remaining <= 0:
        # Already resolved
        return 1.0 if current_price > threshold else 0.0

    # Time to expiry in years
    tau = hours_remaining / 8760.0

    # No drift (mu=0) for short timeframes
    # d2 = (ln(S/K) - 0.5*sigma^2*tau) / (sigma*sqrt(tau))
    # P(S_T > K) = Phi(d2)
    sigma_sqrt_tau = volatility * math.sqrt(tau)

    if sigma_sqrt_tau < 1e-10:
        return 1.0 if current_price > threshold else 0.0

    d2 = (math.log(current_price / threshold) - 0.5 * volatility**2 * tau) / sigma_sqrt_tau

    return _norm_cdf(d2)


# ── Weather Bracket Probability ───────────────────────────────────────

# Forecast error standard deviations (empirical estimates)
# NOAA next-day high temp forecast error: ~2.5°F / ~1.4°C
# Open-Meteo next-day: slightly worse ~3.0°F / ~1.7°C
# Same-day (hours away): much tighter ~1.5°F / ~0.8°C

FORECAST_ERROR_STD = {
    "noaa": {
        "same_day": 1.5,      # °F, when resolution is today
        "next_day": 2.5,      # °F, when resolution is tomorrow
        "two_days": 3.5,      # °F
    },
    "open-meteo": {
        "same_day": 1.0,      # °C
        "next_day": 1.7,      # °C
        "two_days": 2.2,      # °C
    },
}


def _get_forecast_std(source: str, hours_remaining: float, unit: str,
                      override_std: float | None = None, confidence: float = 1.0) -> float:
    """Get forecast error standard deviation based on source and timeframe.

    Args:
        source: "noaa", "open-meteo", "ensemble", "observation"
        hours_remaining: Hours until market resolution
        unit: "°F" or "°C"
        override_std: If provided (from ensemble/observation), use this directly
        confidence: Multiplier from ensemble (>1 = tighter, <1 = wider)
    """
    # Observation data — very tight uncertainty
    if source == "observation" or override_std is not None:
        if override_std is not None:
            std = override_std
        else:
            std = 0.5  # °C for observations
        # Convert if needed
        if unit == "°F" and source in ("observation", "ensemble"):
            std = std * 9 / 5
        return std

    errors = FORECAST_ERROR_STD.get(source, FORECAST_ERROR_STD["open-meteo"])

    if hours_remaining < 12:
        std = errors["same_day"]
    elif hours_remaining < 36:
        std = errors["next_day"]
    else:
        std = errors["two_days"]

    # If NOAA but unit is °C (shouldn't happen, but just in case)
    if source == "noaa" and unit == "°C":
        std = std * 5 / 9

    # Apply confidence multiplier from ensemble
    # confidence > 1 = models agree = tighter std
    # confidence < 1 = models disagree = wider std
    if confidence != 1.0 and confidence > 0:
        std = std / confidence

    return std


def weather_bracket_prob(
    forecast_temp: float,
    bracket_low: float,
    bracket_high: float | None,
    bracket_type: str,
    hours_remaining: float,
    source: str = "noaa",
    unit: str = "°F",
    confidence: float = 1.0,
    forecast_std_override: float | None = None,
) -> float:
    """Compute probability that actual high temp falls in bracket.

    Models the actual temperature as:
        T_actual ~ Normal(forecast_temp, forecast_error_std)

    Args:
        forecast_temp: NOAA/Open-Meteo forecast high temp
        bracket_low: Lower bound of bracket (or threshold for at_or_below/at_or_above)
        bracket_high: Upper bound of bracket (None for at_or_below/at_or_above)
        bracket_type: "range", "at_or_below", "at_or_above"
        hours_remaining: Hours until resolution
        source: "noaa" or "open-meteo"
        unit: "°F" or "°C"
        confidence: Ensemble confidence (>1 = tighter, <1 = wider)
        forecast_std_override: Direct std override from ensemble/observation

    Returns:
        Probability between 0 and 1
    """
    std = _get_forecast_std(source, hours_remaining, unit,
                            override_std=forecast_std_override, confidence=confidence)

    if std < 0.01:
        std = 0.01  # Prevent division by zero

    if bracket_type == "at_or_below":
        # P(T ≤ threshold)
        # For weather brackets, "75°F or below" means the actual temp is at most 75
        # The bracket boundary is typically X.5 (e.g., ≤75 means T < 75.5)
        z = (bracket_low + 0.5 - forecast_temp) / std
        return _norm_cdf(z)

    elif bracket_type == "at_or_above":
        # P(T ≥ threshold)
        # "90°F or higher" means T ≥ 89.5
        z = (bracket_low - 0.5 - forecast_temp) / std
        return 1.0 - _norm_cdf(z)

    elif bracket_type == "range":
        # P(low ≤ T ≤ high)
        # For "76-77°F", actual bracket is 75.5 to 77.5
        if bracket_high is None:
            bracket_high = bracket_low

        z_low = (bracket_low - 0.5 - forecast_temp) / std
        z_high = (bracket_high + 0.5 - forecast_temp) / std
        raw_prob = _norm_cdf(z_high) - _norm_cdf(z_low)

        # ── Confidence discount for narrow brackets ──
        # Single-degree ranges are very hard to hit — model overestimates
        # Apply a discount to account for model uncertainty
        bracket_width = bracket_high - bracket_low
        if bracket_width <= 1:
            # Single-degree (e.g., "exactly 17°C") — hardest to predict
            return raw_prob * 0.6
        elif bracket_width <= 3:
            # Small range (e.g., "40-41°F", 2°F range) — still tricky
            return raw_prob * 0.8
        else:
            return raw_prob

    else:
        return 0.5  # Unknown type


# ── GFS Ensemble Bracket Probability ─────────────────────────────────

def _c_to_f(temp_c: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return temp_c * 9.0 / 5.0 + 32.0


def ensemble_bracket_prob(
    ensemble_maxes: list[float],
    bracket_low: float,
    bracket_high: float | None,
    bracket_type: str,
    unit: str,
) -> float:
    """Compute bracket probability by counting GFS ensemble members.

    The ensemble API returns temperatures in °C. If the bracket is in °F,
    we convert each ensemble member before comparing.

    Args:
        ensemble_maxes: List of max-temp values from ensemble (°C)
        bracket_low: Lower bound / threshold
        bracket_high: Upper bound (for range type) or None
        bracket_type: "range", "at_or_below", "at_or_above"
        unit: "°F" or "°C" — the unit of the bracket thresholds

    Returns:
        Probability between 0 and 1 (hits / total members)
    """
    if not ensemble_maxes:
        return 0.5

    # Convert ensemble temps to bracket unit if needed
    if unit == "°F":
        temps = [_c_to_f(t) for t in ensemble_maxes]
    else:
        temps = list(ensemble_maxes)

    total = len(temps)
    hits = 0

    if bracket_type == "at_or_above":
        # P(T >= threshold) — use threshold - 0.5 for rounding
        cutoff = bracket_low - 0.5
        hits = sum(1 for t in temps if t >= cutoff)

    elif bracket_type == "at_or_below":
        # P(T <= threshold) — use threshold + 0.5 for rounding
        cutoff = bracket_low + 0.5
        hits = sum(1 for t in temps if t <= cutoff)

    elif bracket_type == "range":
        if bracket_high is None:
            bracket_high = bracket_low
        low_cutoff = bracket_low - 0.5
        high_cutoff = bracket_high + 0.5
        hits = sum(1 for t in temps if low_cutoff <= t <= high_cutoff)

    else:
        return 0.5

    return hits / total


# ── Bracket Scoring ───────────────────────────────────────────────────

@dataclass
class BracketScore:
    """Score for a single bracket showing edge vs Polymarket price."""
    question: str
    threshold: float
    model_prob_yes: float      # Our model P(yes)
    poly_yes_price: float      # Polymarket YES price
    poly_no_price: float       # Polymarket NO price
    edge_yes: float            # model_prob - poly_yes - fee
    edge_no: float             # (1 - model_prob) - poly_no - fee
    best_side: str             # "yes" or "no"
    best_edge: float           # max(edge_yes, edge_no)
    token_id: str              # Token ID for the best side
    buy_price: float           # Price we'd pay for best side
    slug: str


POLYMARKET_FEE = 0.02  # ~2% fee on binary markets


def score_bracket(
    question: str,
    threshold: float,
    model_prob_yes: float,
    poly_yes_price: float,
    poly_no_price: float,
    yes_token_id: str,
    no_token_id: str,
    slug: str = "",
) -> BracketScore:
    """Score a single bracket for trading edge."""
    edge_yes = model_prob_yes - poly_yes_price - POLYMARKET_FEE
    edge_no = (1.0 - model_prob_yes) - poly_no_price - POLYMARKET_FEE

    if edge_yes >= edge_no:
        best_side = "yes"
        best_edge = edge_yes
        token_id = yes_token_id
        buy_price = poly_yes_price
    else:
        best_side = "no"
        best_edge = edge_no
        token_id = no_token_id
        buy_price = poly_no_price

    return BracketScore(
        question=question,
        threshold=threshold,
        model_prob_yes=model_prob_yes,
        poly_yes_price=poly_yes_price,
        poly_no_price=poly_no_price,
        edge_yes=edge_yes,
        edge_no=edge_no,
        best_side=best_side,
        best_edge=best_edge,
        token_id=token_id,
        buy_price=buy_price,
        slug=slug,
    )


# ── Standalone test ───────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 70)
    print("Testing probability models")
    print("=" * 70)

    # Test crypto volatility
    print("\n📊 Crypto Volatility:")
    for coin in ["BTC", "ETH"]:
        vol = estimate_volatility(coin)
        print(f"  {coin}: {vol:.1%} annualized")

    # Test crypto bracket probabilities
    btc_price = 71400.0
    btc_vol = estimate_volatility("BTC")
    hours = 16.0

    print(f"\n₿ BTC Bracket Probabilities (price=${btc_price:,.0f}, vol={btc_vol:.1%}, {hours:.0f}h):")
    for threshold in [66000, 68000, 70000, 72000, 74000, 76000]:
        prob = crypto_bracket_prob(btc_price, threshold, hours, btc_vol)
        print(f"  P(BTC > ${threshold:,}) = {prob:.1%}")

    # Test weather bracket probabilities
    print(f"\n🌡️ Weather Bracket Probabilities (Dallas forecast: 82°F):")
    forecast = 82.0
    for bracket in [
        ("≤75°F", 75, None, "at_or_below"),
        ("76-77°F", 76, 77, "range"),
        ("78-79°F", 78, 79, "range"),
        ("80-81°F", 80, 81, "range"),
        ("82-83°F", 82, 83, "range"),
        ("84-85°F", 84, 85, "range"),
        ("≥90°F", 90, None, "at_or_above"),
    ]:
        label, low, high, btype = bracket
        prob = weather_bracket_prob(forecast, low, high, btype, 18.0, "noaa", "°F")
        print(f"  P({label:10s}) = {prob:.1%}")

    # Compare weather model vs Polymarket for Dallas March 14
    print(f"\n📈 Dallas March 14 — Model vs Polymarket:")
    print(f"  {'Bracket':12s} {'Model':>8s} {'Poly':>8s} {'Edge':>8s}")
    print(f"  {'-'*12} {'-'*8} {'-'*8} {'-'*8}")

    # Polymarket prices from our earlier scan
    poly_prices = {
        "≤75°F": 0.0125,
        "76-77°F": 0.085,
        "78-79°F": 0.245,
        "80-81°F": 0.400,
        "82-83°F": 0.175,
        "84-85°F": 0.090,
        "86-87°F": 0.0105,
        "88-89°F": 0.0035,
        "≥90°F": 0.0035,
    }
    brackets_data = [
        ("≤75°F", 75, None, "at_or_below"),
        ("76-77°F", 76, 77, "range"),
        ("78-79°F", 78, 79, "range"),
        ("80-81°F", 80, 81, "range"),
        ("82-83°F", 82, 83, "range"),
        ("84-85°F", 84, 85, "range"),
        ("86-87°F", 86, 87, "range"),
        ("88-89°F", 88, 89, "range"),
        ("≥90°F", 90, None, "at_or_above"),
    ]
    for label, low, high, btype in brackets_data:
        model_p = weather_bracket_prob(forecast, low, high, btype, 18.0, "noaa", "°F")
        poly_p = poly_prices.get(label, 0)
        edge = model_p - poly_p - POLYMARKET_FEE
        marker = " ← EDGE!" if edge > 0.05 else ""
        print(f"  {label:12s} {model_p:7.1%} {poly_p:7.1%} {edge:+7.1%}{marker}")
