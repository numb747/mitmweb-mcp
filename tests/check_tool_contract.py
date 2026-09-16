#!/usr/bin/env python3
"""Assert the tool contract clients rely on. Needs no mitmweb and no network.

Both CI jobs run this file rather than each inlining its own assertions: import-check
runs it on every supported Python against the newest mcp, floor-check against the oldest
mcp the package claims to support, and neither can drift from the other.

It is deliberately strict about the read-only boundary. `assert tool.annotations` looks
like it checks something but cannot fail: ToolAnnotations is a pydantic model, so an
instance with every hint left unset is still truthy. The boundary is only worth
publishing if inverting it breaks the build, so assert the hint values themselves.

    python tests/check_tool_contract.py
"""

import asyncio
import sys

from mitmweb_mcp.server import mcp

# The one tool that puts a captured request back on the wire; everything else is a GET
# against the local mitmweb and should stay safe for a client to run unattended.
WRITER = "replay_flow"
EXPECTED_TOOLS = 10


def main() -> int:
    tools = asyncio.run(mcp.list_tools())
    errors: list[str] = []

    names = sorted(t.name for t in tools)
    print(f"{len(names)} tools: {', '.join(names)}")
    if len(names) != EXPECTED_TOOLS:
        errors.append(f"expected {EXPECTED_TOOLS} tools, got {len(names)}")
    if "clear_flows" in names:
        errors.append("clear_flows must not exist: wiping the session stays the user's call")
    if WRITER not in names:
        errors.append(f"{WRITER} is missing, which makes the boundary check below vacuous")

    for t in tools:
        if not t.description:
            errors.append(f"{t.name}: no description")

        undocumented = sorted(
            k for k, v in (t.inputSchema or {}).get("properties", {}).items()
            if not v.get("description")
        )
        if undocumented:
            errors.append(f"{t.name}: undocumented parameters {undocumented}")

        a = t.annotations
        if a is None:
            errors.append(f"{t.name}: no annotations")
        elif t.name == WRITER:
            if a.readOnlyHint is not False or a.destructiveHint is not True:
                errors.append(
                    f"{t.name} must declare readOnlyHint=False and destructiveHint=True "
                    f"— it replays whatever was captured — but declares "
                    f"readOnlyHint={a.readOnlyHint} destructiveHint={a.destructiveHint}"
                )
        elif a.readOnlyHint is not True:
            errors.append(
                f"{t.name} must declare readOnlyHint=True, got {a.readOnlyHint}"
            )

    for e in errors:
        print(f"FAIL  {e}", file=sys.stderr)
    print(f"{len(errors)} failure(s)" if errors else "tool contract OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
