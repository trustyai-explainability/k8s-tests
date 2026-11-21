"""The harness unit tests are pure: nothing here talks to a cluster.

The root conftest has a session-scoped autouse fixture that logs where the run points
and skips the session when there is no usable kubeconfig. These tests shadow it, so
`pytest tests/harness` runs on a machine with no cluster at all - and they also start
from an environment with no harness settings in it, because a `k8s-tests test` run
exports its own into the pytest process and a unit test must not inherit them.
"""

from __future__ import annotations

import pytest

from harness.config import KEEP_RESOURCES_VAR, SETTINGS


@pytest.fixture(scope="session", autouse=True)
def report_target() -> None:
    """Nothing to report: the tests below read settings, records and command lines."""


@pytest.fixture(autouse=True)
def clean_harness_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the settings an enclosing `k8s-tests test` exported into this process."""
    for setting in SETTINGS:
        monkeypatch.delenv(setting.envvar, raising=False)
    monkeypatch.delenv(KEEP_RESOURCES_VAR, raising=False)
