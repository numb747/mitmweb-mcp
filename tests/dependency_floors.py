#!/usr/bin/env python3
"""Print the declared lower bound of each dependency floor-check pins, as pip specifiers.

The bounds are read from the installed metadata rather than repeated in the workflow,
because a hardcoded pin drifts silently the first time a floor is raised and pip does not
object: installing a version the package excludes prints an error and still exits 0.

Kept as a file rather than inlined in ci.yml so that it is linted with the rest of the
repo. The version of this that lived in the workflow used an f-string containing a regex,
which is a SyntaxError on the 3.10 that floor-check runs — invisible in YAML, and not
something `ruff` or `ast.parse(feature_version=(3, 10))` will tell you about either.

    python tests/dependency_floors.py > pins.txt && pip install -r pins.txt
"""

import re
import sys
from importlib.metadata import requires

DISTRIBUTION = "mitmweb-mcp"
# Only the dependencies whose oldest accepted release the tests actually exercise: mcp
# for the tool registration path, pydantic for the argument-model hardening.
PINNED = ("mcp", "pydantic")

NAME = re.compile(r"[A-Za-z0-9._-]+")
LOWER_BOUND = re.compile(r">=\s*([\d.]+)")


def _name_of(requirement: str) -> str:
    """The distribution name at the head of a requirement string, e.g. "mcp<2,>=1.14.0"."""
    match = NAME.match(requirement)
    return match.group(0) if match else ""


def main() -> int:
    declared = requires(DISTRIBUTION) or []
    for want in PINNED:
        requirement = next((r for r in declared if _name_of(r) == want), None)
        if requirement is None:
            print(f"{DISTRIBUTION} declares no dependency on {want}", file=sys.stderr)
            return 1
        bound = LOWER_BOUND.search(requirement)
        if bound is None:
            print(f"{want} has no >= lower bound to pin: {requirement}", file=sys.stderr)
            return 1
        print(want + "==" + bound.group(1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
