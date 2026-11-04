"""Weather snapshots for entries (Open-Meteo, no API key).

Barometric pressure change is a commonly reported migraine trigger, so each
entry can carry the weather observed for its moment: pressure plus 3-hour
tendency, temperature, humidity, and a plain-language condition.

Privacy: coordinates are used for a single API call and never stored; only
the resulting snapshot is kept on the entry. Everything here is fail-soft:
any network or parsing problem returns None and the entry saves without
weather. Nothing here interprets or advises; snapshots only record.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .models import WeatherSnapshot

__all__ = [
    "FORECAST_URL",
    "ARCHIVE_URL",
    "BACKDATED_HOURS",
    "valid_coords",
    "condition_label",
    "pressure_tendency",
    "fetch_current_weather",
    "fetch_historical_weather",
    "refresh_for_attack",
]

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# An attack that started longer ago than this gets historical weather for its
# own hour instead of the weather at log time.
BACKDATED_HOURS = 6

# hPa change over 3 hours that counts as rising/falling rather than steady.
TENDENCY_DELTA_HPA = 0.5

_WMO_LABELS = {
    0: "Clear",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Fog",
    51: "Drizzle",
    53: "Drizzle",
    55: "Drizzle",
    56: "Drizzle",
    57: "Drizzle",
    61: "Rain",
    63: "Rain",
    65: "Rain",
    66: "Rain",
    67: "Rain",
    71: "Snow",
    73: "Snow",
    75: "Snow",
    77: "Snow",
    80: "Showers",
    81: "Showers",
    82: "Showers",
    85: "Snow",
    86: "Snow",
    95: "Thunderstorm",
    96: "Thunderstorm",
    99: "Thunderstorm",
}


def valid_coords(latitude: Any, longitude: Any) -> bool:
    """True when both coordinates are plausible numbers (not NaN/blank)."""
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return False
    if lat != lat or lon != lon:  # NaN
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def condition_label(code: Any) -> str:
    """Plain-language label for a WMO weather code. Never raises."""
    try:
        return _WMO_LABELS.get(int(code), "Unknown")
    except (TypeError, ValueError):
        return "Unknown"


def pressure_tendency(
    hourly_hpa: Any, index: int
) -> Optional[str]:
    """Pressure trend at `index` vs 3 hours earlier: rising/falling/steady.

    Returns None when the series is too short or either value is missing.
    """
    try:
        if index < 3 or index >= len(hourly_hpa):
            return None
        now = hourly_hpa[index]
        then = hourly_hpa[index - 3]
        if now is None or then is None:
            return None
        delta = float(now) - float(then)
    except (TypeError, ValueError, IndexError):
        return None
    if delta >= TENDENCY_DELTA_HPA:
        return "rising"
    if delta <= -TENDENCY_DELTA_HPA:
        return "falling"
    return "steady"


def _get(url: str, params: dict[str, Any], timeout: float):  # noqa: ANN202
    import httpx  # local import: only needed for real network calls

    with httpx.Client(timeout=timeout) as client:
        return client.get(url, params=params)


def _num(value: Any, low: float, high: float) -> Optional[float]:
    """A plausible measurement, else None (fail-soft per field)."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num or not (low <= num <= high):
        return None
    return num


def _aware(naive_local: datetime, utc_offset_seconds: Any) -> datetime:
    try:
        offset = int(utc_offset_seconds)
    except (TypeError, ValueError):
        offset = 0
    return naive_local.replace(tzinfo=timezone(timedelta(seconds=offset)))


def _snapshot(
    *,
    observed_at: Optional[datetime],
    temperature_c: Any,
    humidity_pct: Any,
    pressure_hpa: Any,
    tendency: Optional[str],
    code: Any,
    basis: str,
) -> Optional[WeatherSnapshot]:
    temp = _num(temperature_c, -90.0, 60.0)
    humidity = _num(humidity_pct, 0.0, 100.0)
    pressure = _num(pressure_hpa, 800.0, 1100.0)
    if temp is None and humidity is None and pressure is None:
        return None
    return WeatherSnapshot(
        observed_at=observed_at,
        temperature_c=temp,
        humidity_pct=humidity,
        pressure_hpa=pressure,
        pressure_tendency=tendency,
        condition=condition_label(code),
        basis=basis,  # type: ignore[arg-type]
        source="open-meteo",
    )


def log_time_snapshot(
    latitude: Any,
    longitude: Any,
    *,
    enabled: bool = True,
    timeout: float = 6.0,
) -> Optional[WeatherSnapshot]:
    """Weather snapshot for a save happening now. None when disabled,
    coordinates are missing, or the fetch fails. Never raises."""
    if not enabled or latitude is None or longitude is None:
        return None
    return fetch_current_weather(latitude, longitude, timeout=timeout)


def fetch_current_weather(
    latitude: float,
    longitude: float,
    *,
    timeout: float = 6.0,
    get: Optional[Callable[..., Any]] = None,
) -> Optional[WeatherSnapshot]:
    """Snapshot of the weather right now. None on any failure."""
    if not valid_coords(latitude, longitude):
        return None
    request = get or _get
    try:
        resp = request(
            FORECAST_URL,
            {
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,relative_humidity_2m,pressure_msl,weather_code",
                "hourly": "pressure_msl",
                "forecast_days": 1,
                "timezone": "auto",
            },
            timeout,
        )
        if resp.status_code != 200:
            return None
        payload = resp.json()
        current = payload.get("current") or {}
        observed = datetime.fromisoformat(str(current.get("time")))
        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        pressures = hourly.get("pressure_msl") or []
        hour = observed.strftime("%Y-%m-%dT%H")
        index = next(
            (i for i, t in enumerate(times) if str(t).startswith(hour)),
            None,
        )
        tendency = pressure_tendency(pressures, index) if index is not None else None
        return _snapshot(
            observed_at=_aware(observed, payload.get("utc_offset_seconds")),
            temperature_c=current.get("temperature_2m"),
            humidity_pct=current.get("relative_humidity_2m"),
            pressure_hpa=current.get("pressure_msl"),
            tendency=tendency,
            code=current.get("weather_code"),
            basis="log_time",
        )
    except Exception:  # noqa: BLE001 - weather must never break a save
        return None


def fetch_historical_weather(
    latitude: float,
    longitude: float,
    when: datetime,
    *,
    timeout: float = 6.0,
    get: Optional[Callable[..., Any]] = None,
) -> Optional[WeatherSnapshot]:
    """Snapshot for a past moment `when` (attack hour). None on any failure.

    Naive `when` is read as location wall-clock (that is how resolved attack
    times arrive); aware `when` is shifted by the location's UTC offset. The
    archive lags several days, so recent moments use the forecast API's past
    days instead; both return the same hourly shape.
    """
    if not valid_coords(latitude, longitude):
        return None
    request = get or _get
    try:
        day = when.date()
        age_days = (datetime.now(timezone.utc).date() - day).days
        hourly_vars = "temperature_2m,relative_humidity_2m,pressure_msl,weather_code"
        if age_days < 5:
            url, params = FORECAST_URL, {
                "latitude": latitude,
                "longitude": longitude,
                "hourly": hourly_vars,
                "past_days": 8,
                "forecast_days": 1,
                "timezone": "auto",
            }
        else:
            url, params = ARCHIVE_URL, {
                "latitude": latitude,
                "longitude": longitude,
                "start_date": (day - timedelta(days=1)).isoformat(),
                "end_date": day.isoformat(),
                "hourly": hourly_vars,
                "timezone": "auto",
            }
        resp = request(url, params, timeout)
        if resp.status_code != 200:
            return None
        payload = resp.json()
        try:
            offset = int(payload.get("utc_offset_seconds") or 0)
        except (TypeError, ValueError):
            offset = 0
        if when.tzinfo is None:
            target = when
        else:
            target = (when + timedelta(seconds=offset)).replace(tzinfo=None)
        hourly = payload.get("hourly") or {}
        times = [str(t) for t in hourly.get("time") or []]
        want = target.strftime("%Y-%m-%dT%H:00")
        if want not in times:
            return None
        index = times.index(want)
        temps = hourly.get("temperature_2m") or []
        humids = hourly.get("relative_humidity_2m") or []
        pressures = hourly.get("pressure_msl") or []
        codes = hourly.get("weather_code") or []
        observed = datetime.fromisoformat(times[index])
        return _snapshot(
            observed_at=_aware(observed, offset),
            temperature_c=temps[index] if index < len(temps) else None,
            humidity_pct=humids[index] if index < len(humids) else None,
            pressure_hpa=pressures[index] if index < len(pressures) else None,
            tendency=pressure_tendency(pressures, index),
            code=codes[index] if index < len(codes) else None,
            basis="attack_time",
        )
    except Exception:  # noqa: BLE001 - weather must never break a save
        return None


def _age_hours(start: datetime) -> float:
    aware = start if start.tzinfo is not None else start.astimezone()
    return (datetime.now(timezone.utc) - aware).total_seconds() / 3600.0


def refresh_for_attack(
    existing: Optional[WeatherSnapshot],
    latitude: float,
    longitude: float,
    attack_start: Optional[datetime],
    *,
    timeout: float = 6.0,
    get: Optional[Callable[..., Any]] = None,
) -> Optional[WeatherSnapshot]:
    """Best snapshot once the attack moment is known.

    A backdated attack gets historical weather for its own hour; otherwise the
    log-time snapshot is kept (or fetched when missing). Failures keep whatever
    was there before, so this never removes weather.
    """
    if not valid_coords(latitude, longitude):
        return existing
    if attack_start is not None:
        try:
            backdated = _age_hours(attack_start) > BACKDATED_HOURS
        except Exception:  # noqa: BLE001 - fall through to existing/current
            backdated = False
        if backdated:
            historical = fetch_historical_weather(
                latitude, longitude, attack_start, timeout=timeout, get=get
            )
            return historical if historical is not None else existing
    if existing is not None:
        return existing
    return fetch_current_weather(latitude, longitude, timeout=timeout, get=get)
