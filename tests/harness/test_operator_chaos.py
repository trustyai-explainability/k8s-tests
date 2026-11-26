"""Check operator fault injection against stub Kubernetes APIs, never a cluster."""

from unittest.mock import Mock

import pytest
from kubernetes import client

from utilities import operator_chaos
from utilities.trustyai import MODULE_KIND, MODULE_OPERATOR_DEPLOYMENT, WORKLOAD_OPERATOR_DEPLOYMENT


@pytest.fixture
def operator():
    apps = Mock(spec=client.AppsV1Api)
    core = Mock(spec=client.CoreV1Api)

    def owner(kind, name, uid):
        return client.V1OwnerReference(api_version="apps/v1", kind=kind, name=name, uid=uid, controller=True)

    deployment = Mock()
    deployment.metadata.uid = "deployment-uid"
    deployment.spec.replicas = 1
    deployment.spec.selector = client.V1LabelSelector(match_labels={"app": "operator"})
    apps.read_namespaced_deployment.return_value = deployment
    apps.read_namespaced_replica_set.return_value.metadata = client.V1ObjectMeta(
        uid="rs-uid", owner_references=[owner("Deployment", WORKLOAD_OPERATOR_DEPLOYMENT, "deployment-uid")]
    )

    def pod(name, uid):
        return client.V1Pod(
            metadata=client.V1ObjectMeta(name=name, uid=uid, owner_references=[owner("ReplicaSet", "rs", "rs-uid")])
        )

    initial, replacement = pod("before", "old-uid"), pod("after", "new-uid")
    core.list_namespaced_pod.side_effect = [client.V1PodList(items=[pod]) for pod in (initial, replacement)]
    return apps, core, initial


@pytest.fixture(autouse=True)
def recovery(monkeypatch):
    monkeypatch.setattr(operator_chaos, "assert_deployment_healthy", Mock())
    monkeypatch.setattr(operator_chaos, "wait_for_replacement_pods", Mock())
    monkeypatch.setattr(
        operator_chaos,
        "wait_for_deployment_available",
        Mock(side_effect=lambda apps, name, namespace, **kwargs: apps.read_namespaced_deployment()),
    )


def test_delete_targets_exact_pod_uid_and_waits_for_replacement(operator):
    apps, core, _ = operator
    operator_chaos.delete_operator_pod_and_wait(apps, core, "test-ns", WORKLOAD_OPERATOR_DEPLOYMENT)
    core.delete_namespaced_pod.assert_called_once()
    call = core.delete_namespaced_pod.call_args.kwargs
    assert (call["namespace"], call["name"], call["body"].preconditions.uid) == ("test-ns", "before", "old-uid")
    assert operator_chaos.wait_for_replacement_pods.call_args.kwargs["replaced"] == {"old-uid"}
    operator_chaos.wait_for_deployment_available.assert_called_once()


def test_matching_label_does_not_authorize_deletion(operator):
    apps, core, pod = operator
    pod.metadata.owner_references = []
    with pytest.raises(AssertionError, match="ReplicaSet"):
        operator_chaos.delete_operator_pod_and_wait(apps, core, "test-ns", WORKLOAD_OPERATOR_DEPLOYMENT)
    core.delete_namespaced_pod.assert_not_called()


@pytest.mark.parametrize("restart", [False, True])
def test_workload_deployment_loss_requires_module_ownership_and_waits_for_new_uid(operator, monkeypatch, restart):
    apps, core, _ = operator
    deployment = apps.read_namespaced_deployment.return_value
    replacement = Mock()
    replacement.metadata.uid = "new-deployment-uid"
    replacement.spec.replicas = 1
    deployment.metadata.owner_references = []
    calls = []
    apps.delete_namespaced_deployment.side_effect = lambda **kwargs: calls.append("delete")
    wait = Mock(return_value=replacement)
    monkeypatch.setattr(operator_chaos, "wait_for_workload_operator", wait, raising=False)
    operator_chaos.wait_for_deployment_available.return_value = replacement
    operator_chaos.wait_for_deployment_available.side_effect = None
    monkeypatch.setattr(operator_chaos, "container_images", Mock(return_value=["test-image"]), raising=False)

    def restart_module(apps, core, namespace, name, *, during_restart):
        assert name == MODULE_OPERATOR_DEPLOYMENT
        calls.append("restart")
        during_restart()
        calls.append("ready")

    monkeypatch.setattr(operator_chaos, "delete_operator_pod_and_wait", restart_module)
    with pytest.raises(AssertionError, match="owned"):
        operator_chaos.check_workload_deployment_recovery(apps, core, "test-ns", "module-uid", restart=restart)
    apps.delete_namespaced_deployment.assert_not_called()

    deployment.metadata.owner_references = [Mock(kind=MODULE_KIND, uid="module-uid")]
    operator_chaos.check_workload_deployment_recovery(apps, core, "test-ns", "module-uid", restart=restart)
    assert calls == (["restart", "delete", "ready"] if restart else ["delete"])
    delete = apps.delete_namespaced_deployment.call_args.kwargs
    assert (delete["name"], delete["namespace"]) == (WORKLOAD_OPERATOR_DEPLOYMENT, "test-ns")
    assert delete["body"].preconditions.uid == "deployment-uid"
    wait.assert_called_once_with(apps, "test-ns", replaced_uid="deployment-uid")


def test_non_operator_cannot_be_deleted(operator):
    apps, core, _ = operator
    with pytest.raises(ValueError, match="operator"):
        operator_chaos.delete_operator_pod_and_wait(apps, core, "test-ns", "evalhub")
    core.delete_namespaced_pod.assert_not_called()
