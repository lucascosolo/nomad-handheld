"""`get_system_info` — the tool the end-to-end vertical slice runs (D5).

READ_ONLY with the `SYSTEM_INFO` permission, so it auto-grants in every mode
(D14) and the slice never needs a human. It still goes through the full
pipeline: request -> decision -> grant -> execution. Auto-approval mints a
grant; it does not skip one.

It requires the `EXEC` capability rather than nothing at all. `Capability` has
exactly three members (D12) and there is no "introspection" capability, so
`EXEC` is the honest choice: it is the capability that marks "a machine you
could run something on", which is precisely what has system info to report,
and it excludes a HID target structurally.
"""

from __future__ import annotations

from pydantic import BaseModel

from nomad.targets.base import Capability, require_exec
from nomad.tools.base import Permission, Risk, ToolContext, ToolResult, ToolSpec


class SystemInfoParams(BaseModel):
    """No parameters."""


class GetSystemInfoTool:
    spec = ToolSpec(
        name="get_system_info",
        description=(
            "Report the operating system, architecture, CPU count, Python version "
            "and disk usage of the target machine."
        ),
        params_model=SystemInfoParams,
        risk=Risk.READ_ONLY,
        permissions=frozenset({Permission.SYSTEM_INFO}),
        required_capabilities=frozenset({Capability.EXEC}),
    )

    async def execute(self, params: SystemInfoParams, ctx: ToolContext) -> ToolResult:
        target = require_exec(ctx.target)
        info = await target.system_info()
        body = "\n".join(f"{key}: {value}" for key, value in info.items())
        return ToolResult.success(body, **info)
