"""Tests for nomad.utilities.units and nomad.utilities.worldclock.

No sleeps, no real-clock reads — every datetime is built explicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nomad.utilities.errors import UtilityError
from nomad.utilities.units import convert, known_units
from nomad.utilities.worldclock import (
    convert_time,
    resolve_zone,
    time_in,
    zone_difference_minutes,
)

# ---------------------------------------------------------------------------
# units.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "from_unit", "to_unit"),
    [
        (5.0, "km", "mi"),
        (1.0, "kg", "lb"),
        (1.0, "l", "gal"),
        (100.0, "km/h", "mph"),
        (1.0, "gb", "mb"),
        (1.0, "h", "min"),
    ],
)
def test_round_trip(value: float, from_unit: str, to_unit: str) -> None:
    forward = convert(value, from_unit, to_unit)
    back = convert(forward.value, to_unit, from_unit)
    assert back.value == pytest.approx(value, rel=1e-9)


def test_length_units() -> None:
    units = [
        "mm", "cm", "m", "km", "in", "inch", "ft", "foot", "feet",
        "yd", "yard", "mi", "mile", "nmi",
    ]
    for unit in units:
        result = convert(1.0, unit, "m")
        assert result.dimension == "length"


def test_mass_units() -> None:
    for unit in ["mg", "g", "kg", "t", "tonne", "oz", "ounce", "lb", "lbs", "pound", "st", "stone"]:
        result = convert(1.0, unit, "kg")
        assert result.dimension == "mass"


def test_volume_units() -> None:
    units = [
        "ml", "l", "litre", "liter", "tsp", "tbsp", "cup", "floz",
        "fluid ounce", "pt", "qt", "gal", "gallon",
    ]
    for unit in units:
        result = convert(1.0, unit, "l")
        assert result.dimension == "volume"


def test_speed_units() -> None:
    for unit in ["m/s", "km/h", "kph", "mph", "kn", "knot"]:
        result = convert(1.0, unit, "m/s")
        assert result.dimension == "speed"


def test_data_units_binary() -> None:
    result = convert(1.0, "gb", "mb")
    assert result.value == pytest.approx(1024.0)
    result = convert(1.0, "mib", "kib")
    assert result.value == pytest.approx(1024.0)
    result = convert(1.0, "kib", "kb")
    assert result.value == pytest.approx(1.0)


def test_duration_units() -> None:
    units = [
        "ms", "s", "sec", "second", "min", "minute", "h", "hr", "hour", "d", "day", "wk", "week",
    ]
    for unit in units:
        result = convert(1.0, unit, "s")
        assert result.dimension == "duration"


def test_temperature_celsius_fahrenheit() -> None:
    assert convert(0.0, "c", "f").value == pytest.approx(32.0)
    assert convert(100.0, "c", "f").value == pytest.approx(212.0)
    assert convert(32.0, "f", "c").value == pytest.approx(0.0)
    assert convert(212.0, "f", "c").value == pytest.approx(100.0)
    assert convert(-40.0, "c", "f").value == pytest.approx(-40.0)
    assert convert(-40.0, "f", "c").value == pytest.approx(-40.0)


def test_temperature_celsius_kelvin() -> None:
    assert convert(0.0, "c", "k").value == pytest.approx(273.15)
    result = convert(273.15, "k", "c")
    assert result.value == pytest.approx(0.0)


def test_temperature_round_trip_all_pairs() -> None:
    for a, b in [("c", "f"), ("f", "c"), ("c", "k"), ("k", "c"), ("f", "k"), ("k", "f")]:
        forward = convert(20.0, a, b)
        back = convert(forward.value, b, a)
        assert back.value == pytest.approx(20.0, abs=1e-6)


def test_temperature_symbol_and_dimension() -> None:
    result = convert(0.0, "celsius", "fahrenheit")
    assert result.dimension == "temperature"
    assert result.symbol == "°F"
    assert result.source_symbol == "°C"
    assert result.source_unit == "celsius"
    assert result.unit == "fahrenheit"


def test_cross_dimension_raises() -> None:
    with pytest.raises(UtilityError) as excinfo:
        convert(1.0, "kg", "km")
    assert "mass" in str(excinfo.value)
    assert "length" in str(excinfo.value)


def test_unknown_unit_raises() -> None:
    with pytest.raises(UtilityError) as excinfo:
        convert(1.0, "banana", "kg")
    assert "banana" in str(excinfo.value)


def test_unknown_unit_suggestions() -> None:
    with pytest.raises(UtilityError) as excinfo:
        convert(1.0, "kilogrم", "kg")
    assert isinstance(excinfo.value.details.get("suggestions"), list)


def test_case_and_whitespace_insensitivity() -> None:
    a = convert(1.0, "  KM  ", "m")
    b = convert(1.0, "km", "m")
    assert a.value == pytest.approx(b.value)
    c = convert(1.0, "Fluid Ounce", "ml")
    d = convert(1.0, "floz", "ml")
    assert c.value == pytest.approx(d.value)


def test_ambiguous_tokens_pinned() -> None:
    # "in" is inch, not a preposition.
    assert convert(1.0, "in", "cm").value == pytest.approx(2.54)
    # "t" is tonne, not a time/other unit.
    assert convert(1.0, "t", "kg").value == pytest.approx(1000.0)
    # "b" is byte, not bit.
    assert convert(1.0, "b", "kb").value == pytest.approx(1.0 / 1024.0)


def test_known_units_shape() -> None:
    units = known_units()
    assert isinstance(units, dict)
    for dimension in ["length", "mass", "temperature", "volume", "speed", "data", "duration"]:
        assert dimension in units
        assert units[dimension] == sorted(units[dimension])
        assert len(units[dimension]) > 0


# ---------------------------------------------------------------------------
# worldclock.py
# ---------------------------------------------------------------------------


def test_resolve_zone_exact_iana() -> None:
    assert resolve_zone("Europe/London") == "Europe/London"
    assert resolve_zone("europe/london") == "Europe/London"


def test_resolve_zone_alias() -> None:
    assert resolve_zone("nyc") == "America/New_York"
    assert resolve_zone("New York") == "America/New_York"
    # "GMT" is itself a valid IANA zone name, so exact match wins over the
    # alias table here — both read as zero offset either way.
    assert resolve_zone("gmt") in ("UTC", "GMT")
    assert resolve_zone("sao paulo") == "America/Sao_Paulo"
    assert resolve_zone("sao-paulo") == "America/Sao_Paulo"
    assert resolve_zone("sao_paulo") == "America/Sao_Paulo"


def test_resolve_zone_unknown_raises() -> None:
    with pytest.raises(UtilityError) as excinfo:
        resolve_zone("Mars/Olympus_Mons")
    assert "Mars/Olympus_Mons" in str(excinfo.value)


def test_time_in_naive_is_utc() -> None:
    at = datetime(2026, 1, 15, 12, 0, 0)
    result = time_in("UTC", at)
    assert result.utc_offset_minutes == 0
    assert result.local == "2026-01-15 12:00"


def test_time_in_london_summer_is_dst() -> None:
    at = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
    result = time_in("Europe/London", at)
    assert result.is_dst is True
    assert result.abbreviation == "BST"
    assert result.utc_offset_minutes == 60


def test_time_in_london_winter_not_dst() -> None:
    at = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    result = time_in("Europe/London", at)
    assert result.is_dst is False
    assert result.abbreviation == "GMT"
    assert result.utc_offset_minutes == 0


def test_time_in_day_of_week() -> None:
    at = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)  # a Saturday
    result = time_in("UTC", at)
    assert result.day_of_week == "Saturday"


def test_convert_time_us_europe_pair() -> None:
    at = datetime(2026, 7, 15, 12, 0, 0)  # naive, interpreted as from_zone
    source, target = convert_time(at, "America/New_York", "Europe/London")
    assert source.zone == "America/New_York"
    assert target.zone == "Europe/London"
    # both in DST in July: NY is UTC-4, London is UTC+1 -> 5 hour gap
    assert target.utc_offset_minutes - source.utc_offset_minutes == 300


def test_convert_time_dst_states_differ() -> None:
    # Early April: US already in DST (since mid-March), UK not yet (starts
    # late March... so both may be in DST). Pick a date where they differ:
    # UK DST starts last Sunday of March, US DST starts second Sunday of
    # March -> both typically in DST by April. Use early November instead,
    # where UK has left DST (last Sunday Oct) but US hasn't yet (first
    # Sunday Nov straddles) -> use a date solidly after UK end, before US end.
    at = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
    ny = time_in("America/New_York", at)
    london = time_in("Europe/London", at)
    assert ny.is_dst is True
    assert london.is_dst is False


def test_zone_difference_minutes_sign() -> None:
    at = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
    # London ahead of New York in summer -> positive.
    diff = zone_difference_minutes("America/New_York", "Europe/London", at)
    assert diff == 300
    # Reversed order flips sign.
    reverse = zone_difference_minutes("Europe/London", "America/New_York", at)
    assert reverse == -300


def test_zone_difference_minutes_same_zone_is_zero() -> None:
    at = datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC)
    assert zone_difference_minutes("UTC", "UTC", at) == 0
