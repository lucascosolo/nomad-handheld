"""Unit conversion for the offline tier, and the choices that make it one answer.

This has to work with no network and no model, so every ambiguity a human
would resolve from context has to be resolved once, here, and documented
rather than guessed per call. Two of those choices matter enough to spell
out:

- **Data units are binary (1024-based), not decimal.** "kb"/"mb"/"gb"/"tb"
  mean KiB/MiB/GiB/TiB — this is a device-memory and file-size tool, and that
  is the convention every OS file browser and `du` actually uses. `kib`/
  `mib`/`gib` are accepted as explicit synonyms of the same factor, not a
  second, decimal system; there is exactly one data scale in this module.
- **Token collisions are resolved once, not contextually.** "in" is always
  inch (never the preposition), "t" is always tonne (never ton, never a time
  unit), "b" is always byte (never bit). A tool with no surrounding sentence
  cannot disambiguate from context, so it does not try.

Every non-temperature dimension is a ratio: each unit stores how many base
units it is worth, and conversion is `value * from_factor / to_factor`.
Temperature is the one affine dimension (a 0-point that isn't the same
across scales), so it gets its own conversion functions instead of a factor
table — folding it into the ratio model would silently produce wrong
answers for anything but a *difference* in temperature.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import get_close_matches

from pydantic import BaseModel

from nomad.utilities.errors import UtilityError


class Conversion(BaseModel):
    """The result of one unit conversion, both ends included.

    Carrying the source alongside the result means a caller (a tool result,
    a chat reply) never has to re-derive "converted from what" — it's the
    same object that answered the question.
    """

    value: float
    unit: str
    symbol: str
    dimension: str
    source_value: float
    source_unit: str
    source_symbol: str


@dataclass(frozen=True)
class _Unit:
    """One canonical unit within a ratio dimension: name, symbol, and its
    worth in the dimension's base unit."""

    name: str
    symbol: str
    factor: float


# Each dimension's base unit is the one with factor == 1.0: metre, kilogram,
# litre, metre/second, byte, second. The choice of base is arbitrary — only
# ratios between units in the same dimension are ever computed.
_LENGTH = [
    _Unit("millimetre", "mm", 0.001),
    _Unit("centimetre", "cm", 0.01),
    _Unit("metre", "m", 1.0),
    _Unit("kilometre", "km", 1000.0),
    _Unit("inch", "in", 0.0254),
    _Unit("foot", "ft", 0.3048),
    _Unit("yard", "yd", 0.9144),
    _Unit("mile", "mi", 1609.344),
    _Unit("nautical mile", "nmi", 1852.0),
]

_MASS = [
    _Unit("milligram", "mg", 0.001),
    _Unit("gram", "g", 1.0),
    _Unit("kilogram", "kg", 1000.0),
    _Unit("tonne", "t", 1_000_000.0),
    _Unit("ounce", "oz", 28.349523125),
    _Unit("pound", "lb", 453.59237),
    _Unit("stone", "st", 6350.29318),
]

_VOLUME = [
    _Unit("millilitre", "ml", 0.001),
    _Unit("litre", "l", 1.0),
    _Unit("teaspoon", "tsp", 0.00492892159375),
    _Unit("tablespoon", "tbsp", 0.01478676478125),
    _Unit("cup", "cup", 0.2365882365),
    _Unit("fluid ounce", "floz", 0.0295735295625),
    _Unit("pint", "pt", 0.473176473),
    _Unit("quart", "qt", 0.946352946),
    _Unit("gallon", "gal", 3.785411784),
]

_SPEED = [
    _Unit("metre per second", "m/s", 1.0),
    _Unit("kilometre per hour", "km/h", 1000.0 / 3600.0),
    _Unit("mile per hour", "mph", 1609.344 / 3600.0),
    _Unit("knot", "kn", 1852.0 / 3600.0),
]

# Binary throughout — see module docstring. kib/mib/gib/tib are the same
# factor as kb/mb/gb/tb, not a second scale.
_DATA = [
    _Unit("byte", "b", 1.0),
    _Unit("kilobyte", "kb", 1024.0),
    _Unit("megabyte", "mb", 1024.0**2),
    _Unit("gigabyte", "gb", 1024.0**3),
    _Unit("terabyte", "tb", 1024.0**4),
]

_DURATION = [
    _Unit("millisecond", "ms", 0.001),
    _Unit("second", "s", 1.0),
    _Unit("minute", "min", 60.0),
    _Unit("hour", "h", 3600.0),
    _Unit("day", "d", 86400.0),
    _Unit("week", "wk", 604800.0),
]

_RATIO_DIMENSIONS: dict[str, list[_Unit]] = {
    "length": _LENGTH,
    "mass": _MASS,
    "volume": _VOLUME,
    "speed": _SPEED,
    "data": _DATA,
    "duration": _DURATION,
}

# Alias -> (dimension, canonical unit name). Built once at import time so
# lookup is a dict hit, not a scan, on every call.
_ALIASES: dict[str, tuple[str, str]] = {}


def _alias(dimension: str, canonical: str, *tokens: str) -> None:
    for token in tokens:
        _ALIASES[token] = (dimension, canonical)


_alias("length", "millimetre", "mm", "millimetre", "millimeter")
_alias("length", "centimetre", "cm", "centimetre", "centimeter")
_alias("length", "metre", "m", "metre", "meter")
_alias("length", "kilometre", "km", "kilometre", "kilometer")
_alias("length", "inch", "in", "inch", "inches")
_alias("length", "foot", "ft", "foot", "feet")
_alias("length", "yard", "yd", "yard", "yards")
_alias("length", "mile", "mi", "mile", "miles")
_alias("length", "nautical mile", "nmi", "nautical mile", "nauticalmile")

_alias("mass", "milligram", "mg", "milligram", "milligrams")
_alias("mass", "gram", "g", "gram", "grams")
_alias("mass", "kilogram", "kg", "kilogram", "kilograms")
_alias("mass", "tonne", "t", "tonne", "tonnes", "metric ton", "metricton")
_alias("mass", "ounce", "oz", "ounce", "ounces")
_alias("mass", "pound", "lb", "lbs", "pound", "pounds")
_alias("mass", "stone", "st", "stone", "stones")

_alias("volume", "millilitre", "ml", "millilitre", "milliliter")
_alias("volume", "litre", "l", "litre", "liter", "litres", "liters")
_alias("volume", "teaspoon", "tsp", "teaspoon", "teaspoons")
_alias("volume", "tablespoon", "tbsp", "tablespoon", "tablespoons")
_alias("volume", "cup", "cup", "cups")
_alias("volume", "fluid ounce", "floz", "fluid ounce", "fluidounce", "fl oz")
_alias("volume", "pint", "pt", "pint", "pints")
_alias("volume", "quart", "qt", "quart", "quarts")
_alias("volume", "gallon", "gal", "gallon", "gallons")

_alias("speed", "metre per second", "m/s", "mps", "metre per second", "meter per second")
_alias("speed", "kilometre per hour", "km/h", "kph", "kilometre per hour", "kilometer per hour")
_alias("speed", "mile per hour", "mph", "mile per hour")
_alias("speed", "knot", "kn", "knot", "knots")

_alias("data", "byte", "b", "byte", "bytes")
_alias("data", "kilobyte", "kb", "kib", "kilobyte", "kibibyte")
_alias("data", "megabyte", "mb", "mib", "megabyte", "mebibyte")
_alias("data", "gigabyte", "gb", "gib", "gigabyte", "gibibyte")
_alias("data", "terabyte", "tb", "tib", "terabyte", "tebibyte")

_alias("duration", "millisecond", "ms", "millisecond", "milliseconds")
_alias("duration", "second", "s", "sec", "second", "seconds")
_alias("duration", "minute", "min", "minute", "minutes")
_alias("duration", "hour", "h", "hr", "hour", "hours")
_alias("duration", "day", "d", "day", "days")
_alias("duration", "week", "wk", "week", "weeks")

# Temperature is affine, not ratio-based (see module docstring), so it lives
# outside _RATIO_DIMENSIONS and _ALIASES entirely — resolved by its own
# small alias map below.
_TEMP_ALIASES: dict[str, str] = {
    "c": "celsius",
    "celsius": "celsius",
    "°c": "celsius",
    "f": "fahrenheit",
    "fahrenheit": "fahrenheit",
    "°f": "fahrenheit",
    "k": "kelvin",
    "kelvin": "kelvin",
}
_TEMP_SYMBOLS = {"celsius": "°C", "fahrenheit": "°F", "kelvin": "K"}


def _lookup_token(unit: str) -> str:
    """Whitespace-collapsed, case-folded key used for alias lookup."""
    return " ".join(unit.split()).casefold()


def _suggest(unit: str) -> list[str]:
    all_tokens = list(_ALIASES) + list(_TEMP_ALIASES)
    close = get_close_matches(unit.casefold(), all_tokens, n=5, cutoff=0.5)
    substr = [t for t in all_tokens if unit.casefold() in t or t in unit.casefold()]
    seen: dict[str, None] = {}
    for token in [*close, *substr]:
        seen[token] = None
    return list(seen)[:5]


def _resolve(unit: str) -> tuple[str, str]:
    """Resolve a unit token to (dimension, canonical name), including
    "temperature" as a pseudo-dimension for celsius/fahrenheit/kelvin."""
    token = _lookup_token(unit)
    if token in _TEMP_ALIASES:
        return "temperature", _TEMP_ALIASES[token]
    if token in _ALIASES:
        return _ALIASES[token]
    suggestions = _suggest(unit)
    raise UtilityError(
        f"unknown unit {unit!r}",
        details={"unit": unit, "suggestions": suggestions},
    )


def _to_celsius(value: float, unit: str) -> float:
    if unit == "celsius":
        return value
    if unit == "fahrenheit":
        return (value - 32.0) * 5.0 / 9.0
    return value - 273.15  # kelvin


def _from_celsius(value: float, unit: str) -> float:
    if unit == "celsius":
        return value
    if unit == "fahrenheit":
        return value * 9.0 / 5.0 + 32.0
    return value + 273.15  # kelvin


def convert(value: float, from_unit: str, to_unit: str) -> Conversion:
    """Convert `value` from `from_unit` to `to_unit`.

    Raises `UtilityError` if either unit is unrecognised, or if the two
    units belong to different dimensions.
    """
    from_dim, from_name = _resolve(from_unit)
    to_dim, to_name = _resolve(to_unit)
    if from_dim != to_dim:
        raise UtilityError(
            f"cannot convert {from_dim} to {to_dim}",
            details={"from_dimension": from_dim, "to_dimension": to_dim},
        )

    if from_dim == "temperature":
        result = _from_celsius(_to_celsius(value, from_name), to_name)
        return Conversion(
            value=result,
            unit=to_name,
            symbol=_TEMP_SYMBOLS[to_name],
            dimension="temperature",
            source_value=value,
            source_unit=from_name,
            source_symbol=_TEMP_SYMBOLS[from_name],
        )

    units = {u.name: u for u in _RATIO_DIMENSIONS[from_dim]}
    from_u, to_u = units[from_name], units[to_name]
    result = value * from_u.factor / to_u.factor
    return Conversion(
        value=result,
        unit=to_u.name,
        symbol=to_u.symbol,
        dimension=from_dim,
        source_value=value,
        source_unit=from_u.name,
        source_symbol=from_u.symbol,
    )


def known_units() -> dict[str, list[str]]:
    """Every canonical unit name, grouped by dimension, for help text."""
    result: dict[str, list[str]] = {
        dim: sorted({u.name for u in units}) for dim, units in _RATIO_DIMENSIONS.items()
    }
    result["temperature"] = sorted(set(_TEMP_ALIASES.values()))
    return result
