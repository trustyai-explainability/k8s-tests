"""Checks for an EvalHub instance the tests create themselves.

The CRD shape and conversion contract, the Deployment topology, the resource's
status conditions, the tenant namespace the operator seeds, and the health
endpoint, called through a port-forward to the Service.

The instance, its namespace, the tenant namespace and, because Kind has no
service CA, its TLS material all come from fixtures in `tests/conftest.py`, so
nothing about EvalHub is baked into the harness.

Provider, collection and job APIs, MCP and the multi-tenancy API are not covered
here: they need provider images, a Kueue or MCP installation, or the platform's
tenant RBAC.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pytest
from kubernetes import client
from kubernetes.dynamic import DynamicClient

from utilities.evalhub import (
    CONDITION_MONITORING_DEGRADED,
    CONDITION_READY,
    EVALHUB_APP_LABEL,
    EVALHUB_COMPONENT_LABEL,
    EVALHUB_CONTAINER_NAME,
    EVALHUB_CRD_NAME,
    EVALHUB_HEALTH_PATH,
    EVALHUB_HEALTH_STATUS_HEALTHY,
    EVALHUB_IMAGE_RELATED_ENV,
    EVALHUB_IMAGE_VAR,
    EVALHUB_KIND,
    EVALHUB_NAME,
    EVALHUB_PROXY_CONTAINER_NAME,
    EVALHUB_SERVICE_PORT,
    EVALHUB_TENANT_CONFIG_MAPS,
    container_image,
    describe_evalhub_conditions,
    evalhub_api_version,
    evalhub_condition,
    port_forward,
    read_evalhub,
    request,
    tenant_config_maps,
)
from utilities.kube import read_deployment
from utilities.release import release
from utilities.trustyai import WORKLOAD_OPERATOR_DEPLOYMENT, deployment_env, expected_image_from_env

LOGGER = logging.getLogger(__name__)

pytestmark = [pytest.mark.evalhub]

FORWARDED_PORT = 18443
TENANT_CONFIG_MAP_TIMEOUT = 120
SINGLE_POD_TIMEOUT = 60


def evalhub_pods(core_client: client.CoreV1Api, namespace: str) -> list[client.V1Pod]:
    """Serving pods of the EvalHub instance, selected by the labels the operator sets.

    Pods that are terminating are left out: a rollout briefly runs the old and
    the new ReplicaSet side by side.
    """
    selector = f"app={EVALHUB_APP_LABEL},component={EVALHUB_COMPONENT_LABEL}"
    deadline = time.monotonic() + SINGLE_POD_TIMEOUT
    pods: list[client.V1Pod] = []
    while True:
        pods = [
            pod
            for pod in core_client.list_namespaced_pod(namespace=namespace, label_selector=selector).items
            if pod.metadata.deletion_timestamp is None
        ]
        LOGGER.info("%s: %d serving pod(s) matching %s", namespace, len(pods), selector)
        if len(pods) <= 1 or time.monotonic() >= deadline:
            return pods
        time.sleep(2)


def evalhub_deployment_image(apps_client: client.AppsV1Api, namespace: str) -> str:
    """Image the EvalHub container runs in the instance's namespace."""
    deployment = read_deployment(apps_client, name=EVALHUB_NAME, namespace=namespace)
    image = container_image(deployment, EVALHUB_CONTAINER_NAME)
    LOGGER.info("%s/%s runs %s", namespace, EVALHUB_NAME, image)
    assert image, f"deployment {namespace}/{EVALHUB_NAME} has no {EVALHUB_CONTAINER_NAME} container"
    return image


def test_evalhub_crd_exists_and_serves_both_versions(api_client: client.ApiClient) -> None:
    """Given EvalHub is part of this operator, its CRD is installed and serves v1 and v1alpha1."""
    crd = client.ApiextensionsV1Api(api_client).read_custom_resource_definition(name=EVALHUB_CRD_NAME)
    served = {version.name for version in crd.spec.versions or [] if version.served}
    LOGGER.info("CRD %s serves %s", EVALHUB_CRD_NAME, sorted(served))

    assert "v1" in served, f"v1 is not served by {EVALHUB_CRD_NAME}; served versions: {sorted(served)}"
    assert "v1alpha1" in served, f"v1alpha1 is not served by {EVALHUB_CRD_NAME}; served versions: {sorted(served)}"


def test_evalhub_crd_conversion_uses_a_webhook(api_client: client.ApiClient) -> None:
    """Given both versions are served, the CRD converts between them with a webhook."""
    crd = client.ApiextensionsV1Api(api_client).read_custom_resource_definition(name=EVALHUB_CRD_NAME)
    conversion = crd.spec.conversion

    assert conversion is not None, f"CRD {EVALHUB_CRD_NAME} has no conversion configuration"
    LOGGER.info("CRD %s conversion strategy: %s", EVALHUB_CRD_NAME, conversion.strategy)
    assert conversion.strategy == "Webhook", f"expected conversion strategy Webhook, got {conversion.strategy!r}"


def test_evalhub_instance_reads_through_both_versions(
    dynamic_client: DynamicClient,
    api_client: client.ApiClient,
    evalhub_instance: dict[str, Any],
    evalhub_instance_namespace: str,
) -> None:
    """The instance the tests created reads back through v1 and v1alpha1, so conversion works."""
    storage_version = evalhub_api_version(api_client)
    as_storage = read_evalhub(dynamic_client, storage_version, EVALHUB_NAME, evalhub_instance_namespace)
    as_alpha = read_evalhub(dynamic_client, "v1alpha1", EVALHUB_NAME, evalhub_instance_namespace)

    LOGGER.info("read %s/%s as %s and as v1alpha1", EVALHUB_KIND, EVALHUB_NAME, storage_version)
    assert as_storage["metadata"]["uid"] == as_alpha["metadata"]["uid"], (
        f"{EVALHUB_KIND}/{EVALHUB_NAME} read through {storage_version} and v1alpha1 is not the same resource"
    )
    assert as_storage["spec"].get("replicas") == as_alpha["spec"].get("replicas"), (
        f"replicas differ across versions: {storage_version}={as_storage['spec'].get('replicas')}, "
        f"v1alpha1={as_alpha['spec'].get('replicas')}"
    )


def test_evalhub_instance_reports_ready(evalhub_instance: dict[str, Any]) -> None:
    """The operator reconciled the instance the tests created: Ready, monitoring not degraded."""
    LOGGER.info("evalhub conditions: %s", describe_evalhub_conditions(evalhub_instance))

    assert evalhub_condition(evalhub_instance, CONDITION_READY) == "True", (
        f"expected {CONDITION_READY}=True on {EVALHUB_KIND}/{EVALHUB_NAME}; "
        f"conditions: {describe_evalhub_conditions(evalhub_instance)}"
    )
    assert evalhub_condition(evalhub_instance, CONDITION_MONITORING_DEGRADED) == "False", (
        f"expected {CONDITION_MONITORING_DEGRADED}=False on {EVALHUB_KIND}/{EVALHUB_NAME}; "
        f"conditions: {describe_evalhub_conditions(evalhub_instance)}"
    )


def test_evalhub_pod_has_expected_containers(
    core_client: client.CoreV1Api, evalhub_instance: dict[str, Any], evalhub_instance_namespace: str
) -> None:
    """The Deployment runs one pod with the service and its auth proxy."""
    pods = evalhub_pods(core_client, evalhub_instance_namespace)
    assert len(pods) == 1, f"expected 1 EvalHub pod, found {len(pods)}: {[pod.metadata.name for pod in pods]}"

    pod = pods[0]
    containers = [container.name for container in pod.spec.containers or []]
    LOGGER.info("pod %s containers: %s", pod.metadata.name, containers)
    assert containers == [EVALHUB_CONTAINER_NAME, EVALHUB_PROXY_CONTAINER_NAME], (
        f"expected containers {[EVALHUB_CONTAINER_NAME, EVALHUB_PROXY_CONTAINER_NAME]}, found {containers}"
    )


def test_evalhub_pod_has_expected_labels(
    core_client: client.CoreV1Api, evalhub_instance: dict[str, Any], evalhub_instance_namespace: str
) -> None:
    """The pod carries the labels the operator's Service and NetworkPolicy select on."""
    pods = evalhub_pods(core_client, evalhub_instance_namespace)
    assert pods, f"no pod matches app={EVALHUB_APP_LABEL},component={EVALHUB_COMPONENT_LABEL}"

    labels = pods[0].metadata.labels or {}
    expected = {
        "app": EVALHUB_APP_LABEL,
        "instance": EVALHUB_NAME,
        "component": EVALHUB_COMPONENT_LABEL,
    }
    LOGGER.info("pod %s labels: %s", pods[0].metadata.name, labels)
    for key, value in expected.items():
        assert labels.get(key) == value, f"expected label {key}={value}, got {key}={labels.get(key)}"


def test_evalhub_runs_the_image_the_workload_operator_passes(
    apps_client: client.AppsV1Api,
    namespace: str,
    evalhub_instance: dict[str, Any],
    evalhub_instance_namespace: str,
) -> None:
    """The EvalHub container runs the image the workload operator resolves from its RELATED_IMAGE_* value."""
    passed = deployment_env(
        apps_client,
        name=WORKLOAD_OPERATOR_DEPLOYMENT,
        namespace=namespace,
        env_name=EVALHUB_IMAGE_RELATED_ENV,
    )
    if passed is None:
        pytest.skip(
            f"{namespace}/{WORKLOAD_OPERATOR_DEPLOYMENT} does not set {EVALHUB_IMAGE_RELATED_ENV}, "
            "so the operator resolves the EvalHub image from its ConfigMap instead"
        )

    image = evalhub_deployment_image(apps_client, evalhub_instance_namespace)
    assert image == passed, (
        f"deployment {evalhub_instance_namespace}/{EVALHUB_NAME} runs {image}, but the workload operator "
        f"resolves {EVALHUB_IMAGE_RELATED_ENV}={passed}"
    )


def test_evalhub_runs_the_requested_image(
    apps_client: client.AppsV1Api, evalhub_instance: dict[str, Any], evalhub_instance_namespace: str
) -> None:
    """`k8s-tests up --evalhub-img ...` reaches the instances the tests create."""
    expected = expected_image_from_env(EVALHUB_IMAGE_VAR)
    if expected is None:
        pytest.skip(f"{EVALHUB_IMAGE_VAR} is not set, so no EvalHub image was requested for this run")

    image = evalhub_deployment_image(apps_client, evalhub_instance_namespace)
    assert image == expected, (
        f"deployment {evalhub_instance_namespace}/{EVALHUB_NAME} runs {image}, expected the requested image {expected}"
    )


@release(min="3.6")
@pytest.mark.usefixtures("modular_trustyai_architecture")
def test_evalhub_tenant_namespace_gets_its_config_maps(
    core_client: client.CoreV1Api, evalhub_instance: dict[str, Any], evalhub_tenant_namespace: str
) -> None:
    """A namespace labelled as a tenant receives the discovery and CA ConfigMaps of the instance."""
    deadline = time.monotonic() + TENANT_CONFIG_MAP_TIMEOUT
    while True:
        present = tenant_config_maps(core_client, evalhub_tenant_namespace)
        missing = [name for name in EVALHUB_TENANT_CONFIG_MAPS if name not in present]
        if not missing:
            return
        if time.monotonic() >= deadline:
            break
        LOGGER.info("waiting for %s in %s", missing, evalhub_tenant_namespace)
        time.sleep(5)
    pytest.fail(
        f"the operator did not seed {missing} into tenant namespace {evalhub_tenant_namespace} "
        f"within {TENANT_CONFIG_MAP_TIMEOUT}s; present: {sorted(present)}",
        pytrace=False,
    )


def test_evalhub_health_endpoint(
    kube_context: str, evalhub_instance: dict[str, Any], evalhub_instance_namespace: str
) -> None:
    """Given a running instance, GET /api/v1/health answers 200 with status healthy."""
    with port_forward(
        context=kube_context,
        namespace=evalhub_instance_namespace,
        target=f"service/{EVALHUB_NAME}",
        local_port=FORWARDED_PORT,
        remote_port=EVALHUB_SERVICE_PORT,
    ) as base_url:
        response = request("get", f"{base_url}{EVALHUB_HEALTH_PATH}")

    LOGGER.info("health response: %s %s", response.status_code, response.text)
    assert response.status_code == 200, f"expected 200 from {EVALHUB_HEALTH_PATH}, got {response.status_code}"
    assert response.json()["status"] == EVALHUB_HEALTH_STATUS_HEALTHY, (
        f"expected status {EVALHUB_HEALTH_STATUS_HEALTHY!r}, got {response.json()}"
    )


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_evalhub_health_rejects_non_get_methods(
    kube_context: str, evalhub_instance: dict[str, Any], evalhub_instance_namespace: str, method: str
) -> None:
    """Given a running instance, non-GET methods on the health endpoint answer 405."""
    with port_forward(
        context=kube_context,
        namespace=evalhub_instance_namespace,
        target=f"service/{EVALHUB_NAME}",
        local_port=FORWARDED_PORT,
        remote_port=EVALHUB_SERVICE_PORT,
    ) as base_url:
        response = request(method, f"{base_url}{EVALHUB_HEALTH_PATH}")

    LOGGER.info("%s health response: %s", method.upper(), response.status_code)
    assert response.status_code == 405, (
        f"expected 405 for {method.upper()} on {EVALHUB_HEALTH_PATH}, got {response.status_code}"
    )
