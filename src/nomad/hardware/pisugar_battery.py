"""PiSugar S Plus battery driver (D18).

`pisugar` (the client for `pisugar-server`'s local RPC socket) is an optional
dependency, imported lazily inside `__init__` — exactly the pattern
`agent/backends/__init__.py` uses for `claude-agent-sdk` (D24). Importing
this module must never require the dependency; only *constructing* the
driver does, and a missing dependency there becomes a clear `HardwareError`,
never a bare `ImportError` escaping from module scope.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from nomad.core.logging import get_logger
from nomad.hardware.errors import HardwareError

logger = get_logger(__name__)


@dataclass(frozen=True)
class BatteryReading:
    """Structurally what `nomad.mcp.hardware.BatteryStatus` expects — same
    field names, read as attributes by every caller. Defined here rather than
    imported from `mcp.hardware` because `hardware` sits below `mcp` in the
    dependency graph; a caller reading `.percent`/`.charging`/`.voltage`
    cannot tell the two classes apart."""

    percent: float
    charging: bool = False
    voltage: float | None = None


class PiSugarBattery:
    """Reads charge state from a running `pisugar-server` over its local RPC
    socket. Fails to *construct* — never to import — when the client
    dependency is missing."""

    def __init__(self, *, address: str = "127.0.0.1", port: int = 8423) -> None:
        try:
            import pisugar  # type: ignore[import-not-found]
        except ImportError as exc:
            raise HardwareError(
                "PiSugarBattery requires the optional 'pisugar' package; "
                "install it or select the mock/headless battery driver instead",
                {"address": address, "port": port},
            ) from exc

        try:
            self._client = pisugar.PiSugarServer(pisugar.connect_tcp(address, port))
        except Exception as exc:  # noqa: BLE001 - any connect failure is a HardwareError
            raise HardwareError(
                "Could not connect to pisugar-server",
                {"address": address, "port": port, "error": str(exc)},
            ) from exc

    async def read(self) -> BatteryReading:
        return await asyncio.to_thread(self._read_sync)

    def _read_sync(self) -> BatteryReading:
        try:
            percent = float(self._client.get_battery_level())
            charging = bool(self._client.get_battery_charging())
        except Exception as exc:  # noqa: BLE001 - surfaced as one clear error
            raise HardwareError("PiSugar read failed", {"error": str(exc)}) from exc

        voltage: float | None = None
        getter = getattr(self._client, "get_battery_voltage", None)
        if getter is not None:
            try:
                voltage = float(getter())
            except Exception as exc:  # noqa: BLE001 - voltage is a bonus, never fatal
                logger.debug("PiSugar voltage read failed", extra={"error": str(exc)})
                voltage = None

        return BatteryReading(percent=percent, charging=charging, voltage=voltage)
