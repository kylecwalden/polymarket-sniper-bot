"""
Weather forecast feed for Polymarket temperature bracket trading.

US cities: NOAA Weather API (api.weather.gov) — free, no key needed
International cities: Open-Meteo API (api.open-meteo.com) — free, no key needed

Both provide next-day high temperature forecasts with ~2°F / ~1°C accuracy.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import requests

NOAA_BASE = "https://api.weather.gov"
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "PolymarketWeatherBot/1.0 (contact: bot@example.com)"

# Cache forecasts for 30 minutes (they don't change often)
CACHE_TTL = 1800
# Cache observations for 10 minutes (more real-time)
OBS_CACHE_TTL = 600


@dataclass
class CityForecast:
    city: str
    date: str               # "2026-03-14"
    high_temp: float         # Forecast high temperature
    low_temp: float | None   # Forecast low temperature (if available)
    unit: str                # "°F" or "°C"
    source: str              # "noaa", "open-meteo", "ensemble", or "observation"
    fetched_at: float        # time.time() when fetched
    confidence: float = 1.0  # 0-1, lower = less certain (wider std)
    is_observation: bool = False  # True if this is actual observed data, not a forecast
    forecast_std: float | None = None  # Override std if multi-model provides it

    @property
    def high_temp_f(self) -> float:
        """High temp in Fahrenheit."""
        if self.unit == "°F":
            return self.high_temp
        return self.high_temp * 9 / 5 + 32

    @property
    def high_temp_c(self) -> float:
        """High temp in Celsius."""
        if self.unit == "°C":
            return self.high_temp
        return (self.high_temp - 32) * 5 / 9


# ── City Coordinates ──────────────────────────────────────────────────
# lat, lon, country (US = use NOAA, else = Open-Meteo)

CITY_COORDS = {
    # (lat, lon, country, utc_offset_hours)
    # US cities (NOAA) — offsets are standard time (DST adds 1)
    "Dallas": (32.7767, -96.7970, "US", -6),
    "Atlanta": (33.7490, -84.3880, "US", -5),
    "NYC": (40.7128, -74.0060, "US", -5),
    "New York City": (40.7128, -74.0060, "US", -5),
    "Chicago": (41.8781, -87.6298, "US", -6),
    "Miami": (25.7617, -80.1918, "US", -5),
    "Seattle": (47.6062, -122.3321, "US", -8),

    # International cities (Open-Meteo)
    "Paris": (48.8566, 2.3522, "INT", 1),
    "London": (51.5074, -0.1278, "INT", 0),
    "Tokyo": (35.6762, 139.6503, "INT", 9),
    "Seoul": (37.5665, 126.9780, "INT", 9),
    "Toronto": (43.6532, -79.3832, "INT", -5),
    "Buenos Aires": (-34.6037, -58.3816, "INT", -3),
    "Sao Paulo": (-23.5505, -46.6333, "INT", -3),
    "Tel Aviv": (32.0853, 34.7818, "INT", 2),
    "Ankara": (39.9334, 32.8597, "INT", 3),
    "Munich": (48.1351, 11.5820, "INT", 1),
    "Wellington": (-41.2865, 174.7762, "INT", 12),
    "Shanghai": (31.2304, 121.4737, "INT", 8),
    "Lucknow": (26.8467, 80.9462, "INT", 5),
    "Singapore": (1.3521, 103.8198, "INT", 8),
}

# Forecast cache: key = "city|date" → CityForecast
_forecast_cache: dict[str, CityForecast] = {}

# Observation cache: key = "city" → (temp_c, timestamp)
_obs_cache: dict[str, tuple[float, float]] = {}

# NOAA grid cache: key = "lat,lon" → forecast_url
_noaa_grid_cache: dict[str, str] = {}


# ── Timezone Guard ────────────────────────────────────────────────────

def _get_city_coords(city: str) -> tuple | None:
    """Case-insensitive city lookup."""
    if city in CITY_COORDS:
        return CITY_COORDS[city]
    for name, c in CITY_COORDS.items():
        if name.lower() == city.lower():
            return c
    return None


def get_city_local_hour(city: str) -> float | None:
    """Get the current local hour (0-24) for a city. Returns None if unknown."""
    coords = _get_city_coords(city)
    if not coords:
        return None
    utc_offset = coords[3]
    # Rough DST: add 1 hour March-October for non-tropical cities
    now = datetime.now(timezone.utc)
    month = now.month
    lat = coords[0]
    if 3 <= month <= 10 and abs(lat) > 23.5:
        # Northern hemisphere spring/summer DST (or southern autumn/winter — skip DST)
        if lat > 0:
            utc_offset += 1
    elif month <= 2 or month >= 11:
        # Southern hemisphere summer DST
        if lat < -23.5 and city not in ("Buenos Aires", "Sao Paulo"):  # Argentina doesn't use DST
            utc_offset += 1

    local_hour = (now.hour + now.minute / 60 + utc_offset) % 24
    return local_hour


def is_observation_complete(city: str, resolution_date: str) -> bool:
    """Check if a city's daily high temp is likely already known.

    Returns True if the city's local time is past 4 PM on the resolution date,
    meaning the daily high has almost certainly already been recorded.
    This prevents betting against already-known outcomes.
    """
    coords = _get_city_coords(city)
    if not coords:
        return False  # Unknown city — don't block

    utc_offset = coords[3]
    now = datetime.now(timezone.utc)

    # Parse resolution date
    try:
        res_date = datetime.strptime(resolution_date, "%Y-%m-%d").date()
    except ValueError:
        return False

    # Check if resolution date is today in the city's timezone
    local_now = now + timedelta(hours=utc_offset)
    local_date = local_now.date()

    if local_date > res_date:
        # Resolution date has passed in local time — definitely complete
        return True

    if local_date == res_date:
        # Same day — check if past 4 PM local (daily high typically recorded by then)
        local_hour = get_city_local_hour(city)
        if local_hour is not None and local_hour >= 16.0:
            return True

    return False


# ── Real-Time Observations ────────────────────────────────────────────

def get_current_observation(city: str) -> float | None:
    """Fetch current/recent temperature observation for a city.

    Uses Open-Meteo's current weather API for real-time data.
    Returns temperature in °C, or None if unavailable.
    """
    # Check cache
    cached = _obs_cache.get(city)
    if cached and (time.time() - cached[1]) < OBS_CACHE_TTL:
        return cached[0]

    coords = _get_city_coords(city)
    if not coords:
        return None

    lat, lon = coords[0], coords[1]

    try:
        resp = requests.get(
            OPEN_METEO_BASE,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m",
                "timezone": "auto",
            },
            timeout=10,
        )
        resp.raise_for_status()
        current = resp.json().get("current", {})
        temp = current.get("temperature_2m")
        if temp is not None:
            _obs_cache[city] = (float(temp), time.time())
            return float(temp)
    except Exception as e:
        print(f"[weather] Observation fetch failed for {city}: {e}")

    return None


def get_daily_max_observation(city: str, target_date: str) -> float | None:
    """Fetch the observed daily maximum temperature for a city on a specific date.

    Uses Open-Meteo's historical/forecast daily data.
    Returns temperature in °C, or None if unavailable.
    Only reliable for past dates or today if local time is past peak hours.
    """
    coords = _get_city_coords(city)
    if not coords:
        return None

    lat, lon = coords[0], coords[1]

    try:
        resp = requests.get(
            OPEN_METEO_BASE,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max",
                "timezone": "auto",
                "start_date": target_date,
                "end_date": target_date,
            },
            timeout=10,
        )
        resp.raise_for_status()
        daily = resp.json().get("daily", {})
        highs = daily.get("temperature_2m_max", [])
        if highs and highs[0] is not None:
            return float(highs[0])
    except Exception as e:
        print(f"[weather] Daily max observation failed for {city}: {e}")

    return None


# ── Multi-Model Ensemble ──────────────────────────────────────────────

def _fetch_open_meteo_for_us_city(city: str, lat: float, lon: float) -> list[CityForecast]:
    """Fetch Open-Meteo forecast for a US city (second opinion alongside NOAA)."""
    try:
        resp = requests.get(
            OPEN_METEO_BASE,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min",
                "temperature_unit": "fahrenheit",
                "timezone": "auto",
                "forecast_days": 7,
            },
            timeout=10,
        )
        resp.raise_for_status()
        daily = resp.json().get("daily", {})

        dates = daily.get("time", [])
        highs = daily.get("temperature_2m_max", [])

        forecasts = []
        now = time.time()

        for i, d in enumerate(dates):
            if i < len(highs) and highs[i] is not None:
                forecasts.append(CityForecast(
                    city=city,
                    date=d,
                    high_temp=float(highs[i]),
                    low_temp=None,
                    unit="°F",
                    source="open-meteo",
                    fetched_at=now,
                ))

        return forecasts
    except Exception:
        return []


def _ensemble_forecast(noaa_forecast: CityForecast | None,
                       meteo_forecast: CityForecast | None) -> CityForecast | None:
    """Combine NOAA and Open-Meteo into an ensemble forecast.

    If both available: average the highs, set confidence based on agreement.
    If only one: use it with lower confidence.
    """
    if noaa_forecast and meteo_forecast:
        # Both models available — ensemble
        # Convert to same unit for comparison
        noaa_f = noaa_forecast.high_temp_f
        meteo_f = meteo_forecast.high_temp_f

        spread = abs(noaa_f - meteo_f)
        avg_f = (noaa_f + meteo_f) / 2.0

        # Confidence based on model agreement
        if spread <= 1.0:
            confidence = 1.2  # Models agree tightly — very confident
            forecast_std = 1.0  # Tighter std (°F)
        elif spread <= 3.0:
            confidence = 1.0  # Normal agreement
            forecast_std = 2.0
        elif spread <= 5.0:
            confidence = 0.7  # Some disagreement — less confident
            forecast_std = 3.5
        else:
            confidence = 0.5  # Big disagreement — very uncertain
            forecast_std = 5.0

        return CityForecast(
            city=noaa_forecast.city,
            date=noaa_forecast.date,
            high_temp=avg_f if noaa_forecast.unit == "°F" else (avg_f - 32) * 5 / 9,
            low_temp=None,
            unit=noaa_forecast.unit,
            source="ensemble",
            fetched_at=time.time(),
            confidence=confidence,
            forecast_std=forecast_std,
        )

    # Only one model — use it with slightly lower confidence
    solo = noaa_forecast or meteo_forecast
    if solo:
        solo.confidence = 0.8
        solo.source = f"{solo.source}-solo"
        return solo

    return None


# ── NOAA (US cities) ─────────────────────────────────────────────────

def _get_noaa_forecast_url(lat: float, lon: float) -> str | None:
    """Get NOAA forecast URL from coordinates (cached)."""
    key = f"{lat},{lon}"
    if key in _noaa_grid_cache:
        return _noaa_grid_cache[key]

    try:
        resp = requests.get(
            f"{NOAA_BASE}/points/{lat},{lon}",
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        url = resp.json().get("properties", {}).get("forecast", "")
        if url:
            _noaa_grid_cache[key] = url
        return url or None
    except Exception as e:
        print(f"[weather] NOAA grid lookup failed: {e}")
        return None


def _fetch_noaa_forecast(city: str, lat: float, lon: float) -> list[CityForecast]:
    """Fetch daily forecasts from NOAA for a US city."""
    forecast_url = _get_noaa_forecast_url(lat, lon)
    if not forecast_url:
        return []

    try:
        resp = requests.get(
            forecast_url,
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        periods = resp.json().get("properties", {}).get("periods", [])

        forecasts = []
        now = time.time()

        # NOAA periods alternate: "Saturday" (day), "Saturday Night" (night), etc.
        # We want daytime periods for high temps
        for p in periods:
            if not p.get("isDaytime", False):
                continue

            # Parse date from startTime: "2026-03-14T06:00:00-05:00"
            start_time = p.get("startTime", "")
            forecast_date = start_time[:10] if start_time else ""

            temp = p.get("temperature")
            unit_str = p.get("temperatureUnit", "F")
            unit = "°F" if unit_str == "F" else "°C"

            if temp is not None and forecast_date:
                forecasts.append(CityForecast(
                    city=city,
                    date=forecast_date,
                    high_temp=float(temp),
                    low_temp=None,
                    unit=unit,
                    source="noaa",
                    fetched_at=now,
                ))

        return forecasts

    except Exception as e:
        print(f"[weather] NOAA forecast failed for {city}: {e}")
        return []


# ── Open-Meteo (International cities) ────────────────────────────────

def _fetch_open_meteo_forecast(city: str, lat: float, lon: float) -> list[CityForecast]:
    """Fetch daily forecasts from Open-Meteo for an international city."""
    try:
        resp = requests.get(
            OPEN_METEO_BASE,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min",
                "timezone": "auto",
                "forecast_days": 7,
            },
            timeout=10,
        )
        resp.raise_for_status()
        daily = resp.json().get("daily", {})

        dates = daily.get("time", [])
        highs = daily.get("temperature_2m_max", [])
        lows = daily.get("temperature_2m_min", [])

        forecasts = []
        now = time.time()

        for i, d in enumerate(dates):
            if i < len(highs) and highs[i] is not None:
                forecasts.append(CityForecast(
                    city=city,
                    date=d,
                    high_temp=float(highs[i]),
                    low_temp=float(lows[i]) if i < len(lows) and lows[i] is not None else None,
                    unit="°C",
                    source="open-meteo",
                    fetched_at=now,
                ))

        return forecasts

    except Exception as e:
        print(f"[weather] Open-Meteo forecast failed for {city}: {e}")
        return []


# ── Public API ────────────────────────────────────────────────────────

def get_forecast(city: str, target_date: str) -> CityForecast | None:
    """Get forecast for a city on a specific date.

    Args:
        city: City name (must be in CITY_COORDS)
        target_date: Date string "YYYY-MM-DD"

    Returns:
        CityForecast or None if city unknown or API fails
    """
    # Check cache
    cache_key = f"{city}|{target_date}"
    cached = _forecast_cache.get(cache_key)
    if cached and (time.time() - cached.fetched_at) < CACHE_TTL:
        return cached

    coords = _get_city_coords(city)
    if not coords:
        print(f"[weather] Unknown city: {city}")
        return None

    lat, lon, country = coords[0], coords[1], coords[2]

    # ── Check if observation is already available (same-day, past peak) ──
    if is_observation_complete(city, target_date):
        obs_temp = get_daily_max_observation(city, target_date)
        if obs_temp is not None:
            # We have the actual observed high — use it with very tight uncertainty
            obs_forecast = CityForecast(
                city=city,
                date=target_date,
                high_temp=obs_temp,
                low_temp=None,
                unit="°C",
                source="observation",
                fetched_at=time.time(),
                confidence=2.0,      # Very high confidence
                is_observation=True,
                forecast_std=0.5,     # Tiny uncertainty (°C) — it's real data
            )
            _forecast_cache[cache_key] = obs_forecast
            return obs_forecast

    # ── Fetch forecast(s) ──
    if country == "US":
        # Multi-model ensemble: NOAA + Open-Meteo
        noaa_forecasts = _fetch_noaa_forecast(city, lat, lon)
        meteo_forecasts = _fetch_open_meteo_for_us_city(city, lat, lon)

        # Find matching date in each
        noaa_match = next((f for f in noaa_forecasts if f.date == target_date), None)
        meteo_match = next((f for f in meteo_forecasts if f.date == target_date), None)

        # Ensemble
        result = _ensemble_forecast(noaa_match, meteo_match)

        # Cache individual forecasts too
        for f in noaa_forecasts + meteo_forecasts:
            key = f"{f.city}|{f.date}|{f.source}"
            _forecast_cache[key] = f

        if result:
            _forecast_cache[cache_key] = result
        return result
    else:
        forecasts = _fetch_open_meteo_forecast(city, lat, lon)

        # Cache all dates and return the requested one
        result = None
        for f in forecasts:
            key = f"{f.city}|{f.date}"
            _forecast_cache[key] = f
            if f.date == target_date:
                result = f

        return result


def get_all_forecasts(target_date: str) -> dict[str, CityForecast]:
    """Get forecasts for all known cities on a target date."""
    results = {}
    for city in CITY_COORDS:
        forecast = get_forecast(city, target_date)
        if forecast:
            results[city] = forecast
    return results


# ── Standalone test ───────────────────────────────────────────────────

if __name__ == "__main__":
    from datetime import date, timedelta

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    print(f"Fetching forecasts for {tomorrow}...")
    print("=" * 60)

    forecasts = get_all_forecasts(tomorrow)

    # Group by source
    noaa = {k: v for k, v in forecasts.items() if v.source == "noaa"}
    meteo = {k: v for k, v in forecasts.items() if v.source == "open-meteo"}

    print(f"\n🇺🇸 NOAA (US cities):")
    for city, f in sorted(noaa.items()):
        print(f"  {city:15s}  High: {f.high_temp:.0f}{f.unit}")

    print(f"\n🌍 Open-Meteo (International):")
    for city, f in sorted(meteo.items()):
        f_temp = f"({f.high_temp_f:.0f}°F)" if f.unit == "°C" else ""
        print(f"  {city:15s}  High: {f.high_temp:.1f}{f.unit} {f_temp}")

    print(f"\nTotal: {len(forecasts)} cities")
