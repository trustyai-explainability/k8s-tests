"""The policy an LMEvalJob runs under, and the identity that ties it to the job.

An `LMEvalJob` is lm-evaluation-harness as a resource. The workload operator turns it
into a Pod for the benchmark and a NetworkPolicy whose selector is that Pod's identity
labels, and it re-derives both on every reconcile: deleting the policy, editing its
spec, or taking an identity label off the Pod has to be repaired, because otherwise a
running evaluation detaches from the resource that owns it. A *completed* job is the
hard case - nothing re-checks it on an interval once it is done, so its retained Pod
and policy have to be repaired from the watch alone.

The Pod is held out of the scheduler here (see `utilities/lmeval.py`), so these checks
say nothing about whether an evaluation scores anything: they are about the policy and
the identity, and they read both from what the operator reports rather than assuming
they exist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pytest
from kubernetes import client

from utilities import lmeval
from utilities.kube import keep_resources
from utilities.release import release
from utilities.trustyai import crd_exists

LOGGER = logging.getLogger(__name__)

pytestmark = [pytest.mark.lmeval, pytest.mark.usefixtures("trustyai_module")]

# The release whose operator writes the NetworkPolicy that guards an LMEvalJob's Pod.
POLICY_MIN_VERSION = "3.6"


@dataclass(frozen=True)
class Evaluation:
    """The job under test, and the two objects the operator made for it."""

    namespace: str
    name: str
    uid: str
    pod_name: str
    policy_name: str


@pytest.fixture(scope="module")
def evaluation(
    request: pytest.FixtureRequest,
    workload_namespaces,
    custom_api: client.CustomObjectsApi,
    networking_client: client.NetworkingV1Api,
    api_client: client.ApiClient,
    trustyai_module: dict[str, Any] | None,
) -> Evaluation:
    """One `LMEvalJob` in a namespace of its own, with its policy and Pod already made.

    Nothing is created when the module resource leaves the `lmes` service off: the
    resource would be accepted and never reconciled, which is a property of that
    install rather than a failure of this one.
    """
    if not crd_exists(api_client, lmeval.CRD_NAME):
        pytest.skip(f"{lmeval.CRD_NAME} is not installed, so this cluster runs no LMEvalJob")
    enabled = ((trustyai_module or {}).get("spec") or {}).get("enabledServices") or {}
    if enabled and not enabled.get("lmes", True):
        pytest.skip("the module resource does not enable the lmes service, so no LMEvalJob is reconciled")

    namespace = workload_namespaces(request, prefix="trustyai-lmeval")
    name = "arc-easy"
    lmeval.create_job(custom_api, namespace, name)
    if keep_resources():
        LOGGER.warning("KEEP_RESOURCES is set: leaving %s/%s and its Pod and policy on the cluster", namespace, name)
    else:
        request.addfinalizer(lambda: lmeval.delete_job(custom_api, namespace, name))

    job = lmeval.wait_for_scheduled_job(custom_api, namespace, name)
    policy = lmeval.wait_for_policy(networking_client, namespace, job["metadata"]["uid"])
    return Evaluation(
        namespace=namespace,
        name=name,
        uid=job["metadata"]["uid"],
        pod_name=job["status"]["podName"],
        policy_name=policy.metadata.name,
    )


def _policy(networking_client: client.NetworkingV1Api, evaluation: Evaluation) -> client.V1NetworkPolicy:
    return networking_client.read_namespaced_network_policy(name=evaluation.policy_name, namespace=evaluation.namespace)


@release(min=POLICY_MIN_VERSION)
def test_policy_guards_the_execution_pod(networking_client: client.NetworkingV1Api, evaluation: Evaluation) -> None:
    """The operator creates one policy, owned by the job, admitting no ingress and all egress."""
    policy = _policy(networking_client, evaluation)
    LOGGER.info("%s/%s: %s", evaluation.namespace, policy.metadata.name, policy.spec)

    owners = [(reference.kind, reference.name, reference.uid) for reference in policy.metadata.owner_references or []]
    assert (lmeval.KIND, evaluation.name, evaluation.uid) in owners, f"owner references are {owners}"
    assert any(reference.controller for reference in policy.metadata.owner_references or []), (
        "the policy has no controller owner reference, so nothing would garbage collect it"
    )
    assert policy.spec.policy_types == ["Ingress", "Egress"], policy.spec.policy_types
    # The driver listens on loopback and is driven over pods/exec; replies to
    # outbound connections need no ingress grant.
    assert not policy.spec.ingress, f"the policy admits {policy.spec.ingress}"
    assert len(policy.spec.egress or []) == 1, f"the policy allows {policy.spec.egress}, expected one allow-all rule"
    assert policy.spec.egress[0].ports is None and policy.spec.egress[0].to is None, (
        "the egress rule is narrower than the operator's documented allow-all intent"
    )


@release(min=POLICY_MIN_VERSION)
def test_policy_selects_the_pods_identity(
    networking_client: client.NetworkingV1Api, core_client: client.CoreV1Api, evaluation: Evaluation
) -> None:
    """The selector is the identity the operator derived, and the Pod carries it.

    The four labels are one identity in two places: the policy selects on three of
    them, and the Pod has all four. A Pod that lost a selected label would stop being
    guarded without anything reporting it, which is why the two are asserted together.
    """
    policy = _policy(networking_client, evaluation)
    selector = policy.spec.pod_selector.match_labels or {}
    expected = {
        lmeval.COMPONENT_LABEL: lmeval.COMPONENT,
        lmeval.ROLE_LABEL: lmeval.ROLE,
        lmeval.OWNER_UID_LABEL: evaluation.uid,
    }
    assert selector == expected, f"the policy selects {selector}, expected {expected}"

    pod = lmeval.read_pod(core_client, evaluation.namespace, evaluation.pod_name)
    labels = pod.metadata.labels or {}
    LOGGER.info("%s/%s labels: %s", evaluation.namespace, evaluation.pod_name, labels)
    expected_pod = dict(expected, **{lmeval.JOB_UID_LABEL: evaluation.uid})
    for label, value in expected_pod.items():
        assert labels.get(label) == value, f"the Pod has {label}={labels.get(label)!r}, expected {value!r}"
    assert all(labels.get(label) == value for label, value in selector.items()), (
        "the Pod is not selected by the policy the operator created for it"
    )


@pytest.mark.parametrize("label", lmeval.POD_IDENTITY_LABELS)
@release(min=POLICY_MIN_VERSION)
def test_operator_restores_a_pod_identity_label(
    core_client: client.CoreV1Api, evaluation: Evaluation, label: str
) -> None:
    """Taking an identity label off the Pod is repaired, not left to the next job."""
    pod = lmeval.read_pod(core_client, evaluation.namespace, evaluation.pod_name)
    expected = pod.metadata.labels[label]
    lmeval.remove_label(core_client, evaluation.namespace, evaluation.pod_name, label)
    lmeval.wait_for_label(core_client, evaluation.namespace, evaluation.pod_name, label, expected)


@release(min=POLICY_MIN_VERSION)
def test_operator_repairs_policy_drift(networking_client: client.NetworkingV1Api, evaluation: Evaluation) -> None:
    """Editing the policy's rules is repaired, and annotations it does not own are kept."""
    policy = _policy(networking_client, evaluation)
    drifted = {
        "metadata": {"annotations": {**policy.metadata.annotations, "k8s-tests/keep": "preserve"}},
        "spec": {"egress": None, "ingress": [{"from": [{"podSelector": {}}]}]},
    }
    networking_client.patch_namespaced_network_policy(
        name=evaluation.policy_name, namespace=evaluation.namespace, body=drifted
    )
    LOGGER.info("drifted %s/%s: %s", evaluation.namespace, evaluation.policy_name, drifted)

    repaired = lmeval.wait_for_policy_rules(networking_client, evaluation.namespace, evaluation.policy_name)
    assert (repaired.metadata.annotations or {}).get("k8s-tests/keep") == "preserve", (
        "the operator removed an annotation it does not own"
    )


@release(min=POLICY_MIN_VERSION)
def test_operator_recreates_a_deleted_policy(networking_client: client.NetworkingV1Api, evaluation: Evaluation) -> None:
    """Deleting the policy brings a new one, selected the same way, not the old one back."""
    deleted = _policy(networking_client, evaluation)
    networking_client.delete_namespaced_network_policy(name=evaluation.policy_name, namespace=evaluation.namespace)
    LOGGER.info("deleted %s/%s (%s)", evaluation.namespace, evaluation.policy_name, deleted.metadata.uid)

    replacement = lmeval.wait_for_policy(
        networking_client, evaluation.namespace, evaluation.uid, not_uid=deleted.metadata.uid
    )
    assert replacement.metadata.name == evaluation.policy_name, (
        f"the replacement is {replacement.metadata.name}, expected the same name"
    )
    assert replacement.spec.policy_types == ["Ingress", "Egress"], replacement.spec.policy_types
    assert len(replacement.spec.egress or []) == 1, replacement.spec.egress


@release(min=POLICY_MIN_VERSION)
def test_retained_completed_job_is_still_repaired(
    custom_api: client.CustomObjectsApi, core_client: client.CoreV1Api, evaluation: Evaluation
) -> None:
    """A finished job keeps its outcome, and still gets its Pod's identity back.

    This is the case the operator's own suite asserts and the one that regresses
    first: once a job is complete there is no polling interval left to fall back on,
    so the repair has to come from watching the Pod. Losing the label here means the
    retained workload is no longer selected by its policy, and nothing reports it.
    """
    completed = lmeval.mark_complete(custom_api, evaluation.namespace, evaluation.name)
    assert completed["status"]["state"] == lmeval.COMPLETE_STATE
    assert completed["status"]["results"] == "retained-result"

    pod = lmeval.read_pod(core_client, evaluation.namespace, evaluation.pod_name)
    expected = pod.metadata.labels[lmeval.COMPONENT_LABEL]
    lmeval.remove_label(core_client, evaluation.namespace, evaluation.pod_name, lmeval.COMPONENT_LABEL)
    lmeval.wait_for_label(core_client, evaluation.namespace, evaluation.pod_name, lmeval.COMPONENT_LABEL, expected)

    job = lmeval.read_job(custom_api, evaluation.namespace, evaluation.name)
    status = job["status"]
    assert status["state"] == lmeval.COMPLETE_STATE, f"the repair reset the job to {status['state']}"
    assert status["results"] == "retained-result", f"the repair replaced the results with {status['results']!r}"
    assert lmeval.condition_is_true(job, lmeval.NETWORK_POLICY_READY_CONDITION), (
        f"the job reports {lmeval.NETWORK_POLICY_READY_CONDITION}=False: {status.get('conditions')}"
    )
