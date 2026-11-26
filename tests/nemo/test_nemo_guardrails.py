"""Checks for a NemoGuardrails resource the workload operator serves as a NeMo Guardrails server.

The checks create the resource in a namespace of their own, with one configuration, and
hold the operator to what its code promises: the Deployment runs the server image the run
was given, the configuration is mounted where the server reads it, and the server lists it.
The last two are separate checks, so a failure says which side of that contract broke.

The component first ships in the 3.2 release, so an older `--release` is not applicable, and
an install that leaves the service off is skipped rather than failed (see `skip_unless_served`).
"""

from __future__ import annotations

import logging

import pytest
from kubernetes import client
from kubernetes.dynamic import DynamicClient

from utilities.kube import keep_resources, read_deployment
from utilities.nemo import (
    NEMO_CONFIG_ID,
    NEMO_CRD_NAME,
    NEMO_FORWARDED_PORT,
    NEMO_MOUNT_PATH,
    NEMO_NAME,
    NEMO_SERVICE_PORT,
    create_nemo,
    delete_nemo,
    served_configurations,
    server_container,
    skip_unless_enabled,
    skip_unless_served,
    wait_for_nemo_deployment,
)
from utilities.portforward import allocate_local_ports, port_forward, release_local_ports
from utilities.release import release
from utilities.trustyai import crd_exists, expected_image_from_env

LOGGER = logging.getLogger(__name__)

# The release whose operator ships the NemoGuardrails component: its CRD, its controller and the
# `nemo-guardrails-image` key. Earlier releases have none of them.
NEMO_MIN_VERSION = "3.2"

pytestmark = [
    pytest.mark.nemo,
    pytest.mark.usefixtures("trustyai_module"),
    release(min=NEMO_MIN_VERSION),
]


@pytest.fixture(scope="module")
def nemo_namespace(
    request: pytest.FixtureRequest,
    workload_namespaces,
    api_client: client.ApiClient,
    core_client: client.CoreV1Api,
    apps_client: client.AppsV1Api,
    dynamic_client: DynamicClient,
    trustyai_module: dict | None,
) -> str:
    """A namespace holding a NemoGuardrails resource, rolled out before the first check.

    Nothing is created where the install does not serve NeMo: the resource would be accepted and
    never reconciled, which is a property of that install rather than a failure of this one.
    """
    skip_unless_served(api_client, trustyai_module)
    namespace = workload_namespaces(request, prefix="nemo-guardrails")

    def remove_resource() -> None:
        # Ahead of the namespace's own release, which is registered first and so runs last.
        if keep_resources():
            LOGGER.warning("KEEP_RESOURCES is set: leaving %s/%s in place", namespace, NEMO_NAME)
            return
        delete_nemo(dynamic_client, namespace)

    request.addfinalizer(remove_resource)
    create_nemo(core_client, dynamic_client, namespace)
    wait_for_nemo_deployment(apps_client, core_client, namespace)
    return namespace


def test_the_resource_definition_is_installed(api_client: client.ApiClient, trustyai_module: dict | None) -> None:
    skip_unless_enabled(trustyai_module)
    assert crd_exists(api_client, NEMO_CRD_NAME), f"{NEMO_CRD_NAME} is not installed; the operator installs it"


def test_the_operator_runs_the_server_image_it_was_given(apps_client: client.AppsV1Api, nemo_namespace: str) -> None:
    image = server_container(read_deployment(apps_client, name=NEMO_NAME, namespace=nemo_namespace)).image
    expected = expected_image_from_env("NEMO_IMG")
    if expected is None:
        # The run named no image, so there is nothing to compare: this only shows which one is running.
        LOGGER.info("NEMO_IMG is not set; the Deployment runs %s", image)
        assert image, "the Deployment's server container names no image"
        return
    assert image == expected, f"the Deployment runs {image}, not the NeMo Guardrails image this run built ({expected})"


def test_the_configuration_is_mounted_where_the_server_reads_it(
    apps_client: client.AppsV1Api, nemo_namespace: str
) -> None:
    container = server_container(read_deployment(apps_client, name=NEMO_NAME, namespace=nemo_namespace))
    mounts = sorted(mount.mount_path for mount in container.volume_mounts or [])
    assert NEMO_MOUNT_PATH in mounts, f"configuration {NEMO_CONFIG_ID} is mounted at {mounts}, not {NEMO_MOUNT_PATH}"


def test_the_server_lists_the_configuration_it_was_given(kube_context: str, nemo_namespace: str) -> None:
    (local_port,) = allocate_local_ports(NEMO_FORWARDED_PORT)
    try:
        with port_forward(
            context=kube_context,
            namespace=nemo_namespace,
            target=f"service/{NEMO_NAME}",
            local_port=local_port,
            remote_port=NEMO_SERVICE_PORT,
        ) as base_url:
            response = served_configurations(base_url)
    finally:
        release_local_ports(local_port)
    assert response.status_code == 200, f"GET /v1/rails/configs answered {response.status_code}: {response.text[:200]}"
    served = [entry.get("id") for entry in response.json()]
    assert NEMO_CONFIG_ID in served, (
        f"the server lists {served}, not {NEMO_CONFIG_ID}: it is not reading {NEMO_MOUNT_PATH}"
    )
