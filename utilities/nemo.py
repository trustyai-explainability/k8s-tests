"""The NemoGuardrails resource the checks create, and the server it asks.

The workload operator renders a Deployment and a Service, both named after the resource.
It mounts each configuration's ConfigMaps at /app/config/<name> and runs the server image
in a container called `nemo-guardrails`, listening on 8000. With auth off (the default)
the Service's port 80 is that port. The checks create a resource with one configuration of
their own, then read both halves of that contract: where the operator mounts it, and what
the server serves from there.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pytest
import requests
from kubernetes import client
from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import NotFoundError

from utilities.kube import wait_for_deployment_available
from utilities.trustyai import crd_exists, deployment_exists

LOGGER = logging.getLogger(__name__)

NEMO_API_GROUP = "trustyai.opendatahub.io"
NEMO_API_VERSION = f"{NEMO_API_GROUP}/v1alpha1"
NEMO_KIND = "NemoGuardrails"
NEMO_CRD_NAME = "nemoguardrails.trustyai.opendatahub.io"

NEMO_NAME = "nemo-guardrails"
NEMO_CONTAINER = "nemo-guardrails"
# `nemo` is the configuration name the operator's server image starts with: its entrypoint reads
# /app/config/${CONFIG_ID}, and CONFIG_ID defaults to `nemo`, which the operator does not override.
NEMO_CONFIG_ID = "nemo"
NEMO_CONFIG_MAP = "nemo-guardrails-config"
# Where the operator mounts a configuration, and so where the server has to read it from.
NEMO_MOUNT_PATH = f"/app/config/{NEMO_CONFIG_ID}"
NEMO_SERVICE_PORT = 80
NEMO_FORWARDED_PORT = 18000

# The smallest configuration the server lists. It lists configurations by their directories
# and does not load them, so no model has to be reachable for the check.
NEMO_CONFIG_YAML = "models: []\n"

# The first rollout pulls the operator's server image, about 2.7 GB compressed, on the Kind node's
# clock, so the wait covers that pull as well as the start.
DEPLOYMENT_TIMEOUT = 900
DEPLOYMENT_POLL_SECONDS = 5


def skip_unless_served(api_client: client.ApiClient, trustyai_module: dict[str, Any] | None) -> None:
    """Skip when this install does not run a NeMo Guardrails server, so the checks are not applicable.

    Used by the fixture and the checks alike, so an install that has the CRD but leaves the service
    off is recorded as not applicable rather than as a failure of the section.
    """
    if not crd_exists(api_client, NEMO_CRD_NAME):
        pytest.skip(f"{NEMO_CRD_NAME} is not installed, so this install serves no NeMo Guardrails server")
    enabled = ((trustyai_module or {}).get("spec") or {}).get("enabledServices") or {}
    if enabled and not enabled.get("nemoGuardrails", True):
        pytest.skip("the module resource does not enable nemoGuardrails, so no NemoGuardrails is reconciled")


def nemo_config_map(namespace: str) -> client.V1ConfigMap:
    return client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=NEMO_CONFIG_MAP, namespace=namespace),
        data={"config.yml": NEMO_CONFIG_YAML},
    )


def nemo_body(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": NEMO_API_VERSION,
        "kind": NEMO_KIND,
        "metadata": {"name": NEMO_NAME, "namespace": namespace},
        "spec": {
            "nemoConfigs": [{"name": NEMO_CONFIG_ID, "configMaps": [NEMO_CONFIG_MAP], "default": True}],
            # The route is OpenShift's. These checks reach the Service through a port-forward.
            "exposeRoute": False,
        },
    }


def _resource(dynamic_client: DynamicClient) -> Any:
    return dynamic_client.resources.get(api_version=NEMO_API_VERSION, kind=NEMO_KIND)


def create_nemo(core_client: client.CoreV1Api, dynamic_client: DynamicClient, namespace: str) -> None:
    """Create the configuration and the resource that serves it, in one namespace."""
    LOGGER.info("creating %s/%s with configuration %s", namespace, NEMO_NAME, NEMO_CONFIG_MAP)
    core_client.create_namespaced_config_map(namespace=namespace, body=nemo_config_map(namespace))
    _resource(dynamic_client).create(body=nemo_body(namespace), namespace=namespace)


def delete_nemo(dynamic_client: DynamicClient, namespace: str) -> None:
    try:
        _resource(dynamic_client).delete(name=NEMO_NAME, namespace=namespace)
    except NotFoundError:
        LOGGER.info("%s/%s is already gone", namespace, NEMO_NAME)


def wait_for_nemo_deployment(
    apps_client: client.AppsV1Api,
    core_client: client.CoreV1Api,
    namespace: str,
    timeout: int = DEPLOYMENT_TIMEOUT,
) -> None:
    """Wait until the operator has rendered the Deployment and it has rolled out.

    The Deployment appears a reconcile after the resource does, and the rollout wait
    reads it at once, so the wait for its existence comes first.
    """
    deadline = time.monotonic() + timeout
    while not deployment_exists(apps_client, NEMO_NAME, namespace):
        if time.monotonic() >= deadline:
            pytest.fail(
                f"the operator did not render Deployment {namespace}/{NEMO_NAME} within {timeout}s; "
                f"check the workload operator's logs for {NEMO_KIND}",
                pytrace=False,
            )
        time.sleep(DEPLOYMENT_POLL_SECONDS)
    wait_for_deployment_available(apps_client, NEMO_NAME, namespace, timeout=timeout, core_client=core_client)


def server_container(deployment: client.V1Deployment) -> client.V1Container:
    for container in deployment.spec.template.spec.containers or []:
        if container.name == NEMO_CONTAINER:
            return container
    pytest.fail(f"the Deployment has no container named {NEMO_CONTAINER}", pytrace=False)


def served_configurations(base_url: str) -> requests.Response:
    """The server's answer to its configuration list, over the plain-HTTP port-forward."""
    LOGGER.info("GET %s/v1/rails/configs", base_url)
    return requests.get(f"{base_url}/v1/rails/configs", timeout=30)
