"""Fault injection limited to the TrustyAI operator Deployments."""

from __future__ import annotations

import logging
from collections.abc import Callable

from kubernetes import client

from utilities.kube import (
    assert_deployment_healthy,
    container_images,
    read_deployment,
    wait_for_deployment_available,
    wait_for_replacement_pods,
)
from utilities.trustyai import (
    MODULE_KIND,
    MODULE_OPERATOR_DEPLOYMENT,
    WORKLOAD_OPERATOR_DEPLOYMENT,
    wait_for_workload_operator,
)

LOGGER = logging.getLogger(__name__)


def _operator_pod(
    apps: client.AppsV1Api, core: client.CoreV1Api, namespace: str, name: str, deployment: client.V1Deployment
) -> client.V1Pod:
    selector = deployment.spec.selector
    if not selector.match_labels or selector.match_expressions:
        raise AssertionError(f"{namespace}/{name}: expected a label-only selector")
    labels = ",".join(f"{key}={value}" for key, value in sorted(selector.match_labels.items()))
    pods = core.list_namespaced_pod(namespace=namespace, label_selector=labels).items
    if len(pods) != 1:
        raise AssertionError(f"{namespace}/{name}: expected exactly one matching Pod, found {len(pods)}")
    pod = pods[0]
    if not pod.metadata.uid or pod.metadata.deletion_timestamp is not None:
        raise AssertionError(f"{namespace}/{name}: Pod is not stable")

    owner = next((ref for ref in pod.metadata.owner_references or [] if ref.controller), None)
    if not owner or owner.kind != "ReplicaSet":
        raise AssertionError(f"{pod.metadata.name}: Pod is not controlled by a ReplicaSet")
    replica_set = apps.read_namespaced_replica_set(name=owner.name, namespace=namespace)
    if replica_set.metadata.uid != owner.uid or not any(
        ref.controller and ref.kind == "Deployment" and ref.name == name and ref.uid == deployment.metadata.uid
        for ref in replica_set.metadata.owner_references or []
    ):
        raise AssertionError(f"{pod.metadata.name}: Pod is not owned by the target Deployment {namespace}/{name}")
    return pod


def delete_operator_pod_and_wait(
    apps: client.AppsV1Api,
    core: client.CoreV1Api,
    namespace: str,
    name: str,
    *,
    during_restart: Callable[[], None] | None = None,
) -> None:
    """Delete a single Pod, optionally inject another fault, then verify recovery."""
    if name not in (MODULE_OPERATOR_DEPLOYMENT, WORKLOAD_OPERATOR_DEPLOYMENT):
        raise ValueError(f"{name!r} is not a supported TrustyAI operator")
    deployment = read_deployment(apps, name, namespace)
    if deployment.spec.replicas != 1:
        raise AssertionError(f"{namespace}/{name}: chaos requires a single-replica operator")
    assert_deployment_healthy(apps, core, name, namespace)
    pod = _operator_pod(apps, core, namespace, name, deployment)
    uid = pod.metadata.uid
    LOGGER.info("injecting operator fault: deleting pod %s/%s (%s)", namespace, pod.metadata.name, uid)
    core.delete_namespaced_pod(
        name=pod.metadata.name,
        namespace=namespace,
        body=client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=uid)),
    )
    try:
        if during_restart is not None:
            during_restart()
    finally:
        wait_for_replacement_pods(apps, core, name=name, namespace=namespace, replaced={uid})
    recovered = wait_for_deployment_available(apps, name, namespace, core_client=core)
    assert recovered.metadata.uid == deployment.metadata.uid, f"{namespace}/{name}: Deployment was replaced"
    replacement = _operator_pod(apps, core, namespace, name, recovered)
    assert replacement.metadata.uid != uid, f"{namespace}/{name}: expected a new Pod UID"
    assert_deployment_healthy(apps, core, name, namespace)


def check_workload_deployment_recovery(
    apps: client.AppsV1Api, core: client.CoreV1Api, namespace: str, module_uid: str, *, restart: bool
) -> None:
    """Require the module reconciler to rebuild its workload operator Deployment."""
    name = WORKLOAD_OPERATOR_DEPLOYMENT
    original = read_deployment(apps, name, namespace)
    uid = original.metadata.uid
    if (
        not uid
        or not module_uid
        or not any(
            ref.kind == MODULE_KIND and ref.uid == module_uid for ref in original.metadata.owner_references or []
        )
    ):
        raise AssertionError(f"deployment {namespace}/{name} is not owned by the TrustyAI module under test")
    assert_deployment_healthy(apps, core, name, namespace)
    images = container_images(original)
    replicas = original.spec.replicas
    if replicas != 1:
        raise AssertionError(f"{namespace}/{name}: chaos requires a single-replica operator")

    def delete_workload() -> None:
        LOGGER.info("injecting operator fault: deleting Deployment %s/%s (%s)", namespace, name, uid)
        apps.delete_namespaced_deployment(
            name=name,
            namespace=namespace,
            body=client.V1DeleteOptions(propagation_policy="Foreground", preconditions=client.V1Preconditions(uid=uid)),
        )

    if restart:
        delete_operator_pod_and_wait(apps, core, namespace, MODULE_OPERATOR_DEPLOYMENT, during_restart=delete_workload)
    else:
        assert_deployment_healthy(apps, core, MODULE_OPERATOR_DEPLOYMENT, namespace)
        delete_workload()

    wait_for_workload_operator(apps, namespace, replaced_uid=uid)
    recovered = wait_for_deployment_available(apps, name, namespace, core_client=core)
    assert recovered.metadata.uid != uid, f"{namespace}/{name}: expected a new Deployment UID"
    assert recovered.spec.replicas == replicas, f"{namespace}/{name}: replica count was not restored"
    assert container_images(recovered) == images, f"{namespace}/{name}: container images were not restored"
    assert_deployment_healthy(apps, core, name, namespace)
