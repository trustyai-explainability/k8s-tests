"""Opt-in operator recovery checks; no operands or custom resources are mutated."""

import pytest
from kubernetes import client

from utilities.operator_chaos import delete_operator_pod_and_wait
from utilities.release import declared_release, release
from utilities.trustyai import (
    MODULE_CRD_NAME,
    MODULE_OPERATOR_DEPLOYMENT,
    WORKLOAD_OPERATOR_DEPLOYMENT,
    crd_exists,
    deployment_exists,
)

# A standalone CLI can discover a newer checkout before its dependencies are refreshed.
# When --chaos is requested, pytest_configure reports the missing dependency as a usage error.
hypothesis = pytest.importorskip("hypothesis")
pytestmark = pytest.mark.chaos


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(MODULE_OPERATOR_DEPLOYMENT, id="module-operator", marks=[pytest.mark.module, release(min="3.6")]),
        pytest.param(WORKLOAD_OPERATOR_DEPLOYMENT, id="workload-operator", marks=pytest.mark.workload),
    ],
)
@hypothesis.settings(max_examples=3, deadline=None, database=None, phases=(hypothesis.Phase.generate,))
@hypothesis.given(restarts=hypothesis.strategies.integers(min_value=1, max_value=3))
def test_operator_recovers_from_pod_loss(
    apps_client: client.AppsV1Api, core_client: client.CoreV1Api, namespace: str, name: str, restarts: int
) -> None:
    if (
        name == MODULE_OPERATOR_DEPLOYMENT
        and declared_release() is None
        and not deployment_exists(apps_client, name, namespace)
        and not crd_exists(apps_client.api_client, MODULE_CRD_NAME)
    ):
        pytest.skip("pre-module layout: no TrustyAI module operator")

    hypothesis.note(f"{namespace}/{name}: {restarts} sequential Pod deletions")
    for _ in range(restarts):
        delete_operator_pod_and_wait(apps_client, core_client, namespace, name)
