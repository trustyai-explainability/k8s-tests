"""Health checks for the TrustyAI modular operator stack.

The module CRD, both operator Deployments, the singleton TrustyAI resource and
the module operator image, all read through the Kubernetes Python client.

There is no manifest for the module resource: `k8s-tests deploy` applies it through
`utilities.stack`, and the `trustyai_module` fixture creates it when a cluster has
none, then waits for the workload operator the module operator deploys.

The module operator was split out of the operator for 3.6: an rhoai-3.5 checkout
deploys one operator directly and has no module to reconcile, so these checks apply
from 3.6 on and are skipped on an older release.

The module operator is deployed here on its own, without a platform operator, so
the check for the ConfigMaps a platform operator writes (`odh-trustyai-config`
and `trustyai-dsc-config`) has no subject on this harness and is not made.
"""

from __future__ import annotations

import logging

import pytest
from kubernetes import client
from kubernetes.dynamic import DynamicClient

from utilities.kube import assert_deployment_healthy, read_deployment
from utilities.kube import container_images as deployment_images
from utilities.release import release
from utilities.trustyai import (
    MODULE_CRD_NAME,
    MODULE_IMAGE_VAR,
    MODULE_OPERATOR_DEPLOYMENT,
    WORKLOAD_OPERATOR_DEPLOYMENT,
    assert_module_observed_generation,
    assert_module_provisioning_succeeded,
    assert_module_ready,
    crd_exists,
    expected_image_from_env,
    module_api_version,
)

LOGGER = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.module,
    release(min="3.6"),
    pytest.mark.usefixtures("modular_trustyai_architecture", "trustyai_module"),
]


def test_trustyai_module_crd_exists(api_client: client.ApiClient) -> None:
    """Given TrustyAI is enabled, the module CRD is installed."""
    assert crd_exists(api_client, MODULE_CRD_NAME), f"CRD {MODULE_CRD_NAME} does not exist on the cluster"


def test_trustyai_module_operator_deployment_available(
    apps_client: client.AppsV1Api, core_client: client.CoreV1Api, namespace: str
) -> None:
    """Given modular TrustyAI is enabled, the module operator Deployment is Available."""
    assert_deployment_healthy(
        apps_client=apps_client,
        core_client=core_client,
        name=MODULE_OPERATOR_DEPLOYMENT,
        namespace=namespace,
    )


def test_trustyai_workload_operator_deployment_available(
    apps_client: client.AppsV1Api, core_client: client.CoreV1Api, namespace: str
) -> None:
    """Given the module operator reconciled, the workload operator Deployment is Available."""
    assert_deployment_healthy(
        apps_client=apps_client,
        core_client=core_client,
        name=WORKLOAD_OPERATOR_DEPLOYMENT,
        namespace=namespace,
    )


def test_trustyai_module_cr_ready(dynamic_client: DynamicClient, api_client: client.ApiClient) -> None:
    """Given both operators reconciled, the singleton module resource is Ready."""
    api_version = module_api_version(api_client)
    LOGGER.info("waiting for %s to report Ready", MODULE_CRD_NAME)
    module = assert_module_ready(dynamic_client=dynamic_client, api_version=api_version)

    assert_module_provisioning_succeeded(module)
    assert_module_observed_generation(module)


def test_module_operator_runs_the_built_image(apps_client: client.AppsV1Api, namespace: str) -> None:
    """The module operator runs the image the harness built, when the run came through `k8s-tests test`."""
    expected = expected_image_from_env(MODULE_IMAGE_VAR)
    if expected is None:
        pytest.skip(f"{MODULE_IMAGE_VAR} is not set, so there is no built image to compare against")

    deployment = read_deployment(apps_client, name=MODULE_OPERATOR_DEPLOYMENT, namespace=namespace)
    images = deployment_images(deployment)
    LOGGER.info("%s/%s runs %s, expected %s", namespace, MODULE_OPERATOR_DEPLOYMENT, images, expected)

    assert expected in images, (
        f"deployment {namespace}/{MODULE_OPERATOR_DEPLOYMENT} runs {images}, expected the built image {expected}"
    )
