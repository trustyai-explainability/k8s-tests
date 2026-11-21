"""Health checks for the TrustyAI operators deployed by the Kind harness.

On a modular checkout the workload operator is deployed by the module operator, so both
Deployments appearing and staying Ready is the first end-to-end signal that the harness
works. A pre-module checkout (3.5 and earlier) has only the workload operator: the
module operator's rows are gated by release when the run declares one, and by what the
cluster has when it does not. A run that does have the module operator - a declared 3.6
or later, or a cluster with the module CRD installed - and finds no Deployment has
deployed badly, so that row fails instead of skipping.
"""

from __future__ import annotations

import logging

import pytest
from kubernetes import client

from utilities.kube import assert_deployment_healthy, container_images, read_deployment
from utilities.release import applies, declared_release, release
from utilities.trustyai import MODULE_CRD_NAME, MODULE_OPERATOR_DEPLOYMENT, crd_exists, deployment_exists

LOGGER = logging.getLogger(__name__)

# The first release whose operator split the module operator out.
MODULE_OPERATOR_MIN_RELEASE = "3.6"

OPERATOR_DEPLOYMENTS = [
    pytest.param(
        MODULE_OPERATOR_DEPLOYMENT,
        id="module-operator",
        marks=[pytest.mark.module, release(min=MODULE_OPERATOR_MIN_RELEASE)],
    ),
    pytest.param(
        "trustyai-service-operator-controller-manager",
        id="workload-operator",
        marks=pytest.mark.workload,
    ),
]

EXPECTED_REPLICAS = 1


def _deployment(apps_client: client.AppsV1Api, namespace: str, name: str) -> client.V1Deployment:
    """The parametrized Deployment, skipped on a layout that does not deploy it.

    The module operator's rows also carry a release bound, which skips them before the
    body runs when a release without one was declared; with no release declared the
    layout falls to the cluster, which skips a pre-module one and fails a modular one
    whose Deployment never appeared.
    """
    require_layout(apps_client, namespace, name)
    return read_deployment(apps_client, name=name, namespace=namespace)


def require_layout(apps_client: client.AppsV1Api, namespace: str, name: str) -> None:
    """Skip a row this layout does not deploy, and fail when a modular run has no Deployment.

    The module operator exists only from 3.6 on, so its absence on a pre-module cluster is
    the layout difference the row skips for. A run that does have it - a declared release
    from 3.6 on, or an undeclared run against a cluster that installed the module CRD -
    without its Deployment has deployed badly, and the row says so.
    """
    if name != MODULE_OPERATOR_DEPLOYMENT or deployment_exists(apps_client, name, namespace):
        return
    run = declared_release()
    if run is None:
        if crd_exists(apps_client.api_client, MODULE_CRD_NAME):
            pytest.fail(
                f"deployment {namespace}/{name} is not present, but {MODULE_CRD_NAME} is installed: "
                "this cluster is modular",
                pytrace=False,
            )
        pytest.skip(f"deployment {namespace}/{name} is not present; this cluster has no module operator")
    if applies(min=MODULE_OPERATOR_MIN_RELEASE, run=run):
        pytest.fail(
            f"deployment {namespace}/{name} is not present; release {run} deploys the module operator",
            pytrace=False,
        )
    pytest.skip(f"deployment {namespace}/{name} is not present; release {run} has no module operator")


@pytest.mark.parametrize("name", OPERATOR_DEPLOYMENTS)
def test_deployment_exists(apps_client: client.AppsV1Api, namespace: str, name: str) -> None:
    LOGGER.info("checking that deployment %s/%s exists", namespace, name)
    deployment = _deployment(apps_client, namespace, name)
    LOGGER.info(
        "deployment %s/%s exists: %s replica(s), image(s) %s",
        namespace,
        name,
        deployment.spec.replicas,
        container_images(deployment),
    )

    assert deployment.metadata.name == name
    assert deployment.spec.replicas, f"deployment {namespace}/{name} asks for no replicas"


@pytest.mark.parametrize("name", OPERATOR_DEPLOYMENTS)
def test_deployment_has_single_replica(apps_client: client.AppsV1Api, namespace: str, name: str) -> None:
    LOGGER.info("checking that deployment %s/%s asks for %d replica", namespace, name, EXPECTED_REPLICAS)
    deployment = _deployment(apps_client, namespace, name)
    LOGGER.info(
        "deployment %s/%s wants %s replica(s), %s ready",
        namespace,
        name,
        deployment.spec.replicas,
        deployment.status.ready_replicas or 0,
    )

    assert deployment.spec.replicas == EXPECTED_REPLICAS, (
        f"deployment {namespace}/{name} asks for {deployment.spec.replicas} replica(s), expected {EXPECTED_REPLICAS}"
    )


@pytest.mark.parametrize("name", OPERATOR_DEPLOYMENTS)
def test_deployment_is_healthy(
    apps_client: client.AppsV1Api, core_client: client.CoreV1Api, namespace: str, name: str
) -> None:
    LOGGER.info("checking that deployment %s/%s and its pods are healthy", namespace, name)
    require_layout(apps_client, namespace, name)
    assert_deployment_healthy(apps_client, core_client, name=name, namespace=namespace)
