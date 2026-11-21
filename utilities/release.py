"""Release gating for the checks.

A check declares the releases it applies to, and the session skips the ones the run's
release falls outside of:

    from utilities.release import release

    @release(min="3.6")
    def test_trustyai_module_crd_exists(...): ...

Bounds are inclusive and compared component by component, with missing components
treated as zero, so `max="3.6"` covers 3.6.0 but not 3.6.1 and a pre-release spelling
such as `3.6-ea.2` is compared as `3.6`.

The release comes from `--release` or the `OPERATOR_RELEASE` variable the harness
exports. When neither names one no gate is applied: the checks run and the fixtures skip
whatever the cluster does not have, which is how a bare `pytest` against a cluster
somebody else deployed still works.
"""

from __future__ import annotations

import os
import re

import pytest

# The flag is short because it is typed with its value (`--release 3.5`); the variable
# keeps the OPERATOR_ prefix so it cannot be confused with the release of something else
# in the environment it is exported into.
RELEASE_VAR = "OPERATOR_RELEASE"
RELEASE_OPTION = "--release"

# The marker the checks declare their range with; registered in pyproject.toml.
RELEASE_MARKER = "release"

_RELEASE = re.compile(r"^v?(\d+(?:\.\d+)*)$")


def _release_part(text: str) -> str:
    """The numeric part of a release spelling, without a leading v or a pre-release suffix."""
    return re.split(r"[-+]", text.strip(), maxsplit=1)[0]


def parse(text: str) -> tuple[int, ...]:
    """Parse a release version into comparable components."""
    match = _RELEASE.match(_release_part(text))
    if match is None:
        raise ValueError(f"{text!r} is not a release version like 3.5 or 3.5.1")
    return tuple(int(part) for part in match.group(1).split("."))


def declared_release() -> str | None:
    """The release this run targets, as declared; None when it was not declared."""
    return os.environ.get(RELEASE_VAR, "").strip() or None


def compare(left: str, right: str) -> int:
    """Compare two releases: -1, 0 or 1, with missing components treated as zero."""
    left_parts, right_parts = parse(left), parse(right)
    for index in range(max(len(left_parts), len(right_parts))):
        a = left_parts[index] if index < len(left_parts) else 0
        b = right_parts[index] if index < len(right_parts) else 0
        if a != b:
            return -1 if a < b else 1
    return 0


def applies(min: str | None = None, max: str | None = None, run: str | None = None) -> bool:
    """Whether a check bounded by `min`/`max` applies to the `run` release.

    An undeclared run applies to everything: without a release to compare against there
    is nothing to gate on, and the fixtures decide from the cluster.
    """
    if run is None:
        return True
    if min is not None and compare(run, min) < 0:
        return False
    return not (max is not None and compare(run, max) > 0)


def describe(min: str | None = None, max: str | None = None) -> str:
    """Readable spelling of a declared range, for skip reasons and logs."""
    if min is not None and max is not None:
        return f"{min} to {max}"
    if min is not None:
        return f"{min} and newer"
    if max is not None:
        return f"{max} and older"
    return "every release"


def release(min: str | None = None, max: str | None = None) -> pytest.MarkDecorator:
    """Mark a check as applying to the releases between `min` and `max`, both inclusive."""
    if min is None and max is None:
        raise ValueError("release() needs at least one of min or max")
    return pytest.mark.release(min=min, max=max)


__all__ = [
    "RELEASE_OPTION",
    "RELEASE_VAR",
    "RELEASE_MARKER",
    "applies",
    "compare",
    "declared_release",
    "describe",
    "parse",
    "release",
]
