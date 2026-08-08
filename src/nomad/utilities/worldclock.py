"""Timezone lookups for the offline tier, using the tz database already on
the device rather than any notion this module invents.

`zoneinfo` is the source of truth for every offset, abbreviation and DST
transition — this module's own job is narrower: turning what a person
actually types ("nyc", "gmt", "sao paulo") into the IANA key `zoneinfo`
needs, and turning a resolved zone plus an instant into the fields a caller
wants to display. It never computes an offset itself.

A naive `datetime` is treated as UTC, not as local device time — the device
has no reliable notion of "local" (it may not even know its own timezone),
so the only unambiguous reading of a bare timestamp is the one with no
timezone assumption baked in at all. Callers that mean something else
attach a `tzinfo` before calling.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, available_timezones

from pydantic import BaseModel

from nomad.utilities.errors import UtilityError

_WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

# Curated aliases for the way people actually type a place name. Keys are
# normalized (lowercased, spaces/hyphens/underscores collapsed to a single
# space) before lookup — see `_normalize_zone_token`.
_ZONE_ALIASES: dict[str, str] = {
    "utc": "UTC",
    "gmt": "UTC",
    "london": "Europe/London",
    "new york": "America/New_York",
    "nyc": "America/New_York",
    "los angeles": "America/Los_Angeles",
    "la": "America/Los_Angeles",
    "pacific": "America/Los_Angeles",
    "chicago": "America/Chicago",
    "denver": "America/Denver",
    "tokyo": "Asia/Tokyo",
    "sydney": "Australia/Sydney",
    "paris": "Europe/Paris",
    "berlin": "Europe/Berlin",
    "madrid": "Europe/Madrid",
    "rome": "Europe/Rome",
    "amsterdam": "Europe/Amsterdam",
    "dublin": "Europe/Dublin",
    "lisbon": "Europe/Lisbon",
    "sao paulo": "America/Sao_Paulo",
    "mexico city": "America/Mexico_City",
    "toronto": "America/Toronto",
    "vancouver": "America/Vancouver",
    "singapore": "Asia/Singapore",
    "hong kong": "Asia/Hong_Kong",
    "shanghai": "Asia/Shanghai",
    "beijing": "Asia/Shanghai",
    "seoul": "Asia/Seoul",
    "mumbai": "Asia/Kolkata",
    "delhi": "Asia/Kolkata",
    "india": "Asia/Kolkata",
    "dubai": "Asia/Dubai",
    "moscow": "Europe/Moscow",
    "istanbul": "Europe/Istanbul",
    "johannesburg": "Africa/Johannesburg",
    "cairo": "Africa/Cairo",
    "lagos": "Africa/Lagos",
    "nairobi": "Africa/Nairobi",
    "auckland": "Pacific/Auckland",
}

_IANA_BY_LOWER: dict[str, str] = {name.casefold(): name for name in available_timezones()}


class ZoneTime(BaseModel):
    """A resolved zone's reading of one instant."""

    zone: str
    iso: str
    local: str
    utc_offset_minutes: int
    abbreviation: str
    is_dst: bool
    day_of_week: str


def _normalize_zone_token(name: str) -> str:
    return " ".join(name.replace("-", " ").replace("_", " ").split()).casefold()


def _suggest(name: str) -> list[str]:
    token = name.casefold()
    matches = [z for z in available_timezones() if token in z.casefold()]
    return sorted(matches)[:5]


def resolve_zone(name: str) -> str:
    """Resolve a typed zone name to its canonical IANA key.

    Tries an exact IANA match first (case-insensitive), then the curated
    alias table. Raises `UtilityError` with up to five substring-matched
    suggestions if nothing resolves.
    """
    stripped = name.strip()
    exact = _IANA_BY_LOWER.get(stripped.casefold())
    if exact is not None:
        return exact

    token = _normalize_zone_token(stripped)
    alias = _ZONE_ALIASES.get(token)
    if alias is not None:
        return alias

    raise UtilityError(
        f"unknown timezone {name!r}",
        details={"zone": name, "suggestions": _suggest(stripped)},
    )


def _as_aware(at: datetime, zone: ZoneInfo) -> datetime:
    """Naive datetimes are UTC (see module docstring); aware ones keep their
    own tzinfo — `time_in`/`convert_time` only use `zone` to *read* it."""
    if at.tzinfo is None:
        return at.replace(tzinfo=UTC)
    return at


def time_in(zone: str, at: datetime) -> ZoneTime:
    """How `at` reads in `zone`. `at` naive is treated as UTC."""
    canonical = resolve_zone(zone)
    info = ZoneInfo(canonical)
    aware = _as_aware(at, info).astimezone(info)

    offset = aware.utcoffset()
    offset_minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    dst = aware.dst()
    abbreviation = aware.tzname() or canonical

    return ZoneTime(
        zone=canonical,
        iso=aware.isoformat(),
        local=aware.strftime("%Y-%m-%d %H:%M"),
        utc_offset_minutes=offset_minutes,
        abbreviation=abbreviation,
        is_dst=dst is not None and dst.total_seconds() != 0,
        day_of_week=_WEEKDAYS[aware.weekday()],
    )


def convert_time(at: datetime, from_zone: str, to_zone: str) -> tuple[ZoneTime, ZoneTime]:
    """Read `at` as a wall-clock time in `from_zone`, then show the same
    instant in `to_zone`.

    If `at` is naive, its wall-clock fields are interpreted as `from_zone`
    local time (not UTC — this is the one place a naive `at` means
    something other than UTC, because the caller supplied a source zone
    precisely to give it a reading). If `at` is aware, its own tzinfo is
    honoured and `from_zone` only labels the source reading.
    """
    source_info = ZoneInfo(resolve_zone(from_zone))
    source_instant = at.replace(tzinfo=source_info) if at.tzinfo is None else at

    source = time_in(from_zone, source_instant)
    target = time_in(to_zone, source_instant)
    return source, target


def zone_difference_minutes(a: str, b: str, at: datetime) -> int:
    """Minutes to add to zone `a`'s clock to read zone `b`'s clock, at `at`.

    Sign convention: positive means `b` is ahead of `a` (e.g. `a="America/
    New_York"`, `b="Europe/London"` in northern-hemisphere summer returns
    +300). This matches the everyday phrasing "London is 5 hours ahead of
    New York."
    """
    time_a = time_in(a, at)
    time_b = time_in(b, at)
    return time_b.utc_offset_minutes - time_a.utc_offset_minutes
