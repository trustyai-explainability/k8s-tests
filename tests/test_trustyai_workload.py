"""Image consistency checks for the TrustyAI workload operator.

The image the Deployment runs is compared against every source that decides it:
the `RELATED_IMAGE_*` value the module operator renders the workload operator
from, the image the harness built, and the operator's own ConfigMap.

Two further checks need an OLM-installed operator and have no subject on Kind,
which has neither OLM nor a CSV: the image being advertised in the operator CSV's
`relatedImages`, and it being released from `registry.redhat.io` by digest.
"""

from __future__ import annotations

import logging

import pytest
from kubernetes import client

from utilities.kube import container_images, read_deployment
from utilities.release import release
from utilities.trustyai import (
    MODULE_OPERATOR_DEPLOYMENT,
    WORKLOAD_CONFIG_MAP,
    WORKLOAD_IMAGE_CONFIG_KEY,
    WORKLOAD_IMAGE_RELATED_ENV,
    WORKLOAD_IMAGE_VAR,
    WORKLOAD_OPERATOR_DEPLOYMENT,
    deployment_env,
    deployment_exists,
    expected_image_from_env,
    read_config_map,
)

LOGGER = logging.getLogger(__name__)

pytestmark = [pytest.mark.workload, pytest.mark.usefixtures("trustyai_module")]


def _workload_operator_image(apps_client: client.AppsV1Api, namespace: str) -> str:
    """Image the workload operator Deployment runs."""
    deployment = read_deployment(apps_client, name=WORKLOAD_OPERATOR_DEPLOYMENT, namespace=namespace)
    images = container_images(deployment)
    assert images, f"deployment {namespace}/{WORKLOAD_OPERATOR_DEPLOYMENT} runs no containers"
    LOGGER.info("%s/%s runs %s", namespace, WORKLOAD_OPERATOR_DEPLOYMENT, images)
    return images[0]


@release(min="3.6")
def test_workload_operator_image_matches_the_module_override(apps_client: client.AppsV1Api, namespace: str) -> None:
    """The workload operator runs the image the module operator was told to render."""
    if not deployment_exists(apps_client, MODULE_OPERATOR_DEPLOYMENT, namespace):
        pytest.skip(
            f"deployment {namespace}/{MODULE_OPERATOR_DEPLOYMENT} is not present; nothing renders the workload "
            "operator on this cluster, so there is no override to compare against"
        )
    override = deployment_env(
        apps_client,
        name=MODULE_OPERATOR_DEPLOYMENT,
        namespace=namespace,
        env_name=WORKLOAD_IMAGE_RELATED_ENV,
    )
    if override is None:
        pytest.skip(
            f"{namespace}/{MODULE_OPERATOR_DEPLOYMENT} does not set {WORKLOAD_IMAGE_RELATED_ENV}, "
            "so it does not render the workload image from a RELATED_IMAGE_* value"
        )

    image = _workload_operator_image(apps_client, namespace)
    assert image == override, (
        f"deployment {namespace}/{WORKLOAD_OPERATOR_DEPLOYMENT} runs {image}, but the module operator "
        f"renders {WORKLOAD_IMAGE_RELATED_ENV}={override}"
    )


def test_workload_operator_runs_the_built_image(apps_client: client.AppsV1Api, namespace: str) -> None:
    """The workload operator runs the image the harness built, when the run came through `k8s-tests test`."""
    expected = expected_image_from_env(WORKLOAD_IMAGE_VAR)
    if expected is None:
        pytest.skip(f"{WORKLOAD_IMAGE_VAR} is not set, so there is no built image to compare against")

    image = _workload_operator_image(apps_client, namespace)
    assert image == expected, (
        f"deployment {namespace}/{WORKLOAD_OPERATOR_DEPLOYMENT} runs {image}, expected the built image {expected}"
    )


def test_workload_operator_image_matches_the_operator_config_map(
    apps_client: client.AppsV1Api, core_client: client.CoreV1Api, namespace: str
) -> None:
    """The workload operator runs the image its own ConfigMap advertises."""
    config_map = read_config_map(core_client=core_client, name=WORKLOAD_CONFIG_MAP, namespace=namespace)
    if config_map is None:
        pytest.skip(
            f"ConfigMap {namespace}/{WORKLOAD_CONFIG_MAP} is not present; it is written by the "
            "OLM-managed TrustyAI operator, not by the self-managed module operator"
        )

    configured = (config_map.data or {}).get(WORKLOAD_IMAGE_CONFIG_KEY)
    assert configured, f"ConfigMap {namespace}/{WORKLOAD_CONFIG_MAP} has no {WORKLOAD_IMAGE_CONFIG_KEY}"

    image = _workload_operator_image(apps_client, namespace)
    assert image == configured, (
        f"deployment {namespace}/{WORKLOAD_OPERATOR_DEPLOYMENT} runs {image}, but "
        f"{WORKLOAD_CONFIG_MAP} advertises {WORKLOAD_IMAGE_CONFIG_KEY}={configured}"
    )
